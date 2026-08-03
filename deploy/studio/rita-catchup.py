#!/usr/bin/env python3
"""Record what Rita missed while the gateway was down.

context7-verified: slack_sdk conversations_history / conversations_list usage
and the 429 Retry-After contract were checked against current
python-slack-sdk docs before this was written.

Runs from ``rita-gateway.sh`` immediately BEFORE the gateway starts, which
is exactly when the downtime window is knowable: everything newer than the
last message we recorded seeing arrived while she was offline.

Writes a brief to ``~/.hermes/slack-catchup-brief.json``. The
``slack_history`` plugin's llm_request middleware injects that brief into
Rita's context once, on her next real Slack turn, then marks it consumed.
Nothing is ever posted to Slack - this is silent, context-only catch-up
(owner decision 2026-08-03).

Fails soft by design. A catch-up problem must never stop Rita from booting,
so every failure path exits 0 without a brief.
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import sys
import time
from typing import Any, Dict, List, Optional

HERMES_HOME = pathlib.Path(os.environ.get("HERMES_HOME", pathlib.Path.home() / ".hermes"))
STATE_PATH = HERMES_HOME / "slack-catchup-state.json"
BRIEF_PATH = HERMES_HOME / "slack-catchup-brief.json"

# Bounds: a long outage must not produce an unbounded brief that blows out
# the context window on her first turn back.
MAX_MESSAGES_PER_CHANNEL = 25
MAX_AGE_SECONDS = 24 * 3600
MAX_TEXT_CHARS = 400
CHANNEL_PAGE_SIZE = 200
MAX_CHANNEL_PAGES = 5
RATE_LIMIT_RETRIES = 2

logging.basicConfig(
    level=logging.INFO,
    format="[rita-catchup] %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("rita-catchup")


def _load_json(path: pathlib.Path) -> Optional[dict]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _call(client: Any, method: str, **kwargs: Any) -> Any:
    from slack_sdk.errors import SlackApiError

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
            time.sleep(max(1, min(delay, 30)))
            attempt += 1


def _plugin_helpers():
    """Reuse the slack_history plugin's Block Kit and author rendering.

    Keeping one implementation means the brief reads exactly like what the
    ``slack_history`` tool returns, instead of drifting into a second,
    worse Slack renderer.
    """
    import importlib.util

    tools_path = HERMES_HOME / "plugins/slack_history/tools.py"
    spec = importlib.util.spec_from_file_location("_rita_slack_tools", tools_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {tools_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _self_identity(client: Any) -> Dict[str, str]:
    """Rita's own bot/user ids, so her own posts stay out of the brief.

    Her shutdown notices ("Gateway shutting down") land in the window she was
    going down, and echoing them back as things she "missed" is noise that
    crowds out what other people actually said.
    """
    try:
        who = _call(client, "auth_test")
        return {
            "user_id": str(who.get("user_id") or ""),
            "bot_id": str(who.get("bot_id") or ""),
        }
    except Exception:  # noqa: BLE001
        return {"user_id": "", "bot_id": ""}


def _is_self(message: Dict[str, Any], identity: Dict[str, str]) -> bool:
    user_id, bot_id = identity.get("user_id"), identity.get("bot_id")
    if user_id and message.get("user") == user_id:
        return True
    if bot_id and message.get("bot_id") == bot_id:
        return True
    return False


def _member_channels(client: Any) -> List[Dict[str, Any]]:
    channels: List[Dict[str, Any]] = []
    cursor: Optional[str] = None
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
            if channel.get("is_member") and channel.get("id"):
                channels.append({"id": channel["id"], "name": channel.get("name") or channel["id"]})
        cursor = (resp.get("response_metadata") or {}).get("next_cursor") or None
        if not cursor:
            break
    return channels


def main() -> int:
    token = os.environ.get("SLACK_BOT_TOKEN")
    if not token:
        logger.warning("SLACK_BOT_TOKEN absent; skipping catch-up")
        return 0

    try:
        from slack_sdk import WebClient

        helpers = _plugin_helpers()
        client = WebClient(token=token)
        identity = _self_identity(client)
        channels = _member_channels(client)
    except Exception as exc:  # noqa: BLE001 - must never block boot
        logger.warning("catch-up skipped (%s: %s)", type(exc).__name__, exc)
        return 0

    state = _load_json(STATE_PATH) or {}
    last_seen: Dict[str, str] = state.get("last_seen") or {}
    first_run = not last_seen

    now = time.time()
    floor_ts = now - MAX_AGE_SECONDS
    new_last_seen: Dict[str, str] = {}
    missed: List[Dict[str, Any]] = []
    total = 0

    for channel in channels:
        cid, name = channel["id"], channel["name"]
        try:
            resp = _call(client, "conversations_history", channel=cid, limit=MAX_MESSAGES_PER_CHANNEL)
            messages = list(resp.get("messages") or [])
        except Exception as exc:  # noqa: BLE001
            logger.warning("channel %s unreadable (%s)", name, type(exc).__name__)
            if cid in last_seen:
                new_last_seen[cid] = last_seen[cid]
            continue

        newest = messages[0].get("ts") if messages else last_seen.get(cid)
        if newest:
            new_last_seen[cid] = newest
        if first_run:
            continue

        since = last_seen.get(cid)
        fresh = []
        for message in messages:
            ts = message.get("ts")
            if not ts:
                continue
            try:
                ts_f = float(ts)
            except (TypeError, ValueError):
                continue
            if ts_f <= floor_ts:
                continue
            if since is not None and ts_f <= float(since):
                continue
            if _is_self(message, identity):
                continue
            fresh.append(message)

        if not fresh:
            continue

        fresh.reverse()  # chronological
        rendered = [
            {
                "time": helpers._iso(m.get("ts", "")),
                "age": helpers._age(m.get("ts", "")),
                "author": helpers._author(client, m),
                "text": helpers._message_text(client, m)[:MAX_TEXT_CHARS],
            }
            for m in fresh
        ]
        total += len(rendered)
        missed.append({"channel": name, "count": len(rendered), "messages": rendered})

    state_out = {"last_seen": new_last_seen, "updated_at": now}
    try:
        STATE_PATH.write_text(json.dumps(state_out, indent=2), encoding="utf-8")
    except OSError as exc:
        logger.warning("could not persist state: %s", exc)

    if first_run:
        logger.info("first run: seeded last-seen for %d channel(s), no brief", len(new_last_seen))
        return 0

    if not missed:
        # Remove any stale brief so she is never told about an old gap.
        BRIEF_PATH.unlink(missing_ok=True)
        logger.info("nothing missed across %d channel(s)", len(channels))
        return 0

    brief = {
        "generated_at": now,
        "downtime_since": state.get("updated_at"),
        "total_messages": total,
        "channels": missed,
    }
    try:
        BRIEF_PATH.write_text(json.dumps(brief, indent=2), encoding="utf-8")
        logger.info("brief written: %d message(s) across %d channel(s)", total, len(missed))
    except OSError as exc:
        logger.warning("could not write brief: %s", exc)
    return 0


if __name__ == "__main__":
    sys.exit(main())
