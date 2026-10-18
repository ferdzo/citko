#!/usr/bin/env python3
"""Create a small, original JPEG carrying real EXIF, for photo-library evidence.

Deliberately includes the awkward parts: an orientation that forces a transpose, rational (tuple)
values for aperture/exposure/focal length, an Exif sub-IFD, and GPS in degrees/minutes/seconds.
Tag numbers are written literally because Pillow's ExifTags enum member names have drifted between
releases (34855 is PhotographicSensitivity in the spec but ISOSpeedRatings in Pillow's table).
"""
from pathlib import Path

from PIL import ExifTags, Image

OUT = Path(__file__).parent

MAKE, MODEL, ORIENTATION, DATETIME = 271, 272, 274, 306
DATETIME_ORIGINAL, ISO, FNUMBER, EXPOSURE, FOCAL, LENS = 36867, 34855, 33437, 33434, 37386, 42036


def photo():
    image = Image.new("RGB", (1600, 1000), (200, 200, 200))
    # A visible asymmetry, so a wrongly-oriented thumbnail is obvious in a screenshot.
    for x in range(0, 400, 20):
        for y in range(0, 120, 20):
            for dx in range(20):
                for dy in range(20):
                    image.putpixel((x + dx, y + dy), (23, 23, 23))
    exif = Image.Exif()
    exif[MAKE] = "Citko Cameras"
    exif[MODEL] = "CK-1"
    exif[ORIENTATION] = 6
    exif[DATETIME] = "2024:05:01 10:30:00"
    sub = exif.get_ifd(ExifTags.IFD.Exif)
    sub[DATETIME_ORIGINAL] = "2024:05:01 10:30:00"
    sub[ISO] = 400
    sub[FNUMBER] = (28, 10)
    sub[EXPOSURE] = (1, 250)
    sub[FOCAL] = (35, 1)
    sub[LENS] = "CK 35mm f/2.8"
    gps = exif.get_ifd(ExifTags.IFD.GPSInfo)
    gps[1] = "N"
    gps[2] = (51.0, 30.0, 0.0)
    gps[3] = "W"
    gps[4] = (0.0, 7.0, 0.0)
    image.save(OUT / "citko-photo.jpg", exif=exif, quality=88)


if __name__ == "__main__":
    photo()
