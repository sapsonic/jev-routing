---
name: jev-skill-routing
description: Use when selecting a specialized Hermes skill via Jev.
version: 1.1.0
license: MIT
metadata:
  hermes:
    tags: [Jev, TypeSafe, skill-routing, workflow-routing, Hermes]
---

# Jev Skill Routing

Use this skill as the first routing step for each new request that may need a specialized skill.

## Required procedure

1. Before task-specific action, call `mcp_jev_skill_router_jev_route_skill` with the complete current user request and concise relevant conversation context. Exclude credentials, raw private customer records, and unnecessary internal data.
2. Treat `selected_skill` as an allowlisted candidate only. Never invent, rename, or manually substitute a skill name from the response.
3. If the selected skill is not `general`, load it with the Hermes skill mechanism before task-specific action.
4. When `needs_multiple_skills` is material, use the selected skill for the first phase. Route each distinct later phase with Jev rather than loading an arbitrary bundle.
5. Treat selection, probabilities, confidence, and flags as routing evidence. They do not override approval requirements, privacy rules, safety checks, or external-action safeguards.
6. If the response is invalid, unavailable, times out, or cannot support a safe clarification path, use normal Hermes skill selection. Do not fabricate a Jev response.
7. For material routing decisions in Slack, say briefly before the call: `Routing this request with Jev to choose the right workflow.` Then report the result concisely without exposing internal state, credentials, or unnecessary probability detail.

## Interpreting the result

- `selected_skill` is always an installed allowlisted name or `general`.
- `needs_multiple_skills` and `needs_clarification` are Noul probabilities, not authorization to act.
- High clarification probability means inspect the selected and close competing skills, then ask only for the missing facts needed to start safely.
- Keep `general` for simple conversation, factual questions, or work with no specialized workflow match.

## Scope and privacy

Jev only routes. Hermes loads skills and executes tools. The router never reads skill bodies, loads skills, accesses customer data, or performs actions.

Jev routes against every installed skill. The registry is rescanned from the skills directory on each call, so newly installed skills are candidates automatically with no allowlist to maintain. The result's `routing_scope` is `full`.

Do not create a persistent routing log. Do not store raw requests, context, customer data, credentials, probabilities, model versions, or outcomes. The routing skill itself is excluded from Jev's candidate registry to prevent recursive self-selection.
