"""HTML-сводка: один самодостаточный файл без внешних ресурсов (работает внутри закрытого контура)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from jinja2 import Environment, select_autoescape

TEMPLATE_PATH = Path(__file__).with_name("digest_template.html")

_env = Environment(autoescape=select_autoescape(default=True, default_for_string=True), trim_blocks=True, lstrip_blocks=True)
_template = _env.from_string(TEMPLATE_PATH.read_text(encoding="utf-8"))


def render_html(model: dict[str, Any]) -> str:
    return _template.render(**model)
