"""Enrich book records from open, key-free bibliographic sources.

Open Library is queried first, with Google Books as a fallback. Only public bibliographic records
and cover images are fetched. Nothing in this module contacts a shadow library, scrapes a search
page, or downloads a book file — it fills in title/author/description/publisher/date/cover for a
book the user already owns.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from html import unescape

import httpx

OPEN_LIBRARY = "https://openlibrary.org"
OPEN_LIBRARY_COVERS = "https://covers.openlibrary.org/b/id"
GOOGLE_BOOKS = "https://www.googleapis.com/books/v1/volumes"
TIMEOUT = float(os.getenv("METADATA_HTTP_TIMEOUT", "8"))
MAX_COVER_BYTES = int(os.getenv("METADATA_MAX_COVER_BYTES", str(3 * 1024 * 1024)))
ALLOWED_COVER_TYPES = ("image/jpeg", "image/png", "image/webp", "image/gif")
USER_AGENT = os.getenv("METADATA_USER_AGENT", "citko/0.1 (self-hosted reading platform)")

_TAGS = re.compile(r"<[^>]+>")
_SPACE = re.compile(r"\s+")
# A record is only trusted once it clearly matches what we asked for, so a loose search hit can
# never silently rewrite a user's library.
MATCH_THRESHOLD = 8


def clean(value) -> str | None:
    """Collapse markup and whitespace from a third-party record into plain display text."""
    if value is None:
        return None
    text = _SPACE.sub(" ", unescape(_TAGS.sub(" ", str(value)))).strip()
    return text or None


def normalize(value) -> str:
    return _SPACE.sub(" ", re.sub(r"[^0-9a-z ]+", " ", str(value or "").lower())).strip()


def isbn13(value: str | None) -> str | None:
    digits = re.sub(r"[^0-9Xx]", "", str(value or "")).upper()
    return digits if len(digits) == 13 else None


def isbn10(value: str | None) -> str | None:
    digits = re.sub(r"[^0-9Xx]", "", str(value or "")).upper()
    return digits if len(digits) == 10 else None


def isbn_of(identifiers: dict | None) -> str | None:
    """Pull whichever ISBN a book already carries, from any of the shapes we store it in.

    EPUB producers write identifiers under arbitrary keys — `isbn`, `calibre:isbn`, or the worker's
    positional `dc:0` — so a value is also accepted when it identifies itself as an ISBN
    (`urn:isbn:...`). Anything else is ignored, which keeps a `urn:uuid:` identifier out.
    """
    for key, value in (identifiers or {}).items():
        by_key = "isbn" in str(key).lower()
        for candidate in value if isinstance(value, list) else [value]:
            if candidate is None:
                continue
            text = str(candidate)
            if not by_key and "isbn" not in text.lower():
                continue
            found = isbn13(text) or isbn10(text)
            if found:
                return found
    return None


@dataclass
class Candidate:
    source: str
    source_url: str | None = None
    title: str | None = None
    author: str | None = None
    description: str | None = None
    publisher: str | None = None
    published_at: str | None = None
    language: str | None = None
    isbn: str | None = None
    cover_url: str | None = None
    score: int = 0

    def fields(self) -> dict:
        """Only the book columns this record can legitimately supply."""
        values = {
            "title": self.title,
            "author": self.author,
            "description": self.description,
            "publisher": self.publisher,
            "published_at": self.published_at,
            "language": self.language,
        }
        return {key: value for key, value in values.items() if value}


@dataclass
class Enrichment:
    source: str
    source_url: str | None
    fields: dict
    cover_bytes: bytes | None = None
    cover_type: str | None = None


def _client() -> httpx.Client:
    return httpx.Client(timeout=TIMEOUT, headers={"User-Agent": USER_AGENT}, follow_redirects=True)


def score(candidate: Candidate, *, isbn: str | None, title: str | None, author: str | None) -> int:
    """How confidently a record describes the book we asked about."""
    points = 0
    wanted_isbn = isbn13(isbn) or isbn10(isbn)
    if wanted_isbn and candidate.isbn and wanted_isbn == candidate.isbn:
        points += 8
    wanted_title, got_title = normalize(title), normalize(candidate.title)
    if wanted_title and got_title:
        if wanted_title == got_title:
            points += 5
        elif wanted_title in got_title or got_title in wanted_title:
            points += 3
    wanted_author, got_author = normalize(author), normalize(candidate.author)
    if wanted_author and got_author:
        if got_author in wanted_author or wanted_author in got_author:
            points += 3
    return points


def _ol_description(value) -> str | None:
    """Open Library stores a work description either as text or as {"type": ..., "value": ...}."""
    return clean(value.get("value")) if isinstance(value, dict) else clean(value)


_LANGUAGE_CODES = {
    "eng": "en", "ger": "de", "fre": "fr", "spa": "es", "ita": "it", "por": "pt", "dut": "nl",
    "rus": "ru", "jpn": "ja", "chi": "zh", "kor": "ko", "pol": "pl", "swe": "sv", "dan": "da",
    "nor": "no", "fin": "fi", "cze": "cs", "hun": "hu", "gre": "el", "tur": "tr", "ara": "ar",
    "heb": "he", "hin": "hi", "ukr": "uk", "rum": "ro", "srp": "sr", "hrv": "hr", "slo": "sk",
    "slv": "sl", "bul": "bg", "cat": "ca",
}
_DATE_PART = re.compile(r"\d{3,4}(?:-\d{3,4})?\.?")


def _ol_language(value) -> str | None:
    """Open Library reports MARC codes (`eng`); the book column holds ISO 639-1 (`en`)."""
    key = first(value) if isinstance(value, list) else value
    code = key.get("key") if isinstance(key, dict) else key
    if not code:
        return None
    code = str(code).rsplit("/", 1)[-1].lower()
    return _LANGUAGE_CODES.get(code, code)


def _ol_author(value) -> str | None:
    """Open Library writes authors surname-first with life dates: "Austen, Jane, 1775-1817."."""
    raw = clean(first(value) if isinstance(value, list) else value)
    if not raw:
        return None
    parts = [part.strip() for part in raw.split(",") if part.strip()]
    named = [part for part in parts if not _DATE_PART.fullmatch(part)]
    if len(named) == 2 and len(parts) in (2, 3):
        return f"{named[1]} {named[0]}"
    return raw


def _from_ol_edition(data: dict, isbn: str) -> Candidate:
    covers = data.get("covers") or []
    cover_id = next((c for c in covers if isinstance(c, int) and c > 0), None)
    return Candidate(
        source="openlibrary",
        source_url=f"{OPEN_LIBRARY}/isbn/{isbn}",
        title=clean(data.get("title")),
        author=_ol_author(data.get("author")) or _ol_author(data.get("by_statement")),
        description=_ol_description(data.get("description")),
        publisher=clean(first(data.get("publishers"))),
        published_at=clean(data.get("publish_date")),
        language=_ol_language(data.get("languages")),
        isbn=next((isbn13(c) or isbn10(c) for c in (data.get("isbn_13") or [])), None)
        or isbn13(isbn)
        or isbn10(isbn),
        cover_url=f"{OPEN_LIBRARY_COVERS}/{cover_id}-L.jpg" if cover_id else None,
    )


SEARCH_FIELDS = "key,title,author_name,first_publish_year,isbn,cover_i,publisher,language"


def _lead_author(author: str | None) -> str:
    """The first named author.

    Files routinely store a whole author list in one field ("Paul Chiusano, Rúnar Bjarnason"), which
    is not a value any catalogue holds, so only the leading name is safe to search or match on.
    """
    return re.split(r"[,;&]| and ", author or "", maxsplit=1)[0].strip()


def _query_for(title: str | None, author: str | None) -> str:
    """A precise general-index query for a book that arrived with no ISBN.

    The title= and author= parameters are ANDed exact-field filters, so passing a combined author
    string alongside a title matches nothing at all; the general q index is tolerant and scoring
    then decides how well a record actually agrees.
    """
    lead = _lead_author(author)
    parts = []
    if title:
        parts.append(f'title:"{title}"')
    if lead:
        parts.append(f'author:"{lead}"')
    return " ".join(parts)


def _open_library(isbn: str | None, title: str | None, author: str | None) -> list[Candidate]:
    results: list[Candidate] = []
    with _client() as client:
        try:
            if isbn:
                response = client.get(f"{OPEN_LIBRARY}/isbn/{isbn}.json")
                if response.status_code == 200:
                    edition = response.json()
                    candidate = _from_ol_edition(edition, isbn)
                    # An edition record carries no blurb or author name; the work it belongs to does.
                    works = edition.get("works") or []
                    key = works[0].get("key") if works and isinstance(works[0], dict) else None
                    if key and not candidate.description:
                        work = client.get(f"{OPEN_LIBRARY}{key}.json")
                        if work.status_code == 200:
                            candidate.description = _ol_description(work.json().get("description"))
                    results.append(candidate)
            query = None if isbn else _query_for(title, author)
            params = {"limit": 5, "fields": SEARCH_FIELDS}
            if isbn:
                params["isbn"] = isbn
            elif query:
                params["q"] = query
            else:
                params = None
            if params:
                response = client.get(f"{OPEN_LIBRARY}/search.json", params=params)
                docs = response.json().get("docs", [])[:5] if response.status_code == 200 else []
                if not docs and title:
                    # A quoted phrase query is precise but brittle, so widen to plain keywords before
                    # giving up on a book that has no ISBN to match on.
                    params["q"] = title
                    retry = client.get(f"{OPEN_LIBRARY}/search.json", params=params)
                    if retry.status_code == 200:
                        docs = retry.json().get("docs", [])[:5]
                candidates = [_from_ol_doc(doc) for doc in docs]
                # Search records carry no blurb at all, so the top hit's work is fetched once.
                key = docs[0].get("key") if docs and isinstance(docs[0], dict) else None
                if candidates and key and str(key).startswith("/works/"):
                    if not any(c.description for c in candidates):
                        work = client.get(f"{OPEN_LIBRARY}{key}.json")
                        if work.status_code == 200:
                            candidates[0].description = _ol_description(
                                work.json().get("description")
                            )
                results.extend(candidates)
        except httpx.HTTPError:
            return [c for c in results if c.title]
    return [c for c in results if c.title]


def _from_ol_doc(doc: dict) -> Candidate:
    isbns = doc.get("isbn") or []
    cover_id = doc.get("cover_i")
    return Candidate(
        source="openlibrary",
        source_url=f"{OPEN_LIBRARY}{doc.get('key')}" if doc.get("key") else None,
        title=clean(doc.get("title")),
        author=clean(first(doc.get("author_name"))),
        publisher=clean(first(doc.get("publisher"))),
        published_at=str(doc["first_publish_year"]) if doc.get("first_publish_year") else None,
        language=_ol_language(doc.get("language")),
        isbn=next((isbn13(c) or isbn10(c) for c in isbns if isbn13(c) or isbn10(c)), None),
        cover_url=f"{OPEN_LIBRARY_COVERS}/{cover_id}-L.jpg" if cover_id else None,
    )


def _google_books(isbn: str | None, title: str | None, author: str | None) -> list[Candidate]:
    if isbn:
        query = f"isbn:{isbn}"
    else:
        query = " ".join(
            part
            for part in (
                f"intitle:{title}" if title else "",
                f"inauthor:{_lead_author(author)}" if author else "",
            )
            if part
        )
    if not query:
        return []
    results: list[Candidate] = []
    with _client() as client:
        try:
            response = client.get(GOOGLE_BOOKS, params={"q": query, "maxResults": 5})
            if response.status_code != 200:
                return []
            for item in response.json().get("items", [])[:5]:
                results.append(_from_google(item))
        except httpx.HTTPError:
            return []
    return [c for c in results if c.title]


def _from_google(item: dict) -> Candidate:
    info = item.get("volumeInfo") or {}
    identifiers = info.get("industryIdentifiers") or []
    found = next((i.get("identifier") for i in identifiers if i.get("type") == "ISBN_13"), None)
    found = found or next((i.get("identifier") for i in identifiers), None)
    links = info.get("imageLinks") or {}
    cover = links.get("thumbnail") or links.get("smallThumbnail")
    return Candidate(
        source="googlebooks",
        source_url=info.get("infoLink") or info.get("canonicalVolumeLink"),
        title=clean(info.get("title")),
        author=clean(", ".join(info.get("authors") or []) or None),
        description=clean(info.get("description")),
        publisher=clean(info.get("publisher")),
        published_at=clean(info.get("publishedDate")),
        language=clean(info.get("language")),
        isbn=isbn13(found) or isbn10(found),
        # Google serves these over plain http; keep the page secure.
        cover_url=cover.replace("http://", "https://", 1) if cover else None,
    )


def first(values):
    if isinstance(values, (list, tuple)):
        return values[0] if values else None
    return values


def _merge(preferred: Candidate, fallback: Candidate) -> Candidate:
    """Fill gaps in the preferred record from the other one, keeping the preferred source."""
    merged = Candidate(**{**preferred.__dict__})
    for key, value in fallback.__dict__.items():
        if key in ("source", "source_url", "score"):
            continue
        if not getattr(merged, key) and value:
            setattr(merged, key, value)
    return merged


def lookup(isbn: str | None = None, title: str | None = None, author: str | None = None) -> list[Candidate]:
    """Candidate records from every configured source, best first."""
    candidates = _open_library(isbn, title, author) + _google_books(isbn, title, author)
    for candidate in candidates:
        candidate.score = score(candidate, isbn=isbn, title=title, author=author)
    return sorted(candidates, key=lambda c: c.score, reverse=True)


def fetch_cover(url: str | None) -> tuple[bytes, str] | None:
    """Download a cover image, refusing anything oversized or not an image."""
    if not url:
        return None
    try:
        with _client() as client:
            with client.stream("GET", url) as response:
                if response.status_code != 200:
                    return None
                content_type = (response.headers.get("content-type") or "").split(";")[0].strip().lower()
                if content_type not in ALLOWED_COVER_TYPES:
                    return None
                declared = response.headers.get("content-length")
                if declared and declared.isdigit() and int(declared) > MAX_COVER_BYTES:
                    return None
                body = bytearray()
                for chunk in response.iter_bytes(64 * 1024):
                    body.extend(chunk)
                    if len(body) > MAX_COVER_BYTES:
                        return None
                return (bytes(body), content_type) if body else None
    except httpx.HTTPError:
        return None


def enrich(
    *, isbn: str | None = None, title: str | None = None, author: str | None = None, want_cover: bool = True
) -> Enrichment | None:
    """Best matching record for a book, already merged and optionally carrying its cover."""
    candidates = lookup(isbn=isbn, title=title, author=author)
    if not candidates or candidates[0].score < MATCH_THRESHOLD:
        return None
    best = candidates[0]
    # Every other record that also clears the bar can fill what the best one omitted: a search result
    # usually carries the author name the edition record lacks, and Google Books carries the blurb.
    for other in candidates[1:]:
        if other.score < MATCH_THRESHOLD:
            break
        if other.source_url and other.source_url == best.source_url:
            continue
        best = _merge(best, other)
    cover = fetch_cover(best.cover_url) if want_cover else None
    return Enrichment(
        source=best.source,
        source_url=best.source_url,
        fields=best.fields(),
        cover_bytes=cover[0] if cover else None,
        cover_type=cover[1] if cover else None,
    )
