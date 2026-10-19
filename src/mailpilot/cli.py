"""Командная строка mailpilot."""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
from pathlib import Path
from typing import Any

from . import analysis
from . import folders as fld
from . import feedback as fb
from .calendar_src import calendar_source, refresh_calendar
from .config import (
    IMPORTANCE_LEVELS, Config, ConfigError, example_config_path, load_config, read_overrides, write_overrides,
)
from .digest import new_period, rerender, write_digest
from .fetch import fetch_new
from .learn import learn_from_sent, write_style_request
from .mail_source import MailError, make_source
from .sending import confirm, describe_draft, edit_in_editor, send_draft
from .store import Store
from .textutil import iso, now_utc
from .threads import assign_threads


def _store(cfg: Config) -> Store:
    return Store(cfg.db_path)


def _print_json(data: Any) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2))


# --- команды -------------------------------------------------------------------------

def cmd_init(args: argparse.Namespace) -> None:
    target = Path(args.config or "config.yaml")
    if target.exists() and not args.force:
        raise SystemExit(f"{target} уже существует. Добавьте --force, чтобы перезаписать.")
    shutil.copy(example_config_path(), target)
    env = target.parent / ".env"
    if not env.exists():
        env.write_text("# Пароль от почты для mcp-mail-server. Файл не попадает в git.\nEMAIL_PASS=\n", encoding="utf-8")
    print(f"Создан {target} и {env}.")
    print("Дальше: впишите адреса и параметры почты в config.yaml, пароль в .env, затем `mailpilot doctor`.")


def cmd_doctor(args: argparse.Namespace) -> None:
    cfg = load_config(args.config)
    print(f"Настройки: {cfg.path}")
    print(f"Данные: {cfg.data_dir}")
    print(f"Источник почты: {cfg.get('source', 'mcp')}")
    if cfg.get("source", "mcp") == "mcp":
        cmd = cfg.get("mcp.command") or "npx"
        print(f"Команда сервера: {cmd} → {shutil.which(cmd) or 'НЕ НАЙДЕНА в PATH'}")

    async def check() -> None:
        async with make_source(cfg) as src:
            print("Проверка соединения:")
            _print_json(await src.check())
            print("Папки почты:")
            _print_json(await src.list_mailboxes())

    try:
        asyncio.run(check())
    except (MailError, OSError) as exc:
        print(f"Ошибка соединения: {exc}")
    store = _store(cfg)
    print(f"Календарь: {calendar_source(cfg)}")
    print(f"Профиль стиля: {'есть' if cfg.style_path.exists() else 'нет (запустите mailpilot learn)'}")
    n = store.one("SELECT COUNT(*) AS n FROM examples")
    print(f"Образцов ваших ответов: {n['n'] if n else 0}")
    store.close()


async def _fetch(cfg: Config, store: Store) -> dict[str, int]:
    async with make_source(cfg) as src:
        stats = await fetch_new(cfg, store, src)
    assign_threads(cfg, store)
    return stats


def cmd_fetch(args: argparse.Namespace) -> None:
    cfg = load_config(args.config)
    store = _store(cfg)
    stats = asyncio.run(_fetch(cfg, store))
    print(f"Найдено писем: {stats['found']}, новых: {stats['new']}, встреч из приглашений: {stats['events']}.")
    store.close()


def cmd_run(args: argparse.Namespace) -> None:
    """Основная команда для cron: забрать почту, разложить, подготовить пакеты и собрать сводку."""
    cfg = load_config(args.config)
    store = _store(cfg)
    stats = asyncio.run(_fetch(cfg, store))
    refresh_calendar(cfg, store)
    batches = analysis.prepare(cfg, store)
    period_from, period_to = new_period(cfg, store)
    path = write_digest(cfg, store, period_from, period_to, open_browser=args.open)
    store.add_run("digest", iso(now_utc()), period_from, period_to, str(path), stats)
    store.commit()
    print(f"Новых писем: {stats['new']}. Сводка: {path}")
    if stats.get("folders"):
        print(f"Папок просмотрено: {stats['folders']}, новых писем в них: {stats['folder_new']}.")
    if batches:
        print(f"Пакетов на смысловой анализ: {len(batches)}. Откройте gigacode и запустите скилл mailpilot.")
    store.close()


def cmd_prepare(args: argparse.Namespace) -> None:
    cfg = load_config(args.config)
    store = _store(cfg)
    assign_threads(cfg, store)
    files = analysis.prepare(cfg, store)
    _print_json({"batches": files, "work_dir": str(cfg.work_dir)})
    store.close()


def _rerender(cfg: Config, store: Store, open_browser: bool = False) -> Path:
    return rerender(cfg, store, open_browser)


def cmd_ingest(args: argparse.Namespace) -> None:
    cfg = load_config(args.config)
    store = _store(cfg)
    stats = analysis.ingest(cfg, store)
    path = _rerender(cfg, store, args.open)
    stats["digest"] = str(path)
    _print_json(stats)
    store.close()


def cmd_render(args: argparse.Namespace) -> None:
    cfg = load_config(args.config)
    store = _store(cfg)
    print(_rerender(cfg, store, args.open))
    store.close()


def cmd_status(args: argparse.Namespace) -> None:
    cfg = load_config(args.config)
    store = _store(cfg)
    st = analysis.analysis_status(cfg, store)
    st["batches"] = [str(p) for p in sorted(cfg.work_dir.glob("batch_*.json"))] if cfg.work_dir.exists() else []
    st["drafts_proposed"] = len(store.drafts(("proposed",)))
    last = store.last_run("digest")
    st["last_digest"] = last["output"] if last else None
    st["style_profile"] = str(cfg.style_path) if cfg.style_path.exists() else None
    st["style_request"] = str(cfg.work_dir / "style_request.json") if (cfg.work_dir / "style_request.json").exists() else None
    st["preferences_file"] = str(cfg.preferences_path)
    st["decisions"] = store.one("SELECT COUNT(*) AS n FROM feedback")["n"]
    st["suggestions"] = [{"n": x["n"], "text": x["text"], "count": x["count"]} for x in fb.suggestions(cfg, store)]
    _print_json(st)
    store.close()


def cmd_drafts(args: argparse.Namespace) -> None:
    cfg = load_config(args.config)
    store = _store(cfg)
    drafts = store.drafts(("proposed",))
    if not drafts:
        print("Черновиков на согласование нет.")
    for d in drafts:
        print(describe_draft(cfg, store, d))
        print()
    store.close()


def cmd_draft(args: argparse.Namespace) -> None:
    cfg = load_config(args.config)
    store = _store(cfg)
    d = store.draft(args.id)
    if not d:
        raise SystemExit(f"Нет черновика №{args.id}.")
    if args.action == "show":
        print(describe_draft(cfg, store, d))
    elif args.action == "edit":
        if args.text:
            text = args.text
        elif args.file:
            text = Path(args.file).read_text(encoding="utf-8").strip()
        else:
            text = edit_in_editor(d.get("text_final") or d["text_proposed"])
        if not text:
            raise SystemExit("Пустой текст, черновик не изменён.")
        store.update_draft(d["id"], text_final=text)
        store.commit()
        print(describe_draft(cfg, store, store.draft(d["id"])))
    elif args.action == "reject":
        store.update_draft(d["id"], status="rejected")
        store.commit()
        print(f"Черновик №{d['id']} отклонён.")
    elif args.action == "no-reply":
        fb.mark(store, d["thread_id"], "no_reply", args.reason)
        print(f"Отмечено: на «{(store.thread(d['thread_id']) or {}).get('subject')}» ответ не нужен. "
              "Похожие решения учитываются при следующих разборах.")
    store.close()


def cmd_send(args: argparse.Namespace) -> None:
    cfg = load_config(args.config)
    store = _store(cfg)
    d = store.draft(args.id)
    if not d or d["status"] != "proposed":
        raise SystemExit(f"Черновик №{args.id} не найден или уже обработан.")
    print(describe_draft(cfg, store, d))
    if not args.yes:
        if not sys.stdin.isatty():
            raise SystemExit("Отправка требует подтверждения: запустите команду в терминале или добавьте --yes после явного согласия.")
        if not confirm("Отправить это письмо? Введите «да»: "):
            print("Не отправлено.")
            store.close()
            return

    async def go() -> Any:
        async with make_source(cfg) as src:
            return await send_draft(cfg, store, src, d)

    result = asyncio.run(go())
    print("Отправлено.")
    if isinstance(result, dict) and result.get("sentFolderError"):
        print(f"Внимание: письмо ушло, но не сохранилось в «Отправленных»: {result['sentFolderError']}")
    _rerender(cfg, store)
    store.close()


def cmd_learn(args: argparse.Namespace) -> None:
    cfg = load_config(args.config)
    store = _store(cfg)
    if not args.style_only:
        async def go() -> dict[str, int]:
            async with make_source(cfg) as src:
                return await learn_from_sent(cfg, store, src, args.days)

        stats = asyncio.run(go())
        print(f"Писем в «Отправленных»: {stats['sent_found']}, сохранено образцов ответов: {stats['examples']}.")
    path = write_style_request(cfg, store)
    print(f"Пакет для профиля стиля: {path}")
    print("Откройте gigacode и попросите скилл mailpilot обновить профиль стиля.")
    store.close()


def cmd_folders(args: argparse.Namespace) -> None:
    """Все папки ящика и какие из них попадают в раздел «По папкам»."""
    cfg = load_config(args.config)

    async def go() -> Any:
        async with make_source(cfg) as src:
            return await src.list_mailboxes()

    tree = asyncio.run(go())
    chosen = set(fld.choose(cfg, tree))
    if not fld.enabled(cfg):
        print("Сводка по папкам выключена (folders.enabled: false).")
    for box in fld.flatten(tree):
        mark = "смотрю    " if box["name"] in chosen else "пропускаю "
        attribs = " ".join(box["attribs"])
        print(f"{mark} {box['name']}" + (f"  {attribs}" if attribs else ""))
    print(f"\nВ сводке папок: {len(chosen)}. Выбрать вручную: folders.include / folders.exclude в config.yaml.")


def cmd_categories(args: argparse.Namespace) -> None:
    cfg = load_config(args.config)
    for c in cfg.categories:
        extra = []
        if c.analyze:
            extra.append("анализ")
        if c.drafts:
            extra.append("черновики")
        if c.extractor:
            extra.append(c.extractor)
        if c.default:
            extra.append("по умолчанию")
        print(f"{c.id:16} {c.importance:7} {c.title}" + (f"  [{', '.join(extra)}]" if extra else ""))


def cmd_importance(args: argparse.Namespace) -> None:
    cfg = load_config(args.config)
    if not cfg.category(args.category):
        raise SystemExit(f"Нет категории {args.category}. Список: mailpilot categories")
    ov = read_overrides(cfg)
    ov.setdefault("importance", {})[args.category] = args.level
    write_overrides(cfg, ov)
    print(f"Категория {args.category}: важность {args.level}. Применится к следующей сводке (или `mailpilot render`).")


def _add_rule(cfg: Config, category: str, match: dict[str, Any]) -> None:
    ov = read_overrides(cfg)
    ov.setdefault("rules", []).append({"category": category, "match": match})
    write_overrides(cfg, ov)


def cmd_rule(args: argparse.Namespace) -> None:
    cfg = load_config(args.config)
    if not cfg.category(args.category):
        raise SystemExit(f"Нет категории {args.category}. Список: mailpilot categories")
    match: dict[str, Any] = {}
    if args.sender:
        match["from_contains"] = [args.sender]
    if args.recipient:
        match["recipients_contain"] = [args.recipient]
    if args.subject:
        match["subject_regex"] = [args.subject]
    if args.body:
        match["body_regex"] = [args.body]
    if not match:
        raise SystemExit("Укажите хотя бы одно условие: --from, --recipient, --subject или --body.")
    _add_rule(cfg, args.category, match)
    print(f"Правило добавлено в {cfg.overrides_path}. Действует для новых писем.")


def cmd_mute(args: argparse.Namespace) -> None:
    cfg = load_config(args.config)
    ov = read_overrides(cfg)
    if not any(c.get("id") == "muted" for c in ov.get("categories_add") or []) and not cfg.category("muted"):
        ov.setdefault("categories_add", []).append({"id": "muted", "title": "Скрытые отправители", "importance": "hidden", "first": True, "match": []})
    ov.setdefault("rules", []).append({"category": "muted", "match": {"from_contains": [args.sender]}})
    write_overrides(cfg, ov)
    print(f"Письма от {args.sender} больше не будут показываться в сводке (только счётчик).")


def cmd_vip(args: argparse.Namespace) -> None:
    cfg = load_config(args.config)
    ov = read_overrides(cfg)
    adds, removes = ov.setdefault("vip_senders_add", []), ov.setdefault("vip_senders_remove", [])
    if args.action == "list":
        people = cfg.get("digest.vip_senders") or []
        print("\n".join(people) if people else "Список важных людей пуст.")
        return
    if not args.name:
        raise SystemExit("Укажите имя: mailpilot vip add \"Имя Фамилия\"")
    if args.action == "add":
        if args.name in removes:
            removes.remove(args.name)
        if args.name not in adds:
            adds.append(args.name)
        print(f"{args.name}: письма будут показываться в самом верху сводки.")
    else:
        if args.name in adds:
            adds.remove(args.name)
        if args.name not in removes:
            removes.append(args.name)
        print(f"{args.name} убран из важных людей.")
    write_overrides(cfg, ov)


def cmd_demo(args: argparse.Namespace) -> None:
    from .demo import run_demo

    path = run_demo(Path(args.dir), with_analysis=not args.raw, open_browser=args.open)
    print(f"Демо-сводка: {path}")


def cmd_serve(args: argparse.Namespace) -> None:
    from .server import serve

    serve(args.config, port=args.port, open_browser=not args.no_open)


MARK_KINDS = {"important": "important", "not-important": "not_important", "clear": "clear"}


def cmd_mark(args: argparse.Namespace) -> None:
    cfg = load_config(args.config)
    store = _store(cfg)
    try:
        fb.mark(store, args.thread, MARK_KINDS[args.label], args.reason)
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
    t = store.thread(args.thread) or {}
    print(f"«{t.get('subject')}»: " + ("отметка снята" if args.label == "clear" else fb.KIND_TITLES[MARK_KINDS[args.label]]))
    _rerender(cfg, store)
    store.close()


def cmd_feedback(args: argparse.Namespace) -> None:
    cfg = load_config(args.config)
    store = _store(cfg)
    rows = fb.decisions(store, args.limit)
    if args.json:
        _print_json({"preferences_file": str(cfg.preferences_path), "preferences": fb.read_preferences(cfg),
                     "decisions": fb.decisions_for_agent(store, args.limit),
                     "suggestions": fb.suggestions(cfg, store)})
    elif not rows:
        print("Отметок пока нет. Их ставят кнопками в сводке (`mailpilot serve`) или командой `mailpilot mark`.")
    else:
        for r in rows:
            reason = f" — {r['reason']}" if r.get("reason") else ""
            print(f"{r['created_at'][:10]}  {fb.KIND_TITLES.get(r['kind'], r['kind']):16} {r.get('subject') or ''}"
                  f" ({r.get('sender') or '?'}){reason}")
    store.close()


def cmd_suggestions(args: argparse.Namespace) -> None:
    cfg = load_config(args.config)
    store = _store(cfg)
    try:
        if args.action == "apply":
            sug = fb.apply_suggestion(cfg, store, args.n)
            print(f"Правило добавлено: {sug['text']}. Действует для новых писем.")
        elif args.action == "dismiss":
            fb.dismiss_suggestion(cfg, store, args.n)
            print("Больше не предлагать.")
        else:
            sugs = fb.suggestions(cfg, store)
            if not sugs:
                print("Предложений нет: они появляются, когда вы несколько раз отмечаете похожие письма.")
            for x in sugs:
                print(f"{x['n']}. {x['text']} (отметок: {x['count']}; например: {'; '.join(x['examples'][:2])})")
            if sugs:
                print("Принять: mailpilot suggestions apply N, отказаться: mailpilot suggestions dismiss N")
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
    store.close()


def cmd_preferences(args: argparse.Namespace) -> None:
    cfg = load_config(args.config)
    fb.ensure_preferences(cfg)
    print(cfg.preferences_path)
    if args.show:
        print(cfg.preferences_path.read_text(encoding="utf-8"))


# --- разбор аргументов ---------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="mailpilot", description="Помощник по почте и календарю: сводка, категории, черновики.")
    p.add_argument("--config", "-c", help="Путь к config.yaml (по умолчанию ./config.yaml или $MAILPILOT_CONFIG)")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("init", help="Создать config.yaml и .env из примера")
    s.add_argument("--force", action="store_true")
    s.set_defaults(func=cmd_init)

    sub.add_parser("doctor", help="Проверить настройки и соединение с почтой").set_defaults(func=cmd_doctor)
    sub.add_parser("fetch", help="Забрать новые письма").set_defaults(func=cmd_fetch)

    s = sub.add_parser("run", help="Забрать почту и собрать сводку (для cron)")
    s.add_argument("--open", action="store_true", help="Открыть сводку в браузере")
    s.set_defaults(func=cmd_run)

    sub.add_parser("prepare", help="Подготовить пакеты на анализ для агента").set_defaults(func=cmd_prepare)

    s = sub.add_parser("ingest", help="Принять результаты анализа от агента и пересобрать сводку")
    s.add_argument("--open", action="store_true")
    s.set_defaults(func=cmd_ingest)

    s = sub.add_parser("render", help="Пересобрать последнюю сводку")
    s.add_argument("--open", action="store_true")
    s.set_defaults(func=cmd_render)

    sub.add_parser("status", help="Состояние анализа и черновиков (JSON, для скилла)").set_defaults(func=cmd_status)
    sub.add_parser("drafts", help="Показать черновики на согласование").set_defaults(func=cmd_drafts)

    s = sub.add_parser("draft", help="Действия с черновиком")
    s.add_argument("action", choices=["show", "edit", "reject", "no-reply"])
    s.add_argument("id", type=int)
    s.add_argument("--text", help="Новый текст черновика")
    s.add_argument("--file", help="Взять новый текст из файла")
    s.add_argument("--reason", help="Почему ответ не нужен (для no-reply)")
    s.set_defaults(func=cmd_draft)

    s = sub.add_parser("send", help="Отправить черновик после подтверждения")
    s.add_argument("id", type=int)
    s.add_argument("--yes", action="store_true", help="Подтверждение уже получено (использовать только после явного «да»)")
    s.set_defaults(func=cmd_send)

    s = sub.add_parser("learn", help="Изучить ваши ответы из «Отправленных» и подготовить профиль стиля")
    s.add_argument("--days", type=int, default=90)
    s.add_argument("--style-only", action="store_true", help="Только пересобрать пакет для профиля стиля")
    s.set_defaults(func=cmd_learn)

    sub.add_parser("categories", help="Список категорий").set_defaults(func=cmd_categories)
    sub.add_parser("folders", help="Папки ящика и какие из них идут в раздел «По папкам»").set_defaults(func=cmd_folders)

    s = sub.add_parser("importance", help="Изменить важность категории")
    s.add_argument("category")
    s.add_argument("level", choices=IMPORTANCE_LEVELS)
    s.set_defaults(func=cmd_importance)

    s = sub.add_parser("rule", help="Добавить правило раскладки")
    s.add_argument("action", choices=["add"])
    s.add_argument("category")
    s.add_argument("--from", dest="sender")
    s.add_argument("--recipient")
    s.add_argument("--subject", help="Регулярное выражение для темы")
    s.add_argument("--body", help="Регулярное выражение для текста")
    s.set_defaults(func=cmd_rule)

    s = sub.add_parser("mute", help="Скрыть отправителя из сводки")
    s.add_argument("--from", dest="sender", required=True)
    s.set_defaults(func=cmd_mute)

    s = sub.add_parser("vip", help="Важные люди: их письма всегда наверху сводки")
    s.add_argument("action", choices=["list", "add", "remove"])
    s.add_argument("name", nargs="?", help="«Имя Фамилия», фамилия или адрес")
    s.set_defaults(func=cmd_vip)

    s = sub.add_parser("serve", help="Открыть сводку с кнопками разметки и правки ответов (локальный сервер)")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--no-open", action="store_true", help="Не открывать браузер")
    s.set_defaults(func=cmd_serve)

    s = sub.add_parser("mark", help="Отметить переписку: важно, не важно или снять отметку")
    s.add_argument("thread", help="id переписки (есть в пакетах и в mailpilot status)")
    s.add_argument("label", choices=list(MARK_KINDS))
    s.add_argument("--reason", help="Почему (агент учтёт)")
    s.set_defaults(func=cmd_mark)

    s = sub.add_parser("feedback", help="Ваши отметки и решения по письмам")
    s.add_argument("--limit", type=int, default=50)
    s.add_argument("--json", action="store_true", help="Для скилла: отметки, предпочтения и предложения")
    s.set_defaults(func=cmd_feedback)

    s = sub.add_parser("suggestions", help="Правила, предложенные по вашим отметкам")
    s.add_argument("action", nargs="?", choices=["list", "apply", "dismiss"], default="list")
    s.add_argument("n", nargs="?", help="Номер предложения")
    s.set_defaults(func=cmd_suggestions)

    s = sub.add_parser("preferences", help="Файл ваших правил для агента (создать, если нет)")
    s.add_argument("--show", action="store_true")
    s.set_defaults(func=cmd_preferences)

    s = sub.add_parser("demo", help="Собрать демо-сводку на выдуманных письмах")
    s.add_argument("--dir", default="demo", help="Папка для демо (по умолчанию ./demo)")
    s.add_argument("--raw", action="store_true", help="Без примера смыслового анализа")
    s.add_argument("--open", action="store_true")
    s.set_defaults(func=cmd_demo)
    return p


def main(argv: list[str] | None = None) -> None:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")  # консоль Windows
        except (ValueError, OSError):
            pass
    args = build_parser().parse_args(argv)
    try:
        args.func(args)
    except ConfigError as exc:
        raise SystemExit(f"Ошибка настроек: {exc}") from None
    except MailError as exc:
        raise SystemExit(f"Ошибка почтового сервера: {exc}") from None
