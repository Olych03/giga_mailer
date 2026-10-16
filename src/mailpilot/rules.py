"""Раскладка писем по категориям правилами из настроек."""

from __future__ import annotations

import re
from typing import Any

from .config import Category, Config


def _as_list(v: Any) -> list[str]:
    if v is None:
        return []
    if isinstance(v, (list, tuple)):
        return [str(x) for x in v]
    return [str(v)]


def _contains_any(haystack: str, needles: list[str]) -> bool:
    h = haystack.lower()
    return any(n.lower() in h for n in needles)


def _regex_any(haystack: str, patterns: list[str]) -> bool:
    return any(re.search(p, haystack, re.IGNORECASE | re.MULTILINE) for p in patterns)


def condition_matches(cond: dict[str, Any], msg: dict[str, Any]) -> bool:
    """Все ключи одного условия должны совпасть (И)."""
    recipients = f"{msg.get('to_raw') or ''}, {msg.get('cc_raw') or ''}"
    checks = {
        "recipients_contain": lambda v: _contains_any(recipients, _as_list(v)),
        "from_contains": lambda v: _contains_any(msg.get("from_raw") or "", _as_list(v)),
        "from_regex": lambda v: _regex_any(msg.get("from_raw") or "", _as_list(v)),
        "subject_contains": lambda v: _contains_any(msg.get("subject") or "", _as_list(v)),
        "subject_regex": lambda v: _regex_any(msg.get("subject") or "", _as_list(v)),
        "body_regex": lambda v: _regex_any(msg.get("text") or "", _as_list(v)),
    }
    if not cond:
        return False
    for key, value in cond.items():
        check = checks.get(key)
        if check is None or not check(value):
            return False
    return True


def classify(cfg: Config, msg: dict[str, Any]) -> Category:
    default = None
    for cat in cfg.categories:
        if cat.default:
            default = cat
        if any(condition_matches(c, msg) for c in cat.match):
            return cat
    assert default is not None
    return default
