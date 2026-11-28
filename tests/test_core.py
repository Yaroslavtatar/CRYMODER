import sqlite3

import pytest

from crymoder.core import Core
from crymoder.db import DB
from crymoder.handlers.automod import find_bad_word, has_invite, is_flooding
from crymoder.utils import FOREVER, fmt_duration, take_period


def test_take_period():
    assert take_period(["10m", "x"]) == (600, ["x"])
    assert take_period(["2h"]) == (7200, [])
    assert take_period(["1d"]) == (86400, [])
    assert take_period(["1w"]) == (604800, [])
    assert take_period(["2", "часа", "спам"]) == (7200, ["спам"])
    assert take_period(["навсегда"]) == (FOREVER, [])
    assert take_period(["спам", "10"]) == (None, ["спам", "10"])
    assert take_period(["10"]) == (None, ["10"])


def test_fmt_duration():
    assert fmt_duration(FOREVER) == "навсегда"
    assert fmt_duration(600) == "10 мин."
    assert fmt_duration(86400) == "1 дн."


def test_flood_detection():
    t = 1000.0
    results = [is_flooding(1, 1, limit=3, window=5, now=t + i) for i in range(3)]
    assert results == [False, False, True]
    # после срабатывания счётчик сброшен
    assert is_flooding(1, 1, limit=3, window=5, now=t + 10) is False
    # медленный поток не флуд
    assert not any(is_flooding(1, 2, limit=3, window=5, now=t + i * 10) for i in range(10))


def test_bad_words():
    words = ["казино", "спам"]
    assert find_bad_word("Лучшее КАЗИНО тут", words) == "казино"
    assert find_bad_word("спам!", words) == "спам"
    assert find_bad_word("спамер", words) is None   # границы слова
    assert find_bad_word("всё чисто", words) is None


class _Msg:
    def __init__(self, text):
        self.text, self.caption, self.entities, self.caption_entities = text, None, [], []


@pytest.mark.parametrize("text,expected", [
    ("заходи t.me/+AbCdEf123", True),
    ("https://t.me/joinchat/XYZ", True),
    ("подписывайся t.me/some_channel", True),
    ("обычное сообщение", False),
    ("сайт example.com", False),
])
def test_invite_detection(text, expected):
    assert has_invite(_Msg(text)) is expected


def test_settings_words_captcha(tmp_path):
    db = DB(str(tmp_path / "t.db"))
    core = Core(db)
    assert core.get(42, "warn_limit") == 3 and core.get(42, "captcha") == 1
    assert core.get(42, "warn_action") == "mute" and core.get(42, "warn_mute_secs") == 86400

    assert core.set_setting(42, "warn_limit", 5, 1, "chat").ok and core.get(42, "warn_limit") == 5
    assert not core.set_setting(42, "warn_limit", 99, 1, "chat").ok       # вне диапазона
    assert not core.set_setting(42, "warn_action", "explode", 1, "chat").ok
    assert core.set_setting(42, "antilink", 1, 1, "chat").ok and core.get(42, "antilink") == 1

    core.add_word(42, "слово")
    assert core.words(42) == ["слово"]
    assert core.del_word(42, "слово") is True and core.del_word(42, "слово") is False
    assert core.words(42) == []

    core.captcha_add(42, 7, 100, 123)
    assert core.captcha_get(42, 7)["message_id"] == 100
    assert len(core.captcha_all()) == 1
    core.captcha_del(42, 7)
    assert core.captcha_get(42, 7) is None


def test_migration_from_old_crymoder_db(tmp_path):
    """База версии 0.1 (старые warns без by_id/expires_at, лимит в max_warns) открывается без потерь."""
    path = str(tmp_path / "old.db")
    old = sqlite3.connect(path)
    old.executescript("""
        CREATE TABLE settings(chat_id INTEGER NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL, PRIMARY KEY(chat_id,key));
        CREATE TABLE warns(id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
                           reason TEXT, created_at INTEGER NOT NULL);
        CREATE TABLE badwords(chat_id INTEGER NOT NULL, word TEXT NOT NULL, PRIMARY KEY(chat_id,word));
        INSERT INTO settings VALUES (42,'max_warns','5'),(42,'antilink','1'),(42,'warn_action','ban');
        INSERT INTO warns(chat_id,user_id,reason,created_at) VALUES (42,7,'старый варн',1000000000);
        INSERT INTO badwords VALUES (42,'казино');
    """)
    old.commit()
    old.close()

    db = DB(path)
    core = Core(db)
    assert core.get(42, "warn_limit") == 5            # max_warns → warn_limit
    assert core.get(42, "antilink") == 1 and core.get(42, "warn_action") == "ban"
    assert core.words(42) == ["казино"]
    w = core.active_warns(42, 7)
    assert len(w) == 1 and w[0]["reason"] == "старый варн" and w[0]["expires_at"] == 0   # старый варн жив
    DB(path)                                           # повторная миграция безопасна
