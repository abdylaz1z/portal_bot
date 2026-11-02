from __future__ import annotations

import asyncio
import os
import re
import socket
import sys
import time
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.redis import RedisEventIsolation, RedisStorage
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from aiohttp import web
from loguru import logger
from redis.asyncio import Redis
from sqlalchemy import text

from app.config import settings
from app.database import Database, validate_postgresql_url
from app.handlers import create_router
from app.keyboards import login_menu
from app.portal import PortalClient, PortalUnavailable


def validate_webhook_settings() -> None:
    if settings.bot_mode != "webhook":
        return
    public_url = urlsplit(settings.webhook_public_url)
    if public_url.scheme != "https" or not public_url.hostname or public_url.path:
        raise RuntimeError("WEBHOOK_PUBLIC_URL must be a public HTTPS URL.")
    if not settings.webhook_path.startswith("/"):
        raise RuntimeError("WEBHOOK_PATH must start with '/'.")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", settings.webhook_secret):
        raise RuntimeError(
            "WEBHOOK_SECRET must contain 1-256 ASCII letters, digits, '_' or '-'."
        )
    if settings.webhook_secret.startswith("replace-with-"):
        raise RuntimeError("Replace the WEBHOOK_SECRET example with a generated secret.")


async def validate_saved_sessions(db: Database, portal: PortalClient, bot: Bot) -> None:
    accounts = await db.list_valid_accounts()
    for account in accounts:
        try:
            valid = await portal.validate_session(account.storage_state)
        except PortalUnavailable:
            logger.warning("Skipped scheduled session validation because portal is unavailable")
            return
        except Exception:
            logger.exception("Scheduled session validation failed")
            continue
        if not valid:
            await db.mark_invalid(account.telegram_id)
            try:
                await bot.send_message(
                    account.telegram_id,
                    "Сессия портала завершилась. Нажмите «🔐 Войти в портал», чтобы войти снова.",
                    reply_markup=login_menu(),
                )
            except Exception:
                logger.warning("Could not notify a user about an expired portal session")


async def run() -> None:
    if not settings.bot_token:
        raise RuntimeError("Не задан BOT_TOKEN. Создайте .env по примеру .env.example.")
    if settings.bot_role not in {"worker", "scheduler"}:
        raise RuntimeError("BOT_ROLE must be either 'worker' or 'scheduler'.")
    if settings.bot_mode not in {"polling", "webhook"}:
        raise RuntimeError("BOT_MODE must be either 'polling' or 'webhook'.")
    if settings.bot_role == "worker":
        validate_webhook_settings()
    validate_postgresql_url(settings.database_url)
    os.umask(0o077)
    logger.remove()
    logger.add(sys.stderr, diagnose=False)
    if settings.bot_mode == "polling":
        os.makedirs("logs", exist_ok=True)
        logger.add(
            "logs/bot.log",
            rotation="10 MB",
            retention="7 days",
            enqueue=True,
            diagnose=False,
        )

    database = Database()
    await database.initialize()
    redis: Redis | None = None
    storage = None
    isolation = None
    if settings.bot_mode == "webhook" or settings.bot_role == "scheduler":
        redis = Redis.from_url(settings.redis_url, decode_responses=True)
        await redis.ping()
    scheduler_lock_connection = None
    if settings.bot_role == "scheduler":
        scheduler_lock_connection = await database.engine.connect()
        lock_acquired = await scheduler_lock_connection.scalar(
            text("SELECT pg_try_advisory_lock(738249102)")
        )
        await scheduler_lock_connection.commit()
        if not lock_acquired:
            await scheduler_lock_connection.close()
            raise RuntimeError(
                "Another scheduler instance already holds the PostgreSQL scheduler lock."
            )
    portal = PortalClient()
    await portal.start()
    bot = Bot(token=settings.bot_token)
    if settings.bot_mode == "webhook" and settings.bot_role == "worker":
        storage = RedisStorage.from_url(
            settings.redis_url,
            state_ttl=24 * 60 * 60,
            data_ttl=24 * 60 * 60,
        )
        isolation = RedisEventIsolation.from_url(settings.redis_url)
    dispatcher = Dispatcher(storage=storage, events_isolation=isolation)
    dispatcher.include_router(create_router())
    dispatcher["db"] = database
    dispatcher["portal"] = portal

    scheduler = AsyncIOScheduler(timezone=ZoneInfo("Asia/Bishkek"))
    if settings.bot_role == "scheduler":
        scheduler.add_job(
            validate_saved_sessions,
            "interval",
            minutes=30,
            args=[database, portal, bot],
            id="validate_portal_sessions",
            max_instances=1,
            coalesce=True,
        )

    async def clean_pending_logins() -> None:
        expired_ids = await portal.cleanup_expired_logins()
        for telegram_id in expired_ids:
            try:
                await bot.send_message(
                    telegram_id,
                    "Время ввода капчи истекло. Нажмите «🔐 Войти в портал», чтобы начать заново.",
                    reply_markup=login_menu(),
                )
            except Exception:
                logger.warning("Could not notify a user about an expired login attempt")

    if settings.bot_role == "worker":
        scheduler.add_job(
            clean_pending_logins,
            "interval",
            minutes=2,
            id="cleanup_pending_logins",
            max_instances=1,
            coalesce=True,
        )
    scheduler.start()
    registry_task: asyncio.Task | None = None
    runner: web.AppRunner | None = None
    if settings.bot_role == "worker" and settings.bot_mode == "webhook":
        assert redis is not None
        worker_ip = socket.gethostbyname(socket.gethostname())
        await redis.zadd("portal-bot:workers", {worker_ip: time.time()})

        async def register_worker() -> None:
            while True:
                try:
                    await redis.zadd("portal-bot:workers", {worker_ip: time.time()})
                except Exception:
                    logger.exception("Could not register webhook worker in Redis")
                await asyncio.sleep(10)

        registry_task = asyncio.create_task(register_worker())

        async def health(_: web.Request) -> web.Response:
            await redis.ping()
            return web.json_response({"status": "ok"})

        app = web.Application()
        SimpleRequestHandler(
            dispatcher,
            bot,
            secret_token=settings.webhook_secret,
            handle_in_background=False,
        ).register(app, path=settings.webhook_path)
        app.router.add_get("/healthz", health)
        setup_application(app, dispatcher, bot=bot)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", settings.webhook_worker_port)
        await site.start()
        await bot.set_webhook(
            url=f"{settings.webhook_public_url}{settings.webhook_path}",
            secret_token=settings.webhook_secret,
            max_connections=settings.webhook_max_connections,
            allowed_updates=dispatcher.resolve_used_update_types(),
        )
        logger.info("Telegram webhook worker started at {}", settings.webhook_path)
    elif settings.bot_role == "scheduler":
        logger.info("Portal session scheduler started")
    else:
        logger.info("ОшТУ bot started in polling mode")
    try:
        if settings.bot_role == "worker" and settings.bot_mode == "polling":
            await dispatcher.start_polling(
                bot,
                allowed_updates=dispatcher.resolve_used_update_types(),
            )
        else:
            await asyncio.Event().wait()
    finally:
        scheduler.shutdown(wait=False)
        if registry_task is not None:
            registry_task.cancel()
            await asyncio.gather(registry_task, return_exceptions=True)
            if redis is not None:
                await redis.zrem("portal-bot:workers", worker_ip)
        if runner is not None:
            await runner.cleanup()
        await portal.close()
        await bot.session.close()
        if storage is not None:
            await storage.close()
        if isolation is not None:
            await isolation.close()
        if redis is not None:
            await redis.aclose()
        if scheduler_lock_connection is not None:
            await scheduler_lock_connection.execute(
                text("SELECT pg_advisory_unlock(738249102)")
            )
            await scheduler_lock_connection.commit()
            await scheduler_lock_connection.close()
        await database.dispose()


if __name__ == "__main__":
    asyncio.run(run())
