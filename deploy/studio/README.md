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
subscription brain; the `claude -p` Opus strong-tier shim is dropped. The scoped
LiteLLM provider and `local-bulk` (Qwen3.6-35B-A3B-oQ6-mtp) heartbeat route are
configured, but a 2026-07-13 sanitized audit did not establish successful current
background traffic. Treat the heartbeat route as operationally unverified until a
fresh non-sensitive Rita canary passes; do not substitute it for the Codex strong
tier.

The heartbeat route is VERIFIED as of 2026-08-03: with the scoped
`RITA_LITELLM_KEY`, LiteLLM `:4000` served `local-bulk` a PONG at HTTP 200.
That supersedes the 2026-07-13 unverified caveat above.

## Upgrading Hermes

Rebase this deploy layer onto the upstream tag. Do NOT merge upstream into
the deploy branch: upstream refactors orphan in-tree patches (0.18.0 moved
`gateway/platforms/slack.py` to `plugins/platforms/slack/`, stranding two
fork patches that had lived there since May).

```bash
git fetch upstream --tags
git worktree add ../hermes-agent-<tag> -b studio-deploy-<tag> <tag>   # build here, Rita stays up
# cherry-pick the deploy commits, re-apply any local shim with `git apply -3`
# uv sync in the worktree first: proves the build and warms the cache
```

Rehearse the config migration on a COPY before touching the live one -
`hermes doctor --fix` is what migrates (a plain config load does not), the
migration is one-way, and it strips every YAML comment from `config.yaml`:

```bash
cp -a ~/.hermes /tmp/hermes-migtest
HERMES_HOME=/tmp/hermes-migtest venv/bin/hermes doctor --fix
```

Cutover: bootout both daemons, `tar -czf` `~/.hermes` (consistent only with
the gateway stopped), `mv venv venv-<oldver>-rollback`, checkout, `uv sync`,
`hermes doctor --fix`, `bash deploy/studio/rita-gateway.sh --check`, then
bootstrap. Verify Slack auth, MCP tool registration, and the LiteLLM route
before calling it done.

Pushing the fork requires `/infrastructure/GITHUB_PAT` over HTTPS. The
Studio's SSH deploy key is read-only on this repo and fails with
"Permission to jasonnickel/hermes-agent.git denied to deploy key".

## Local agent capability (user plugins)

Local tools live in `~/.hermes/plugins/`, NOT in this repo tree, so upstream
refactors cannot orphan them. `plugins/slack_history/` here is the
version-controlled copy; `~/.hermes/plugins/` is what actually loads.

```bash
cp -a deploy/studio/plugins/slack_history ~/.hermes/plugins/
hermes plugins enable slack_history
```

Both gates are required or the tools register but stay invisible to Rita:

- `plugins.enabled` must list `slack_history`
- `platform_toolsets.slack` must list `slack_history` alongside `hermes-slack`

`hermes config set` writes scalars, so setting `platform_toolsets.slack`
through it yields `slack: a,b` where every sibling is a block sequence.
Fix the YAML to list form by hand afterwards.

`slack_history` provides two read-only tools: `slack_history` (recent
messages in a channel, Block Kit aware) and `slack_channels` (readable
channels plus how long since each last received a message, which is how a
quiet alert channel is distinguished from a broken one). Neither posts nor
joins; a non-member channel returns `not_in_channel` with the `/invite`
fix rather than auto-joining.
