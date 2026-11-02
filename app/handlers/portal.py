from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import Message
from loguru import logger

from app.database import Database
from app.keyboards import (
    EXIT_SCHEDULE_TEXT,
    RATING_TEXT,
    SCHEDULE_TEXT,
    TEACHERS_TEXT,
    TODAY_TEXT,
    TOMORROW_TEXT,
    WEEK_TEXT,
    YESTERDAY_TEXT,
    login_menu,
    main_menu,
    schedule_menu,
)
from app.portal import PortalClient, PortalLayoutError, PortalUnavailable, SessionExpired


router = Router(name="portal-sections")
PERIOD_LABELS = {
    "today": "сегодня",
    "yesterday": "вчера",
    "tomorrow": "завтра",
}
SCHEDULE_PERIODS = {
    TODAY_TEXT: "today",
    YESTERDAY_TEXT: "yesterday",
    TOMORROW_TEXT: "tomorrow",
    WEEK_TEXT: "week",
}
WEEKDAY_INDEX = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
}
_SECTION_REFRESHES: set[str] = set()
CACHE_TTL_SECONDS = {
    "schedule": 7 * 24 * 60 * 60,
    "teachers": 30 * 60,
    "rating": 5 * 60,
}


def _schedule_heading(portal: PortalClient, period: str) -> str:
    today = datetime.now(ZoneInfo("Asia/Bishkek")).date()
    if period == "week":
        week_start = today - timedelta(days=today.weekday())
        week_number, parity = portal.week_info(week_start)
        return f"🗓 Общая неделя · {parity} неделя №{week_number}"
    if period in {"yesterday", "today", "tomorrow"}:
        offset = {"yesterday": -1, "today": 0, "tomorrow": 1}[period]
        target_date = today + timedelta(days=offset)
        week_number, parity = portal.week_info(target_date)
        return (
            f"📅 Расписание на {PERIOD_LABELS[period]} · "
            f"{parity} неделя №{week_number}"
        )
    week_start = today - timedelta(days=today.weekday())
    target_date = week_start + timedelta(days=WEEKDAY_INDEX[period])
    week_number, parity = portal.week_info(target_date)
    return f"📅 Расписание на {target_date:%d.%m.%Y} · {parity} неделя №{week_number}"


def _schedule_lessons_only(text: str) -> str:
    return "\n".join(
        line
        for line in text.splitlines()
        if not (" · " in line and " неделя (№" in line)
    ).strip()


async def _refresh_section_cache(
    message: Message,
    account_id: int,
    storage_state: str,
    section: str,
    period: str,
    title: str,
    institute: str | None,
    course: int | None,
    group_name: str | None,
    cache_key: str,
    previous_data: str,
    db: Database,
    portal: PortalClient,
) -> None:
    if cache_key in _SECTION_REFRESHES:
        return
    _SECTION_REFRESHES.add(cache_key)
    try:
        updated_data = await portal.fetch_section(
            storage_state,
            section,
            None if section == "schedule" else period,
            institute=institute,
            course=course,
            group_name=group_name,
            force_refresh=True,
        )
        if section == "schedule":
            changed = _schedule_lessons_only(previous_data) != _schedule_lessons_only(
                updated_data
            )
            if period == "week":
                updated_text = portal.refresh_cached_schedule_week_dates(updated_data)
            else:
                updated_text = portal.refresh_cached_schedule_date(updated_data, period)
            heading = _schedule_heading(portal, period)
        else:
            changed = previous_data != updated_data
            updated_text = updated_data
            heading = title
        if changed:
            await db.save_cached_response(cache_key, updated_data)
        else:
            await db.mark_cached_response_checked(cache_key)
        if changed:
            logger.info("Saved portal response changed after background refresh: section={}", section)
            await message.answer("🔄 Данные портала обновились:")
            await message.answer(heading)
            for chunk in _split_text(updated_text, 3500):
                await message.answer(chunk)
    except SessionExpired:
        logger.warning("Portal session expired while refreshing saved section: {}", section)
        await db.mark_invalid(account_id)
    except (PortalUnavailable, PortalLayoutError):
        logger.warning("Keeping saved portal response because refresh failed: section={}", section)
        try:
            await db.mark_cached_response_checked(cache_key)
        except Exception:
            logger.exception("Could not record portal cache check: section={}", section)
    except Exception:
        logger.exception("Background portal refresh failed: section={}", section)
    finally:
        _SECTION_REFRESHES.discard(cache_key)


async def require_account(message: Message, db: Database) -> bool:
    account = await db.get_account(message.from_user.id)
    if account is not None and account.is_valid:
        return True
    await message.answer(
        "Сессия портала не найдена или завершилась. Нажмите «🔐 Войти в портал».",
        reply_markup=login_menu(),
    )
    return False


async def send_section(
    message: Message,
    section: str,
    title: str,
    db: Database,
    portal: PortalClient,
    period: str | None = None,
    telegram_id: int | None = None,
) -> None:
    account_id = telegram_id if telegram_id is not None else message.from_user.id
    account = await db.get_account(account_id)
    if account is None or not account.is_valid:
        await message.answer(
            "Сессия портала не найдена или завершилась. Нажмите «🔐 Войти в портал».",
            reply_markup=login_menu(),
        )
        return
    profile = await db.get_student_profile(account_id) if section == "schedule" else None
    if section == "schedule" and profile is None:
        await message.answer(
            "Сначала укажите институт, курс и группу через «⚙️ Настройки / Выход» → «Изменить институт, группу и курс».",
            reply_markup=main_menu(),
        )
        return

    cache_key = portal.persistent_cache_key(
        account_id,
        account.storage_state,
        section,
        period,
        institute=profile.institute if profile else None,
        course=profile.course if profile else None,
        group_name=profile.group_name if profile else None,
    )
    loading_message = await message.answer("⏳ Загружаю данные портала…")
    offline_notice: str | None = None
    try:
        cached = await db.get_cached_response(cache_key)
    except Exception:
        logger.exception("Could not read saved portal response before request: section={}", section)
        cached = None
    if cached is not None:
        saved_data, saved_at, checked_at = cached
        if saved_at.tzinfo is None:
            saved_at = saved_at.replace(tzinfo=timezone.utc)
        saved_at_utc = saved_at.astimezone(timezone.utc)
        if checked_at.tzinfo is None:
            checked_at = checked_at.replace(tzinfo=timezone.utc)
        checked_at_utc = checked_at.astimezone(timezone.utc)
        saved_at_local = saved_at_utc.astimezone(ZoneInfo("Asia/Bishkek"))
        if section == "schedule":
            if period == "week":
                display_data = portal.refresh_cached_schedule_week_dates(saved_data)
            else:
                display_data = portal.refresh_cached_schedule_date(saved_data, period)
            heading = (
                f"{_schedule_heading(portal, period)}\n"
                f"📦 Сохранённые данные · {saved_at_local:%d.%m.%Y %H:%M}"
            )
        else:
            display_data = saved_data
            heading = f"{title}\n📦 Сохранённые данные · {saved_at_local:%d.%m.%Y %H:%M}"
        await loading_message.edit_text(heading)
        for chunk in _split_text(display_data, 3500):
            await message.answer(chunk)
        cache_age = (datetime.now(timezone.utc) - checked_at_utc).total_seconds()
        this_week = datetime.now(ZoneInfo("Asia/Bishkek")).date().isocalendar()[:2]
        checked_week = checked_at_utc.astimezone(
            ZoneInfo("Asia/Bishkek")
        ).date().isocalendar()[:2]
        week_changed = section == "schedule" and checked_week != this_week
        if cache_age >= CACHE_TTL_SECONDS[section] or week_changed:
            portal.create_background_task(
                _refresh_section_cache(
                    message,
                    account_id,
                    account.storage_state,
                    section,
                    period,
                    title,
                    profile.institute if profile else None,
                    profile.course if profile else None,
                    profile.group_name if profile else None,
                    cache_key,
                    saved_data,
                    db,
                    portal,
                )
            )
        return

    try:
        text = await portal.fetch_section(
            account.storage_state,
            section,
            None if section == "schedule" else period,
            institute=profile.institute if profile else None,
            course=profile.course if profile else None,
            group_name=profile.group_name if profile else None,
        )
    except SessionExpired:
        await db.mark_invalid(account_id)
        await loading_message.edit_text(
            "Сессия портала завершилась. Нажмите «🔐 Войти в портал», чтобы войти снова.",
            reply_markup=login_menu(),
        )
        return
    except (PortalUnavailable, PortalLayoutError) as exc:
        logger.exception("Portal request failed for section {}", section)
        try:
            cached = await db.get_cached_response(cache_key)
        except Exception:
            logger.exception("Could not read saved portal response for section {}", section)
            cached = None
        if cached is None:
            error_text = (
                "⚠ Студенческий портал временно недоступен. Попробуйте позже."
                if isinstance(exc, PortalUnavailable)
                else "⚠ Ошибка обработки данных портала. Попробуйте позже."
            )
            await loading_message.edit_text(error_text)
            return

        text, saved_at, _ = cached
        if section == "schedule":
            if period == "week":
                text = portal.refresh_cached_schedule_week_dates(text)
            else:
                text = portal.refresh_cached_schedule_date(text, period)
        if saved_at.tzinfo is None:
            saved_at = saved_at.replace(tzinfo=timezone.utc)
        saved_at_local = saved_at.astimezone(ZoneInfo("Asia/Bishkek"))
        offline_notice = (
            "⚠ Портал сейчас недоступен. Показываю сохранённые данные "
            f"(обновлены {saved_at_local:%d.%m.%Y %H:%M})."
        )
    else:
        try:
            await db.save_cached_response(cache_key, text)
        except Exception:
            logger.exception("Could not save portal response for section {}", section)

    if section == "schedule":
        if period == "week":
            text = portal.refresh_cached_schedule_week_dates(text)
        else:
            text = portal.refresh_cached_schedule_date(text, period)

    heading = title
    if period:
        heading = _schedule_heading(portal, period)
    chunks = _split_text(text, 3500)
    if offline_notice:
        heading = f"{offline_notice}\n\n{heading}"
    await loading_message.edit_text(heading)
    for chunk in chunks:
        await message.answer(chunk)


def _split_text(value: str, limit: int) -> list[str]:
    lines = value.splitlines()
    chunks: list[str] = []
    current = ""
    for line in lines:
        piece = line if not current else f"{current}\n{line}"
        if len(piece) > limit and current:
            chunks.append(current)
            current = line
        else:
            current = piece
    if current:
        chunks.append(current)
    return chunks or ["Портал не вернул данные."]


@router.message(F.text == SCHEDULE_TEXT)
async def open_schedule(message: Message, db: Database) -> None:
    if await require_account(message, db):
        profile = await db.get_student_profile(message.from_user.id)
        if profile is None:
            await message.answer(
                "Сначала укажите институт, курс и группу через «⚙️ Настройки / Выход» → «Изменить институт, группу и курс».",
                reply_markup=main_menu(),
            )
            return
        await message.answer("Выберите период:", reply_markup=schedule_menu())


@router.message(F.text.in_(SCHEDULE_PERIODS))
async def schedule_period(
    message: Message,
    db: Database,
    portal: PortalClient,
) -> None:
    period = SCHEDULE_PERIODS[message.text]
    await send_section(message, "schedule", "Расписание", db, portal, period)


@router.message(F.text == EXIT_SCHEDULE_TEXT)
async def exit_schedule(message: Message) -> None:
    await message.answer("Вы вышли из режима расписания.", reply_markup=main_menu())


@router.message(F.text == TEACHERS_TEXT)
async def teachers(message: Message, db: Database, portal: PortalClient) -> None:
    await send_section(message, "teachers", "👩‍🏫 Мои преподаватели", db, portal)


@router.message(F.text == RATING_TEXT)
async def rating(message: Message, db: Database, portal: PortalClient) -> None:
    await send_section(message, "rating", "📊 Моя успеваемость", db, portal)


@router.message()
async def unknown(message: Message, state: FSMContext) -> None:
    if await state.get_state() is None:
        await message.answer("Используйте кнопки меню.", reply_markup=main_menu())
