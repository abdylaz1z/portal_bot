from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from collections.abc import Coroutine
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo

from loguru import logger
from playwright.async_api import (
    Browser,
    BrowserContext,
    Locator,
    Page,
    Route,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

from app.config import settings


class PortalError(Exception):
    """Base exception for portal access and parsing errors."""


class PortalUnavailable(PortalError):
    pass


class PortalLayoutError(PortalError):
    pass


class SessionExpired(PortalError):
    pass


@dataclass
class PendingLogin:
    context: BrowserContext
    page: Page
    username: str
    password: str
    started_at: float = field(default_factory=time.monotonic)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


@dataclass
class CaptchaRetry:
    image: bytes


@dataclass
class LoginRejected:
    reason: Literal["credentials"] = "credentials"


@dataclass
class LoginSucceeded:
    storage_state: str


class PortalClient:
    ROUTES = {
        "schedule": "#/schedule",
        "teachers": "#/student/my-teachers",
        "rating": "#/student/my-rating",
    }
    @staticmethod
    def local_today() -> date:
        return datetime.now(ZoneInfo("Asia/Bishkek")).date()

    def __init__(self) -> None:
        self._playwright = None
        self._browser: Browser | None = None
        self._pending: dict[int, PendingLogin] = {}
        self._response_cache: dict[tuple, tuple[float, str]] = {}
        self._response_cache_lock = asyncio.Lock()
        self._background_tasks: set[asyncio.Task] = set()

    def create_background_task(
        self, coroutine: Coroutine[Any, Any, None]
    ) -> asyncio.Task:
        task = asyncio.create_task(coroutine)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return task

    async def _get_cached_response(self, key: tuple) -> str | None:
        async with self._response_cache_lock:
            cached = self._response_cache.get(key)
            if cached is None:
                return None
            expires_at, text = cached
            if expires_at <= time.monotonic():
                self._response_cache.pop(key, None)
                return None
            return text

    async def _cache_response(self, key: tuple, text: str, ttl_seconds: int) -> None:
        async with self._response_cache_lock:
            now = time.monotonic()
            expired_keys = [
                cached_key
                for cached_key, (expires_at, _) in self._response_cache.items()
                if expires_at <= now
            ]
            for cached_key in expired_keys:
                self._response_cache.pop(cached_key, None)
            if len(self._response_cache) >= 256 and key not in self._response_cache:
                oldest_key = min(
                    self._response_cache,
                    key=lambda cached_key: self._response_cache[cached_key][0],
                )
                self._response_cache.pop(oldest_key, None)
            self._response_cache[key] = (now + ttl_seconds, text)

    def persistent_cache_key(
        self,
        telegram_id: int,
        storage_state: str,
        section: str,
        period: str | None = None,
        institute: str | None = None,
        course: int | None = None,
        group_name: str | None = None,
    ) -> str:
        today = self.local_today()
        if section == "schedule":
            date_scope = None
            cache_period = "week"
        else:
            date_scope = None
            cache_period = period

        identity = {
            "telegram_id": telegram_id,
            "session": hashlib.sha256(storage_state.encode("utf-8")).hexdigest(),
            "section": section,
            "period": cache_period,
            "institute": institute,
            "course": course,
            "group": group_name,
            "date_scope": date_scope,
        }
        serialized_identity = json.dumps(
            identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        return hashlib.sha256(serialized_identity.encode("utf-8")).hexdigest()

    @staticmethod
    def select_cached_schedule_day(text: str, period: str | None) -> str:
        schedule_days = {
            "monday": "Понедельник",
            "tuesday": "Вторник",
            "wednesday": "Среда",
            "thursday": "Четверг",
            "friday": "Пятница",
            "saturday": "Суббота",
        }
        if period not in schedule_days:
            return text
        lines = text.splitlines()
        heading_indexes = [
            index
            for index, line in enumerate(lines)
            if " · " in line and " неделя (№" in line
        ]
        day_name = schedule_days[period]
        start = next(
            (
                index
                for index in heading_indexes
                if lines[index].startswith(f"{day_name},")
            ),
            None,
        )
        if start is None:
            return f"{day_name}: расписание на этот день не найдено."
        end = next(
            (index for index in heading_indexes if index > start),
            len(lines),
        )
        prefix = lines[: heading_indexes[0]]
        return "\n".join([*prefix, *lines[start:end]]).strip()

    def refresh_cached_schedule_date(self, text: str, period: str | None) -> str:
        relative_days = {"yesterday": -1, "today": 0, "tomorrow": 1}
        if period in relative_days:
            target_date = self.local_today() + timedelta(days=relative_days[period])
        else:
            weekdays = {
                "monday": 0,
                "tuesday": 1,
                "wednesday": 2,
                "thursday": 3,
                "friday": 4,
                "saturday": 5,
            }
            if period not in weekdays:
                return text
            target_date = self.local_today() - timedelta(days=self.local_today().weekday())
            target_date += timedelta(days=weekdays[period])

        if target_date.weekday() > 5:
            return "В воскресенье занятий нет."
        weekday = target_date.strftime("%A").lower()
        selected = self.select_cached_schedule_day(text, weekday)
        week_number, parity = self.week_info(target_date)
        lines = selected.splitlines()
        for index, line in enumerate(lines):
            if " · " in line and " неделя (№" in line:
                day_name = line.split(",", maxsplit=1)[0]
                lines[index] = (
                    f"{day_name}, {target_date.strftime('%d.%m.%Y')} · "
                    f"{parity} неделя (№{week_number})"
                )
                break
        return "\n".join(lines)

    def refresh_cached_schedule_week_dates(self, text: str) -> str:
        weekdays = {
            "Понедельник": 0,
            "Вторник": 1,
            "Среда": 2,
            "Четверг": 3,
            "Пятница": 4,
            "Суббота": 5,
        }
        today = self.local_today()
        monday = today - timedelta(days=today.weekday())
        lines = text.splitlines()
        for index, line in enumerate(lines):
            if " · " not in line or " неделя (№" not in line:
                continue
            day_name = line.split(",", maxsplit=1)[0]
            if day_name not in weekdays:
                continue
            target_date = monday + timedelta(days=weekdays[day_name])
            week_number, parity = self.week_info(target_date)
            lines[index] = (
                f"{day_name}, {target_date.strftime('%d.%m.%Y')} · "
                f"{parity} неделя (№{week_number})"
            )
        return "\n".join(lines)

    async def start(self) -> None:
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=settings.headless_mode
        )

    async def close(self) -> None:
        background_tasks = list(self._background_tasks)
        for task in background_tasks:
            task.cancel()
        if background_tasks:
            await asyncio.gather(*background_tasks, return_exceptions=True)
        for user_id in list(self._pending):
            await self.cancel_login(user_id)
        if self._browser is not None:
            await self._browser.close()
            self._browser = None
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None

    def _require_browser(self) -> Browser:
        if self._browser is None:
            raise PortalUnavailable("Браузерный сервис ещё не запущен")
        return self._browser

    async def _new_context(
        self, storage_state: dict[str, Any] | None = None
    ) -> BrowserContext:
        browser = self._require_browser()
        context = await browser.new_context(storage_state=storage_state)
        await context.route("**/*", self._route_nonessential_requests)
        return context

    @staticmethod
    async def _route_nonessential_requests(route: Route) -> None:
        request = route.request
        purpose = " ".join(
            (
                request.headers.get("purpose", ""),
                request.headers.get("sec-purpose", ""),
            )
        ).casefold()
        if (
            request.url.startswith("https://www.googletagmanager.com/")
            or "prefetch" in purpose
        ):
            await route.abort()
            return
        await route.continue_()

    async def _first_visible(self, page: Page, selectors: str) -> Locator | None:
        for selector in (part.strip() for part in selectors.split(",") if part.strip()):
            try:
                locator = page.locator(selector).first
                if await locator.count() and await locator.is_visible():
                    return locator
            except Exception:
                continue
        return None

    async def _fill_login_form(self, pending: PendingLogin) -> None:
        deadline = time.monotonic() + settings.parser_timeout_ms / 1000
        username = None
        password = None
        while time.monotonic() < deadline:
            username = await self._first_visible(pending.page, settings.login_user_selector)
            password = await self._first_visible(pending.page, settings.login_password_selector)
            if username is not None and password is not None:
                break
            await pending.page.wait_for_timeout(250)
        if username is None or password is None:
            raise PortalLayoutError("На странице портала не найдены поля входа")
        await username.fill(pending.username)
        await password.fill(pending.password)

    async def _capture_captcha(self, page: Page) -> bytes:
        try:
            deadline = time.monotonic() + settings.parser_timeout_ms / 1000
            while time.monotonic() < deadline:
                locator = await self._first_visible(page, settings.captcha_image_selector)
                if locator is not None:
                    try:
                        loaded = await locator.evaluate(
                            "image => image.complete && image.naturalWidth > 0"
                        )
                    except Exception:
                        loaded = False
                    if loaded:
                        return await locator.screenshot(
                            type="png", timeout=settings.parser_timeout_ms
                        )
                await page.wait_for_timeout(250)
            raise PortalLayoutError("Изображение капчи не загрузилось вовремя")
        except Exception as exc:
            if isinstance(exc, PortalLayoutError):
                raise
            raise PortalLayoutError("Не удалось найти изображение капчи на портале") from exc

    async def begin_login(self, telegram_id: int, username: str, password: str) -> bytes:
        await self.cancel_login(telegram_id)
        try:
            context = await self._new_context()
            page = await context.new_page()
            page.set_default_timeout(settings.parser_timeout_ms)
            await page.goto(
                settings.portal_url,
                wait_until="commit",
                timeout=settings.parser_timeout_ms,
            )
            pending = PendingLogin(context=context, page=page, username=username, password=password)
            await self._fill_login_form(pending)
            image = await self._capture_captcha(page)
            self._pending[telegram_id] = pending
            return image
        except Exception as exc:
            if "context" in locals():
                await context.close()
            if isinstance(exc, PortalError):
                raise
            raise PortalUnavailable("Не удалось открыть портал ОшТУ") from exc

    async def submit_captcha(
        self, telegram_id: int, code: str
    ) -> CaptchaRetry | LoginRejected | LoginSucceeded:
        pending = self._pending.get(telegram_id)
        if pending is None:
            raise PortalError("Время авторизации истекло. Начните вход заново кнопкой «Войти в портал».")
        async with pending.lock:
            pending.started_at = time.monotonic()
            try:
                captcha = await self._first_visible(
                    pending.page, settings.captcha_input_selector
                )
                submit = await self._first_visible(
                    pending.page, settings.login_submit_selector
                )
                if captcha is None or submit is None:
                    raise PortalLayoutError("Не найдены поле капчи или кнопка входа")
                await captcha.fill(code.strip())
                await submit.click()
                deadline = time.monotonic() + 8
                body = ""
                form_visible = False
                while time.monotonic() < deadline:
                    await pending.page.wait_for_timeout(400)
                    body = (await pending.page.locator("body").inner_text()).casefold()
                    if self._credentials_error(body):
                        await self.cancel_login(telegram_id)
                        return LoginRejected()
                    if self._captcha_error(body):
                        break

                    user_field = await self._first_visible(
                        pending.page, settings.login_user_selector
                    )
                    password_field = await self._first_visible(
                        pending.page, settings.login_password_selector
                    )
                    form_visible = user_field is not None or password_field is not None
                    if not form_visible:
                        await pending.page.wait_for_timeout(800)
                        break

                # Some versions of the portal leave the login form visible while processing.
                # Validate the stored cookies against a protected page before deciding to retry.
                state = await pending.context.storage_state()
                serialized_state = json.dumps(state, ensure_ascii=False)
                if await self.validate_session(serialized_state):
                    await self.cancel_login(telegram_id)
                    return LoginSucceeded(storage_state=serialized_state)

                logger.info(
                    "Portal did not confirm login: form_visible={}, captcha_error_detected={}",
                    form_visible,
                    self._captcha_error(body),
                )

                # Start a fresh browser context if the portal still shows its login form.
                # Some portal responses leave a partially rendered page that cannot be reloaded
                # reliably, so reuse the in-memory credentials and open the form from scratch.
                image = await self.begin_login(
                    telegram_id, pending.username, pending.password
                )
                return CaptchaRetry(image=image)
            except Exception as exc:
                await self.cancel_login(telegram_id)
                if isinstance(exc, PortalError):
                    raise
                raise PortalUnavailable("Не удалось обработать ответ портала") from exc

    @staticmethod
    def semester_label(on_date: date | None = None) -> str:
        today = on_date or PortalClient.local_today()
        # As requested: autumn is selected from September through October 1;
        # the spring semester is selected on all other dates.
        is_autumn = today.month == 9 or (today.month == 10 and today.day == 1)
        return "Осенний" if is_autumn else "Весенний"

    @staticmethod
    def week_info(on_date: date | None = None) -> tuple[int, str]:
        week_number = (on_date or PortalClient.local_today()).isocalendar().week
        parity = "чётная" if week_number % 2 == 0 else "нечётная"
        return week_number, parity

    @staticmethod
    def _normalize_option(value: str) -> str:
        return re.sub(r"\s+", " ", value).strip().casefold()

    async def _select_option(self, page: Page, label: str, requested: str) -> None:
        select = page.get_by_label(label, exact=True)
        target = self._normalize_option(requested)
        deadline = time.monotonic() + min(8, settings.parser_timeout_ms / 1000)
        while time.monotonic() < deadline:
            try:
                options = await select.locator("option").evaluate_all(
                    "items => items.map(item => ({label: (item.textContent || '').trim(), value: item.value}))"
                )
            except Exception:
                options = []
            match = next(
                (
                    item
                    for item in options
                    if self._normalize_option(item["label"]) == target
                ),
                None,
            )
            if match is not None:
                await select.select_option(value=match["value"])
                return
            await page.wait_for_timeout(250)
        raise PortalLayoutError(f"В списке «{label}» нет выбранного значения")

    async def _select_semester(self, page: Page, semester: str | None = None) -> None:
        await self._select_option(page, "Семестр", semester or self.semester_label())

    async def _select_schedule_profile(
        self,
        page: Page,
        institute: str | None,
        course: int | None,
        group_name: str | None,
        semester: str | None = None,
    ) -> None:
        await self._select_semester(page, semester)
        if institute is None or course is None or group_name is None:
            return
        await self._select_option(page, "Факультет/Институт", institute)
        await self._select_option(page, "Курс", f"{course} курс")
        await self._select_option(page, "Группа", group_name)

    async def schedule_filter_options(
        self,
        storage_state: str,
        institute: str | None = None,
        course: int | None = None,
    ) -> dict[str, list[str]]:
        context = None
        try:
            context = await self._new_context(storage_state=json.loads(storage_state))
            page = await context.new_page()
            page.set_default_timeout(settings.parser_timeout_ms)
            await page.goto(
                settings.portal_url + self.ROUTES["schedule"],
                wait_until="commit",
                timeout=settings.parser_timeout_ms,
            )
            if await self._is_login_page(page):
                raise SessionExpired("Сессия портала завершилась")
            await self._select_semester(page)

            async def options_for(label: str, wait_for_values: bool = False) -> list[str]:
                select = page.get_by_label(label, exact=True)
                deadline = time.monotonic() + min(8, settings.parser_timeout_ms / 1000)
                while True:
                    raw = await select.locator("option").evaluate_all(
                        "items => items.map(item => (item.textContent || '').trim())"
                    )
                    values = [
                        value
                        for value in raw
                        if value
                        and not self._normalize_option(value).startswith("выберите")
                    ]
                    if values or not wait_for_values or time.monotonic() >= deadline:
                        return values
                    await page.wait_for_timeout(250)

            institutes = await options_for("Факультет/Институт", wait_for_values=True)
            if institute is not None:
                await self._select_option(page, "Факультет/Институт", institute)
            courses = await options_for("Курс", wait_for_values=True)
            groups: list[str] = []
            if course is not None:
                await self._select_option(page, "Курс", f"{course} курс")
                deadline = time.monotonic() + 8
                while time.monotonic() < deadline:
                    groups = await options_for("Группа")
                    if groups:
                        break
                    await page.wait_for_timeout(300)
            return {"institutes": institutes, "courses": courses, "groups": groups}
        except (SessionExpired, PortalLayoutError, PortalUnavailable):
            raise
        except Exception as exc:
            raise PortalUnavailable("Не удалось загрузить фильтры расписания") from exc
        finally:
            if context is not None:
                await context.close()

    @staticmethod
    def _credentials_error(body: str) -> bool:
        normalized = re.sub(r"\s+", " ", body)
        credential_phrases = (
            "неверный логин",
            "неверный пароль",
            "неправильный логин",
            "неправильный пароль",
            "неверные учетные данные",
            "неправильные учетные данные",
            "неверно указаны логин и пароль",
            "логин или пароль введены неверно",
            "invalid username",
            "incorrect username",
            "invalid password",
            "incorrect password",
            "invalid credentials",
            "authentication failed",
            "login or password is incorrect",
        )
        return any(phrase in normalized for phrase in credential_phrases)

    @staticmethod
    def _captcha_error(body: str) -> bool:
        normalized = re.sub(r"\s+", " ", body)
        captcha_phrases = (
            "неверный код",
            "неправильный код",
            "ошибка капчи",
            "код с картинки невер",
            "код введен неверно",
            "captcha is invalid",
            "invalid captcha",
            "incorrect captcha",
        )
        return any(phrase in normalized for phrase in captcha_phrases)

    async def cancel_login(self, telegram_id: int) -> None:
        pending = self._pending.pop(telegram_id, None)
        if pending is not None:
            try:
                await pending.context.close()
            except Exception:
                pass

    async def cleanup_expired_logins(self, max_age_seconds: int = 900) -> list[int]:
        expired = [
            user_id
            for user_id, pending in self._pending.items()
            if time.monotonic() - pending.started_at > max_age_seconds
        ]
        for user_id in expired:
            await self.cancel_login(user_id)
        return expired

    async def validate_session(self, storage_state: str) -> bool:
        context = None
        try:
            context = await self._new_context(storage_state=json.loads(storage_state))
            page = await context.new_page()
            page.set_default_timeout(settings.parser_timeout_ms)
            await page.goto(
                settings.portal_url + self.ROUTES["schedule"],
                wait_until="commit",
                timeout=settings.parser_timeout_ms,
            )
            return not await self._is_login_page(page)
        except Exception as exc:
            if isinstance(exc, PortalError):
                raise
            raise PortalUnavailable("Портал временно недоступен") from exc
        finally:
            if context is not None:
                await context.close()

    async def fetch_section(
        self,
        storage_state: str,
        section: str,
        period: str | None = None,
        institute: str | None = None,
        course: int | None = None,
        group_name: str | None = None,
        *,
        force_refresh: bool = False,
    ) -> str:
        cache_key = (
            hashlib.sha256(storage_state.encode("utf-8")).hexdigest(),
            section,
            "week" if section == "schedule" else period,
            institute,
            course,
            group_name,
            None,
        )
        if not force_refresh:
            cached_text = await self._get_cached_response(cache_key)
            if cached_text is not None:
                logger.info("Portal section cache hit: section={}", section)
                return cached_text

        route = self.ROUTES.get(section)
        if route is None:
            raise PortalLayoutError("Неизвестный раздел портала")
        context = None
        started_at = time.monotonic()
        navigation_ms = 0
        try:
            context = await self._new_context(storage_state=json.loads(storage_state))
            page = await context.new_page()
            page.set_default_timeout(settings.parser_timeout_ms)
            navigation_started_at = time.monotonic()
            await page.goto(
                settings.portal_url + route,
                wait_until="commit",
                timeout=settings.parser_timeout_ms,
            )
            navigation_ms = round((time.monotonic() - navigation_started_at) * 1000)
            if await self._is_login_page(page):
                raise SessionExpired("Сессия портала завершилась")

            if section == "schedule":
                requested_semester = self.semester_label()
                await self._select_schedule_profile(
                    page, institute, course, group_name, requested_semester
                )
                active_semester = requested_semester
                if not await self._schedule_has_lessons(page):
                    alternative_semester = (
                        "Весенний" if requested_semester == "Осенний" else "Осенний"
                    )
                    try:
                        await self._select_schedule_profile(
                            page, institute, course, group_name, alternative_semester
                        )
                        if await self._schedule_has_lessons(page):
                            active_semester = alternative_semester
                    except PortalLayoutError:
                        pass
                text = await self._extract_schedule(page, period)
                if active_semester != requested_semester and "не найдено" not in text:
                    text = (
                        f"По дате выбран семестр «{requested_semester}», но расписание "
                        f"на портале заполнено в семестре «{active_semester}».\n\n{text}"
                    )
            else:
                selector = {
                    "teachers": settings.teachers_rows_selector,
                    "rating": settings.rating_rows_selector,
                }[section]
                text = await self._extract_text(page, selector, section)
            if not text:
                raise PortalLayoutError("На странице раздела не найдены данные")
            ttl_seconds = 30 if section == "schedule" else 180
            await self._cache_response(cache_key, text, ttl_seconds)
            return text
        except (SessionExpired, PortalLayoutError, PortalUnavailable):
            raise
        except Exception as exc:
            raise PortalUnavailable("Портал временно недоступен") from exc
        finally:
            logger.info(
                "Portal request timing: section={} total_ms={} navigation_ms={}",
                section,
                round((time.monotonic() - started_at) * 1000),
                navigation_ms,
            )
            if context is not None:
                await context.close()

    async def _login_form_visible(self, page: Page) -> bool:
        username = await self._first_visible(page, settings.login_user_selector)
        password = await self._first_visible(page, settings.login_password_selector)
        return username is not None and password is not None

    async def _is_login_page(self, page: Page) -> bool:
        try:
            await page.wait_for_function(
                """selectors => {
                    const visible = selector => [...document.querySelectorAll(selector)]
                        .some(element => element.getClientRects().length > 0);
                    const body = (document.body?.innerText || '').toLocaleLowerCase();
                    const loginMessage = body.includes('авторизуйтесь для входа')
                        && body.includes('войти в систему');
                    const scheduleButton = [...document.querySelectorAll('button')]
                        .some(button => button.getClientRects().length > 0
                            && button.innerText.trim() === 'Расписание');
                    return (visible(selectors[0]) && visible(selectors[1]))
                        || loginMessage
                        || scheduleButton
                        || visible('[aria-label="Семестр"]')
                        || visible('.c-app main.c-main');
                }""",
                arg=[settings.login_user_selector, settings.login_password_selector],
                timeout=settings.parser_timeout_ms,
            )
        except PlaywrightTimeoutError as exc:
            raise PortalUnavailable(
                "Портал не успел загрузить страницу для проверки сессии"
            ) from exc

        if await self._login_form_visible(page):
            return True
        try:
            body = (await page.locator("body").inner_text()).casefold()
        except Exception:
            body = ""
        return "авторизуйтесь для входа" in body and "войти в систему" in body

    async def _schedule_has_lessons(self, page: Page) -> bool:
        deadline = time.monotonic() + min(4, settings.parser_timeout_ms / 1000)
        while time.monotonic() < deadline:
            try:
                has_lessons = await page.locator("td.td-week-day").evaluate_all(
                    "cells => cells.some(cell => (cell.innerText || '').trim().length > 0)"
                )
                if has_lessons:
                    return True
            except Exception:
                pass
            await page.wait_for_timeout(250)
        return False

    async def _extract_schedule(self, page: Page, period: str | None) -> str:
        """Read the portal's weekday-by-pair matrix and select its week variant."""
        today = self.local_today()
        weekday_indexes = {
            "monday": 0,
            "tuesday": 1,
            "wednesday": 2,
            "thursday": 3,
            "friday": 4,
            "saturday": 5,
        }
        if period in weekday_indexes:
            first_day = today - timedelta(days=today.weekday())
            day_indexes = [weekday_indexes[period]]
        elif period in {"yesterday", "today", "tomorrow"}:
            day_offset = {"yesterday": -1, "today": 0, "tomorrow": 1}[period]
            first_day = today + timedelta(days=day_offset)
            day_indexes = [first_day.weekday()]
        else:
            monday = today - timedelta(days=today.weekday())
            if period == "next_week":
                monday += timedelta(days=7)
            first_day = monday
            day_indexes = list(range(6))

        deadline = time.monotonic() + min(5, settings.parser_timeout_ms / 1000)
        table_data: dict | None = None
        script = '''() => {
          const table = [...document.querySelectorAll('table')]
            .find(item => item.querySelector('td.td-week-day'));
          if (!table) return null;
          const rows = [...table.rows];
          const headers = rows.length ? [...rows[0].cells].map(cell => (cell.innerText || '').trim()) : [];
          const scheduleRows = rows.slice(1).map(row => ({
            pair: (row.cells[0]?.innerText || '').trim(),
            cells: [...row.cells].slice(1).map(cell => {
              const blocks = [''];
              const append = node => {
                if (node.nodeType === Node.TEXT_NODE) blocks[blocks.length - 1] += node.nodeValue || '';
                else if (node.nodeType === Node.ELEMENT_NODE && node.tagName === 'HR') blocks.push('');
                else if (node.nodeType === Node.ELEMENT_NODE && node.tagName === 'BR') blocks[blocks.length - 1] += '\\n';
                else if (node.childNodes) [...node.childNodes].forEach(append);
              };
              [...cell.childNodes].forEach(append);
              return blocks;
            })
          }));
          return {headers, rows: scheduleRows};
        }'''
        while time.monotonic() < deadline:
            try:
                table_data = await page.evaluate(script)
                if table_data and any(
                    block.strip()
                    for row in table_data["rows"]
                    for cell in row["cells"]
                    for block in cell
                ):
                    break
            except Exception:
                table_data = None
            await page.wait_for_timeout(250)

        if not table_data:
            raise PortalLayoutError("Не найдена таблица расписания портала")
        if not any(
            block.strip()
            for row in table_data["rows"]
            for cell in row["cells"]
            for block in cell
        ):
            return "В выбранном семестре расписание для этой группы не найдено."

        headers = table_data["headers"]
        days = ("Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота")
        abbreviations = {
            "пнд": "Понедельник",
            "втн": "Вторник",
            "срд": "Среда",
            "чтв": "Четверг",
            "птн": "Пятница",
            "сбт": "Суббота",
            "вс": "Воскресенье",
        }
        output: list[str] = []
        for day_index in day_indexes:
            target_date = first_day + timedelta(
                days=day_index
                if period not in {"yesterday", "today", "tomorrow"}
                else 0
            )
            week_number, parity = self.week_info(target_date)
            header_index = day_index + 1
            day_header = headers[header_index].casefold() if header_index < len(headers) else ""
            day_name = abbreviations.get(day_header, days[day_index] if day_index < len(days) else "Воскресенье")
            # The upper table block is used this (even-numbered) week; the lower
            # block is used next (odd-numbered) week.
            week_block = 0 if parity == "чётная" else 1
            lessons: list[str] = []

            for row_index, row in enumerate(table_data["rows"], start=1):
                cells = row["cells"]
                if day_index >= len(cells):
                    continue
                blocks = [
                    "\n".join(
                        cleaned
                        for cleaned in (
                            re.sub(r"\s+", " ", line).strip()
                            for line in block.splitlines()
                        )
                        if cleaned
                    )
                    for block in cells[day_index]
                ]
                if not blocks:
                    continue
                lesson = blocks[week_block] if len(blocks) > 1 and week_block < len(blocks) else blocks[0]
                if not lesson:
                    continue
                pair = row["pair"].strip() or str(row_index)
                lessons.append(f"{pair}-я пара:\n{lesson}")

            output.append(
                f"{day_name}, {target_date.strftime('%d.%m.%Y')} · "
                f"{parity} неделя (№{week_number})\n"
                + ("\n\n".join(lessons) if lessons else "Занятий нет.")
            )
        return "\n\n".join(output)

    async def _extract_text(self, page: Page, selector: str, section: str) -> str:
        collected: list[str] = []
        try:
            rows = page.locator(selector)
            deadline = time.monotonic() + min(10, settings.parser_timeout_ms / 1000)
            while await rows.count() == 0 and time.monotonic() < deadline:
                await page.wait_for_timeout(250)
            count = min(await rows.count(), 250)
            for index in range(count):
                try:
                    value = (await rows.nth(index).inner_text(timeout=1500)).strip()
                except Exception:
                    continue
                if value and value not in collected:
                    collected.append(value)
        except Exception:
            collected = []
        if not collected:
            if section in {"teachers", "rating"}:
                # Never expose navigation, modal, or filter text if a section selector
                # stops matching after a portal redesign.
                raise PortalLayoutError("На странице раздела не найдены записи")
            try:
                body = (await page.locator("body").inner_text()).strip()
            except Exception as exc:
                raise PortalLayoutError("Не удалось прочитать содержимое страницы") from exc
            lines = [
                re.sub(r"\s+", " ", line).strip()
                for line in body.splitlines()
                if line.strip()
            ]
            return "\n".join(dict.fromkeys(lines))

        rendered: list[str] = []
        seen: set[str] = set()
        for block in collected:
            values = [
                re.sub(r"\s+", " ", line).strip()
                for line in block.splitlines()
                if line.strip()
            ]
            normalized_values = list(dict.fromkeys(values))
            if not normalized_values:
                continue
            row_text = " | ".join(normalized_values)
            if row_text in seen:
                continue
            seen.add(row_text)
            rendered.append(f"• {row_text}")
        return "\n\n".join(rendered)
