"""Сборка писем в цепочки и признаки цепочки: инциденты, эскалация, чей ход."""

from __future__ import annotations

import hashlib
from datetime import timedelta
from typing import Any

from .config import Config
from .store import Store
from .textutil import iso, match_person, parse_addresses, parse_dt, person_variants

SUBJECT_MATCH_DAYS = 14
INCIDENT_MATCH_DAYS = 45
NO_THREAD = ""  # письмо разобрано, но в цепочку не входит (уведомления)


def _category_rank(cfg: Config) -> dict[str, int]:
    return {c.id: i for i, c in enumerate(cfg.categories)}


def _new_thread_id(m: dict[str, Any]) -> str:
    seed = m.get("message_id") or f"{m['mailbox']}:{m.get('uid_validity')}:{m['uid']}"
    return "t" + hashlib.sha1(seed.encode("utf-8")).hexdigest()[:10]


def _find_thread(cfg: Config, store: Store, m: dict[str, Any]) -> str | None:
    refs = list(m.get("refs") or [])
    if m.get("in_reply_to"):
        refs.append(m["in_reply_to"])
    hits = store.find_by_message_ids(refs)
    hits = [h for h in hits if h.get("thread_id")]
    if hits:
        return hits[-1]["thread_id"]

    date = parse_dt(m["date"])
    for inc in m.get("incidents") or []:
        for t in store.threads_by_incident(inc):
            if parse_dt(t["last_date"]) >= date - timedelta(days=INCIDENT_MATCH_DAYS):
                return t["id"]

    ns = m.get("norm_subject") or ""
    if len(ns) >= 6:
        cands = store.threads_by_subject(ns, iso(date - timedelta(days=SUBJECT_MATCH_DAYS)))
        if cands:
            return cands[0]["id"]
    return None


def assign_threads(cfg: Config, store: Store) -> set[str]:
    """Раскладывает новые письма по цепочкам. Возвращает id затронутых цепочек."""
    touched: set[str] = set()
    rank = _category_rank(cfg)
    for m in store.messages_without_thread():
        cat = cfg.category(m.get("category"))
        if cat is None or not cat.analyze:
            store.update_message(m["id"], thread_id=NO_THREAD)
            continue
        tid = _find_thread(cfg, store, m) or _new_thread_id(m)
        store.update_message(m["id"], thread_id=tid)
        existing = store.thread(tid)
        category = m["category"]
        if existing and rank.get(existing["category"], 99) < rank.get(category, 99):
            category = existing["category"]
        store.upsert_thread({
            **(existing or {}),
            "id": tid,
            "category": category,
            "subject": (existing or {}).get("subject") or m.get("subject"),
            "norm_subject": (existing or {}).get("norm_subject") or m.get("norm_subject"),
            "first_date": (existing or {}).get("first_date") or m["date"],
            "last_date": m["date"],
        })
        touched.add(tid)
    for tid in touched:
        refresh_thread(cfg, store, tid)
    store.commit()
    return touched


def _recipients(m: dict[str, Any]) -> list[tuple[str, str]]:
    return parse_addresses(m.get("to_raw")) + parse_addresses(m.get("cc_raw"))


def compute_flags(cfg: Config, msgs: list[dict[str, Any]], category_importance: str) -> dict[str, Any]:
    me = set(cfg.my_addresses)
    kw = [k.lower() for k in cfg.get("escalation.keywords") or []]
    people = list(cfg.get("escalation.people") or [])
    my_side = [k.lower() for k in cfg.get("me.my_side_keywords") or []]
    long_n = int(cfg.get("escalation.long_thread_messages") or 6)

    keywords_found: list[str] = []
    my_side_found: list[str] = []
    people_found: list[str] = []
    for m in msgs:
        if not m.get("is_mine"):
            text = (m.get("new_text") or "").lower()
            for k in kw:
                if k in text and k not in keywords_found:
                    keywords_found.append(k)
            for k in my_side:
                if k in text and k not in my_side_found:
                    my_side_found.append(k)
        rec = f"{m.get('to_raw') or ''} {m.get('cc_raw') or ''}".lower()
        for p in people:
            if p not in people_found and any(v in rec for v in person_variants(p)):
                people_found.append(p)

    vip_list = list(cfg.get("digest.vip_senders") or [])
    vip = None
    for m in reversed(msgs):
        if not m.get("is_mine"):
            vip = match_person(vip_list, m.get("from_raw") or "")
            if vip:
                break

    first_rec = f"{msgs[0].get('to_raw') or ''} {msgs[0].get('cc_raw') or ''}".lower() if msgs else ""
    people_added = [p for p in people_found if not any(v in first_rec for v in person_variants(p))]

    last = msgs[-1] if msgs else {}
    last_mine_idx = max((i for i, m in enumerate(msgs) if m.get("is_mine")), default=-1)
    after_mine = msgs[last_mine_idx + 1 :]
    addressed_to_me = any(addr in me for _, addr in _recipients(last)) if last else False
    my_turn = bool(last) and not last.get("is_mine") and (addressed_to_me or category_importance == "high")
    incidents = sorted({i for m in msgs for i in (m.get("incidents") or [])})

    participants: list[str] = []
    for m in msgs:
        for name, addr in parse_addresses(m.get("from_raw")):
            label = name or addr
            if addr not in me and label not in participants:
                participants.append(label)

    return {
        "incidents": incidents,
        "escalation_keywords": keywords_found,
        "escalation_people": people_found,
        "people_added": people_added,
        "my_side_keywords": my_side_found,
        "my_turn": my_turn,
        "addressed_to_me": addressed_to_me,
        "i_replied": last_mine_idx >= 0,
        "waiting_since": after_mine[0]["date"] if (my_turn and after_mine) else None,
        "unanswered_count": len(after_mine) if my_turn else 0,
        "long_thread": len(after_mine) >= long_n,
        "escalation": bool(keywords_found or people_found),
        "participants": participants[:12],
        "vip": vip,
    }


def refresh_thread(cfg: Config, store: Store, tid: str) -> None:
    t = store.thread(tid)
    msgs = store.thread_messages(tid)
    if not t or not msgs:
        return
    cat = cfg.category(t["category"])
    flags = compute_flags(cfg, msgs, cat.importance if cat else "normal")
    t.update({
        "first_date": msgs[0]["date"],
        "last_date": msgs[-1]["date"],
        "msg_count": len(msgs),
        "incidents": flags["incidents"],
        "flags": flags,
        "updated_at": iso(parse_dt(None)),
    })
    store.upsert_thread(t)
