"""
Google Search Console — Sitemap Uploader
=========================================
Submits sitemap files to Google Search Console via the Webmasters v3 API.

Two auth modes supported:
  1. OAuth2 / client_secrets.json  (desktop / Jupyter use)
  2. Service Account JSON key      (server / CI use)

Install dependencies:
    pip install google-auth google-auth-oauthlib google-api-python-client

Quick start in Jupyter:
    from gsc_sitemap_uploader import submit_sitemaps_to_gsc

    result = submit_sitemaps_to_gsc(
        site_url    = "https://example.com/",
        sitemap_files = [
            "https://example.com/sitemap_en.xml",
            "https://example.com/sitemap_cn.xml",
            "https://example.com/sitemap_index.xml",
        ],
        credentials_path = "client_secrets.json",   # or service_account.json
        auth_mode        = "oauth",                  # "oauth" or "service_account"
    )
"""

import os
import json
import time
from typing import Literal

# ── Google auth / API client ──────────────────────────────────────────────────
try:
    from google.oauth2.credentials import Credentials
    from google.oauth2 import service_account
    from google_auth_oauthlib.flow import InstalledAppFlow
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError
except ImportError:
    raise ImportError(
        "Missing Google libraries. Install them with:\n"
        "  pip install google-auth google-auth-oauthlib google-api-python-client"
    )


# ── Scopes ────────────────────────────────────────────────────────────────────
SCOPES = ["https://www.googleapis.com/auth/webmasters"]

# Token cache path (OAuth2 only)  — stores refresh token so you only log in once
_TOKEN_CACHE = "gsc_token.json"


# ─────────────────────────────────────────────────────────────────────────────
# Auth helpers
# ─────────────────────────────────────────────────────────────────────────────

def _build_oauth_service(credentials_path: str, force_reauth: bool = False):
    """
    OAuth2 flow using client_secrets.json.

    First run: opens a browser window to authenticate.
    Subsequent runs: uses cached token in gsc_token.json automatically.

    force_reauth=True deletes the cached token and forces a fresh login.
    Use this if you previously logged in with the WRONG Google account —
    `login_hint` only hints; it will NOT override an existing valid token.
    """
    creds = None

    # A stale token from a different account is a very common cause of 403s.
    if force_reauth and os.path.exists(_TOKEN_CACHE):
        os.remove(_TOKEN_CACHE)

    # Load cached token if it exists
    if os.path.exists(_TOKEN_CACHE):
        creds = Credentials.from_authorized_user_file(_TOKEN_CACHE, SCOPES)

    # Refresh or re-authenticate as needed
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(credentials_path, SCOPES)
            # run_local_server works in Jupyter too (opens browser tab).
            # No login_hint: lets ANY user authenticate with their own Google
            # account. Pick the account that owns the GSC property when the
            # browser opens.
            creds = flow.run_local_server(port=0)

        # Cache the token for next run
        with open(_TOKEN_CACHE, "w") as fh:
            fh.write(creds.to_json())

    return build("webmasters", "v3", credentials=creds)


def _build_service_account_service(credentials_path: str, impersonate_email: str = None):
    """
    Service account with optional user impersonation.
    impersonate_email = the GSC owner's Gmail address
    """
    from google.oauth2 import service_account

    creds = service_account.Credentials.from_service_account_file(
        credentials_path, scopes=SCOPES
    )

    if impersonate_email:
        creds = creds.with_subject(impersonate_email)

    return build("webmasters", "v3", credentials=creds)


def _build_service(
    credentials_path: str,
    auth_mode: Literal["oauth", "service_account"],
    impersonate_email: str = None,
    force_reauth: bool = False,
):
    if auth_mode == "service_account":
        return _build_service_account_service(credentials_path, impersonate_email)
    return _build_oauth_service(credentials_path, force_reauth=force_reauth)


# ─────────────────────────────────────────────────────────────────────────────
# Diagnostics — run this FIRST when you hit a 403
# ─────────────────────────────────────────────────────────────────────────────

def diagnose_access(
    site_url: str,
    credentials_path: str = "client_secrets.json",
    auth_mode: Literal["oauth", "service_account"] = "oauth",
    impersonate_email: str = None,
    force_reauth: bool = False,
) -> dict:
    """
    Answers the only two questions that matter for a 403:
      1. WHO am I authenticated as?
      2. WHICH properties does that identity have access to?

    Then tells you whether `site_url` is one of them.
    """
    print("\n── GSC access diagnosis ───────────────────────────────────────")

    # 1. WHO am I? (only knowable up-front for service accounts)
    if auth_mode == "service_account":
        with open(credentials_path) as fh:
            sa_email = json.load(fh).get("client_email", "<unknown>")
        print(f"  Authenticated as SERVICE ACCOUNT: {sa_email}")
        if impersonate_email:
            print(f"  Impersonating user:               {impersonate_email}")
        else:
            print("  Impersonation:                    OFF")
            print("  → This exact email must be added in Search Console:")
            print("    Settings → Users and permissions → Add user → 'Full'")
    else:
        print("  Authenticated via OAUTH (logged-in Google user).")

    service = _build_service(credentials_path, auth_mode, impersonate_email, force_reauth)

    # 2. WHICH properties can this identity see?
    try:
        sites = service.sites().list().execute().get("siteEntry", [])
    except HttpError as err:
        print(f"  ✖ Could not even list sites: {_parse_http_error(err)}")
        return {"accessible_sites": [], "target_found": False, "error": str(err)}

    accessible = [(s.get("siteUrl"), s.get("permissionLevel")) for s in sites]

    print(f"\n  Properties this identity can access ({len(accessible)}):")
    if not accessible:
        print("    (none — the identity owns NO properties; this is your 403 cause)")
    for url, level in accessible:
        print(f"    • {url}   [{level}]")

    # 3. Is the target reachable?
    norm = site_url
    if not norm.startswith("sc-domain:") and not norm.endswith("/"):
        norm += "/"
    target_found = any(url == norm for url, _ in accessible)

    print(f"\n  Target requested: {norm}")
    if target_found:
        print("  ✔ Target IS accessible — submitting should work.")
    else:
        print("  ✖ Target NOT in the accessible list. Either:")
        print("     - the property string is wrong (www / non-www / http / "
              "sc-domain:), or")
        print("     - this identity hasn't been granted access to it.")
        # offer the closest matches as a hint
        domain = norm.replace("https://", "").replace("http://", "") \
                     .replace("sc-domain:", "").rstrip("/")
        hints = [u for u, _ in accessible if domain.split('.')[-2] in u] \
            if len(domain.split('.')) >= 2 else []
        if hints:
            print(f"     Did you mean one of: {hints}")
    print("───────────────────────────────────────────────────────────────\n")

    return {"accessible_sites": accessible, "target_found": target_found}


# ─────────────────────────────────────────────────────────────────────────────
# Core upload function
# ─────────────────────────────────────────────────────────────────────────────

def submit_sitemaps_to_gsc(
    site_url: str,
    sitemap_files: list,
    credentials_path: str = "client_secrets.json",
    auth_mode: Literal["oauth", "service_account"] = "oauth",
    impersonate_email: str = None,
    force_reauth: bool = False,
    retry_count: int = 3,
    retry_delay: float = 2.0,
) -> dict:
    """
    Submit one or more sitemap URLs to Google Search Console.

    Parameters
    ----------
    site_url : str
        Your property URL exactly as registered in GSC.
        MUST end with a slash for domain properties:
            "https://example.com/"
        Domain properties use sc-domain: prefix:
            "sc-domain:example.com"

    sitemap_files : list of str
        Full sitemap URLs to submit, e.g.:
            ["https://example.com/sitemap_en.xml",
             "https://example.com/sitemap_index.xml"]

    credentials_path : str
        Path to client_secrets.json (OAuth2) or service_account.json.

    auth_mode : "oauth" | "service_account"
        Which auth strategy to use.

    retry_count : int
        How many times to retry on transient API errors (429 / 5xx).

    retry_delay : float
        Seconds to wait between retries (doubles each attempt).

    Returns
    -------
    dict  {
        "submitted":  [ list of successfully submitted sitemap URLs ],
        "failed":     [ {"url": ..., "error": ...} ],
        "skipped":    [ list of already-submitted sitemap URLs ],
        "total":      int,
    }
    """
    service = _build_service(credentials_path, auth_mode, impersonate_email, force_reauth)
    if not site_url.startswith("sc-domain:") and not site_url.endswith("/"):
        site_url = site_url + "/"

    submitted, failed, skipped = [], [], []

    print(f"\n  Submitting {len(sitemap_files)} sitemap(s) to GSC property: {site_url}\n")

    for sitemap_url in sitemap_files:
        attempt  = 0
        success  = False
        last_err = None

        while attempt < retry_count and not success:
            attempt += 1
            try:
                service.sitemaps().submit(
                    siteUrl  = site_url,
                    feedpath = sitemap_url,
                ).execute()
                submitted.append(sitemap_url)
                print(f"  ✔ Submitted  {sitemap_url}")
                success = True

            except HttpError as err:
                status = err.resp.status
                # 400 = already exists / bad URL  →  skip, don't retry
                if status == 400:
                    detail = _parse_http_error(err)
                    skipped.append({"url": sitemap_url, "reason": detail})
                    print(f"  ⚠ Skipped    {sitemap_url}  ({detail})")
                    success = True   # treat as handled

                # 403 = no permission
                elif status == 403:
                    detail = _parse_http_error(err)
                    failed.append({"url": sitemap_url, "error": f"403 Forbidden – {detail}"})
                    print(f"  ✖ Failed     {sitemap_url}  (403 – check site ownership)")
                    success = True   # no point retrying

                # 429 / 5xx  →  retry with back-off
                elif status in (429, 500, 502, 503, 504):
                    wait = retry_delay * (2 ** (attempt - 1))
                    print(f"  ↺ Retry {attempt}/{retry_count}  {sitemap_url}  "
                          f"(HTTP {status}, waiting {wait}s)")
                    time.sleep(wait)
                    last_err = str(err)

                else:
                    last_err = str(err)
                    print(f"  ✖ Failed     {sitemap_url}  (HTTP {status})")
                    success = True   # unknown error, don't retry

            except Exception as err:
                last_err = str(err)
                wait = retry_delay * (2 ** (attempt - 1))
                print(f"  ↺ Retry {attempt}/{retry_count}  {sitemap_url}  ({err})")
                time.sleep(wait)

        if not success:
            failed.append({"url": sitemap_url, "error": last_err or "Unknown error"})
            print(f"  ✖ Failed     {sitemap_url}  (all retries exhausted)")

    result = {
        "submitted": submitted,
        "failed":    failed,
        "skipped":   [s["url"] for s in skipped],
        "total":     len(sitemap_files),
    }

    print(f"\n  Done — ✔ {len(submitted)} submitted  "
          f"⚠ {len(skipped)} skipped  "
          f"✖ {len(failed)} failed\n")
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Helper: list all sitemaps already submitted for a property
# ─────────────────────────────────────────────────────────────────────────────

def list_submitted_sitemaps(
    site_url: str,
    credentials_path: str = "client_secrets.json",
    auth_mode: Literal["oauth", "service_account"] = "oauth",
) -> list:
    """
    Return all sitemaps currently registered in GSC for `site_url`.

    Useful to check before submitting or to clean up stale sitemaps.
    """
    service = _build_service(credentials_path, auth_mode)
    if not site_url.startswith("sc-domain:") and not site_url.endswith("/"):
        site_url += "/"

    response  = service.sitemaps().list(siteUrl=site_url).execute()
    sitemaps  = response.get("sitemap", [])
    return sitemaps


# ─────────────────────────────────────────────────────────────────────────────
# Helper: delete / remove a sitemap from GSC
# ─────────────────────────────────────────────────────────────────────────────

def delete_sitemap_from_gsc(
    site_url: str,
    sitemap_url: str,
    credentials_path: str = "client_secrets.json",
    auth_mode: Literal["oauth", "service_account"] = "oauth",
) -> bool:
    """
    Remove a single sitemap from GSC.
    Returns True on success, False on failure.
    """
    service = _build_service(credentials_path, auth_mode)
    if not site_url.startswith("sc-domain:") and not site_url.endswith("/"):
        site_url += "/"
    try:
        service.sitemaps().delete(siteUrl=site_url, feedpath=sitemap_url).execute()
        print(f"  ✔ Deleted {sitemap_url}")
        return True
    except HttpError as err:
        print(f"  ✖ Could not delete {sitemap_url}: {err}")
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Convenience: build sitemap URL list from generate_sitemaps() result
# ─────────────────────────────────────────────────────────────────────────────

def sitemap_urls_from_result(generate_result: dict, base_url: str) -> list:
    """
    Convert the dict returned by generate_sitemaps() into a list of full
    sitemap URLs ready to pass to submit_sitemaps_to_gsc().

    Usage
    -----
        from sitemap_generator import generate_sitemaps
        from gsc_sitemap_uploader import sitemap_urls_from_result, submit_sitemaps_to_gsc

        gen_result = generate_sitemaps(urls=my_urls, depth=1, ...)
        urls_to_submit = sitemap_urls_from_result(gen_result, "https://example.com")
        submit_sitemaps_to_gsc(site_url="https://example.com/", sitemap_files=urls_to_submit, ...)
    """
    base = base_url.rstrip("/")
    # Submit index file(s) + individual sitemap files
    all_files = generate_result.get("index_files", []) + generate_result.get("sitemap_files", [])
    return [f"{base}/{f}" for f in all_files]


# ─────────────────────────────────────────────────────────────────────────────
# Utility
# ─────────────────────────────────────────────────────────────────────────────

def _parse_http_error(err: "HttpError") -> str:
    try:
        body = json.loads(err.content.decode())
        return body.get("error", {}).get("message", str(err))
    except Exception:
        return str(err)


# ─────────────────────────────────────────────────────────────────────────────
# Demo (run as script)
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # ── Replace these values ──────────────────────────────────────────────
    SITE_URL         = "https://example.com/"
    CREDENTIALS_PATH = "client_secrets.json"   # OAuth2 credentials file
    AUTH_MODE        = "oauth"                 # "oauth" or "service_account"

    SITEMAPS = [
        "https://example.com/sitemap_en.xml",
        "https://example.com/sitemap_cn.xml",
        "https://example.com/sitemap_de.xml",
        "https://example.com/sitemap_index.xml",
    ]
    # ─────────────────────────────────────────────────────────────────────

    # 0. Diagnose access FIRST — this tells you exactly why a 403 happens
    diagnose_access(SITE_URL, CREDENTIALS_PATH, AUTH_MODE)

    # 1. List existing sitemaps first
    print("Existing sitemaps in GSC:")
    existing = list_submitted_sitemaps(SITE_URL, CREDENTIALS_PATH, AUTH_MODE)
    for sm in existing:
        print(f"  {sm.get('path')}  (errors: {sm.get('errors', 0)})")

    # 2. Submit new/updated sitemaps
    result = submit_sitemaps_to_gsc(
        site_url         = SITE_URL,
        sitemap_files    = SITEMAPS,
        credentials_path = CREDENTIALS_PATH,
        auth_mode        = AUTH_MODE,
    )
    print("Result:", result)
