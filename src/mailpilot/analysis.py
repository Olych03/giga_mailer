"""Пакеты на смысловой анализ для агента (gigacode со скиллом) и приём его результатов.

Модель вызывает не скрипт, а агент: скрипт кладёт в data/work/ файлы batch_NN.json,
агент читает их и пишет result_NN.json, затем `mailpilot ingest` сохраняет результат.
"""

from __future__ import annotations

import json
import re
import shutil
from datetime import timedelta
from typing import Any

from .config import Config
from .learn import similar_examples
from .store import Store
from .textutil import display_name, iso, local, now_utc, one_line, parse_dt, truncate

ANALYSIS_LOOKBACK_DAYS = 7

RESULT_SCHEMA = {
    "thread_id": "id цепочки из пакета (обязательно)",
    "summary": "суть всей переписки с начала, 1–3 предложения",
    "status": "одно из: ждёт меня | ждёт других | в работе | решено | к сведению",
    "needs_me": "true, если от меня нужно действие или ответ",
    "action_items": ["что мне сделать, коротко"],
    "my_side_problem": "что не так на моей стороне и что поправить, или null",
    "conflict": "в чём конфликт или эскалация, или null",
    "join_recommended": "true, если мне стоит подключиться к переписке",
    "join_reason": "почему стоит подключиться, или null",
    "priority": "1 — срочно, 2 — сегодня-завтра, 3 — не горит",
    "draft": "текст ответа от моего имени, если ответ простой и allow_draft=true, иначе null",
    "reply_all": "true, если отвечать всем",
}


def _msg_entry(m: dict[str, Any], full: bool, max_chars: int) -> dict[str, Any]:
    entry = {
        "date": local(m["date"]).strftime("%d.%m %H:%M"),
        "from": "Я" if m.get("is_mine") else (m.get("from_raw") or ""),
    }
    if full:
        entry["to"] = m.get("to_raw") or ""
        if m.get("cc_raw"):
            entry["cc"] = m["cc_raw"]
        entry["text"] = truncate(m.get("new_text") or m.get("text") or "", max_chars)
    else:
        entry["gist"] = one_line(m.get("new_text") or "", 200)
    return entry


def threads_to_analyze(cfg: Config, store: Store) -> list[dict[str, Any]]:
    since = iso(now_utc() - timedelta(days=ANALYSIS_LOOKBACK_DAYS))
    out = []
    for t in store.threads_active_since(since):
        cat = cfg.category(t["category"])
        if not cat or not cat.analyze:
            continue
        if t.get("analyzed_upto") and t["analyzed_upto"] >= t["last_date"]:
            continue
        out.append(t)
    # ПРОМ и эскалации первыми, чтобы при нехватке времени разобрать главное
    rank = {c.id: i for i, c in enumerate(cfg.categories)}
    out.sort(key=lambda t: (rank.get(t["category"], 99), not (t.get("flags") or {}).get("escalation"), t["last_date"]))
    return out


def prepare(cfg: Config, store: Store) -> list[str]:
    work = cfg.work_dir
    work.mkdir(parents=True, exist_ok=True)
    for old in list(work.glob("batch_*.json")) + list(work.glob("result_*.json")):
        old.unlink()

    max_chars = int(cfg.get("analysis.max_message_chars") or 2500)
    max_full = int(cfg.get("analysis.max_full_messages") or 10)
    per_batch = int(cfg.get("analysis.batch_threads") or 8)
    k_examples = int(cfg.get("analysis.examples_per_thread") or 3)
    style = cfg.style_path.read_text(encoding="utf-8") if cfg.style_path.exists() else None

    entries = []
    for t in threads_to_analyze(cfg, store):
        cat = cfg.category(t["category"])
        msgs = store.thread_messages(t["id"])
        flags = t.get("flags") or {}
        state = t.get("state")
        upto = t.get("analyzed_upto")
        items = []
        for i, m in enumerate(msgs):
            is_new = not upto or m["date"] > upto
            full = (is_new or not state) and i >= len(msgs) - max_full
            e = _msg_entry(m, full, max_chars)
            e["new"] = bool(is_new)
            items.append(e)
        allow_draft = bool(cat and cat.drafts and flags.get("my_turn"))
        last_incoming = next((m for m in reversed(msgs) if not m.get("is_mine")), None)
        entry = {
            "thread_id": t["id"],
            "category": cat.title if cat else t["category"],
            "subject": t["subject"],
            "started": local(t["first_date"]).strftime("%d.%m.%Y"),
            "messages_total": len(msgs),
            "incidents": flags.get("incidents") or [],
            "signals": {
                "escalation_keywords": flags.get("escalation_keywords") or [],
                "managers_in_recipients": flags.get("escalation_people") or [],
                "managers_added_later": flags.get("people_added") or [],
                "my_side_keywords": flags.get("my_side_keywords") or [],
                "my_turn": flags.get("my_turn"),
                "unanswered_messages": flags.get("unanswered_count"),
            },
            "previous_state": state,
            "messages": items,
            "allow_draft": allow_draft,
            "upto": t["last_date"],
        }
        if allow_draft and last_incoming:
            entry["examples_of_my_replies"] = similar_examples(
                store, f"{t['subject']} {last_incoming.get('new_text') or ''}", k_examples
            )
        entries.append(entry)

    files = []
    me = {
        "name": cfg.get("me.name"),
        "addresses": cfg.my_addresses,
        "my_side_keywords": cfg.get("me.my_side_keywords") or [],
    }
    for n, i in enumerate(range(0, len(entries), per_batch), start=1):
        batch = {
            "batch": n,
            "result_file": str(work / f"result_{n:02d}.json"),
            "me": me,
            "style_profile": style,
            "result_schema": RESULT_SCHEMA,
            "threads": entries[i : i + per_batch],
        }
        p = work / f"batch_{n:02d}.json"
        p.write_text(json.dumps(batch, ensure_ascii=False, indent=2), encoding="utf-8")
        files.append(str(p))
    store.set_kv("prepared_at", iso(now_utc()))
    store.set_kv("prepared_batches", str(len(files)))
    store.commit()
    return files


def _load_json_lenient(text: str) -> Any:
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:
        text = fence.group(1)
    return json.loads(text)


def _as_bool(v: Any) -> bool:
    if isinstance(v, str):
        return v.strip().lower() in ("true", "да", "yes", "1")
    return bool(v)


def ingest(cfg: Config, store: Store) -> dict[str, Any]:
    work = cfg.work_dir
    stats: dict[str, Any] = {"threads": 0, "drafts": 0, "missing": [], "errors": []}
    batches = sorted(work.glob("batch_*.json"))
    if not batches:
        return stats
    done_dir = work / "done" / now_utc().astimezone().strftime("%Y%m%d_%H%M%S")
    for bpath in batches:
        batch = json.loads(bpath.read_text(encoding="utf-8"))
        rpath = work / f"result_{batch['batch']:02d}.json"
        if not rpath.exists():
            stats["missing"].append(rpath.name)
            continue
        try:
            result = _load_json_lenient(rpath.read_text(encoding="utf-8"))
        except ValueError as exc:
            stats["errors"].append(f"{rpath.name}: не JSON ({exc})")
            continue
        items = result.get("threads") if isinstance(result, dict) else result
        meta = {t["thread_id"]: t for t in batch["threads"]}
        for item in items or []:
            tid = item.get("thread_id")
            if tid not in meta:
                stats["errors"].append(f"{rpath.name}: неизвестная цепочка {tid}")
                continue
            _apply_result(store, item, meta[tid])
            stats["threads"] += 1
            if meta[tid]["allow_draft"] and (item.get("draft") or "").strip():
                if _make_draft(store, tid, item):
                    stats["drafts"] += 1
        done_dir.mkdir(parents=True, exist_ok=True)
        shutil.move(str(bpath), done_dir / bpath.name)
        shutil.move(str(rpath), done_dir / rpath.name)
    store.commit()
    return stats


def _apply_result(store: Store, item: dict[str, Any], meta: dict[str, Any]) -> None:
    t = store.thread(item["thread_id"])
    if not t:
        return
    state = {
        "summary": item.get("summary"),
        "status": item.get("status"),
        "needs_me": _as_bool(item.get("needs_me")),
        "action_items": [a for a in (item.get("action_items") or []) if a],
        "my_side_problem": item.get("my_side_problem") or None,
        "conflict": item.get("conflict") or None,
        "join_recommended": _as_bool(item.get("join_recommended")),
        "join_reason": item.get("join_reason") or None,
        "priority": int(item.get("priority") or 3) if str(item.get("priority") or "3").isdigit() else 3,
        "analyzed_at": iso(now_utc()),
    }
    t["state"] = state
    t["analyzed_upto"] = meta["upto"]
    store.upsert_thread(t)


def _make_draft(store: Store, tid: str, item: dict[str, Any]) -> bool:
    msgs = store.thread_messages(tid)
    target = next((m for m in reversed(msgs) if not m.get("is_mine")), None)
    if not target:
        return False
    store.db.execute("UPDATE drafts SET status='superseded' WHERE thread_id=? AND status='proposed'", (tid,))
    store.add_draft({
        "thread_id": tid,
        "reply_to_id": target["id"],
        "reply_all": _as_bool(item.get("reply_all", True)),
        "text_proposed": item["draft"].strip(),
        "created_at": iso(now_utc()),
    })
    return True


def analysis_status(cfg: Config, store: Store) -> dict[str, Any]:
    pending = sorted(cfg.work_dir.glob("batch_*.json")) if cfg.work_dir.exists() else []
    return {
        "pending_batches": len(pending),
        "prepared_at": store.get_kv("prepared_at"),
        "threads_waiting": len(threads_to_analyze(cfg, store)),
    }


def sender_label(m: dict[str, Any]) -> str:
    return "Я" if m.get("is_mine") else display_name(m.get("from_raw"))


def thread_timeline(store: Store, tid: str, limit: int = 8) -> list[dict[str, str]]:
    msgs = store.thread_messages(tid)
    tail = msgs[-limit:]
    out = [{"date": local(m["date"]).strftime("%d.%m %H:%M"), "who": sender_label(m),
            "gist": one_line(m.get("new_text") or "", 140), "mine": bool(m.get("is_mine"))} for m in tail]
    if len(msgs) > limit:
        out.insert(0, {"date": "", "who": "", "gist": f"…ещё {len(msgs) - limit} писем раньше", "mine": False})
    return out


def days_ago(date_iso: str) -> int:
    return (now_utc().date() - parse_dt(date_iso).date()).days
