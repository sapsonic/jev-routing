# jev-routing

Routes Hermes skill selection through [Jev (TypeSafe)](https://typesafe.ai) against **every installed skill**.
Skills added later are included automatically; there is no allowlist to maintain.

## What gets installed

| Piece | Location on the instance | Purpose |
|---|---|---|
| Router | `~/.hermes/jev-router/jev_router.py` | One Jev call against the live skill registry (rescanned on every call) |
| MCP tool | `~/.hermes/jev-router/jev_router_mcp.py` | Exposes `jev_route_skill` so the model can call Jev itself |
| Plugin | `~/.hermes/plugins/jev-skill-router/` | Routes automatically on the first turn of every session, and re-routes when a cheap gate decides the topic changed |
| Skill | `~/.hermes/skills/mcp/jev-skill-routing/` | Tells the model how to use the MCP tool |
| Config | `~/.hermes/config.yaml` | Enables the plugin, allows the gate model, registers the MCP server |

## Requirements

- A Hermes instance (tested on v0.21.4).
- `TYPESAFE_API_KEY` in `~/.hermes/.env`.
- `OPENAI_API_KEY` in `~/.hermes/.env` (the reroute gate uses `openai-api` / `gpt-5.6-luna`; without it the plugin just calls Jev on every turn).

No secrets are stored in this repo.

## Install

```bash
git clone git@github.com:sapsonic/jev-routing.git
cd jev-routing
bash install.sh --dry-run   # preview
bash install.sh
# add TYPESAFE_API_KEY to ~/.hermes/.env if it is not there yet, then restart the agent
bash verify.sh
```

`install.sh` is idempotent. Changed files are backed up as `*.bak-<timestamp>` and `config.yaml` is backed up before any edit.
It never restarts the agent; restart from the dashboard (or `oc-remote restart`) so the plugin and MCP server load the new code.

## Update

```bash
cd jev-routing && git pull && bash install.sh && bash verify.sh   # then restart the agent
```

## Behaviour and tuning

- First turn of every session: Jev is always called. Later turns: a small gate model decides whether the topic changed.
- A specialized pick is applied only if confidence >= 0.75, the margin over the runner-up >= 0.15, and `needs_clarification` < 0.5. Otherwise the model is told to ask one clarifying question instead of guessing. Thresholds are `_MIN_ROUTE_CONFIDENCE`, `_MIN_ROUTE_MARGIN` and `_CLARIFICATION_THRESHOLD` in `plugin/jev-skill-router/__init__.py`.
- Fails open: if Jev is unreachable, times out, or the key is missing, normal Hermes skill selection is used.
- Privacy: only the current user message is sent to Jev (no history). Routing state is in memory only, never written to disk.

## Check it is working

```bash
grep 'jev-skill-router: session' ~/.hermes/logs/agent.log | tail
```

Expect `routed -> <skill> (scope=full, valid=True)`. `routing withheld ... low_confidence` is the confidence gate working, not an error.

## Layout

```
router/        jev_router.py, jev_router_mcp.py
plugin/        jev-skill-router/{plugin.yaml, __init__.py}
skill/         jev-skill-routing/SKILL.md
install.sh     idempotent installer
verify.sh      read-only post-install checks
```
