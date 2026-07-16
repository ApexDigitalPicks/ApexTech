"""
Scout — identity/persona loader.

Keeps tone and self-reference rules in scout_persona.yaml instead of
hardcoded in route logic, so tone can be tuned without touching the
pipeline. See "Identity layer" in the Phase 1 spec.
"""
import os
import re

import yaml

_PERSONA_PATH = os.environ.get(
    "SCOUT_PERSONA_FILE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "scout_persona.yaml"),
)

_persona = None


def _load():
    global _persona
    if _persona is None:
        with open(_PERSONA_PATH) as f:
            _persona = yaml.safe_load(f)
    return _persona


def get_name():
    return _load().get("name", "Scout")


def get_system_prompt():
    return _load().get("system_prompt", "")


def sanitize_output(text):
    """Last-resort safety net: strip provider names from anything about to
    reach a dashboard, reply, or notification. Not a substitute for a
    correctly-scoped system prompt."""
    if not text:
        return text
    for term in _load().get("redact_terms", []):
        text = re.sub(re.escape(term), "", text, flags=re.IGNORECASE)
    return text
