import io
import os
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from ebooklib import epub
from fastapi.testclient import TestClient
from pypdf import PdfWriter

from citko import models as m
from citko.db import SessionLocal
from citko.main import app, login_attempts
from citko.worker import run_once


def setup_user(client, email):
    # Setup is one-time; tests create users directly after the first admin.
    return client.post(
        "/api/v1/auth/setup",
        headers={"Origin": "http://localhost:5173", "X-Setup-Token": "test-setup-token"},
        json={"email": email, "password": "secure-passphrase", "name": "One"},
    )


def test_setup_login_origin_csrf_and_user_isolation(monkeypatch):
    monkeypatch.setenv("SETUP_TOKEN", "test-setup-token")
    a = TestClient(app)
    assert a.get("/api/v1/auth/setup").json() == {"required": True}
    response = setup_user(a, "one@example.test")
    assert response.status_code == 200
    csrf = response.json()["csrfToken"]
    assert a.get("/api/v1/auth/me").json()["user"]["email"] == "one@example.test"
    assert a.get("/api/v1/auth/me").json()["csrfToken"] == csrf
    assert a.post("/api/v1/collections", json={"name": "No origin"}).status_code == 403
    assert (
        a.post(
            "/api/v1/collections",
            headers={"Origin": "http://localhost:5173"},
            json={"name": "No CSRF"},
        ).status_code
        == 403
    )
    assert (
        a.post(
            "/api/v1/collections",
            headers={"Origin": "http://localhost:5173", "X-CSRF-Token": csrf},
            json={"name": "Shelf"},
        ).status_code
        == 200
    )
    assert setup_user(a, "two@example.test").status_code == 409
    a.post("/api/v1/auth/logout", headers={"Origin": "http://localhost:5173", "X-CSRF-Token": csrf})
    assert a.get("/api/v1/auth/me").status_code == 401


def test_setup_race_and_login_rate_limit(monkeypatch):
    monkeypatch.setenv("SETUP_TOKEN", "test-setup-token")
    clients = [TestClient(app), TestClient(app)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(lambda i: setup_user(clients[i], f"race{i}@example.test"), range(2))
        )
    assert sorted(r.status_code for r in results) == [200, 409]
    c = clients[0] if results[0].status_code == 200 else clients[1]
    user_email = (
        results[0].json()["user"]["email"]
        if results[0].status_code == 200
        else results[1].json()["user"]["email"]
    )
    login_attempts.clear()
    for _ in range(10):
        assert (
            c.post(
                "/api/v1/auth/login",
                headers={"Origin": "http://localhost:5173"},
                json={"email": user_email, "password": "wrong-password"},
            ).status_code
            == 401
        )
    assert (
        c.post(
            "/api/v1/auth/login",
            headers={"Origin": "http://localhost:5173"},
            json={"email": user_email, "password": "wrong-password"},
        ).status_code
        == 429
    )


def test_annotation_global_id_cannot_be_taken_over(monkeypatch):
    monkeypatch.setenv("SETUP_TOKEN", "test-setup-token")
    c = TestClient(app)
    setup_user(c, "one@example.test")
    with SessionLocal() as s:
        owner = m.User(email="owner@example.test", name="Owner", password="x")
        s.add(owner)
        s.flush()
        book = m.Book(user_id=owner.id, title="Private")
        s.add(book)
        s.flush()
        rendition = m.BookFile(
            book_id=book.id, format="pdf", storage_name=f"{owner.id}/x.pdf", size=1, sha256="0" * 64
        )
        s.add(rendition)
        s.flush()
        book.rendition_id = rendition.id
        annotation = m.Annotation(
            id=str(uuid.uuid4()),
            user_id=owner.id,
            book_id=book.id,
            value={"bookId": book.id, "renditionId": rendition.id},
            revision=1,
        )
        s.add(annotation)
        s.commit()
        aid = annotation.id
    device = str(uuid.uuid4())
    csrf = c.get("/api/v1/auth/csrf").json()["csrfToken"]
    headers = {"Origin": "http://localhost:5173", "X-CSRF-Token": csrf}
    c.post("/api/v1/devices", headers=headers, json={"id": device, "name": "d"})
    op = {
        "opId": str(uuid.uuid4()),
        "kind": "annotation.put",
        "annotationId": aid,
        "baseRevision": 1,
        "value": {
            "bookId": str(uuid.uuid4()),
            "renditionId": str(uuid.uuid4()),
            "type": "note",
            "locator": {"type": "pdf", "page": 0, "y": 0.5},
            "quote": "takeover",
            "note": "x",
            "color": "yellow",
            "anchor": None,
        },
    }
    result = c.post(
        "/api/v1/sync/ops", headers=headers, json={"deviceId": device, "ops": [op]}
    ).json()["results"][0]
    assert result["status"] == "rejected"
    with SessionLocal() as s:
        saved = s.get(m.Annotation, aid)
        assert saved.user_id != c.cookies.get("citko_session")
        assert saved.value.get("note") is None


def test_sync_receipts_cas_and_complete_position_snapshots(monkeypatch):
    monkeypatch.setenv("SETUP_TOKEN", "test-setup-token")
    c = TestClient(app)
    created = setup_user(c, "sync@example.test")
    csrf = created.json()["csrfToken"]
    headers = {"Origin": "http://localhost:5173", "X-CSRF-Token": csrf}
    with SessionLocal() as s:
        owner = s.scalar(
            __import__("sqlalchemy").select(m.User).where(m.User.email == "sync@example.test")
        )
        b = m.Book(user_id=owner.id, title="Sync", status="ready")
        s.add(b)
        s.flush()
        f = m.BookFile(
            book_id=b.id,
            format="pdf",
            storage_name=f"{owner.id}/book/file.pdf",
            size=100,
            sha256="1" * 64,
        )
        s.add(f)
        s.flush()
        b.rendition_id = f.id
        s.commit()
        bid, fid = b.id, f.id
    device = str(uuid.uuid4())
    assert (
        c.post("/api/v1/devices", headers=headers, json={"id": device, "name": "test"}).status_code
        == 200
    )
    op = {
        "opId": str(uuid.uuid4()),
        "kind": "position.put",
        "bookId": bid,
        "renditionId": fid,
        "baseRevision": None,
        "value": {"locator": {"type": "pdf", "page": 0, "y": 0.42}, "progress": 0.42},
    }
    body = {"deviceId": device, "ops": [op]}
    first = c.post("/api/v1/sync/ops", headers=headers, json=body).json()["results"][0]
    assert first["status"] == "applied"
    assert c.post("/api/v1/sync/ops", headers=headers, json=body).json()["results"][0] == first
    altered = {**op, "value": {**op["value"], "progress": 0.6}}
    reused = c.post(
        "/api/v1/sync/ops", headers=headers, json={"deviceId": device, "ops": [altered]}
    ).json()["results"][0]
    assert reused["code"] == "id_reuse"
    state = c.get(f"/api/v1/books/{bid}").json()
    assert state["progress"] == 0.42
    stale = {
        "opId": str(uuid.uuid4()),
        "kind": "book.patch",
        "bookId": bid,
        "baseRevision": 0,
        "value": {"title": "stale"},
    }
    conflict = c.post(
        "/api/v1/sync/ops", headers=headers, json={"deviceId": device, "ops": [stale]}
    ).json()["results"][0]
    assert conflict["status"] == "conflict"
    assert conflict["current"]["title"] == "Sync"
    changes = c.get("/api/v1/sync/changes?after=0&limit=50").json()["changes"]
    book_changes = [change for change in changes if change["entity"] == "book"]
    assert book_changes[-1]["value"]["progress"] == 0.42
    assert book_changes[-1]["value"]["revision"] == book_changes[-1]["revision"]


def test_concurrent_book_cas_has_single_winner(monkeypatch):
    login_attempts.clear()
    monkeypatch.setenv("SETUP_TOKEN", "test-setup-token")
    first = TestClient(app)
    setup = setup_user(first, "cas@example.test")
    csrf_a = setup.json()["csrfToken"]
    second = TestClient(app)
    login = second.post(
        "/api/v1/auth/login",
        headers={"Origin": "http://localhost:5173"},
        json={"email": "cas@example.test", "password": "secure-passphrase"},
    )
    assert login.status_code == 200, login.text
    csrf_b = login.json()["csrfToken"]
    device = str(uuid.uuid4())
    first.post(
        "/api/v1/devices",
        headers={"Origin": "http://localhost:5173", "X-CSRF-Token": csrf_a},
        json={"id": device, "name": "cas"},
    )
    with SessionLocal() as s:
        owner = s.scalar(
            __import__("sqlalchemy").select(m.User).where(m.User.email == "cas@example.test")
        )
        book = m.Book(user_id=owner.id, title="Initial", status="ready")
        s.add(book)
        s.commit()
        bid = book.id
    bodies = []
    for title in ("Winner A", "Winner B"):
        bodies.append(
            {
                "deviceId": device,
                "ops": [
                    {
                        "opId": str(uuid.uuid4()),
                        "kind": "book.patch",
                        "bookId": bid,
                        "baseRevision": 0,
                        "value": {"title": title},
                    }
                ],
            }
        )

    def submit(args):
        client, csrf, body = args
        return client.post(
            "/api/v1/sync/ops",
            headers={"Origin": "http://localhost:5173", "X-CSRF-Token": csrf},
            json=body,
        ).json()["results"][0]["status"]

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(submit, [(first, csrf_a, bodies[0]), (second, csrf_b, bodies[1])]))
    assert sorted(statuses) == ["applied", "conflict"]


def test_jsonb_filters_collection_counts_and_activity_stats(monkeypatch):
    monkeypatch.setenv("SETUP_TOKEN", "test-setup-token")
    c = TestClient(app)
    created = setup_user(c, "analytics@example.test")
    csrf = created.json()["csrfToken"]
    headers = {"Origin": "http://localhost:5173", "X-CSRF-Token": csrf}
    collection = c.post("/api/v1/collections", headers=headers, json={"name": "Shelf"}).json()
    with SessionLocal() as s:
        owner = s.scalar(
            __import__("sqlalchemy").select(m.User).where(m.User.email == "analytics@example.test")
        )
        book = m.Book(
            user_id=owner.id,
            title="Analytics",
            tags=["science"],
            collection_ids=[collection["id"]],
            reading_status="reading",
        )
        s.add(book)
        s.commit()
        bid = book.id
    assert c.get("/api/v1/books?tag=science").json()["total"] == 1
    assert c.get("/api/v1/books?collectionId=" + collection["id"]).json()["total"] == 1
    assert c.get("/api/v1/collections").json()["items"][0]["bookCount"] == 1
    device = str(uuid.uuid4())
    c.post("/api/v1/devices", headers=headers, json={"id": device, "name": "stats"})
    session_id = str(uuid.uuid4())
    event = {
        "opId": str(uuid.uuid4()),
        "kind": "activity.append",
        "eventId": str(uuid.uuid4()),
        "bookId": bid,
        "sessionId": session_id,
        "sequence": 0,
        "activeMs": 60000,
        "localDate": "2026-10-18",
        "timezoneOffset": 0,
    }
    result = c.post(
        "/api/v1/sync/ops", headers=headers, json={"deviceId": device, "ops": [event]}
    ).json()["results"][0]
    assert result["status"] == "applied"
    replay = c.post(
        "/api/v1/sync/ops", headers=headers, json={"deviceId": device, "ops": [event]}
    ).json()["results"][0]
    assert replay == result
    stats = c.get("/api/v1/stats").json()
    assert stats["totalActiveMs"] == 60000
    assert stats["booksStarted"] == 1
    assert stats["recentSessions"][0]["activeMs"] == 60000


@pytest.mark.skipif(
    not os.getenv("S3_ENDPOINT_URL"), reason="requires private integration S3 service"
)
def test_upload_duplicate_worker_metadata_and_real_ranges(monkeypatch):
    monkeypatch.setenv("SETUP_TOKEN", "test-setup-token")
    c = TestClient(app)
    created = setup_user(c, "reader@example.test")
    csrf = created.json()["csrfToken"]
    headers = {"Origin": "http://localhost:5173", "X-CSRF-Token": csrf}
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=300)
    raw = io.BytesIO()
    writer.add_metadata({"/Title": "Postgres and S3 Test", "/Author": "Citko Test"})
    writer.write(raw)
    payload = raw.getvalue()
    uploaded = c.post(
        "/api/v1/books", headers=headers, files={"file": ("sample.pdf", payload, "application/pdf")}
    )
    assert uploaded.status_code == 201, uploaded.text
    book = uploaded.json()["book"]
    assert book["status"] == "queued"
    duplicate = c.post(
        "/api/v1/books", headers=headers, files={"file": ("other.pdf", payload, "application/pdf")}
    )
    assert duplicate.status_code == 409
    file_url = f"/api/v1/books/{book['id']}/file"
    for range_header, expected in (
        ("bytes=0-9", payload[:10]),
        ("bytes=10-19", payload[10:20]),
        ("bytes=-8", payload[-8:]),
    ):
        partial = c.get(file_url, headers={"Range": range_header})
        assert partial.status_code == 206
        assert partial.content == expected
    ignored = c.get(file_url, headers={"Range": "bytes=0-1", "If-Range": '"not-current"'})
    assert ignored.status_code == 200 and ignored.content == payload
    assert run_once() is True
    ready = c.get(f"/api/v1/books/{book['id']}").json()
    assert ready["status"] == "ready"
    assert ready["title"] == "Postgres and S3 Test"
    assert c.get(file_url, headers={"Range": f"bytes={len(payload)}-"}).status_code == 416
    epub_book = epub.EpubBook()
    epub_book.set_identifier("urn:test:epub")
    epub_book.set_title("EPUB Metadata Test")
    epub_book.add_author("Citko EPUB")
    epub_book.add_item(epub.EpubNav())
    epub_book.spine = ["nav"]
    epub_raw = io.BytesIO()
    epub.write_epub(epub_raw, epub_book)
    epub_result = c.post(
        "/api/v1/books",
        headers=headers,
        files={"file": ("sample.epub", epub_raw.getvalue(), "application/epub+zip")},
    )
    assert epub_result.status_code == 201
    assert run_once() is True
    assert c.get(f"/api/v1/books/{epub_result.json()['book']['id']}").json()["status"] == "ready"
    bad = c.post(
        "/api/v1/books",
        headers=headers,
        files={"file": ("broken.pdf", b"%PDF-not-a-real-pdf", "application/pdf")},
    )
    assert bad.status_code == 201
    for _ in range(3):
        assert run_once() is True
    imports = c.get("/api/v1/imports").json()["items"]
    failed = next(job for job in imports if job["bookId"] == bad.json()["book"]["id"])
    assert failed["status"] == "failed" and failed["attempts"] == 3
    retried = c.post(f"/api/v1/imports/{failed['id']}/retry", headers=headers)
    assert retried.status_code == 200
