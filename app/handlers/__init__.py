from aiogram import Router

from app.handlers.auth import router as auth_router
from app.handlers.private_only import PrivateChatOnlyMiddleware
from app.handlers.portal import router as portal_router


def create_router() -> Router:
    router = Router()
    router.message.outer_middleware(PrivateChatOnlyMiddleware())
    router.callback_query.outer_middleware(PrivateChatOnlyMiddleware())
    router.include_router(auth_router)
    router.include_router(portal_router)
    return router
