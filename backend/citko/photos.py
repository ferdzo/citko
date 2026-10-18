"""EXIF extraction and thumbnail rendering for uploaded photos.

Only metadata already present in the uploaded file is read — nothing is looked up externally and no
image content is changed beyond producing a display-sized thumbnail. Every EXIF field is optional, so
an absent tag stays None instead of being guessed.
"""

from __future__ import annotations

import io
from datetime import datetime

from PIL import ExifTags, Image, ImageOps

THUMBNAIL_SIZE = (480, 480)
THUMBNAIL_QUALITY = 82
# Decoding an enormous image is a memory-exhaustion vector; the default Pillow ceiling is too high
# for a photo library, so we refuse anything above this before touching pixels.
MAX_PIXELS = 80_000_000
MAX_EXIF_ENTRIES = 40

EXTENSION_MIME = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".bmp": "image/bmp",
}
ALLOWED_MIME = set(EXTENSION_MIME.values())

# Tags worth surfacing in a details panel, in the order a reader would want them.
KEPT_TAGS = (
    "Make", "Model", "LensModel", "LensMake", "DateTimeOriginal", "DateTime", "CreateDate",
    "ExposureTime", "FNumber", "ApertureValue", "ISOSpeedRatings", "PhotographicSensitivity",
    "FocalLength", "FocalLengthIn35mmFilm", "ExposureProgram", "ExposureBiasValue",
    "MeteringMode", "Flash", "WhiteBalance", "Orientation", "Software", "Artist", "Copyright",
    "ImageWidth", "ImageLength", "ColorSpace", "SceneCaptureType",
)


def text(value) -> str | None:
    if value is None:
        return None
    cleaned = str(value).replace("\x00", "").strip()
    return cleaned or None


def number(value) -> float | None:
    """Pillow returns rationals as (numerator, denominator) tuples; flatten them to a float."""
    try:
        if isinstance(value, tuple) and len(value) == 2:
            return float(value[0]) / float(value[1]) if value[1] else None
        return float(value)
    except (TypeError, ValueError):
        return None


def _aperture(value) -> str | None:
    aperture = number(value)
    return f"f/{aperture:g}" if aperture else None


def _shutter(value) -> str | None:
    seconds = number(value)
    if not seconds or seconds <= 0:
        return None
    return f"{seconds:g}s" if seconds >= 1 else f"1/{round(1 / seconds)}"


def _focal(value) -> str | None:
    millimetres = number(value)
    return f"{millimetres:g} mm" if millimetres else None


def _iso(value) -> int | None:
    # Some writers store a list here; take the first usable value.
    if isinstance(value, (list, tuple)):
        value = value[0] if value else None
    result = number(value)
    return round(result) if result else None


def _taken_at(value) -> str | None:
    raw = text(value)
    if not raw:
        return None
    for pattern in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y:%m:%d"):
        try:
            # EXIF carries no timezone; emit a naive local timestamp rather than inventing one.
            return datetime.strptime(raw, pattern).isoformat()
        except ValueError:
            continue
    return raw


def _plain(value):
    """Coerce an EXIF value into something JSON can hold."""
    if isinstance(value, bytes):
        return text(value.decode("utf-8", "replace"))
    if isinstance(value, (list, tuple)):
        if len(value) == 2 and all(isinstance(item, int) for item in value):
            return _shutter(value) if value[1] == 0 else f"{value[0]}/{value[1]}"
        return [_plain(item) for item in value[:8]]
    if isinstance(value, (int, float, str)) or value is None:
        return value
    return text(value)


def _gps(ifd: dict) -> tuple[float | None, float | None]:
    def convert(values, reference) -> float | None:
        if not values:
            return None
        parts = [number(value) for value in values]
        if len(parts) != 3 or any(part is None for part in parts):
            return None
        degrees = parts[0] + parts[1] / 60 + parts[2] / 3600
        if str(reference).upper() in ("S", "W"):
            degrees = -degrees
        return round(degrees, 6)

    return convert(ifd.get("GPSLatitude"), ifd.get("GPSLatitudeRef")), convert(
        ifd.get("GPSLongitude"), ifd.get("GPSLongitudeRef")
    )


def _guard(image: Image.Image) -> None:
    if image.width * image.height > MAX_PIXELS:
        raise ValueError("image_too_large")


def _open(data: bytes) -> Image.Image:
    """Anything Pillow cannot identify becomes a ValueError, so callers handle one failure mode.

    Pillow raises UnidentifiedImageError (an OSError) for unrecognised bytes, which is not a
    ValueError and would otherwise escape the route's intended 415 path.
    """
    try:
        return Image.open(io.BytesIO(data))
    except Image.UnidentifiedImageError as exc:
        raise ValueError("unsupported_image") from exc


def inspect(data: bytes) -> dict:
    """Dimensions and EXIF facts for an uploaded image, with absent tags left as None."""
    result = {
        "width": None, "height": None, "taken_at": None, "camera": None, "lens": None, "iso": None,
        "aperture": None, "shutter": None, "focal_length": None, "orientation": None,
        "gps_lat": None, "gps_lon": None, "exif": {},
    }
    with _open(data) as image:
        _guard(image)
        result["width"], result["height"] = image.size
        exif = image.getexif()
        if not exif:
            return result
        top = {ExifTags.TAGS.get(key, key): value for key, value in exif.items()}
        try:
            detail = exif.get_ifd(ExifTags.IFD.Exif)
        except (KeyError, OSError):
            detail = {}
        sub = {ExifTags.TAGS.get(key, key): value for key, value in (detail or {}).items()}
        make, model = text(top.get("Make")), text(top.get("Model"))
        # Bodies commonly repeat the maker inside Model ("NIKON CORPORATION" + "NIKON CORPORATION
        # NIKON Z 6"); showing it twice is noise, so keep the fullest single form.
        if make and model and model.lower().startswith(make.lower()):
            model = model[len(make) :].strip() or model
            make = None
        result.update(
            taken_at=_taken_at(sub.get("DateTimeOriginal") or top.get("DateTime")),
            camera=" ".join(part for part in (make, model) if part) or None,
            lens=text(sub.get("LensModel")) or text(sub.get("LensMake")),
            iso=_iso(sub.get("PhotographicSensitivity") or sub.get("ISOSpeedRatings")),
            aperture=_aperture(sub.get("FNumber") or sub.get("ApertureValue")),
            shutter=_shutter(sub.get("ExposureTime")),
            focal_length=_focal(sub.get("FocalLength")),
            orientation=int(top["Orientation"]) if isinstance(top.get("Orientation"), int) else None,
        )
        try:
            gps = exif.get_ifd(ExifTags.IFD.GPSInfo) or {}
        except (KeyError, OSError):
            gps = {}
        result["gps_lat"], result["gps_lon"] = _gps(
            {ExifTags.GPSTAGS.get(key, key): value for key, value in gps.items()}
        )
        combined = {**top, **sub}
        result["exif"] = {
            tag: _plain(combined[tag]) for tag in KEPT_TAGS if tag in combined
        }
    return result


def thumbnail(data: bytes, size: tuple[int, int] = THUMBNAIL_SIZE) -> bytes:
    """A display-sized JPEG, upright per the EXIF orientation, with transparency flattened."""
    with _open(data) as image:
        _guard(image)
        upright = ImageOps.exif_transpose(image)
        if upright.mode in ("RGBA", "LA", "P"):
            rgba = upright.convert("RGBA")
            flattened = Image.new("RGB", rgba.size, (255, 255, 255))
            flattened.paste(rgba, mask=rgba.split()[-1])
            upright = flattened
        elif upright.mode != "RGB":
            upright = upright.convert("RGB")
        upright.thumbnail(size)
        out = io.BytesIO()
        upright.save(out, format="JPEG", quality=THUMBNAIL_QUALITY, optimize=True)
        return out.getvalue()
