"""Запуск: Telegram-бот + веб-панель в одном процессе."""
import asyncio
import logging
import sys

import uvicorn

from app import bot as botmod
from app import config
from app.core import Core
from app.db import DB
from app.web.main import create_app


async def run() -> None:
    if not config.BOT_TOKEN:
        sys.exit("Не задан BOT_TOKEN. Скопируйте .env.example в .env и впишите токен от @BotFather.")
    db = DB(config.DB_PATH)
    bot = botmod.make_bot(config.BOT_TOKEN)
    core = Core(db, bot)
    me = await bot.get_me()
    core.me_id, core.me_username = me.id, me.username or ""
    dp = botmod.build_dispatcher(core)
    app = create_app(core)
    server = uvicorn.Server(uvicorn.Config(app, host=config.WEB_HOST, port=config.WEB_PORT, log_level="info"))
    logging.info("Бот @%s запущен, панель: %s", me.username, config.PUBLIC_URL)
    await bot.delete_webhook(drop_pending_updates=False)
    await asyncio.gather(
        dp.start_polling(bot, allowed_updates=["message", "chat_member", "my_chat_member", "chat_join_request",
                                               "callback_query"], handle_signals=False),
        server.serve(),
        botmod.maintenance_loop(core, bot),
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
