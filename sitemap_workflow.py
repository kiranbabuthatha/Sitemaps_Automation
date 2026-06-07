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
import shutil
import argparse
from datetime import datetime, timezone
from urllib.parse import urlparse, urlunparse

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
# Trailing-slash normalization
# ─────────────────────────────────────────────

def make_normalizer(mode: str):
    """Return a function loc -> loc that normalizes trailing slashes.

    mode:
      "keep"  -> identity (URLs untouched; exact-match dedupe)
      "add"   -> force a trailing slash  (/blog -> /blog/)
      "strip" -> remove a trailing slash (/blog/ -> /blog)

    The root ('/'), URLs with a query or fragment, and file-like last segments
    (anything with a '.', e.g. /sitemap.xml, /a.pdf) are ALWAYS left untouched,
    since rewriting those would change their meaning."""
    if mode not in ("keep", "add", "strip"):
        raise ValueError(f"unknown trailing-slash mode '{mode}'")
    if mode == "keep":
        return lambda loc: loc

    def normalize(loc: str) -> str:
        p = urlparse(loc)
        path = p.path
        last = path.rsplit("/", 1)[-1]
        if path in ("", "/") or p.query or p.fragment or "." in last:
            return loc                              # leave special cases alone
        if mode == "add" and not path.endswith("/"):
            path += "/"
        elif mode == "strip":
            path = path.rstrip("/") or "/"
        return urlunparse((p.scheme, p.netloc, path, p.params, p.query, p.fragment))

    return normalize


# ─────────────────────────────────────────────
# Step 3: merge new URLs into existing (dedupe by loc only)
# ─────────────────────────────────────────────

def merge_entries(
    existing: list[dict],
    new: list[dict],
    normalize=None,
    stamp_date: str | None = None,
) -> tuple[list[dict], int, int]:
    """Merge `new` URLs into `existing`, identifying duplicates by <loc> ONLY
    (lastmod is never part of the identity check).

    - URL already present  -> update its <lastmod> in place with the new value
                              (does not create a second entry).
    - URL not present       -> append as a new entry.

    `normalize` (loc -> loc, e.g. from make_normalizer) is applied to every loc
    before matching AND stored on the entry, so dedupe is slash-insensitive and
    the emitted URLs share one canonical form.

    `stamp_date` (e.g. today's "YYYY-MM-DD"): when set, an input URL that matches
    an existing entry which ALREADY had a <lastmod> gets that date refreshed to
    `stamp_date`. An explicit lastmod in the input still takes precedence; input
    URLs with no prior lastmod are left dateless.

    Returns (merged, added_count, updated_count). Original order is preserved:
    existing entries first (in place), then any genuinely new URLs appended."""
    norm = normalize or (lambda loc: loc)
    by_loc: dict[str, dict] = {}
    merged: list[dict] = []
    had_lastmod: set[str] = set()         # locs that already carried a lastmod
    for e in existing:
        loc = norm(e["loc"])
        if loc in by_loc:
            continue                      # collapse any pre-existing dupes
        ent = dict(e)
        ent["loc"] = loc
        by_loc[loc] = ent
        merged.append(ent)
        if (ent.get("lastmod") or "").strip():
            had_lastmod.add(loc)

    added = updated = 0
    for e in new:
        loc = norm(e["loc"])
        if loc in by_loc:                 # duplicate: update lastmod in place
            new_lm = (e.get("lastmod") or "").strip()
            if new_lm:                    # explicit input date wins
                if by_loc[loc].get("lastmod") != new_lm:
                    by_loc[loc]["lastmod"] = new_lm
                    updated += 1
            elif stamp_date and loc in had_lastmod:
                # refresh previously-dated input URLs to the generation date
                if by_loc[loc].get("lastmod") != stamp_date:
                    by_loc[loc]["lastmod"] = stamp_date
                    updated += 1
        else:                             # genuinely new URL
            ent = dict(e)
            ent["loc"] = loc
            by_loc[loc] = ent
            merged.append(ent)
            added += 1

    print(f"  Step 3: {added} new URL(s) added, {updated} existing URL(s) "
          f"had lastmod updated  (deduped by URL only, lastmod ignored for matching)")
    return merged, added, updated


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
    single_file: bool = False,
) -> dict:
    if group_by not in GROUPERS:
        raise ValueError(f"unknown --group-by '{group_by}'. "
                         f"Choices: {', '.join(GROUPERS)}")

    if not with_lastmod:
        entries = [{k: v for k, v in e.items() if k != "lastmod"} for e in entries]

    group_func = GROUPERS[group_by]
    if single_file:
        print(f"  Step 4: generating ONE flat sitemap ({prefix}.xml) with all URLs")
    else:
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
        single_file=single_file,
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
    normalize = make_normalizer(args.trailing_slash)
    stamp = (datetime.now(timezone.utc).strftime("%Y-%m-%d")
             if args.refresh_lastmod else None)
    combined, added, updated = merge_entries(
        existing, new, normalize=normalize, stamp_date=stamp)

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
        single_file=args.single_file,
    )
    valid = validate_outputs(args.out, result)

    append_log([
        f"site: {args.site or '(none)'}  base_url: {base_url}",
        f"input: {args.input}  trailing-slash: {args.trailing_slash}  "
        f"refresh-lastmod: {'on' if args.refresh_lastmod else 'off'}  "
        f"{'single-file' if args.single_file else 'group-by: ' + args.group_by}",
        f"existing: {len(existing)}  new: {len(new)}  added: {added}  "
        f"lastmod updated: {updated}  off-host dropped: {len(dropped)}  "
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


def cmd_clean(args) -> int:
    """Remove generated artifacts so the next run starts fresh.

    Deletes the output directory, the run log, and __pycache__. Only touches
    generated files — never your source, input CSVs, or credentials."""
    targets = [args.out, LOG_FILE, "__pycache__"]
    existing = [t for t in targets if os.path.exists(t)]

    if not existing:
        print("  Nothing to clean — no generated artifacts found.")
        return 0

    print("\n  🧹 About to delete:")
    for t in existing:
        kind = "dir " if os.path.isdir(t) else "file"
        print(f"     - [{kind}] {t}")

    if not args.yes:
        ans = input("\n  Type 'yes' to delete: ").strip().lower()
        if ans != "yes":
            print("  Aborted. Nothing deleted.")
            return 1

    for t in existing:
        if os.path.isdir(t):
            shutil.rmtree(t, ignore_errors=True)
        else:
            os.remove(t)
        print(f"  ✔ removed {t}")

    print("\n  Done. Re-run `update` for a fresh build.")
    return 0


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
    up.add_argument("--trailing-slash", dest="trailing_slash",
                    choices=["keep", "add", "strip"], default="keep",
                    help="Normalize trailing slashes for dedupe AND output. "
                         "'add' forces a trailing slash, 'strip' removes it, "
                         "'keep' leaves URLs untouched (default). Root, query/"
                         "fragment, and file-like URLs (e.g. .xml) are never changed.")
    up.add_argument("--single-file", dest="single_file", action="store_true",
                    help="Write ONE flat sitemap.xml with ALL URLs (no child sitemaps, no index). "
                         "Overrides --group-by.")
    up.add_argument("--refresh-lastmod", dest="refresh_lastmod", action="store_true",
                    help="For input URLs that ALREADY have a <lastmod>, refresh it to today's "
                         "generation date. (An explicit lastmod in the input still wins; input "
                         "URLs with no prior lastmod stay dateless. Needs --with-lastmod to emit.)")
    up.add_argument("--with-lastmod", action="store_true", help="Emit <lastmod> when present in the data")
    up.add_argument("--legacy-tags", action="store_true",
                    help="Also emit <changefreq>/<priority> (Google ignores these)")
    up.set_defaults(func=cmd_update)

    va = sub.add_parser("validate", help="Validate a sitemap or sitemap index (local path or URL)")
    va.add_argument("source", help="Path or URL to a sitemap / sitemap index")
    va.add_argument("--recurse", action="store_true", help="Follow and validate child sitemaps of an index")
    va.set_defaults(func=cmd_validate)

    sm = sub.add_parser("submit", help="Step 5: submit sitemaps to Google Search Console (confirmation-gated)")
    sm.add_argument("--site", required=True, help="GSC property URL (e.g. https://example.com/)")
    sm.add_argument("--out", default="./sitemaps", help="Directory holding the generated .xml sitemaps")
    sm.add_argument("--base-url", default="", help="Absolute base used to build sitemap URLs for submission")
    sm.add_argument("--credentials", default="client_secrets.json", help="Path to credentials JSON")
    sm.add_argument("--auth-mode", default="oauth", choices=["oauth", "service_account"],
                    help="Authentication mode for GSC")
    sm.add_argument("--yes", action="store_true", help="Skip the interactive confirmation prompt")
    sm.set_defaults(func=cmd_submit)

    cl = sub.add_parser("clean", help="Delete generated artifacts (output dir, log, __pycache__) for a fresh run")
    cl.add_argument("--out", default="./sitemaps", help="Output directory to remove (default: ./sitemaps)")
    cl.add_argument("--yes", action="store_true", help="Skip the interactive confirmation prompt")
    cl.set_defaults(func=cmd_clean)

    return p


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())