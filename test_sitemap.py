"""
Offline test harness for the sitemap automation.
================================================
No network, no GSC, no pandas required. Proves the generate -> validate ->
dedup -> grouping logic works before you point the workflow at a live domain.

Run:
    python test_sitemap.py
Exit 0 = all passed, 1 = a check failed.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import xml.etree.ElementTree as ET

from sitemap_generator import generate_sitemaps
from validate_sitemap import validate, validate_path
from sitemap_workflow import merge_entries, make_normalizer, load_new_urls, GROUPERS

PASS, FAIL = "  ✔", "  ✖"
_failures = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global _failures
    if condition:
        print(f"{PASS} {label}")
    else:
        _failures += 1
        print(f"{FAIL} {label}  {detail}")


def _locs_in(filepath: str) -> list[str]:
    root = ET.parse(filepath).getroot()
    ln = lambda t: t.rsplit("}", 1)[-1]
    return [el.text.strip() for el in root.iter() if ln(el.tag) == "loc"]


SAMPLE = [
    {"loc": "https://example.com/en/a", "lastmod": "2024-01-01"},
    {"loc": "https://example.com/en/b", "lastmod": "2024-02-01"},
    {"loc": "https://example.com/en/c", "lastmod": "2024-02-01"},
    {"loc": "https://example.com/de/a", "lastmod": "2024-03-01"},
    {"loc": "https://example.com/de/b", "lastmod": "2024-03-01"},
    {"loc": "https://example.com/about"},          # lone -> merged
]


def test_path_depth(tmp):
    out = os.path.join(tmp, "depth")
    res = generate_sitemaps(SAMPLE, depth=1, output_dir=out,
                            base_url="https://example.com", min_urls_per_group=2)
    check("path_depth: en + de groups present",
          "en" in res["groups"] and "de" in res["groups"], str(res["groups"]))
    check("path_depth: lone /about merged (not its own group)",
          "about" not in res["groups"], str(res["groups"]))
    check("path_depth: total URLs preserved", res["total_urls"] == len(SAMPLE))
    check("path_depth: an index file written", len(res["index_files"]) >= 1)


def test_single(tmp):
    out = os.path.join(tmp, "single")
    res = generate_sitemaps(SAMPLE, depth=1, output_dir=out,
                            base_url="https://example.com",
                            group_func=GROUPERS["single"], min_urls_per_group=1)
    check("single: exactly one urlset group", len(res["groups"]) == 1, str(res["groups"]))
    fname = res["sitemap_files"][0]
    check("single: all URLs in one file",
          len(_locs_in(os.path.join(out, fname))) == len(SAMPLE))


def test_first_segment(tmp):
    out = os.path.join(tmp, "seg")
    res = generate_sitemaps(SAMPLE, depth=99, output_dir=out,
                            base_url="https://example.com",
                            group_func=GROUPERS["first_segment"], min_urls_per_group=1)
    check("first_segment: keys are top-level segments",
          set(res["groups"]) >= {"en", "de"}, str(res["groups"]))


def test_lastmod_month(tmp):
    out = os.path.join(tmp, "month")
    res = generate_sitemaps(SAMPLE, depth=1, output_dir=out,
                            base_url="https://example.com",
                            group_func=GROUPERS["lastmod_month"], min_urls_per_group=1)
    check("lastmod_month: groups by YYYY-MM",
          {"2024-01", "2024-02", "2024-03"} <= set(res["groups"]), str(res["groups"]))
    check("lastmod_month: undated bucket for missing lastmod",
          "undated" in res["groups"], str(res["groups"]))


def test_tags_off_by_default(tmp):
    out = os.path.join(tmp, "tags")
    res = generate_sitemaps(SAMPLE, depth=1, output_dir=out, base_url="https://example.com")
    fname = res["sitemap_files"][0]
    text = open(os.path.join(out, fname), encoding="utf-8").read()
    check("priority/changefreq omitted by default",
          "<priority>" not in text and "<changefreq>" not in text)

    out2 = os.path.join(tmp, "tags_on")
    res2 = generate_sitemaps(SAMPLE, depth=1, output_dir=out2, base_url="https://example.com",
                             emit_changefreq=True, emit_priority=True)
    text2 = open(os.path.join(out2, res2["sitemap_files"][0]), encoding="utf-8").read()
    check("priority/changefreq emitted when flags on",
          "<priority>" in text2 and "<changefreq>" in text2)


def test_split(tmp):
    out = os.path.join(tmp, "split")
    many = [{"loc": f"https://example.com/en/p{i}"} for i in range(12)]
    res = generate_sitemaps(many, depth=1, output_dir=out, base_url="https://example.com",
                            max_urls_per_sitemap=5)
    check("split: 12 URLs / max 5 -> 3 files", len(res["sitemap_files"]) == 3,
          str(res["sitemap_files"]))


def test_generated_is_valid(tmp):
    out = os.path.join(tmp, "valid")
    res = generate_sitemaps(SAMPLE, depth=1, output_dir=out, base_url="https://example.com")
    all_ok = True
    for f in res["index_files"] + res["sitemap_files"]:
        results = validate(os.path.join(out, f))
        all_ok = all_ok and all(r.ok for r in results)
    check("generated output passes the validator", all_ok)


def test_validator_catches_bad(tmp):
    # off-host + duplicate + bad lastmod, hand-written
    bad = os.path.join(tmp, "bad.xml")
    with open(bad, "w", encoding="utf-8") as fh:
        fh.write(
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
            '  <url><loc>https://a.com/x</loc></url>\n'
            '  <url><loc>https://b.com/y</loc></url>\n'        # off-host
            '  <url><loc>https://a.com/x</loc></url>\n'         # duplicate
            '  <url><loc>https://a.com/z</loc><lastmod>15-01-2024</lastmod></url>\n'  # bad date
            '</urlset>\n'
        )
    results = validate(bad)
    errs = " | ".join(results[0].errors)
    check("validator flags multi-host", "multiple hosts" in errs, errs)
    check("validator flags duplicate loc", "duplicate" in errs, errs)
    check("validator flags bad lastmod", "lastmod" in errs, errs)


def test_dedup_and_loader(tmp):
    csv_path = os.path.join(tmp, "new.csv")
    with open(csv_path, "w", encoding="utf-8") as fh:
        fh.write("url,lastmod\n"
                 "https://example.com/new1,2024-05-01\n"
                 "https://example.com/existing,2024-05-02\n"   # already in sitemap
                 "https://example.com/new1,2024-05-01\n")      # internal dup
    new = load_new_urls(csv_path)
    check("loader: reads 3 rows", len(new) == 3, str(len(new)))

    # existing "/existing" has an OLD lastmod that should be updated in place
    existing = [{"loc": "https://example.com/existing", "lastmod": "2020-01-01"}]
    merged, added, updated = merge_entries(existing, new)

    check("merge: adds only the genuinely new URL", added == 1, f"added={added}")
    check("merge: updates lastmod of the existing URL", updated == 1, f"updated={updated}")
    check("merge: no duplicate entries (dedup by loc only)", len(merged) == 2,
          f"len={len(merged)}")

    by_loc = {m["loc"]: m for m in merged}
    check("merge: existing URL kept once with NEW lastmod",
          by_loc["https://example.com/existing"].get("lastmod") == "2024-05-02",
          by_loc["https://example.com/existing"].get("lastmod"))
    check("merge: new URL present",
          "https://example.com/new1" in by_loc)


def test_trailing_slash_normalize(tmp):
    norm = make_normalizer("add")
    check("norm/add: appends slash", norm("https://x.com/blog") == "https://x.com/blog/")
    check("norm/add: idempotent", norm("https://x.com/blog/") == "https://x.com/blog/")
    check("norm/add: root untouched", norm("https://x.com/") == "https://x.com/")
    check("norm/add: file-like untouched",
          norm("https://x.com/sitemap.xml") == "https://x.com/sitemap.xml")
    check("norm/add: query untouched",
          norm("https://x.com/p?a=1") == "https://x.com/p?a=1")

    strip = make_normalizer("strip")
    check("norm/strip: removes slash", strip("https://x.com/blog/") == "https://x.com/blog")

    # slash-insensitive dedupe: existing has slash, CSV has none -> one entry,
    # lastmod updated, NOT added as a new URL.
    existing = [{"loc": "https://x.com/blog/post/", "lastmod": "2020-01-01"}]
    new = [{"loc": "https://x.com/blog/post", "lastmod": "2026-06-06"}]  # no slash
    merged, added, updated = merge_entries(existing, new, normalize=norm)
    check("norm: slash variant deduped (not added)", added == 0, f"added={added}")
    check("norm: lastmod updated on dedupe", updated == 1, f"updated={updated}")
    check("norm: single canonical entry kept", len(merged) == 1, f"len={len(merged)}")
    check("norm: emitted URL uses trailing-slash form",
          merged[0]["loc"] == "https://x.com/blog/post/", merged[0]["loc"])


def test_refresh_lastmod(tmp):
    today = "2026-06-07"
    existing = [
        {"loc": "https://x.com/a/", "lastmod": "2020-01-01"},  # had a date
        {"loc": "https://x.com/b/"},                           # no date
    ]
    new = [
        {"loc": "https://x.com/a/"},   # in input, had a date -> bump to today
        {"loc": "https://x.com/b/"},   # in input, no prior date -> stays dateless
        {"loc": "https://x.com/c/"},   # brand new -> appended, dateless
    ]
    merged, added, updated = merge_entries(existing, new, stamp_date=today)
    by_loc = {m["loc"]: m for m in merged}
    check("refresh: dated input URL bumped to today",
          by_loc["https://x.com/a/"].get("lastmod") == today,
          by_loc["https://x.com/a/"].get("lastmod"))
    check("refresh: dateless input URL stays dateless",
          "lastmod" not in by_loc["https://x.com/b/"] or
          not by_loc["https://x.com/b/"].get("lastmod"))
    check("refresh: brand-new URL added without date", added == 1, f"added={added}")
    check("refresh: only the dated URL counts as updated", updated == 1, f"updated={updated}")

    # explicit input lastmod still wins over the stamp
    merged2, _, _ = merge_entries(
        [{"loc": "https://x.com/a/", "lastmod": "2020-01-01"}],
        [{"loc": "https://x.com/a/", "lastmod": "2025-12-31"}],
        stamp_date=today)
    check("refresh: explicit input date beats the stamp",
          merged2[0]["lastmod"] == "2025-12-31", merged2[0]["lastmod"])


def test_home_goes_to_pages(tmp):
    out = os.path.join(tmp, "home")
    urls = [
        {"loc": "https://example.com/"},                 # home → pages
        {"loc": "https://example.com/blog/post-1"},
        {"loc": "https://example.com/blog/post-2"},
    ]
    result = generate_sitemaps(urls=urls, depth=1, output_dir=out,
                               base_url="https://example.com", min_urls_per_group=2)
    check("home: a sitemap_pages.xml is produced",
          "sitemap_pages.xml" in result["sitemap_files"],
          str(result["sitemap_files"]))
    pages_xml = open(os.path.join(out, "sitemap_pages.xml"), encoding="utf-8").read()
    check("home: root URL lives in pages sitemap",
          "<loc>https://example.com/</loc>" in pages_xml, pages_xml)
    # home must NOT be merged into 'other' or land in the blog sitemap
    blog_xml = open(os.path.join(out, "sitemap_blog.xml"), encoding="utf-8").read()
    check("home: root URL NOT in blog sitemap",
          "<loc>https://example.com/</loc>" not in blog_xml)


def test_single_file(tmp):
    out = os.path.join(tmp, "single")
    urls = [
        {"loc": "https://example.com/"},
        {"loc": "https://example.com/blog/post-1"},
        {"loc": "https://example.com/seo/x"},
    ]
    result = generate_sitemaps(urls=urls, depth=1, output_dir=out,
                               base_url="https://example.com", single_file=True)
    check("single: exactly one flat sitemap.xml",
          result["sitemap_files"] == ["sitemap.xml"], str(result["sitemap_files"]))
    check("single: no index file produced", result["index_files"] == [],
          str(result["index_files"]))
    flat = open(os.path.join(out, "sitemap.xml"), encoding="utf-8").read()
    check("single: all 3 URLs in one file", flat.count("<loc>") == 3,
          str(flat.count("<loc>")))


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="sitemap_test_")
    print(f"\nRunning offline sitemap tests in {tmp}\n")
    try:
        test_path_depth(tmp)
        test_single(tmp)
        test_first_segment(tmp)
        test_lastmod_month(tmp)
        test_tags_off_by_default(tmp)
        test_split(tmp)
        test_generated_is_valid(tmp)
        test_validator_catches_bad(tmp)
        test_dedup_and_loader(tmp)
        test_trailing_slash_normalize(tmp)
        test_refresh_lastmod(tmp)
        test_home_goes_to_pages(tmp)
        test_single_file(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{'🎉 ALL PASSED' if _failures == 0 else f'✖ {_failures} CHECK(S) FAILED'}")
    return 0 if _failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
