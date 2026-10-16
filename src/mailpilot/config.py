"""Загрузка настроек: config.yaml, .env рядом с ним и пользовательские правки overrides.yaml."""

from __future__ import annotations

import copy
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

CONFIG_NAME = "config.yaml"
OVERRIDES_NAME = "overrides.yaml"
IMPORTANCE_LEVELS = ("high", "normal", "low", "hidden")


class ConfigError(Exception):
    pass


def example_config_path() -> Path:
    here = Path(__file__).resolve().parent
    for candidate in (here / "config.example.yaml", here.parents[1] / "config.example.yaml"):
        if candidate.exists():
            return candidate
    raise ConfigError("Не найден config.example.yaml рядом с программой.")


def load_dotenv(path: Path) -> None:
    """Простейший разбор .env: KEY=VALUE, без перезаписи уже заданных переменных."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key.strip(), value)


@dataclass
class Category:
    id: str
    title: str
    importance: str = "normal"
    analyze: bool = False
    drafts: bool = False
    extractor: str | None = None
    default: bool = False
    color: str = "#8a8f98"
    match: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class Config:
    raw: dict[str, Any]
    path: Path

    @property
    def base_dir(self) -> Path:
        return self.path.parent

    @property
    def data_dir(self) -> Path:
        d = Path(self.raw.get("data_dir") or "data")
        if not d.is_absolute():
            d = self.base_dir / d
        return d

    @property
    def db_path(self) -> Path:
        return self.data_dir / "mailpilot.db"

    @property
    def work_dir(self) -> Path:
        return self.data_dir / "work"

    @property
    def digests_dir(self) -> Path:
        return self.data_dir / "digests"

    @property
    def style_path(self) -> Path:
        return self.data_dir / "style.md"

    @property
    def preferences_path(self) -> Path:
        return self.data_dir / "preferences.md"

    @property
    def overrides_path(self) -> Path:
        return self.data_dir / OVERRIDES_NAME

    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self.raw
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    @property
    def my_addresses(self) -> list[str]:
        return [a.lower() for a in (self.get("me.addresses") or [])]

    @property
    def incident_re(self) -> re.Pattern[str]:
        return re.compile(self.get("incident_regex") or r"\bINC\d{4,}\b", re.IGNORECASE)

    @property
    def categories(self) -> list[Category]:
        cats = []
        for c in self.raw.get("categories") or []:
            cats.append(
                Category(
                    id=str(c["id"]),
                    title=str(c.get("title") or c["id"]),
                    importance=str(c.get("importance") or "normal"),
                    analyze=bool(c.get("analyze", False)),
                    drafts=bool(c.get("drafts", False)),
                    extractor=c.get("extractor"),
                    default=bool(c.get("default", False)),
                    color=str(c.get("color") or "#8a8f98"),
                    match=list(c.get("match") or []),
                )
            )
        return cats

    def category(self, cat_id: str | None) -> Category | None:
        for c in self.categories:
            if c.id == cat_id:
                return c
        return None


def _apply_overrides(raw: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    """Правки из overrides.yaml: важность категорий, дополнительные правила, новые категории."""
    raw = copy.deepcopy(raw)
    cats = raw.setdefault("categories", [])
    by_id = {c["id"]: c for c in cats}

    for cat in overrides.get("categories_add") or []:
        if cat.get("id") not in by_id:
            # Новые категории вставляем перед категорией по умолчанию
            if cat.get("first"):
                idx = 0  # например, скрытые отправители проверяются раньше всех правил
            else:
                idx = next((i for i, c in enumerate(cats) if c.get("default")), len(cats))
            cats.insert(idx, dict(cat))
            by_id[cat["id"]] = cats[idx]

    for cat_id, level in (overrides.get("importance") or {}).items():
        if cat_id in by_id:
            by_id[cat_id]["importance"] = level

    # Важные люди, добавленные командой `mailpilot vip add`
    extra_vip = [v for v in overrides.get("vip_senders_add") or []]
    removed = {v.lower() for v in overrides.get("vip_senders_remove") or []}
    if extra_vip or removed:
        digest = raw.setdefault("digest", {}) or {}
        raw["digest"] = digest
        current = list(digest.get("vip_senders") or []) + [v for v in extra_vip if v not in (digest.get("vip_senders") or [])]
        digest["vip_senders"] = [v for v in current if v.lower() not in removed]

    # Отправители, на письма которых не нужно предлагать ответы (решения из сводки)
    no_drafts = list(overrides.get("no_drafts_from") or [])
    if no_drafts:
        analysis = raw.setdefault("analysis", {}) or {}
        raw["analysis"] = analysis
        analysis["no_drafts_from"] = list(analysis.get("no_drafts_from") or []) + [a for a in no_drafts if a not in (analysis.get("no_drafts_from") or [])]

    # Пользовательские правила проверяются раньше правил категории
    for rule in overrides.get("rules") or []:
        cat = by_id.get(rule.get("category"))
        if cat is not None:
            cat["match"] = [rule["match"]] + list(cat.get("match") or [])
    return raw


def load_config(path: str | Path | None = None) -> Config:
    if path is None:
        path = os.environ.get("MAILPILOT_CONFIG") or CONFIG_NAME
    p = Path(path).expanduser().resolve()
    if not p.exists():
        raise ConfigError(f"Не найден файл настроек {p}. Создайте его командой `mailpilot init`.")
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    load_dotenv(p.parent / ".env")
    cfg = Config(raw=raw, path=p)
    if cfg.overrides_path.exists():
        overrides = yaml.safe_load(cfg.overrides_path.read_text(encoding="utf-8")) or {}
        cfg = Config(raw=_apply_overrides(raw, overrides), path=p)
    validate(cfg)
    return cfg


def validate(cfg: Config) -> None:
    cats = cfg.categories
    if not cats:
        raise ConfigError("В настройках нет ни одной категории (раздел categories).")
    ids = [c.id for c in cats]
    if len(ids) != len(set(ids)):
        raise ConfigError("У категорий должны быть разные id.")
    for c in cats:
        if c.importance not in IMPORTANCE_LEVELS:
            raise ConfigError(
                f"Категория {c.id}: importance должно быть одним из {', '.join(IMPORTANCE_LEVELS)}."
            )
    if not any(c.default for c in cats):
        raise ConfigError("Отметьте одну категорию как default: true, туда попадут остальные письма.")
    if cfg.get("source", "mcp") == "mcp" and not cfg.my_addresses:
        raise ConfigError("Укажите свои адреса в me.addresses.")


def read_overrides(cfg: Config) -> dict[str, Any]:
    if cfg.overrides_path.exists():
        return yaml.safe_load(cfg.overrides_path.read_text(encoding="utf-8")) or {}
    return {}


def write_overrides(cfg: Config, data: dict[str, Any]) -> None:
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    header = "# Ваши правки категорий, сделанные командами mailpilot. Можно править руками.\n"
    cfg.overrides_path.write_text(
        header + yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
