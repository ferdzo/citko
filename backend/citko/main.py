import hashlib
import io
import json
import logging
import math
import os
import secrets
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import StreamingResponse
from pwdlib import PasswordHash
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session as DBSession

from . import metadata, photos
from . import models as m
from .db import SessionLocal
from .storage import get_storage

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format='{"time":"%(asctime)s","level":"%(levelname)s","message":"%(message)s"}',
)
log = logging.getLogger("citko")
login_attempts: dict[str, list[datetime]] = {}
app = FastAPI(title="Citko API", version="1")
pwd = PasswordHash.recommended()
DEV = os.getenv("APP_ENV", "development") == "development"
ORIGINS = {
    origin.strip()
    for origin in os.getenv("ALLOWED_ORIGINS", "http://localhost:5173,http://localhost:8000").split(
        ","
    )
    if origin.strip()
}
MAX_UPLOAD = int(os.getenv("MAX_UPLOAD_BYTES", str(100 * 1024 * 1024)))


def db():
    with SessionLocal() as s:
        yield s


def err(code, message, status=400, details=None):
    raise HTTPException(
        status,
        {"error": {"code": code, "message": message, **({"details": details} if details else {})}},
    )


@app.exception_handler(HTTPException)
async def exception_handler(request, exc):
    detail = exc.detail
    return Response(
        content=json.dumps(
            detail
            if isinstance(detail, dict) and "error" in detail
            else {"error": {"code": "request_error", "message": str(detail)}}
        ),
        status_code=exc.status_code,
        media_type="application/json",
    )


@app.exception_handler(RequestValidationError)
async def validation_handler(request, exc):
    return Response(
        json.dumps(
            {
                "error": {
                    "code": "validation_error",
                    "message": "Request validation failed",
                    "details": exc.errors(),
                }
            }
        ),
        status_code=422,
        media_type="application/json",
    )


@app.exception_handler(Exception)
async def unexpected_handler(request, exc):
    log.exception("unhandled API error", exc_info=exc)
    return Response(
        '{"error":{"code":"internal_error","message":"Internal server error"}}',
        status_code=500,
        media_type="application/json",
    )


def user(request: Request, s: DBSession):
    token = request.cookies.get("citko_session")
    ses = s.get(m.Session, token) if token else None
    now = datetime.now(timezone.utc)
    if (
        not ses
        or ses.expires < now
        or (ses.absolute_expires and ses.absolute_expires < now)
        or (now - ses.last_seen).total_seconds() > 60 * 60 * 24 * 7
    ):
        err("unauthorized", "Authentication required", 401)
    ses.last_seen = now
    ses.expires = now + timedelta(days=7)
    s.commit()
    return s.get(m.User, ses.user_id)


def auth(request: Request, s: DBSession = Depends(db)):
    return user(request, s)


@app.middleware("http")
async def origin_check(request, call_next):
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = request.headers.get("origin")
        if not origin or origin not in ORIGINS:
            return Response(
                status_code=403,
                content='{"error":{"code":"origin","message":"Origin not allowed"}}',
                media_type="application/json",
            )
    return await call_next(request)


def user_json(u):
    return {"id": u.id, "email": u.email, "name": u.name}


def rev(u, s, entity, ident, value):
    # Serialize each user's revision allocator by locking the user row.
    locked = s.scalar(
        select(m.User)
        .where(m.User.id == u.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    locked.revision += 1
    u.revision = locked.revision
    s.add(
        m.Change(
            user_id=u.id, revision=locked.revision, entity=entity, entity_id=str(ident), value=value
        )
    )
    return locked.revision


def book_json(b, s):
    files = s.scalars(select(m.BookFile).where(m.BookFile.book_id == b.id)).all()
    active = next((f for f in files if f.id == b.rendition_id), None)
    return {
        "id": b.id,
        "title": b.title,
        "author": b.author,
        "description": b.description,
        "publisher": b.publisher,
        "language": b.language,
        "publishedAt": b.published_at,
        "identifiers": b.identifiers or {},
        "tags": b.tags or [],
        "collectionIds": b.collection_ids or [],
        "favorite": b.favorite,
        "status": b.status,
        "error": b.error,
        "readingStatus": b.reading_status,
        "completedAt": b.completed_at,
        "progress": b.progress,
        "createdAt": b.created_at.isoformat() if b.created_at else None,
        "openedAt": b.opened_at.isoformat() if b.opened_at else None,
        "revision": b.revision,
        "coverUrl": f"/api/v1/books/{b.id}/cover" if b.cover_key else None,
        "formats": list(dict.fromkeys(f.format for f in files)),
        "content": {
            "renditionId": active.id,
            "format": active.format,
            "sha256": active.sha256,
            "size": active.size,
            "url": f"/api/v1/books/{b.id}/file",
        }
        if active
        else None,
        "files": [
            {"id": f.id, "format": f.format, "size": f.size, "original": f.original} for f in files
        ],
    }


@app.get("/api/v1/health")
def health():
    return {"status": "ok"}


@app.get("/api/v1/health/ready")
def ready(s=Depends(db)):
    s.execute(select(func.count()).select_from(m.User))
    get_storage().check()
    return {"status": "ready"}


@app.get("/api/v1/auth/setup")
def setup_status(s=Depends(db)):
    return {"required": s.scalar(select(func.count()).select_from(m.User)) == 0}


class AuthBody(BaseModel):
    email: str
    password: str
    name: str | None = None


class SyncRequest(BaseModel):
    deviceId: uuid.UUID
    ops: list[dict] = Field(max_length=200)


def issue_session(u, s, response):
    token = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(32)
    s.add(
        m.Session(
            token=token,
            user_id=u.id,
            csrf=csrf,
            expires=datetime.now(timezone.utc) + timedelta(days=7),
            absolute_expires=datetime.now(timezone.utc) + timedelta(days=30),
        )
    )
    s.commit()
    response.set_cookie(
        "citko_session",
        token,
        httponly=True,
        samesite="lax",
        secure=not DEV,
        max_age=2592000,
        path="/",
    )
    return {"user": user_json(u), "csrfToken": csrf}


@app.post("/api/v1/auth/setup")
def setup(body: AuthBody, request: Request, response: Response, s=Depends(db)):
    s.execute(select(func.pg_advisory_xact_lock(173492017)))
    if s.scalar(select(func.count()).select_from(m.User)):
        err("setup_complete", "Initial setup is already complete", 409)
    token = os.getenv("SETUP_TOKEN")
    if not token or not secrets.compare_digest(request.headers.get("x-setup-token", ""), token):
        err("setup_token", "Valid setup token required", 403)
    if len(body.password) < 12:
        err("weak_password", "Password must be at least 12 characters")
    if not body.name:
        err("invalid_name", "Name is required")
    u = m.User(
        email=body.email.lower(), name=body.name, password=pwd.hash(body.password), is_admin=True
    )
    s.add(u)
    s.commit()
    return issue_session(u, s, response)


@app.post("/api/v1/auth/login")
def login(body: AuthBody, request: Request, response: Response, s=Depends(db)):
    # Rate limit by remote address; keep a bounded sliding window in each API process.
    key = request.client.host if request.client else "unknown"
    attempts = [
        t
        for t in login_attempts.get(key, [])
        if t > datetime.now(timezone.utc) - timedelta(minutes=15)
    ]
    if len(attempts) >= 10:
        err("rate_limited", "Too many login attempts", 429)
    attempts.append(datetime.now(timezone.utc))
    if len(login_attempts) >= 10_000:
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=15)
        stale = [ip for ip, history in login_attempts.items() if not history or history[-1] <= cutoff]
        for ip in stale:
            login_attempts.pop(ip, None)
    login_attempts[key] = attempts
    u = s.scalar(select(m.User).where(m.User.email == body.email.lower()))
    if not u or not pwd.verify(body.password, u.password):
        err("invalid_credentials", "Invalid email or password", 401)
    login_attempts[key] = []
    return issue_session(u, s, response)


@app.get("/api/v1/auth/me")
def me(request: Request, s=Depends(db), u=Depends(auth)):
    return {
        "user": user_json(u),
        "csrfToken": s.get(m.Session, request.cookies["citko_session"]).csrf,
    }


@app.get("/api/v1/auth/csrf")
def csrf(request: Request, s=Depends(db), u=Depends(auth)):
    return {"csrfToken": s.get(m.Session, request.cookies["citko_session"]).csrf}


@app.post("/api/v1/auth/logout")
def logout(request: Request, response: Response, s=Depends(db), u=Depends(auth)):
    s.delete(s.get(m.Session, request.cookies["citko_session"]))
    s.commit()
    response.delete_cookie("citko_session", path="/")
    return {"ok": True}


@app.middleware("http")
async def csrf_check(request, call_next):
    if (
        request.url.path.startswith("/api/v1")
        and request.method not in ("GET", "HEAD", "OPTIONS")
        and request.url.path not in ("/api/v1/auth/login", "/api/v1/auth/setup")
    ):
        token = request.cookies.get("citko_session")
        if token:
            with SessionLocal() as s:
                ses = s.get(m.Session, token)
                if not ses or not secrets.compare_digest(
                    request.headers.get("x-csrf-token", ""), ses.csrf
                ):
                    return Response(
                        status_code=403,
                        content='{"error":{"code":"csrf","message":"Valid CSRF token required"}}',
                        media_type="application/json",
                    )
    return await call_next(request)


@app.post("/api/v1/books", status_code=201)
async def upload(file: UploadFile = File(...), s=Depends(db), u=Depends(auth)):
    name = Path(file.filename or "book").name
    fmt = Path(name).suffix.lower().lstrip(".")
    if fmt not in ("epub", "pdf"):
        err("unsupported_format", "Only verified EPUB and PDF ingestion is enabled", 415)
    temp = tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024)
    hasher = hashlib.sha256()
    size = 0
    while chunk := await file.read(1024 * 1024):
        size += len(chunk)
        if size > MAX_UPLOAD:
            temp.close()
            err("file_too_large", "Upload exceeds configured size limit", 413)
        hasher.update(chunk)
        temp.write(chunk)
    if not size:
        temp.close()
        err("empty_file", "Uploaded file is empty")
    digest = hasher.hexdigest()
    temp.seek(0)
    prefix = temp.read(16)
    temp.seek(0)
    signatures = {
        "pdf": prefix.startswith(b"%PDF-"),
        "epub": prefix.startswith(b"PK\x03\x04"),
    }
    if not signatures.get(fmt, False):
        temp.close()
        err("invalid_file", "File signature does not match the declared format", 415)
    u = s.scalar(
        select(m.User)
        .where(m.User.id == u.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    existing = s.scalar(
        select(m.BookFile)
        .join(m.Book)
        .where(m.Book.user_id == u.id, m.BookFile.sha256 == digest, m.Book.deleted.is_(False))
    )
    if existing:
        err("duplicate_file", "This file already exists", 409, {"bookId": existing.book_id})
    book_id = str(uuid.uuid4())
    storage_name = f"{u.id}/{book_id}/{uuid.uuid4()}/{fmt}"
    temp.seek(0)
    get_storage().put(storage_name, temp, size, file.content_type or "application/octet-stream")
    temp.close()
    b = m.Book(id=book_id, user_id=u.id, title=Path(name).stem, author="Unknown", status="queued")
    s.add(b)
    s.flush()
    f = m.BookFile(
        book_id=b.id,
        format=fmt,
        storage_name=storage_name,
        size=size,
        sha256=digest,
        original=True,
    )
    s.add(f)
    s.flush()
    b.rendition_id = f.id
    job = m.Job(user_id=u.id, book_id=b.id)
    s.add(job)
    u = s.scalar(
        select(m.User)
        .where(m.User.id == u.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    b.revision = u.revision + 1
    b.revision = rev(u, s, "book", b.id, book_json(b, s))
    s.commit()
    return {"book": book_json(b, s), "jobId": job.id}


@app.get("/api/v1/books")
def books(
    page: int = 1,
    pageSize: int = 48,
    q: str = "",
    status: str | None = None,
    format: str | None = None,
    tag: str | None = None,
    collectionId: str | None = None,
    author: str | None = None,
    favorite: bool | None = None,
    sort: str = "added",
    direction: str = "desc",
    s=Depends(db),
    u=Depends(auth),
):
    page = max(page, 1)
    pageSize = min(max(pageSize, 1), 100)
    query = select(m.Book).where(m.Book.user_id == u.id, m.Book.deleted.is_(False))
    if q:
        query = query.where(or_(m.Book.title.ilike(f"%{q}%"), m.Book.author.ilike(f"%{q}%")))
    if status:
        query = query.where(m.Book.reading_status == status)
    if author:
        query = query.where(m.Book.author.ilike(f"%{author}%"))
    if favorite is not None:
        query = query.where(m.Book.favorite == favorite)
    if tag:
        query = query.where(m.Book.tags.op("@>")([tag]))
    if collectionId:
        query = query.where(m.Book.collection_ids.op("@>")([collectionId]))
    if format:
        query = query.where(
            m.Book.id.in_(select(m.BookFile.book_id).where(m.BookFile.format == format))
        )
    total = s.scalar(select(func.count()).select_from(query.subquery()))
    col = {
        "title": m.Book.title,
        "author": m.Book.author,
        "progress": m.Book.progress,
        "opened": m.Book.opened_at,
        "added": m.Book.created_at,
    }.get(sort, m.Book.created_at)
    query = (
        query.order_by(col.asc() if direction == "asc" else col.desc())
        .offset((page - 1) * pageSize)
        .limit(pageSize)
    )
    return {
        "items": [book_json(b, s) for b in s.scalars(query)],
        "total": total,
        "page": page,
        "pageSize": pageSize,
    }


def owned_book(s, u, bid):
    b = s.scalar(
        select(m.Book).where(m.Book.id == bid, m.Book.user_id == u.id, m.Book.deleted.is_(False))
    )
    if not b:
        err("not_found", "Book not found", 404)
    return b


def validate_locator(book_file, locator):
    if not isinstance(locator, dict):
        raise ValueError("invalid_locator")
    fmt = book_file.format.lower()
    if fmt == "pdf":
        if (
            locator.get("type") != "pdf"
            or type(locator.get("page")) is not int
            or locator["page"] < 0
            or set(locator) != {"type", "page", "y"}
        ):
            raise ValueError("invalid_locator")
        y = locator.get("y")
        if not isinstance(y, (int, float)) or not math.isfinite(y) or not 0 <= y <= 1:
            raise ValueError("invalid_locator")
    elif fmt in ("epub", "mobi", "azw3", "fb2"):
        if (
            locator.get("type") != "epub"
            or not isinstance(locator.get("cfi"), str)
            or not locator["cfi"].startswith("epubcfi(")
            or len(locator["cfi"]) > 4096
            or len(locator["cfi"]) < 12
            or set(locator) != {"type", "cfi"}
        ):
            raise ValueError("invalid_locator")
    else:
        raise ValueError("unsupported_locator_format")


def validate_anchor(book_file, annotation):
    anchor = annotation.get("anchor")
    if anchor is None:
        return
    if not isinstance(anchor, dict) or not isinstance(anchor.get("quote"), str):
        raise ValueError("invalid_anchor")
    if len(anchor["quote"]) > 20000 or any(
        not isinstance(annotation.get(key, ""), str) or len(annotation.get(key, "")) > 4000
        for key in ("quote", "prefix", "suffix")
    ):
        raise ValueError("invalid_anchor")
    if anchor["quote"] != annotation.get("quote", ""):
        raise ValueError("invalid_anchor")
    for key in ("prefix", "suffix"):
        if key in anchor and (not isinstance(anchor[key], str) or len(anchor[key]) > 4000):
            raise ValueError("invalid_anchor")
    if book_file.format == "pdf":
        rects = anchor.get("rects")
        if not isinstance(rects, list) or not 1 <= len(rects) <= 1000 or set(anchor) - {"rects", "quote", "prefix", "suffix"}:
            raise ValueError("invalid_anchor")
        for rect in rects:
            if not isinstance(rect, dict) or type(rect.get("page")) is not int or rect["page"] < 0 or set(rect) != {"page", "x", "y", "width", "height"}:
                raise ValueError("invalid_anchor")
            vals = {}
            for key in ("x", "y", "width", "height"):
                val = rect.get(key)
                if (
                    not isinstance(val, (int, float))
                    or not math.isfinite(val)
                    or not 0 <= val <= 1
                    or (key in ("width", "height") and val == 0)
                ):
                    raise ValueError("invalid_anchor")
                vals[key] = val
            if vals["x"] + vals["width"] > 1 or vals["y"] + vals["height"] > 1:
                raise ValueError("invalid_anchor")
    elif (
        not isinstance(anchor.get("cfi"), str)
        or not anchor["cfi"].startswith("epubcfi(")
        or len(anchor["cfi"]) > 4096
        or len(anchor["cfi"]) < 12
        or not anchor["cfi"].endswith(")")
        or set(anchor) - {"cfi", "quote", "prefix", "suffix"}
    ):
        raise ValueError("invalid_anchor")


def normalize_book_patch(s, u, patch):
    if not isinstance(patch, dict):
        raise ValueError("invalid_patch")
    fields = {
        "title": "title", "author": "author", "description": "description",
        "publisher": "publisher", "language": "language", "publishedAt": "published_at",
        "identifiers": "identifiers", "tags": "tags", "collectionIds": "collection_ids",
        "favorite": "favorite", "readingStatus": "reading_status",
        "completedAt": "completed_at", "openedAt": "opened_at",
    }
    normalized = {}
    for key, value in patch.items():
        if key not in fields:
            raise ValueError("invalid_field")
        if key in ("title", "author", "publisher", "language", "description"):
            max_length = {"title": 500, "author": 500, "publisher": 500, "language": 32, "description": 200000}[key]
            if not isinstance(value, str) or not value.strip() or len(value) > max_length:
                raise ValueError("invalid_" + key)
        elif key == "publishedAt":
            if value is not None and (not isinstance(value, str) or len(value) > 64):
                raise ValueError("invalid_published_at")
        elif key == "identifiers":
            if not isinstance(value, dict) or len(value) > 50 or any(not isinstance(k, str) or not isinstance(v, str) or len(k) > 100 or len(v) > 500 for k, v in value.items()):
                raise ValueError("invalid_identifiers")
        elif key == "tags":
            if not isinstance(value, list) or len(value) > 100 or any(not isinstance(tag, str) or not tag.strip() or len(tag) > 100 for tag in value):
                raise ValueError("invalid_tags")
            value = list(dict.fromkeys(tag.strip() for tag in value))
        elif key == "collectionIds":
            if not isinstance(value, list) or len(value) > 100:
                raise ValueError("invalid_collection")
            try:
                value = [str(uuid.UUID(item)) for item in value]
            except (TypeError, ValueError, AttributeError) as exc:
                raise ValueError("invalid_collection") from exc
            owned = set(s.scalars(select(m.Collection.id).where(m.Collection.user_id == u.id, m.Collection.id.in_(value)))) if value else set()
            if owned != set(value):
                raise ValueError("invalid_collection")
        elif key == "favorite":
            if type(value) is not bool:
                raise ValueError("invalid_favorite")
        elif key == "readingStatus":
            if value not in ("unread", "reading", "completed", "abandoned"):
                raise ValueError("invalid_reading_status")
        elif key in ("completedAt", "openedAt"):
            if value is not None:
                if not isinstance(value, str):
                    raise ValueError("invalid_timestamp")
                try:
                    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                except ValueError as exc:
                    raise ValueError("invalid_timestamp") from exc
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                value = parsed if key == "openedAt" else parsed.astimezone(timezone.utc).isoformat()
        normalized[fields[key]] = value
    return normalized


@app.get("/api/v1/books/{bid}")
def get_book(bid: str, s=Depends(db), u=Depends(auth)):
    return book_json(owned_book(s, u, bid), s)


@app.patch("/api/v1/books/{bid}")
def patch_book(bid: str, payload: dict, s=Depends(db), u=Depends(auth)):
    b = owned_book(s, u, bid)
    u = s.scalar(
        select(m.User)
        .where(m.User.id == u.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    s.refresh(b)
    base = payload.pop("baseRevision", None)
    if base != b.revision:
        err("conflict", "Book revision conflict", 409, {"current": book_json(b, s)})
    fields = {
        "title": "title",
        "author": "author",
        "description": "description",
        "publisher": "publisher",
        "language": "language",
        "publishedAt": "published_at",
        "identifiers": "identifiers",
        "tags": "tags",
        "collectionIds": "collection_ids",
        "favorite": "favorite",
        "readingStatus": "reading_status",
        "completedAt": "completed_at",
        "openedAt": "opened_at",
    }
    for k, v in payload.items():
        if k not in fields:
            err("invalid_field", f"Field {k} cannot be changed")
        if k in ("tags", "collectionIds") and not isinstance(v, list):
            err("invalid_field", f"{k} must be an array")
        if k == "readingStatus" and v not in ("unread", "reading", "completed", "abandoned"):
            err("invalid_reading_status", "Unsupported reading status")
        if k == "collectionIds":
            owned_ids = set(
                s.scalars(
                    select(m.Collection.id).where(
                        m.Collection.user_id == u.id, m.Collection.id.in_(v)
                    )
                )
            )
            if set(v) != owned_ids:
                err("invalid_collection", "Collection not found", 404)
        if k == "openedAt" and v:
            try:
                v = datetime.fromisoformat(v.replace("Z", "+00:00"))
            except (TypeError, ValueError):
                err("invalid_timestamp", "openedAt must be an ISO timestamp")
        setattr(b, fields[k], v)
    b.revision = u.revision + 1
    b.revision = rev(u, s, "book", b.id, book_json(b, s))
    s.commit()
    return book_json(b, s)


@app.delete("/api/v1/books/{bid}")
def delete_book(bid: str, baseRevision: int, s=Depends(db), u=Depends(auth)):
    b = owned_book(s, u, bid)
    u = s.scalar(
        select(m.User)
        .where(m.User.id == u.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    s.refresh(b)
    if b.revision != baseRevision:
        err("conflict", "Book revision conflict", 409, {"current": book_json(b, s)})
    b.deleted = True
    b.revision = u.revision + 1
    b.revision = rev(u, s, "book", b.id, {"id": b.id, "deleted": True})
    s.commit()
    return {"ok": True}


def stream_object(key, size, digest, content_type, request):
    """Byte-range aware private streaming for a single stored object."""
    etag = f'"{digest}"'
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag})
    start, end, status = 0, size - 1, 200
    r = request.headers.get("range")
    if (
        r
        and (not request.headers.get("if-range") or request.headers["if-range"] == etag)
        and r.startswith("bytes=")
    ):
        try:
            a, z = r[6:].split("-")
            if a:
                start = int(a)
                end = min(int(z) if z else end, end)
            else:
                suffix = int(z)
                if suffix <= 0:
                    raise ValueError()
                start = max(size - suffix, 0)
            if start > end or start >= size:
                raise ValueError()
            status = 206
        except ValueError:
            return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})
    headers = {
        "Accept-Ranges": "bytes",
        "ETag": etag,
        "Content-Length": str(end - start + 1),
        "Cache-Control": "private, no-store",
        "Content-Disposition": "inline",
    }
    if status == 206:
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"

    def chunks():
        response = get_storage().get(
            key, start if status == 206 else None, end if status == 206 else None
        )
        body = response["Body"]
        try:
            yield from body.iter_chunks(chunk_size=1024 * 1024)
        finally:
            body.close()

    return StreamingResponse(chunks(), status_code=status, media_type=content_type, headers=headers)


def send_file(f, request):
    return stream_object(
        f.storage_name,
        f.size,
        f.sha256,
        {"epub": "application/epub+zip", "pdf": "application/pdf"}.get(
            f.format, "application/octet-stream"
        ),
        request,
    )


@app.get("/api/v1/books/{bid}/file")
def book_file(bid: str, request: Request, s=Depends(db), u=Depends(auth)):
    b = owned_book(s, u, bid)
    f = s.get(m.BookFile, b.rendition_id)
    if not f:
        err("not_found", "Book file not found", 404)
    return send_file(f, request)


@app.get("/api/v1/books/{bid}/original")
def original_file(bid: str, request: Request, s=Depends(db), u=Depends(auth)):
    b = owned_book(s, u, bid)
    f = s.scalar(
        select(m.BookFile).where(m.BookFile.book_id == b.id, m.BookFile.original.is_(True))
    )
    if not f:
        err("not_found", "Original not found", 404)
    return send_file(f, request)


@app.post("/api/v1/books/{bid}/files", status_code=201)
async def attach_file(bid: str, file: UploadFile = File(...), s=Depends(db), u=Depends(auth)):
    book = owned_book(s, u, bid)
    fmt = Path(Path(file.filename or "").name).suffix.lower().lstrip(".")
    if fmt not in ("epub", "pdf"):
        err("unsupported_format", "Only verified EPUB and PDF ingestion is enabled", 415)
    temp = tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024)
    hasher = hashlib.sha256()
    size = 0
    while chunk := await file.read(1024 * 1024):
        size += len(chunk)
        if size > MAX_UPLOAD:
            temp.close()
            err("file_too_large", "Upload exceeds configured size limit", 413)
        temp.write(chunk)
        hasher.update(chunk)
    digest = hasher.hexdigest()
    if not size:
        temp.close()
        err("empty_file", "Uploaded file is empty")
    temp.seek(0)
    prefix = temp.read(16)
    temp.seek(0)
    valid = {"epub": prefix.startswith(b"PK\x03\x04"), "pdf": prefix.startswith(b"%PDF-")}
    if not valid.get(fmt):
        temp.close()
        err("invalid_file", "File signature does not match the declared format", 415)
    u = s.scalar(
        select(m.User)
        .where(m.User.id == u.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    s.refresh(book)
    if book.deleted:
        temp.close()
        err("not_found", "Book not found", 404)
    if s.scalar(
        select(m.BookFile)
        .join(m.Book)
        .where(m.Book.user_id == u.id, m.BookFile.sha256 == digest, m.Book.deleted.is_(False))
    ):
        temp.close()
        err("duplicate_file", "This file already exists", 409, {"bookId": bid})
    key = f"{u.id}/{book.id}/{uuid.uuid4()}/{fmt}"
    get_storage().put(key, temp, size, file.content_type or "application/octet-stream")
    temp.close()
    stored = m.BookFile(
        book_id=book.id, format=fmt, storage_name=key, size=size, sha256=digest, original=True
    )
    s.add(stored)
    s.flush()
    book.revision = u.revision + 1
    book.revision = rev(u, s, "book", book.id, book_json(book, s))
    s.commit()
    return {"id": stored.id, "format": stored.format, "size": stored.size, "original": True}


@app.get("/api/v1/books/{bid}/cover")
def book_cover(bid: str, request: Request, s=Depends(db), u=Depends(auth)):
    book = owned_book(s, u, bid)
    if not book.cover_key:
        err("not_found", "Cover not available for this book", 404)
    obj = get_storage().get(book.cover_key)
    body = obj["Body"]

    def cover_chunks():
        try:
            yield from body.iter_chunks(128 * 1024)
        finally:
            body.close()

    return StreamingResponse(
        cover_chunks(),
        media_type=obj.get("ContentType", "image/jpeg"),
        headers={"Cache-Control": "private, max-age=3600"},
    )


@app.post("/api/v1/books/{bid}/metadata/refresh")
def refresh_metadata(bid: str, s=Depends(db), u=Depends(auth)):
    """Fill missing details and cover art from open bibliographic sources (Open Library, Google Books)."""
    b = owned_book(s, u, bid)
    u = s.scalar(
        select(m.User)
        .where(m.User.id == u.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    s.refresh(b)
    found = metadata.enrich(
        isbn=metadata.isbn_of(b.identifiers),
        title=b.title,
        author=b.author,
        want_cover=not b.cover_key,
    )
    if not found:
        err("metadata_not_found", "No matching public record was found", 404)
    filled = []
    for attr, value in found.fields.items():
        # Only fill what neither the file nor the reader supplied: enrichment never overwrites edits.
        if value and not getattr(b, attr):
            setattr(b, attr, value)
            filled.append(attr)
    if found.cover_bytes and not b.cover_key:
        key = f"covers/{u.id}/{b.id}"
        get_storage().put(
            key, io.BytesIO(found.cover_bytes), len(found.cover_bytes), found.cover_type or "image/jpeg"
        )
        b.cover_key = key
        filled.append("cover")
    b.revision = u.revision + 1
    b.revision = rev(u, s, "book", b.id, book_json(b, s))
    s.commit()
    return {
        "book": book_json(b, s),
        "source": found.source,
        "sourceUrl": found.source_url,
        "filled": sorted(filled),
    }


MAX_PHOTO = int(os.getenv("MAX_PHOTO_BYTES", str(40 * 1024 * 1024)))


def photo_json(p):
    return {
        "id": p.id,
        "caption": p.caption or None,
        "fileName": p.file_name,
        "mime": p.mime,
        "size": p.size,
        "sha256": p.sha256,
        "width": p.width,
        "height": p.height,
        "takenAt": p.taken_at,
        "camera": p.camera,
        "lens": p.lens,
        "iso": p.iso,
        "aperture": p.aperture,
        "shutter": p.shutter,
        "focalLength": p.focal_length,
        "orientation": p.orientation,
        "gpsLat": p.gps_lat,
        "gpsLon": p.gps_lon,
        "tags": p.tags or [],
        "favorite": p.favorite,
        "createdAt": p.created_at,
        "revision": p.revision,
        "fileUrl": f"/api/v1/photos/{p.id}/file",
        "thumbnailUrl": f"/api/v1/photos/{p.id}/thumbnail",
    }


def owned_photo(s, u, pid):
    p = s.scalar(
        select(m.Photo).where(
            m.Photo.id == pid, m.Photo.user_id == u.id, m.Photo.deleted.is_(False)
        )
    )
    if not p:
        err("not_found", "Photo not found", 404)
    return p


@app.post("/api/v1/photos", status_code=201)
async def upload_photo(
    file: UploadFile = File(...), caption: str = Form(""), s=Depends(db), u=Depends(auth)
):
    """Store one photo plus a display thumbnail, keeping whatever EXIF the file carries.

    Photos stay out of the offline sync journal on purpose: like covers and imports they are a
    server-side library, so bumping the user revision here would push entities the sync client has no
    handling for.
    """
    name = Path(file.filename or "photo").name
    mime = (file.content_type or "").split(";")[0].strip().lower()
    if mime not in photos.ALLOWED_MIME:
        mime = photos.EXTENSION_MIME.get(Path(name).suffix.lower(), "")
    if mime not in photos.ALLOWED_MIME:
        err("unsupported_photo", "Only JPEG, PNG, WebP, GIF, TIFF and BMP images are supported", 415)
    temp = tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024)
    hasher = hashlib.sha256()
    size = 0
    while chunk := await file.read(1024 * 1024):
        size += len(chunk)
        if size > MAX_PHOTO:
            err("too_large", f"Photos must be under {MAX_PHOTO // (1024 * 1024)} MB", 413)
        hasher.update(chunk)
        temp.write(chunk)
    if not size:
        err("empty_file", "The uploaded file is empty", 400)
    temp.seek(0)
    data = temp.read()
    temp.close()
    try:
        facts = photos.inspect(data)
        rendered = photos.thumbnail(data)
    except ValueError as exc:
        if str(exc) == "image_too_large":
            err("image_too_large", "That image has too many pixels to process", 413)
        err("unsupported_photo", "That file could not be read as an image", 415)
    except Exception:
        err("unsupported_photo", "That file could not be read as an image", 415)
    photo_id = m.uid()
    key = f"photos/{u.id}/{photo_id}"
    thumb_key = f"{key}-thumb.jpg"
    get_storage().put(key, io.BytesIO(data), len(data), mime)
    get_storage().put(thumb_key, io.BytesIO(rendered), len(rendered), "image/jpeg")
    photo = m.Photo(
        id=photo_id,
        user_id=u.id,
        file_name=name[:512],
        caption=caption[:4000],
        mime=mime,
        storage_name=key,
        thumb_name=thumb_key,
        thumb_size=len(rendered),
        size=len(data),
        sha256=hasher.hexdigest(),
        revision=1,
        **facts,
    )
    s.add(photo)
    s.commit()
    return {"photo": photo_json(photo)}


@app.get("/api/v1/photos")
def list_photos(
    page: int = 1,
    pageSize: int = 60,
    sort: str = "taken",
    favorite: bool = False,
    s=Depends(db),
    u=Depends(auth),
):
    base = select(m.Photo).where(m.Photo.user_id == u.id, m.Photo.deleted.is_(False))
    if favorite:
        base = base.where(m.Photo.favorite.is_(True))
    total = s.scalar(select(func.count()).select_from(base.subquery()))
    page = max(1, page)
    pageSize = max(1, min(200, pageSize))
    if sort == "added":
        ordered = base.order_by(m.Photo.created_at.desc(), m.Photo.id.desc())
    else:
        # Undated photos sort last so a real capture date always leads the gallery.
        ordered = base.order_by(
            m.Photo.taken_at.is_(None), m.Photo.taken_at.desc(), m.Photo.created_at.desc()
        )
    items = s.scalars(ordered.offset((page - 1) * pageSize).limit(pageSize)).all()
    return {
        "items": [photo_json(p) for p in items],
        "total": total,
        "page": page,
        "pageSize": pageSize,
    }


@app.get("/api/v1/photos/{pid}/file")
def photo_file(pid: str, request: Request, s=Depends(db), u=Depends(auth)):
    p = owned_photo(s, u, pid)
    return stream_object(p.storage_name, p.size, p.sha256, p.mime, request)


@app.get("/api/v1/photos/{pid}/thumbnail")
def photo_thumbnail(pid: str, request: Request, s=Depends(db), u=Depends(auth)):
    p = owned_photo(s, u, pid)
    if not p.thumb_name:
        return stream_object(p.storage_name, p.size, p.sha256, p.mime, request)
    return stream_object(p.thumb_name, p.thumb_size, f"{p.sha256}-thumb", "image/jpeg", request)


@app.patch("/api/v1/photos/{pid}")
def patch_photo(pid: str, payload: dict, s=Depends(db), u=Depends(auth)):
    p = owned_photo(s, u, pid)
    if "caption" in payload:
        p.caption = str(payload["caption"] or "")[:4000]
    if "favorite" in payload:
        p.favorite = bool(payload["favorite"])
    if "tags" in payload:
        value = payload["tags"]
        if not isinstance(value, list):
            err("invalid_tags", "Tags must be a list of strings", 422)
        p.tags = [str(tag)[:100] for tag in value][:50]
    p.revision += 1
    s.commit()
    return {"photo": photo_json(p)}


@app.delete("/api/v1/photos/{pid}", status_code=204)
def delete_photo(pid: str, s=Depends(db), u=Depends(auth)):
    """Soft delete, matching books: the original stays in storage and an admin can recover it."""
    p = owned_photo(s, u, pid)
    p.deleted = True
    p.revision += 1
    s.commit()
    return Response(status_code=204)


@app.get("/api/v1/imports")
def imports(page: int = 1, pageSize: int = 50, s=Depends(db), u=Depends(auth)):
    q = select(m.Job).where(m.Job.user_id == u.id)
    total = s.scalar(select(func.count()).select_from(q.subquery()))
    jobs = s.scalars(q.order_by(m.Job.id).offset((page - 1) * pageSize).limit(pageSize)).all()
    return {
        "items": [
            {
                "id": j.id,
                "bookId": j.book_id,
                "status": j.status,
                "attempts": j.attempts,
                "error": j.error,
            }
            for j in jobs
        ],
        "total": total,
        "page": page,
        "pageSize": pageSize,
    }


@app.post("/api/v1/imports/{jid}/retry")
def retry(jid: str, s=Depends(db), u=Depends(auth)):
    u = s.scalar(
        select(m.User)
        .where(m.User.id == u.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    j = s.scalar(select(m.Job).where(m.Job.id == jid, m.Job.user_id == u.id))
    if not j:
        err("not_found", "Job not found", 404)
    if j.status != "failed":
        err("invalid_state", "Only failed jobs can be retried", 409)
    j.status = "queued"
    j.attempts = 0
    j.error = None
    book = s.scalar(select(m.Book).where(m.Book.id == j.book_id, m.Book.user_id == u.id))
    if book and not book.deleted:
        book.status = "queued"
        book.error = None
        book.revision = u.revision + 1
        book.revision = rev(u, s, "book", book.id, book_json(book, s))
    s.commit()
    return {"id": j.id, "status": j.status}


@app.post("/api/v1/devices")
def register_device(body: dict, s=Depends(db), u=Depends(auth)):
    try:
        uuid.UUID(body["id"])
    except Exception:
        err("invalid_device", "Device id must be a UUID")
    d = s.get(m.Device, body["id"])
    if d and d.user_id != u.id:
        err("device_owned", "Device belongs to another user", 409)
    if not d:
        s.add(m.Device(id=body["id"], user_id=u.id, name=str(body.get("name", "Device"))[:200]))
    s.commit()
    return {"id": body["id"], "name": body.get("name", "Device")}


@app.get("/api/v1/devices")
def devices(s=Depends(db), u=Depends(auth)):
    return {
        "items": [
            {"id": d.id, "name": d.name}
            for d in s.scalars(select(m.Device).where(m.Device.user_id == u.id))
        ]
    }


@app.get("/api/v1/sync/changes")
def changes(after: int = 0, limit: int = 500, s=Depends(db), u=Depends(auth)):
    limit = min(max(limit, 1), 1000)
    rows = s.scalars(
        select(m.Change)
        .where(m.Change.user_id == u.id, m.Change.revision > after)
        .order_by(m.Change.revision)
        .limit(limit + 1)
    ).all()
    more = len(rows) > limit
    rows = rows[:limit]
    return {
        "changes": [
            {"revision": c.revision, "entity": c.entity, "id": c.entity_id, "value": c.value}
            for c in rows
        ],
        "nextCursor": rows[-1].revision if rows else after,
        "hasMore": more,
    }


@app.post("/api/v1/sync/ops")
def sync_ops(body: SyncRequest, s=Depends(db), u=Depends(auth)):
    device = str(body.deviceId)
    d = s.get(m.Device, device)
    if not d or d.user_id != u.id:
        err("invalid_device", "Register this device before syncing", 403)
    output = []
    for op in body.ops:
        u = s.scalar(
            select(m.User)
            .where(m.User.id == u.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if u is None:
            err("unauthorized", "Authentication required", 401)
        raw = json.dumps(op, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(raw.encode()).hexdigest()
        opid = op.get("opId")
        try:
            uuid.UUID(str(opid))
        except Exception:
            output.append(
                {
                    "opId": opid,
                    "status": "rejected",
                    "code": "invalid_id",
                    "message": "opId must be UUID",
                }
            )
            continue
        existing = s.scalar(
            select(m.Receipt).where(
                m.Receipt.user_id == u.id, m.Receipt.device_id == device, m.Receipt.op_id == opid
            )
        )
        if existing:
            output.append(
                existing.result
                if existing.digest == digest
                else {
                    "opId": opid,
                    "status": "rejected",
                    "code": "id_reuse",
                    "message": "Operation id reused with different payload",
                }
            )
            continue
        try:
            kind = op.get("kind")
            value = op.get("value", {})
            entity = ""
            ident = ""
            current = None
            if kind in ("book.patch", "book.delete"):
                b = owned_book(s, u, op.get("bookId"))
                ident = b.id
                entity = "book"
                if op.get("baseRevision") != b.revision:
                    raise ValueError(json.dumps({"conflict": True, "current": book_json(b, s)}))
                if kind == "book.delete":
                    b.deleted = True
                    b.revision = u.revision + 1
                    snapshot = {"id": b.id, "deleted": True}
                else:
                    fields = {
                        "title": "title",
                        "author": "author",
                        "description": "description",
                        "publisher": "publisher",
                        "language": "language",
                        "publishedAt": "published_at",
                        "identifiers": "identifiers",
                        "tags": "tags",
                        "collectionIds": "collection_ids",
                        "favorite": "favorite",
                        "readingStatus": "reading_status",
                        "completedAt": "completed_at",
                        "openedAt": "opened_at",
                    }
                    for k, v in value.items():
                        if k not in fields:
                            raise ValueError("invalid_field")
                        if k in ("tags", "collectionIds") and not isinstance(v, list):
                            raise ValueError("invalid_field")
                        if k == "collectionIds":
                            owned_ids = (
                                set(
                                    s.scalars(
                                        select(m.Collection.id).where(
                                            m.Collection.user_id == u.id, m.Collection.id.in_(v)
                                        )
                                    )
                                )
                                if v
                                else set()
                            )
                            if set(v) != owned_ids:
                                raise ValueError("invalid_collection")
                        if k == "readingStatus" and v not in (
                            "unread",
                            "reading",
                            "completed",
                            "abandoned",
                        ):
                            raise ValueError("invalid_reading_status")
                        if k == "openedAt" and v:
                            try:
                                v = datetime.fromisoformat(v.replace("Z", "+00:00"))
                            except (TypeError, ValueError) as exc:
                                raise ValueError("invalid_timestamp") from exc
                        setattr(b, fields[k], v)
                    if "openedAt" in value and value["openedAt"]:
                        b.opened_at = datetime.fromisoformat(
                            value["openedAt"].replace("Z", "+00:00")
                        )
                b.revision = u.revision + 1
                snapshot = book_json(b, s)
            elif kind == "position.put":
                b = owned_book(s, u, op.get("bookId"))
                ident = f"{b.id}:{device}"
                entity = "position"
                current = s.get(m.Position, ident)
                if current and current.revision != op.get("baseRevision"):
                    raise ValueError(
                        json.dumps(
                            {
                                "conflict": True,
                                "current": {
                                    "id": current.id,
                                    "bookId": current.book_id,
                                    "deviceId": current.device_id,
                                    "renditionId": current.rendition_id,
                                    "locator": current.locator,
                                    "progress": current.progress,
                                    "revision": current.revision,
                                },
                            }
                        )
                    )
                if not current and op.get("baseRevision") is not None:
                    raise ValueError(json.dumps({"conflict": True, "current": None}))
                rendition = s.scalar(
                    select(m.BookFile).where(
                        m.BookFile.id == op.get("renditionId"), m.BookFile.book_id == b.id
                    )
                )
                if not rendition:
                    raise ValueError("invalid_rendition")
                validate_locator(rendition, value.get("locator"))
                progress = value.get("progress")
                if not isinstance(progress, (int, float)) or isinstance(progress, bool) or not math.isfinite(progress) or not 0 <= progress <= 1:
                    raise ValueError("invalid_progress")
                p = current or m.Position(
                    id=ident,
                    user_id=u.id,
                    book_id=b.id,
                    device_id=device,
                    rendition_id=op.get("renditionId"),
                    locator={},
                    progress=0,
                    revision=0,
                )
                p.rendition_id = op.get("renditionId")
                p.locator = value["locator"]
                p.progress = value["progress"]
                p.revision = u.revision + 1
                s.add(p)
                b.progress = p.progress
                b.revision = u.revision + 2
                s.flush()
                snapshot = {
                    "id": p.id,
                    "bookId": b.id,
                    "deviceId": device,
                    "renditionId": p.rendition_id,
                    "locator": p.locator,
                    "progress": p.progress,
                    "revision": p.revision,
                    "updatedAt": p.updated_at.isoformat(),
                }
            elif kind == "annotation.put":
                ident = op.get("annotationId")
                uuid.UUID(str(ident))
                entity = "annotation"
                current = s.get(m.Annotation, ident)
                if current and current.user_id != u.id:
                    raise PermissionError("forbidden")
                if current and current.revision != op.get("baseRevision"):
                    raise ValueError(
                        json.dumps(
                            {
                                "conflict": True,
                                "current": {
                                    "id": current.id,
                                    **current.value,
                                    "revision": current.revision,
                                },
                            }
                        )
                    )
                if not current and op.get("baseRevision") is not None:
                    raise ValueError(json.dumps({"conflict": True, "current": None}))
                book = owned_book(s, u, value["bookId"])
                rendition = s.scalar(
                    select(m.BookFile).where(
                        m.BookFile.id == value.get("renditionId"), m.BookFile.book_id == book.id
                    )
                )
                if not rendition:
                    raise ValueError("invalid_rendition")
                if (
                    value.get("type") not in ("highlight", "note", "bookmark")
                    or value.get("color") not in ("yellow", "green", "blue", "pink")
                    or not isinstance(value.get("quote", ""), str)
                    or not isinstance(value.get("note", ""), str)
                ):
                    raise ValueError("invalid_annotation")
                if len(value.get("quote", "")) > 20000 or len(value.get("note", "")) > 20000:
                    raise ValueError("invalid_annotation")
                if value.get("type") == "highlight" and value.get("anchor") is None:
                    raise ValueError("invalid_anchor")
                validate_locator(rendition, value.get("locator"))
                validate_anchor(rendition, value)
                if current and (
                    current.book_id != book.id or current.value.get("renditionId") != rendition.id
                ):
                    raise ValueError("immutable_anchor")
                a = current or m.Annotation(
                    id=ident, user_id=u.id, book_id=value["bookId"], value=value, revision=0
                )
                a.value = value
                a.deleted = False
                a.revision = u.revision + 1
                current = a
                s.add(a)
                s.flush()
                snapshot = {
                    "id": ident,
                    **value,
                    "deleted": False,
                    "revision": a.revision,
                    "createdAt": a.created_at.isoformat(),
                }
            elif kind == "annotation.delete":
                ident = op.get("annotationId")
                uuid.UUID(str(ident))
                entity = "annotation"
                current = s.get(m.Annotation, ident)
                if (
                    not current
                    or current.user_id != u.id
                    or current.revision != op.get("baseRevision")
                ):
                    if current and current.user_id == u.id:
                        raise ValueError(
                            json.dumps(
                                {
                                    "conflict": True,
                                    "current": {
                                        "id": current.id,
                                        **current.value,
                                        "revision": current.revision,
                                        "deleted": current.deleted,
                                    },
                                }
                            )
                        )
                    raise ValueError(json.dumps({"conflict": True, "current": None}))
                current.deleted = True
                current.revision = u.revision + 1
                snapshot = {
                    "id": ident,
                    **current.value,
                    "deleted": True,
                    "revision": current.revision,
                }
            elif kind == "preferences.put":
                entity = "preferences"
                ident = u.id
                current = s.get(m.Preference, u.id)
                if current and current.revision != op.get("baseRevision"):
                    raise ValueError(
                        json.dumps(
                            {
                                "conflict": True,
                                "current": {"revision": current.revision, "value": current.value},
                            }
                        )
                    )
                if not current and op.get("baseRevision") is not None:
                    raise ValueError(json.dumps({"conflict": True, "current": None}))
                p = current or m.Preference(user_id=u.id, value={})
                p.value = value
                p.revision = u.revision + 1
                current = p
                s.add(p)
                snapshot = {"revision": p.revision, "value": value}
            elif kind == "activity.append":
                ident = op.get("eventId")
                entity = "stats"
                active = int(op.get("activeMs", 0))
                if active < 0 or active > 60000:
                    raise ValueError("invalid_activity")
                sequence = int(op["sequence"])
                if sequence < 0:
                    raise ValueError("invalid_activity_sequence")
                uuid.UUID(str(ident))
                uuid.UUID(str(op["sessionId"]))
                date = str(op.get("localDate", ""))
                try:
                    datetime.strptime(date, "%Y-%m-%d")
                except ValueError:
                    date = datetime.now(timezone.utc).date().isoformat()
                offset = int(op.get("timezoneOffset", 0))
                if not -840 <= offset <= 840:
                    raise ValueError("invalid_timezone_offset")
                prior_event = s.get(m.Activity, ident)
                if prior_event:
                    raise ValueError("duplicate_event")
                duplicate = s.scalar(
                    select(m.Activity).where(
                        m.Activity.user_id == u.id,
                        m.Activity.session_id == op["sessionId"],
                        m.Activity.sequence == sequence,
                    )
                )
                if duplicate:
                    raise ValueError("duplicate_session_sequence")
                previous_sequence = s.scalar(
                    select(func.max(m.Activity.sequence)).where(
                        m.Activity.user_id == u.id, m.Activity.session_id == op["sessionId"]
                    )
                )
                if previous_sequence is not None and sequence <= previous_sequence:
                    raise ValueError("out_of_order_session_sequence")
                owned_book(s, u, op.get("bookId"))
                s.add(
                    m.Activity(
                        id=ident,
                        user_id=u.id,
                        book_id=op["bookId"],
                        session_id=op["sessionId"],
                        sequence=sequence,
                        active_ms=active,
                        local_date=date,
                        timezone_offset=offset,
                    )
                )
                snapshot = {"id": ident, "bookId": op["bookId"], "activeMs": active}
            else:
                raise ValueError("unknown_operation")
            number = rev(u, s, entity, ident, snapshot)
            if isinstance(current, (m.Position, m.Annotation, m.Preference)):
                current.revision = number
            if kind in ("book.patch", "book.delete"):
                b.revision = number
            if kind == "position.put":
                b.revision = rev(u, s, "book", b.id, book_json(b, s))
            if kind == "annotation.put" and current.revision != number:
                current.revision = number
            result = {"opId": opid, "status": "applied", "revision": number}
            s.add(
                m.Receipt(user_id=u.id, device_id=device, op_id=opid, digest=digest, result=result)
            )
            s.commit()
            output.append(result)
        except ValueError as e:
            s.rollback()
            error = str(e)
            try:
                details = json.loads(error)
            except json.JSONDecodeError:
                details = {}
            result = {
                "opId": opid,
                "status": "conflict"
                if error == "conflict" or details.get("conflict")
                else "rejected",
                "code": "conflict" if details.get("conflict") else error or "conflict",
                "message": "Operation could not be applied",
                **({"current": details["current"]} if "current" in details else {}),
            }
            s.add(
                m.Receipt(user_id=u.id, device_id=device, op_id=opid, digest=digest, result=result)
            )
            s.commit()
            output.append(result)
        except PermissionError:
            s.rollback()
            result = {
                "opId": opid,
                "status": "rejected",
                "code": "forbidden",
                "message": "Entity is not owned by this user",
            }
            s.add(
                m.Receipt(user_id=u.id, device_id=device, op_id=opid, digest=digest, result=result)
            )
            s.commit()
            output.append(result)
        except HTTPException as exc:
            s.rollback()
            detail = exc.detail.get("error", {}) if isinstance(exc.detail, dict) else {}
            result = {
                "opId": opid,
                "status": "rejected",
                "code": detail.get("code", "rejected"),
                "message": detail.get("message", "Operation rejected"),
            }
            s.add(
                m.Receipt(user_id=u.id, device_id=device, op_id=opid, digest=digest, result=result)
            )
            s.commit()
            output.append(result)
        except Exception:
            s.rollback()
            log.exception("sync operation failed")
            result = {
                "opId": opid,
                "status": "rejected",
                "code": "invalid_operation",
                "message": "Operation rejected",
            }
            s.add(
                m.Receipt(user_id=u.id, device_id=device, op_id=opid, digest=digest, result=result)
            )
            s.commit()
            output.append(result)
    return {"results": output}


@app.get("/api/v1/preferences")
def preferences(s=Depends(db), u=Depends(auth)):
    p = s.get(m.Preference, u.id)
    return {
        "revision": p.revision if p else 0,
        "value": p.value
        if p
        else {
            "theme": "light",
            "fontFamily": "serif",
            "fontSize": 18,
            "lineHeight": 1.6,
            "paragraphSpacing": 1,
            "margin": 24,
            "maxWidth": 760,
            "flow": "paginated",
            "pdfMode": "continuous",
            "goalMinutes": 30,
            "goalBooks": 12,
            "bookOverrides": {},
        },
    }


@app.get("/api/v1/collections")
def collections(s=Depends(db), u=Depends(auth)):
    return {
        "items": [
            {
                "id": c.id,
                "name": c.name,
                "description": c.description,
                "bookCount": s.scalar(
                    select(func.count())
                    .select_from(m.Book)
                    .where(
                        m.Book.user_id == u.id,
                        m.Book.deleted.is_(False),
                        m.Book.collection_ids.op("@>")([c.id]),
                    )
                ),
            }
            for c in s.scalars(select(m.Collection).where(m.Collection.user_id == u.id))
        ]
    }


@app.post("/api/v1/collections")
def add_collection(body: dict, s=Depends(db), u=Depends(auth)):
    u = s.scalar(
        select(m.User)
        .where(m.User.id == u.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    c = m.Collection(user_id=u.id, name=body["name"], description=body.get("description", ""))
    s.add(c)
    s.flush()
    c.revision = u.revision + 1
    n = rev(
        u,
        s,
        "collection",
        c.id,
        {"id": c.id, "name": c.name, "description": c.description, "revision": c.revision},
    )
    s.commit()
    return {"id": c.id, "name": c.name, "description": c.description, "bookCount": 0, "revision": n}


@app.patch("/api/v1/collections/{cid}")
def patch_collection(cid: str, body: dict, s=Depends(db), u=Depends(auth)):
    u = s.scalar(
        select(m.User)
        .where(m.User.id == u.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    c = s.scalar(select(m.Collection).where(m.Collection.id == cid, m.Collection.user_id == u.id))
    if not c:
        err("not_found", "Collection not found", 404)
    if "name" in body:
        c.name = str(body["name"])[:200]
    if "description" in body:
        c.description = str(body["description"])
    c.revision = u.revision + 1
    number = rev(
        u,
        s,
        "collection",
        c.id,
        {"id": c.id, "name": c.name, "description": c.description, "revision": c.revision},
    )
    s.commit()
    return {"id": c.id, "name": c.name, "description": c.description, "revision": number}


@app.delete("/api/v1/collections/{cid}")
def delete_collection(cid: str, s=Depends(db), u=Depends(auth)):
    u = s.scalar(
        select(m.User)
        .where(m.User.id == u.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    c = s.scalar(select(m.Collection).where(m.Collection.id == cid, m.Collection.user_id == u.id))
    if not c:
        err("not_found", "Collection not found", 404)
    for book in s.scalars(
        select(m.Book).where(m.Book.user_id == u.id, m.Book.collection_ids.op("@>")([cid]))
    ):
        book.collection_ids = [x for x in book.collection_ids if x != cid]
        book.revision = u.revision + 1
        book.revision = rev(u, s, "book", book.id, book_json(book, s))
    s.delete(c)
    rev(u, s, "collection", cid, {"id": cid, "deleted": True, "revision": u.revision + 1})
    s.commit()
    return {"ok": True}


@app.get("/api/v1/library/export")
def export_library(s=Depends(db), u=Depends(auth)):
    return {
        "books": [
            book_json(b, s)
            for b in s.scalars(
                select(m.Book).where(m.Book.user_id == u.id, m.Book.deleted.is_(False))
            )
        ],
        "collections": [
            {"id": c.id, "name": c.name, "description": c.description}
            for c in s.scalars(select(m.Collection).where(m.Collection.user_id == u.id))
        ],
    }


@app.post("/api/v1/library/import")
def import_library(body: dict, s=Depends(db), u=Depends(auth)):
    u = s.scalar(
        select(m.User)
        .where(m.User.id == u.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    updated = 0
    for item in body.get("books", []):
        b = s.scalar(select(m.Book).where(m.Book.id == item.get("id"), m.Book.user_id == u.id))
        if not b:
            continue
        for key, attr in {
            "title": "title",
            "author": "author",
            "description": "description",
            "publisher": "publisher",
            "language": "language",
            "tags": "tags",
            "favorite": "favorite",
            "readingStatus": "reading_status",
            "identifiers": "identifiers",
        }.items():
            if key in item:
                setattr(b, attr, item[key])
        b.revision = u.revision + 1
        b.revision = rev(u, s, "book", b.id, book_json(b, s))
        updated += 1
    s.commit()
    return {"updated": updated}


@app.get("/api/v1/books/{bid}/sessions")
def book_sessions(bid: str, page: int = 1, pageSize: int = 50, s=Depends(db), u=Depends(auth)):
    owned_book(s, u, bid)
    q = select(m.Activity).where(m.Activity.user_id == u.id, m.Activity.book_id == bid)
    rows = s.scalars(q.order_by(m.Activity.received.desc())).all()
    grouped = {}
    for activity in rows:
        item = grouped.setdefault(
            activity.session_id,
            {
                "id": activity.session_id,
                "bookId": bid,
                "activeMs": 0,
                "receivedAt": activity.received.isoformat(),
            },
        )
        item["activeMs"] += activity.active_ms
        item["receivedAt"] = max(item["receivedAt"], activity.received.isoformat())
    summaries = sorted(grouped.values(), key=lambda item: item["receivedAt"], reverse=True)
    return {
        "items": summaries[(page - 1) * pageSize : page * pageSize],
        "total": len(summaries),
        "page": page,
        "pageSize": pageSize,
    }


@app.get("/api/v1/annotations")
def annotations(
    bookId: str | None = None,
    q: str = "",
    page: int = 1,
    pageSize: int = 50,
    s=Depends(db),
    u=Depends(auth),
):
    query = select(m.Annotation).where(
        m.Annotation.user_id == u.id, m.Annotation.deleted.is_(False)
    )
    if bookId:
        query = query.where(m.Annotation.book_id == bookId)
    rows = s.scalars(query).all()
    rows = [a for a in rows if q.lower() in json.dumps(a.value).lower()]
    total = len(rows)
    return {
        "items": [
            {
                "id": a.id,
                "bookId": a.book_id,
                **a.value,
                "revision": a.revision,
                "deleted": a.deleted,
                "createdAt": a.created_at.isoformat(),
            }
            for a in rows[(page - 1) * pageSize : page * pageSize]
        ],
        "total": total,
        "page": page,
        "pageSize": pageSize,
    }


@app.get("/api/v1/annotations/export")
def export_annotations(
    format: str = "json", bookId: str | None = None, s=Depends(db), u=Depends(auth)
):
    query = select(m.Annotation).where(
        m.Annotation.user_id == u.id, m.Annotation.deleted.is_(False)
    )
    if bookId:
        query = query.where(m.Annotation.book_id == bookId)
    rows = s.scalars(query.order_by(m.Annotation.id)).all()
    if format == "json":
        content = json.dumps(
            [{"id": a.id, "bookId": a.book_id, **a.value, "revision": a.revision} for a in rows],
            ensure_ascii=False,
        )
        media = "application/json"
        filename = "annotations.json"
    elif format == "markdown":
        content = "\n\n".join(
            f"## {a.value.get('quote', '')}\n\n{a.value.get('note', '')}" for a in rows
        )
        media = "text/markdown; charset=utf-8"
        filename = "annotations.md"
    else:
        err("invalid_format", "format must be json or markdown")
    return Response(
        content,
        media_type=media,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/v1/stats")
def stats(days: int = 365, s=Depends(db), u=Depends(auth)):
    acts = s.scalars(select(m.Activity).where(m.Activity.user_id == u.id)).all()
    totals = {}
    for a in acts:
        totals[a.local_date] = totals.get(a.local_date, 0) + a.active_ms
    books = s.scalars(select(m.Book).where(m.Book.user_id == u.id, m.Book.deleted.is_(False))).all()
    names = {b.id: b.title for b in books}
    by = {}
    for a in acts:
        by[a.book_id] = by.get(a.book_id, 0) + a.active_ms
    today = datetime.now(timezone.utc).date()
    dates = {datetime.strptime(day, "%Y-%m-%d").date() for day in totals}
    cursor = today if today in dates else today - timedelta(days=1)
    streak = 0
    while cursor in dates:
        streak += 1
        cursor -= timedelta(days=1)
    sessions = {}
    for activity in acts:
        entry = sessions.setdefault(
            activity.session_id,
            {
                "id": activity.session_id,
                "bookId": activity.book_id,
                "activeMs": 0,
                "receivedAt": activity.received.isoformat(),
            },
        )
        entry["activeMs"] += activity.active_ms
        entry["receivedAt"] = max(entry["receivedAt"], activity.received.isoformat())
    preferences = s.get(m.Preference, u.id)
    goals = preferences.value if preferences else {}
    window_size = min(max(days, 1), 3650)
    window_days = [(today - timedelta(days=offset)).isoformat() for offset in reversed(range(window_size))]
    return {
        "totalActiveMs": sum(totals.values()),
        "booksStarted": sum(b.reading_status != "unread" for b in books),
        "booksCompleted": sum(b.reading_status == "completed" for b in books),
        "currentStreak": streak,
        "daily": [{"date": day, "activeMs": totals.get(day, 0), "estimate": True} for day in window_days],
        "byBook": [{"bookId": k, "title": names.get(k, ""), "activeMs": v} for k, v in by.items()],
        "recentSessions": sorted(
            sessions.values(), key=lambda entry: entry["receivedAt"], reverse=True
        )[:20],
        "goalMinutes": goals.get("goalMinutes", 30),
        "goalBooks": goals.get("goalBooks", 12),
    }
