from aiogram.fsm.state import State, StatesGroup


class PortalLogin(StatesGroup):
    waiting_for_username = State()
    waiting_for_password = State()
    waiting_for_captcha = State()


class StudentProfileSetup(StatesGroup):
    waiting_for_institute = State()
    waiting_for_course = State()
    waiting_for_group = State()
