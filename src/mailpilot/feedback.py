"""Дообучение по решениям пользователя: отметки «важно / не важно», «ответ не нужен»,
правила, которые из них следуют, и файл предпочтений для агента."""

from __future__ import annotations

import json
import re
from typing import Any

from .config import Config, read_overrides, write_overrides
from .store import Store
from .textutil import iso, normalize_subject, now_utc, one_line, plural

LABELS = ("important", "not_important")
KINDS = LABELS + ("no_reply",)
KIND_TITLES = {"important": "важно", "not_important": "не важно", "no_reply": "ответ не нужен"}
# Сколько похожих отметок нужно, чтобы предложить правило
SUGGEST_AFTER = 2
DEPRIORITIZED = {
    "id": "deprioritized", "title": "Неважное по вашим отметкам", "importance": "low",
    "color": "#8a8f98", "first": True, "match": [],
}

PREFERENCES_TEMPLATE = """# Мои правила для разбора почты

Этот файл читает агент при каждом разборе почты. Пишите обычным языком, по одному правилу в строке.
Агент дополняет его по вашим отметкам в сводке, править можно и руками.

## Что для меня важно

## Что для меня не важно

## Когда не нужно готовить ответ
"""


def subject_key(subject: str | None) -> str:
    """Шаблон темы: без Re/Fw, номеров и дат, чтобы похожие уведомления совпадали."""
    s = normalize_subject(subject)
    s = re.sub(r"\binc\d+\b", "inc#", s)
    s = re.sub(r"\d+", "#", s)
    return s[:120]


def _key_regex(key: str) -> str:
    return re.escape(key).replace("\\#", "#").replace("#", r"\d+")


def _thread_context(store: Store, thread_id: str) -> dict[str, Any]:
    t = store.thread(thread_id) or {}
    msgs = store.thread_messages(thread_id)
    last_in = next((m for m in reversed(msgs) if not m.get("is_mine")), None) or {}
    return {
        "sender": (last_in.get("from_addr") or "").lower(),
        "category": t.get("category"),
        "subject": t.get("subject") or last_in.get("subject"),
    }


def mark(store: Store, thread_id: str, kind: str, reason: str | None = None) -> dict[str, Any]:
    """Сохранить решение по переписке. kind: important | not_important | no_reply | clear."""
    if not store.thread(thread_id):
        raise ValueError(f"Нет переписки {thread_id}")
    if kind not in KINDS + ("clear",):
        raise ValueError(f"Неизвестная отметка {kind}")
    ctx = _thread_context(store, thread_id)
    if kind in LABELS or kind == "clear":
        # У переписки одна отметка важности: новая заменяет прежнюю
        store.db.execute("DELETE FROM feedback WHERE thread_id=? AND kind IN (?, ?)", (thread_id, *LABELS))
    if kind == "clear":
        store.commit()
        return {"thread_id": thread_id, "label": None}
    if kind == "no_reply":
        store.db.execute("DELETE FROM feedback WHERE thread_id=? AND kind='no_reply'", (thread_id,))
        store.db.execute("UPDATE drafts SET status='no_reply' WHERE thread_id=? AND status='proposed'", (thread_id,))
    store.db.execute(
        "INSERT INTO feedback(thread_id, kind, reason, sender, category, subject, subject_key, created_at) VALUES(?,?,?,?,?,?,?,?)",
        (thread_id, kind, (reason or "").strip() or None, ctx["sender"], ctx["category"], ctx["subject"],
         subject_key(ctx["subject"]), iso(now_utc())),
    )
    store.commit()
    return {"thread_id": thread_id, "label": kind}


def set_reason(store: Store, thread_id: str, kind: str, reason: str) -> None:
    store.db.execute(
        "UPDATE feedback SET reason=? WHERE id=(SELECT MAX(id) FROM feedback WHERE thread_id=? AND kind=?)",
        ((reason or "").strip() or None, thread_id, kind),
    )
    store.commit()


def labels(store: Store) -> dict[str, str]:
    """Текущая отметка важности по каждой переписке."""
    rows = store.query("SELECT thread_id, kind FROM feedback WHERE kind IN (?, ?) ORDER BY id", LABELS)
    return {r["thread_id"]: r["kind"] for r in rows}


def no_reply_since(store: Store, thread_id: str) -> str | None:
    row = store.one("SELECT MAX(created_at) AS at FROM feedback WHERE thread_id=? AND kind='no_reply'", (thread_id,))
    return row["at"] if row else None


def decisions(store: Store, limit: int | None = None) -> list[dict[str, Any]]:
    sql = "SELECT * FROM feedback ORDER BY id DESC" + (f" LIMIT {int(limit)}" if limit else "")
    return store.query(sql)


def decisions_for_agent(store: Store, limit: int = 15) -> list[dict[str, Any]]:
    """Последние решения пользователя в коротком виде — примеры для модели."""
    return [{
        "decision": KIND_TITLES.get(d["kind"], d["kind"]),
        "subject": one_line(d.get("subject") or "", 120),
        "from": d.get("sender"),
        "category": d.get("category"),
        "reason": d.get("reason"),
    } for d in decisions(store, limit)]


def read_preferences(cfg: Config) -> str | None:
    p = cfg.preferences_path
    if not p.exists():
        return None
    text = p.read_text(encoding="utf-8").strip()
    return text if text and text != PREFERENCES_TEMPLATE.strip() else None


def ensure_preferences(cfg: Config) -> None:
    if not cfg.preferences_path.exists():
        cfg.preferences_path.parent.mkdir(parents=True, exist_ok=True)
        cfg.preferences_path.write_text(PREFERENCES_TEMPLATE, encoding="utf-8")


# --- предложения правил -------------------------------------------------------

def _dismissed(store: Store) -> set[str]:
    return set(json.loads(store.get_kv("dismissed_suggestions", "[]") or "[]"))


def _applied(cfg: Config) -> tuple[set[str], set[str], set[str]]:
    ov = read_overrides(cfg)
    senders, subjects = set(), set()
    for r in ov.get("rules") or []:
        if r.get("category") == DEPRIORITIZED["id"]:
            senders.update(a.lower() for a in r["match"].get("from_contains") or [])
            subjects.update(r["match"].get("subject_regex") or [])
    no_drafts = {a.lower() for a in ov.get("no_drafts_from") or []}
    return senders, subjects, no_drafts


def suggestions(cfg: Config, store: Store) -> list[dict[str, Any]]:
    """Правила, которые следуют из повторяющихся отметок. Применяются только с согласия пользователя."""
    dismissed = _dismissed(store)
    senders_done, subjects_done, no_drafts_done = _applied(cfg)
    me = set(cfg.my_addresses)
    rows = store.query("SELECT thread_id, kind, sender, subject_key, subject FROM feedback WHERE kind IN ('not_important', 'no_reply')")
    current = labels(store)
    out: list[dict[str, Any]] = []

    def group(kind: str, field: str) -> dict[str, dict[str, Any]]:
        g: dict[str, dict[str, Any]] = {}
        for r in rows:
            if r["kind"] != kind or not r[field]:
                continue
            if kind == "not_important" and current.get(r["thread_id"]) != "not_important":
                continue  # отметку уже сняли
            it = g.setdefault(r[field], {"threads": set(), "subjects": []})
            it["threads"].add(r["thread_id"])
            if r["subject"] and len(it["subjects"]) < 3 and r["subject"] not in it["subjects"]:
                it["subjects"].append(r["subject"])
        return {k: v for k, v in g.items() if len(v["threads"]) >= SUGGEST_AFTER}

    for sender, it in group("not_important", "sender").items():
        sid = f"low-from:{sender}"
        if sender in me or sender in senders_done or sid in dismissed:
            continue
        out.append({"id": sid, "kind": "deprioritize", "match": {"from_contains": [sender]},
                    "count": len(it["threads"]), "examples": it["subjects"],
                    "text": f"Письма от {sender} больше не поднимать в «Требуют внимания»"})
    for key, it in group("not_important", "subject_key").items():
        rx = _key_regex(key)
        sid = f"low-subject:{key}"
        if len(key.replace("#", "").strip()) < 6 or rx in subjects_done or sid in dismissed:
            continue
        out.append({"id": sid, "kind": "deprioritize", "match": {"subject_regex": [rx]},
                    "count": len(it["threads"]), "examples": it["subjects"],
                    "text": f"Письма с темой вида «{key}» больше не поднимать в «Требуют внимания»"})
    for sender, it in group("no_reply", "sender").items():
        sid = f"no-drafts:{sender}"
        if sender in me or sender in no_drafts_done or sid in dismissed:
            continue
        out.append({"id": sid, "kind": "no_drafts", "sender": sender,
                    "count": len(it["threads"]), "examples": it["subjects"],
                    "text": f"Не готовить ответы на письма от {sender}"})
    for n, s in enumerate(out, start=1):
        s["n"] = n
        s["times"] = plural(s["count"], "раз", "раза", "раз")
    return out


def apply_suggestion(cfg: Config, store: Store, sid: str) -> dict[str, Any]:
    sug = next((s for s in suggestions(cfg, store) if s["id"] == sid or str(s["n"]) == str(sid)), None)
    if not sug:
        raise ValueError("Такого предложения нет или оно уже применено")
    ov = read_overrides(cfg)
    if sug["kind"] == "deprioritize":
        cats = ov.setdefault("categories_add", [])
        if not any(c.get("id") == DEPRIORITIZED["id"] for c in cats):
            cats.append(dict(DEPRIORITIZED))
        ov.setdefault("rules", []).append({"category": DEPRIORITIZED["id"], "match": sug["match"]})
    else:
        lst = ov.setdefault("no_drafts_from", [])
        if sug["sender"] not in lst:
            lst.append(sug["sender"])
    write_overrides(cfg, ov)
    return sug


def dismiss_suggestion(cfg: Config, store: Store, sid: str) -> None:
    sug = next((s for s in suggestions(cfg, store) if s["id"] == sid or str(s["n"]) == str(sid)), None)
    key = sug["id"] if sug else sid
    d = _dismissed(store)
    d.add(key)
    store.set_kv("dismissed_suggestions", json.dumps(sorted(d), ensure_ascii=False))
    store.commit()
