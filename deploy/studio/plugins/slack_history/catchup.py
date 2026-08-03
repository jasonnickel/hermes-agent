"""Silent catch-up injection for Rita.

``deploy/studio/rita-catchup.py`` runs just before the gateway starts and
writes ``~/.hermes/slack-catchup-brief.json`` describing what arrived while
she was offline. This module injects that brief into her context exactly
once, on her next real Slack turn, then marks it consumed.

Owner decision 2026-08-03: catch-up is SILENT. Rita never posts about the
gap unprompted; she simply knows what happened so she answers correctly
when asked. Do not turn this into an announcement without asking.

Failure policy: this runs on the LLM request path for a live assistant, so
every path is wrapped and returns ``None`` (leave the payload untouched) on
any problem. A broken catch-up must never break Rita's ability to answer.
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import time
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

HERMES_HOME = pathlib.Path(os.environ.get("HERMES_HOME", pathlib.Path.home() / ".hermes"))
BRIEF_PATH = HERMES_HOME / "slack-catchup-brief.json"

# Past this age the gap is history, not news - never surface it as catch-up.
BRIEF_TTL_SECONDS = 12 * 3600
# Hard cap on injected characters so a noisy outage cannot crowd out the
# actual conversation.
MAX_INJECTED_CHARS = 6000


def _load_brief() -> Optional[Dict[str, Any]]:
    try:
        if not BRIEF_PATH.exists():
            return None
        brief = json.loads(BRIEF_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(brief, dict) or brief.get("consumed_at"):
        return None
    generated = brief.get("generated_at")
    if not isinstance(generated, (int, float)):
        return None
    if (time.time() - float(generated)) > BRIEF_TTL_SECONDS:
        return None
    if not brief.get("channels"):
        return None
    return brief


def _mark_consumed(brief: Dict[str, Any]) -> None:
    try:
        brief["consumed_at"] = time.time()
        BRIEF_PATH.write_text(json.dumps(brief, indent=2), encoding="utf-8")
    except OSError as exc:
        logger.debug("could not mark catch-up brief consumed: %s", exc)


def _render(brief: Dict[str, Any]) -> str:
    since = brief.get("downtime_since")
    when = ""
    if isinstance(since, (int, float)):
        from datetime import datetime, timezone

        stamp = (
            datetime.fromtimestamp(float(since), tz=timezone.utc)
            .astimezone()
            .isoformat(timespec="minutes")
        )
        when = f" You were offline since {stamp}."

    lines = [
        f"[Catch-up] While the gateway was down, {brief.get('total_messages', 0)} "
        f"Slack message(s) arrived in channels you follow.{when}",
        "This is context only - do NOT announce it or reply to these messages. "
        "Use it to answer accurately if Jason asks what he missed. "
        "For anything beyond this window use the slack_history tool.",
    ]
    for channel in brief.get("channels") or []:
        lines.append(f"\n#{channel.get('channel')} ({channel.get('count')} message(s)):")
        for message in channel.get("messages") or []:
            text = " / ".join(str(message.get("text") or "").splitlines())
            lines.append(f"  [{message.get('age')}] {message.get('author')}: {text}")

    rendered = "\n".join(lines)
    if len(rendered) > MAX_INJECTED_CHARS:
        rendered = rendered[:MAX_INJECTED_CHARS] + "\n  ... (truncated; use slack_history for the rest)"
    return rendered


def llm_request_middleware(**kwargs: Any) -> Optional[Dict[str, Any]]:
    """Inject the catch-up brief once, on the next real Slack turn.

    Returns ``{"request": ...}`` to replace the provider kwargs, or None to
    leave them untouched.
    """
    try:
        # Only a genuine Slack conversation should consume the brief. A cron
        # or internal turn would burn it where Jason never sees the benefit.
        if (kwargs.get("platform") or "") != "slack":
            return None

        request = kwargs.get("request")
        if not isinstance(request, dict):
            return None
        messages = request.get("messages")
        if not isinstance(messages, list) or not messages:
            return None

        brief = _load_brief()
        if brief is None:
            return None

        note = {"role": "system", "content": _render(brief)}

        # Place it after any leading system messages so it reads as current
        # state rather than displacing the persona.
        index = 0
        for i, message in enumerate(messages):
            if isinstance(message, dict) and message.get("role") == "system":
                index = i + 1
            else:
                break

        updated = dict(request)
        updated["messages"] = messages[:index] + [note] + messages[index:]

        _mark_consumed(brief)
        logger.info(
            "catch-up brief injected (%s message(s), %s channel(s)) and marked consumed",
            brief.get("total_messages"), len(brief.get("channels") or []),
        )
        return {"request": updated}
    except Exception as exc:  # noqa: BLE001 - never break the LLM path
        logger.warning("catch-up injection skipped (%s: %s)", type(exc).__name__, exc)
        return None
