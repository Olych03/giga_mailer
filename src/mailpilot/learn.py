"""Обучение на ваших письмах: образцы ответов из «Отправленных», поиск похожих, профиль стиля."""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

from .config import Config
from .fetch import _get_batch, normalize
from .mail_source import search_all
from .store import Store
from .textutil import iso, now_utc, one_line, split_quote, stems, strip_quote_headers, truncate

MAX_REPLY_CHARS = 1500


def example_from_sent(m: dict[str, Any]) -> dict[str, Any] | None:
    """Из вашего отправленного ответа берём пару: что вам написали (цитата) и что вы ответили."""
    reply, quoted = split_quote(m.get("text") or "")
    incoming = strip_quote_headers(quoted) if quoted else ""
    if not reply or not incoming or len(reply) > MAX_REPLY_CHARS:
        return None
    return {
        "source": "sent",
        "subject": m.get("subject"),
        "incoming": truncate(incoming, 2000),
        "reply": reply,
        "terms": " ".join(sorted(stems(f"{m.get('subject') or ''} {incoming}", limit=120))),
        "date": m.get("date"),
    }


async def learn_from_sent(cfg: Config, store: Store, src: Any, days: int) -> dict[str, int]:
    sent = cfg.get("mailboxes.sent")
    if not sent:
        raise SystemExit("Укажите имя папки «Отправленные» в mailboxes.sent (точное имя покажет `mailpilot doctor`).")
    until = now_utc()
    summaries = await search_all(src, [sent], until - timedelta(days=days), until)
    uids_by_v: dict[Any, list[int]] = {}
    for s in summaries:
        uids_by_v.setdefault(s.get("uidValidity"), []).append(int(s["uid"]))
    added = 0
    batch = int(cfg.get("fetch.batch_size") or 20)
    for uidv, uids in uids_by_v.items():
        for i in range(0, len(uids), batch):
            for raw in await _get_batch(src, sent, uids[i : i + batch], uidv):
                raw.setdefault("sourceMailbox", sent)
                ex = example_from_sent(normalize(cfg, raw, sent))
                if ex:
                    store.add_example(ex)
                    added += 1
        store.commit()
    return {"sent_found": len(summaries), "examples": added}


def similar_examples(store: Store, text: str, k: int = 3, _cache: dict[str, Any] = {}) -> list[dict[str, Any]]:
    """Ищет ваши прошлые ответы на похожие письма по совпадению слов."""
    query = stems(text, limit=150)
    if not query:
        return []
    rows = _cache.get(str(store.path))
    if rows is None:
        rows = [(r, set((r.get("terms") or "").split())) for r in store.examples()]
        _cache[str(store.path)] = rows
    scored = []
    for row, terms in rows:
        if not terms:
            continue
        overlap = len(query & terms)
        if overlap < 3:
            continue
        score = overlap / (len(query | terms) ** 0.5)
        # Ваши правки черновиков ценнее старых писем
        if row["source"] == "edit":
            score *= 1.3
        scored.append((score, row))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [{"incoming": truncate(r["incoming"] or "", 700), "reply": r["reply"]} for _, r in scored[:k]]


def write_style_request(cfg: Config, store: Store, sample: int = 40) -> str:
    """Готовит пакет для агента: по нему он пишет профиль стиля в data/style.md."""
    rows = store.examples()
    edits = [r for r in rows if r["source"] == "edit"][:20]
    sent = [r for r in rows if r["source"] == "sent"]
    # Берём разнообразные ответы: разной длины и свежие
    sent.sort(key=lambda r: (len(r["reply"]) // 200, r["date"] or ""), reverse=True)
    picked = sent[: sample]
    payload = {
        "task": "style_profile",
        "output_file": str(cfg.style_path),
        "current_profile": cfg.style_path.read_text(encoding="utf-8") if cfg.style_path.exists() else None,
        "replies": [{"to_what": one_line(r["incoming"] or "", 300), "reply": r["reply"]} for r in picked],
        "edits": [{"to_what": one_line(r["incoming"] or "", 300), "final_reply": r["reply"]} for r in edits],
    }
    cfg.work_dir.mkdir(parents=True, exist_ok=True)
    out = cfg.work_dir / "style_request.json"
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    store.set_kv("style_request_at", iso(now_utc()))
    store.commit()
    return str(out)
