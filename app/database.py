from __future__ import annotations

import os
from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Integer,
    String,
    Text,
    inspect,
    make_url,
    select,
    text,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.engine import Connection
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.config import settings


def validate_postgresql_url(url: str) -> None:
    if not url:
        raise RuntimeError("Не задан DATABASE_URL. Укажите адрес PostgreSQL в .env.")
    if make_url(url).get_backend_name() != "postgresql":
        raise RuntimeError(
            "DATABASE_URL должен указывать на PostgreSQL; SQLite используется только "
            "как источник переноса."
        )


class Base(DeclarativeBase):
    pass


class PortalAccount(Base):
    __tablename__ = "portal_accounts"

    telegram_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    storage_state: Mapped[str] = mapped_column(Text, nullable=False)
    is_valid: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )
    last_validated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class StudentProfile(Base):
    __tablename__ = "student_profiles"

    telegram_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    institute: Mapped[str] = mapped_column(Text, nullable=False)
    group_name: Mapped[str] = mapped_column(Text, nullable=False)
    course: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )


class CachedPortalResponse(Base):
    __tablename__ = "portal_response_cache"

    cache_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    response_text: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )
    checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Database:
    def __init__(self, url: str = settings.database_url) -> None:
        if not url:
            raise RuntimeError("Не задан DATABASE_URL. Укажите адрес PostgreSQL в .env.")
        parsed_url = make_url(url)
        self.database_path: str | None = None
        if parsed_url.get_backend_name() == "sqlite" and parsed_url.database not in {
            None,
            ":memory:",
        }:
            self.database_path = os.path.abspath(parsed_url.database)
            os.makedirs(os.path.dirname(self.database_path), exist_ok=True)
        engine_options: dict[str, int | bool] = {"echo": False}
        if parsed_url.get_backend_name() == "postgresql":
            if settings.database_pool_size < 1 or settings.database_max_overflow < 0:
                raise ValueError(
                    "DATABASE_POOL_SIZE must be positive and DATABASE_MAX_OVERFLOW "
                    "must not be negative."
                )
            engine_options.update(
                pool_size=settings.database_pool_size,
                max_overflow=settings.database_max_overflow,
            )
        self.engine = create_async_engine(url, **engine_options)
        self.sessions = async_sessionmaker(
            self.engine, expire_on_commit=False, class_=AsyncSession
        )

    async def initialize(self) -> None:
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
            await connection.run_sync(self._ensure_cache_checked_at)
            if connection.dialect.name == "postgresql":
                await connection.execute(
                    text(
                        "ALTER TABLE portal_accounts "
                        "ALTER COLUMN telegram_id TYPE BIGINT"
                    )
                )
                await connection.execute(
                    text(
                        "ALTER TABLE student_profiles "
                        "ALTER COLUMN telegram_id TYPE BIGINT"
                    )
                )
        if self.database_path and os.path.exists(self.database_path):
            os.chmod(self.database_path, 0o600)

    @staticmethod
    def _ensure_cache_checked_at(connection: Connection) -> None:
        columns = {
            column["name"]
            for column in inspect(connection).get_columns("portal_response_cache")
        }
        if "checked_at" not in columns:
            column_type = (
                "TIMESTAMP WITH TIME ZONE"
                if connection.dialect.name == "postgresql"
                else "DATETIME"
            )
            connection.execute(
                text(
                    "ALTER TABLE portal_response_cache "
                    f"ADD COLUMN checked_at {column_type}"
                )
            )
            connection.execute(
                text(
                    "UPDATE portal_response_cache "
                    "SET checked_at = updated_at WHERE checked_at IS NULL"
                )
            )

    async def get_account(self, telegram_id: int) -> PortalAccount | None:
        async with self.sessions() as session:
            return await session.get(PortalAccount, telegram_id)

    async def save_account(self, telegram_id: int, storage_state: str) -> None:
        async with self.sessions() as session:
            account = await session.get(PortalAccount, telegram_id)
            now = datetime.now(timezone.utc)
            if account is None:
                account = PortalAccount(
                    telegram_id=telegram_id,
                    storage_state=storage_state,
                    is_valid=True,
                    created_at=now,
                    updated_at=now,
                )
                session.add(account)
            else:
                account.storage_state = storage_state
                account.is_valid = True
                account.updated_at = now
                account.last_validated_at = now
            await session.commit()

    async def delete_account(self, telegram_id: int) -> None:
        async with self.sessions() as session:
            account = await session.get(PortalAccount, telegram_id)
            if account is not None:
                await session.delete(account)
                await session.commit()

    async def get_student_profile(self, telegram_id: int) -> StudentProfile | None:
        async with self.sessions() as session:
            return await session.get(StudentProfile, telegram_id)

    async def save_student_profile(
        self, telegram_id: int, institute: str, group_name: str, course: int
    ) -> None:
        async with self.sessions() as session:
            profile = await session.get(StudentProfile, telegram_id)
            now = datetime.now(timezone.utc)
            if profile is None:
                profile = StudentProfile(
                    telegram_id=telegram_id,
                    institute=institute,
                    group_name=group_name,
                    course=course,
                    updated_at=now,
                )
                session.add(profile)
            else:
                profile.institute = institute
                profile.group_name = group_name
                profile.course = course
                profile.updated_at = now
            await session.commit()

    async def mark_invalid(self, telegram_id: int) -> None:
        async with self.sessions() as session:
            account = await session.get(PortalAccount, telegram_id)
            if account is not None:
                account.is_valid = False
                account.last_validated_at = datetime.now(timezone.utc)
                await session.commit()

    async def list_valid_accounts(self) -> list[PortalAccount]:
        async with self.sessions() as session:
            result = await session.scalars(
                select(PortalAccount).where(PortalAccount.is_valid.is_(True))
            )
            return list(result.all())

    async def get_cached_response(
        self, cache_key: str
    ) -> tuple[str, datetime, datetime] | None:
        async with self.sessions() as session:
            cached = await session.get(CachedPortalResponse, cache_key)
            if cached is None:
                return None
            return (
                cached.response_text,
                cached.updated_at,
                cached.checked_at or cached.updated_at,
            )

    async def save_cached_response(self, cache_key: str, response_text: str) -> None:
        async with self.sessions() as session:
            cached = await session.get(CachedPortalResponse, cache_key)
            now = datetime.now(timezone.utc)
            if cached is None:
                session.add(
                    CachedPortalResponse(
                        cache_key=cache_key,
                        response_text=response_text,
                        updated_at=now,
                        checked_at=now,
                    )
                )
            else:
                if cached.response_text != response_text:
                    cached.response_text = response_text
                    cached.updated_at = now
                cached.checked_at = now
            await session.commit()

    async def mark_cached_response_checked(self, cache_key: str) -> None:
        async with self.sessions() as session:
            cached = await session.get(CachedPortalResponse, cache_key)
            if cached is not None:
                cached.checked_at = datetime.now(timezone.utc)
                await session.commit()

    async def dispose(self) -> None:
        await self.engine.dispose()
