"""Локальный сервер сводки: `mailpilot serve`.

Показывает ту же сводку, но с кнопками: «важно / не важно», правка ответа, «ответ не нужен»,
отправка с подтверждением, принятие предложенных правил. Работает только на 127.0.0.1,
без внешних зависимостей (встроенный http.server).

Защита от чужих страниц в браузере: каждый запрос на изменение должен нести случайный токен,
который есть только в странице, выданной этим сервером, а заголовок Host должен быть локальным.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Callable

from . import feedback as fb
from .config import Config, load_config
from .digest import build_digest, current_period, rerender
from .mail_source import MailError, make_source
from .render import render_html
from .sending import send_draft
from .store import Store

MAX_BODY = 256 * 1024


class ApiError(Exception):
    pass


def page_html(cfg: Config, store: Store, token: str) -> str:
    period_from, period_to = current_period(cfg, store)
    model = build_digest(cfg, store, period_from, period_to)
    model.update(interactive=True, token=token)
    return render_html(model)


# --- действия --------------------------------------------------------------------

def act_mark(cfg: Config, store: Store, body: dict[str, Any]) -> dict[str, Any]:
    tid, label = str(body.get("thread_id") or ""), str(body.get("label") or "")
    if label not in ("important", "not_important", "clear"):
        raise ApiError("Неизвестная отметка")
    try:
        return fb.mark(store, tid, label, body.get("reason"))
    except ValueError as exc:
        raise ApiError(str(exc)) from None


def act_reason(cfg: Config, store: Store, body: dict[str, Any]) -> dict[str, Any]:
    fb.set_reason(store, str(body.get("thread_id") or ""), str(body.get("kind") or ""), str(body.get("reason") or ""))
    return {"saved": True}


def _proposed_draft(store: Store, body: dict[str, Any]) -> dict[str, Any]:
    try:
        d = store.draft(int(body.get("id")))
    except (TypeError, ValueError):
        d = None
    if not d or d["status"] != "proposed":
        raise ApiError("Черновик не найден или уже обработан")
    return d


def _save_text(store: Store, d: dict[str, Any], text: Any) -> dict[str, Any]:
    text = (str(text) if text is not None else "").strip()
    if not text:
        raise ApiError("Пустой текст")
    if text != (d.get("text_final") or d["text_proposed"]).strip():
        store.update_draft(d["id"], text_final=text)
        store.commit()
    return store.draft(d["id"]) or d


def act_draft(cfg: Config, store: Store, body: dict[str, Any]) -> dict[str, Any]:
    d = _proposed_draft(store, body)
    action = body.get("action")
    if action == "save":
        d = _save_text(store, d, body.get("text"))
        return {"id": d["id"], "text": d.get("text_final") or d["text_proposed"]}
    if action == "reject":
        store.update_draft(d["id"], status="rejected")
        store.commit()
        return {"id": d["id"], "status": "rejected"}
    if action == "no_reply":
        fb.mark(store, d["thread_id"], "no_reply", body.get("reason"))
        return {"id": d["id"], "status": "no_reply"}
    if action == "send":
        # Отправка только с явным подтверждением из окна, где пользователь видел текст и адресатов
        if body.get("confirm") is not True:
            raise ApiError("Нужно подтверждение отправки")
        d = _save_text(store, d, body.get("text"))

        async def go() -> Any:
            async with make_source(cfg) as src:
                return await send_draft(cfg, store, src, d)

        try:
            result = asyncio.run(go())
        except MailError as exc:
            raise ApiError(f"Не отправлено: {exc}") from None
        warn = result.get("sentFolderError") if isinstance(result, dict) else None
        return {"id": d["id"], "status": "sent", "warning": warn}
    raise ApiError("Неизвестное действие")


def act_suggestion(cfg: Config, store: Store, body: dict[str, Any]) -> dict[str, Any]:
    sid = str(body.get("id") or "")
    try:
        if body.get("action") == "apply":
            sug = fb.apply_suggestion(cfg, store, sid)
            return {"applied": sug["text"]}
        if body.get("action") == "dismiss":
            fb.dismiss_suggestion(cfg, store, sid)
            return {"dismissed": True}
    except ValueError as exc:
        raise ApiError(str(exc)) from None
    raise ApiError("Неизвестное действие")


ROUTES: dict[str, Callable[[Config, Store, dict[str, Any]], dict[str, Any]]] = {
    "/api/mark": act_mark,
    "/api/reason": act_reason,
    "/api/draft": act_draft,
    "/api/suggestion": act_suggestion,
}


# --- HTTP ------------------------------------------------------------------------

def make_handler(config_path: str | None, token: str) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "mailpilot"

        def log_message(self, fmt: str, *args: Any) -> None:  # без шума в терминале
            pass

        def _local_host(self) -> bool:
            port = self.server.server_address[1]
            return self.headers.get("Host", "") in (f"127.0.0.1:{port}", f"localhost:{port}")

        def _send(self, status: int, body: bytes, ctype: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Frame-Options", "DENY")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, data: dict[str, Any]) -> None:
            self._send(status, json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

        def do_GET(self) -> None:  # noqa: N802
            if not self._local_host():
                self._send(HTTPStatus.FORBIDDEN, b"forbidden", "text/plain")
                return
            if self.path.split("?")[0] != "/":
                self._send(HTTPStatus.NOT_FOUND, b"not found", "text/plain")
                return
            cfg = load_config(config_path)
            store = Store(cfg.db_path)
            try:
                html = page_html(cfg, store, token)
            finally:
                store.close()
            self._send(HTTPStatus.OK, html.encode("utf-8"), "text/html; charset=utf-8")

        def do_POST(self) -> None:  # noqa: N802
            if not self._local_host() or not secrets.compare_digest(self.headers.get("X-Mailpilot-Token", ""), token):
                self._json(HTTPStatus.FORBIDDEN, {"error": "Запрос не со страницы mailpilot"})
                return
            route = ROUTES.get(self.path)
            if not route:
                self._json(HTTPStatus.NOT_FOUND, {"error": "Нет такого действия"})
                return
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                self._json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "Слишком большой запрос"})
                return
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
                if not isinstance(body, dict):
                    raise ValueError
            except ValueError:
                self._json(HTTPStatus.BAD_REQUEST, {"error": "Ожидается JSON"})
                return
            cfg = load_config(config_path)
            store = Store(cfg.db_path)
            try:
                result = route(cfg, store, body)
                rerender(cfg, store)  # файл сводки тоже отражает ваши решения
                self._json(HTTPStatus.OK, {"ok": True, **result})
            except ApiError as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
            finally:
                store.close()

    return Handler


def make_server(config_path: str | None, port: int = 8765, token: str | None = None) -> tuple[HTTPServer, str]:
    token = token or secrets.token_urlsafe(24)
    handler = make_handler(config_path, token)
    last_error: OSError | None = None
    # Если порт занят, пробуем следующие
    for p in ([port] if port == 0 else range(port, port + 20)):
        try:
            return HTTPServer(("127.0.0.1", p), handler), token
        except OSError as exc:
            last_error = exc
    raise OSError(f"Не удалось занять порт начиная с {port}: {last_error}")


def serve(config_path: str | None, port: int = 8765, open_browser: bool = True) -> None:
    load_config(config_path)  # сразу показать ошибку настроек, а не при первом запросе
    httpd, _ = make_server(config_path, port)
    url = f"http://127.0.0.1:{httpd.server_address[1]}/"
    print(f"Сводка с кнопками: {url}")
    print("Сервер доступен только с этого компьютера. Остановить: Ctrl+C или закрыть окно терминала.")
    if open_browser:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nОстановлено.")
    finally:
        httpd.server_close()
