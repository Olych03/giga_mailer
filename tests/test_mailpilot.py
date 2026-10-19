import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from mailpilot.calendar_src import find_conflicts, parse_ics
from mailpilot.cli import main
from mailpilot.config import load_config
from mailpilot.demo import run_demo
from mailpilot.extract import confluence_items, jira_items
from mailpilot.learn import example_from_sent
from mailpilot.mail_source import search_all
from mailpilot.rules import classify
from mailpilot.store import Store
from mailpilot.textutil import normalize_subject, split_quote


def test_normalize_subject_strips_prefixes():
    assert normalize_subject("RE: Fw: Ответ: INC0012345 Не проходят платежи") == "inc0012345 не проходят платежи"
    assert normalize_subject("[EXTERNAL] RE[2]: Тема") == "тема"


def test_split_quote_outlook_style():
    text = "Да, буду.\n\nОльга\n\nОт: Анна <a@x.ru>\nОтправлено: вчера\nКому: Ольга\nТема: Встреча\n\nПодтвердите участие"
    new, quoted = split_quote(text)
    assert new == "Да, буду.\n\nОльга"
    assert quoted.startswith("От: Анна")


def test_split_quote_keeps_plain_from_line():
    text = "Письмо пришло от: службы поддержки\nбез цитаты"
    new, quoted = split_quote(text)
    assert quoted == ""


def test_example_from_sent():
    m = {"subject": "RE: Встреча", "date": "2026-10-01T10:00:00Z",
         "text": "Да, буду.\n\nОт: Анна\nОтправлено: вчера\nТема: Встреча\n\nПодтвердите участие во встрече"}
    ex = example_from_sent(m)
    assert ex["reply"] == "Да, буду."
    assert "Подтвердите участие" in ex["incoming"]


@pytest.fixture()
def demo(tmp_path):
    path = run_demo(tmp_path / "demo")
    cfg = load_config(tmp_path / "demo" / "config.yaml")
    return cfg, path


def test_rules_classify(demo):
    cfg, _ = demo
    base = {"to_raw": "", "cc_raw": "", "from_raw": "", "subject": "", "text": ""}
    assert classify(cfg, {**base, "to_raw": "ПРОМ <prom-support@example.com>"}).id == "prom"
    assert classify(cfg, {**base, "from_raw": "X (Jira) <jira@example.com>"}).id == "jira"
    assert classify(cfg, {**base, "subject": "Приглашение: Встреча"}).id == "invites"
    assert classify(cfg, {**base, "from_raw": "Коллега <c@example.com>"}).id == "routine"


def test_demo_threads_and_flags(demo):
    cfg, _ = demo
    store = Store(cfg.db_path)
    threads = {t["subject"]: t for t in store.query("SELECT * FROM threads")}
    inc = next(t for s, t in threads.items() if s.startswith("INC0012345"))
    assert inc["msg_count"] == 4  # письма за три дня в одной цепочке, включая ваш ответ
    assert inc["category"] == "prom"
    assert inc["flags"]["escalation"] is True
    assert set(inc["flags"]["people_added"]) == {"Петров", "Сидорова"}
    assert inc["flags"]["my_turn"] is True
    assert "срочно" in inc["flags"]["escalation_keywords"]
    assert inc["state"]["my_side_problem"]
    store.close()


def test_demo_digest_content(demo):
    cfg, path = demo
    html = Path(path).read_text(encoding="utf-8")
    for needle in ["От руководства", "Виктор Громов", "INC0012345", "Требуют внимания", "ALFA-101",
                   "Архитектура интеграций", "mailpilot send", "пересечения", "Стоит подключиться",
                   'href="#attention"', 'id="draft-1"']:
        assert needle in html, needle
    # Порядок разделов как у карточек: руководство, внимание, ответы, затем обзор новых писем
    assert html.index('id="vip"') < html.index('id="attention"') < html.index('id="drafts"') < html.index('id="new"')
    # Решённый инцидент не в «Требуют внимания», а только в обзоре
    att = html[html.index('id="attention"'):html.index('id="drafts"')]
    assert "INC0012399" not in att


def test_custom_tiles(demo):
    cfg, _ = demo
    from mailpilot.digest import build_digest
    from mailpilot.textutil import iso, now_utc

    cfg.raw.setdefault("digest", {})["tiles"] = [{"type": "category", "category": "prom", "title": "ПРОМ"}, {"type": "drafts"}]
    store = Store(cfg.db_path)
    model = build_digest(cfg, store, iso(now_utc() - timedelta(days=1)), iso(now_utc()))
    store.close()
    assert [t["title"] for t in model["tiles"]] == ["ПРОМ", "Ответы готовы"]
    assert model["tiles"][0]["anchor"] == "cat-prom" and model["tiles"][0]["value"] == 3
    assert model["sections"][0] == "vip"


def test_drafts_and_send_requires_confirmation(demo, monkeypatch, capsys):
    cfg, _ = demo
    store = Store(cfg.db_path)
    drafts = store.drafts(("proposed",))
    assert len(drafts) == 4
    store.close()
    did = drafts[0]["id"]
    # Не терминал и без --yes — отказ
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    with pytest.raises(SystemExit):
        main(["--config", str(cfg.path), "send", str(did)])
    assert not (cfg.data_dir / "outbox.jsonl").exists()
    # Правка и отправка с подтверждением
    main(["--config", str(cfg.path), "draft", "edit", str(did), "--text", "Подтверждаю.\n\nОльга"])
    main(["--config", str(cfg.path), "send", str(did), "--yes"])
    sent = [json.loads(l) for l in (cfg.data_dir / "outbox.jsonl").read_text(encoding="utf-8").splitlines()]
    assert sent[0]["text"].startswith("Подтверждаю.")
    store = Store(cfg.db_path)
    assert store.draft(did)["status"] == "sent"
    assert store.one("SELECT COUNT(*) AS n FROM examples WHERE source='edit'")["n"] == 1
    store.close()


def test_importance_and_mute_overrides(demo):
    cfg, _ = demo
    main(["--config", str(cfg.path), "importance", "jira", "hidden"])
    main(["--config", str(cfg.path), "mute", "--from", "monitoring-noreply@example.com"])
    cfg2 = load_config(cfg.path)
    assert cfg2.category("jira").importance == "hidden"
    assert cfg2.categories[0].id == "muted"
    msg = {"to_raw": "", "cc_raw": "", "from_raw": "Мониторинг <monitoring-noreply@example.com>", "subject": "x", "text": ""}
    assert classify(cfg2, msg).id == "muted"


def test_ingest_is_incremental(demo):
    cfg, _ = demo
    store = Store(cfg.db_path)
    # После анализа пакетов не остаётся, повторная подготовка ничего не создаёт
    from mailpilot.analysis import prepare

    assert prepare(cfg, store) == []
    store.close()


def test_parse_ics_and_conflicts():
    ics = ("BEGIN:VCALENDAR\r\nMETHOD:REQUEST\r\nBEGIN:VEVENT\r\nUID:1\r\nDTSTART:20261016T120000Z\r\n"
           "DTEND:20261016T130000Z\r\nSUMMARY:Встреча\r\n  с переносом\r\nEND:VEVENT\r\nBEGIN:VEVENT\r\nUID:2\r\n"
           "DTSTART:20261016T123000Z\r\nDTEND:20261016T133000Z\r\nSUMMARY:Вторая\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n")
    evs = parse_ics(ics)
    assert evs[0]["subject"] == "Встреча с переносом"
    assert find_conflicts(evs) == {"1", "2"}


def test_extractors():
    msgs = [
        {"subject": "[JIRA] (ABC-1) Починить вход", "text": "Иван изменил статус", "date": "2026-10-15T10:00:00Z", "from_raw": "Иван (Jira) <jira@x.ru>"},
        {"subject": "[JIRA] (ABC-1) Починить вход", "text": "Иван прокомментировал", "date": "2026-10-15T11:00:00Z", "from_raw": "Иван (Jira) <jira@x.ru>"},
    ]
    items = jira_items(msgs)
    assert items[0]["key"] == "ABC-1" and items[0]["count"] == 2 and items[0]["title"] == "Починить вход"
    conf = confluence_items([{"subject": "[Confluence] Команда > Регламент", "text": "", "date": "2026-10-15T10:00:00Z",
                              "from_raw": "Пётр (Confluence) <c@x.ru>"}])
    assert conf[0]["space"] == "Команда" and conf[0]["pages"][0]["page"] == "Регламент"


def test_search_all_splits_when_more_than_limit():
    class Src:
        async def search(self, mailboxes, since, before, limit=200):
            n = int((before - since).total_seconds() // 60)  # одно письмо в минуту
            msgs = [{"uid": i} for i in range(min(n, limit))]
            return {"hasMore": n > limit, "messages": msgs}

    start = datetime(2026, 10, 15, tzinfo=timezone.utc)
    res = asyncio.run(search_all(Src(), None, start, start + timedelta(hours=8)))
    assert len(res) == 480


def test_person_matches_latin_address():
    from mailpilot.textutil import person_variants

    rec = "aabelousova@bank.example, orlov.ivan@bank.example"
    assert any(v in rec for v in person_variants("Белоусова"))
    assert any(v in rec for v in person_variants("Орлов"))
    assert not any(v in rec for v in person_variants("Тихонов"))


def test_vip_add_remove(demo):
    cfg, _ = demo
    main(["--config", str(cfg.path), "vip", "add", "Павел Лебедев"])
    main(["--config", str(cfg.path), "vip", "remove", "Виктор Громов"])
    assert load_config(cfg.path).get("digest.vip_senders") == ["Павел Лебедев"]


# --- дообучение по отметкам ------------------------------------------------------

def _model(cfg):
    from mailpilot.digest import build_digest
    from mailpilot.textutil import iso, now_utc

    store = Store(cfg.db_path)
    m = build_digest(cfg, store, iso(now_utc() - timedelta(days=1)), iso(now_utc()))
    store.close()
    return m


def _thread_id(cfg, prefix):
    store = Store(cfg.db_path)
    tid = next(t["id"] for t in store.query("SELECT * FROM threads") if (t["subject"] or "").startswith(prefix))
    store.close()
    return tid


def test_marks_override_attention(demo):
    from mailpilot import feedback as fb

    cfg, _ = demo
    release = _thread_id(cfg, "Окно релиза")
    assert release in [i["id"] for i in _model(cfg)["attention"]]
    store = Store(cfg.db_path)
    fb.mark(store, release, "not_important", "это рассылка")
    store.close()
    m = _model(cfg)
    assert release not in [i["id"] for i in m["attention"]]
    assert m["marked_unimportant"] == 1
    # «Важно» поднимает переписку даже из неважной категории
    cfg.raw["categories"] = [dict(c, importance="low") if c["id"] == "routine" else c for c in cfg.raw["categories"]]
    schema = _thread_id(cfg, "Схема интеграции")
    assert schema not in [i["id"] for i in _model(cfg)["attention"]]
    main(["--config", str(cfg.path), "mark", schema, "important"])
    att = _model(cfg)["attention"]
    assert att[0]["id"] == schema and "вы отметили как важное" in att[0]["why"]
    # Повторная отметка заменяет прежнюю, clear снимает
    main(["--config", str(cfg.path), "mark", release, "clear"])
    store = Store(cfg.db_path)
    assert release not in fb.labels(store)
    store.close()


def test_suggestions_from_repeated_marks(demo):
    from mailpilot import feedback as fb

    cfg, _ = demo
    store = Store(cfg.db_path)
    for prefix in ("Окно релиза", "Схема интеграции"):
        fb.mark(store, _thread_id(cfg, prefix), "not_important")
    store.db.execute("UPDATE feedback SET sender='digest@news.example.com'")
    store.commit()
    sugs = fb.suggestions(cfg, store)
    assert [s["id"] for s in sugs] == ["low-from:digest@news.example.com"]
    fb.apply_suggestion(cfg, store, "1")
    assert fb.suggestions(cfg, store) == []
    store.close()
    cfg2 = load_config(cfg.path)
    assert cfg2.categories[0].id == "deprioritized" and cfg2.categories[0].importance == "low"
    msg = {"to_raw": "", "cc_raw": "", "from_raw": "digest@news.example.com", "subject": "x", "text": ""}
    assert classify(cfg2, msg).id == "deprioritized"


def test_no_reply_blocks_drafts_and_preferences_reach_agent(demo):
    from mailpilot import analysis
    from mailpilot import feedback as fb

    cfg, _ = demo
    schema = _thread_id(cfg, "Схема интеграции")
    store = Store(cfg.db_path)
    did = next(d["id"] for d in store.drafts(("proposed",)) if d["thread_id"] == schema)
    store.close()
    main(["--config", str(cfg.path), "draft", "no-reply", str(did), "--reason", "отвечу устно"])
    fb.ensure_preferences(cfg)
    cfg.preferences_path.write_text("# Мои правила\n\n- Письма про релизы Альфы не важны\n", encoding="utf-8")
    store = Store(cfg.db_path)
    assert store.draft(did)["status"] == "no_reply"
    store.db.execute("UPDATE threads SET analyzed_upto=NULL")  # заставить разобрать заново
    store.commit()
    files = analysis.prepare(cfg, store)
    store.close()
    batch = json.loads(Path(files[0]).read_text(encoding="utf-8"))
    entry = next(t for f in files for t in json.loads(Path(f).read_text(encoding="utf-8"))["threads"] if t["thread_id"] == schema)
    assert entry["allow_draft"] is False
    assert "релизы Альфы" in batch["my_preferences"]
    assert batch["my_recent_decisions"][0]["decision"] == "ответ не нужен"
    assert batch["my_recent_decisions"][0]["reason"] == "отвечу устно"


def test_local_server_marks_and_sends_only_with_token_and_confirm(demo):
    import threading
    import urllib.error
    import urllib.request

    from mailpilot.server import make_server

    cfg, _ = demo
    httpd, token = make_server(str(cfg.path), port=0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"

    def post(path, body, tok=token):
        req = urllib.request.Request(base + path, data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json", "X-Mailpilot-Token": tok})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    try:
        page = urllib.request.urlopen(base + "/").read().decode()
        assert token in page and 'data-label="not_important"' in page and "Отправить…" in page
        release = _thread_id(cfg, "Окно релиза")
        assert post("/api/mark", {"thread_id": release, "label": "not_important"}, tok="wrong")[0] == 403
        assert post("/api/mark", {"thread_id": release, "label": "not_important"}) == (200, {"ok": True, "thread_id": release, "label": "not_important"})
        store = Store(cfg.db_path)
        did = store.drafts(("proposed",))[0]["id"]
        store.close()
        code, data = post("/api/draft", {"id": did, "action": "send", "text": "Да."})
        assert code == 400 and not (cfg.data_dir / "outbox.jsonl").exists()
        code, data = post("/api/draft", {"id": did, "action": "send", "text": "Да, подтверждаю.", "confirm": True})
        assert code == 200 and data["status"] == "sent"
        sent = json.loads((cfg.data_dir / "outbox.jsonl").read_text(encoding="utf-8").splitlines()[0])
        assert sent["text"].startswith("Да, подтверждаю.")
        assert post("/api/draft", {"id": did, "action": "send", "text": "ещё раз", "confirm": True})[0] == 400
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_tiles_any_count_shorthand_and_warnings(demo):
    cfg, _ = demo
    cfg.raw["digest"]["tiles"] = ["new", "attention", "vip", "drafts", "folders", "prom",
                                  {"category": "jira", "title": "Задачи"}, {"type": "calendar"}, "nonsense"]
    m = _model(cfg)
    titles = [t["title"] for t in m["tiles"]]
    assert len(titles) == 8
    assert titles[4:7] == ["По папкам", "Обращения с ПРОМ", "Задачи"]
    assert m["tile_warnings"] == ["не показана карточка: неизвестный тип «nonsense»"]

    cfg.raw["digest"]["tiles"] = ["categories"]
    visible = [c.id for c in cfg.categories if c.importance != "hidden"]
    assert [t["anchor"] for t in _model(cfg)["tiles"]] == [f"cat-{c}" for c in visible]

    # Частая ошибка: tiles без отступа, на верхнем уровне
    cfg.raw["tiles"] = cfg.raw["digest"].pop("tiles")
    m = _model(cfg)
    assert len(m["tiles"]) == 4 and "вне раздела digest" in m["tile_warnings"][0]


def test_choose_folders_skips_service_ones(demo):
    from mailpilot import folders as fld

    cfg, _ = demo
    tree = {
        "INBOX": {"name": "INBOX", "attribs": [], "delimiter": "/", "children": {
            "Рассылки": {"name": "INBOX/Рассылки", "attribs": []}}},
        "Отправленные": {"name": "Отправленные", "attribs": ["\\Sent"]},
        "Sent Items": {"name": "Sent Items", "attribs": []},
        "Календарь": {"name": "Календарь", "attribs": []},
        "Нежелательная почта": {"name": "Нежелательная почта", "attribs": []},
        "Проекты": {"name": "Проекты", "selectable": False, "children": {"Альфа": {"name": "Проекты/Альфа"}}},
    }
    assert fld.choose(cfg, tree) == ["INBOX/Рассылки", "Проекты/Альфа"]
    cfg.raw["folders"]["exclude"] = ["Рассылки"]
    assert fld.choose(cfg, tree) == ["Проекты/Альфа"]
    cfg.raw["folders"]["include"] = ["Календарь"]
    assert fld.choose(cfg, tree) == ["Календарь"]
    assert fld.choose(cfg, {"mailboxes": ["INBOX", "Архив"]}) == []  # include сильнее автоматического выбора


def test_folders_section_separate_from_main_digest(demo):
    cfg, path = demo
    html = Path(path).read_text(encoding="utf-8")
    sec = html[html.index('id="folders"'):html.index('id="new"')]
    assert "Проекты/Альфа" in sec and "Стенд НТ Системы Альфа переезжает" in sec
    assert "черновик плана на квартал" not in sec  # старше суток
    # Письма из папок не попадают в цепочки и в обзор новых писем
    assert "Опрос вовлечённости" not in html[html.index('id="new"'):]
    store = Store(cfg.db_path)
    rows = store.query("SELECT thread_id FROM messages WHERE mailbox IN ('Проекты/Альфа', 'INBOX/Рассылки')")
    store.close()
    assert rows and all(r["thread_id"] == "" for r in rows)
    groups = {g["name"]: g for g in _model(cfg)["folders"]}
    assert groups["Проекты/Альфа"]["count"] == 2 and not groups["Проекты/Альфа"]["summary_stale"]


def test_folder_summary_round_trip(demo):
    from mailpilot import folders as fld
    from mailpilot.analysis import ingest, prepare
    from mailpilot.textutil import iso, now_utc

    cfg, _ = demo
    store = Store(cfg.db_path)
    work = cfg.work_dir
    # Суть уже есть по всем папкам: новый запрос не нужен
    prepare(cfg, store)
    assert not (work / "folders.json").exists()
    # Пришло новое письмо в папку: суть устарела, агенту уходит запрос только по этой папке
    store.insert_message({"mailbox": "Проекты/Альфа", "uid_validity": 4, "uid": 999, "subject": "Альфа: стенд вернули",
                          "from_raw": "a@example.com", "date": iso(now_utc() - timedelta(minutes=5)),
                          "new_text": "Стенд снова доступен.", "is_mine": 0, "thread_id": ""})
    store.commit()
    assert _model(cfg)["folders"][0]["summary_stale"] is True
    prepare(cfg, store)
    req = json.loads((work / "folders.json").read_text(encoding="utf-8"))
    assert [f["folder"] for f in req["folders"]] == ["Проекты/Альфа"]
    assert req["folders"][0]["messages"][0]["subject"] == "Альфа: стенд вернули"
    Path(req["result_file"]).write_text(json.dumps({"folders": [
        {"folder": "Проекты/Альфа", "summary": "Стенд вернули."}, {"folder": "Чужая", "summary": "x"}]}), encoding="utf-8")
    stats = ingest(cfg, store)
    assert stats["folders"] == 1 and any("Чужая" in e for e in stats["errors"])
    assert fld.summaries(store)["Проекты/Альфа"]["summary"] == "Стенд вернули."
    assert not (work / "folders.json").exists()
    store.close()
