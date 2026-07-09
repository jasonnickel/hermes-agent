# Rita / Hermes gateway - Mac Studio deployment

Deployment artifacts for running the Rita (Hermes Agent) gateway on the Mac
Studio (`jasonnickel-mac-studio`). Migrated from the retired `openclaw-vm`
(Debian, N150 mini-PC) on 2026-07-09.

## Layout

| Path | Role |
|------|------|
| `~/Developer/hermes-agent/` | Hermes code (this repo, Jason's fork), venv at `venv/` |
| `~/.hermes/` | Runtime state: `config.yaml`, `SOUL.md`, `auth.json`, `memories/`, `cron/`, `kanban.db`, `scripts/`, `rita.env` |
| `deploy/studio/rita-gateway.sh` | Launch wrapper: env + Infisical secrets + `exec` gateway |
| `deploy/studio/rita.env` | **Non-secret** runtime config (installed to `~/.hermes/rita.env`) |
| `deploy/studio/rita-slack-watchdog.sh` | Slack Socket Mode self-heal (kickstarts the gateway when the socket is dead) |
| `deploy/studio/com.jasonnickel.rita-gateway.plist` | LaunchDaemon (KeepAlive, RunAtLoad) |
| `deploy/studio/com.jasonnickel.rita-slack-watchdog.plist` | LaunchDaemon (StartInterval 300s) |

## Secrets

No plaintext secrets on disk. `rita-gateway.sh` fetches every secret from
Infisical at launch (`~/.claude/scripts/infisical-get.sh`) and exports it under
the env var name Hermes expects. The map is the `SECRET_MAP` block in
`rita-gateway.sh` (single source of truth). Pre-flight check:

```bash
~/Developer/hermes-agent/deploy/studio/rita-gateway.sh --check
```

## Install / update

```bash
# 1. Install the non-secret config + validate secrets resolve
cp deploy/studio/rita.env ~/.hermes/rita.env
deploy/studio/rita-gateway.sh --check

# 2. Install both LaunchDaemons (system domain -> survives headless boot)
sudo cp deploy/studio/com.jasonnickel.rita-gateway.plist /Library/LaunchDaemons/
sudo cp deploy/studio/com.jasonnickel.rita-slack-watchdog.plist /Library/LaunchDaemons/
sudo launchctl bootstrap system /Library/LaunchDaemons/com.jasonnickel.rita-gateway.plist
sudo launchctl bootstrap system /Library/LaunchDaemons/com.jasonnickel.rita-slack-watchdog.plist
```

Reload after a code/config change:

```bash
sudo launchctl kickstart -k system/com.jasonnickel.rita-gateway
```

## Why LaunchDaemon, not LaunchAgent

The gateway must survive a headless reboot (no GUI login). System
LaunchDaemons with `UserName jasonnickel-mac-studio` run at boot yet still read
the user's `~/.hermes`, `~/.claude/.credentials.json`, and Infisical auth - the
same proven pattern as `com.jasonnickel.alert-watchdog` and the
`com.mycelium.*` fleet. See `~/.claude/ops/launchd/README.md`.

## Retirement path

```bash
sudo launchctl bootout system/com.jasonnickel.rita-gateway
sudo launchctl bootout system/com.jasonnickel.rita-slack-watchdog
sudo rm /Library/LaunchDaemons/com.jasonnickel.rita-gateway.plist
sudo rm /Library/LaunchDaemons/com.jasonnickel.rita-slack-watchdog.plist
```

Then remove the entries from `~/.claude/ops/launchd/fleet-catalog.yaml` and the
Rita card from `~/.claude/LOOP-REGISTRY.md`.

## Model routing

Strong tier and heartbeat tier are configured in `~/.hermes/config.yaml` /
`auth.json`. Decision record: corpus doc "Rita / Hermes - Model Routing
Recommendation (2026-07-09)". Cutover shipped on the incumbent GPT-5.5/Codex
subscription brain; the `claude -p` Opus strong-tier shim and `local-bulk`
(Qwen3.6-35B) heartbeat tier are wired post-cutover.
