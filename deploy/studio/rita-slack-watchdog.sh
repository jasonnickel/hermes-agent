#!/bin/bash
# rita-slack-watchdog.sh - restart the Rita/Hermes gateway when its Slack Socket
# Mode session is dead. Ported from the openclaw-vm hermes-slack-watchdog
# (2026-07-09 Studio migration): systemd/journal -> launchd, GNU date/stat ->
# BSD (macOS).
#
# Failure shape (2026-07-08): a Slack API 503 during reconnect leaves slack_bolt
# with no session; the gateway stays "alive" (memory heartbeats) but is deaf and
# never self-recovers. Log timestamps are "YYYY-MM-DD HH:MM:SS,mmm" so a lexical
# compare of the first 23 chars is ordering-safe.
set -euo pipefail

readonly GATEWAY_LOG="${HOME}/.hermes/logs/gateway.log"
readonly ERRORS_LOG="${HOME}/.hermes/logs/errors.log"
readonly STAMP="${HOME}/.hermes/.slack-watchdog.laststamp"
readonly LABEL="com.jasonnickel.rita-gateway"
readonly COOLDOWN_SECS=1800    # do not flap: min 30 min between forced restarts
readonly SELF_RECOVER_SECS=600 # give slack_bolt 10 min to reconnect on its own

log() { /usr/bin/logger -t rita-slack-watchdog "$*"; printf '%s %s\n' "$(date -u +%FT%TZ)" "$*"; }

# BSD-date parse of a "YYYY-MM-DD HH:MM:SS" string to epoch; echo 0 on failure.
to_epoch() { date -j -f "%Y-%m-%d %H:%M:%S" "$1" +%s 2>/dev/null || echo 0; }

last_connect="$(grep -a "Socket Mode connected" "$GATEWAY_LOG" 2>/dev/null | tail -1 | cut -c1-23 || true)"
last_fail="$(grep -aE "Failed to retrieve WSS URL|reconnect to the server" "$ERRORS_LOG" 2>/dev/null | tail -1 | cut -c1-23 || true)"

# No failure marker at all -> healthy.
[[ -z "$last_fail" ]] && exit 0
# Connected more recently than the last failure -> healthy.
if [[ -n "$last_connect" && "$last_connect" > "$last_fail" ]]; then
  exit 0
fi

# Failure newer than any connect: give slack_bolt time to self-recover first.
fail_epoch="$(to_epoch "${last_fail%,*}")"
now="$(date +%s)"
(( now - fail_epoch < SELF_RECOVER_SECS )) && exit 0

# Cooldown guard against restart flapping.
if [[ -f "$STAMP" ]] && (( now - $(stat -f %m "$STAMP") < COOLDOWN_SECS )); then
  exit 0
fi

touch "$STAMP"
log "Slack socket dead since $last_fail (last connect: ${last_connect:-never}) - kickstarting $LABEL"
/bin/launchctl kickstart -k "system/${LABEL}"
