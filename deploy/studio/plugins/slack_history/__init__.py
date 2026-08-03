"""Slack channel-history plugin - user-installed, read-only.

Lives in ``~/.hermes/plugins/`` rather than the hermes-agent repo on
purpose. The 0.14 -> 0.20 upgrade orphaned a fork patch that lived in
``gateway/platforms/slack.py`` because upstream moved Slack into
``plugins/platforms/slack/``; anything carried in the repo tree is exposed
to that class of refactor. A user plugin is loaded through the documented
extension point and survives upgrades untouched.

Registers two tools into the ``slack_history`` toolset. Both gate on
``SLACK_BOT_TOKEN`` being present in the process environment, which
``deploy/studio/rita-gateway.sh`` exports from Infisical at launch.
"""

from __future__ import annotations

from .catchup import llm_request_middleware
from .tools import (
    SLACK_CHANNELS_SCHEMA,
    SLACK_HISTORY_SCHEMA,
    _check_slack_history_available,
    _handle_slack_channels,
    _handle_slack_history,
)

_TOOLS = (
    (
        "slack_history",
        SLACK_HISTORY_SCHEMA,
        _handle_slack_history,
        "💬",
        "Read the most recent messages in a Slack channel (default 10). Use when "
        "asked what has been posted in a channel, to review alerts or digests, or "
        "to check whether an expected notification actually arrived.",
    ),
    (
        "slack_channels",
        SLACK_CHANNELS_SCHEMA,
        _handle_slack_channels,
        "📋",
        "List the Slack channels Rita can read, with how long ago each last "
        "received a message. Use to find the right channel name, or to spot an "
        "alert channel that has gone silent.",
    ),
)


def register(ctx) -> None:
    """Register the Slack history tools. Called once by the plugin loader."""
    for name, schema, handler, emoji, description in _TOOLS:
        ctx.register_tool(
            name=name,
            toolset="slack_history",
            schema=schema,
            handler=handler,
            check_fn=_check_slack_history_available,
            description=description,
            emoji=emoji,
        )

    # Silent catch-up: injects what she missed while the gateway was down
    # into her context once, on her next real Slack turn. Never posts.
    ctx.register_middleware("llm_request", llm_request_middleware)
