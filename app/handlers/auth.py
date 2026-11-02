from __future__ import annotations

import re

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, Message
from loguru import logger

from app.database import Database
from app.keyboards import (
    CANCEL_TEXT,
    LOGIN_TEXT,
    login_cancel_menu,
    login_menu,
    main_menu,
    profile_choices_menu,
    settings_menu,
)
from app.portal import (
    CaptchaRetry,
    LoginRejected,
    LoginSucceeded,
    PortalClient,
    PortalError,
    PortalUnavailable,
    SessionExpired,
)
from app.states import PortalLogin, StudentProfileSetup


router = Router(name="authentication")


def _match_choice(value: str, options: list[str]) -> str | None:
    normalized = " ".join(value.split()).casefold()
    return next(
        (option for option in options if " ".join(option.split()).casefold() == normalized),
        None,
    )


async def begin_profile_setup(
    message: Message,
    state: FSMContext,
    db: Database,
    portal: PortalClient,
    telegram_id: int | None = None,
) -> None:
    await state.clear()
    user_id = telegram_id or message.from_user.id
    account = await db.get_account(user_id)
    if account is None or not account.is_valid:
        await message.answer(
            "Сначала войдите в портал кнопкой «🔐 Войти в портал».",
            reply_markup=login_menu(),
        )
        return
    try:
        options = await portal.schedule_filter_options(account.storage_state)
    except SessionExpired:
        await db.mark_invalid(user_id)
        await message.answer(
            "Сессия портала завершилась. Нажмите «🔐 Войти в портал», чтобы войти снова.",
            reply_markup=login_menu(),
        )
        return
    except PortalError as exc:
        logger.error("Could not load profile choices: {}", exc)
        await message.answer(
            "Не удалось загрузить институты с портала. Попробуйте ещё раз через «⚙️ Настройки / Выход».",
            reply_markup=main_menu(),
        )
        return

    institutes = options["institutes"]
    if not institutes:
        await message.answer(
            "Портал не вернул список институтов. Попробуйте позже.",
            reply_markup=main_menu(),
        )
        return
    await state.clear()
    await state.set_state(StudentProfileSetup.waiting_for_institute)
    await state.update_data(institutes=institutes)
    await message.answer(
        "✅ Вход выполнен. Для расписания выберите институт:",
        reply_markup=profile_choices_menu(institutes),
    )


async def ask_for_username(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(PortalLogin.waiting_for_username)
    await message.answer(
        "Введите логин от студенческого портала ОшТУ. Чтобы отменить вход, нажмите «❌ Отмена».",
        reply_markup=login_cancel_menu(),
    )


@router.message(CommandStart())
async def start(
    message: Message, state: FSMContext, db: Database, portal: PortalClient
) -> None:
    await portal.cancel_login(message.from_user.id)
    await state.clear()
    account = await db.get_account(message.from_user.id)
    if account is not None and account.is_valid:
        profile = await db.get_student_profile(message.from_user.id)
        if profile is None:
            await begin_profile_setup(message, state, db, portal)
            return
        await message.answer("Вы вошли в портал ОшТУ. Выберите раздел:", reply_markup=main_menu())
        return
    await ask_for_username(message, state)


@router.message(Command("login"))
@router.message(F.text == LOGIN_TEXT)
async def login_command(message: Message, state: FSMContext, portal: PortalClient) -> None:
    await portal.cancel_login(message.from_user.id)
    await ask_for_username(message, state)


@router.message(Command("cancel"))
@router.message(F.text == CANCEL_TEXT)
async def cancel(message: Message, state: FSMContext, portal: PortalClient) -> None:
    current_state = await state.get_state()
    if current_state and current_state.startswith("StudentProfileSetup:"):
        await state.clear()
        await message.answer(
            "Настройка профиля отменена. Её можно продолжить через «⚙️ Настройки / Выход» → «Изменить институт, группу и курс».",
            reply_markup=main_menu(),
        )
        return
    await portal.cancel_login(message.from_user.id)
    await state.clear()
    await message.answer(
        "Вход отменён. Нажмите «🔐 Войти в портал», чтобы начать снова.",
        reply_markup=login_menu(),
    )


@router.message(PortalLogin.waiting_for_username, F.text)
async def receive_username(message: Message, state: FSMContext) -> None:
    username = (message.text or "").strip()
    if not username:
        await message.answer("Логин не должен быть пустым. Введите логин ещё раз.")
        return
    await state.update_data(username=username)
    await state.set_state(PortalLogin.waiting_for_password)
    await message.answer(
        "Теперь отправьте пароль от портала. Бот попытается удалить это сообщение после получения. "
        "Для отмены нажмите «❌ Отмена».",
        reply_markup=login_cancel_menu(),
    )


@router.message(PortalLogin.waiting_for_password, F.text)
async def receive_password(
    message: Message, state: FSMContext, portal: PortalClient
) -> None:
    password = message.text or ""
    try:
        await message.delete()
    except Exception:
        pass
    data = await state.get_data()
    username = data.get("username", "")
    if not username or not password:
        await state.clear()
        await message.answer(
            "Не удалось получить данные входа. Нажмите «🔐 Войти в портал» и начните заново.",
            reply_markup=login_menu(),
        )
        return
    try:
        image = await portal.begin_login(message.from_user.id, username, password)
    except PortalUnavailable as exc:
        logger.error("Portal unavailable during login setup: {}", exc)
        await state.clear()
        await message.answer(
            "⚠ Студенческий портал временно недоступен. Попробуйте позже.",
            reply_markup=login_menu(),
        )
        return
    except PortalError as exc:
        logger.error("Portal login form could not be processed: {}", exc)
        await state.clear()
        await message.answer(
            "⚠ Ошибка обработки данных портала. Попробуйте позже.",
            reply_markup=login_menu(),
        )
        return
    await state.set_state(PortalLogin.waiting_for_captcha)
    await message.answer_photo(
        BufferedInputFile(image, filename="portal-captcha.png"),
        caption="Введите код с картинки. Чтобы отменить вход, нажмите «❌ Отмена».",
        reply_markup=login_cancel_menu(),
    )


@router.message(PortalLogin.waiting_for_captcha, F.text)
async def receive_captcha(
    message: Message, state: FSMContext, db: Database, portal: PortalClient
) -> None:
    try:
        result = await portal.submit_captcha(message.from_user.id, message.text or "")
    except PortalUnavailable as exc:
        logger.error("Portal unavailable while completing login: {}", exc)
        await state.clear()
        await message.answer(
            "⚠ Студенческий портал временно недоступен. Попробуйте позже.",
            reply_markup=login_menu(),
        )
        return
    except PortalError as exc:
        logger.error("Portal login flow failed: {}", exc)
        await state.clear()
        await message.answer(
            "⚠ Не удалось обработать вход. Нажмите «🔐 Войти в портал» и попробуйте снова.",
            reply_markup=login_menu(),
        )
        return

    if isinstance(result, LoginSucceeded):
        await db.save_account(message.from_user.id, result.storage_state)
        await begin_profile_setup(message, state, db, portal)
    elif isinstance(result, LoginRejected):
        await state.clear()
        await message.answer(
            "❌ Неверный логин или пароль. Проверьте данные и нажмите «🔐 Войти в портал». Это начнёт вход заново.",
            reply_markup=login_menu(),
        )
    elif isinstance(result, CaptchaRetry):
        await message.answer(
            "⚠ Портал не подтвердил вход. Проверьте введённый код и попробуйте с новой капчей.",
            reply_markup=login_cancel_menu(),
        )
        await message.answer_photo(
            BufferedInputFile(result.image, filename="portal-captcha.png"),
            caption="Введите код с картинки. Чтобы отменить вход, нажмите «❌ Отмена».",
            reply_markup=login_cancel_menu(),
        )


@router.message(F.text == "⚙️ Настройки / Выход")
async def settings(message: Message) -> None:
    await message.answer("Настройки аккаунта портала:", reply_markup=settings_menu())


@router.message(StudentProfileSetup.waiting_for_institute, F.text)
async def choose_institute(
    message: Message, state: FSMContext, db: Database, portal: PortalClient
) -> None:
    data = await state.get_data()
    institutes = data.get("institutes", [])
    institute = _match_choice(message.text or "", institutes)
    if institute is None:
        await message.answer(
            "Выберите институт кнопкой из списка ниже.",
            reply_markup=profile_choices_menu(institutes),
        )
        return
    account = await db.get_account(message.from_user.id)
    if account is None or not account.is_valid:
        await state.clear()
        await message.answer(
            "Сессия портала завершилась. Нажмите «🔐 Войти в портал», чтобы войти снова.",
            reply_markup=login_menu(),
        )
        return
    try:
        options = await portal.schedule_filter_options(
            account.storage_state, institute=institute
        )
    except PortalError as exc:
        logger.error("Could not load course choices: {}", exc)
        await message.answer("Не удалось загрузить курсы. Попробуйте выбрать институт ещё раз.")
        return
    courses = options["courses"]
    if not courses:
        await message.answer("Портал не вернул список курсов. Выберите институт ещё раз.")
        return
    await state.update_data(institute=institute, courses=courses)
    await state.set_state(StudentProfileSetup.waiting_for_course)
    await message.answer(
        "Теперь выберите курс:",
        reply_markup=profile_choices_menu(courses),
    )


@router.message(StudentProfileSetup.waiting_for_course, F.text)
async def choose_course(
    message: Message, state: FSMContext, db: Database, portal: PortalClient
) -> None:
    data = await state.get_data()
    courses = data.get("courses", [])
    course_label = _match_choice(message.text or "", courses)
    if course_label is None:
        await message.answer(
            "Выберите курс кнопкой из списка ниже.",
            reply_markup=profile_choices_menu(courses),
        )
        return
    course_match = re.search(r"\d+", course_label)
    if course_match is None:
        await message.answer("Не удалось определить номер курса. Выберите курс ещё раз.")
        return
    course = int(course_match.group())
    institute = data.get("institute", "")
    account = await db.get_account(message.from_user.id)
    if account is None or not account.is_valid:
        await state.clear()
        await message.answer(
            "Сессия портала завершилась. Нажмите «🔐 Войти в портал», чтобы войти снова.",
            reply_markup=login_menu(),
        )
        return
    try:
        options = await portal.schedule_filter_options(
            account.storage_state, institute=institute, course=course
        )
    except PortalError as exc:
        logger.error("Could not load group choices: {}", exc)
        await message.answer("Не удалось загрузить группы. Выберите курс ещё раз.")
        return
    groups = options["groups"]
    if not groups:
        await message.answer(
            "Для выбранных института и курса портал не показал группы. "
            "Вернитесь к выбору института и проверьте данные.",
            reply_markup=profile_choices_menu(data.get("institutes", [])),
        )
        await state.set_state(StudentProfileSetup.waiting_for_institute)
        return
    await state.update_data(course=course, groups=groups)
    await state.set_state(StudentProfileSetup.waiting_for_group)
    await message.answer(
        "Выберите свою группу:",
        reply_markup=profile_choices_menu(groups),
    )


@router.message(StudentProfileSetup.waiting_for_group, F.text)
async def choose_group(message: Message, state: FSMContext, db: Database) -> None:
    data = await state.get_data()
    groups = data.get("groups", [])
    group_name = _match_choice(message.text or "", groups)
    if group_name is None:
        await message.answer(
            "Выберите группу кнопкой из списка ниже.",
            reply_markup=profile_choices_menu(groups),
        )
        return
    await db.save_student_profile(
        message.from_user.id,
        data["institute"],
        group_name,
        int(data["course"]),
    )
    await state.clear()
    await message.answer(
        "✅ Профиль сохранён. Институт, курс и группа будут применяться к расписанию автоматически.",
        reply_markup=main_menu(),
    )


@router.callback_query(F.data == "account:logout")
async def logout(callback: CallbackQuery, state: FSMContext, db: Database, portal: PortalClient) -> None:
    await portal.cancel_login(callback.from_user.id)
    await db.delete_account(callback.from_user.id)
    await state.clear()
    await callback.answer("Сессия удалена")
    if callback.message:
        await callback.message.answer(
            "Вы вышли из портала. Нажмите «🔐 Войти в портал», чтобы войти снова.",
            reply_markup=login_menu(),
        )


@router.callback_query(F.data == "account:renew")
async def renew(callback: CallbackQuery, state: FSMContext, portal: PortalClient) -> None:
    await portal.cancel_login(callback.from_user.id)
    await state.clear()
    await state.set_state(PortalLogin.waiting_for_username)
    await callback.answer()
    if callback.message:
        await callback.message.answer(
            "Введите логин от студенческого портала ОшТУ. Чтобы отменить вход, нажмите «❌ Отмена».",
            reply_markup=login_cancel_menu(),
        )


@router.callback_query(F.data == "account:profile")
async def edit_profile(
    callback: CallbackQuery, state: FSMContext, db: Database, portal: PortalClient
) -> None:
    await callback.answer()
    if isinstance(callback.message, Message):
        await begin_profile_setup(
            callback.message, state, db, portal, telegram_id=callback.from_user.id
        )
