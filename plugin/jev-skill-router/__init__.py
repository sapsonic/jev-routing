"""Deterministic Jev skill-routing plugin.

Guarantees that skill selection is routed through the Jev (TypeSafe) model on:
  - the first turn of every session (unconditionally), and
  - any later turn where a cheap classifier gate decides the currently active
    skill no longer fits the new request.

Privacy / ephemerality (required by the jev-skill-routing SKILL.md contract):
  - No routing state is ever written to disk. The "currently active skill per
    session" cache is an in-memory dict only, scoped to this process, cleared
    on `on_session_start` / `on_session_reset`, and lost entirely on process
    restart. This is a deliberate, accepted tradeoff: a session resumed in a
    brand-new OS process (CLI `--resume`, or a gateway restart mid-conversation)
    has no record of an active skill, so it correctly falls back to the
    unconditional full-Jev path for that turn rather than guessing -- the safe
    direction, and consistent with "do not create a persistent routing log."
  - No raw conversation history is ever sent to Jev or to the reroute-gate
    classifier. Only the current user message text is used. This trades away
    some routing context for hard compliance with "concise relevant context...
    exclude credentials, raw private customer records, and unnecessary
    internal data" -- there is no redaction pipeline in place, so the safe
    choice is to not transmit prior-turn content at all rather than transmit
    it unfiltered.

Routing-confidence gate (added after a routing incident where a 0.44
confidence broad-routing pick was injected as an unconditional directive and
selected the wrong of two similar client workflows):
  - A specialized `selected_skill` is only injected as a directive when
    route_skill()'s own confidence, probability margin, and
    needs_clarification signals all clear a floor. Below the floor, routing
    is withheld and the model is told to ask a clarifying question instead
    of guessing. This uses only fields route_skill() already returns -- no
    new context, channel metadata, or persistence.

Fails open throughout: any error, timeout, or missing credential simply
skips injection for that turn and falls back to normal Hermes skill
selection (matching the existing jev-skill-routing SKILL.md's own fallback
rule) rather than blocking or corrupting the turn.
"""
from __future__ import annotations

import logging
import sys
import threading
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger("hermes_cli.plugins.jev_skill_router")

_ROUTER_DIR = Path.home() / ".hermes" / "jev-router"
if str(_ROUTER_DIR) not in sys.path:
    sys.path.insert(0, str(_ROUTER_DIR))

try:
    from jev_router import route_skill  # type: ignore
except Exception as exc:  # pragma: no cover - import guard
    route_skill = None
    logger.warning("jev-skill-router: could not import jev_router.route_skill: %s", exc)

# In-memory only: {session_id: active_skill}. Never written to disk. Lost on
# process restart (by design -- see module docstring). Capped to bound memory
# in a long-lived gateway process; eviction is oldest-inserted-first, which is
# fine since an evicted session just re-triggers the safe unconditional path.
_SESSION_SKILLS: dict[str, str] = {}
_PROCESS_LOCK = threading.Lock()
_MAX_TRACKED_SESSIONS = 500

_ROUTE_TIMEOUT_S = 25.0
_GATE_TIMEOUT_S = 12.0

_GATE_SCHEMA = {
    "type": "object",
    "properties": {"needs_reroute": {"type": "boolean"}},
    "required": ["needs_reroute"],
    "additionalProperties": False,
}

# Routing-confidence gate thresholds. Starting points carried over from the
# incident postmortem; tune against real routing data over time rather than
# treating these as final.
_MIN_ROUTE_CONFIDENCE = 0.75
_MIN_ROUTE_MARGIN = 0.15
_CLARIFICATION_THRESHOLD = 0.5


def _get_active_skill(session_id: str) -> Optional[str]:
    with _PROCESS_LOCK:
        return _SESSION_SKILLS.get(session_id)


def _set_active_skill(session_id: str, skill: str) -> None:
    with _PROCESS_LOCK:
        if session_id not in _SESSION_SKILLS and len(_SESSION_SKILLS) >= _MAX_TRACKED_SESSIONS:
            oldest = next(iter(_SESSION_SKILLS), None)
            if oldest is not None:
                _SESSION_SKILLS.pop(oldest, None)
        _SESSION_SKILLS[session_id] = skill


def _clear_state(*, session_id: str = "", **_kwargs: Any) -> None:
    if not session_id:
        return
    with _PROCESS_LOCK:
        _SESSION_SKILLS.pop(session_id, None)


def _extract_text(message: Any) -> str:
    if isinstance(message, str):
        return message
    if isinstance(message, list):
        parts = []
        for block in message:
            if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
                parts.append(block["text"])
        if parts:
            return "\n".join(parts)
    try:
        return str(message) if message is not None else ""
    except Exception:
        return ""


def _run_route_skill(user_text: str) -> Optional[dict[str, Any]]:
    """Call route_skill() off-thread with our own bounded timeout, fail-open.

    Only the current user message is passed -- no conversation history -- per
    the SKILL.md's "concise... exclude... unnecessary internal data" rule.
    """
    if route_skill is None:
        return None
    result: dict[str, Any] = {}
    error: list[BaseException] = []

    def _work() -> None:
        try:
            result.update(route_skill(user_text, ""))
        except Exception as exc:  # ValueError/RuntimeError from route_skill, or anything else
            error.append(exc)

    thread = threading.Thread(target=_work, daemon=True)
    thread.start()
    thread.join(_ROUTE_TIMEOUT_S)
    if thread.is_alive():
        logger.warning("jev-skill-router: route_skill timed out after %ss", _ROUTE_TIMEOUT_S)
        return None
    if error:
        logger.info("jev-skill-router: route_skill unavailable: %s", error[0])
        return None
    return result or None


def _route_gate_result(routed: dict[str, Any]) -> tuple[bool, str]:
    """Decide whether a specialized `selected_skill` is trustworthy enough to
    inject as a directive, or whether the router's own signals say to hold
    back and ask instead. Uses only fields route_skill() already returns
    (confidence, probabilities, needs_clarification, valid_response) -- no
    new context, no persistence. Returns (passes, reason); reason is for
    logging only, never persisted.
    """
    if not routed.get("valid_response", True):
        return False, "invalid_response"

    confidence = routed.get("confidence")
    if not isinstance(confidence, (int, float)) or confidence < _MIN_ROUTE_CONFIDENCE:
        return False, f"low_confidence({confidence!r})"

    # needs_clarification is a numeric "noul" score (like confidence), not a
    # boolean -- same shape jev_router.py already uses for outside_pilot_scope.
    clarification = routed.get("needs_clarification")
    if isinstance(clarification, (int, float)) and clarification >= _CLARIFICATION_THRESHOLD:
        return False, f"needs_clarification({clarification!r})"

    probabilities = routed.get("probabilities")
    if isinstance(probabilities, dict) and len(probabilities) >= 2:
        ranked = sorted(
            (v for v in probabilities.values() if isinstance(v, (int, float))), reverse=True
        )
        if len(ranked) >= 2:
            margin = ranked[0] - ranked[1]
            if margin < _MIN_ROUTE_MARGIN:
                return False, f"low_margin({margin:.2f})"

    return True, "ok"


def _should_reroute(ctx: Any, active_skill: str, user_text: str) -> bool:
    """Cheap classifier gate used only when a skill is already active for this
    session. Sees only the active skill name and the new user message -- no
    prior conversation content.
    """
    llm = getattr(ctx, "llm", None)
    if llm is None:
        return True  # no host LLM facade available -> fail toward calling Jev
    try:
        result = llm.complete_structured(
            instructions=(
                "You are a routing gate for a Hermes agent. The conversation currently has "
                f"an active skill selection of '{active_skill}' (this may literally be "
                "'general', meaning no specialized skill was needed last time). Decide "
                "whether the NEW user message plausibly needs a different skill decision - "
                "either a different specialized skill, or a switch between 'general' and a "
                "specific skill. If the new message is simply continuing the same task or "
                "topic, answer false. If it introduces a new task, a topic change, or "
                "anything that could need a different skill, answer true. When unsure, "
                "answer true."
            ),
            input=[{"type": "text", "text": f"New user message:\n{user_text}"}],
            json_schema=_GATE_SCHEMA,
            schema_name="jev_reroute_gate",
            purpose="jev-skill-router-gate",
            timeout=_GATE_TIMEOUT_S,
            # gpt-5.6-luna is a non-reasoning-by-default model (verified live:
            # 0 reasoning_tokens with no explicit reasoning_effort flag), so a
            # small budget for a one-key JSON boolean is enough -- no
            # reasoning-token starvation risk like the earlier
            # glm-5.3-flash/NVIDIA route had.
            max_tokens=150,
            provider="openai-api",
            model="gpt-5.6-luna",
        )
        parsed = result.parsed if result and result.content_type == "json" else None
        if isinstance(parsed, dict) and isinstance(parsed.get("needs_reroute"), bool):
            return parsed["needs_reroute"]
    except Exception as exc:
        logger.info("jev-skill-router: classifier gate failed, defaulting to reroute: %s", exc)
    return True  # fail open toward Jev rather than silently missing a needed reroute


def _inject_jev_routing(
    ctx: Any = None,
    *,
    session_id: str = "",
    user_message: Any = None,
    is_first_turn: bool = False,
    **_kwargs: Any,
) -> Optional[str]:
    if route_skill is None or not session_id:
        return None

    user_text = _extract_text(user_message).strip()
    if not user_text:
        return None

    active_skill = _get_active_skill(session_id)

    # First turn of a session, or no routing decision on record (including a
    # process restart that dropped in-memory state): always call Jev directly,
    # bypassing the classifier gate entirely.
    needs_full_route = is_first_turn or active_skill is None

    if not needs_full_route:
        if not _should_reroute(ctx, active_skill, user_text):
            logger.info(
                "jev-skill-router: session=%s gate=skip active_skill=%s", session_id, active_skill
            )
            return None
        logger.info("jev-skill-router: session=%s gate=reroute active_skill=%s", session_id, active_skill)

    routed = _run_route_skill(user_text)
    if not routed:
        logger.info(
            "jev-skill-router: session=%s route_skill unavailable this turn (first_turn=%s)",
            session_id, is_first_turn,
        )
        return None  # Jev unavailable/timed out -> fall back to normal Hermes skill selection

    selected = routed.get("selected_skill") or "general"

    logger.info(
        "jev-skill-router: session=%s first_turn=%s routed -> %s (scope=%s, valid=%s)",
        session_id, is_first_turn, selected, routed.get("routing_scope"), routed.get("valid_response"),
    )

    if selected == "general":
        # "general" is itself a safe, neutral outcome -- no wrong specialized
        # workflow is being forced, so no confidence floor is needed here.
        _set_active_skill(session_id, selected)
        return None

    gate_ok, gate_reason = _route_gate_result(routed)
    if not gate_ok:
        # Router's own signals say this specialized pick isn't trustworthy
        # enough to force. Deliberately do NOT overwrite existing session
        # state with an uncertain skill: leave it as-is, so the next turn
        # either retries the full route (if nothing was active yet) or keeps
        # whichever skill was last actually trusted.
        logger.info(
            "jev-skill-router: session=%s routing withheld for '%s' (%s)",
            session_id, selected, gate_reason,
        )
        return (
            "[jev-skill-router] Routing for this request was ambiguous "
            f"(candidate: '{selected}', reason: {gate_reason}). Do not assume "
            "which workflow applies -- ask the user one targeted clarifying "
            "question to confirm which skill/workflow they mean before "
            "proceeding, instead of guessing."
        )

    _set_active_skill(session_id, selected)

    confidence = routed.get("confidence")
    conf_note = f" (confidence={confidence:.2f})" if isinstance(confidence, (int, float)) else ""
    return (
        f"[jev-skill-router] Deterministic pre-routing selected the '{selected}' skill{conf_note}. "
        f"Load and follow that skill's SKILL.md for this request before choosing another approach. "
        f"If it genuinely does not fit once you review it, fall back to normal Hermes skill "
        f"selection instead of forcing it."
    )


def register(ctx: Any) -> None:
    ctx.register_hook("pre_llm_call", lambda **kwargs: _inject_jev_routing(ctx, **kwargs))
    ctx.register_hook("on_session_start", _clear_state)
    ctx.register_hook("on_session_reset", _clear_state)
