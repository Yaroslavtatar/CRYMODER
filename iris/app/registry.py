"""Реестр команд: используется и ботом (разбор текста), и веб-панелью (таблица «доступа команд»)."""
from dataclasses import dataclass
from typing import Callable, Optional

from .util import norm


@dataclass
class Command:
    code: str
    title: str
    section: str
    default_rank: int
    phrases: tuple
    handler: Optional[Callable] = None
    log: bool = False          # писать в журнал действий
    pm: bool = False           # работает и в личке с ботом
    number: int = 0


COMMANDS: dict[str, Command] = {}
PHRASE_INDEX: list[tuple[str, Command]] = []


def cmd(code: str, title: str, section: str, rank: int, phrases, log: bool = False, pm: bool = False):
    def deco(fn):
        c = Command(code, title, section, rank, tuple(norm(p) for p in phrases), fn, log, pm,
                    number=len(COMMANDS) + 1)
        COMMANDS[code] = c
        return fn
    return deco


def rebuild_index() -> None:
    PHRASE_INDEX.clear()
    for c in COMMANDS.values():
        for p in c.phrases:
            PHRASE_INDEX.append((p, c))
    PHRASE_INDEX.sort(key=lambda x: -len(x[0]))


def find_command(line_norm: str):
    """Возвращает (Command, число слов фразы) для самой длинной подходящей фразы."""
    for phrase, c in PHRASE_INDEX:
        if line_norm == phrase or line_norm.startswith(phrase + " "):
            return c, len(phrase.split())
    return None, 0


def resolve_code(token: str) -> Optional[Command]:
    """Команда по коду или номеру (для «дк бан 2» / «дк 12 2»)."""
    t = norm(token)
    if t.isdigit():
        for c in COMMANDS.values():
            if c.number == int(t):
                return c
        return None
    if t in COMMANDS:
        return COMMANDS[t]
    for c in COMMANDS.values():
        if t in c.phrases:
            return c
    return None
