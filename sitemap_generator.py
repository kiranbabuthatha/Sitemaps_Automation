"""
Sitemap Generator
=================
Generates Google-compliant XML sitemaps from a list of URLs.

-------------------------------------------------------------------------------
Built by Kiran Babu Thatha — technical SEO + automation.
Want your sitemaps fully automated, with zero manual work (auto-pull existing
URLs, merge new ones, validate, and submit to Search Console on a schedule)?
Get in touch: https://www.kiranbabuthatha.com
-------------------------------------------------------------------------------

Google limits:
  - Max 50,000 URLs per sitemap file
  - Max 50MB (uncompressed) per sitemap file
  - Max 50,000 sitemaps per sitemap index file

New behaviours vs v1:
  • Groups with fewer than `min_urls_per_group` URLs (default 2) are merged
    into a single "other" group whose filename uses the parent path
    (depth - 1 segments).  If depth <= 1 the merged group is called "other".
  • Every segment used in a filename has the dot-suffix stripped:
    "page.html" → "page",  "en.US" → "en"

Usage:
    result = generate_sitemaps(
        urls               = ["https://example.com/en/page1", ...],
        depth              = 1,
        output_dir         = "./sitemaps",
        base_url           = "https://example.com",
        prefix             = "sitemap",
        min_urls_per_group = 2,     # groups with fewer urls → merged
    )
"""

import os
from datetime import datetime, timezone
from collections import defaultdict
from urllib.parse import urlparse
from xml.dom import minidom
from typing import Callable, Optional
import xml.etree.ElementTree as ET


# ─────────────────────────────────────────────
# Google hard limits
# ─────────────────────────────────────────────
GOOGLE_MAX_URLS_PER_SITEMAP   = 50_000
GOOGLE_MAX_SITEMAPS_PER_INDEX = 50_000
GOOGLE_MAX_BYTES_PER_SITEMAP  = 50 * 1024 * 1024   # 50 MB


# ─────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────

def generate_sitemaps(
    urls: list,
    depth: int,
    output_dir: str = "sitemaps",
    base_url: str = "",
    prefix: str = "sitemap",
    max_urls_per_sitemap: int = GOOGLE_MAX_URLS_PER_SITEMAP,
    min_urls_per_group: int = 2,
    default_changefreq: str = "weekly",
    default_priority: float = 0.5,
    group_func: Optional[Callable[[str, dict], str]] = None,
    emit_changefreq: bool = False,
    emit_priority: bool = False,
) -> dict:
    """
    Parameters
    ----------
    urls : list
        Plain URL strings  →  ["https://example.com/en/a", ...]
        OR dicts with optional fields:
            { "loc": "https://...", "lastmod": "2024-01-15",
              "changefreq": "weekly", "priority": "0.8" }

    depth : int
        Number of path segments (from the left) used to form the group key.

        depth=0  → one group  "root"
        depth=1  → /en → "en",  /fr → "fr"
        depth=2  → /en/tools → "en_tools"

    min_urls_per_group : int  (default 2)
        Groups with FEWER than this many URLs are merged into a single
        "other" group.  The other-group filename is derived from depth-1
        segments (one level up).  At depth <= 1 the name falls back to "other".

    group_func : callable (url: str, entry: dict) -> str, optional
        Custom grouping strategy.  If provided, it OVERRIDES the default
        path-depth grouping: the returned string is used as the group key
        (one sitemap file per distinct key).  `depth` is then ignored.
        Return a constant string (e.g. "all") to emit a single sitemap.
        If omitted, the original depth-based grouping is used unchanged.

    emit_changefreq / emit_priority : bool  (default False)
        Google ignores <changefreq> and <priority>, so they are omitted by
        default.  Set True to write them (uses default_changefreq /
        default_priority, or per-entry values when present).

    output_dir / base_url / prefix / max_urls_per_sitemap
        See inline comments.

    Returns
    -------
    dict  {
        "sitemap_files":  [...],
        "index_files":    [...],
        "total_urls":     int,
        "groups":         { group_key: url_count, ... },
        "merged_groups":  [ group_keys that were merged ],
    }
    """
    max_urls = min(max_urls_per_sitemap, GOOGLE_MAX_URLS_PER_SITEMAP)
    os.makedirs(output_dir, exist_ok=True)

    # ── 1. Normalise ────────────────────────────────────────────────────
    entries = _normalise_urls(
        urls, default_changefreq, default_priority,
        emit_changefreq=emit_changefreq, emit_priority=emit_priority,
    )

    # ── 2. Group: custom group_func if given, else path prefix at `depth` ─
    groups: dict[str, list] = defaultdict(list)
    for entry in entries:
        if group_func is not None:
            key = str(group_func(entry["loc"], entry)) or "other"
        else:
            key = _group_key(entry["loc"], depth)
        groups[key].append(entry)

    # ── 3. Merge small groups ────────────────────────────────────────────
    #   • Find groups whose URL count < min_urls_per_group
    #   • Compute the "other" bucket name  = depth-1 segments of a
    #     representative URL from that group  (strip dot-suffixes)
    #   • All small groups collapse into that single bucket
    small_keys   = [k for k, v in groups.items() if len(v) < min_urls_per_group]
    merged_into  = {}          # small_key → other_key  (for reporting)

    if small_keys:
        # Derive "other" key name from the first small group's first URL
        rep_url      = groups[small_keys[0]][0]["loc"]
        other_key    = _group_key(rep_url, max(depth - 1, 0)) or "other"
        if other_key in ("root", ""):
            other_key = "other"

        # Avoid collision: if other_key already exists as a large group,
        # append "_other" so we don't silently overwrite it
        if other_key in groups and other_key not in small_keys:
            other_key = other_key + "_other"

        merged_urls: list = []
        for k in small_keys:
            merged_urls.extend(groups.pop(k))
            merged_into[k] = other_key

        # If other_key itself was a small group (also being removed), start fresh
        if other_key in groups:
            groups[other_key].extend(merged_urls)
        else:
            groups[other_key] = merged_urls

        print(f"\n  ℹ Merged {len(small_keys)} small group(s) → '{other_key}' "
              f"({len(merged_urls)} URLs total)")
        for k in small_keys:
            print(f"      '{k}' absorbed")

    # ── 4. Write one (or more) .xml per group ───────────────────────────
    sitemap_files: list[str] = []

    for group_key in sorted(groups):
        group_entries = groups[group_key]
        chunks        = _split(group_entries, max_urls)

        for chunk_idx, chunk in enumerate(chunks):
            filename = (
                f"{prefix}_{group_key}.xml"
                if len(chunks) == 1
                else f"{prefix}_{group_key}_{chunk_idx}.xml"
            )
            filepath = os.path.join(output_dir, filename)
            _write_urlset(chunk, filepath)
            sitemap_files.append(filename)
            print(f"  ✔ {filename}  ({len(chunk):,} URLs)")

    # ── 5. Write sitemap-index file(s) ──────────────────────────────────
    index_chunks = _split(sitemap_files, GOOGLE_MAX_SITEMAPS_PER_INDEX)
    index_files: list[str] = []

    for idx, idx_chunk in enumerate(index_chunks):
        index_filename = (
            f"{prefix}_index.xml"
            if len(index_chunks) == 1
            else f"{prefix}_index_{idx}.xml"
        )
        index_filepath = os.path.join(output_dir, index_filename)
        _write_sitemapindex(idx_chunk, index_filepath, base_url)
        index_files.append(index_filename)
        print(f"  ✔ {index_filename}  ({len(idx_chunk):,} sitemaps)")

    summary = {
        "sitemap_files":  sitemap_files,
        "index_files":    index_files,
        "total_urls":     len(entries),
        "groups":         {k: len(v) for k, v in groups.items()},
        "merged_groups":  list(merged_into.keys()),
    }

    print(f"\n  Summary: {len(entries):,} URLs → "
          f"{len(sitemap_files)} sitemap(s) across "
          f"{len(groups)} group(s) in "
          f"{len(index_files)} index file(s)")
    return summary


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────

def _clean_segment(segment: str) -> str:
    """Strip everything after the first '.' in a path segment.

    Examples
    --------
    "page.html"  → "page"
    "en.US"      → "en"
    "compress"   → "compress"   (unchanged)
    """
    return segment.split(".")[0]


def _group_key(url: str, depth: int) -> str:
    """
    Build a filesystem-safe group key from the first `depth` path segments.

    Each segment has its dot-suffix stripped before joining.

    depth=0  → "root"
    depth=1  → "en"          from /en/pdf-converter
    depth=2  → "en_tools"    from /en/tools/page
    """
    if depth == 0:
        return "root"

    path     = urlparse(url).path                           # /en/pdf-converter/page
    segments = [s for s in path.split("/") if s]           # ['en','pdf-converter','page']

    if not segments:
        return "root"

    # Take up to `depth` segments, strip dot-suffix on each
    key_parts = [_clean_segment(s) for s in segments[:depth]]
    return "_".join(key_parts)


def _normalise_urls(
    urls: list,
    default_changefreq: str,
    default_priority: float,
    emit_changefreq: bool = False,
    emit_priority: bool = False,
) -> list:
    """Coerce URL entries to dicts. <changefreq>/<priority> are only added
    when their emit flag is set (Google ignores both, so off by default)."""
    result = []
    for item in urls:
        if isinstance(item, str):
            entry = {"loc": item}
        elif isinstance(item, dict):
            entry = dict(item)
        else:
            raise TypeError(f"Unsupported URL entry type: {type(item)}")

        if emit_changefreq:
            entry.setdefault("changefreq", default_changefreq)
        else:
            entry.pop("changefreq", None)
        if emit_priority:
            entry.setdefault("priority", str(default_priority))
        else:
            entry.pop("priority", None)
        result.append(entry)
    return result


def _split(items: list, chunk_size: int) -> list:
    return [items[i : i + chunk_size] for i in range(0, len(items), chunk_size)]


def _pretty_xml(root: ET.Element) -> str:
    raw    = ET.tostring(root, encoding="unicode")
    pretty = minidom.parseString(raw).toprettyxml(indent="  ", encoding=None)
    lines  = [ln for ln in pretty.splitlines() if ln.strip()]
    return "\n".join(lines) + "\n"


def _write_urlset(entries: list, filepath: str) -> None:
    root = ET.Element("urlset")
    root.set("xmlns", "http://www.sitemaps.org/schemas/sitemap/0.9")

    for entry in entries:
        url_el = ET.SubElement(root, "url")
        ET.SubElement(url_el, "loc").text = entry["loc"]

        if entry.get("lastmod"):
            ET.SubElement(url_el, "lastmod").text = entry["lastmod"]
        if entry.get("changefreq"):
            ET.SubElement(url_el, "changefreq").text = entry["changefreq"]
        if entry.get("priority") is not None:
            ET.SubElement(url_el, "priority").text = str(entry["priority"])

    with open(filepath, "w", encoding="utf-8") as fh:
        fh.write(_pretty_xml(root))


def _write_sitemapindex(filenames: list, filepath: str, base_url: str) -> None:
    root  = ET.Element("sitemapindex")
    root.set("xmlns", "http://www.sitemaps.org/schemas/sitemap/0.9")
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    for filename in filenames:
        sitemap_el = ET.SubElement(root, "sitemap")
        loc_text   = (
            f"{base_url.rstrip('/')}/{filename}" if base_url else filename
        )
        ET.SubElement(sitemap_el, "loc").text     = loc_text
        ET.SubElement(sitemap_el, "lastmod").text = today

    with open(filepath, "w", encoding="utf-8") as fh:
        fh.write(_pretty_xml(root))


# ─────────────────────────────────────────────
# Demo
# ─────────────────────────────────────────────

if __name__ == "__main__":

    sample_urls = [
        # English — 6 URLs  → large group, kept as "en"
        "https://example.com/en/pdf-converter",
        "https://example.com/en/compress-pdf",
        "https://example.com/en/pdf-to-word",
        "https://example.com/en/pdf-to-excel",
        "https://example.com/en/pdf-to-ppt",
        "https://example.com/en/sign-pdf",
        # Chinese — 3 URLs  → large group, kept as "cn"
        "https://example.com/cn/pdf-converter",
        "https://example.com/cn/compress-pdf",
        "https://example.com/cn/pdf-to-word",
        # German — 1 URL   → small group, merged into "other"
        "https://example.com/de/pdf-converter",
        # Root-level — 1 URL each → small groups, merged into "other"
        "https://example.com/about",
        "https://example.com/pricing",
        # Dot-suffix in segment — should be stripped
        "https://example.com/en/page.html",
        "https://example.com/en/faq.php",
    ]

    result = generate_sitemaps(
        urls               = sample_urls,
        depth              = 1,
        output_dir         = "./demo_sitemaps_v2",
        base_url           = "https://example.com",
        prefix             = "sitemap",
        min_urls_per_group = 2,          # groups with < 2 URLs → merged
        max_urls_per_sitemap = 5,        # small limit to demo splitting
    )

    print("\nFinal groups:")
    for g, c in result["groups"].items():
        print(f"  {g}: {c} URLs")

    print("\nMerged (were small):", result["merged_groups"])
