from __future__ import annotations

import asyncio
import hashlib
import hmac
import time

from aiohttp import ClientError, ClientSession, ClientTimeout, web
from loguru import logger
from redis.asyncio import Redis

from app.config import settings

WORKER_REGISTRY_KEY = "portal-bot:workers"
AFFINITY_TTL_SECONDS = 24 * 60 * 60
WORKER_HEARTBEAT_TTL_SECONDS = 30


def update_user_id(update: dict) -> int | None:
    for update_type in (
        "message",
        "edited_message",
        "channel_post",
        "edited_channel_post",
        "callback_query",
        "inline_query",
        "chosen_inline_result",
        "shipping_query",
        "pre_checkout_query",
        "poll_answer",
        "my_chat_member",
        "chat_member",
        "chat_join_request",
        "business_connection",
        "business_message",
        "edited_business_message",
        "deleted_business_messages",
    ):
        value = update.get(update_type)
        if not isinstance(value, dict):
            continue
        sender = value.get("from")
        if isinstance(sender, dict) and isinstance(sender.get("id"), int):
            return sender["id"]
        user = value.get("user")
        if isinstance(user, dict) and isinstance(user.get("id"), int):
            return user["id"]
    return None


def select_worker(user_id: int, workers: list[str]) -> str:
    if not workers:
        raise ValueError("At least one worker is required.")
    return max(
        workers,
        key=lambda worker: hashlib.sha256(
            f"{user_id}:{worker}".encode("utf-8")
        ).digest(),
    )


async def handle_webhook(request: web.Request) -> web.Response:
    expected_secret = settings.webhook_secret.encode("utf-8")
    supplied_secret = request.headers.get(
        "X-Telegram-Bot-Api-Secret-Token", ""
    ).encode("utf-8")
    if not hmac.compare_digest(supplied_secret, expected_secret):
        raise web.HTTPUnauthorized()

    try:
        update = await request.json()
    except (ValueError, web.HTTPBadRequest):
        raise web.HTTPBadRequest(text="Invalid Telegram update JSON.") from None
    if not isinstance(update, dict):
        raise web.HTTPBadRequest(text="Telegram update must be a JSON object.")

    redis: Redis = request.app["redis"]
    now = time.time()
    await redis.zremrangebyscore(
        WORKER_REGISTRY_KEY,
        "-inf",
        now - WORKER_HEARTBEAT_TTL_SECONDS,
    )
    workers = await redis.zrangebyscore(
        WORKER_REGISTRY_KEY,
        now - WORKER_HEARTBEAT_TTL_SECONDS,
        "+inf",
    )
    if not workers:
        raise web.HTTPServiceUnavailable(text="No bot workers are available.")

    user_id = update_user_id(update)
    if user_id is None:
        update_id = update.get("update_id")
        if not isinstance(update_id, int):
            raise web.HTTPBadRequest(text="Telegram update has no routing key.")
        user_id = update_id
    affinity_key = f"portal-bot:webhook-owner:{user_id}"
    worker = await redis.get(affinity_key)
    if worker not in workers:
        candidate = select_worker(user_id, workers)
        if worker is None:
            await redis.set(
                affinity_key,
                candidate,
                ex=AFFINITY_TTL_SECONDS,
                nx=True,
            )
        else:
            await redis.set(affinity_key, candidate, ex=AFFINITY_TTL_SECONDS)
        owner = await redis.get(affinity_key)
        worker = owner if owner in workers else candidate

    client: ClientSession = request.app["client"]
    worker_url = f"http://{worker}:{settings.webhook_worker_port}{settings.webhook_path}"
    try:
        async with client.post(
            worker_url,
            json=update,
            headers={"X-Telegram-Bot-Api-Secret-Token": settings.webhook_secret},
        ) as response:
            body = await response.read()
            return web.Response(
                body=body,
                status=response.status,
                content_type=response.content_type,
                headers={
                    key: value
                    for key, value in response.headers.items()
                    if key.lower() == "retry-after"
                },
            )
    except (asyncio.TimeoutError, ClientError):
        logger.exception("Could not forward Telegram update to worker {}", worker)
        raise web.HTTPServiceUnavailable(
            text="Bot worker is temporarily unavailable."
        ) from None


async def health(_: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})


async def create_app() -> web.Application:
    if not settings.webhook_secret:
        raise RuntimeError("WEBHOOK_SECRET must be configured for the webhook gateway.")
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    await redis.ping()
    app = web.Application(client_max_size=1024 * 1024)
    app["redis"] = redis
    app["client"] = ClientSession(timeout=ClientTimeout(total=55))
    app.router.add_post(settings.webhook_path, handle_webhook)
    app.router.add_get("/healthz", health)

    async def close_resources(application: web.Application) -> None:
        await application["client"].close()
        await application["redis"].aclose()

    app.on_cleanup.append(close_resources)
    return app


def main() -> None:
    web.run_app(create_app(), host="0.0.0.0", port=settings.webhook_gateway_port)


if __name__ == "__main__":
    main()
