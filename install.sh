#!/usr/bin/env bash
# Install the Jev skill router (plugin + MCP tool + routing skill) on a Hermes instance.
#
# Idempotent: safe to re-run. Existing files that differ are backed up first.
# Never writes secrets. See README.md.
#
#   bash install.sh             install / update
#   bash install.sh --dry-run   show what would change, change nothing
set -euo pipefail

HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TS="$(date +%Y%m%d-%H%M%S)"
DRY_RUN=0

for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

die() { echo "ERROR: $*" >&2; exit 1; }
if [ "$DRY_RUN" = 1 ]; then V_INSTALLED="would install"; V_BACKUP="would back up"; V_CREATED="would create"; else V_INSTALLED="installed"; V_BACKUP="backed up"; V_CREATED="created"; fi
run() { if [ "$DRY_RUN" = 1 ]; then echo "  [dry-run] $*"; else "$@"; fi; }

echo "Jev skill router installer  (HERMES_HOME=$HERMES_HOME, dry-run=$DRY_RUN)"

# --- 1. preflight -----------------------------------------------------------
[ -d "$HERMES_HOME" ] || die "$HERMES_HOME not found"
[ -f "$HERMES_HOME/config.yaml" ] || die "$HERMES_HOME/config.yaml not found"

HPY=""
for c in /usr/local/lib/hermes/hermes-agent/venv/bin/python "$(command -v python3 || true)"; do
  if [ -n "$c" ] && [ -x "$c" ] && "$c" -c 'import yaml, dotenv' 2>/dev/null; then HPY="$c"; break; fi
done
[ -n "$HPY" ] || die "need a python with PyYAML and python-dotenv (the Hermes venv has both)"

command -v hermes >/dev/null 2>&1 && echo "hermes: $(hermes --version 2>/dev/null | head -n 1)"

has_env_key() {
  HERMES_HOME="$HERMES_HOME" "$HPY" - "$1" <<'PY'
import os, sys
from dotenv import dotenv_values
v = dotenv_values(os.path.join(os.environ["HERMES_HOME"], ".env"))
sys.exit(0 if (v.get(sys.argv[1]) or "").strip() else 1)
PY
}

echo "== credentials (presence only) =="
if has_env_key TYPESAFE_API_KEY; then echo "  TYPESAFE_API_KEY: set"; else
  echo "  TYPESAFE_API_KEY: MISSING - add it to $HERMES_HOME/.env before restarting (routing is skipped until then)"; fi
if has_env_key OPENAI_API_KEY; then echo "  OPENAI_API_KEY:   set"; else
  echo "  OPENAI_API_KEY:   MISSING - the reroute gate uses openai-api/gpt-5.6-luna; without it the plugin"
  echo "                    falls back to calling Jev on every turn (works, but more calls)"; fi

# --- 2. files ---------------------------------------------------------------
install_file() {
  local src="$1" dst="$2"
  if [ -f "$dst" ] && cmp -s "$src" "$dst"; then echo "  unchanged  $dst"; return; fi
  if [ -f "$dst" ]; then run cp -p "$dst" "$dst.bak-$TS"; echo "  $V_BACKUP  $dst -> $(basename "$dst").bak-$TS"; fi
  run mkdir -p "$(dirname "$dst")"
  run cp "$src" "$dst"
  echo "  $V_INSTALLED  $dst"
}

echo "== files =="
install_file "$HERE/router/jev_router.py"                   "$HERMES_HOME/jev-router/jev_router.py"
install_file "$HERE/router/jev_router_mcp.py"               "$HERMES_HOME/jev-router/jev_router_mcp.py"
install_file "$HERE/plugin/jev-skill-router/plugin.yaml"    "$HERMES_HOME/plugins/jev-skill-router/plugin.yaml"
install_file "$HERE/plugin/jev-skill-router/__init__.py"    "$HERMES_HOME/plugins/jev-skill-router/__init__.py"
install_file "$HERE/skill/jev-skill-routing/SKILL.md"       "$HERMES_HOME/skills/mcp/jev-skill-routing/SKILL.md"

# --- 3. venv for the MCP server (mcp<2: v2 removed FastMCP) ------------------
echo "== router venv =="
VENV="$HERMES_HOME/jev-router/.venv"
if [ ! -x "$VENV/bin/python" ]; then
  if command -v uv >/dev/null 2>&1; then run uv venv -q "$VENV"; else run python3 -m venv "$VENV"; fi
  echo "  $V_CREATED  $VENV"
else
  echo "  exists     $VENV"
fi
if [ "$DRY_RUN" = 1 ]; then
  echo "  [dry-run] would ensure 'mcp<2' is importable as mcp.server.fastmcp"
else
  if ! "$VENV/bin/python" -c 'from mcp.server.fastmcp import FastMCP' 2>/dev/null; then
    if command -v uv >/dev/null 2>&1; then uv pip install -q --python "$VENV/bin/python" 'mcp<2'
    else "$VENV/bin/python" -m pip install -q 'mcp<2'; fi
  fi
  "$VENV/bin/python" -c 'from mcp.server.fastmcp import FastMCP' || die "mcp.server.fastmcp not importable in $VENV"
  echo "  ok         mcp.server.fastmcp importable"
fi

# --- 4. config.yaml (idempotent; backup before any change) -------------------
echo "== config.yaml =="
HERMES_HOME="$HERMES_HOME" DRY_RUN="$DRY_RUN" TS="$TS" "$HPY" - <<'PY'
import copy, os, sys
from pathlib import Path
import yaml

home = Path(os.environ["HERMES_HOME"])
cfg = home / "config.yaml"
dry = os.environ["DRY_RUN"] == "1"
orig = yaml.safe_load(cfg.read_text()) or {}
d = copy.deepcopy(orig)

def sub(parent, key, default):
    if not isinstance(parent.get(key), type(default)):
        parent[key] = default
    return parent[key]

plugins = sub(d, "plugins", {})
enabled = sub(plugins, "enabled", [])
if "jev-skill-router" not in enabled:
    enabled.append("jev-skill-router")
llm = sub(sub(sub(plugins, "entries", {}), "jev-skill-router", {}), "llm", {})
llm.setdefault("allow_provider_override", True)
llm.setdefault("allow_model_override", True)
for key, wanted in (("allowed_providers", ["openai-api"]), ("allowed_models", ["gpt-5.6-luna"])):
    cur = sub(llm, key, [])
    for item in wanted:
        if item not in cur:
            cur.append(item)

servers = sub(d, "mcp_servers", {})
if "jev_skill_router" in servers:
    print("  mcp_servers.jev_skill_router already present; left as is")
else:
    servers["jev_skill_router"] = {
        "command": str(home / "jev-router" / ".venv" / "bin" / "python"),
        "args": [str(home / "jev-router" / "jev_router_mcp.py")],
        "env": {"TYPESAFE_API_KEY": "${TYPESAFE_API_KEY}"},
        "connect_timeout": 60,
        "timeout": 60,
        "enabled": True,
        "tools": {"resources": False, "prompts": False},
    }

if d == orig:
    print("  unchanged  config.yaml already has the plugin and MCP entries")
    sys.exit(0)
if dry:
    print("  [dry-run] would add the missing plugin / MCP entries to config.yaml")
    sys.exit(0)

backup = cfg.with_name(f"config.yaml.bak-jev-{os.environ['TS']}")
backup.write_bytes(cfg.read_bytes())
text = yaml.safe_dump(d, sort_keys=False, default_flow_style=False, allow_unicode=True, width=10**6)
assert yaml.safe_load(text) == d, "re-parse mismatch; config NOT written"
cfg.write_text(text)
print(f"  updated    config.yaml (backup: {backup.name})")
PY

echo
if [ "$DRY_RUN" = 1 ]; then
  echo "Dry run complete; nothing was changed."
else
  echo "Install complete. NEXT:"
  echo "  1. make sure TYPESAFE_API_KEY is in $HERMES_HOME/.env"
  echo "  2. restart the agent (dashboard Restart, or 'oc-remote restart')"
  echo "  3. bash $HERE/verify.sh"
fi
