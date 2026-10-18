"""Metadata enrichment is tested against a mocked HTTP transport: no network, no database.

The fixtures mirror what Open Library actually returns for an ISBN lookup, including the shapes that
are easy to get wrong: authors under `author` in surname-first form with life dates, a description
that is sometimes a {"type", "value"} object, and MARC language codes.
"""

import httpx
import pytest

from citko import metadata

EDITION = {
    "title": "Pride and Prejudice",
    "author": ["Austen, Jane, 1775-1817."],
    "description": {"type": "/type/text", "value": "When Elizabeth Bennet first meets Fitzwilliam Darcy."},
    "publishers": ["Penguin Books"],
    "publish_date": "2003",
    "covers": [12645114, 15090646, -1],
    "languages": [{"key": "/languages/eng"}],
    "isbn_13": ["9780141439518"],
    "works": [{"key": "/works/OL66554W"}],
}

# An edition with no blurb of its own: the work record has to supply it.
EDITION_NO_BLURB = {key: value for key, value in EDITION.items() if key != "description"}

WORK = {"description": {"type": "/type/text", "value": "A novel of manners and marriage."}}

SEARCH = {
    "docs": [
        {
            "key": "/works/OL66554W",
            "title": "Pride and Prejudice",
            "author_name": ["Jane Austen"],
            "first_publish_year": 1813,
            "cover_i": 14348537,
            "publisher": ["Penguin"],
            "language": ["eng"],
        }
    ]
}

GOOGLE = {
    "items": [
        {
            "volumeInfo": {
                "title": "Pride and Prejudice",
                "authors": ["Jane Austen"],
                "description": "A novel of manners.",
                "publisher": "Penguin",
                "publishedDate": "1813-01-28",
                "language": "en",
                "industryIdentifiers": [{"type": "ISBN_13", "identifier": "9780141439518"}],
                "imageLinks": {"thumbnail": "http://books.google.com/cover.jpg"},
            }
        }
    ]
}


def client_for(handler):
    return httpx.Client(transport=httpx.MockTransport(handler), timeout=5)


def happy(request):
    """Open Library responds fully; Google Books is rate limited, exactly as it was in production."""
    url = str(request.url)
    if "covers.openlibrary.org" in url:
        return httpx.Response(
            200, headers={"content-type": "image/jpeg", "content-length": "4"}, content=b"\xff\xd8\xff\xd9"
        )
    if "/isbn/9780141439518.json" in url:
        return httpx.Response(200, json=EDITION)
    if "/works/OL66554W.json" in url:
        return httpx.Response(200, json=WORK)
    if "/search.json" in url:
        return httpx.Response(200, json=SEARCH)
    if "googleapis" in url:
        return httpx.Response(429, json={"error": {"code": 429}})
    return httpx.Response(404)


@pytest.fixture
def use(monkeypatch):
    def apply(handler):
        monkeypatch.setattr(metadata, "_client", lambda: client_for(handler))
    return apply


def test_clean_strips_markup_and_entities():
    assert metadata.clean("<b>Pride</b> &amp; Prejudice\n") == "Pride & Prejudice"
    assert metadata.clean("   ") is None
    assert metadata.clean(None) is None


def test_isbn_validation_and_extraction():
    assert metadata.isbn13("978-0-14-143951-8") == "9780141439518"
    assert metadata.isbn13("123") is None
    assert metadata.isbn10("0141439513") == "0141439513"
    assert metadata.isbn_of({"isbn13": "9780141439518"}) == "9780141439518"
    # The worker stores EPUB identifiers positionally, so the value has to be recognised too.
    assert metadata.isbn_of({"dc:0": "urn:isbn:9780141439518"}) == "9780141439518"
    assert metadata.isbn_of({"dc:0": "urn:uuid:00000000-0000-4000-8000-000000000001"}) is None
    assert metadata.isbn_of({}) is None


def test_open_library_author_and_language_normalisation():
    # Open Library's "Austen, Jane, 1775-1817." is not something to show a reader.
    assert metadata._ol_author(["Austen, Jane, 1775-1817."]) == "Jane Austen"
    assert metadata._ol_author(["Austen, Jane"]) == "Jane Austen"
    assert metadata._ol_author(["Homer"]) == "Homer"
    assert metadata._ol_author("Melville, Herman, 1819-1891.") == "Herman Melville"
    assert metadata._ol_author([]) is None
    assert metadata._ol_language([{"key": "/languages/eng"}]) == "en"
    assert metadata._ol_language([{"key": "/languages/kor"}]) == "ko"
    # An unmapped code is passed through rather than dropped.
    assert metadata._ol_language([{"key": "/languages/xyz"}]) == "xyz"
    assert metadata._ol_language(None) is None


def test_score_only_rewards_real_agreement():
    match = metadata.Candidate(
        source="openlibrary", title="Pride and Prejudice", author="Jane Austen", isbn="9780141439518"
    )
    assert metadata.score(match, isbn="9780141439518", title="Pride and Prejudice", author="Jane Austen") == 16
    # Same title, wrong author: no ISBN or author credit, just the title.
    other = metadata.Candidate(source="openlibrary", title="Pride and Prejudice", author="Someone Else")
    assert metadata.score(other, isbn="9780141439518", title="Pride and Prejudice", author="Someone Else") == 8
    nothing = metadata.Candidate(source="x", title="Nothing Like It")
    assert metadata.score(nothing, isbn=None, title="Pride and Prejudice", author="Jane Austen") == 0


def test_lookup_ranks_the_record_that_matches(use):
    use(happy)
    results = metadata.lookup(isbn="9780141439518", title="Pride and Prejudice", author="Jane Austen")
    assert results and results[0].score >= metadata.MATCH_THRESHOLD
    # The first usable cover, skipping Open Library's -1 "no cover" placeholder.
    assert results[0].cover_url.endswith("12645114-L.jpg")
    assert [c.score for c in results] == sorted((c.score for c in results), reverse=True)


def test_lookup_survives_a_broken_source(use):
    def broken(request):
        raise httpx.ConnectError("offline")

    use(broken)
    assert metadata.lookup(isbn="9780141439518") == []


def test_enrich_reads_author_description_language_and_cover_from_the_edition(use):
    use(happy)
    found = metadata.enrich(isbn="9780141439518", title="Pride and Prejudice", author="Jane Austen")
    assert found is not None
    assert found.fields["title"] == "Pride and Prejudice"
    assert found.fields["author"] == "Jane Austen"
    assert found.fields["description"] == "When Elizabeth Bennet first meets Fitzwilliam Darcy."
    assert found.fields["publisher"] == "Penguin Books"
    assert found.fields["published_at"] == "2003"
    assert found.fields["language"] == "en"
    assert found.cover_bytes == b"\xff\xd8\xff\xd9"
    assert found.cover_type == "image/jpeg"
    assert found.source == "openlibrary"


def test_enrich_falls_back_to_the_work_record_for_a_blurb(use):
    def no_blurb(request):
        if "/isbn/9780141439518.json" in str(request.url):
            return httpx.Response(200, json=EDITION_NO_BLURB)
        if "/works/OL66554W.json" in str(request.url):
            return httpx.Response(200, json=WORK)
        return httpx.Response(404)

    use(no_blurb)
    found = metadata.enrich(isbn="9780141439518", title="Pride and Prejudice", author="Jane Austen")
    assert found is not None
    assert found.fields["description"] == "A novel of manners and marriage."
    assert found.fields["author"] == "Jane Austen"


def test_enrich_still_identifies_a_book_with_no_title_or_author(use):
    """A file that arrives with no usable metadata must still be enrichable by ISBN alone."""
    use(happy)
    found = metadata.enrich(isbn="9780141439518", title="", author="")
    assert found is not None
    assert found.fields["title"] == "Pride and Prejudice"
    assert found.fields["author"] == "Jane Austen"


SCALA_SEARCH = {
    "docs": [
        {
            "key": "/works/OL16802393W",
            "title": "Functional Programming In Scala",
            "author_name": ["Paul Chiusano"],
            "first_publish_year": 2014,
            "isbn": ["1617290653", "1638353956"],
            "cover_i": 7866340,
            "publisher": ["Manning"],
            "language": ["eng"],
        }
    ]
}


def test_a_book_with_no_isbn_is_found_through_the_general_index(use):
    """Regression: title= and author= are ANDed exact-field filters, so a combined author string
    like "Paul Chiusano, Rúnar Bjarnason" alongside a title matched no record whatsoever and a
    perfectly ordinary book stayed completely bare."""
    seen = {}

    def handler(request):
        url = str(request.url)
        if "covers.openlibrary.org" in url:
            return httpx.Response(200, headers={"content-type": "image/jpeg"}, content=b"\xff\xd8")
        if "/search.json" in url:
            seen["params"] = dict(request.url.params)
            return httpx.Response(200, json=SCALA_SEARCH)
        if "googleapis" in url:
            return httpx.Response(429, json={})
        return httpx.Response(404)

    use(handler)
    found = metadata.enrich(
        isbn=None, title="Functional Programming in Scala", author="Paul Chiusano, Rúnar Bjarnason"
    )
    assert found is not None
    # The query must go to the general index, quoting the title and only the leading author.
    assert seen["params"]["q"] == 'title:"Functional Programming in Scala" author:"Paul Chiusano"'
    assert "title" not in seen["params"] and "author" not in seen["params"]
    # "In" vs "in" must not matter, and the rest of the author list must not block agreement.
    assert found.fields["title"] == "Functional Programming In Scala"
    assert found.fields["author"] == "Paul Chiusano"
    assert found.fields["publisher"] == "Manning"
    assert found.fields["published_at"] == "2014"
    assert found.fields["language"] == "en"
    assert found.cover_bytes == b"\xff\xd8"


def test_google_books_is_used_when_open_library_has_nothing(use):
    def google_only(request):
        url = str(request.url)
        if "googleapis" in url:
            return httpx.Response(200, json=GOOGLE)
        if "covers.openlibrary.org" in url:
            return httpx.Response(200, headers={"content-type": "image/jpeg"}, content=b"\xff\xd8")
        return httpx.Response(404)

    use(google_only)
    found = metadata.enrich(isbn="9780141439518", title="Pride and Prejudice", author="Jane Austen")
    assert found is not None
    assert found.source == "googlebooks"
    assert found.fields["description"] == "A novel of manners."
    # Google serves cover thumbnails over plain http; the stored URL must stay https.
    assert metadata.lookup(isbn="9780141439518")[0].cover_url.startswith("https://")


def test_enrich_refuses_a_weak_match(use):
    def unrelated(request):
        if "/search.json" in str(request.url):
            return httpx.Response(
                200,
                json={"docs": [{"key": "/works/OL1W", "title": "Something Else Entirely", "author_name": ["Nobody"]}]},
            )
        return httpx.Response(404)

    use(unrelated)
    assert metadata.enrich(isbn="9780141439518", title="Pride and Prejudice", author="Jane Austen") is None


def test_fetch_cover_rejects_non_images_and_oversized_bodies(use):
    def html(request):
        return httpx.Response(200, headers={"content-type": "text/html"}, content=b"<html></html>")

    use(html)
    assert metadata.fetch_cover("https://example.com/x") is None

    def huge(request):
        return httpx.Response(
            200,
            headers={"content-type": "image/jpeg", "content-length": str(metadata.MAX_COVER_BYTES + 1)},
            content=b"x",
        )

    use(huge)
    assert metadata.fetch_cover("https://example.com/x") is None
    assert metadata.fetch_cover(None) is None
