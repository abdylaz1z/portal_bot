from __future__ import annotations

import asyncio
import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from sqlalchemy import BigInteger

from app.database import (
    Database,
    PortalAccount,
    StudentProfile,
    validate_postgresql_url,
)
from app.handlers.portal import (
    CACHE_TTL_SECONDS,
    _refresh_section_cache,
    send_section,
)
from app.portal import PortalClient, PortalUnavailable


class ScheduleCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self.portal = PortalClient()

    def test_application_database_url_must_use_postgresql(self) -> None:
        validate_postgresql_url("postgresql+asyncpg://localhost/oshtu_bot")
        with self.assertRaisesRegex(RuntimeError, "должен указывать на PostgreSQL"):
            validate_postgresql_url("sqlite+aiosqlite:///:memory:")

    def test_telegram_ids_use_64_bit_database_columns(self) -> None:
        self.assertIsInstance(PortalAccount.__table__.c.telegram_id.type, BigInteger)
        self.assertIsInstance(StudentProfile.__table__.c.telegram_id.type, BigInteger)

    def test_schedule_cache_key_is_shared_by_week_and_day_views(self) -> None:
        with patch.object(PortalClient, "local_today", return_value=date(2026, 10, 29)):
            keys = {
                self.portal.persistent_cache_key(
                    42,
                    '{"cookies":[]}',
                    "schedule",
                    period,
                    institute="IT",
                    course=2,
                    group_name="A-1",
                )
                for period in ("week", "today", "yesterday", "friday")
            }
        self.assertEqual(len(keys), 1)

    def test_cached_day_is_selected_from_full_week_and_relabelled(self) -> None:
        schedule = "\n\n".join(
            (
                "Понедельник, 26.10.2026 · нечётная неделя (№44)\nПонедельникская пара",
                "Вторник, 27.10.2026 · нечётная неделя (№44)\nВторникская пара",
                "Среда, 28.10.2026 · нечётная неделя (№44)\nСредовая пара",
                "Четверг, 29.10.2026 · нечётная неделя (№44)\nЧетверговая пара",
                "Пятница, 30.10.2026 · нечётная неделя (№44)\nПятничная пара",
                "Суббота, 31.10.2026 · нечётная неделя (№44)\nСубботняя пара",
            )
        )
        with patch.object(PortalClient, "local_today", return_value=date(2026, 10, 29)):
            result = self.portal.refresh_cached_schedule_date(schedule, "today")

        self.assertIn("Четверг, 29.10.2026", result)
        self.assertIn("Четверговая пара", result)
        self.assertNotIn("Понедельникская пара", result)
        self.assertNotIn("Пятничная пара", result)

    def test_cache_check_does_not_rewrite_unchanged_data(self) -> None:
        async def run() -> None:
            db = Database("sqlite+aiosqlite:///:memory:")
            try:
                await db.initialize()
                await db.save_cached_response("schedule-key", "saved schedule")
                original = await db.get_cached_response("schedule-key")
                self.assertIsNotNone(original)
                assert original is not None

                await db.save_cached_response("schedule-key", "saved schedule")
                unchanged = await db.get_cached_response("schedule-key")
                self.assertIsNotNone(unchanged)
                assert unchanged is not None
                self.assertEqual(unchanged[0], original[0])
                self.assertEqual(unchanged[1], original[1])
                self.assertGreaterEqual(unchanged[2], original[2])

                await db.save_cached_response("schedule-key", "updated schedule")
                changed = await db.get_cached_response("schedule-key")
                self.assertIsNotNone(changed)
                assert changed is not None
                self.assertEqual(changed[0], "updated schedule")
                self.assertGreaterEqual(changed[1], unchanged[1])
            finally:
                await db.dispose()

        asyncio.run(run())

    def test_schedule_refresh_interval_is_one_week(self) -> None:
        self.assertEqual(CACHE_TTL_SECONDS["schedule"], 7 * 24 * 60 * 60)

    def test_captcha_capture_waits_for_image_to_finish_loading(self) -> None:
        async def run() -> None:
            locator = SimpleNamespace(
                evaluate=AsyncMock(side_effect=[False, False, True]),
                screenshot=AsyncMock(return_value=b"captcha-image"),
            )
            page = SimpleNamespace(wait_for_timeout=AsyncMock())
            portal = PortalClient()
            portal._first_visible = AsyncMock(return_value=locator)

            with patch(
                "app.portal.settings",
                SimpleNamespace(
                    parser_timeout_ms=1000,
                    captcha_image_selector="img.captcha",
                ),
            ):
                image = await portal._capture_captcha(page)

            self.assertEqual(image, b"captcha-image")
            self.assertEqual(locator.evaluate.await_count, 3)
            locator.screenshot.assert_awaited_once()

        asyncio.run(run())

    def test_existing_cache_table_is_migrated(self) -> None:
        async def run() -> None:
            db = Database("sqlite+aiosqlite:///:memory:")
            try:
                async with db.engine.begin() as connection:
                    await connection.exec_driver_sql(
                        "CREATE TABLE portal_response_cache ("
                        "cache_key VARCHAR(64) PRIMARY KEY, "
                        "response_text TEXT NOT NULL, "
                        "updated_at DATETIME NOT NULL)"
                    )
                    await connection.exec_driver_sql(
                        "INSERT INTO portal_response_cache "
                        "(cache_key, response_text, updated_at) "
                        "VALUES ('old-key', 'old schedule', '2026-10-01 00:00:00')"
                    )

                await db.initialize()
                cached = await db.get_cached_response("old-key")
                self.assertIsNotNone(cached)
                assert cached is not None
                self.assertEqual(cached[0], "old schedule")
                self.assertEqual(cached[1], cached[2])
            finally:
                await db.dispose()

        asyncio.run(run())

    def test_schedule_request_serves_cached_week_without_portal_fetch(self) -> None:
        async def run() -> None:
            db = Database("sqlite+aiosqlite:///:memory:")
            try:
                await db.initialize()
                await db.save_account(42, '{"cookies":[]}')
                await db.save_student_profile(42, "IT", "A-1", 2)
                key = self.portal.persistent_cache_key(
                    42,
                    '{"cookies":[]}',
                    "schedule",
                    "today",
                    institute="IT",
                    course=2,
                    group_name="A-1",
                )
                full_week = (
                    "Понедельник, 26.10.2026 · нечётная неделя (№44)\nПонедельникская пара\n\n"
                    "Четверг, 29.10.2026 · нечётная неделя (№44)\nЧетверговая пара"
                )
                await db.save_cached_response(key, full_week)

                portal = PortalClient()
                portal.fetch_section = AsyncMock()
                portal.create_background_task = Mock()
                loading = SimpleNamespace(edit_text=AsyncMock())
                message = SimpleNamespace(
                    from_user=SimpleNamespace(id=42),
                    answer=AsyncMock(return_value=loading),
                )
                with patch.object(
                    PortalClient, "local_today", return_value=date(2026, 10, 29)
                ):
                    await send_section(
                        message,
                        "schedule",
                        "Расписание",
                        db,
                        portal,
                        period="today",
                    )

                portal.fetch_section.assert_not_awaited()
                self.assertIn("Четверговая пара", message.answer.await_args_list[1].args[0])
                self.assertNotIn(
                    "Понедельникская пара", message.answer.await_args_list[1].args[0]
                )
            finally:
                await db.dispose()

        asyncio.run(run())

    def test_refresh_does_not_replace_schedule_when_lessons_are_unchanged(self) -> None:
        async def run() -> None:
            old_schedule = "Понедельник, 26.10.2026 · нечётная неделя (№44)\nПара 1"
            new_schedule = "Понедельник, 02.11.2026 · нечётная неделя (№45)\nПара 1"
            db = SimpleNamespace(
                save_cached_response=AsyncMock(),
                mark_cached_response_checked=AsyncMock(),
                mark_invalid=AsyncMock(),
            )
            portal = SimpleNamespace(
                fetch_section=AsyncMock(return_value=new_schedule),
                refresh_cached_schedule_week_dates=Mock(return_value=new_schedule),
                refresh_cached_schedule_date=Mock(return_value=new_schedule),
                week_info=PortalClient.week_info,
            )
            message = SimpleNamespace(answer=AsyncMock())

            await _refresh_section_cache(
                message,
                42,
                '{"cookies":[]}',
                "schedule",
                "week",
                "Расписание",
                "IT",
                2,
                "A-1",
                "cache-key",
                old_schedule,
                db,
                portal,
            )

            db.save_cached_response.assert_not_awaited()
            db.mark_cached_response_checked.assert_awaited_once_with("cache-key")
            message.answer.assert_not_awaited()

        asyncio.run(run())

    def test_refresh_replaces_and_notifies_when_schedule_changes(self) -> None:
        async def run() -> None:
            old_schedule = "Понедельник, 26.10.2026 · нечётная неделя (№44)\nПара 1"
            new_schedule = "Понедельник, 26.10.2026 · нечётная неделя (№44)\nНовая пара"
            db = SimpleNamespace(
                save_cached_response=AsyncMock(),
                mark_cached_response_checked=AsyncMock(),
                mark_invalid=AsyncMock(),
            )
            portal = SimpleNamespace(
                fetch_section=AsyncMock(return_value=new_schedule),
                refresh_cached_schedule_week_dates=Mock(return_value=new_schedule),
                refresh_cached_schedule_date=Mock(return_value=new_schedule),
                week_info=PortalClient.week_info,
            )
            message = SimpleNamespace(answer=AsyncMock())

            await _refresh_section_cache(
                message,
                42,
                '{"cookies":[]}',
                "schedule",
                "week",
                "Расписание",
                "IT",
                2,
                "A-1",
                "cache-key",
                old_schedule,
                db,
                portal,
            )

            db.save_cached_response.assert_awaited_once_with("cache-key", new_schedule)
            db.mark_cached_response_checked.assert_not_awaited()
            self.assertEqual(message.answer.await_count, 3)

        asyncio.run(run())

    def test_portal_outage_keeps_cached_schedule(self) -> None:
        async def run() -> None:
            db = SimpleNamespace(
                save_cached_response=AsyncMock(),
                mark_cached_response_checked=AsyncMock(),
                mark_invalid=AsyncMock(),
            )
            portal = SimpleNamespace(
                fetch_section=AsyncMock(side_effect=PortalUnavailable("offline")),
            )
            message = SimpleNamespace(answer=AsyncMock())

            await _refresh_section_cache(
                message,
                42,
                '{"cookies":[]}',
                "schedule",
                "today",
                "Расписание",
                "IT",
                2,
                "A-1",
                "cache-key",
                "saved schedule",
                db,
                portal,
            )

            db.save_cached_response.assert_not_awaited()
            db.mark_cached_response_checked.assert_awaited_once_with("cache-key")
            message.answer.assert_not_awaited()

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
