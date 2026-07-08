"""
Couche de persistance — PostgreSQL (Render) via SQLAlchemy async.

Remplace le stockage en mémoire (perdu à chaque redéploiement) pour tout ce
qui doit survivre dans la durée : comptes Studio, bibliothèque de photos,
styles de contenu (avec exemples de textes) et file de contenus générés.
"""
import datetime
import os
import uuid

from sqlalchemy import JSON, DateTime, ForeignKey, String, Text, select
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _database_url() -> str:
    url = os.environ.get("DATABASE_URL", "")
    # Render fournit souvent "postgres://", SQLAlchemy async veut "postgresql+asyncpg://".
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql+asyncpg://", 1)
    elif url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return url


engine = create_async_engine(_database_url(), pool_pre_ping=True)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def _uuid() -> str:
    return str(uuid.uuid4())


class StudioUser(Base):
    __tablename__ = "studio_users"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    business_name: Mapped[str] = mapped_column(String)
    email: Mapped[str] = mapped_column(String, unique=True)
    profile_id: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=datetime.datetime.utcnow)

    photos: Mapped[list["Photo"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    styles: Mapped[list["ContentStyle"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    contents: Mapped[list["GeneratedContent"]] = relationship(back_populates="user", cascade="all, delete-orphan")


class Photo(Base):
    __tablename__ = "photos"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("studio_users.id"))
    url: Mapped[str] = mapped_column(String)
    filename: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=datetime.datetime.utcnow)

    user: Mapped[StudioUser] = relationship(back_populates="photos")


class ContentStyle(Base):
    __tablename__ = "content_styles"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("studio_users.id"))
    name: Mapped[str] = mapped_column(String)
    example_texts: Mapped[list] = mapped_column(JSON, default=list)  # liste de textes d'exemple
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=datetime.datetime.utcnow)

    user: Mapped[StudioUser] = relationship(back_populates="styles")


class GeneratedContent(Base):
    __tablename__ = "generated_content"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("studio_users.id"))
    style_id: Mapped[str | None] = mapped_column(ForeignKey("content_styles.id"), nullable=True)

    photo_urls: Mapped[list] = mapped_column(JSON, default=list)     # photos sources, dans l'ordre choisi
    composed_urls: Mapped[list] = mapped_column(JSON, default=list)  # photos finales (texte incrusté), uploadées chez Zernio
    caption: Mapped[str] = mapped_column(Text, default="")
    overlay_text: Mapped[str] = mapped_column(Text, default="")

    scheduled_for: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String, default="draft")  # draft | scheduled | published | failed
    zernio_post_id: Mapped[str | None] = mapped_column(String, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=datetime.datetime.utcnow)

    user: Mapped[StudioUser] = relationship(back_populates="contents")


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


def get_session() -> AsyncSession:
    return SessionLocal()


# ─── Helpers CRUD ────────────────────────────────────────────────────────────
async def get_user(session: AsyncSession, user_id: str) -> StudioUser | None:
    return await session.get(StudioUser, user_id)


async def get_user_by_email(session: AsyncSession, email: str) -> StudioUser | None:
    r = await session.execute(select(StudioUser).where(StudioUser.email == email))
    return r.scalar_one_or_none()


async def list_photos(session: AsyncSession, user_id: str) -> list[Photo]:
    r = await session.execute(
        select(Photo).where(Photo.user_id == user_id).order_by(Photo.created_at.desc())
    )
    return list(r.scalars())


async def list_styles(session: AsyncSession, user_id: str) -> list[ContentStyle]:
    r = await session.execute(
        select(ContentStyle).where(ContentStyle.user_id == user_id).order_by(ContentStyle.created_at.desc())
    )
    return list(r.scalars())


async def list_contents(session: AsyncSession, user_id: str) -> list[GeneratedContent]:
    r = await session.execute(
        select(GeneratedContent).where(GeneratedContent.user_id == user_id).order_by(GeneratedContent.scheduled_for)
    )
    return list(r.scalars())
