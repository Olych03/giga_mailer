"""Источник почты: mcp-mail-server по протоколу MCP или выдуманные письма для демо.

Скрипт сам запускает mcp-mail-server как дочерний процесс и вызывает его инструменты напрямую,
без агента и без gigacode.
"""

from __future__ import annotations

import json
import os
import shutil
from contextlib import AsyncExitStack
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .config import Config
from .textutil import iso, parse_dt


class MailError(Exception):
    pass


class McpMailSource:
    """Асинхронный клиент к mcp-mail-server. Используется как `async with McpMailSource(cfg) as src`."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._stack: AsyncExitStack | None = None
        self.session: Any = None

    def _server_params(self) -> Any:
        from mcp import StdioServerParameters

        command = self.cfg.get("mcp.command") or "npx"
        resolved = shutil.which(command) or command  # на Windows npx — это npx.cmd
        env = dict(os.environ)
        env.update({k: str(v) for k, v in (self.cfg.get("mcp.env") or {}).items()})
        if not env.get("EMAIL_PASS"):
            raise MailError("Не задан пароль: добавьте EMAIL_PASS=... в файл .env рядом с config.yaml.")
        # Папка, куда сервер может сохранять вложения (нужно для приглашений .ics)
        att_dir = self.cfg.data_dir / "attachments"
        att_dir.mkdir(parents=True, exist_ok=True)
        roots = [r for r in env.get("MAIL_ALLOWED_ROOTS", "").split(os.pathsep) if r]
        if str(att_dir) not in roots:
            roots.append(str(att_dir))
        env["MAIL_ALLOWED_ROOTS"] = os.pathsep.join(roots)
        return StdioServerParameters(command=resolved, args=list(self.cfg.get("mcp.args") or []), env=env)

    async def __aenter__(self) -> "McpMailSource":
        from mcp import ClientSession
        from mcp.client.stdio import stdio_client

        self._stack = AsyncExitStack()
        try:
            # Служебный вывод сервера пишем в файл, чтобы не засорять терминал и лог cron
            self.cfg.data_dir.mkdir(parents=True, exist_ok=True)
            errlog = self._stack.enter_context(open(self.cfg.data_dir / "mcp-server.log", "a", encoding="utf-8"))
            read, write = await self._stack.enter_async_context(stdio_client(self._server_params(), errlog=errlog))
            self.session = await self._stack.enter_async_context(ClientSession(read, write))
            await self.session.initialize()
        except Exception:
            await self._stack.aclose()
            raise
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._stack:
            await self._stack.aclose()

    async def call(self, tool: str, args: dict[str, Any]) -> Any:
        result = await self.session.call_tool(tool, args)
        texts = [c.text for c in result.content if getattr(c, "type", None) == "text"]
        body = "\n".join(texts)
        # В mcp 1.x поле называется isError, в 2.x — is_error
        is_error = getattr(result, "is_error", None)
        if is_error is None:
            is_error = getattr(result, "isError", False)
        if is_error:
            raise MailError(f"{tool}: {body}")
        try:
            return json.loads(body)
        except ValueError:
            return body

    # --- операции, которые использует mailpilot ---------------------------------
    async def check(self) -> Any:
        return await self.call("check_connection", {})

    async def list_mailboxes(self) -> Any:
        return await self.call("list_mailboxes", {})

    async def search(self, mailboxes: list[str] | None, since: datetime, before: datetime, limit: int = 200) -> dict[str, Any]:
        args: dict[str, Any] = {"since": iso(since), "before": iso(before), "limit": limit}
        if mailboxes:
            args["mailboxes"] = mailboxes
        return await self.call("search_messages", args)

    async def get_messages(self, mailbox: str, uids: list[int], uid_validity: int | None) -> list[dict[str, Any]]:
        args: dict[str, Any] = {"mailbox": mailbox, "uids": uids}
        if uid_validity:
            args["uidValidity"] = uid_validity
        res = await self.call("get_messages", args)
        if isinstance(res, dict):
            return list(res.get("messages") or [])
        return list(res or [])

    async def save_attachment(self, mailbox: str, uid: int, uid_validity: int | None, index: int, save_dir: Path) -> list[Path]:
        save_dir.mkdir(parents=True, exist_ok=True)
        args: dict[str, Any] = {"mailbox": mailbox, "uid": uid, "attachmentIndex": index, "savePath": str(save_dir)}
        if uid_validity:
            args["uidValidity"] = uid_validity
        res = await self.call("save_attachment", args)
        files = []
        for f in (res.get("savedFiles") if isinstance(res, dict) else None) or []:
            p = f.get("path") if isinstance(f, dict) else f
            if p:
                files.append(Path(p))
        if not files:
            files = sorted(save_dir.glob("*.ics"))
        return files

    async def reply(self, mailbox: str, uid: int, uid_validity: int | None, text: str, reply_all: bool) -> Any:
        args: dict[str, Any] = {
            "mailbox": mailbox,
            "originalUid": uid,
            "text": text,
            "replyToAll": reply_all,
            "includeOriginal": True,
        }
        if uid_validity:
            args["uidValidity"] = uid_validity
        return await self.call("reply_to_email", args)


class DemoMailSource:
    """Выдуманные письма (см. demo.py). Ничего никуда не отправляет, ответы пишет в data/outbox.jsonl."""

    def __init__(self, cfg: Config):
        from .demo import demo_messages

        self.cfg = cfg
        self.messages = demo_messages()

    async def __aenter__(self) -> "DemoMailSource":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def check(self) -> Any:
        return {"imap": "demo", "smtp": "demo"}

    async def list_mailboxes(self) -> Any:
        # Тот же вид, что у mcp-mail-server: дерево с признаками папок
        def box(name: str, attribs: list[str] | None = None, children: dict[str, Any] | None = None,
                selectable: bool = True) -> dict[str, Any]:
            return {"name": name, "attribs": attribs or [], "delimiter": "/", "selectable": selectable,
                    "children": children or {}}

        return {
            "INBOX": box("INBOX", children={"Рассылки": box("INBOX/Рассылки")}),
            "Отправленные": box("Отправленные", ["\\Sent"]),
            "Черновики": box("Черновики", ["\\Drafts"]),
            "Удаленные": box("Удаленные", ["\\Trash"]),
            "Проекты": box("Проекты", ["\\Noselect"], {"Альфа": box("Проекты/Альфа")}, selectable=False),
        }

    async def search(self, mailboxes: list[str] | None, since: datetime, before: datetime, limit: int = 200) -> dict[str, Any]:
        found = [
            m for m in self.messages
            if since <= parse_dt(m["date"]) < before and (not mailboxes or m["sourceMailbox"] in mailboxes)
        ]
        found.sort(key=lambda m: m["date"], reverse=True)
        return {
            "totalMatches": len(found),
            "hasMore": len(found) > limit,
            "messages": [{k: m[k] for k in ("uid", "uidValidity", "sourceMailbox", "date")} for m in found[:limit]],
        }

    async def get_messages(self, mailbox: str, uids: list[int], uid_validity: int | None) -> list[dict[str, Any]]:
        return [m for m in self.messages if m["sourceMailbox"] == mailbox and m["uid"] in uids]

    async def save_attachment(self, mailbox: str, uid: int, uid_validity: int | None, index: int, save_dir: Path) -> list[Path]:
        msg = next(m for m in self.messages if m["sourceMailbox"] == mailbox and m["uid"] == uid)
        att = msg["attachments"][index]
        save_dir.mkdir(parents=True, exist_ok=True)
        p = save_dir / att["filename"]
        p.write_text(att["_content"], encoding="utf-8")
        return [p]

    async def reply(self, mailbox: str, uid: int, uid_validity: int | None, text: str, reply_all: bool) -> Any:
        out = self.cfg.data_dir / "outbox.jsonl"
        out.parent.mkdir(parents=True, exist_ok=True)
        rec = {"mailbox": mailbox, "uid": uid, "reply_all": reply_all, "text": text}
        with out.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return {"demo": True, "savedTo": str(out)}


def make_source(cfg: Config) -> Any:
    if cfg.get("source", "mcp") == "demo":
        return DemoMailSource(cfg)
    return McpMailSource(cfg)


async def search_all(src: Any, mailboxes: list[str] | None, since: datetime, before: datetime) -> list[dict[str, Any]]:
    """Сервер отдаёт максимум 200 писем за раз, поэтому большой интервал делим пополам, пока всё не влезет."""
    res = await src.search(mailboxes, since, before, limit=200)
    if res.get("hasMore") and (before - since) > timedelta(minutes=5):
        mid = since + (before - since) / 2
        return await search_all(src, mailboxes, since, mid) + await search_all(src, mailboxes, mid, before)
    return list(res.get("messages") or [])
