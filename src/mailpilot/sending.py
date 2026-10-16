"""Черновики: просмотр, правка и отправка только после явного подтверждения."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from typing import Any

from .config import Config
from .store import Store
from .textutil import display_name, iso, now_utc, parse_addresses, stems

CONFIRM_WORDS = {"да", "д", "yes", "y"}


def draft_text(cfg: Config, d: dict[str, Any]) -> str:
    text = (d.get("text_final") or d.get("text_proposed") or "").rstrip()
    sig = (cfg.get("signature") or "").strip()
    if sig and sig not in text:
        text = f"{text}\n\n{sig}"
    return text


def describe_draft(cfg: Config, store: Store, d: dict[str, Any]) -> str:
    target = store.message(d["reply_to_id"]) or {}
    me = set(cfg.my_addresses)
    to = display_name(target.get("from_raw"))
    lines = [f"Черновик №{d['id']} ({d['status']})", f"Ответ на: «{target.get('subject')}» от {to}"]
    if d.get("reply_all"):
        others = [n or a for n, a in parse_addresses(f"{target.get('to_raw') or ''}, {target.get('cc_raw') or ''}") if a not in me]
        if others:
            lines.append("Копия всем: " + ", ".join(others[:15]) + (" и др." if len(others) > 15 else ""))
    lines += ["-" * 60, draft_text(cfg, d), "-" * 60]
    return "\n".join(lines)


def edit_in_editor(initial: str) -> str:
    editor = os.environ.get("EDITOR") or os.environ.get("VISUAL")
    if not editor:
        editor = "notepad" if sys.platform == "win32" else (shutil.which("nano") and "nano") or "vi"
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as f:
        f.write(initial)
        path = f.name
    try:
        subprocess.call([*editor.split(), path])
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    finally:
        os.unlink(path)


def confirm(prompt: str) -> bool:
    try:
        answer = input(prompt)
    except EOFError:
        return False
    return answer.strip().lower() in CONFIRM_WORDS


async def send_draft(cfg: Config, store: Store, src: Any, d: dict[str, Any]) -> Any:
    target = store.message(d["reply_to_id"])
    if not target:
        raise SystemExit("Не найдено письмо, на которое отвечает черновик.")
    text = draft_text(cfg, d)
    result = await src.reply(target["mailbox"], target["uid"], target.get("uid_validity"), text, bool(d["reply_all"]))
    store.update_draft(d["id"], status="sent", sent_at=iso(now_utc()), text_final=d.get("text_final") or d["text_proposed"],
                       send_result=str(result)[:2000])
    # Отправленный ответ становится образцом, а ваша правка — особенно ценным
    edited = bool(d.get("text_final")) and d["text_final"].strip() != d["text_proposed"].strip()
    incoming = target.get("new_text") or ""
    store.add_example({
        "source": "edit" if edited else "approved",
        "subject": target.get("subject"),
        "incoming": incoming,
        "reply": d.get("text_final") or d["text_proposed"],
        "terms": " ".join(sorted(stems(f"{target.get('subject') or ''} {incoming}", limit=120))),
        "date": iso(now_utc()),
    })
    store.commit()
    return result
