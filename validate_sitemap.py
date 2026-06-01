"""
Sitemap Validator
=================
Validate a sitemap (urlset) or sitemap index against Google's sitemap rules.
Domain-agnostic: works on any sitemap file or any host.

Checks (per https://developers.google.com/search/docs/crawling-indexing/sitemaps):
  - File is well-formed XML with the correct root element + namespace
  - Every <loc> is an absolute http(s) URL
  - All <loc> share one host (Google requires same-host URLs per sitemap)
  - <= 50,000 URLs per file
  - <= 50 MB uncompressed per file
  - Each URL <= 2,048 characters
  - <lastmod>, when present, is a valid W3C datetime
  - No duplicate <loc> values

A sitemap index is validated structurally and each child <loc> is checked
to be an absolute URL. Pass --recurse to also fetch and validate each child
sitemap over HTTP (read-only).

Usage:
    python validate_sitemap.py path/or/url/to/sitemap.xml
    python validate_sitemap.py https://example.com/sitemap_index.xml --recurse

Exit code 0 = valid, 1 = problems found.
"""

from __future__ import annotations

import gzip
import re
import sys
import argparse
import xml.etree.ElementTree as ET
from urllib.parse import urlparse

SITEMAP_NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
SITEMAP_NS_URI = "http://www.sitemaps.org/schemas/sitemap/0.9"

MAX_URLS = 50_000
MAX_BYTES = 50 * 1024 * 1024
MAX_URL_LEN = 2_048

# W3C datetime accepted by sitemaps: YYYY, YYYY-MM, YYYY-MM-DD, or full
# datetime with timezone (e.g. 2024-01-15T10:30:00+00:00 / ...Z).
_W3C_DATETIME = re.compile(
    r"^\d{4}"
    r"(-\d{2}"
    r"(-\d{2}"
    r"(T\d{2}:\d{2}(:\d{2}(\.\d+)?)?"
    r"(Z|[+-]\d{2}:\d{2}))?"
    r")?"
    r")?$"
)


class ValidationResult:
    def __init__(self, source: str):
        self.source = source
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.url_count = 0
        self.byte_size = 0
        self.kind = "unknown"  # "urlset" | "sitemapindex"

    def error(self, msg: str) -> None:
        self.errors.append(msg)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)

    @property
    def ok(self) -> bool:
        return not self.errors

    def report(self) -> str:
        lines = [f"\n── Validating: {self.source} ──"]
        lines.append(f"  type: {self.kind}   urls/children: {self.url_count}   "
                     f"size: {self.byte_size / 1024:.1f} KB")
        if self.errors:
            lines.append(f"  ✖ {len(self.errors)} error(s):")
            lines += [f"     - {e}" for e in self.errors]
        if self.warnings:
            lines.append(f"  ⚠ {len(self.warnings)} warning(s):")
            lines += [f"     - {w}" for w in self.warnings]
        if self.ok and not self.warnings:
            lines.append("  ✔ valid, no warnings")
        elif self.ok:
            lines.append("  ✔ valid (with warnings)")
        return "\n".join(lines)


def _read_bytes(source: str) -> bytes:
    """Read a local path or fetch an http(s) URL. Gzip auto-decompressed."""
    if source.startswith(("http://", "https://")):
        import requests  # lazy import: only needed for URLs
        resp = requests.get(source, timeout=30, headers={
            "User-Agent": "sitemap-validator/1.0 (+https://www.sitemaps.org)"
        })
        resp.raise_for_status()
        content = resp.content
    else:
        with open(source, "rb") as fh:
            content = fh.read()
    if content[:2] == b"\x1f\x8b":
        content = gzip.decompress(content)
    return content


def _localname(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def validate(source: str, recurse: bool = False, _seen: set | None = None) -> list[ValidationResult]:
    """Validate one sitemap. Returns a list of results (more than one if
    recursing into a sitemap index)."""
    if _seen is None:
        _seen = set()
    if source in _seen:
        return []
    _seen.add(source)

    res = ValidationResult(source)
    results = [res]

    try:
        content = _read_bytes(source)
    except Exception as e:
        res.error(f"could not read source: {e}")
        return results

    res.byte_size = len(content)
    if res.byte_size > MAX_BYTES:
        res.error(f"file is {res.byte_size / 1024 / 1024:.1f} MB, exceeds 50 MB limit")

    try:
        root = ET.fromstring(content)
    except ET.ParseError as e:
        res.error(f"malformed XML: {e}")
        return results

    # Namespace check
    if SITEMAP_NS_URI not in root.tag:
        res.warn(f"root element not in sitemaps.org 0.9 namespace: {root.tag}")

    kind = _localname(root.tag)
    res.kind = kind

    if kind == "sitemapindex":
        children = _validate_index(root, res)
        if recurse:
            for child in children:
                results += validate(child, recurse=True, _seen=_seen)
    elif kind == "urlset":
        _validate_urlset(root, res)
    else:
        res.error(f"unknown root element <{kind}>, expected <urlset> or <sitemapindex>")

    return results


def _iter_locs(parent: ET.Element, child_tag: str):
    """Yield <loc> text under each <child_tag>, namespace-tolerant."""
    for el in parent:
        if _localname(el.tag) != child_tag:
            continue
        loc_text = ""
        lastmod_text = ""
        for sub in el:
            ln = _localname(sub.tag)
            if ln == "loc":
                loc_text = (sub.text or "").strip()
            elif ln == "lastmod":
                lastmod_text = (sub.text or "").strip()
        yield loc_text, lastmod_text


def _check_loc(loc: str, res: ValidationResult) -> str | None:
    """Validate a single <loc>. Returns its host, or None if invalid."""
    if not loc:
        res.error("empty <loc>")
        return None
    if len(loc) > MAX_URL_LEN:
        res.error(f"URL exceeds {MAX_URL_LEN} chars: {loc[:80]}...")
    parsed = urlparse(loc)
    if parsed.scheme not in ("http", "https"):
        res.error(f"<loc> is not an absolute http(s) URL: {loc[:120]}")
        return None
    if not parsed.netloc:
        res.error(f"<loc> has no host: {loc[:120]}")
        return None
    return parsed.netloc


def _check_lastmod(lastmod: str, loc: str, res: ValidationResult) -> None:
    if lastmod and not _W3C_DATETIME.match(lastmod):
        res.error(f"invalid W3C lastmod '{lastmod}' for {loc[:80]}")


def _validate_urlset(root: ET.Element, res: ValidationResult) -> None:
    hosts: set[str] = set()
    seen_locs: set[str] = set()
    count = 0

    for loc, lastmod in _iter_locs(root, "url"):
        count += 1
        host = _check_loc(loc, res)
        if host:
            hosts.add(host)
        if loc in seen_locs:
            res.error(f"duplicate <loc>: {loc[:120]}")
        seen_locs.add(loc)
        _check_lastmod(lastmod, loc, res)

    res.url_count = count
    if count == 0:
        res.warn("urlset contains no <url> entries")
    if count > MAX_URLS:
        res.error(f"{count} URLs, exceeds 50,000 limit")
    if len(hosts) > 1:
        res.error(f"URLs span multiple hosts ({sorted(hosts)}); "
                  f"Google requires one host per sitemap")


def _validate_index(root: ET.Element, res: ValidationResult) -> list[str]:
    children: list[str] = []
    seen: set[str] = set()
    count = 0

    for loc, lastmod in _iter_locs(root, "sitemap"):
        count += 1
        _check_loc(loc, res)
        if loc in seen:
            res.error(f"duplicate child sitemap <loc>: {loc[:120]}")
        seen.add(loc)
        _check_lastmod(lastmod, loc, res)
        if loc:
            children.append(loc)

    res.url_count = count
    if count == 0:
        res.warn("sitemapindex contains no <sitemap> entries")
    if count > MAX_URLS:
        res.error(f"{count} child sitemaps, exceeds 50,000 limit")
    return children


def validate_path(source: str, recurse: bool = False) -> bool:
    """Validate and print a report. Returns True if everything is valid."""
    results = validate(source, recurse=recurse)
    all_ok = True
    for r in results:
        print(r.report())
        all_ok = all_ok and r.ok
    total_err = sum(len(r.errors) for r in results)
    print(f"\n  {'✔ ALL VALID' if all_ok else f'✖ {total_err} error(s) across {len(results)} file(s)'}")
    return all_ok


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Validate a sitemap against Google's rules.")
    ap.add_argument("source", help="Local path or http(s) URL to a sitemap / sitemap index")
    ap.add_argument("--recurse", action="store_true",
                    help="For an index, fetch and validate each child sitemap (read-only)")
    args = ap.parse_args()

    ok = validate_path(args.source, recurse=args.recurse)
    sys.exit(0 if ok else 1)
