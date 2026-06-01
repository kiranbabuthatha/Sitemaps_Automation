"""
Sitemap Update Workflow  (domain-agnostic orchestrator)
=======================================================
Implements the mermaid workflow end to end, for ANY domain:

    Start
      1. Pull existing sitemaps from robots.txt   -> sitemap_to_dataframe.py
      2. Load new URLs from an input file (.csv/.txt, column "url")
      3. Deduplicate: drop new URLs already in the existing sitemap
      4. Generate updated sitemap (existing + new unique) -> sitemap_generator.py
         + validate output -> validate_sitemap.py
      5. Upload to GSC  -> gsc_sitemap_uploader.py   (separate, confirmation-gated)
    Done / on failure: log + notify -> retry or manual review

Nothing here is hardcoded to one site. Pass --site / --base-url for whatever
domain you are working on. Steps 1 to 4 are read-only or write only LOCAL
files. Step 5 (submit) writes to your live GSC property and NEVER runs without
explicit confirmation.

-------------------------------------------------------------------------------
USAGE
-------------------------------------------------------------------------------
# Full update for an existing site (steps 1 to 4):
    python sitemap_workflow.py update \
        --site https://example.com \
        --input new_urls.csv \
        --base-url https://example.com \
        --group-by path_depth --depth 1 \
        --out ./sitemaps

# Brand-new site / no existing sitemap (skip the pull, generate from input):
    python sitemap_workflow.py update \
        --input new_urls.csv \
        --base-url https://example.com \
        --group-by single \
        --out ./sitemaps

# Validate any sitemap (local or live), optionally walking an index:
    python sitemap_workflow.py validate ./sitemaps/sitemap_index.xml
    python sitemap_workflow.py validate https://example.com/sitemap_index.xml --recurse

# Upload to GSC (asks for confirmation; --yes to skip the prompt):
    python sitemap_workflow.py submit \
        --site https://example.com/ \
        --out ./sitemaps --base-url https://example.com \
        --credentials client_secrets.json --auth-mode oauth

-------------------------------------------------------------------------------
GROUPING STRATEGIES  (--group-by)
-------------------------------------------------------------------------------
    path_depth     one sitemap per first-N path segments (use --depth N),
                   small groups merged into "other"  [default, original logic]
    first_segment  one sitemap per top-level path segment (/en/... -> en)
    single         one sitemap for the whole site (auto-splits past 50k URLs)
    lastmod_month  one sitemap per YYYY-MM of <lastmod> (undated -> "undated")

To add your own: drop a function into GROUPERS below, signature
    group_func(url: str, entry: dict) -> str
or import generate_sitemaps() directly and pass group_func=...
"""

from __future__ import annotations

import os
import sys
import csv
import argparse
from datetime import datetime, timezone
from urllib.parse import urlparse

# Local modules (all in this folder)
from sitemap_generator import generate_sitemaps
from validate_sitemap import validate_path

LOG_FILE = "SITEMAP_LOG.md"


# ─────────────────────────────────────────────
# Grouping strategy registry
# ─────────────────────────────────────────────

def _first_segment(url: str, entry: dict) -> str:
    segs = [s for s in urlparse(url).path.split("/") if s]
    return segs[0].split(".")[0] if segs else "root"


def _single(url: str, entry: dict) -> str:
    return "all"


def _lastmod_month(url: str, entry: dict) -> str:
    lm = (entry.get("lastmod") or "").strip()
    return lm[:7] if len(lm) >= 7 else "undated"


# name -> group_func  (None means "use built-in path_depth grouping")
GROUPERS = {
    "path_depth": None,
    "first_segment": _first_segment,
    "single": _single,
    "lastmod_month": _lastmod_month,
}


# ─────────────────────────────────────────────
# Step 1: pull existing sitemap URLs
# ─────────────────────────────────────────────

def pull_existing(site: str) -> list[dict]:
    """Return existing sitemap entries [{loc, lastmod}, ...] for `site`.
    Empty list if no site given or nothing found."""
    if not site:
        print("  Step 1: no --site given, treating existing sitemap as empty")
        return []
    try:
        from sitemap_to_dataframe import extract_sitemap_urls
    except ImportError as e:
        print(f"  Step 1: cannot import sitemap_to_dataframe ({e}); skipping pull")
        return []

    print(f"  Step 1: pulling existing sitemaps for {site}")
    df = extract_sitemap_urls(site)
    if df is None or df.empty:
        print("  Step 1: no existing URLs found")
        return []
    entries = []
    for _, row in df.iterrows():
        loc = str(row.get("loc", "")).strip()
        if not loc:
            continue
        e = {"loc": loc}
        lm = str(row.get("lastmod", "")).strip()
        if lm and lm.lower() != "nan":
            e["lastmod"] = lm
        entries.append(e)
    print(f"  Step 1: {len(entries)} existing URL(s)")
    return entries


# ─────────────────────────────────────────────
# Step 2: load new URLs from input file
# ─────────────────────────────────────────────

def load_new_urls(input_path: str) -> list[dict]:
    """Load new URLs from a .csv (column 'url', optional 'lastmod') or a
    .txt (one URL per line). Returns [{loc, lastmod?}, ...]."""
    if not os.path.exists(input_path):
        raise FileNotFoundError(f"input file not found: {input_path}")

    entries: list[dict] = []
    ext = os.path.splitext(input_path)[1].lower()

    if ext == ".csv":
        with open(input_path, newline="", encoding="utf-8-sig") as fh:
            reader = csv.DictReader(fh)
            cols = {c.lower(): c for c in (reader.fieldnames or [])}
            if "url" not in cols:
                raise ValueError("CSV must have a 'url' column")
            for row in reader:
                loc = (row.get(cols["url"]) or "").strip()
                if not loc:
                    continue
                e = {"loc": loc}
                if "lastmod" in cols:
                    lm = (row.get(cols["lastmod"]) or "").strip()
                    if lm:
                        e["lastmod"] = lm
                entries.append(e)
    else:  # treat as plain text, one URL per line
        with open(input_path, encoding="utf-8") as fh:
            for line in fh:
                loc = line.strip()
                if loc and not loc.startswith("#"):
                    entries.append({"loc": loc})

    print(f"  Step 2: loaded {len(entries)} new URL(s) from {input_path}")
    return entries


# ─────────────────────────────────────────────
# Step 3: deduplicate
# ─────────────────────────────────────────────

def deduplicate(existing: list[dict], new: list[dict]) -> tuple[list[dict], int]:
    """Return (new_unique, removed_count). A new URL is a duplicate if its
    loc already appears in the existing set."""
    existing_locs = {e["loc"] for e in existing}
    seen_new: set[str] = set()
    unique: list[dict] = []
    removed = 0
    for e in new:
        loc = e["loc"]
        if loc in existing_locs or loc in seen_new:
            removed += 1
            continue
        seen_new.add(loc)
        unique.append(e)
    print(f"  Step 3: {removed} duplicate(s) removed, {len(unique)} new unique URL(s)")
    return unique, removed


# ─────────────────────────────────────────────
# Step 3b: filter to same host + valid absolute URLs
# ─────────────────────────────────────────────

def filter_same_host(entries: list[dict], base_url: str) -> tuple[list[dict], list[str]]:
    """Keep only absolute http(s) URLs on the same host as base_url.
    Returns (kept, dropped_locs). Google requires one host per sitemap."""
    host = urlparse(base_url).netloc
    kept, dropped = [], []
    for e in entries:
        p = urlparse(e["loc"])
        if p.scheme in ("http", "https") and p.netloc == host:
            kept.append(e)
        else:
            dropped.append(e["loc"])
    if dropped:
        print(f"  Step 3b: dropped {len(dropped)} URL(s) not on host '{host}':")
        for d in dropped[:10]:
            print(f"     - {d}")
        if len(dropped) > 10:
            print(f"     ... and {len(dropped) - 10} more")
    return kept, dropped


# ─────────────────────────────────────────────
# Step 4: generate (+ validate)
# ─────────────────────────────────────────────

def generate(
    entries: list[dict],
    out_dir: str,
    base_url: str,
    group_by: str,
    depth: int,
    min_urls_per_group: int,
    prefix: str,
    with_lastmod: bool,
    emit_legacy_tags: bool,
) -> dict:
    if group_by not in GROUPERS:
        raise ValueError(f"unknown --group-by '{group_by}'. "
                         f"Choices: {', '.join(GROUPERS)}")

    if not with_lastmod:
        entries = [{k: v for k, v in e.items() if k != "lastmod"} for e in entries]

    group_func = GROUPERS[group_by]
    print(f"  Step 4: generating sitemaps  (group-by={group_by}"
          f"{', depth=' + str(depth) if group_by == 'path_depth' else ''})")

    result = generate_sitemaps(
        urls=entries,
        depth=depth,
        output_dir=out_dir,
        base_url=base_url,
        prefix=prefix,
        min_urls_per_group=min_urls_per_group,
        group_func=group_func,
        emit_changefreq=emit_legacy_tags,
        emit_priority=emit_legacy_tags,
    )
    return result


def validate_outputs(out_dir: str, result: dict) -> bool:
    print("\n  Step 4b: validating generated files")
    targets = result.get("index_files", []) + result.get("sitemap_files", [])
    all_ok = True
    for fname in targets:
        ok = validate_path(os.path.join(out_dir, fname), recurse=False)
        all_ok = all_ok and ok
    return all_ok


# ─────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────

def append_log(lines: list[str]) -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    block = [f"\n## Run {stamp}"] + [f"- {ln}" for ln in lines]
    header = "# Sitemap Workflow Log\n" if not os.path.exists(LOG_FILE) else ""
    with open(LOG_FILE, "a", encoding="utf-8") as fh:
        if header:
            fh.write(header)
        fh.write("\n".join(block) + "\n")


# ─────────────────────────────────────────────
# Commands
# ─────────────────────────────────────────────

def cmd_update(args) -> int:
    print("\n🚀 Sitemap Update Workflow")
    base_url = args.base_url or (
        f"{urlparse(args.site).scheme}://{urlparse(args.site).netloc}" if args.site else ""
    )
    if not base_url:
        print("  ✖ need --base-url (or --site) so sitemap index <loc> URLs are absolute")
        return 1

    existing = pull_existing(args.site)
    new = load_new_urls(args.input)
    unique, removed = deduplicate(existing, new)

    combined = existing + unique
    combined, dropped = filter_same_host(combined, base_url)
    if not combined:
        print("  ✖ nothing to write: no same-host URLs remain")
        return 1

    result = generate(
        entries=combined,
        out_dir=args.out,
        base_url=base_url,
        group_by=args.group_by,
        depth=args.depth,
        min_urls_per_group=args.min_urls_per_group,
        prefix=args.prefix,
        with_lastmod=args.with_lastmod,
        emit_legacy_tags=args.legacy_tags,
    )
    valid = validate_outputs(args.out, result)

    append_log([
        f"site: {args.site or '(none)'}  base_url: {base_url}",
        f"input: {args.input}  group-by: {args.group_by}",
        f"existing: {len(existing)}  new: {len(new)}  duplicates removed: {removed}  "
        f"new unique: {len(unique)}  off-host dropped: {len(dropped)}  "
        f"total written: {result['total_urls']}",
        f"sitemaps: {len(result['sitemap_files'])}  index files: {len(result['index_files'])}",
        f"validation: {'PASS' if valid else 'FAIL'}",
        "submit: NOT run (use the submit command + confirmation)",
    ])

    print(f"\n  {'🎉 Done (steps 1 to 4). Files in ' + args.out if valid else '⚠ Generated, but validation FAILED. Fix before submitting.'}")
    print("  Step 5 (submit to GSC) is separate and confirmation-gated.")
    return 0 if valid else 1


def cmd_validate(args) -> int:
    ok = validate_path(args.source, recurse=args.recurse)
    return 0 if ok else 1


def cmd_submit(args) -> int:
    """Step 5. Guarded: writes to the live GSC property."""
    try:
        from gsc_sitemap_uploader import submit_sitemaps_to_gsc, sitemap_urls_from_result  # noqa
    except ImportError as e:
        print(f"  ✖ cannot import gsc_sitemap_uploader: {e}")
        return 1

    base = (args.base_url or "").rstrip("/")
    if not base:
        print("  ✖ --base-url required to build absolute sitemap URLs for submission")
        return 1

    # Collect the sitemap files to submit from the output dir.
    files = [f for f in sorted(os.listdir(args.out)) if f.endswith(".xml")]
    index_files = [f for f in files if "index" in f]
    submit_list = [f"{base}/{f}" for f in (index_files or files)]

    if not submit_list:
        print(f"  ✖ no .xml sitemap files found in {args.out}")
        return 1

    print("\n  ☁️  About to submit these sitemaps to GSC property "
          f"'{args.site}':")
    for u in submit_list:
        print(f"     - {u}")

    if not args.yes:
        print("\n  This writes to your LIVE Search Console property.")
        ans = input("  Type 'yes' to proceed: ").strip().lower()
        if ans != "yes":
            print("  Aborted. Nothing submitted.")
            return 1

    result = submit_sitemaps_to_gsc(
        site_url=args.site,
        sitemap_files=submit_list,
        credentials_path=args.credentials,
        auth_mode=args.auth_mode,
    )
    ok = not result.get("failed")
    append_log([
        f"SUBMIT to {args.site}: {len(result.get('submitted', []))} submitted, "
        f"{len(result.get('skipped', []))} skipped, {len(result.get('failed', []))} failed",
    ])
    if not ok:
        print("  ⚠️  Some sitemaps failed. Log the error and review manually.")
    return 0 if ok else 1


# ─────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Domain-agnostic sitemap update workflow (mermaid steps 1 to 5).")
    sub = p.add_subparsers(dest="command", required=True)

    up = sub.add_parser("update", help="Steps 1 to 4: pull, load, dedup, generate, validate")
    up.add_argument("--site", default="", help="Existing site to pull current sitemaps from (optional)")
    up.add_argument("--input", required=True, help="New URLs file (.csv with 'url' column, or .txt)")
    up.add_argument("--base-url", default="", help="Absolute base for sitemap index <loc> (defaults to --site host)")
    up.add_argument("--group-by", default="path_depth", choices=list(GROUPERS),
                    help="Grouping strategy (default: path_depth)")
    up.add_argument("--depth", type=int, default=1, help="Path depth for path_depth grouping")
    up.add_argument("--min-urls-per-group", type=int, default=2, help="Merge groups smaller than this into 'other'")
    up.add_argument("--prefix", default="sitemap", help="Output filename prefix")
    up.add_argument("--out", default="./sitemaps", help="Output directory")
    up.add_argument("--with-lastmod", action="store_true", help="Emit <lastmod> when present in the data")
    up.add_argument("--legacy-tags", action="store_true",
                    help="Also emit <changefreq>/<priority> (Google ignores these)")
    up.set_defaults(func=cmd_update)

    va = su