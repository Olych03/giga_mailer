"""Сбор новых писем из почты в локальную базу."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any

from . import folders as fld
from .calendar_src import load_ics_files
from .config import Config
from .mail_source import MailError, search_all
from .rules import classify
from .store import Store
from .textutil import html_to_text, iso, normalize_subject, now_utc, parse_addresses, parse_dt, split_quote
from .threads import NO_THREAD

OVERLAP = timedelta(minutes=15)


def normalize(cfg: Config, raw: dict[str, Any], sent_mailbox: str | None) -> dict[str, Any]:
    text = raw.get("text") or ""
    if not text.strip() and raw.get("html"):
        text = html_to_text(raw["html"])
    new_text, _ = split_quote(text)
    from_addrs = parse_addresses(raw.get("from"))
    from_addr = from_addrs[0][1] if from_addrs else ""
    mailbox = raw.get("sourceMailbox") or raw.get("mailbox") or "INBOX"
    is_mine = from_addr in cfg.my_addresses or (sent_mailbox is not None and mailbox == sent_mailbox)
    incidents = sorted({i.upper() for i in cfg.incident_re.findall(f"{raw.get('subject') or ''}\n{new_text}")})
    refs = raw.get("references") or []
    if isinstance(refs, str):
        refs = refs.split()
    return {
        "mailbox": mailbox,
        "uid_validity": raw.get("uidValidity"),
        "uid": int(raw["uid"]),
        "message_id": raw.get("messageId"),
        "in_reply_to": raw.get("inReplyTo"),
        "refs": list(refs),
        "subject": raw.get("subject") or "",
        "norm_subject": normalize_subject(raw.get("subject")),
        "from_raw": raw.get("from") or "",
        "from_addr": from_addr,
        "to_raw": raw.get("to") or "",
        "cc_raw": raw.get("cc") or "",
        "date": iso(parse_dt(raw.get("date"))),
        "text": text,
        "new_text": new_text,
        "is_mine": int(is_mine),
        "incidents": incidents,
        "attachments": raw.get("attachments") or [],
        "fetched_at": iso(now_utc()),
    }


def _is_calendar(att: dict[str, Any]) -> bool:
    ct = (att.get("contentType") or "").lower()
    return ct.startswith("text/calendar") or (att.get("filename") or "").lower().endswith(".ics")


async def _get_batch(src: Any, mailbox: str, uids: list[int], uidv: int | None) -> list[dict[str, Any]]:
    """Забирает письма пачкой; если сервер отказал (слишком большие письма), берёт по одному."""
    try:
        return await src.get_messages(mailbox, uids, uidv)
    except MailError:
        if len(uids) == 1:
            print(f"  Не удалось получить письмо {mailbox}/{uids[0]}, пропускаю.")
            return []
        out: list[dict[str, Any]] = []
        for uid in uids:
            out.extend(await _get_batch(src, mailbox, [uid], uidv))
        return out


def mailboxes_to_search(cfg: Config) -> list[str] | None:
    inbox = cfg.get("mailboxes.inbox")
    sent = cfg.get("mailboxes.sent")
    extra = list(cfg.get("mailboxes.extra") or [])
    if not inbox and not sent and not extra:
        return None  # сервер сам возьмёт INBOX и найденную папку отправленных
    boxes = [b for b in [inbox or "INBOX", sent, *extra] if b]
    return list(dict.fromkeys(boxes))


async def _store_found(cfg: Config, store: Store, src: Any, summaries: list[dict[str, Any]],
                       stats: dict[str, int], folder_only: bool = False) -> None:
    sent_mailbox = cfg.get("mailboxes.sent")
    groups: dict[tuple[str, int | None], list[int]] = defaultdict(list)
    for s in summaries:
        key = (s.get("sourceMailbox") or "INBOX", s.get("uidValidity"))
        uid = int(s["uid"])
        if not store.has_message(key[0], key[1], uid) and uid not in groups[key]:
            groups[key].append(uid)

    batch = int(cfg.get("fetch.batch_size") or 20)
    for (mailbox, uidv), uids in groups.items():
        for i in range(0, len(uids), batch):
            for raw in await _get_batch(src, mailbox, uids[i : i + batch], uidv):
                raw.setdefault("sourceMailbox", mailbox)
                m = normalize(cfg, raw, sent_mailbox)
                m["category"] = classify(cfg, m).id
                if folder_only:
                    m["thread_id"] = NO_THREAD  # только для раздела «По папкам», в цепочки не идёт
                if store.insert_message(m):
                    stats["new"] += 1
                    if folder_only:
                        stats["folder_new"] += 1
                for idx, att in enumerate(m["attachments"]):
                    if _is_calendar(att):
                        target = cfg.data_dir / "attachments" / f"{m['uid_validity']}_{m['uid']}"
                        try:
                            files = await src.save_attachment(mailbox, m["uid"], uidv, att.get("index", idx), target)
                            stats["events"] += load_ics_files(store, files)
                        except MailError as exc:
                            print(f"  Не удалось сохранить приглашение из письма «{m['subject']}»: {exc}")
        store.commit()


async def fetch_new(cfg: Config, store: Store, src: Any, since: datetime | None = None,
                    until: datetime | None = None) -> dict[str, int]:
    until = until or now_utc()
    if since is None:
        wm = store.get_kv("fetch_watermark")
        since = parse_dt(wm) - OVERLAP if wm else until - timedelta(hours=int(cfg.get("fetch.initial_hours") or 24))

    summaries = await search_all(src, mailboxes_to_search(cfg), since, until)
    stats = {"found": len(summaries), "new": 0, "events": 0, "folder_new": 0, "folders": 0}
    await _store_found(cfg, store, src, summaries, stats)

    # Остальные папки ящика: всегда последние folders.hours часов, уже забранные письма пропускаются
    if fld.enabled(cfg):
        names = await fld.discover(cfg, store, src)
        stats["folders"] = len(names)
        if names:
            fsince = min(since, until - timedelta(hours=fld.window_hours(cfg)))
            found = await search_all(src, names, fsince, until)
            stats["found"] += len(found)
            await _store_found(cfg, store, src, found, stats, folder_only=not fld.in_main_digest(cfg))

    store.set_kv("fetch_watermark", iso(until))
    store.commit()
    return stats
