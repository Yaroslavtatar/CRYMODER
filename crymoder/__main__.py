"""Точка входа: один процесс — бот (модерация, автомодерация, капча, команды) и веб-панель."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import signal

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from . import __version__
from .config import load_config
from .core import Core
from .db import DB
from .handlers import captcha
from .handlers.events import build_dispatcher, maintenance_loop
from .utils import set_tz


def _make_server(app, host: str, port: int):
    """uvicorn без собственных обработчиков сигналов: иначе после остановки он повторно шлёт SIGTERM
    процессу, и корректное закрытие поллинга и базы не успевает выполниться."""
    import uvicorn

    class _Server(uvicorn.Server):
        @contextlib.contextmanager
        def capture_signals(self):           # uvicorn >= 0.29
            yield

        def install_signal_handlers(self):   # старые версии uvicorn
            pass

    return _Server(uvicorn.Config(app, host=host, port=port, log_level="warning"))


async def main() -> None:
    config = load_config()
    logging.basicConfig(level=config.log_level, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    log = logging.getLogger("crymoder")
    set_tz(config.tz_offset_hours)

    db = DB(config.db_path)
    bot = Bot(config.token, default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True))
    core = Core(db, bot, config)
    me = await bot.get_me()
    core.me_id, core.me_username = me.id, me.username or ""
    dp = build_dispatcher(core, config)
    log.info("CRYMODER %s запущен как @%s", __version__, me.username)

    await captcha.restore(bot, core)
    await bot.delete_webhook(drop_pending_updates=True)

    # SIGINT / SIGTERM (Ctrl+C, docker stop) обрабатываем сами — один раз для бота и панели
    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            asyncio.get_running_loop().add_signal_handler(sig, stop.set)
        except NotImplementedError:          # Windows
            pass

    tasks = [
        asyncio.create_task(dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types(),
                                             handle_signals=False), name="polling"),
        asyncio.create_task(maintenance_loop(core, bot), name="maintenance"),
    ]
    server = web_task = None
    if config.web_enabled:
        from .web.main import create_app
        server = _make_server(create_app(core), config.web_host, config.web_port)
        web_task = asyncio.create_task(server.serve(), name="web")
        tasks.append(web_task)
        log.info("Веб-панель: %s (порт %s)", config.public_url, config.web_port)

    stopper = asyncio.create_task(stop.wait(), name="stop")
    try:
        done, _ = await asyncio.wait([*tasks, stopper], return_when=asyncio.FIRST_COMPLETED)
        for t in done:
            if t is not stopper:
                t.result()                   # пробросить исключение упавшей задачи
        log.info("Останавливаюсь...")
    finally:
        if server:
            server.should_exit = True
            await asyncio.wait([web_task], timeout=5)   # дать панели закрыть соединения
        try:
            await dp.stop_polling()
        except RuntimeError:                 # поллинг уже остановлен
            pass
        for t in (*tasks, stopper):
            t.cancel()
        await asyncio.gather(*tasks, stopper, return_exceptions=True)
        await bot.session.close()
        db.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
