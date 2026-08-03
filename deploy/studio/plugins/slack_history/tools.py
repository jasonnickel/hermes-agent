"""Slack channel-history tools for Rita.

context7-verified: slack_sdk conversations_history / conversations_list
signatures and the 429 Retry-After contract were checked against current
python-slack-sdk docs before this was written.

Why this exists
---------------
Hermes ships no way for the agent to READ a Slack channel it is not being
spoken to in. Rita receives alerts and digests across several dedicated
channels (``#alerts-mycelium``, ``#mycelium-daily``, ...), and the useful
question is usually about what HAS or HAS NOT arrived there. These tools let
her pull the recent window on demand.

Design notes
------------
- Read-only. Nothing here posts, joins, or mutates Slack state. Joining a
  public channel is a visible side effect in someone else's workspace view,
  so a non-member channel reports ``not_in_channel`` with the fix rather
  than silently joining.
- Block Kit aware. Jason's notification surfaces post Block Kit payloads
  where the top-level ``text`` is frequently empty; naive readers show
  blank rows. ``_message_text`` walks blocks and attachments so digests are
  actually legible.
- Silence is a signal. ``slack_channels`` reports each channel's age since
  last message, because "no alerts fired" is exactly as informative as an
  alert, and is invisible if you only ever read message bodies.
"""

from __future__ import annotations

import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from tools.registry import tool_error, tool_result

logger = logging.getLogger(__name__)

# Slack conversations.history caps at 1000; 100 is well past anything a
# human asks to eyeball and keeps a single turn's context affordable.
DEFAULT_LIMIT = 10
MAX_LIMIT = 100
# conversations.list paging - one page covers this workspace comfortably.
CHANNEL_PAGE_SIZE = 200
MAX_CHANNEL_PAGES = 5
# A 429 during an interactive read should retry once, not unwind the turn.
RATE_LIMIT_RETRIES = 2

_USER_CACHE: Dict[str, str] = {}
_CHANNEL_CACHE: Dict[str, str] = {}
_CHANNEL_CACHE_AT = 0.0
_CHANNEL_CACHE_TTL = 300.0

_MENTION_RE = re.compile(r"<@([UW][A-Z0-9]+)(\|[^>]*)?>")


def _check_slack_history_available() -> bool:
    """Tools stay registered but undispatchable without a bot token."""
    return bool(os.environ.get("SLACK_BOT_TOKEN"))


def _client() -> WebClient:
    token = os.environ.get("SLACK_BOT_TOKEN")
    if not token:
        raise RuntimeError("SLACK_BOT_TOKEN is not set in this process")
    return WebClient(token=token)


def _call(client: WebClient, method: str, **kwargs: Any) -> Any:
    """Invoke a Web API method, absorbing rate limits with Retry-After.

    slack_sdk's built-in retry handlers are not attached by default on a
    bare WebClient, so honour the documented 429 contract explicitly.
    """
    attempt = 0
    while True:
        try:
            return getattr(client, method)(**kwargs)
        except SlackApiError as exc:
            status = getattr(exc.response, "status_code", None)
            if status != 429 or attempt >= RATE_LIMIT_RETRIES:
                raise
            raw = (exc.response.headers or {}).get("Retry-After", "1")
            try:
                delay = int(raw)
            except (TypeError, ValueError):
                delay = 1
            logger.info("Slack rate limited on %s; retrying in %ss", method, delay)
            time.sleep(max(1, min(delay, 30)))
            attempt += 1


def _resolve_user(client: WebClient, user_id: str) -> str:
    if not user_id:
        return "unknown"
    if user_id in _USER_CACHE:
        return _USER_CACHE[user_id]
    try:
        info = _call(client, "users_info", user=user_id)
        profile = info["user"].get("profile") or {}
        name = (
            profile.get("display_name")
            or profile.get("real_name")
            or info["user"].get("name")
            or user_id
        )
    except SlackApiError:
        name = user_id
    _USER_CACHE[user_id] = name
    return name


def _channel_index(client: WebClient, *, force: bool = False) -> Dict[str, str]:
    """Map channel name -> id, cached briefly (names change rarely)."""
    global _CHANNEL_CACHE_AT
    if not force and _CHANNEL_CACHE and (time.time() - _CHANNEL_CACHE_AT) < _CHANNEL_CACHE_TTL:
        return _CHANNEL_CACHE

    index: Dict[str, str] = {}
    cursor: Optional[str] = None
    for _ in range(MAX_CHANNEL_PAGES):
        resp = _call(
            client,
            "conversations_list",
            types="public_channel,private_channel",
            exclude_archived=True,
            limit=CHANNEL_PAGE_SIZE,
            cursor=cursor,
        )
        for channel in resp.get("channels") or []:
            if channel.get("name"):
                index[channel["name"]] = channel["id"]
        cursor = (resp.get("response_metadata") or {}).get("next_cursor") or None
        if not cursor:
            break

    _CHANNEL_CACHE.clear()
    _CHANNEL_CACHE.update(index)
    _CHANNEL_CACHE_AT = time.time()
    return _CHANNEL_CACHE


def _resolve_channel(client: WebClient, channel: str) -> str:
    """Accept '#name', 'name', or a raw channel ID."""
    value = (channel or "").strip().lstrip("#")
    if not value:
        raise ValueError("channel is required")
    # Channel IDs are C/G/D + uppercase alphanumerics; names are lowercase.
    if re.fullmatch(r"[CGD][A-Z0-9]{5,}", value):
        return value
    index = _channel_index(client)
    if value in index:
        return index[value]
    index = _channel_index(client, force=True)
    if value in index:
        return index[value]
    raise ValueError(
        f"channel {channel!r} not found among channels this bot can see "
        f"(it may be private and un-invited, or archived)"
    )


def _block_text(node: Any, out: List[str]) -> None:
    """Recursively pull human-readable text out of a Block Kit payload."""
    if isinstance(node, dict):
        # A {"type": "text"|"mrkdwn", "text": ...} leaf.
        text = node.get("text")
        if isinstance(text, str) and text.strip():
            out.append(text.strip())
        elif isinstance(text, dict):
            _block_text(text, out)
        for key in ("fields", "elements", "blocks", "attachments"):
            _block_text(node.get(key), out)
        # Image blocks carry only alt text.
        alt = node.get("alt_text")
        if isinstance(alt, str) and alt.strip() and not node.get("text"):
            out.append(f"[image: {alt.strip()}]")
    elif isinstance(node, list):
        for item in node:
            _block_text(item, out)


def _message_text(client: WebClient, message: Dict[str, Any]) -> str:
    """Best-effort readable body: text, else Block Kit, else attachments."""
    parts: List[str] = []
    top = message.get("text")
    if isinstance(top, str) and top.strip():
        parts.append(top.strip())
    if not parts:
        _block_text(message.get("blocks"), parts)
    if not parts:
        for attachment in message.get("attachments") or []:
            for key in ("text", "fallback", "title", "pretext"):
                value = attachment.get(key)
                if isinstance(value, str) and value.strip():
                    parts.append(value.strip())
                    break
            _block_text(attachment.get("blocks"), parts)

    body = "\n".join(dict.fromkeys(parts))  # de-dupe, preserve order
    if not body:
        subtype = message.get("subtype") or message.get("type") or "message"
        return f"[no renderable text; subtype={subtype}]"

    def _sub(match: re.Match[str]) -> str:
        return "@" + _resolve_user(client, match.group(1))

    return _MENTION_RE.sub(_sub, body)


def _iso(ts: str) -> str:
    try:
        return (
            datetime.fromtimestamp(float(ts), tz=timezone.utc)
            .astimezone()
            .isoformat(timespec="seconds")
        )
    except (TypeError, ValueError):
        return ts or ""


def _age(ts: str) -> str:
    try:
        delta = time.time() - float(ts)
    except (TypeError, ValueError):
        return "unknown"
    if delta < 90:
        return f"{int(delta)}s ago"
    if delta < 5400:
        return f"{int(delta // 60)}m ago"
    if delta < 172800:
        return f"{int(delta // 3600)}h ago"
    return f"{int(delta // 86400)}d ago"


def _author(client: WebClient, message: Dict[str, Any]) -> str:
    if message.get("user"):
        return _resolve_user(client, message["user"])
    # Bot posts (notify_job_failure.py, digests) carry bot_id, not user.
    return message.get("username") or message.get("bot_id") or "bot"


SLACK_HISTORY_SCHEMA = {
    "type": "object",
    "properties": {
        "channel": {
            "type": "string",
            "description": (
                "Channel to read: '#alerts-mycelium', 'alerts-mycelium', or a "
                "channel ID like 'C0AL98E7UMD'."
            ),
        },
        "limit": {
            "type": "integer",
            "description": (
                f"How many of the most recent messages to return. "
                f"Default {DEFAULT_LIMIT}, maximum {MAX_LIMIT}."
            ),
        },
        "oldest_first": {
            "type": "boolean",
            "description": (
                "Return the window in chronological order (oldest first). "
                "Defaults to true, which reads naturally for a digest."
            ),
        },
    },
    "required": ["channel"],
}


def _handle_slack_history(channel: str = "", limit: Any = DEFAULT_LIMIT, oldest_first: Any = True, **_: Any) -> str:
    try:
        client = _client()
    except RuntimeError as exc:
        return tool_error(str(exc))

    try:
        count = int(limit)
    except (TypeError, ValueError):
        count = DEFAULT_LIMIT
    count = max(1, min(count, MAX_LIMIT))

    try:
        channel_id = _resolve_channel(client, channel)
    except ValueError as exc:
        return tool_error(str(exc))
    except SlackApiError as exc:
        return tool_error(f"Slack API error resolving channel: {exc.response.get('error', exc)}")

    try:
        resp = _call(client, "conversations_history", channel=channel_id, limit=count)
    except SlackApiError as exc:
        code = exc.response.get("error", "unknown_error")
        if code == "not_in_channel":
            return tool_error(
                f"Rita is not a member of {channel!r} ({channel_id}), so Slack will not "
                f"serve its history. Invite the bot with '/invite @rita3' in that channel "
                f"(deliberately not auto-joining - that is a visible action in the workspace)."
            )
        if code == "channel_not_found":
            return tool_error(f"Channel {channel!r} not found or not visible to this bot.")
        return tool_error(f"Slack API error reading history: {code}")

    messages = list(resp.get("messages") or [])
    # conversations.history returns newest-first.
    if _coerce_bool(oldest_first, True):
        messages.reverse()

    rendered = [
        {
            "ts": message.get("ts"),
            "time": _iso(message.get("ts", "")),
            "age": _age(message.get("ts", "")),
            "author": _author(client, message),
            "text": _message_text(client, message),
            "thread_replies": message.get("reply_count", 0),
        }
        for message in messages
    ]

    name = (channel or "").lstrip("#")
    if not rendered:
        return tool_result(
            {
                "channel": name,
                "channel_id": channel_id,
                "message_count": 0,
                "messages": [],
                "note": "No messages in this channel's recent history - it is silent.",
            }
        )

    return tool_result(
        {
            "channel": name,
            "channel_id": channel_id,
            "message_count": len(rendered),
            "newest": rendered[-1]["age"] if _coerce_bool(oldest_first, True) else rendered[0]["age"],
            "messages": rendered,
        }
    )


def _coerce_bool(raw: Any, default: bool = False) -> bool:
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        cleaned = raw.strip().lower()
        if cleaned in {"1", "true", "yes", "on"}:
            return True
        if cleaned in {"0", "false", "no", "off"}:
            return False
    return default


SLACK_CHANNELS_SCHEMA = {
    "type": "object",
    "properties": {
        "member_only": {
            "type": "boolean",
            "description": (
                "Only list channels Rita has joined - these are the ones whose "
                "history she can actually read. Defaults to true."
            ),
        },
        "with_activity": {
            "type": "boolean",
            "description": (
                "Include how long ago each channel last received a message, which "
                "is how you tell a quiet alert channel from a broken one. Costs one "
                "extra API call per channel. Defaults to true."
            ),
        },
    },
    "required": [],
}


def _handle_slack_channels(member_only: Any = True, with_activity: Any = True, **_: Any) -> str:
    try:
        client = _client()
    except RuntimeError as exc:
        return tool_error(str(exc))

    only_member = _coerce_bool(member_only, True)
    want_activity = _coerce_bool(with_activity, True)

    channels: List[Dict[str, Any]] = []
    cursor: Optional[str] = None
    try:
        for _page in range(MAX_CHANNEL_PAGES):
            resp = _call(
                client,
                "conversations_list",
                types="public_channel,private_channel",
                exclude_archived=True,
                limit=CHANNEL_PAGE_SIZE,
                cursor=cursor,
            )
            for channel in resp.get("channels") or []:
                if only_member and not channel.get("is_member"):
                    continue
                channels.append(
                    {
                        "name": channel.get("name"),
                        "id": channel.get("id"),
                        "is_member": bool(channel.get("is_member")),
                        "is_private": bool(channel.get("is_private")),
                        "purpose": ((channel.get("purpose") or {}).get("value") or "")[:160],
                    }
                )
            cursor = (resp.get("response_metadata") or {}).get("next_cursor") or None
            if not cursor:
                break
    except SlackApiError as exc:
        return tool_error(f"Slack API error listing channels: {exc.response.get('error', exc)}")

    if want_activity:
        for entry in channels:
            if not entry["is_member"]:
                entry["last_activity"] = "unreadable (not a member)"
                continue
            try:
                recent = _call(client, "conversations_history", channel=entry["id"], limit=1)
                messages = recent.get("messages") or []
                entry["last_activity"] = _age(messages[0]["ts"]) if messages else "never"
            except SlackApiError as exc:
                entry["last_activity"] = f"unreadable ({exc.response.get('error', 'error')})"

    channels.sort(key=lambda c: c["name"] or "")
    return tool_result({"channel_count": len(channels), "channels": channels})
