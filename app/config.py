from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv


load_dotenv(encoding="utf-8-sig")


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    bot_token: str = os.getenv("BOT_TOKEN", "")
    database_url: str = os.getenv("DATABASE_URL", "")
    database_pool_size: int = int(os.getenv("DATABASE_POOL_SIZE", "5"))
    database_max_overflow: int = int(os.getenv("DATABASE_MAX_OVERFLOW", "5"))
    sqlite_database_url: str = os.getenv(
        "SQLITE_DATABASE_URL", "sqlite+aiosqlite:///./data/oshtu_bot.db"
    )
    redis_url: str = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    bot_mode: str = os.getenv("BOT_MODE", "polling")
    bot_role: str = os.getenv("BOT_ROLE", "worker")
    webhook_public_url: str = os.getenv("WEBHOOK_PUBLIC_URL", "").rstrip("/")
    webhook_path: str = os.getenv("WEBHOOK_PATH", "/telegram/webhook")
    webhook_secret: str = os.getenv("WEBHOOK_SECRET", "")
    webhook_worker_port: int = int(os.getenv("WEBHOOK_WORKER_PORT", "8080"))
    webhook_gateway_port: int = int(os.getenv("WEBHOOK_GATEWAY_PORT", "8081"))
    webhook_max_connections: int = int(os.getenv("WEBHOOK_MAX_CONNECTIONS", "40"))
    headless_mode: bool = _env_bool("HEADLESS_MODE", True)
    parser_timeout_ms: int = int(os.getenv("PARSER_TIMEOUT", "30000"))
    portal_url: str = os.getenv("PORTAL_URL", "https://portal.oshtu.kg/")
    login_user_selector: str = os.getenv(
        "PORTAL_LOGIN_USER_SELECTOR",
        'input[autocomplete="userLogin"], input[placeholder*="логин" i], '
        'input[name*="login" i], input[type="text"]',
    )
    login_password_selector: str = os.getenv(
        "PORTAL_LOGIN_PASSWORD_SELECTOR",
        'input[autocomplete="userPwd"], input[placeholder*="пароль" i], '
        'input[name*="password" i], input[type="password"]',
    )
    captcha_image_selector: str = os.getenv(
        "PORTAL_CAPTCHA_IMAGE_SELECTOR", "img.pointer, #captchaImage, img[alt*='captcha' i]"
    )
    captcha_input_selector: str = os.getenv(
        "PORTAL_CAPTCHA_INPUT_SELECTOR",
        'input[placeholder="Код"], input[name*="captcha" i], '
        'input[id*="captcha" i], input[placeholder*="код" i], '
        'input[placeholder*="captcha" i]',
    )
    login_submit_selector: str = os.getenv(
        "PORTAL_LOGIN_SUBMIT_SELECTOR",
        'button:has-text("Войти в систему"), button:has-text("Войти"), '
        'input[type="submit"], button[type="submit"]',
    )
    teachers_rows_selector: str = os.getenv(
        "PORTAL_TEACHERS_ROWS_SELECTOR",
        ".teacher-container",
    )
    rating_rows_selector: str = os.getenv(
        "PORTAL_RATING_ROWS_SELECTOR",
        "table.table-rating tr.pointer",
    )


settings = Settings()
