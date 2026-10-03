"""Allowlisted TypeSafe Jev router for Hermes skills."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

API_URL = "https://api.typesafe.ai/v1/systemone"
DEFAULT_SKILLS_ROOT = Path.home() / ".hermes" / "skills"
SKILL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
PostFunction = Callable[[str, dict[str, str], dict[str, Any], int], dict[str, Any]]


def _frontmatter_value(text: str, field: str) -> str | None:
    if not text.startswith("---\n"):
        return None
    closing = text.find("\n---", 4)
    if closing < 0:
        return None
    match = re.search(rf"^{re.escape(field)}:\s*(.+?)\s*$", text[4:closing], re.MULTILINE)
    return match.group(1).strip().strip("\"'") if match else None


def discover_skills(skills_root: Path = DEFAULT_SKILLS_ROOT) -> dict[str, str]:
    """Return the installed skill registry without reading skill body content."""
    registry: dict[str, str] = {}
    if not skills_root.exists():
        return registry
    for skill_file in skills_root.rglob("SKILL.md"):
        try:
            frontmatter = skill_file.read_text(encoding="utf-8", errors="replace")[:8192]
        except OSError:
            continue
        name = _frontmatter_value(frontmatter, "name")
        description = _frontmatter_value(frontmatter, "description")
        if not name or not description or not SKILL_NAME_RE.fullmatch(name) or name in {"general", "jev-skill-routing"}:
            continue
        registry.setdefault(name, description)
    return dict(sorted(registry.items()))


def _post_json(url: str, headers: dict[str, str], payload: dict[str, Any], timeout: int) -> dict[str, Any]:
    request = Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
    try:
        with urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        raise RuntimeError(f"TypeSafe API returned HTTP {exc.code}") from exc
    except URLError as exc:
        raise RuntimeError("Could not reach the TypeSafe API") from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("TypeSafe API returned an invalid response") from exc


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _route_payload(user_request: str, conversation_context: str, available_skills: dict[str, str]) -> dict[str, Any]:
    questions: dict[str, Any] = {
        "primary_skill": {
            "type": "choice",
            "instructions": "Choose the one best installed skill for this request. Choose general if no specialized installed skill is needed.",
            "criteria": available_skills,
        },
        "needs_multiple_skills": {
            "type": "noul",
            "instructions": "Would completing this request require multiple distinct specialized skills or workflow phases?",
        },
        "needs_clarification": {
            "type": "noul",
            "instructions": "Is required information missing before the selected skill can safely begin its task?",
        },
    }
    return {
        "model": "jev-latest",
        "state": {
            "user_request": user_request.strip(),
            "conversation_context": conversation_context.strip()[:6000],
            "available_skills": [{"name": name, "description": description} for name, description in available_skills.items()],
        },
        "questions": questions,
    }


def _parse_response(response: dict[str, Any], available_skills: dict[str, str]) -> dict[str, Any]:
    answers = response.get("answers") if isinstance(response, dict) else None
    if not isinstance(answers, dict):
        raise RuntimeError("TypeSafe API response did not contain answers")
    primary = answers.get("primary_skill")
    multiple = answers.get("needs_multiple_skills")
    clarification = answers.get("needs_clarification")
    if not all(isinstance(answer, dict) for answer in (primary, multiple, clarification)):
        raise RuntimeError("TypeSafe API response was missing routing answers")
    raw_choice = primary.get("choice")
    valid_response = isinstance(raw_choice, str) and raw_choice in available_skills
    raw_probabilities = primary.get("probabilities")
    probabilities = {name: value for name, value in raw_probabilities.items() if name in available_skills and _number(value) is not None} if isinstance(raw_probabilities, dict) else {}
    result = {
        "selected_skill": raw_choice if valid_response else "general",
        "model": response.get("model") if isinstance(response.get("model"), str) else None,
        "confidence": _number(primary.get("confidence")),
        "probabilities": probabilities,
        "needs_multiple_skills": _number(multiple.get("noul")),
        "needs_clarification": _number(clarification.get("noul")),
        "valid_response": valid_response,
    }
    return result


def route_skill(
    user_request: str,
    conversation_context: str = "",
    *,
    skills_root: Path = DEFAULT_SKILLS_ROOT,
    api_key: str | None = None,
    post: PostFunction = _post_json,
) -> dict[str, Any]:
    """Route against every installed skill in a single Jev call.

    The registry is rescanned from the skills directory on every call, so
    newly installed skills are candidates automatically.
    """
    if not isinstance(user_request, str) or not user_request.strip():
        raise ValueError("user_request must be a non-empty string")
    if not isinstance(conversation_context, str):
        raise ValueError("conversation_context must be a string")
    api_key = api_key or os.environ.get("TYPESAFE_API_KEY")
    if not api_key:
        raise RuntimeError("TYPESAFE_API_KEY is not configured")

    registry = discover_skills(skills_root)
    candidates = {"general": "No specialized installed skill is needed.", **registry}
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    response = post(API_URL, headers, _route_payload(user_request, conversation_context, candidates), 60)
    result = _parse_response(response, candidates)
    return {**result, "routing_scope": "full", "fallback_used": False}
