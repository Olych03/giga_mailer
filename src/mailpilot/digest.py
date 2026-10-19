"""Сборка данных для сводки и запись HTML-файла."""

from __future__ import annotations

import hashlib
import webbrowser
from datetime import timedelta
from pathlib import Path
from typing import Any

from . import feedback as fb
from . import folders as fld
from .analysis import analysis_status, days_ago, thread_timeline
from .calendar_src import agenda_window, find_conflicts
from .config import Config
from .extract import confluence_items, jira_items
from .render import render_html
from .store import Store
from .textutil import display_name, iso, local, match_person, now_utc, one_line, parse_addresses, parse_dt, plural

IMPORTANCE_ORDER = {"high": 0, "normal": 1, "low": 2, "hidden": 3}
STATUS_DONE = {"решено", "к сведению"}


def _fmt(dt_iso: str, with_date: bool = True) -> str:
    d = local(dt_iso)
    return d.strftime("%d.%m %H:%M") if with_date else d.strftime("%H:%M")


def _known_people(cfg: Config) -> list[str]:
    return list(cfg.get("digest.vip_senders") or []) + list(cfg.get("escalation.people") or [])


def pretty_person(cfg: Config, raw: str) -> str:
    """Сервер отдаёт только адреса, поэтому знакомых по настройкам людей показываем по имени."""
    if not raw or raw == "Я":
        return raw
    known = match_person(_known_people(cfg), raw)
    return known or display_name(raw)


def _why(cat: Any, flags: dict[str, Any], state: dict[str, Any], label: str | None) -> list[str]:
    """Почему переписка попала наверх: по этим причинам удобно понять, что сработало зря."""
    why = []
    if label == "important":
        why.append("вы отметили как важное")
    if flags.get("vip"):
        why.append(f"пишет {flags['vip']}")
    if flags.get("my_turn"):
        why.append("ждут вашего ответа")
    elif state.get("needs_me"):
        why.append("агент считает, что нужно ваше действие")
    if flags.get("escalation_keywords"):
        why.append("в тексте " + ", ".join(f"«{k}»" for k in flags["escalation_keywords"]))
    if flags.get("escalation_people"):
        why.append("в адресатах " + ", ".join(flags["escalation_people"]))
    if flags.get("incidents"):
        why.append("инцидент " + ", ".join(flags["incidents"][:3]))
    if flags.get("my_side_keywords"):
        why.append("упоминают " + ", ".join(flags["my_side_keywords"]))
    if state.get("conflict"):
        why.append("агент видит конфликт")
    if state.get("join_recommended"):
        why.append("агент советует подключиться")
    if flags.get("long_thread"):
        why.append("длинная переписка без вашего ответа")
    if cat and cat.importance == "high":
        why.append(f"категория «{cat.title}» отмечена как важная")
    return why


def _thread_item(cfg: Config, store: Store, t: dict[str, Any], period_from: str,
                 drafts_by_thread: dict[str, dict[str, Any]], label: str | None = None) -> dict[str, Any]:
    flags = t.get("flags") or {}
    state = t.get("state") or {}
    cat = cfg.category(t["category"])
    msgs = store.thread_messages(t["id"])
    done = (state.get("status") or "") in STATUS_DONE
    last_in = next((m for m in reversed(msgs) if not m.get("is_mine")), None)

    meta = [cat.title if cat else t["category"]]
    days = days_ago(t["first_date"])
    if days:
        meta.append(f"{days} дн.")
    meta.append(plural(t["msg_count"], "письмо", "письма", "писем"))
    if last_in:
        meta.append(f"от {pretty_person(cfg, last_in.get('from_raw') or '')}")

    tags = [i for i in flags.get("incidents") or [] if i.lower() not in (t["subject"] or "").lower()]
    alerts = []
    if state.get("my_side_problem"):
        alerts.append(("На вашей стороне", state["my_side_problem"], "red"))
    elif flags.get("my_side_keywords") and not state:
        alerts.append(("Упоминают вашу сторону", ", ".join(flags["my_side_keywords"]), "red"))
    if state.get("conflict"):
        alerts.append(("Конфликт", state["conflict"], "amber"))
    elif flags.get("escalation") and not state:
        who = ", ".join(flags.get("escalation_people") or [])
        words = ", ".join(f"«{k}»" for k in flags.get("escalation_keywords") or [])
        alerts.append(("Эскалация", "; ".join(x for x in [f"в адресатах {who}" if who else "", words] if x), "amber"))
    if state.get("join_recommended"):
        alerts.append(("Стоит подключиться", state.get("join_reason") or "ждут вашего участия", "violet"))

    waiting = days_ago(flags["waiting_since"]) if (flags.get("waiting_since") and not done) else None
    status = state.get("status") or ("ждёт вас" if flags.get("my_turn") else "")
    if waiting:
        status = f"{status}, {waiting} дн." if status else f"ждёт {waiting} дн."

    timeline = thread_timeline(store, t["id"])
    for e in timeline:
        if e["who"] and e["who"] != "Я":
            e["who"] = pretty_person(cfg, e["who"])

    needs_me = bool(flags.get("my_turn") or state.get("needs_me"))
    return {
        "id": t["id"],
        "subject": t["subject"] or "(без темы)",
        "category": t["category"],
        "importance": cat.importance if cat else "normal",
        "meta": " · ".join(meta),
        "color": cat.color if cat else "#9ca3af",
        "status_tone": "green" if done else "red" if (flags.get("escalation") or state.get("conflict")) else "amber" if needs_me else "neutral",
        "tags": tags,
        "incident_ids": list(flags.get("incidents") or []),
        "status": status,
        "alerts": alerts,
        "summary": state.get("summary") or one_line((last_in or {}).get("new_text") or "", 240),
        "analyzed": bool(state),
        "actions": state.get("action_items") or [],
        "priority": state.get("priority") or (1 if flags.get("escalation") else 2 if needs_me else 3),
        "escalation": bool(flags.get("escalation") or state.get("conflict")),
        "vip": flags.get("vip"),
        "needs_me": needs_me,
        "attention": not done and label != "not_important" and (needs_me or bool(alerts) or label == "important"),
        "label": label,
        "why": _why(cat, flags, state, label),
        "done": done,
        "last_iso": t["last_date"],
        "timeline": timeline,
        "draft": drafts_by_thread.get(t["id"]),
    }


def _sender_groups(cfg: Config, msgs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_sender: dict[str, dict[str, Any]] = {}
    for m in msgs:
        addr = m.get("from_addr") or m.get("from_raw") or "?"
        it = by_sender.setdefault(addr, {"sender": pretty_person(cfg, m.get("from_raw") or ""), "addr": addr,
                                         "count": 0, "subjects": []})
        it["count"] += 1
        if len(it["subjects"]) < 3:
            it["subjects"].append(one_line(m.get("subject") or "", 110))
    return sorted(by_sender.values(), key=lambda i: (-i["count"], i["sender"]))


def build_digest(cfg: Config, store: Store, period_from: str, period_to: str) -> dict[str, Any]:
    msgs = store.messages_between(period_from, period_to)
    folder_only = set(fld.scanned(store)) if not fld.in_main_digest(cfg) else set()
    msgs = [m for m in msgs if m["mailbox"] not in folder_only]  # они в разделе «По папкам»
    incoming = [m for m in msgs if not m.get("is_mine")]
    carry_since = iso(parse_dt(period_to) - timedelta(days=int(cfg.get("digest.carry_over_days") or 5)))
    drafts_by_thread = {d["thread_id"]: d for d in store.drafts(("proposed",))}
    user_labels = fb.labels(store)

    # Цепочки: активные за период плюс более старые, где всё ещё ждут вас
    items = []
    for t in store.threads_active_since(carry_since):
        cat = cfg.category(t["category"])
        if not cat or cat.importance == "hidden":
            continue
        flags, state = t.get("flags") or {}, t.get("state") or {}
        pending = (flags.get("my_turn") or state.get("needs_me")) and (state.get("status") or "") not in STATUS_DONE
        if t["last_date"] >= period_from or pending:
            items.append(_thread_item(cfg, store, t, period_from, drafts_by_thread, user_labels.get(t["id"])))

    # 1. Важные люди — всегда первыми
    vip = [i for i in items if i["vip"] and not i["done"] and i["label"] != "not_important"]
    vip.sort(key=lambda i: i["last_iso"], reverse=True)
    # 2. Требует внимания — в порядке важности категорий, затем эскалации и срочности
    rank = {"high": 0, "normal": 1, "low": 2}
    # Ваши отметки сильнее правил: «важно» поднимает переписку, «не важно» убирает её отсюда
    attention = [i for i in items if i not in vip and i["attention"]
                 and (i["importance"] in ("high", "normal") or i["label"] == "important")]
    attention.sort(key=lambda i: i["last_iso"], reverse=True)
    attention.sort(key=lambda i: (i["label"] != "important", rank.get(i["importance"], 2), i["priority"], not i["escalation"]))

    # 3. Все новые письма по категориям (обзор)
    overview = []
    for cat in sorted(cfg.categories, key=lambda c: IMPORTANCE_ORDER.get(c.importance, 1)):
        if cat.importance == "hidden":
            continue
        cat_msgs = [m for m in incoming if m.get("category") == cat.id]
        group: dict[str, Any] = {"id": cat.id, "title": cat.title, "color": cat.color, "count": len(cat_msgs)}
        if cat.analyze:
            threads = [i for i in items if i["category"] == cat.id]
            if not threads and not cat_msgs:
                continue
            threads.sort(key=lambda i: i["last_iso"], reverse=True)
            threads.sort(key=lambda i: (i["done"], not i["attention"]))
            need = sum(1 for i in threads if i["attention"])
            group.update(kind="threads", entries=threads,
                         line=plural(len(threads), "переписка", "переписки", "переписок")
                         + (f", ждут вас {need}" if need else ""))
        elif cat.extractor == "jira":
            if not cat_msgs:
                continue
            tasks = jira_items(cat_msgs)
            group.update(kind="jira", entries=tasks, line=plural(len(tasks), "задача", "задачи", "задач") + ": " + ", ".join(t["key"] for t in tasks[:3]))
        elif cat.extractor == "confluence":
            if not cat_msgs:
                continue
            spaces = confluence_items(cat_msgs)
            pages = [p for sp in spaces for p in sp["pages"]]
            group.update(kind="confluence", entries=spaces,
                         line=plural(len(pages), "страница изменена", "страницы изменены", "страниц изменено"))
        else:
            if not cat_msgs:
                continue
            senders = _sender_groups(cfg, cat_msgs)
            group.update(kind="plain", entries=senders, line=plural(len(senders), "отправитель", "отправителя", "отправителей"))
        overview.append(group)

    # 4. Подготовленные ответы
    drafts = []
    for d in store.drafts(("proposed",)):
        target = store.message(d["reply_to_id"]) or {}
        t = store.thread(d["thread_id"]) or {}
        me = set(cfg.my_addresses)
        copy = [pretty_person(cfg, a) for _, a in parse_addresses(f"{target.get('to_raw') or ''}, {target.get('cc_raw') or ''}")
                if a and a not in me] if d["reply_all"] else []
        drafts.append({"id": d["id"], "thread_id": d["thread_id"], "subject": t.get("subject") or target.get("subject"),
                       "to": pretty_person(cfg, target.get("from_raw") or ""), "reply_all": bool(d["reply_all"]),
                       "cc": copy, "text": d.get("text_final") or d["text_proposed"],
                       "signature": (cfg.get("signature") or "").strip()})

    # Календарь
    agenda = []
    for label, start, end in agenda_window(parse_dt(period_to)):
        evs = [{**e, "start": parse_dt(e["start"]), "end": parse_dt(e["end"])}
               for e in store.events_between(iso(start), iso(end))]
        clash = find_conflicts(evs)
        agenda.append({"label": label, "conflicts": len(clash), "events": [
            {"time": e["start"].astimezone().strftime("%H:%M"), "end": e["end"].astimezone().strftime("%H:%M"),
             "subject": e["subject"], "location": e.get("location"), "conflict": e["uid"] in clash} for e in evs]})

    # По папкам: где за последние сутки появились письма
    folder_groups = folder_overview(cfg, store, period_to)

    # Карточки-счётчики сверху: что показывать, задаётся в digest.tiles
    escalations = [i for i in vip + attention if i["escalation"]]
    incidents = sorted({tag for i in items if not i["done"] for tag in i["incident_ids"]})
    values = {
        "new": (len(incoming), "new", "за период"),
        "attention": (len(attention), "attention", "ждут вашего действия"),
        "vip": (len(vip), "vip", "переписки с важными людьми"),
        "drafts": (len(drafts), "drafts", "ждут вашего согласования"),
        "escalations": (len(escalations), "attention", "переписки с эскалацией"),
        "incidents": (len(incidents), "attention", "открытые инциденты INC"),
        "calendar": (sum(len(d["events"]) for d in agenda[1:]), "calendar", agenda[1]["label"].lower() + " встреч" if len(agenda) > 1 else "встреч"),
        "folders": (len(folder_groups), "folders", plural(sum(g["count"] for g in folder_groups), "письмо", "письма", "писем") + " в папках за сутки"),
    }
    tiles = []
    tile_warnings = []
    if cfg.get("tiles") is not None and cfg.get("digest.tiles") is None:
        tile_warnings.append("список tiles стоит вне раздела digest, поэтому показаны карточки по умолчанию; "
                             "сдвиньте его внутрь digest на два пробела")
    for spec in normalize_tiles(cfg):
        kind = spec.get("type")
        if kind == "category":
            cat = cfg.category(spec.get("category"))
            if not cat:
                tile_warnings.append(f"не показана карточка: нет категории «{spec.get('category')}»")
                continue
            n = sum(1 for m in incoming if m.get("category") == cat.id)
            tiles.append({"title": spec.get("title") or cat.title, "value": n, "anchor": f"cat-{cat.id}",
                          "hint": "за период", "tone": spec.get("tone") or "neutral"})
        elif kind in values:
            n, anchor, hint = values[kind]
            tiles.append({"title": spec.get("title") or TILE_TITLES[kind], "value": n, "anchor": anchor,
                          "hint": hint, "tone": spec.get("tone") or TILE_TONES.get(kind, "neutral")})
        else:
            tile_warnings.append(f"не показана карточка: неизвестный тип «{kind}»")

    titles = {t["anchor"]: t["title"] for t in reversed(tiles)}
    # Порядок разделов постоянный: важные люди всегда в самом верху
    order = list(SECTION_ORDER)

    hidden_count = sum(1 for m in incoming if (cfg.category(m.get("category")) or cfg.categories[0]).importance == "hidden")
    status = analysis_status(cfg, store)
    return {
        "title_date": local(period_to).strftime("%d.%m.%Y, %H:%M"),
        "period_from": _fmt(period_from),
        "me": cfg.get("me.name") or "",
        "tiles": tiles,
        "sections": order,
        "section_titles": {a: titles.get(a, TILE_TITLES.get(a, a)) for a in SECTION_ORDER},
        "vip": vip,
        "attention": attention,
        "overview": overview,
        "drafts": drafts,
        "agenda": agenda,
        "folders": folder_groups,
        "folders_hours": fld.window_hours(cfg),
        "tile_warnings": tile_warnings,
        "hidden_count": plural(hidden_count, "письмо", "письма", "писем") if hidden_count else None,
        "pending_analysis": status["threads_waiting"],
        "marked_unimportant": sum(1 for i in items if i["label"] == "not_important"),
        "suggestions": fb.suggestions(cfg, store),
        "interactive": False,
        "token": "",
    }


def normalize_tiles(cfg: Config) -> list[dict[str, Any]]:
    """Карточки из digest.tiles. Сколько записей, столько и карточек.

    Понимает и короткие записи: «- prom», «- {category: prom}», «- {type: prom}»,
    а «- categories» разворачивает в карточку на каждую видимую категорию.
    """
    out = []
    for spec in cfg.get("digest.tiles") or DEFAULT_TILES:
        if isinstance(spec, str):
            spec = {"type": spec}
        if not isinstance(spec, dict):
            continue
        spec = dict(spec)
        kind = spec.get("type")
        if kind == "categories":
            for cat in cfg.categories:
                if cat.importance != "hidden":
                    out.append({"type": "category", "category": cat.id, "tone": spec.get("tone")})
            continue
        if not kind and spec.get("category"):
            spec["type"] = "category"
        elif kind not in TILE_TITLES and kind != "category" and cfg.category(kind):
            spec.update(type="category", category=kind)  # «type: prom» — это карточка категории
        out.append(spec)
    return out


def folder_overview(cfg: Config, store: Store, period_to: str) -> list[dict[str, Any]]:
    done = fld.summaries(store)
    groups = []
    for name, msgs in fld.folder_messages(cfg, store, period_to).items():
        newest = msgs[0]["date"]
        summ = done.get(name) or {}
        senders: dict[str, int] = {}
        for m in msgs:
            who = pretty_person(cfg, m.get("from_raw") or "")
            senders[who] = senders.get(who, 0) + 1
        top = sorted(senders.items(), key=lambda kv: -kv[1])[:3]
        groups.append({
            "id": "f-" + hashlib.sha1(name.encode("utf-8")).hexdigest()[:8],
            "name": name,
            "title": fld.leaf(name),
            "count": len(msgs),
            "line": plural(len(msgs), "письмо", "письма", "писем") + " · " + ", ".join(
                f"{w} ({n})" if n > 1 else w for w, n in top),
            "summary": summ.get("summary"),
            "summary_stale": bool(summ) and summ.get("upto", "") < newest,
            "entries": [{"time": _fmt(m["date"]), "who": pretty_person(cfg, m.get("from_raw") or ""),
                         "subject": one_line(m.get("subject") or "(без темы)", 140),
                         "gist": one_line(m.get("new_text") or "", 200)} for m in msgs[:15]],
            "more": max(0, len(msgs) - 15),
        })
    groups.sort(key=lambda g: -g["count"])
    return groups


DEFAULT_TILES = [
    {"type": "new"}, {"type": "attention"}, {"type": "vip"}, {"type": "drafts"},
]
TILE_TITLES = {
    "new": "Новые письма", "attention": "Требуют внимания", "vip": "От руководства", "drafts": "Ответы готовы",
    "escalations": "Эскалации", "incidents": "Инциденты", "calendar": "Встречи", "folders": "По папкам",
}
TILE_TONES = {"new": "blue", "attention": "red", "vip": "violet", "drafts": "green", "escalations": "red",
              "incidents": "amber", "calendar": "blue", "folders": "neutral"}
SECTION_ORDER = ["vip", "attention", "drafts", "calendar", "folders", "new"]


def digest_paths(cfg: Config, period_to: str) -> tuple[Path, Path]:
    cfg.digests_dir.mkdir(parents=True, exist_ok=True)
    name = local(period_to).strftime("digest_%Y-%m-%d_%H%M.html")
    return cfg.digests_dir / name, cfg.digests_dir / "latest.html"


def write_digest(cfg: Config, store: Store, period_from: str, period_to: str, open_browser: bool = False) -> Path:
    model = build_digest(cfg, store, period_from, period_to)
    html = render_html(model)
    path, latest = digest_paths(cfg, period_to)
    path.write_text(html, encoding="utf-8")
    latest.write_text(html, encoding="utf-8")
    if open_browser or cfg.get("digest.open_after_render"):
        webbrowser.open(path.resolve().as_uri())
    return path


def current_period(cfg: Config, store: Store) -> tuple[str, str]:
    """Период последней сводки; если сводок ещё не было — с прошлого запуска до сейчас."""
    last = store.last_run("digest")
    if last and last.get("period_from") and last.get("period_to"):
        return last["period_from"], last["period_to"]
    return new_period(cfg, store)


def rerender(cfg: Config, store: Store, open_browser: bool = False) -> Path:
    """Пересобрать файл последней сводки (после ваших отметок, правок и отправки)."""
    last = store.last_run("digest")
    if last:
        return write_digest(cfg, store, last["period_from"], last["period_to"], open_browser)
    period_from, period_to = new_period(cfg, store)
    path = write_digest(cfg, store, period_from, period_to, open_browser)
    store.add_run("digest", iso(now_utc()), period_from, period_to, str(path))
    store.commit()
    return path


def new_period(cfg: Config, store: Store) -> tuple[str, str]:
    last = store.last_run("digest")
    to = iso(now_utc())
    if last and last.get("period_to"):
        return last["period_to"], to
    return iso(now_utc() - timedelta(hours=int(cfg.get("fetch.initial_hours") or 24))), to


def sender_addresses(raw: str) -> list[str]:
    return [a for _, a in parse_addresses(raw)]
