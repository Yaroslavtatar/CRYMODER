"""Разбор периодов, форматирование, упоминания."""
import html
import re
import time
from datetime import datetime, timedelta, timezone

from . import config

TZ = timezone(timedelta(hours=config.TZ_OFFSET_HOURS))

def unit_of(tok: str):
    t = tok.lower().replace("ё", "е")
    if re.fullmatch(r"мин(?:ут[ауы]?)?", t):
        return 60
    if re.fullmatch(r"час(?:а|ов)?", t):
        return 3600
    if re.fullmatch(r"сут(?:ки|ок)?|ден[ья]|дн(?:я|ей)?|дней", t):
        return 86400
    if re.fullmatch(r"нед(?:ел[яьи])?", t):
        return 604800
    if re.fullmatch(r"мес(?:яц(?:а|ев)?)?", t):
        return 2592000
    if re.fullmatch(r"год(?:а)?|лет", t):
        return 31536000
    return None


FOREVER = -1


def take_period(tokens: list[str]):
    """Снимает период с начала списка токенов.
    Возвращает (секунды | FOREVER | None, оставшиеся токены)."""
    if not tokens:
        return None, tokens
    t0 = tokens[0].lower()
    if t0 in ("навсегда", "навечно", "perm"):
        return FOREVER, tokens[1:]
    if t0.isdigit() and len(tokens) > 1:
        u = unit_of(tokens[1])
        if u:
            return int(t0) * u, tokens[2:]
    u = unit_of(t0)
    if u:
        return u, tokens[1:]
    return None, tokens


def fmt_duration(secs: int) -> str:
    if secs == FOREVER or secs <= 0:
        return "навсегда"
    for name, size in (("г.", 31536000), ("мес.", 2592000), ("нед.", 604800), ("дн.", 86400),
                       ("ч.", 3600), ("мин.", 60)):
        if secs >= size:
            n = secs // size
            rest = secs - n * size
            out = f"{n} {name}"
            for n2, s2 in (("дн.", 86400), ("ч.", 3600), ("мин.", 60)):
                if rest >= s2 and s2 < size:
                    out += f" {rest // s2} {n2}"
                    break
            return out
    return f"{secs} сек."


def fmt_ts(ts: int) -> str:
    if not ts:
        return "—"
    return datetime.fromtimestamp(ts, TZ).strftime("%d.%m.%Y %H:%M")


def fmt_until(until: int) -> str:
    return "навсегда" if not until or until < 0 else "до " + fmt_ts(until)


def today_str(ts=None) -> str:
    return datetime.fromtimestamp(ts or time.time(), TZ).strftime("%Y-%m-%d")


def day_ago(n: int) -> str:
    return (datetime.now(TZ) - timedelta(days=n)).strftime("%Y-%m-%d")


def esc(s) -> str:
    return html.escape(str(s if s is not None else ""), quote=False)


def mention(uid: int, name: str) -> str:
    return f'<a href="tg://user?id={uid}">{esc(name) or uid}</a>'


def full_name(first, last, username, uid) -> str:
    n = " ".join(x for x in (first, last) if x).strip()
    return n or (f"@{username}" if username else str(uid))


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", s.lower().replace("ё", "е")).strip()


def now() -> int:
    return int(time.time())
