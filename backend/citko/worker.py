"""Separate ingestion worker: python -m citko.worker."""

import logging
import signal
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select

from . import models as m
from .db import SessionLocal
from .main import MAX_UPLOAD, book_json, rev
from .storage import get_storage

log = logging.getLogger("citko.worker")


def bounded_extract(fmt, data):
    def timed_out(_signum, _frame):
        raise TimeoutError("Book parser exceeded 30-second CPU wall limit")

    previous = signal.signal(signal.SIGALRM, timed_out)
    signal.alarm(30)
    try:
        return extract(fmt, data)
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)


def extract(fmt, data):
    import zipfile

    if fmt in ("epub", "cbz"):
        with zipfile.ZipFile(__import__("io").BytesIO(data)) as archive:
            infos = archive.infolist()
            if len(infos) > 5000 or sum(i.file_size for i in infos) > 512 * 1024 * 1024:
                raise ValueError("Archive exceeds extraction bounds")
            if any(i.file_size > max(i.compress_size, 1) * 200 for i in infos):
                raise ValueError("Archive compression ratio exceeds limit")
    if fmt == "epub":
        import io

        from bs4 import BeautifulSoup
        from ebooklib import epub

        book = epub.read_epub(io.BytesIO(data), options={"ignore_ncx": True})
        title = book.get_metadata("DC", "title")
        author = book.get_metadata("DC", "creator")
        desc = book.get_metadata("DC", "description")
        lang = book.get_metadata("DC", "language")
        publisher = book.get_metadata("DC", "publisher")
        date = book.get_metadata("DC", "date")
        identifiers = book.get_metadata("DC", "identifier")
        return {
            "title": title[0][0] if title else None,
            "author": author[0][0] if author else None,
            "description": BeautifulSoup(desc[0][0], "html.parser").get_text(" ", strip=True)[
                :20000
            ]
            if desc
            else "",
            "language": lang[0][0] if lang else "",
            "publisher": publisher[0][0] if publisher else "",
            "published_at": date[0][0] if date else None,
            "identifiers": {f"dc:{index}": value[0] for index, value in enumerate(identifiers)},
            "_cover": next(
                (
                    item.get_content()
                    for item in book.get_items()
                    if item.get_type() == 1
                    and item.get_name().lower().endswith((".jpg", ".jpeg", ".png", ".webp"))
                ),
                None,
            ),
            "_cover_type": next(
                (
                    item.media_type
                    for item in book.get_items()
                    if item.get_type() == 1
                    and item.get_name().lower().endswith((".jpg", ".jpeg", ".png", ".webp"))
                ),
                "image/jpeg",
            ),
        }
    if fmt == "pdf":
        import io

        from pypdf import PdfReader

        r = PdfReader(io.BytesIO(data), strict=True)
        if r.is_encrypted:
            raise ValueError("Encrypted PDFs are not ingested")
        if len(r.pages) > 20000:
            raise ValueError("PDF page count exceeds limit")
        md = r.metadata
        return {
            "title": md.title if md and md.title else None,
            "author": md.author if md and md.author else None,
            "publisher": "",
            "description": "",
            "language": "",
            "published_at": None,
            "identifiers": {},
        }
    if fmt in ("mobi", "azw3"):
        if len(data) < 78 or b"BOOKMOBI" not in data[:128]:
            raise ValueError("Malformed MOBI/AZW3 container")
        return {"title": None, "author": None, "description": "", "language": ""}
    if fmt == "fb2":
        import xml.etree.ElementTree as ET

        root = ET.fromstring(data)
        title = root.find(".//{*}book-title")
        author = root.find(".//{*}author")
        name = (
            " ".join((author.findtext("{*}first-name", ""), author.findtext("{*}last-name", "")))
            if author is not None
            else ""
        )
        return {
            "title": title.text if title is not None else None,
            "author": name.strip() or None,
            "description": "",
            "language": "",
        }
    if fmt == "cbz":
        with zipfile.ZipFile(__import__("io").BytesIO(data)) as archive:
            if not any(
                n.lower().endswith((".jpg", ".jpeg", ".png", ".webp")) for n in archive.namelist()
            ):
                raise ValueError("CBZ archive has no supported images")
        return {"title": None, "author": None, "description": "", "language": ""}
    raise ValueError("Unsupported format")


def run_once():
    with SessionLocal() as s:
        candidate = s.scalar(
            select(m.Job)
            .where(
                or_(
                    m.Job.status == "queued",
                    (m.Job.status == "processing")
                    & (m.Job.lease_expires < datetime.now(timezone.utc)),
                ),
                m.Job.attempts < 3,
            )
            .order_by(m.Job.id)
            .limit(1)
        )
        if not candidate:
            return False
        user = s.scalar(
            select(m.User)
            .where(m.User.id == candidate.user_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        job = s.scalar(
            select(m.Job).where(m.Job.id == candidate.id).with_for_update(skip_locked=True)
        )
        if (
            not job
            or job.status not in ("queued", "processing")
            or job.attempts >= 3
            or (
                job.status == "processing"
                and job.lease_expires
                and job.lease_expires > datetime.now(timezone.utc)
            )
        ):
            s.rollback()
            return True
        fence = str(uuid.uuid4())
        job.lease = fence
        job.status = "processing"
        job.lease_expires = datetime.now(timezone.utc) + timedelta(minutes=5)
        job.attempts += 1
        b = s.get(m.Book, job.book_id)
        if b.deleted:
            job.status = "cancelled"
            job.lease = None
            job.lease_expires = None
            s.commit()
            return True
        b.status = "processing"
        b.error = None
        b.revision = user.revision + 1
        b.revision = rev(user, s, "book", b.id, book_json(b, s))
        s.commit()
        try:
            job = s.get(m.Job, job.id)
            b = s.get(m.Book, job.book_id)
            if b.deleted:
                job.status = "cancelled"
                job.lease = None
                job.lease_expires = None
                s.commit()
                return True
            f = s.get(m.BookFile, b.rendition_id)
            if f.size > MAX_UPLOAD:
                raise ValueError("Book exceeds worker extraction size limit")
            response = get_storage().get(f.storage_name)
            data = response["Body"].read(MAX_UPLOAD + 1)
            response["Body"].close()
            metadata = bounded_extract(f.format, data)
            s.refresh(job)
            if job.lease != fence or job.status != "processing":
                return True
            user = s.scalar(
                select(m.User)
                .where(m.User.id == b.user_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            job = s.scalar(
                select(m.Job)
                .where(m.Job.id == job.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            s.refresh(b)
            if b.deleted and job.lease == fence:
                job.status = "cancelled"
                job.lease = None
                job.lease_expires = None
                s.commit()
                return True
            if job.lease != fence or job.status != "processing":
                return True
            for k, v in metadata.items():
                if k == "_cover":
                    if v:
                        key = f"{b.user_id}/{b.id}/cover"
                        get_storage().put(
                            key,
                            __import__("io").BytesIO(v),
                            len(v),
                            metadata.get("_cover_type", "image/jpeg"),
                        )
                        b.cover_key = key
                elif k == "_cover_type":
                    continue
                elif v:
                    setattr(b, k, v)
            b.status = "ready"
            b.error = None
            b.revision = user.revision + 1
            b.revision = rev(user, s, "book", b.id, book_json(b, s))
            job.status = "succeeded"
            job.lease = None
            job.lease_expires = None
            s.commit()
        except Exception as exc:
            s.rollback()
            job = s.get(m.Job, job.id)
            b = s.get(m.Book, job.book_id)
            if job.lease == fence:
                user = s.scalar(
                    select(m.User)
                    .where(m.User.id == b.user_id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
                job = s.scalar(
                    select(m.Job)
                    .where(m.Job.id == job.id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
                s.refresh(b)
            if job.lease == fence and not b.deleted:
                job.status = "queued" if job.attempts < 3 else "failed"
                job.lease = None
                job.lease_expires = None
                job.error = str(exc)[:1000]
                b.status = "processing" if job.attempts < 3 else "failed"
                b.error = job.error
                b.revision = user.revision + 1
                b.revision = rev(user, s, "book", b.id, book_json(b, s))
                s.commit()
            log.exception("ingestion failed job=%s", job.id)
        return True


def main():
    import os
    import time

    while True:
        if not run_once():
            time.sleep(float(os.getenv("WORKER_POLL_SECONDS", "2")))


if __name__ == "__main__":
    main()
