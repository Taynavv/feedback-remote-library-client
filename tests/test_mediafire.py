from __future__ import annotations

import base64
import io
import sys
from pathlib import Path
from urllib import error

import pytest

from remote_library_client.mediafire import (
    FILE_GONE_MESSAGE,
    NO_LINK_MESSAGE,
    UNEXPECTED_PAGE_MESSAGE,
    direct_url_from_page,
    download_mediafire_file,
    is_mediafire_url,
)
from remote_library_client.provider import BaseLibraryProvider

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DIRECT_URL = "https://download1583.mediafire.com/abcTOKENxyz/fakekey01234567/Fake+Artist+-+Fake+Song.feedpak"


def _page(anchor: str) -> str:
    return f"<html><body><div class='download_link'>{anchor}</div></body></html>"


# Replicates the live page shape (verified 2026-07-19): a multi-line anchor with href
# BEFORE id, and a '+'-encoded filename in the direct URL.
LIVE_SHAPE_PAGE = _page(
    '<a class="input popsok"\n'
    '           aria-label="Download file"\n'
    f'           href="{DIRECT_URL}"           id="downloadButton"\n'
    '           rel="nofollow">Download (4.2MB)</a>'
)


class _Resp:
    """Minimal urllib response stand-in: context manager + read() + headers + geturl()."""

    def __init__(self, body=b"", headers=None, url="https://www.mediafire.com/"):
        self._chunks = list(body) if isinstance(body, list) else [body]
        self.headers = headers or {}
        self._url = url

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _size=-1):
        return self._chunks.pop(0) if self._chunks else b""

    def geturl(self):
        return self._url


def _http_error(url: str, code: int) -> error.HTTPError:
    return error.HTTPError(url, code, "error", {}, io.BytesIO(b""))


class FakeProvider(BaseLibraryProvider):
    """Provider with the network stubbed: canned responses (or exceptions) in order."""

    def __init__(self, cache_dir, responses):
        super().__init__(
            {"providerId": "test:mediafire", "label": "Fake"}, cache_dir,
            origin_host="www.mediafire.com",
        )
        self._responses = list(responses)
        self.opened: list[str] = []

    def _urlopen(self, req, timeout):
        self.opened.append(req.full_url)
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


# --------------------------------------------------------------------------- URL detection


@pytest.mark.parametrize("url,expected", [
    ("https://www.mediafire.com/file/fakekey01234567/Fake_Song.feedpak/file", True),
    ("https://mediafire.com/file/fakekey01234567/Fake_Song.feedpak/file", True),
    (DIRECT_URL, True),
    ("https://drive.google.com/file/d/FID12345678/view", False),
    ("https://www.dropbox.com/scl/fi/x/Song.feedpak?dl=0", False),
    ("https://notmediafire.com/file/x", False),
    ("", False),
])
def test_is_mediafire_url(url, expected):
    assert is_mediafire_url(url) is expected


# --------------------------------------------------------------------------- page parsing


def test_direct_url_from_live_page_shape():
    assert direct_url_from_page(LIVE_SHAPE_PAGE) == DIRECT_URL


def test_direct_url_unescapes_html_entities():
    page = _page('<a id="downloadButton" href="https://download7.mediafire.com/a&amp;b/k/Fake.feedpak">x</a>')
    assert direct_url_from_page(page) == "https://download7.mediafire.com/a&b/k/Fake.feedpak"


def test_direct_url_falls_back_to_scrambled_attribute():
    scrambled = base64.b64encode(DIRECT_URL.encode()).decode()
    page = _page(
        f'<a href="javascript:void(0)" data-scrambled-url="{scrambled}" id="downloadButton">x</a>'
    )
    assert direct_url_from_page(page) == DIRECT_URL


def test_direct_url_prefers_plain_href_over_scrambled():
    scrambled = base64.b64encode(b"https://download9.mediafire.com/other/k/Other.feedpak").decode()
    page = _page(f'<a href="{DIRECT_URL}" data-scrambled-url="{scrambled}" id="downloadButton">x</a>')
    assert direct_url_from_page(page) == DIRECT_URL


def test_dead_file_page_raises_gone():
    page = "<html><body><h1>Invalid or Deleted File.</h1></body></html>"
    with pytest.raises(RuntimeError, match="no longer available"):
        direct_url_from_page(page)
    assert FILE_GONE_MESSAGE  # message constant stays user-facing


def test_page_without_button_raises_no_link():
    with pytest.raises(RuntimeError, match="layout may have changed"):
        direct_url_from_page("<html><body>nothing here</body></html>")


def test_scraped_target_must_stay_on_mediafire():
    page = _page('<a id="downloadButton" href="https://evil.example.com/x.feedpak">x</a>')
    with pytest.raises(RuntimeError) as excinfo:
        direct_url_from_page(page)
    assert str(excinfo.value) == NO_LINK_MESSAGE


def test_unparseable_scrambled_attribute_raises_no_link():
    page = _page('<a href="javascript:void(0)" data-scrambled-url="%%%not-base64%%%" id="downloadButton">x</a>')
    with pytest.raises(RuntimeError) as excinfo:
        direct_url_from_page(page)
    assert str(excinfo.value) == NO_LINK_MESSAGE


# ----------------------------------------------------------------------------- download


def test_download_scrapes_page_then_streams(tmp_path):
    responses = [
        _Resp(LIVE_SHAPE_PAGE.encode(), {"content-type": "text/html; charset=UTF-8"}),
        _Resp([b"PK\x03\x04fake-package"], {"content-type": "application/zip"}),
    ]
    provider = FakeProvider(tmp_path / "cache", responses)

    target, content_hash, size, _headers = download_mediafire_file(
        provider,
        "https://www.mediafire.com/file/fakekey01234567/Fake_Song.feedpak/file",
        "Fake Artist - Fake Song.feedpak",
    )

    assert target.read_bytes() == b"PK\x03\x04fake-package"
    assert size == len(b"PK\x03\x04fake-package")
    assert content_hash
    assert provider.opened == [
        "https://www.mediafire.com/file/fakekey01234567/Fake_Song.feedpak/file",
        DIRECT_URL,
    ]


def test_download_streams_direct_file_response_without_scraping(tmp_path):
    responses = [_Resp([b"PK\x03\x04direct"], {"content-type": "application/octet-stream"})]
    provider = FakeProvider(tmp_path / "cache", responses)

    target, _hash, size, _headers = download_mediafire_file(
        provider, DIRECT_URL, "Fake Artist - Fake Song.feedpak"
    )

    assert target.read_bytes() == b"PK\x03\x04direct"
    assert size == len(b"PK\x03\x04direct")
    assert len(provider.opened) == 1  # no page scrape needed


def test_download_dead_file_404_raises_gone(tmp_path):
    # A dead link redirects to error.php, which answers 404 (verified live).
    responses = [_http_error("https://www.mediafire.com/error.php?errno=320&origin=download", 404)]
    provider = FakeProvider(tmp_path / "cache", responses)

    with pytest.raises(RuntimeError) as excinfo:
        download_mediafire_file(provider, "https://www.mediafire.com/file/gonekey0123456/x.feedpak/file", "x.feedpak")

    assert str(excinfo.value) == FILE_GONE_MESSAGE


def test_download_html_instead_of_file_raises(tmp_path):
    responses = [
        _Resp(LIVE_SHAPE_PAGE.encode(), {"content-type": "text/html; charset=UTF-8"}),
        _Resp(b"<html>another page</html>", {"content-type": "text/html"}),
    ]
    provider = FakeProvider(tmp_path / "cache", responses)

    with pytest.raises(RuntimeError) as excinfo:
        download_mediafire_file(provider, "https://www.mediafire.com/file/fakekey01234567/x.feedpak/file", "x.feedpak")

    assert str(excinfo.value) == UNEXPECTED_PAGE_MESSAGE
