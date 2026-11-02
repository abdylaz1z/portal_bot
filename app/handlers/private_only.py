from __future__ import annotations

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message


class PrivateChatOnlyMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        chat = getattr(event, "chat", None)
        if chat is None and isinstance(event, CallbackQuery):
            chat = getattr(event.message, "chat", None)
        if chat is not None and chat.type == "private":
            return await handler(event, data)

        if isinstance(event, Message):
            await event.answer("Для защиты данных используйте бота в личном чате.")
        elif isinstance(event, CallbackQuery):
            await event.answer("Откройте бота в личном чате.", show_alert=True)
        return None
