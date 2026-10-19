"""Папки почты: какие смотреть и какие письма в них появились за последние сутки.

Outlook часто раскладывает письма правилами по папкам, и в «Входящих» их уже не видно. Эти папки
mailpilot находит сам через list_mailboxes, забирает из них свежие письма и показывает отдельным
разделом сводки «По папкам» с короткой сутью от агента.
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

from .config import Config
from .store import Store
from .textutil import iso, local, one_line, parse_dt

# Служебные папки по признакам IMAP (RFC 6154)
SKIP_ATTRIBS = {"\\noselect", "\\nonexistent", "\\sent", "\\drafts", "\\trash", "\\junk", "\\all"}
# И по именам: Exchange и Outlook отдают их на языке ящика
SKIP_NAMES = {
    "inbox", "входящие", "sent", "sent items", "sent messages", "отправленные", "drafts", "черновики",
    "trash", "deleted", "deleted items", "deleted messages", "удаленные", "удалённые",
    "junk", "junk e-mail", "junk email", "spam", "нежелательная почта", "спам", "outbox", "исходящие",
    "calendar", "календарь", "contacts", "контакты", "tasks", "задачи", "notes", "заметки", "journal", "дневник",
    "conversation history", "журнал разговоров", "sync issues", "проблемы синхронизации", "conflicts",
    "конфликты", "local failures", "локальные ошибки", "server failures", "ошибки сервера",
    "rss feeds", "rss-каналы", "rss-подписки", "suggested contacts", "предлагаемые контакты",
}


def enabled(cfg: Config) -> bool:
    return bool(cfg.get("folders.enabled", True))


def window_hours(cfg: Config) -> int:
    return int(cfg.get("folders.hours") or 24)


def in_main_digest(cfg: Config) -> bool:
    return bool(cfg.get("folders.in_main_digest", False))


def flatten(tree: Any) -> list[dict[str, Any]]:
    """Дерево из list_mailboxes в плоский список {name, attribs, delimiter}."""
    out: list[dict[str, Any]] = []
    if isinstance(tree, dict) and isinstance(tree.get("mailboxes"), list):
        tree = tree["mailboxes"]
    if isinstance(tree, list):
        for item in tree:
            if isinstance(item, str):
                out.append({"name": item, "attribs": [], "delimiter": "/"})
            elif isinstance(item, dict) and item.get("name"):
                out.append({"name": item["name"], "attribs": item.get("attribs") or [], "delimiter": item.get("delimiter") or "/"})
        return out
    if not isinstance(tree, dict):
        return out

    def walk(node: dict[str, Any], key: str) -> None:
        name = node.get("name") or key
        attribs = [str(a) for a in node.get("attribs") or []]
        if node.get("selectable") is False and "\\noselect" not in {a.lower() for a in attribs}:
            attribs.append("\\Noselect")
        out.append({"name": name, "attribs": attribs, "delimiter": node.get("delimiter") or "/"})
        for ck, child in (node.get("children") or {}).items():
            if isinstance(child, dict):
                walk(child, f"{name}{node.get('delimiter') or '/'}{ck}")

    for key, node in tree.items():
        if isinstance(node, dict):
            walk(node, key)
    return out


def leaf(name: str, delimiter: str | None = None) -> str:
    for d in ([delimiter] if delimiter else []) + ["/", ".", "\\"]:
        if d and d in name:
            return name.rsplit(d, 1)[-1]
    return name


def _matches(name: str, delimiter: str, patterns: list[str]) -> bool:
    low = name.lower()
    return any(p.lower() in (low, leaf(name, delimiter).lower()) for p in patterns)


def choose(cfg: Config, tree: Any) -> list[str]:
    """Какие папки смотреть: include из настроек или все, кроме служебных и основных."""
    main = {(cfg.get("mailboxes.inbox") or "INBOX").lower(), "inbox"}
    if cfg.get("mailboxes.sent"):
        main.add(str(cfg.get("mailboxes.sent")).lower())
    main.update(str(x).lower() for x in cfg.get("mailboxes.extra") or [])
    include = [str(x) for x in cfg.get("folders.include") or []]
    exclude = [str(x) for x in cfg.get("folders.exclude") or []]
    out = []
    for box in flatten(tree):
        name, delim = box["name"], box["delimiter"]
        attribs = {a.lower() for a in box["attribs"]}
        if name.lower() in main or "\\noselect" in attribs or "\\nonexistent" in attribs:
            continue
        if include:
            if _matches(name, delim, include):
                out.append(name)
            continue
        if attribs & SKIP_ATTRIBS or leaf(name, delim).lower() in SKIP_NAMES or _matches(name, delim, exclude):
            continue
        out.append(name)
    return out


async def discover(cfg: Config, store: Store, src: Any) -> list[str]:
    """Список папок у сервера; при ошибке остаётся прошлый."""
    try:
        names = choose(cfg, await src.list_mailboxes())
    except Exception as exc:  # сервер мог не ответить — сводка всё равно соберётся
        print(f"  Не удалось получить список папок: {exc}")
        return scanned(store)
    store.set_kv("scanned_folders", json.dumps(names, ensure_ascii=False))
    store.commit()
    return names


def scanned(store: Store) -> list[str]:
    return json.loads(store.get_kv("scanned_folders", "[]") or "[]")


def folder_messages(cfg: Config, store: Store, period_to: str) -> dict[str, list[dict[str, Any]]]:
    """Письма в папках за последние folders.hours часов, по папкам."""
    names = scanned(store)
    if not names or not enabled(cfg):
        return {}
    since = iso(parse_dt(period_to) - timedelta(hours=window_hours(cfg)))
    q = ",".join("?" * len(names))
    rows = store.query(
        f"SELECT * FROM messages WHERE mailbox IN ({q}) AND date>=? AND date<=? AND is_mine=0 ORDER BY date DESC",
        [*names, since, period_to],
    )
    out: dict[str, list[dict[str, Any]]] = {}
    for m in rows:
        out.setdefault(m["mailbox"], []).append(m)
    return out


def summaries(store: Store) -> dict[str, dict[str, Any]]:
    return json.loads(store.get_kv("folder_summaries", "{}") or "{}")


def save_summaries(store: Store, items: dict[str, dict[str, Any]]) -> None:
    store.set_kv("folder_summaries", json.dumps(items, ensure_ascii=False))


def request_for_agent(cfg: Config, store: Store, period_to: str, max_per_folder: int = 40) -> list[dict[str, Any]]:
    """Папки, по которым нужна свежая суть: новые письма после прошлого разбора."""
    done = summaries(store)
    out = []
    for name, msgs in folder_messages(cfg, store, period_to).items():
        newest = msgs[0]["date"]
        if done.get(name, {}).get("upto", "") >= newest:
            continue
        out.append({
            "folder": name,
            "count": len(msgs),
            "upto": newest,
            "messages": [{
                "date": local(m["date"]).strftime("%d.%m %H:%M"),
                "from": m.get("from_raw") or "",
                "subject": m.get("subject") or "",
                "gist": one_line(m.get("new_text") or m.get("text") or "", 300),
            } for m in msgs[:max_per_folder]],
        })
    return out
