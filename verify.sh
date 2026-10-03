#!/usr/bin/env bash
# Verify a Jev skill router install. Read-only; never prints secrets.
# Exit code 0 = all checks passed.
set -uo pipefail

HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
FAILS=0
ok()   { echo "  PASS  $*"; }
bad()  { echo "  FAIL  $*"; FAILS=$((FAILS + 1)); }
info() { echo "  info  $*"; }

HPY=""
for c in /usr/local/lib/hermes/hermes-agent/venv/bin/python "$(command -v python3 || true)"; do
  if [ -n "$c" ] && [ -x "$c" ] && "$c" -c 'import yaml, dotenv' 2>/dev/null; then HPY="$c"; break; fi
done
[ -n "$HPY" ] || { echo "need python with PyYAML and python-dotenv"; exit 2; }
VENV="$HERMES_HOME/jev-router/.venv"

echo "== files =="
for f in jev-router/jev_router.py jev-router/jev_router_mcp.py \
         plugins/jev-skill-router/plugin.yaml plugins/jev-skill-router/__init__.py \
         skills/mcp/jev-skill-routing/SKILL.md; do
  [ -f "$HERMES_HOME/$f" ] && ok "$f" || bad "missing $f"
done
grep -qi pilot "$HERMES_HOME/jev-router/jev_router.py" 2>/dev/null && bad "router still contains pilot code" || ok "no pilot code in router"

echo "== venv =="
"$VENV/bin/python" -c 'from mcp.server.fastmcp import FastMCP' 2>/dev/null \
  && ok "mcp.server.fastmcp importable" || bad "mcp.server.fastmcp not importable in $VENV"

echo "== config =="
HERMES_HOME="$HERMES_HOME" "$HPY" - <<'PY' && ok "config.yaml has plugin + MCP entries" || bad "config.yaml is missing Jev entries"
import os, sys, yaml
d = yaml.safe_load(open(os.path.join(os.environ["HERMES_HOME"], "config.yaml"))) or {}
p = d.get("plugins") or {}
llm = ((p.get("entries") or {}).get("jev-skill-router") or {}).get("llm") or {}
good = ("jev-skill-router" in (p.get("enabled") or [])
        and "openai-api" in (llm.get("allowed_providers") or [])
        and "gpt-5.6-luna" in (llm.get("allowed_models") or [])
        and "jev_skill_router" in (d.get("mcp_servers") or {}))
sys.exit(0 if good else 1)
PY

echo "== live route (single full-scope call; key read from .env, not printed) =="
KEY="$(HERMES_HOME="$HERMES_HOME" "$HPY" -c '
import os
from dotenv import dotenv_values
print((dotenv_values(os.path.join(os.environ["HERMES_HOME"], ".env")).get("TYPESAFE_API_KEY") or "").strip())')"
if [ -z "$KEY" ]; then
  bad "TYPESAFE_API_KEY not set in $HERMES_HOME/.env"
else
  OUT="$(cd "$HERMES_HOME/jev-router" && TYPESAFE_API_KEY="$KEY" HOME="${HOME}" "$VENV/bin/python" -c '
from jev_router import route_skill, discover_skills
r = route_skill("Help me plan and schedule my work for next week")
n = len(discover_skills())
print(r["selected_skill"], r["confidence"], r["routing_scope"], r["valid_response"], len(r["probabilities"]), n)' 2>&1 | tail -n 1)"
  set -- $OUT
  if [ "$#" -eq 6 ] && [ "$3" = "full" ] && [ "$4" = "True" ]; then
    ok "routed -> $1 (confidence $2, scope $3); scored $5 candidates, registry has $6 skills"
  else
    bad "live route failed: $OUT"
  fi
fi
unset KEY

echo "== running services (only meaningful after a restart) =="
pgrep -f 'hermes gateway run' >/dev/null && ok "gateway running" || info "gateway not running"
pgrep -f jev_router_mcp >/dev/null && ok "Jev MCP server running" || info "Jev MCP server not running (restart the agent)"
LOG="$HERMES_HOME/logs/agent.log"
if [ -f "$LOG" ]; then
  last="$(grep 'jev-skill-router: session' "$LOG" | tail -n 1 | cut -c1-200)"
  [ -n "$last" ] && info "last plugin decision: $last" || info "plugin has not routed a message yet"
fi

echo
if [ "$FAILS" -eq 0 ]; then echo "ALL CHECKS PASSED"; else echo "$FAILS CHECK(S) FAILED"; fi
exit "$FAILS"
