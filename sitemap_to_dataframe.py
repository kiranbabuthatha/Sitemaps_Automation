"""
Read a site's robots.txt, discover its Sitemaps (including nested sitemap
indexes and gzipped sitemaps), and load every URL into a pandas DataFrame.

Usage:
    python sitemap_to_dataframe.py https://www.example.com
    # or import and call extract_sitemap_urls("https://www.example.com")
"""

from __future__ import annotations

import gzip
import io
import sys
import xml.etree.ElementTree as ET
from urllib.parse import urljoin, urlparse

import pandas as pd
import requests

# A realistic browser-style User-Agent prevents most 401/403 "authentication"
# style blocks that sites apply to unknown clients.
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# Standard sitemap XML namespace
SITEMAP_NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}

REQUEST_TIMEOUT = 30


def _fetch(url: str) -> bytes:
    """GET a URL and return raw bytes. Raises on HTTP errors."""
    resp = requests.get(url, headers=DEFAULT_HEADERS, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.content


def get_sitemaps_from_robots(base_url: str) -> list[str]:
    """Parse robots.txt and return every Sitemap: URL listed."""
    parsed = urlparse(base_url)
    if not parsed.scheme:
        base_url = "https://" + base_url
        parsed = urlparse(base_url)
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"

    print(f"Fetching {robots_url}")
    try:
        text = _fetch(robots_url).decode("utf-8", errors="replace")
    except requests.RequestException as e:
        print(f"  Could not read robots.txt: {e}")
        return []

    sitemaps = []
    for line in text.splitlines():
        line = line.strip()
        # robots.txt directives are case-insensitive
        if line.lower().startswith("sitemap:"):
            url = line.split(":", 1)[1].strip()
            sitemaps.append(urljoin(robots_url, url))

    # Fallback: if no Sitemap directive, try the conventional location
    if not sitemaps:
        fallback = f"{parsed.scheme}://{parsed.netloc}/sitemap.xml"
        print(f"  No Sitemap directive found, trying {fallback}")
        sitemaps.append(fallback)

    return sitemaps


def _parse_sitemap_bytes(content: bytes) -> ET.Element | None:
    """Decompress if gzipped and parse XML. Returns root element or None."""
    # Auto-detect gzip by magic number
    if content[:2] == b"\x1f\x8b":
        content = gzip.decompress(content)
    try:
        return ET.fromstring(content)
    except ET.ParseError as e:
        print(f"  XML parse error: {e}")
        return None


def extract_urls_from_sitemap(sitemap_url: str, seen: set[str] | None = None) -> list[dict]:
    """
    Recursively walk a sitemap (or sitemap index) and return a list of
    dicts, one per <url> entry, with all available fields.
    """
    if seen is None:
        seen = set()
    if sitemap_url in seen:
        return []
    seen.add(sitemap_url)

    print(f"Fetching sitemap: {sitemap_url}")
    try:
        content = _fetch(sitemap_url)
    except requests.RequestException as e:
        print(f"  Failed: {e}")
        return []

    root = _parse_sitemap_bytes(content)
    if root is None:
        return []

    tag = root.tag.lower()
    rows: list[dict] = []

    # Case 1: <sitemapindex> -> recurse into each <sitemap>/<loc>
    if tag.endswith("sitemapindex"):
        for sm in root.findall("sm:sitemap", SITEMAP_NS):
            loc = sm.findtext("sm:loc", default="", namespaces=SITEMAP_NS).strip()
            if loc:
                rows.extend(extract_urls_from_sitemap(loc, seen))
        return rows

    # Case 2: <urlset> -> collect every <url>
    if tag.endswith("urlset"):
        for url_el in root.findall("sm:url", SITEMAP_NS):
            rows.append({
                "loc": url_el.findtext("sm:loc", default="", namespaces=SITEMAP_NS).strip(),
                "lastmod": url_el.findtext("sm:lastmod", default="", namespaces=SITEMAP_NS).strip(),
                "changefreq": url_el.findtext("sm:changefreq", default="", namespaces=SITEMAP_NS).strip(),
                "priority": url_el.findtext("sm:priority", default="", namespaces=SITEMAP_NS).strip(),
                "sitemap_source": sitemap_url,
            })
        return rows

    print(f"  Unknown root element: {root.tag}")
    return rows


def extract_sitemap_urls(base_url: str) -> pd.DataFrame:
    """End-to-end: site URL -> DataFrame of every URL found in its sitemaps."""
    sitemaps = get_sitemaps_from_robots(base_url)
    if not sitemaps:
        return pd.DataFrame(columns=["loc", "lastmod", "changefreq", "priority", "sitemap_source"])

    all_rows: list[dict] = []
    seen: set[str] = set()
    for sm in sitemaps:
        all_rows.extend(extract_urls_from_sitemap(sm, seen))

    df = pd.DataFrame(all_rows)
    if not df.empty:
        df = df.drop_duplicates(subset=["loc"]).reset_index(drop=True)
    return df


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python sitemap_to_dataframe.py <site_url>")
        sys.exit(1)

    site = sys.argv[1]
    df = extract_sitemap_urls(site)

    print(f"\nFound {len(df)} unique URLs")
    if not df.empty:
        print(df.head(10).to_string(index=False))
        out_path = "sitemap_urls.csv"
        df.to_csv(out_path, index=False)
        print(f"\nSaved to {out_path}")
