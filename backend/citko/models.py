import uuid

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


def uid():
    return str(uuid.uuid4())


JSON_DATA = JSON().with_variant(JSONB, "postgresql")


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200))
    password: Mapped[str] = mapped_column(String(255))
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    revision: Mapped[int] = mapped_column(Integer, default=0)


class Session(Base):
    __tablename__ = "sessions"
    token: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    csrf: Mapped[str] = mapped_column(String(64))
    expires: Mapped[object] = mapped_column(DateTime(timezone=True))
    last_seen: Mapped[object] = mapped_column(DateTime(timezone=True), server_default=func.now())
    absolute_expires: Mapped[object | None] = mapped_column(DateTime(timezone=True))


class Book(Base):
    __tablename__ = "books"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(500), default="Untitled")
    author: Mapped[str] = mapped_column(String(500), default="Unknown")
    description: Mapped[str] = mapped_column(Text, default="")
    publisher: Mapped[str] = mapped_column(String(500), default="")
    language: Mapped[str] = mapped_column(String(32), default="")
    published_at: Mapped[str | None] = mapped_column(String(64))
    identifiers: Mapped[dict] = mapped_column(JSON_DATA, default=dict)
    tags: Mapped[list] = mapped_column(JSON_DATA, default=list)
    collection_ids: Mapped[list] = mapped_column(JSON_DATA, default=list)
    favorite: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(20), default="processing")
    error: Mapped[str | None] = mapped_column(Text)
    reading_status: Mapped[str] = mapped_column(String(20), default="unread")
    completed_at: Mapped[str | None] = mapped_column(String(64))
    progress: Mapped[float] = mapped_column(Float, default=0)
    created_at: Mapped[object] = mapped_column(DateTime(timezone=True), server_default=func.now())
    opened_at: Mapped[object | None] = mapped_column(DateTime(timezone=True))
    revision: Mapped[int] = mapped_column(Integer, default=0)
    rendition_id: Mapped[str | None] = mapped_column(String(36))
    cover_key: Mapped[str | None] = mapped_column(String(512))
    deleted: Mapped[bool] = mapped_column(Boolean, default=False)


class BookFile(Base):
    __tablename__ = "book_files"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    book_id: Mapped[str] = mapped_column(ForeignKey("books.id", ondelete="CASCADE"), index=True)
    format: Mapped[str] = mapped_column(String(10))
    storage_name: Mapped[str] = mapped_column(String(512), unique=True)
    size: Mapped[int] = mapped_column(Integer, default=0)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    original: Mapped[bool] = mapped_column(Boolean, default=True)


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    user_id: Mapped[str] = mapped_column(String(36), index=True)
    book_id: Mapped[str] = mapped_column(String(36), index=True)
    status: Mapped[str] = mapped_column(String(20), default="queued")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    lease: Mapped[str | None] = mapped_column(String(36))
    lease_expires: Mapped[object | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)


class Change(Base):
    __tablename__ = "changes"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(36), index=True)
    revision: Mapped[int] = mapped_column(Integer)
    entity: Mapped[str] = mapped_column(String(30))
    entity_id: Mapped[str] = mapped_column(String(100))
    value: Mapped[dict] = mapped_column(JSON_DATA)
    __table_args__ = (UniqueConstraint("user_id", "revision"),)


class Receipt(Base):
    __tablename__ = "receipts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[str] = mapped_column(String(36))
    device_id: Mapped[str] = mapped_column(String(36))
    op_id: Mapped[str] = mapped_column(String(36))
    digest: Mapped[str] = mapped_column(String(64))
    result: Mapped[dict] = mapped_column(JSON)
    __table_args__ = (UniqueConstraint("user_id", "device_id", "op_id"),)


class Device(Base):
    __tablename__ = "devices"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(36), index=True)
    name: Mapped[str] = mapped_column(String(200))


class Position(Base):
    __tablename__ = "positions"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(36), index=True)
    book_id: Mapped[str] = mapped_column(String(36))
    device_id: Mapped[str] = mapped_column(String(36))
    rendition_id: Mapped[str] = mapped_column(String(36))
    locator: Mapped[dict] = mapped_column(JSON_DATA)
    progress: Mapped[float] = mapped_column(Float)
    revision: Mapped[int] = mapped_column(Integer)
    updated_at: Mapped[object] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Annotation(Base):
    __tablename__ = "annotations"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(36), index=True)
    book_id: Mapped[str] = mapped_column(String(36))
    value: Mapped[dict] = mapped_column(JSON_DATA)
    revision: Mapped[int] = mapped_column(Integer)
    deleted: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[object] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Collection(Base):
    __tablename__ = "collections"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    user_id: Mapped[str] = mapped_column(String(36), index=True)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    revision: Mapped[int] = mapped_column(Integer, default=0)


class Preference(Base):
    __tablename__ = "preferences"
    user_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON_DATA, default=dict)
    revision: Mapped[int] = mapped_column(Integer, default=0)


class Photo(Base):
    __tablename__ = "photos"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    file_name: Mapped[str] = mapped_column(String(512))
    caption: Mapped[str] = mapped_column(Text, default="")
    mime: Mapped[str] = mapped_column(String(100))
    storage_name: Mapped[str] = mapped_column(String(512), unique=True)
    thumb_name: Mapped[str | None] = mapped_column(String(512))
    size: Mapped[int] = mapped_column(Integer, default=0)
    thumb_size: Mapped[int] = mapped_column(Integer, default=0)
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    # EXIF values are stored denormalised so the gallery can render details without re-parsing the
    # original, and as plain strings/nullables so a missing tag is simply absent rather than guessed.
    taken_at: Mapped[str | None] = mapped_column(String(64))
    camera: Mapped[str | None] = mapped_column(String(200))
    lens: Mapped[str | None] = mapped_column(String(200))
    iso: Mapped[int | None] = mapped_column(Integer)
    aperture: Mapped[str | None] = mapped_column(String(32))
    shutter: Mapped[str | None] = mapped_column(String(32))
    focal_length: Mapped[str | None] = mapped_column(String(32))
    orientation: Mapped[int | None] = mapped_column(Integer)
    gps_lat: Mapped[float | None] = mapped_column(Float)
    gps_lon: Mapped[float | None] = mapped_column(Float)
    exif: Mapped[dict] = mapped_column(JSON_DATA, default=dict)
    tags: Mapped[list] = mapped_column(JSON_DATA, default=list)
    favorite: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[object] = mapped_column(DateTime(timezone=True), server_default=func.now())
    revision: Mapped[int] = mapped_column(Integer, default=0)
    deleted: Mapped[bool] = mapped_column(Boolean, default=False)


class Activity(Base):
    __tablename__ = "activities"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(36), index=True)
    book_id: Mapped[str] = mapped_column(String(36))
    session_id: Mapped[str] = mapped_column(String(36))
    sequence: Mapped[int] = mapped_column(Integer)
    active_ms: Mapped[int] = mapped_column(Integer)
    local_date: Mapped[str] = mapped_column(String(10))
    timezone_offset: Mapped[int] = mapped_column(Integer, default=0)
    received: Mapped[object] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (
        UniqueConstraint("user_id", "id"),
        UniqueConstraint("user_id", "session_id", "sequence"),
    )
