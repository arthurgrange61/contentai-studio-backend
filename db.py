"""
Couche de persistance — PostgreSQL (Render) via SQLAlchemy async.

Remplace le stockage en mémoire (perdu à chaque redéploiement) pour tout ce
qui doit survivre dans la durée : comptes Studio, bibliothèque de photos,
styles de contenu (avec exemples de textes) et file de contenus générés.
"""
import datetime
import os
import uuid

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text, select
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

    stripe_customer_id: Mapped[str | None] = mapped_column(String, nullable=True)
    stripe_subscription_id: Mapped[str | None] = mapped_column(String, nullable=True)
    plan: Mapped[str] = mapped_column(String, default="none")                   # none | starter | pro | business
    subscription_status: Mapped[str] = mapped_column(String, default="inactive")  # inactive | active | past_due | canceled

    photos: Mapped[list["Photo"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    styles: Mapped[list["ContentStyle"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    contents: Mapped[list["GeneratedContent"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    posting_rules: Mapped[list["PostingRule"]] = relationship(back_populates="user", cascade="all, delete-orphan")


class Photo(Base):
    __tablename__ = "photos"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("studio_users.id"))
    url: Mapped[str] = mapped_column(String)
    filename: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=datetime.datetime.utcnow)

    user: Mapped[StudioUser] = relationship(back_populates="photos")


class ContentStyle(Base):
    """
    Un style = une recette complète de génération de contenu, pas seulement
    un ton de texte : combien de photos par publication, sur laquelle
    incruster le texte, et si une musique doit être ajoutée (TikTok).
    """
    __tablename__ = "content_styles"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("studio_users.id"))
    name: Mapped[str] = mapped_column(String)
    example_texts: Mapped[list] = mapped_column(JSON, default=list)  # liste de textes d'exemple (ton pour l'IA)
    photo_count: Mapped[int] = mapped_column(Integer, default=1)     # nombre de photos par publication (carrousel)
    overlay_position: Mapped[str] = mapped_column(String, default="first")  # none | first | last | all
    music_enabled: Mapped[bool] = mapped_column(Boolean, default=False)     # musique auto TikTok
    text_style: Mapped[str] = mapped_column(String, default="outline")      # bubble | outline
    text_placement: Mapped[str] = mapped_column(String, default="top")      # top | center | belly | bottom
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=datetime.datetime.utcnow)

    user: Mapped[StudioUser] = relationship(back_populates="styles")


class PostingRule(Base):
    """Une règle récurrente : « chaque lundi à 9h, publie avec le style X »."""
    __tablename__ = "posting_rules"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("studio_users.id"))
    style_id: Mapped[str] = mapped_column(ForeignKey("content_styles.id"))
    day_of_week: Mapped[int] = mapped_column(Integer)  # 0 = lundi ... 6 = dimanche
    time: Mapped[str] = mapped_column(String)          # "HH:MM"
    account_ids: Mapped[list] = mapped_column(JSON, default=list)  # comptes Zernio cibles
    active: Mapped[bool] = mapped_column(Boolean, default=True)    # mise en pause sans supprimer
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=datetime.datetime.utcnow)

    user: Mapped[StudioUser] = relationship(back_populates="posting_rules")
    style: Mapped[ContentStyle] = relationship()


class GeneratedContent(Base):
    __tablename__ = "generated_content"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("studio_users.id"))
    style_id: Mapped[str | None] = mapped_column(ForeignKey("content_styles.id"), nullable=True)

    photo_urls: Mapped[list] = mapped_column(JSON, default=list)     # photos sources, dans l'ordre choisi
    composed_urls: Mapped[list] = mapped_column(JSON, default=list)  # photos finales (texte incrusté), uploadées chez Zernio
    account_ids: Mapped[list] = mapped_column(JSON, default=list)    # comptes Zernio cibles pour cette pièce
    caption: Mapped[str] = mapped_column(Text, default="")
    overlay_text: Mapped[str] = mapped_column(Text, default="")

    scheduled_for: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String, default="draft")  # draft | scheduled | published | failed
    zernio_post_id: Mapped[str | None] = mapped_column(String, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=datetime.datetime.utcnow)

    user: Mapped[StudioUser] = relationship(back_populates="contents")



# Migration additive minimaliste : create_all() ne modifie jamais une table
# déjà existante, donc les colonnes ajoutées après coup doivent être posées
# à la main. Chaque instruction est sans danger à rejouer (IF NOT EXISTS).
_MIGRATIONS = [
    "ALTER TABLE content_styles ADD COLUMN IF NOT EXISTS photo_count INTEGER DEFAULT 1",
    "ALTER TABLE content_styles ADD COLUMN IF NOT EXISTS overlay_position VARCHAR DEFAULT 'first'",
    "ALTER TABLE content_styles ADD COLUMN IF NOT EXISTS music_enabled BOOLEAN DEFAULT false",
    "ALTER TABLE generated_content ADD COLUMN IF NOT EXISTS account_ids JSON DEFAULT '[]'::json",
    "ALTER TABLE studio_users ADD COLUMN IF NOT EXISTS stripe_customer_id VARCHAR",
    "ALTER TABLE studio_users ADD COLUMN IF NOT EXISTS stripe_subscription_id VARCHAR",
    "ALTER TABLE studio_users ADD COLUMN IF NOT EXISTS plan VARCHAR DEFAULT 'none'",
    "ALTER TABLE studio_users ADD COLUMN IF NOT EXISTS subscription_status VARCHAR DEFAULT 'inactive'",
    "ALTER TABLE content_styles ADD COLUMN IF NOT EXISTS text_style VARCHAR DEFAULT 'outline'",
    "ALTER TABLE content_styles ADD COLUMN IF NOT EXISTS text_placement VARCHAR DEFAULT 'top'",
    "ALTER TABLE posting_rules ADD COLUMN IF NOT EXISTS active BOOLEAN DEFAULT true",
]


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        for statement in _MIGRATIONS:
            await conn.exec_driver_sql(statement)


def get_session() -> AsyncSession:
    return SessionLocal()


# ─── Helpers CRUD ────────────────────────────────────────────────────────────
async def get_user(session: AsyncSession, user_id: str) -> StudioUser | None:
    return await session.get(StudioUser, user_id)


async def get_user_by_email(session: AsyncSession, email: str) -> StudioUser | None:
    r = await session.execute(select(StudioUser).where(StudioUser.email == email))
    return r.scalar_one_or_none()


async def get_user_by_stripe_customer(session: AsyncSession, customer_id: str) -> StudioUser | None:
    r = await session.execute(select(StudioUser).where(StudioUser.stripe_customer_id == customer_id))
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


async def list_posting_rules(session: AsyncSession, user_id: str) -> list[PostingRule]:
    r = await session.execute(
        select(PostingRule)
        .where(PostingRule.user_id == user_id)
        .order_by(PostingRule.day_of_week, PostingRule.time)
    )
    return list(r.scalars())
