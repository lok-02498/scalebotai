"""SQLAlchemy ORM models: users, conversations, chats, feedback.

users 1──N conversations 1──N chats 1──0..1 feedback
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Identity,
    Index,
    Integer,
    MetaData,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    true,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# BIGINT in PostgreSQL; SQLite (unit tests) only auto-increments INTEGER keys.
BigIntId = BigInteger().with_variant(Integer, "sqlite")


def utcnow() -> datetime:
    return datetime.now(UTC)


# Deterministic constraint names (readable errors, migration-tool friendly).
NAMING_CONVENTION = {
    "pk": "pk_%(table_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "ix": "ix_%(table_name)s_%(column_0_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("username", name="uq_users_username"),
        UniqueConstraint("email", name="uq_users_email"),
        CheckConstraint("role IN ('user', 'admin')", name="role"),
        Index("ix_users_created_at", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    username: Mapped[str] = mapped_column(String(50), nullable=False)
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    full_name: Mapped[str | None] = mapped_column(String(100))
    role: Mapped[str] = mapped_column(
        String(20), nullable=False, default="user", server_default="user"
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=true()
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
        onupdate=utcnow,
        server_default=func.now(),
    )
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    conversations: Mapped[list["Conversation"]] = relationship(
        back_populates="user", cascade="all, delete-orphan", passive_deletes=True, lazy="raise"
    )


class Conversation(Base):
    __tablename__ = "conversations"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str] = mapped_column(
        String(200), nullable=False, default="New chat", server_default="New chat"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now()
    )

    user: Mapped[User] = relationship(back_populates="conversations", lazy="raise")
    chats: Mapped[list["Chat"]] = relationship(
        back_populates="conversation",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="Chat.timestamp",
        lazy="raise",
    )


class Chat(Base):
    """One question/answer exchange."""

    __tablename__ = "chats"
    __table_args__ = (
        Index("ix_chats_conversation_timestamp", "conversation_id", "timestamp"),
        Index("ix_chats_timestamp", "timestamp"),
    )

    id: Mapped[int] = mapped_column(BigIntId, Identity(), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    question: Mapped[str] = mapped_column(Text, nullable=False)
    response: Mapped[str] = mapped_column(Text, nullable=False)
    bot_mode: Mapped[str] = mapped_column(
        String(20), nullable=False, default="rule", server_default="rule"
    )
    latency_ms: Mapped[float | None] = mapped_column(Float)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now()
    )

    conversation: Mapped[Conversation] = relationship(back_populates="chats", lazy="raise")
    feedback: Mapped["Feedback | None"] = relationship(
        back_populates="chat",
        cascade="all, delete-orphan",
        passive_deletes=True,
        uselist=False,
        lazy="raise",
    )


class Feedback(Base):
    __tablename__ = "feedback"
    __table_args__ = (
        UniqueConstraint("chat_id", name="uq_feedback_chat_id"),
        CheckConstraint("rating BETWEEN 1 AND 5", name="rating"),
    )

    id: Mapped[int] = mapped_column(BigIntId, Identity(), primary_key=True)
    chat_id: Mapped[int] = mapped_column(
        BigIntId, ForeignKey("chats.id", ondelete="CASCADE"), nullable=False
    )
    rating: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    comment: Mapped[str | None] = mapped_column(String(1000))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now()
    )

    chat: Mapped[Chat] = relationship(back_populates="feedback", lazy="raise")


# Composite indexes with a DESC column need the column objects, so they are
# declared after the classes. They serve "newest first" listings per owner.
Index("ix_conversations_user_updated", Conversation.user_id, Conversation.updated_at.desc())
Index("ix_chats_user_timestamp", Chat.user_id, Chat.timestamp.desc())
