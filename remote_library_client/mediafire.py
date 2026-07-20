# SPDX-License-Identifier: AGPL-3.0-or-later
"""MediaFire public-file downloads (stdlib-only), for FeedForge-hosted songs.

FeedForge added MediaFire as an upload host (2026-07); a song's resolved download link
then looks like ``https://www.mediafire.com/file/<key>/<name>/file``. That URL serves an
HTML download *page*, not the file: the real bytes hang off the page's ``#downloadButton``
anchor, whose ``href`` is a per-request ``download####.mediafire.com`` URL (page shape
verified live 2026-07-19). This module extracts that URL and streams the file through the
provider's guarded opener — the same ``(provider, …) -> (path, hash, size, headers)``
shape as :func:`remote_library_client.google_drive.download_drive_file`, so the FeedForge
type dispatches to it exactly like the Drive path.

A dead or removed file redirects to ``error.php``, which answers HTTP 404 ("Invalid or
Deleted File"); that becomes a clear user-facing error rather than an HTML page silently
cached as a package.
"""
from __future__ import annotations

import base64
import html
import re
from pathlib import Path
from urllib import error, parse, request

from remote_library_client.provider import (
    MAX_JSON_RESPONSE_BYTES,
    BaseLibraryProvider,
    _read_limited,
    _remote_error,
)

MEDIAFIRE_HOST = "mediafire.com"

FILE_GONE_MESSAGE = (
    "the MediaFire file for this song is no longer available (removed or blocked by MediaFire)"
)
NO_LINK_MESSAGE = (
    "could not find the download link on the MediaFire page (its layout may have changed)"
)
UNEXPECTED_PAGE_MESSAGE = "MediaFire served another page instead of the file; try again later"

# The whole download-button anchor: attribute order varies (the live page puts href
# before id), so match the tag first and pull attributes out of it separately.
_BUTTON_RE = re.compile(r'<a\b[^>]*\bid="downloadButton"[^>]*>')
_HREF_RE = re.compile(r'\bhref="([^"]+)"')
_SCRAMBLED_RE = re.compile(r'\bdata-scrambled-url="([^"]+)"')
# Markers MediaFire renders for dead/blocked files. The common case is a redirect to
# error.php that 404s before any HTML matters, but 200-rendered error pages exist too.
_GONE_MARKERS = (
    "invalid or deleted file",
    "file blocked for violation",
    "file removed for violation",
)


def is_mediafire_url(url: str) -> bool:
    """True when the input points at a MediaFire host (``mediafire.com`` or a subdomain)."""
    host = (parse.urlparse(str(url or "").strip()).hostname or "").lower()
    return host == MEDIAFIRE_HOST or host.endswith("." + MEDIAFIRE_HOST)


def direct_url_from_page(html_text: str) -> str:
    """Extract the direct-download URL from a MediaFire file page.

    Prefers the ``#downloadButton`` anchor's plain ``href``; falls back to its
    ``data-scrambled-url`` attribute (a base64-encoded URL MediaFire serves in place of
    the href on some renders). Raises with a clear message for a dead-file page, a
    missing button, or a target that is not a MediaFire host.
    """
    text = str(html_text or "")
    lowered = text.lower()
    if any(marker in lowered for marker in _GONE_MARKERS):
        raise RuntimeError(FILE_GONE_MESSAGE)
    tag_match = _BUTTON_RE.search(text)
    if not tag_match:
        raise RuntimeError(NO_LINK_MESSAGE)
    tag = tag_match.group(0)
    candidate = ""
    href_match = _HREF_RE.search(tag)
    if href_match:
        href = html.unescape(href_match.group(1)).strip()
        if href.lower().startswith(("http://", "https://")):
            candidate = href
    if not candidate:
        scrambled_match = _SCRAMBLED_RE.search(tag)
        if scrambled_match:
            try:
                decoded = base64.b64decode(scrambled_match.group(1)).decode("utf-8")
            except (ValueError, UnicodeDecodeError):
                decoded = ""
            if decoded.lower().startswith(("http://", "https://")):
                candidate = decoded
    if not candidate or not is_mediafire_url(candidate):
        # Never follow a scraped link off MediaFire's own hosts — if the page layout
        # changes underneath these regexes, fail loudly instead of fetching who-knows-what.
        raise RuntimeError(NO_LINK_MESSAGE)
    return candidate


def download_mediafire_file(
    provider: BaseLibraryProvider, url: str, fallback_filename: str, headers: dict | None = None
) -> tuple[Path, str, int, dict]:
    """Download a MediaFire-hosted file into ``provider``'s cache.

    Fetches the share page through the provider's guarded opener, scrapes the direct
    download URL, and streams the bytes. A response that is already the file (e.g. a
    direct ``download####.mediafire.com`` link) streams immediately without a scrape.
    """
    request_headers = headers or {}
    req = request.Request(str(url or ""), headers=request_headers)
    try:
        with provider._urlopen(req, timeout=120) as response:
            content_type = (response.headers.get("content-type") or "").lower()
            if "text/html" not in content_type:
                return provider._stream_response_to_cache(response, fallback_filename)
            page = _read_limited(response, MAX_JSON_RESPONSE_BYTES).decode("utf-8", errors="replace")
    except error.HTTPError as exc:
        if exc.code in (404, 410):
            raise RuntimeError(FILE_GONE_MESSAGE) from exc
        raise _remote_error(exc) from exc
    direct_req = request.Request(direct_url_from_page(page), headers=request_headers)
    try:
        with provider._urlopen(direct_req, timeout=120) as response:
            content_type = (response.headers.get("content-type") or "").lower()
            if "text/html" in content_type:
                raise RuntimeError(UNEXPECTED_PAGE_MESSAGE)
            return provider._stream_response_to_cache(response, fallback_filename)
    except error.HTTPError as exc:
        if exc.code in (404, 410):
            raise RuntimeError(FILE_GONE_MESSAGE) from exc
        raise _remote_error(exc) from exc
