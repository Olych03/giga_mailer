"""Работа с текстом писем: адреса, темы, отделение цитат, простая нормализация слов."""

from __future__ import annotations

import html as html_lib
import re
from datetime import datetime, timezone
from email.utils import getaddresses

SUBJECT_PREFIX_RE = re.compile(
    r"^\s*((re|fw|fwd|ответ|отв|пересл|ha|aw|wg|tr)\s*(\[\d+\])?\s*:\s*|\[(external|внешн\w*)\]\s*)",
    re.IGNORECASE,
)

# Маркеры начала цитаты в ответах Outlook и других клиентов
QUOTE_MARKERS = [
    re.compile(r"^-{2,}\s*(original message|исходное сообщение|пересылаемое сообщение|forwarded message)\s*-{2,}", re.I),
    re.compile(r"^_{10,}\s*$"),
    re.compile(r"^(от|from)\s*:\s*.+$", re.I),
    re.compile(r"^.{0,120}(написал|написала|написал\(а\)|wrote)\s*:\s*$", re.I),
]
QUOTE_HEADER_FOLLOWERS = re.compile(r"^(отправлено|дата|sent|date|кому|to)\s*:", re.I)

WORD_RE = re.compile(r"[a-zа-яё0-9]{3,}", re.IGNORECASE)
STOPWORDS = set(
    """это как так что чтобы для или его она они мне вам вас нас нам все всё уже ещё еще
    при над под без через после если тоже также только можно нужно есть был была были будет
    добрый день коллеги здравствуйте спасибо пожалуйста уважением the and for with from this that""".split()
)


def parse_addresses(raw: str | None) -> list[tuple[str, str]]:
    """'Иванов Иван <i@x.ru>, b@y.ru' -> [('Иванов Иван', 'i@x.ru'), ('', 'b@y.ru')]."""
    if not raw:
        return []
    return [(name.strip(), addr.strip().lower()) for name, addr in getaddresses([raw]) if addr or name]


def display_name(raw: str | None) -> str:
    addrs = parse_addresses(raw)
    if not addrs:
        return raw or ""
    name, addr = addrs[0]
    return name or addr


def normalize_subject(subject: str | None) -> str:
    s = (subject or "").strip()
    while True:
        new = SUBJECT_PREFIX_RE.sub("", s, count=1)
        if new == s:
            break
        s = new
    return re.sub(r"\s+", " ", s).strip().lower()


def html_to_text(html: str) -> str:
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", html)
    text = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>|</li>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html_lib.unescape(text)
    text = re.sub(r"[ \t\xa0]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def split_quote(text: str) -> tuple[str, str]:
    """Делит письмо на новую часть и цитату предыдущих писем."""
    lines = (text or "").replace("\r\n", "\n").split("\n")
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith(">") and i > 0:
            return _join(lines[:i]), _join(lines[i:])
        for marker in QUOTE_MARKERS:
            if marker.match(stripped):
                # «От:» считаем началом цитаты, только если рядом есть «Отправлено:»/«Дата:»
                if marker.pattern.startswith("^(от|from)"):
                    window = [l.strip() for l in lines[i + 1 : i + 5]]
                    if not any(QUOTE_HEADER_FOLLOWERS.match(w) for w in window):
                        continue
                if i == 0:
                    continue
                return _join(lines[:i]), _join(lines[i:])
    return _join(lines), ""


def strip_quote_headers(quoted: str) -> str:
    """Убирает из цитаты блок заголовков (От/Отправлено/Кому/Тема) и маркеры '>'."""
    lines = [re.sub(r"^\s*>+\s?", "", l) for l in quoted.split("\n")]
    out: list[str] = []
    skipping = True
    for line in lines:
        s = line.strip()
        if skipping and (
            not s
            or any(m.match(s) for m in QUOTE_MARKERS)
            or re.match(r"^(от|from|отправлено|sent|дата|date|кому|to|копия|cc|тема|subject)\s*:", s, re.I)
        ):
            continue
        skipping = False
        out.append(line)
    new, _ = split_quote("\n".join(out))
    return new


def _join(lines: list[str]) -> str:
    return "\n".join(lines).strip()


def stems(text: str, limit: int | None = None) -> set[str]:
    """Грубая нормализация слов для поиска похожих писем: нижний регистр, обрезка до 6 букв."""
    words = WORD_RE.findall((text or "").lower())
    result: set[str] = set()
    for w in words:
        if w in STOPWORDS:
            continue
        result.add(w[:6])
        if limit and len(result) >= limit:
            break
    return result


def truncate(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + " …[обрезано]"


def one_line(text: str, limit: int = 160) -> str:
    return truncate(re.sub(r"\s+", " ", text or ""), limit)


def parse_dt(value: str | None) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    v = value.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(v)
    except ValueError:
        from email.utils import parsedate_to_datetime

        dt = parsedate_to_datetime(value)
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt.astimezone(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def local(dt_iso: str) -> datetime:
    return parse_dt(dt_iso).astimezone()


TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh", "з": "z", "и": "i",
    "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t",
    "у": "u", "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch", "ъ": "", "ы": "y",
    "ь": "", "э": "e", "ю": "yu", "я": "ya",
}


def translit(text: str) -> str:
    return "".join(TRANSLIT.get(ch, ch) for ch in text.lower())


# Частые имена: в записях вида «Анна Белоусова» искать нужно по фамилии, а не по имени
COMMON_FIRST_NAMES = set("""
александр александра алексей анастасия анатолий андрей анна антон артем артём борис вадим валентина валерий
василий вера виктор виктория виталий владимир владислав вячеслав галина геннадий георгий герман григорий дарья
денис дмитрий евгений евгения екатерина елена елизавета игорь илья инна ирина кирилл константин кристина ксения
лариса леонид людмила максим марина мария михаил надежда наталья наталия никита николай нина оксана олег ольга
павел петр пётр полина роман руслан светлана семен семён сергей софия станислав степан татьяна тимур федор фёдор
филипп эдуард юлия юрий яна ярослав
""".split())


def _expand(variants: set[str], a: str, b: str) -> set[str]:
    return variants | {v.replace(a, b) for v in variants if a in v}


def person_variants(name: str) -> list[str]:
    """Как искать человека в адресатах и отправителе: как написано и латиницей.

    mcp-mail-server отдаёт только адреса без имён, поэтому «Белоусова» ищется и в belousova@..., AABelousova@...
    Для записей из нескольких слов («Анна Белоусова») ищется фамилия, распространённые имена пропускаются.
    Адрес целиком (есть «@») сравнивается как есть.
    """
    entry = name.strip().lower()
    if "@" in entry:
        return [entry]
    words = [w for w in re.split(r"[\s.]+", entry) if len(w) >= 3]
    if len(words) > 1:
        words = [w for w in words if w not in COMMON_FIRST_NAMES] or words
    result: set[str] = set()
    for w in words:
        lat = {translit(w)}
        for a, b in (("iy", "ii"), ("iy", "y"), ("iy", "ij"), ("yy", "y"), ("kh", "h"), ("ts", "c"),
                     ("yu", "iu"), ("ya", "ia"), ("zh", "j")):
            lat = _expand(lat, a, b)
        result |= {w} | lat
    return sorted(v for v in result if v)


def match_person(entries: list[str], haystack: str) -> str | None:
    """Возвращает запись из списка, если человек найден в строке (адреса, имена)."""
    h = (haystack or "").lower()
    for e in entries:
        if any(v in h for v in person_variants(e)):
            return e
    return None


def plural(n: int, one: str, few: str, many: str) -> str:
    """plural(5, "письмо", "письма", "писем") -> "5 писем"."""
    n10, n100 = n % 10, n % 100
    word = one if n10 == 1 and n100 != 11 else few if 2 <= n10 <= 4 and not 12 <= n100 <= 14 else many
    return f"{n} {word}"
