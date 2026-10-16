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
