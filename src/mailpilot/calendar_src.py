"""Календарь: десктопный Outlook на Windows или приглашения (.ics) из писем на любой ОС."""

from __future__ import annotations

import re
import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any

from .config import Config
from .store import Store
from .textutil import iso, now_utc


def calendar_source(cfg: Config) -> str:
    src = cfg.get("calendar.source") or "auto"
    if src == "auto":
        if sys.platform == "win32":
            try:
                import win32com.client  # noqa: F401

                return "outlook"
            except ImportError:
                return "ics_mail"
        return "ics_mail"
    return src


# --- разбор .ics -----------------------------------------------------------------

def _unfold(text: str) -> list[str]:
    lines: list[str] = []
    for raw in text.replace("\r\n", "\n").split("\n"):
        if raw.startswith((" ", "\t")) and lines:
            lines[-1] += raw[1:]
        else:
            lines.append(raw)
    return lines


def _ics_value(v: str) -> str:
    return v.replace("\\n", "\n").replace("\\N", "\n").replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\").strip()


def _ics_dt(params: str, value: str) -> datetime:
    value = value.strip()
    if "VALUE=DATE" in params.upper() or re.fullmatch(r"\d{8}", value):
        d = datetime.strptime(value[:8], "%Y%m%d")
        return d.astimezone()
    if value.endswith("Z"):
        return datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    # Время с TZID считаем местным временем машины: для одного часового пояса этого достаточно
    return datetime.strptime(value[:15], "%Y%m%dT%H%M%S").astimezone()


def parse_ics(text: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    method = ""
    cur: dict[str, Any] | None = None
    for line in _unfold(text):
        if ":" not in line:
            continue
        name_params, value = line.split(":", 1)
        name, _, params = name_params.partition(";")
        name = name.upper()
        if name == "METHOD":
            method = value.strip().upper()
        elif name == "BEGIN" and value.strip().upper() == "VEVENT":
            cur = {}
        elif name == "END" and value.strip().upper() == "VEVENT" and cur is not None:
            if cur.get("uid") and cur.get("start"):
                cur.setdefault("end", cur["start"] + timedelta(hours=1))
                if method == "CANCEL":
                    cur["status"] = "CANCELLED"
                events.append(cur)
            cur = None
        elif cur is not None:
            if name == "UID":
                cur["uid"] = value.strip()
            elif name == "SUMMARY":
                cur["subject"] = _ics_value(value)
            elif name == "LOCATION":
                cur["location"] = _ics_value(value)
            elif name == "STATUS":
                cur["status"] = value.strip().upper()
            elif name == "ORGANIZER":
                m = re.search(r'CN="?([^";:]+)', params)
                cur["organizer"] = m.group(1) if m else value.replace("mailto:", "").replace("MAILTO:", "")
            elif name == "DTSTART":
                cur["start"] = _ics_dt(params, value)
            elif name == "DTEND":
                cur["end"] = _ics_dt(params, value)
            elif name == "RRULE":
                cur["recurring"] = True
    return events


def store_ics_events(store: Store, events: list[dict[str, Any]], source: str = "ics_mail") -> int:
    for ev in events:
        store.upsert_event({
            "uid": ev["uid"],
            "start": iso(ev["start"]),
            "end": iso(ev["end"]),
            "subject": ev.get("subject") or "(без темы)",
            "location": ev.get("location"),
            "organizer": ev.get("organizer"),
            "status": ev.get("status"),
            "source": source,
            "updated_at": iso(now_utc()),
        })
    return len(events)


def load_ics_files(store: Store, files: list[Path]) -> int:
    n = 0
    for f in files:
        try:
            n += store_ics_events(store, parse_ics(f.read_text(encoding="utf-8", errors="replace")))
        except (OSError, ValueError):
            continue
    return n


# --- десктопный Outlook (Windows) ------------------------------------------------

def outlook_events(start: datetime, end: datetime) -> list[dict[str, Any]]:
    import win32com.client  # type: ignore[import-not-found]

    ns = win32com.client.Dispatch("Outlook.Application").GetNamespace("MAPI")
    items = ns.GetDefaultFolder(9).Items  # 9 = календарь
    items.IncludeRecurrences = True
    items.Sort("[Start]")
    local_start, local_end = start.astimezone(), end.astimezone()
    restricted = None
    # Формат даты в фильтре Outlook зависит от региональных настроек, пробуем оба
    for fmt in ("%d.%m.%Y %H:%M", "%m/%d/%Y %I:%M %p"):
        try:
            flt = f"[Start] < '{local_end.strftime(fmt)}' AND [End] > '{local_start.strftime(fmt)}'"
            restricted = items.Restrict(flt)
            break
        except Exception:  # noqa: BLE001 — COM бросает свои исключения
            continue
    if restricted is None:
        return []
    events = []
    for it in restricted:
        try:
            s = datetime.fromtimestamp(it.Start.timestamp()).astimezone()
            e = datetime.fromtimestamp(it.End.timestamp()).astimezone()
        except Exception:  # noqa: BLE001
            continue
        if not (s < local_end and e > local_start):
            continue
        events.append({
            "uid": f"outlook:{it.EntryID}:{s.isoformat()}",
            "start": s,
            "end": e,
            "subject": it.Subject,
            "location": it.Location,
            "organizer": getattr(it, "Organizer", None),
            "status": "CANCELLED" if "отмен" in (it.Subject or "").lower() or "cancel" in (it.Subject or "").lower() else None,
        })
    return events


def refresh_calendar(cfg: Config, store: Store) -> str:
    """Обновляет события из Outlook (на Windows). Приглашения из писем подхватываются при сборе почты."""
    src = calendar_source(cfg)
    if src != "outlook":
        return src
    start = now_utc().astimezone().replace(hour=0, minute=0, second=0)
    end = start + timedelta(days=5)
    try:
        events = outlook_events(start, end)
    except Exception as exc:  # noqa: BLE001
        print(f"Не удалось прочитать календарь Outlook: {exc}. Беру встречи из приглашений в почте.")
        return "ics_mail"
    store.db.execute("DELETE FROM events WHERE source='outlook' AND start>=? AND start<?", (iso(start), iso(end)))
    store_ics_events(store, events, source="outlook")
    store.commit()
    return src


def agenda_window(now: datetime | None = None) -> list[tuple[str, datetime, datetime]]:
    """Остаток сегодняшнего дня и следующий рабочий день."""
    now = (now or now_utc()).astimezone()
    today_end = datetime.combine(now.date() + timedelta(days=1), time.min, tzinfo=now.tzinfo)
    nxt: date = now.date() + timedelta(days=1)
    while nxt.weekday() >= 5:
        nxt += timedelta(days=1)
    nxt_start = datetime.combine(nxt, time.min, tzinfo=now.tzinfo)
    label = "Завтра" if nxt == now.date() + timedelta(days=1) else day_label(nxt_start)
    return [("Сегодня, до конца дня", now, today_end), (label, nxt_start, nxt_start + timedelta(days=1))]


WEEKDAYS_RU = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]


def day_label(d: datetime) -> str:
    return f"{WEEKDAYS_RU[d.weekday()].capitalize()}, {d.strftime('%d.%m')}"


def find_conflicts(events: list[dict[str, Any]]) -> set[str]:
    clash: set[str] = set()
    evs = sorted(events, key=lambda e: e["start"])
    for i, a in enumerate(evs):
        for b in evs[i + 1 :]:
            if b["start"] >= a["end"]:
                break
            clash.update({a["uid"], b["uid"]})
    return clash
