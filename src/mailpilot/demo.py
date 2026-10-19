"""Выдуманные письма для проверки и демонстрации. Все адреса и люди вымышлены."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any

import yaml

from .textutil import iso, now_utc

ME = "Иванова Ольга <o.ivanova@example.com>"
PROM = "ПРОМ сопровождение <prom-support@example.com>"
KOZLOV = "Козлов Дмитрий <d.kozlov@example.com>"
BELOVA = "Белова Марина <m.belova@example.com>"
PETROV = "Петров Алексей <a.petrov@example.com>"
SIDOROVA = "Сидорова Елена <e.sidorova@example.com>"
ORLOV = "Орлов Павел <p.orlov@example.com>"
NIKITINA = "Никитина Светлана <s.nikitina@example.com>"
SMIRNOVA = "Смирнова Анна <a.smirnova@example.com>"
ZAKHAROV = "Захаров Игорь <i.zakharov@example.com>"
LEBEDEVA = "Лебедева Ирина <i.lebedeva@example.com>"
FEDOROV = "Фёдоров Максим <m.fedorov@example.com>"
JIRA = "Белова Марина (Jira) <jira@example.com>"
JIRA2 = "Захаров Игорь (Jira) <jira@example.com>"
CONF = "Захаров Игорь (Confluence) <confluence@example.com>"
CONF2 = "Никитина Светлана (Confluence) <confluence@example.com>"
MON = "Мониторинг <monitoring-noreply@example.com>"
ROBOT = "Робот отчётов <robot@example.com>"


def _quote(sender: str, date: str, to: str, subject: str, body: str) -> str:
    return f"\n\nОт: {sender}\nОтправлено: {date}\nКому: {to}\nТема: {subject}\n\n{body}"


def _next_workday(now: datetime) -> datetime:
    d = now.date() + timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return datetime.combine(d, time.min).astimezone()


def _ics(uid: str, start: datetime, end: datetime, summary: str, organizer: str, location: str) -> str:
    f = "%Y%m%dT%H%M%S"  # местное время, как в приглашениях Exchange с TZID
    return (
        "BEGIN:VCALENDAR\r\nMETHOD:REQUEST\r\nBEGIN:VEVENT\r\n"
        f"UID:{uid}\r\nDTSTART;TZID=Russian Standard Time:{start.strftime(f)}\r\n"
        f"DTEND;TZID=Russian Standard Time:{end.strftime(f)}\r\nSUMMARY:{summary}\r\n"
        f"LOCATION:{location}\r\nORGANIZER;CN={organizer}:mailto:x@example.com\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
    )


def demo_messages(now: datetime | None = None) -> list[dict[str, Any]]:
    now = (now or now_utc()).astimezone()
    msgs: list[dict[str, Any]] = []
    uid = {"INBOX": 100, "Отправленные": 500, "INBOX/Рассылки": 700, "Проекты/Альфа": 900}
    uidv = {"INBOX": 1, "Отправленные": 2, "INBOX/Рассылки": 3, "Проекты/Альфа": 4}

    def add(box: str, ago: timedelta, frm: str, to: str, subject: str, text: str, cc: str = "",
            mid: str | None = None, reply_to: str | None = None, refs: list[str] | None = None,
            attachments: list[dict[str, Any]] | None = None) -> str:
        uid[box] += 1
        message_id = mid or f"<demo-{box}-{uid[box]}@example.com>"
        msgs.append({
            "uid": uid[box], "uidValidity": uidv[box], "sourceMailbox": box,
            "date": iso(now - ago), "subject": subject, "from": frm, "to": to, "cc": cc, "text": text,
            "messageId": message_id, "inReplyTo": reply_to, "references": refs or ([reply_to] if reply_to else []),
            "attachments": attachments or [],
        })
        return message_id

    h, d = timedelta(hours=1), timedelta(days=1)

    # --- ПРОМ: инцидент, который тянется третий день и обрастает эскалацией ----------------------
    s1 = "INC0012345 Не проходят платежи СБП у части клиентов"
    a1 = add("INBOX", 2 * d + 5 * h, KOZLOV, PROM, s1,
             "Коллеги, добрый день!\nС 9:40 у части клиентов не проходят платежи СБП, на шлюзе ошибки 502 "
             "при обращении к Системе Альфа. Затронуто около 3% операций. Прошу взять в работу.\n\nДмитрий Козлов")
    a2 = add("Отправленные", 2 * d + 3 * h, ME, f"{PROM}, {KOZLOV}", "RE: " + s1,
             "Коллеги, смотрим. По логам Системы Альфа ошибок нет, похоже на проблему на стороне шлюза. "
             "Предлагаю шлюзу проверить пул соединений.\n\nОльга"
             + _quote(KOZLOV, "вчера", "ПРОМ сопровождение", s1, "С 9:40 у части клиентов не проходят платежи СБП..."),
             reply_to=a1, refs=[a1])
    a3 = add("INBOX", 1 * d + 6 * h, BELOVA, PROM, "RE: " + s1,
             "Ольга, ошибка до сих пор не устранена. По трассировке таймаут возникает в Системе Альфа при вызове "
             "сервиса лимитов: ответ приходит через 31 секунду при таймауте шлюза 30 секунд. На стороне шлюза "
             "пул в норме. Добавила Алексея Петрова в копию.\n\nМарина"
             + _quote(ME, "позавчера", "ПРОМ сопровождение", "RE: " + s1, "Коллеги, смотрим..."),
             cc=PETROV, reply_to=a2, refs=[a1, a2])
    add("INBOX", 3 * h, KOZLOV, PROM, "RE: " + s1,
        "Срочно нужен статус и срок исправления: клиенты пишут жалобы в соцсетях, растёт количество обращений "
        "в контакт-центр. Кто владелец проблемы со стороны Системы Альфа?\n\nДмитрий"
        + _quote(BELOVA, "вчера", "ПРОМ сопровождение", "RE: " + s1, "Ольга, ошибка до сих пор не устранена..."),
        cc=f"{PETROV}, {SIDOROVA}", reply_to=a3, refs=[a1, a2, a3])

    # --- ПРОМ: решённый инцидент ------------------------------------------------------------
    s2 = "INC0012399 Замедление выгрузки отчётов"
    b1 = add("INBOX", 1 * d + 2 * h, ORLOV, PROM, s2,
             "Коллеги, выгрузка ежедневных отчётов идёт 40 минут вместо обычных 5. Посмотрите, пожалуйста.")
    b2 = add("Отправленные", 1 * d, ME, PROM, "RE: " + s2,
             "Исправили конфигурацию пула подключений к БД, выгрузка снова занимает 4–5 минут.\n\nОльга"
             + _quote(ORLOV, "вчера", "ПРОМ сопровождение", s2, "выгрузка ежедневных отчётов идёт 40 минут"),
             reply_to=b1, refs=[b1])
    add("INBOX", 5 * h, ORLOV, PROM, "RE: " + s2, "Подтверждаю, всё работает. Закрываем обращение, спасибо!",
        reply_to=b2, refs=[b1, b2])

    # --- ПРОМ: простой вопрос, на который можно ответить черновиком -------------------------------
    add("INBOX", 2 * h, NIKITINA, f"{PROM}, {ME}", "Окно релиза Системы Альфа в ПРОМ",
        "Ольга, добрый день! Подтвердите, пожалуйста, что релиз Системы Альфа ставим в окно в четверг с 22:00 "
        "до 02:00, как договаривались. Мне нужно внести его в план работ.\n\nСветлана")

    # --- Письмо от важного человека (digest.vip_senders) ------------------------------------------
    add("INBOX", 1 * h, "v.gromov@example.com", ME, "Статус по Системе Альфа к комитету",
        "Ольга, добрый день. Подготовьте, пожалуйста, к четвергу короткий статус по Системе Альфа: "
        "инциденты за неделю и план по сервису лимитов.\n\nВиктор Громов")

    # --- Текучка ---------------------------------------------------------------------------------
    add("INBOX", 6 * h, SMIRNOVA, ME, "Встреча по архитектуре в четверг",
        "Ольга, добрый день!\nПодтвердите, пожалуйста, участие во встрече по архитектуре в четверг в 15:00. "
        "Если время неудобно, предложите другое.\n\nАнна")
    add("INBOX", 4 * h, ZAKHAROV, ME, "Схема интеграции с Системой Альфа",
        "Ольга, привет! Пришли, пожалуйста, актуальную схему интеграции с Системой Альфа до пятницы, "
        "она нужна для архитектурного комитета.\n\nИгорь")
    add("INBOX", 2 * h + 30 * timedelta(minutes=1), LEBEDEVA, "Отдел сопровождения <team@example.com>",
        "Обучение по новому регламенту инцидентов",
        "Коллеги, напоминаю: в среду в 11:00 обучение по новому регламенту работы с инцидентами. "
        "Запись будет, присутствие желательно.", cc=ME)
    f1 = add("INBOX", 2 * d, FEDOROV, ME, "Бюджет на нагрузочное тестирование",
             "Ольга, подскажи, укладываемся ли мы в бюджет на нагрузочное тестирование в этом квартале?")
    f2 = add("Отправленные", 1 * d + 20 * h, ME, FEDOROV, "RE: Бюджет на нагрузочное тестирование",
             "Максим, да, укладываемся, остаток около 15%.\n\nОльга"
             + _quote(FEDOROV, "позавчера", ME, "Бюджет на нагрузочное тестирование", "укладываемся ли мы в бюджет"),
             reply_to=f1, refs=[f1])
    add("INBOX", 1 * d + 18 * h, FEDOROV, ME, "RE: Бюджет на нагрузочное тестирование", "Спасибо, принято!",
        reply_to=f2, refs=[f1, f2])

    # --- Jira -----------------------------------------------------------------------------------
    add("INBOX", 7 * h, JIRA, ME, "[JIRA] (ALFA-101) Таймаут при вызове сервиса лимитов",
        "Белова Марина изменила статус задачи ALFA-101 на «В работе».\nhttps://jira.example.com/browse/ALFA-101")
    add("INBOX", 3 * h, JIRA, ME, "[JIRA] (ALFA-101) Таймаут при вызове сервиса лимитов",
        "Белова Марина прокомментировала: приложила трассировку.\nhttps://jira.example.com/browse/ALFA-101")
    add("INBOX", 5 * h, JIRA2, ME, "[JIRA] (ALFA-117) Обновить схему интеграции",
        "Захаров Игорь создал задачу ALFA-117.\nhttps://jira.example.com/browse/ALFA-117")
    add("INBOX", 8 * h, JIRA2, ME, "[JIRA] Updates for ALFA-120: Обновить сертификаты на стенде",
        "Захаров Игорь назначил задачу на вас.\nhttps://jira.example.com/browse/ALFA-120")

    # --- Confluence -----------------------------------------------------------------------------
    add("INBOX", 6 * h, CONF, ME, "[Confluence] Альфа > Архитектура интеграций",
        "Захаров Игорь изменил страницу.\nhttps://wiki.example.com/pages/viewpage.action?pageId=1001")
    add("INBOX", 4 * h, CONF, ME, "[Confluence] Альфа > Архитектура интеграций",
        "Захаров Игорь изменил страницу.\nhttps://wiki.example.com/pages/viewpage.action?pageId=1001")
    add("INBOX", 5 * h, CONF2, ME, "[Confluence] Регламенты > Порядок релизов в ПРОМ",
        "Никитина Светлана прокомментировала страницу.\nhttps://wiki.example.com/pages/viewpage.action?pageId=2002")

    # --- Служебные уведомления --------------------------------------------------------------------
    for i, ago in enumerate((9, 6, 1)):
        add("INBOX", ago * h, MON, ME, f"[ALERT] Альфа: время ответа выше порога ({i + 1})",
            "Среднее время ответа сервиса лимитов 28 с при пороге 10 с.")
    add("INBOX", 10 * h, ROBOT, ME, "Ежедневный отчёт по доступности", "Доступность за сутки: 99,2%.")

    # --- Приглашения: две встречи завтра пересекаются ------------------------------------------------
    tomorrow = _next_workday(now)
    ev1 = _ics("demo-ev-1", tomorrow + 15 * h, tomorrow + 16 * h, "Архитектурный комитет", "Смирнова Анна", "Переговорная 5.12")
    ev2 = _ics("demo-ev-2", tomorrow + 15 * h + timedelta(minutes=30), tomorrow + 16 * h + timedelta(minutes=30),
               "Разбор INC0012345", "Белова Марина", "Онлайн")
    ev3 = _ics("demo-ev-3", tomorrow + 10 * h, tomorrow + 10 * h + timedelta(minutes=30), "Ежедневный созвон команды",
               "Смирнова Анна", "Онлайн")
    for n, (who, subj, body) in enumerate([
        (SMIRNOVA, "Приглашение: Архитектурный комитет", ev1),
        (BELOVA, "Приглашение: Разбор INC0012345", ev2),
        (SMIRNOVA, "Приглашение: Ежедневный созвон команды", ev3),
    ]):
        add("INBOX", (8 - n) * h, who, ME, subj, "Приглашение на встречу.",
            attachments=[{"index": 0, "filename": f"invite{n}.ics", "contentType": "text/calendar", "size": len(body), "_content": body}])

    # --- Письма, которые правила Outlook разложили по папкам ------------------------------------------
    add("Проекты/Альфа", 3 * h, ZAKHAROV, ME, "Альфа: перенос стенда нагрузочного тестирования",
        "Коллеги, стенд НТ переезжает в новый ЦОД в субботу. В пятницу после 18:00 стенд будет недоступен.")
    add("Проекты/Альфа", 9 * h, NIKITINA, ME, "Альфа: протокол встречи по релизу",
        "Итоги: релиз в четверг, ответственные за откат назначены, чек-лист в Confluence.")
    add("Проекты/Альфа", 2 * d + 4 * h, ZAKHAROV, ME, "Альфа: черновик плана на квартал",
        "Старое письмо, в сводку за сутки не попадает.")
    add("INBOX/Рассылки", 4 * h, "news@example.com", ME, "Дайджест ИТ-новостей банка",
        "Новая политика паролей с 1 ноября, переезд сервиса заявок, итоги хакатона.")
    add("INBOX/Рассылки", 12 * h, "hr@example.com", ME, "Опрос вовлечённости до пятницы",
        "Пройдите опрос до пятницы, это займёт 10 минут.")

    # --- Старые ответы в «Отправленных»: на них учится стиль --------------------------------------------
    old = [
        ("Встреча по релизу", "Подтвердите участие во встрече по релизу в среду в 11:00.", "Анна, добрый день! Да, буду.\n\nОльга"),
        ("Документы по интеграции", "Пришлите, пожалуйста, описание API интеграции.", "Игорь, привет! Пришлю до конца дня.\n\nОльга"),
        ("Окно работ в ПРОМ", "Подтвердите окно работ в субботу с 23:00.", "Светлана, добрый день! Подтверждаю окно в субботу с 23:00.\n\nОльга"),
        ("Встреча по архитектуре", "Сможешь подключиться к встрече по архитектуре во вторник?", "Да, подключусь. Спасибо!\n\nОльга"),
    ]
    for i, (subj, incoming, reply) in enumerate(old):
        add("Отправленные", (20 + i) * d, ME, "colleague@example.com", "RE: " + subj,
            reply + _quote("Коллега <colleague@example.com>", "давно", ME, subj, incoming))
    return msgs


DEMO_STYLE = """# Профиль стиля (демо)
- Обращение по имени: «Анна, добрый день!», с близкими коллегами «Игорь, привет!».
- Коротко, 1–3 предложения, по делу, без канцелярита.
- Подтверждения: «Да, буду», «Подтверждаю …».
- Подпись: «Ольга» на отдельной строке.
"""

DEMO_FOLDER_RESULTS = {
    "Проекты/Альфа": "Стенд НТ Системы Альфа переезжает в субботу и будет недоступен с пятницы 18:00. "
                     "По релизу в четверг протокол готов, ответственные за откат назначены.",
    "INBOX/Рассылки": "С 1 ноября новая политика паролей; HR просит пройти опрос вовлечённости до пятницы.",
}

DEMO_RESULTS = {
    "INC0012345": {
        "summary": "Третий день у части клиентов не проходят платежи СБП. Сначала мы указали на шлюз, но трассировка "
                   "Беловой показала таймаут в Системе Альфа при вызове сервиса лимитов (31 с при таймауте 30 с). "
                   "Козлов требует срок исправления, в копии Петров и Сидорова.",
        "status": "ждёт меня", "needs_me": True, "priority": 1,
        "action_items": ["Назначить владельца проблемы со стороны Системы Альфа",
                         "Дать Козлову статус и срок исправления сегодня",
                         "Проверить время ответа сервиса лимитов (ALFA-101)"],
        "my_side_problem": "Медленный ответ сервиса лимитов в Системе Альфа (31 с) превышает таймаут шлюза. "
                           "Наша первая версия про шлюз не подтвердилась.",
        "conflict": "Козлов эскалирует: «срочно», жалобы клиентов, в копию добавлены Петров и Сидорова.",
        "join_recommended": True, "join_reason": "Ждут именно вашего ответа о владельце и сроках, руководители уже в копии.",
        "draft": None,
    },
    "INC0012399": {
        "summary": "Выгрузка отчётов замедлилась до 40 минут. Мы исправили пул подключений к БД, Орлов подтвердил, обращение закрыто.",
        "status": "решено", "needs_me": False, "priority": 3, "action_items": [],
        "my_side_problem": None, "conflict": None, "join_recommended": False, "draft": None,
    },
    "Окно релиза": {
        "summary": "Никитина просит подтвердить окно релиза Системы Альфа в четверг с 22:00 до 02:00 для плана работ.",
        "status": "ждёт меня", "needs_me": True, "priority": 2, "action_items": ["Подтвердить окно релиза"],
        "draft": "Светлана, добрый день! Подтверждаю окно релиза Системы Альфа в четверг с 22:00 до 02:00.\n\nОльга",
        "reply_all": False,
    },
    "Статус по Системе Альфа": {
        "summary": "Громов просит к четвергу короткий статус по Системе Альфа: инциденты за неделю и план по сервису лимитов.",
        "status": "ждёт меня", "needs_me": True, "priority": 1,
        "action_items": ["Подготовить статус к четвергу"],
        "draft": "Виктор, добрый день! Да, подготовлю статус к четвергу.\n\nОльга", "reply_all": False,
    },
    "Встреча по архитектуре": {
        "summary": "Смирнова просит подтвердить участие во встрече по архитектуре в четверг в 15:00.",
        "status": "ждёт меня", "needs_me": True, "priority": 2, "action_items": ["Подтвердить участие"],
        "draft": "Анна, добрый день! Да, буду.\n\nОльга", "reply_all": False,
    },
    "Схема интеграции": {
        "summary": "Захаров просит актуальную схему интеграции с Системой Альфа до пятницы для архитектурного комитета.",
        "status": "ждёт меня", "needs_me": True, "priority": 2,
        "action_items": ["Отправить схему интеграции до пятницы"],
        "draft": "Игорь, привет! Пришлю до пятницы.\n\nОльга", "reply_all": False,
    },
    "Обучение": {
        "summary": "В среду в 11:00 обучение по новому регламенту инцидентов, будет запись.",
        "status": "к сведению", "needs_me": False, "priority": 3, "action_items": [], "draft": None,
    },
    "Бюджет": {
        "summary": "Фёдоров спрашивал про бюджет на нагрузочное тестирование, мы ответили, что укладываемся (остаток 15%). Вопрос закрыт.",
        "status": "решено", "needs_me": False, "priority": 3, "action_items": [], "draft": None,
    },
}


def write_demo_results(work_dir: Path) -> None:
    """Имитирует работу агента: пишет result_NN.json для каждого batch_NN.json."""
    for bpath in sorted(work_dir.glob("batch_*.json")):
        batch = json.loads(bpath.read_text(encoding="utf-8"))
        results = []
        for t in batch["threads"]:
            for key, res in DEMO_RESULTS.items():
                if key in t["subject"]:
                    results.append({"thread_id": t["thread_id"], **res})
                    break
        Path(batch["result_file"]).write_text(json.dumps({"threads": results}, ensure_ascii=False, indent=2), encoding="utf-8")
    fpath = work_dir / "folders.json"
    if fpath.exists():
        req = json.loads(fpath.read_text(encoding="utf-8"))
        out = [{"folder": f["folder"], "summary": DEMO_FOLDER_RESULTS[f["folder"]]}
               for f in req["folders"] if f["folder"] in DEMO_FOLDER_RESULTS]
        Path(req["result_file"]).write_text(json.dumps({"folders": out}, ensure_ascii=False, indent=2), encoding="utf-8")


def run_demo(directory: Path, with_analysis: bool = True, open_browser: bool = False) -> Path:
    from .analysis import ingest, prepare
    from .calendar_src import refresh_calendar
    from .config import example_config_path, load_config
    from .digest import write_digest
    from .fetch import fetch_new
    from .learn import learn_from_sent, write_style_request
    from .mail_source import make_source
    from .store import Store
    from .threads import assign_threads

    directory.mkdir(parents=True, exist_ok=True)
    raw = yaml.safe_load(example_config_path().read_text(encoding="utf-8"))
    raw.update({"source": "demo", "data_dir": "data"})
    raw["mailboxes"]["sent"] = "Отправленные"
    raw["calendar"]["source"] = "ics_mail"
    cfg_path = directory / "config.yaml"
    cfg_path.write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8")
    cfg = load_config(cfg_path)
    if cfg.db_path.exists():
        cfg.db_path.unlink()
    store = Store(cfg.db_path)
    now = now_utc()

    async def go() -> None:
        async with make_source(cfg) as src:
            await fetch_new(cfg, store, src, since=now - timedelta(days=4), until=now)
            await learn_from_sent(cfg, store, src, days=60)

    asyncio.run(go())
    assign_threads(cfg, store)
    refresh_calendar(cfg, store)
    cfg.style_path.write_text(DEMO_STYLE, encoding="utf-8")
    write_style_request(cfg, store)
    prepare(cfg, store)
    if with_analysis:
        write_demo_results(cfg.work_dir)
        ingest(cfg, store)
    period_from, period_to = iso(now - timedelta(hours=24)), iso(now)
    path = write_digest(cfg, store, period_from, period_to, open_browser)
    store.add_run("digest", iso(now), period_from, period_to, str(path))
    store.close()
    return path
