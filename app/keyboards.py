from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup


SCHEDULE_TEXT = "📅 Расписание"
TEACHERS_TEXT = "👩‍🏫 Мои преподаватели"
RATING_TEXT = "📊 Моя успеваемость"
SETTINGS_TEXT = "⚙️ Настройки / Выход"
LOGIN_TEXT = "🔐 Войти в портал"
CANCEL_TEXT = "❌ Отмена"
TODAY_TEXT = "📅 Сегодня"
YESTERDAY_TEXT = "⬅️ Вчера"
TOMORROW_TEXT = "➡️ Завтра"
WEEK_TEXT = "🗓 Общая неделя"
EXIT_SCHEDULE_TEXT = "↩️ Выход из расписания"


def login_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=LOGIN_TEXT)]],
        resize_keyboard=True,
    )


def login_cancel_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=CANCEL_TEXT)]],
        resize_keyboard=True,
    )


def profile_choices_menu(options: list[str]) -> ReplyKeyboardMarkup:
    keyboard = [[KeyboardButton(text=option)] for option in options]
    keyboard.append([KeyboardButton(text=CANCEL_TEXT)])
    return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)


def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=SCHEDULE_TEXT), KeyboardButton(text=TEACHERS_TEXT)],
            [KeyboardButton(text=RATING_TEXT)],
            [KeyboardButton(text=SETTINGS_TEXT)],
        ],
        resize_keyboard=True,
    )


def schedule_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=TODAY_TEXT), KeyboardButton(text=YESTERDAY_TEXT)],
            [KeyboardButton(text=TOMORROW_TEXT), KeyboardButton(text=WEEK_TEXT)],
            [KeyboardButton(text=EXIT_SCHEDULE_TEXT)],
        ],
        resize_keyboard=True,
    )


def settings_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Изменить институт, группу и курс", callback_data="account:profile")],
            [InlineKeyboardButton(text="Войти заново", callback_data="account:renew")],
            [InlineKeyboardButton(text="Выйти из аккаунта", callback_data="account:logout")],
        ]
    )
