from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import insert, make_url, text

from app.config import settings
from app.database import (
    CachedPortalResponse,
    Database,
    PortalAccount,
    StudentProfile,
    validate_postgresql_url,
)


TABLES = (
    (PortalAccount, "portal_accounts"),
    (StudentProfile, "student_profiles"),
    (CachedPortalResponse, "portal_response_cache"),
)
DATETIME_COLUMNS = {
    "created_at",
    "updated_at",
    "last_validated_at",
    "checked_at",
}


def _sqlite_path() -> Path:
    url = make_url(settings.sqlite_database_url)
    if url.get_backend_name() != "sqlite" or not url.database:
        raise ValueError("SQLITE_DATABASE_URL must point to a SQLite file.")
    if url.database == ":memory:":
        raise ValueError("SQLite migration source must be a file, not :memory:.")
    return Path(url.database).resolve()


def _convert_row(row: sqlite3.Row) -> dict[str, object]:
    result: dict[str, object] = dict(row)
    for column in DATETIME_COLUMNS & result.keys():
        value = result[column]
        if isinstance(value, str):
            value = datetime.fromisoformat(value)
        if isinstance(value, datetime) and value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        result[column] = value
    if "is_valid" in result:
        result["is_valid"] = bool(result["is_valid"])
    return result


async def migrate() -> None:
    validate_postgresql_url(settings.database_url)
    source_path = _sqlite_path()
    if not source_path.is_file():
        raise FileNotFoundError(f"SQLite source database does not exist: {source_path}")

    database = Database()
    try:
        await database.initialize()
        async with database.engine.begin() as target:
            for model, table_name in TABLES:
                count = await target.scalar(
                    text(f'SELECT COUNT(*) FROM "{table_name}"')
                )
                if count:
                    raise RuntimeError(
                        f"PostgreSQL table {table_name} is not empty; "
                        "refusing to overwrite existing data."
                    )

            migrated: dict[str, int] = {}
            with sqlite3.connect(
                f"file:{source_path}?mode=ro", uri=True
            ) as source:
                source.row_factory = sqlite3.Row
                available_tables = {
                    row[0]
                    for row in source.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
                for model, table_name in TABLES:
                    if table_name not in available_tables:
                        raise RuntimeError(
                            f"SQLite source is missing required table {table_name}."
                        )

                    available_columns = {
                        row[1]
                        for row in source.execute(
                            f'PRAGMA table_info("{table_name}")'
                        )
                    }
                    if (
                        table_name == "portal_response_cache"
                        and "checked_at" not in available_columns
                    ):
                        query = (
                            'SELECT cache_key, response_text, updated_at, '
                            'updated_at AS checked_at FROM "portal_response_cache"'
                        )
                    else:
                        query = f'SELECT * FROM "{table_name}"'

                    rows = [_convert_row(row) for row in source.execute(query).fetchall()]
                    table = model.__table__
                    for offset in range(0, len(rows), 500):
                        await target.execute(
                            insert(table), rows[offset : offset + 500]
                        )
                    migrated[table_name] = len(rows)

        for table_name, count in migrated.items():
            print(f"{table_name}: {count} rows migrated")
    finally:
        await database.dispose()


if __name__ == "__main__":
    asyncio.run(migrate())
