"""Разбор уведомлений Jira и Confluence без модели: задачи и изменённые страницы."""

from __future__ import annotations

import re
from typing import Any

from .textutil import display_name, one_line

JIRA_KEY_RE = re.compile(r"\b([A-Z][A-Z0-9_]{1,15}-\d{1,7})\b")
URL_RE = re.compile(r"https?://[^\s<>\"')\]]+")

JIRA_ACTIONS = [
    (re.compile(r"(создал|created)", re.I), "создана"),
    (re.compile(r"(прокомментир|commented|комментари)", re.I), "комментарий"),
    (re.compile(r"(назнач|assigned)", re.I), "назначение"),
    (re.compile(r"(статус|status|transition|перев[её]л)", re.I), "смена статуса"),
    (re.compile(r"(упомянул|mentioned)", re.I), "вас упомянули"),
    (re.compile(r"(изменил|updated|обновил)", re.I), "изменение"),
]


def _jira_title(subject: str, key: str) -> str:
    s = re.sub(r"^\s*\[JIRA\]\s*", "", subject, flags=re.I)
    # Типичные темы: "(ABC-123) Название", "Updates for ABC-123: Название", "ABC-123 Название"
    after = s.split(key, 1)[1] if key in s else s
    after = re.sub(r"^[\s)\]:–—-]+", "", after)
    return one_line(after, 140) or one_line(s, 140)


def jira_items(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Группирует уведомления по задаче: ключ, название, что происходило, кто, последняя дата."""
    by_key: dict[str, dict[str, Any]] = {}
    for m in messages:
        subject = m.get("subject") or ""
        keys = JIRA_KEY_RE.findall(subject) or JIRA_KEY_RE.findall(m.get("text") or "")
        key = keys[0] if keys else "—"
        item = by_key.setdefault(
            key,
            {"key": key, "title": _jira_title(subject, key) if key != "—" else one_line(subject, 140),
             "actions": [], "people": [], "count": 0, "last_date": m["date"], "url": None},
        )
        item["count"] += 1
        item["last_date"] = max(item["last_date"], m["date"])
        haystack = f"{subject}\n{(m.get('text') or '')[:400]}"
        for rx, label in JIRA_ACTIONS:
            if rx.search(haystack) and label not in item["actions"]:
                item["actions"].append(label)
                break
        who = _actor(m)
        if who and who not in item["people"]:
            item["people"].append(who)
        if not item["url"]:
            item["url"] = next((u for u in URL_RE.findall(m.get("text") or "") if "/browse/" in u), None)
    return sorted(by_key.values(), key=lambda i: i["last_date"], reverse=True)


CONF_SUBJECT_PATTERNS = [
    # "[Confluence] Пространство > Страница"
    re.compile(r"^\s*\[Confluence\]\s*(?P<space>[^>]+?)\s*>\s*(?P<page>.+)$", re.I),
    # "Иванов изменил страницу «Название»" / "... created a page: Title"
    re.compile(r"(?P<who>.+?)\s+(изменил[аи]?|обновил[аи]?|создал[аи]?|edited|updated|created)\s+(страницу|page)[:\s]*[«\"]?(?P<page>[^»\"]+)", re.I),
]


def confluence_items(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_page: dict[tuple[str, str], dict[str, Any]] = {}
    for m in messages:
        subject = re.sub(r"\s+", " ", m.get("subject") or "").strip()
        space, page, who = "", subject, ""
        for rx in CONF_SUBJECT_PATTERNS:
            mt = rx.search(subject)
            if mt:
                gd = mt.groupdict()
                space = (gd.get("space") or "").strip()
                page = (gd.get("page") or subject).strip()
                who = (gd.get("who") or "").strip()
                break
        page = re.sub(r"^\s*\[Confluence\]\s*", "", page, flags=re.I)
        text = m.get("text") or ""
        if not who:
            mt = re.search(r"^(.{3,60}?)\s+(изменил|обновил|создал|прокомментировал|edited|updated|created|commented)", text, re.M | re.I)
            who = mt.group(1).strip() if mt else _actor(m)
        key = (space, page)
        item = by_page.setdefault(key, {"space": space or "Без пространства", "page": page, "people": [], "count": 0,
                                        "last_date": m["date"], "url": None, "commented": False})
        item["count"] += 1
        item["last_date"] = max(item["last_date"], m["date"])
        if re.search(r"коммент|comment", f"{subject} {text[:300]}", re.I):
            item["commented"] = True
        if who and who not in item["people"]:
            item["people"].append(who)
        if not item["url"]:
            item["url"] = next((u for u in URL_RE.findall(text) if "/pages/" in u or "/display/" in u or "pageId" in u), None)
    items = sorted(by_page.values(), key=lambda i: i["last_date"], reverse=True)
    spaces: dict[str, list[dict[str, Any]]] = {}
    for it in items:
        spaces.setdefault(it["space"], []).append(it)
    return [{"space": s, "pages": p} for s, p in spaces.items()]


def _actor(m: dict[str, Any]) -> str:
    name = display_name(m.get("from_raw"))
    # У уведомлений часто вид "Иванов Иван (Jira)" — убираем хвост
    return re.sub(r"\s*\((jira|confluence)\)\s*$", "", name, flags=re.I)
