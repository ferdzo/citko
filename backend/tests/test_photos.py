"""Photo library tests.

EXIF extraction and thumbnailing are exercised against real encoded images rather than mocks, and
the upload routes run against an in-memory storage fake so they do not depend on the private S3
service that the book-ingest integration test needs.
"""

import io

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from citko import main, photos
from citko import models as m
from citko.db import SessionLocal

ORIGIN = "http://localhost:5173"
SETUP_TOKEN = "test-setup-token"

MAKE, MODEL, ORIENTATION, DATETIME = 271, 272, 274, 306
DATETIME_ORIGINAL, ISO, FNUMBER, EXPOSURE, FOCAL, LENS = 36867, 34855, 33437, 33434, 37386, 42036


def encoded(
    size=(1600, 1000),
    *,
    make=None,
    model=None,
    orientation=None,
    taken=None,
    iso=None,
    fnumber=None,
    exposure=None,
    focal=None,
    lens=None,
    gps=None,
) -> bytes:
    """An in-memory JPEG carrying only the EXIF that the caller asked for."""
    image = Image.new("RGB", size, (200, 200, 200))
    exif = Image.Exif()
    if make:
        exif[MAKE] = make
    if model:
        exif[MODEL] = model
    if orientation:
        exif[ORIENTATION] = orientation
    if taken:
        exif[DATETIME] = taken
    sub = exif.get_ifd(Image.ExifTags.IFD.Exif)
    if taken:
        sub[DATETIME_ORIGINAL] = taken
    if iso:
        sub[ISO] = iso
    if fnumber:
        sub[FNUMBER] = fnumber
    if exposure:
        sub[EXPOSURE] = exposure
    if focal:
        sub[FOCAL] = focal
    if lens:
        sub[LENS] = lens
    if gps:
        gps_ifd = exif.get_ifd(Image.ExifTags.IFD.GPSInfo)
        gps_ifd[1], gps_ifd[2], gps_ifd[3], gps_ifd[4] = gps
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", exif=exif)
    return buffer.getvalue()


def full_exif(**overrides) -> bytes:
    fields = {
        "make": "Citko Cameras",
        "model": "CK-1",
        "orientation": 6,
        "taken": "2024:05:01 10:30:00",
        "iso": 400,
        "fnumber": (28, 10),
        "exposure": (1, 250),
        "focal": (35, 1),
        "lens": "CK 35mm f/2.8",
        "gps": ("N", (51.0, 30.0, 0.0), "W", (0.0, 7.0, 0.0)),
    }
    fields.update(overrides)
    return encoded(**fields)


# --- EXIF extraction ---------------------------------------------------------------------------


def test_inspect_normalises_the_tags_a_gallery_shows():
    facts = photos.inspect(full_exif())
    assert (facts["width"], facts["height"]) == (1600, 1000)
    assert facts["taken_at"] == "2024-05-01T10:30:00"
    assert facts["camera"] == "Citko Cameras CK-1"
    assert facts["lens"] == "CK 35mm f/2.8"
    assert facts["iso"] == 400
    # Rationals are the reason this needs normalising: readers should see f/2.8, not (28, 10).
    assert facts["aperture"] == "f/2.8"
    assert facts["shutter"] == "1/250"
    assert facts["focal_length"] == "35 mm"
    assert facts["orientation"] == 6
    assert facts["gps_lat"] == pytest.approx(51.5)
    assert facts["gps_lon"] == pytest.approx(-0.116667, abs=1e-6)
    assert facts["exif"]["ISOSpeedRatings"] == 400


def test_inspect_leaves_absent_tags_empty_rather_than_guessing():
    facts = photos.inspect(encoded())
    assert (facts["width"], facts["height"]) == (1600, 1000)
    for key in ("taken_at", "camera", "lens", "iso", "aperture", "shutter", "focal_length"):
        assert facts[key] is None, key
    assert facts["gps_lat"] is None and facts["gps_lon"] is None
    assert facts["exif"] == {}


def test_camera_does_not_repeat_the_maker():
    facts = photos.inspect(encoded(make="NIKON CORPORATION", model="NIKON CORPORATION NIKON Z 6"))
    assert facts["camera"] == "NIKON Z 6"


def test_gps_is_negative_for_south_and_west():
    facts = photos.inspect(
        full_exif(gps=("S", (33.0, 52.0, 0.0), "E", (151.0, 12.0, 0.0)))
    )
    assert facts["gps_lat"] == pytest.approx(-33.866667, abs=1e-6)
    assert facts["gps_lon"] == pytest.approx(151.2, abs=1e-6)


def test_shutter_aperture_and_iso_formatting_edges():
    assert photos._shutter((1, 250)) == "1/250"
    assert photos._shutter((1, 1)) == "1s"
    assert photos._shutter(2.5) == "2.5s"
    assert photos._shutter(0) is None and photos._shutter(None) is None
    assert photos._aperture((28, 10)) == "f/2.8"
    assert photos._aperture((0, 10)) is None
    # Some writers hand back a list for ISO; the first usable entry wins.
    assert photos._iso([800, 1600]) == 800
    assert photos._iso(None) is None
    assert photos._focal((50, 1)) == "50 mm"


def test_thumbnail_is_transposed_for_an_orientation_six_photo():
    # Orientation 6 means "rotate 90° clockwise", so a 1600x1000 frame becomes 300x480 when
    # scaled into a 480px box. A portrait thumbnail from a landscape file proves the transpose ran.
    rendered = photos.thumbnail(full_exif())
    with Image.open(io.BytesIO(rendered)) as thumb:
        assert thumb.format == "JPEG"
        assert thumb.size == (300, 480)


def test_thumbnail_flattens_transparency_onto_white():
    image = Image.new("RGBA", (40, 40), (0, 0, 0, 0))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    with Image.open(io.BytesIO(photos.thumbnail(buffer.getvalue()))) as thumb:
        assert thumb.mode == "RGB"
        assert thumb.getpixel((20, 20)) == (255, 255, 255)


def test_inspect_refuses_bytes_that_are_not_an_image():
    with pytest.raises(ValueError):
        photos.inspect(b"this is not an image")


def test_inspect_refuses_an_image_with_too_many_pixels(monkeypatch):
    # Refusing before decoding is the point: the guard must not touch pixel data to work.
    monkeypatch.setattr(photos, "MAX_PIXELS", 100)
    with pytest.raises(ValueError, match="image_too_large"):
        photos.inspect(encoded(size=(20, 20)))


# --- Routes ------------------------------------------------------------------------------------


class MemoryBody:
    def __init__(self, data):
        self.data = data

    def iter_chunks(self, chunk_size=1024 * 1024):
        for offset in range(0, len(self.data), chunk_size):
            yield self.data[offset : offset + chunk_size]

    def close(self):
        self.data = b""


class MemoryStorage:
    """Minimal stand-in for ObjectStorage: enough to prove the routes store and serve correctly."""

    def __init__(self):
        self.objects = {}

    def put(self, key, stream, size, content_type):
        self.objects[key] = (stream.read(), content_type)

    def get(self, key, start=None, end=None):
        data, _ = self.objects[key]
        if start is not None:
            data = data[start : end + 1]
        return {"Body": MemoryBody(data)}


@pytest.fixture
def storage(monkeypatch):
    store = MemoryStorage()
    monkeypatch.setattr(main, "get_storage", lambda: store)
    return store


def signed_in(client):
    response = client.post(
        "/api/v1/auth/setup",
        headers={"Origin": ORIGIN, "X-Setup-Token": SETUP_TOKEN},
        json={"email": "photographer@example.test", "password": "secure-passphrase", "name": "One"},
    )
    assert response.status_code == 200, response.text
    return response.json()["csrfToken"]


def login(client, email):
    response = client.post(
        "/api/v1/auth/login",
        headers={"Origin": ORIGIN},
        json={"email": email, "password": "secure-passphrase"},
    )
    assert response.status_code == 200, response.text
    return response.json()["csrfToken"]


def upload(client, csrf, data, name="holiday.jpg", mime="image/jpeg"):
    return client.post(
        "/api/v1/photos",
        headers={"Origin": ORIGIN, "X-CSRF-Token": csrf},
        files={"file": (name, data, mime)},
        data={"caption": "Kyoto, morning"},
    )


def test_photo_upload_list_thumbnail_patch_and_soft_delete(monkeypatch, storage):
    monkeypatch.setenv("SETUP_TOKEN", SETUP_TOKEN)
    client = TestClient(main.app)
    csrf = signed_in(client)

    original = full_exif()
    created = upload(client, csrf, original)
    assert created.status_code == 201, created.text
    photo = created.json()["photo"]
    assert photo["fileName"] == "holiday.jpg"
    assert photo["caption"] == "Kyoto, morning"
    assert (photo["width"], photo["height"]) == (1600, 1000)
    assert photo["camera"] == "Citko Cameras CK-1"
    assert photo["takenAt"] == "2024-05-01T10:30:00"
    assert photo["favorite"] is False and photo["tags"] == []
    assert photo["fileUrl"] == f"/api/v1/photos/{photo['id']}/file"
    assert photo["thumbnailUrl"] == f"/api/v1/photos/{photo['id']}/thumbnail"
    # Exactly two objects: the original and its thumbnail.
    assert len(storage.objects) == 2

    listed = client.get("/api/v1/photos").json()
    assert listed["total"] == 1
    assert listed["items"][0]["id"] == photo["id"]

    thumb = client.get(photo["thumbnailUrl"])
    assert thumb.status_code == 200
    assert thumb.headers["content-type"] == "image/jpeg"
    with Image.open(io.BytesIO(thumb.content)) as image:
        assert image.size == (300, 480)

    served = client.get(photo["fileUrl"])
    assert served.status_code == 200
    assert served.content == original

    # Range requests have to keep working through the shared streaming helper.
    partial = client.get(photo["fileUrl"], headers={"Range": "bytes=0-99"})
    assert partial.status_code == 206
    assert len(partial.content) == 100
    assert partial.content == original[:100]

    patched = client.patch(
        f"/api/v1/photos/{photo['id']}",
        headers={"Origin": ORIGIN, "X-CSRF-Token": csrf},
        json={"caption": "Kyoto, evening", "favorite": True, "tags": ["travel", "japan"]},
    )
    assert patched.status_code == 200
    assert patched.json()["photo"]["caption"] == "Kyoto, evening"
    assert patched.json()["photo"]["favorite"] is True
    assert patched.json()["photo"]["tags"] == ["travel", "japan"]
    assert patched.json()["photo"]["revision"] == 2

    assert client.get("/api/v1/photos?favorite=true").json()["total"] == 1
    assert client.get("/api/v1/photos?favorite=false").json()["total"] == 1

    deleted = client.delete(
        f"/api/v1/photos/{photo['id']}", headers={"Origin": ORIGIN, "X-CSRF-Token": csrf}
    )
    assert deleted.status_code == 204
    # Soft delete: hidden from the library and unreachable by id, but the object stays in storage.
    assert client.get("/api/v1/photos").json()["total"] == 0
    assert client.get(photo["fileUrl"]).status_code == 404
    assert len(storage.objects) == 2


def test_photo_upload_rejects_a_non_image(monkeypatch, storage):
    monkeypatch.setenv("SETUP_TOKEN", SETUP_TOKEN)
    client = TestClient(main.app)
    csrf = signed_in(client)

    rejected = upload(client, csrf, b"definitely not a picture", name="notes.txt", mime="text/plain")
    assert rejected.status_code == 415
    assert rejected.json()["error"]["code"] == "unsupported_photo"
    # A rejected upload must not leave anything behind.
    assert storage.objects == {}

    empty = upload(client, csrf, b"", name="empty.jpg")
    assert empty.status_code == 400
    assert empty.json()["error"]["code"] == "empty_file"


def test_photos_are_private_to_their_owner(monkeypatch, storage):
    monkeypatch.setenv("SETUP_TOKEN", SETUP_TOKEN)
    owner = TestClient(main.app)
    csrf = signed_in(owner)
    photo = upload(owner, csrf, full_exif()).json()["photo"]

    with SessionLocal() as session:
        session.add(
            m.User(email="stranger@example.test", name="Stranger", password=main.pwd.hash("secure-passphrase"))
        )
        session.commit()

    stranger = TestClient(main.app)
    login(stranger, "stranger@example.test")
    assert stranger.get(photo["fileUrl"]).status_code == 404
    assert stranger.get(photo["thumbnailUrl"]).status_code == 404
    assert stranger.get("/api/v1/photos").json()["total"] == 0
