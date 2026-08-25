from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from mewcode.platform.settings import PlatformSettings

SCHEMA_REVISION = "0003_phase5_artifacts"


@dataclass
class Database:
    engine: AsyncEngine
    sessions: async_sessionmaker[AsyncSession]

    async def ping(self) -> bool:
        try:
            async with self.engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
            return True
        except SQLAlchemyError:
            return False

    async def schema_is_current(self) -> bool:
        try:
            async with self.engine.connect() as connection:
                revision = await connection.scalar(
                    text("SELECT version_num FROM alembic_version")
                )
            return revision == SCHEMA_REVISION
        except SQLAlchemyError:
            return False

    async def aclose(self) -> None:
        await self.engine.dispose()


def create_database(settings: PlatformSettings, *, echo: bool = False) -> Database:
    engine = create_async_engine(
        settings.async_database_url,
        echo=echo,
        pool_pre_ping=True,
        pool_recycle=300,
    )
    return Database(
        engine=engine,
        sessions=async_sessionmaker(engine, expire_on_commit=False),
    )
