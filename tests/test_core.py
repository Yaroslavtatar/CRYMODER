import asyncio

import pytest

from crymoder.db import Database
from crymoder.handlers.automod import find_bad_word, has_invite, is_flooding
from crymoder.utils import fmt_duration, parse_duration


def test_parse_duration():
    assert parse_duration("10m") == 600
    assert parse_duration("2h") == 7200
    assert parse_duration("1d") == 86400
    assert parse_duration("5s") == 30          # минимум 30 секунд
    assert parse_duration("спам") is None
    assert parse_duration("10") is None


def test_fmt_duration():
    assert fmt_duration(None) == "навсегда"
    assert fmt_duration(600) == "10 мин"
    assert fmt_duration(86400) == "1 д"


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


def test_database(tmp_path):
    async def run():
        db = Database(str(tmp_path / "t.db"))
        await db.connect()
        try:
            s = await db.get_settings(42)
            assert s["max_warns"] == 3 and s["captcha"] == 1

            await db.set_setting(42, "max_warns", 99)      # зажимается в диапазон
            assert (await db.get_settings(42))["max_warns"] == 10
            await db.set_setting(42, "antilink", 1)
            assert (await db.get_settings(42))["antilink"] == 1
            with pytest.raises(ValueError):
                await db.set_setting(42, "warn_action", "explode")

            assert await db.add_warn(42, 7, "a") == 1
            assert await db.add_warn(42, 7, "b") == 2
            assert await db.remove_last_warn(42, 7) == 1
            await db.reset_warns(42, 7)
            assert await db.warn_count(42, 7) == 0

            await db.add_word(42, "слово")
            assert await db.get_words(42) == ["слово"]
            assert await db.del_word(42, "слово") is True
            assert await db.get_words(42) == []

            await db.add_captcha(42, 7, 100, 123)
            assert (await db.get_captcha(42, 7))["message_id"] == 100
            await db.del_captcha(42, 7)
            assert await db.get_captcha(42, 7) is None
        finally:
            await db.close()

    asyncio.run(run())
