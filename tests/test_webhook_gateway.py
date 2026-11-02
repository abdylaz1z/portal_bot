from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiohttp import ClientSession, web
from aiohttp.test_utils import TestServer
from app.main import validate_webhook_settings
from app.webhook_gateway import handle_webhook, select_worker, update_user_id


class FakeRedis:
    def __init__(self) -> None:
        self.owner: str | None = None

    async def zremrangebyscore(self, *args: object) -> None:
        return None

    async def zrangebyscore(self, *args: object) -> list[str]:
        return ["127.0.0.1"]

    async def get(self, key: str) -> str | None:
        return self.owner

    async def set(self, key: str, value: str, **kwargs: object) -> None:
        self.owner = value


class WebhookGatewayTests(unittest.TestCase):
    def test_webhook_settings_require_https_and_a_real_secret(self) -> None:
        valid = SimpleNamespace(
            bot_mode="webhook",
            webhook_public_url="https://bot.example.org",
            webhook_path="/telegram/webhook",
            webhook_secret="a-random-secret",
        )
        with patch("app.main.settings", valid):
            validate_webhook_settings()

        invalid = SimpleNamespace(
            bot_mode="webhook",
            webhook_public_url="http://bot.example.org",
            webhook_path="/telegram/webhook",
            webhook_secret="replace-with-a-random-url-safe-secret",
        )
        with patch("app.main.settings", invalid):
            with self.assertRaisesRegex(RuntimeError, "HTTPS"):
                validate_webhook_settings()

        invalid.webhook_public_url = "https://bot.example.org"
        with patch("app.main.settings", invalid):
            with self.assertRaisesRegex(RuntimeError, "Replace the WEBHOOK_SECRET"):
                validate_webhook_settings()

    def test_user_id_is_read_from_message_sender(self) -> None:
        update = {
            "update_id": 1,
            "message": {"from": {"id": 5431974131}, "text": "hello"},
        }

        self.assertEqual(update_user_id(update), 5431974131)

    def test_user_id_is_read_from_callback_sender(self) -> None:
        update = {
            "update_id": 2,
            "callback_query": {"from": {"id": 9876543210}},
        }

        self.assertEqual(update_user_id(update), 9876543210)

    def test_routing_is_stable_when_workers_are_added(self) -> None:
        workers = ["10.0.0.1", "10.0.0.2"]
        selected = select_worker(5431974131, workers)

        self.assertEqual(select_worker(5431974131, workers), selected)
        self.assertIn(selected, workers)
        added_worker = workers + ["10.0.0.3"]
        retained = sum(
            select_worker(user_id, workers)
            == select_worker(user_id, added_worker)
            for user_id in range(1000, 1100)
        )
        self.assertGreaterEqual(retained, 50)

    def test_missing_sender_uses_update_id_as_routing_key(self) -> None:
        self.assertIsNone(update_user_id({"update_id": 10, "poll": {"id": "p"}}))

    def test_gateway_forwards_updates_to_the_sticky_worker(self) -> None:
        async def run() -> None:
            received: list[dict] = []

            async def accept(request: web.Request) -> web.Response:
                received.append(await request.json())
                return web.json_response({"ok": True})

            worker_app = web.Application()
            worker_app.router.add_post("/telegram/webhook", accept)
            async with TestServer(worker_app) as worker:
                async with ClientSession() as client:
                    redis = FakeRedis()
                    request = SimpleNamespace(
                        headers={
                            "X-Telegram-Bot-Api-Secret-Token": "test-secret"
                        },
                        json=AsyncMock(
                            return_value={
                                "update_id": 3,
                                "message": {"from": {"id": 5431974131}},
                            }
                        ),
                        app={"redis": redis, "client": client},
                    )
                    gateway_settings = SimpleNamespace(
                        webhook_secret="test-secret",
                        webhook_worker_port=worker.port,
                        webhook_path="/telegram/webhook",
                    )
                    with patch("app.webhook_gateway.settings", gateway_settings):
                        response = await handle_webhook(request)

            self.assertEqual(response.status, 200)
            self.assertEqual(received[0]["update_id"], 3)
            self.assertEqual(redis.owner, "127.0.0.1")

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
