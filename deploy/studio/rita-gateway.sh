#!/bin/bash
# rita-gateway.sh - launch wrapper for the Rita/Hermes agent gateway on the Mac
# Studio. Invoked by the com.jasonnickel.rita-gateway LaunchDaemon.
#
# Responsibilities (best-practice, no plaintext secrets on disk):
#   1. Establish a deterministic environment (HOME/PATH/HERMES_HOME/venv).
#   2. Source ~/.claude/claude-oauth.env so a future `claude -p` strong-tier
#      brain has warm subscription OAuth even under launchd (lesson #443).
#   3. Source the non-secret ~/.hermes/rita.env config.
#   4. Fetch every secret fresh from Infisical and export under the env var
#      name Hermes expects. Nothing is written to disk.
#   5. exec the gateway (replacing this shell so signals reach Hermes).
#
# Modes:
#   rita-gateway.sh            run the gateway (default; used by launchd)
#   rita-gateway.sh --check    verify every Infisical secret resolves, then exit
#                              0 (all good) or 1 (one or more missing). Run this
#                              before (re)installing the daemon.
set -euo pipefail

readonly HERMES_HOME_DIR="${HOME}/.hermes"
readonly HERMES_CODE_DIR="${HOME}/Developer/hermes-agent"
readonly VENV_DIR="${HERMES_CODE_DIR}/venv"
readonly INFISICAL_GET="${HOME}/.claude/scripts/infisical-get.sh"
readonly OAUTH_ENV="${HOME}/.claude/claude-oauth.env"
readonly RITA_ENV="${HERMES_HOME_DIR}/rita.env"

# Map of Hermes-expected env var name -> Infisical full path. One per line.
# Keep this list as the single source of truth for Rita's secret surface.
readonly SECRET_MAP="\
SLACK_BOT_TOKEN=/internal/SLACK_BOT_TOKEN
SLACK_APP_TOKEN=/internal/SLACK_APP_TOKEN
SLACK_SIGNING_SECRET=/internal/SLACK_SIGNING_SECRET
MCP_SEARCH_CORPUS_API_KEY=/internal/RITA_CORPUS_MCP_API_KEY
MCP_MEM0_API_KEY=/internal/MEM0_MCP_API_KEY
HERMES_GATEWAY_TOKEN=/internal/OPENCLAW_GATEWAY_TOKEN
BRAVE_SEARCH_API_KEY=/services/BRAVE_SEARCH_API_KEY
HOME_ASSISTANT_TOKEN=/services/HOMEASSISTANT_TOKEN
OPENROUTER_API_KEY=/llm-keys/OPENROUTER_API_KEY
OPENAI_API_KEY=/llm-keys/OPENAI_API_KEY_OPENCLAW
OPENCLAW_GITHUB_PAT=/credentials/OPENCLAW_GITHUB_PAT
GH_TOKEN=/credentials/OPENCLAW_GITHUB_PAT
RITA_LITELLM_KEY=/internal/RITA_LITELLM_KEY"

log() { printf '[rita-gateway] %s\n' "$*" >&2; }
die() { log "FATAL: $*"; exit 1; }

# Fetch one Infisical path; fail loud on empty/missing.
fetch() {
  local path="$1" val
  val="$("$INFISICAL_GET" "$path" 2>/dev/null)" || return 1
  [[ -n "$val" ]] || return 1
  printf '%s' "$val"
}

load_secrets() {
  local missing=0 name path val
  while IFS='=' read -r name path; do
    [[ -z "$name" ]] && continue
    if val="$(fetch "$path")"; then
      export "$name=$val"
      [[ "${1:-}" == "--report" ]] && log "OK   $name  <- $path"
    else
      missing=$((missing + 1))
      log "MISS $name  <- $path"
    fi
  done <<< "$SECRET_MAP"
  return $missing
}

main() {
  [[ -x "$INFISICAL_GET" ]] || die "infisical-get.sh not found/executable at $INFISICAL_GET"
  [[ -d "$VENV_DIR" ]] || die "venv missing at $VENV_DIR"

  export HOME HERMES_HOME="$HERMES_HOME_DIR" VIRTUAL_ENV="$VENV_DIR"
  export PATH="${VENV_DIR}/bin:/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

  # Warm claude subscription OAuth (belt-and-suspenders; lesson #443).
  if [[ -f "$OAUTH_ENV" ]]; then
    set -a; # shellcheck disable=SC1090
    source "$OAUTH_ENV"; set +a
  else
    log "warn: $OAUTH_ENV missing (claude -p strong tier would 401 until present)"
  fi

  # Non-secret config.
  [[ -f "$RITA_ENV" ]] || die "rita.env missing at $RITA_ENV"
  set -a; # shellcheck disable=SC1090
  source "$RITA_ENV"; set +a

  if [[ "${1:-}" == "--check" ]]; then
    log "Infisical secret pre-flight:"
    if load_secrets --report; then
      log "PASS: all secrets resolved."
      exit 0
    else
      die "one or more secrets did not resolve (see MISS lines above)."
    fi
  fi

  load_secrets || die "aborting start: Infisical secrets missing."

  cd "$HERMES_CODE_DIR"
  log "starting Hermes gateway (HERMES_HOME=$HERMES_HOME)"
  exec "${VENV_DIR}/bin/python" -m hermes_cli.main gateway run --replace
}

main "$@"
