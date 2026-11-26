from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from . import __version__
from .config import load_config
from .db import Database
from .handlers import automod, captcha, common, moderation, settings


async def main() -> None:
    config = load_config()
    logging.basicConfig(
        level=config.log_level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    log = logging.getLogger("crymoder")

    db = Database(config.db_path)
    await db.connect()

    bot = Bot(config.token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(db=db, config=config)
    # Порядок важен: команды → настройки → капча → автомодерация (ловит всё остальное)
    dp.include_routers(common.router, moderation.router, settings.router, captcha.router, automod.router)

    me = await bot.get_me()
    log.info("CRYMODER %s запущен как @%s", __version__, me.username)

    await captcha.restore(bot, db)
    await bot.delete_webhook(drop_pending_updates=True)
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await bot.session.close()
        await db.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
