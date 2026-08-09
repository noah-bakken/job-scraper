#!/usr/bin/env python3
"""
Job scraper for post-undergrad entry-level product roles.

Polls company job sources (Greenhouse, Lever, Ashby, Workday, plus Amazon,
Google, and Microsoft search endpoints), filters for entry-level product roles,
drops anything that requires you to still be a student, appends new hits to a
Google Sheet, and emails you a digest of what's new.

Filter policy (per your spec):
  KEEP  PM / APM / product analyst / TPM / product ops, entry level
  KEEP  internships & co-ops that are open to graduates
  DROP  senior+ roles
  DROP  roles that require current enrollment or returning to school
"""

import os
import re
import ssl
import html
import json
import smtplib
import datetime
from email.message import EmailMessage

import requests
import gspread
from google.oauth2.service_account import Credentials


# ===========================================================================
# CONFIG
# ===========================================================================

# Companies to watch. Verify ats + slug with find_source.py before trusting.
#   greenhouse / lever / ashby -> needs "slug"
#   workday                    -> needs "tenant", "wd_host", "site"
#   amazon / google / microsoft -> no slug (single known endpoint)
COMPANIES = [
    # --- Reliable ATS core (verify slugs with find_source.py) ---
    {"name": "Figma",      "ats": "greenhouse", "slug": "figma"},
    {"name": "Databricks", "ats": "greenhouse", "slug": "databricks"},
    {"name": "Coinbase",   "ats": "greenhouse", "slug": "coinbase"},
    {"name": "Robinhood",  "ats": "greenhouse", "slug": "robinhood"},
    {"name": "Reddit",     "ats": "greenhouse", "slug": "reddit"},
    {"name": "Duolingo",   "ats": "greenhouse", "slug": "duolingo"},
    {"name": "Brex",       "ats": "greenhouse", "slug": "brex"},
    {"name": "Ramp",       "ats": "ashby",      "slug": "ramp"},
    {"name": "Notion",     "ats": "ashby",      "slug": "notion"},

    # --- Broad "search anything" feed: a maintained new-grad list spanning
    #     hundreds of companies (startups + big cos), with apply links. This is
    #     what casts the wide net; the per-company sources above add depth. ---
    {"name": "New-Grad Feed", "ats": "newgrad_feed"},

    # --- Marquee search endpoints (best-effort; verify on first run) ---
    {"name": "Amazon",    "ats": "amazon"},
    {"name": "Google",    "ats": "google"},
    {"name": "Microsoft", "ats": "microsoft"},

    # --- Workday companies: fill tenant/wd_host/site from the careers URL. ---
    # A Workday careers URL looks like:
    #   https://TENANT.wdN.myworkdayjobs.com/SITE
    # e.g. https://salesforce.wd12.myworkdayjobs.com/External_Career_Site
    #   -> tenant="salesforce", wd_host="wd12", site="External_Career_Site"
    # Uncomment and fill each one you want:
    # {"name": "Salesforce", "ats": "workday", "tenant": "salesforce", "wd_host": "wd12", "site": "External_Career_Site"},
    # {"name": "Adobe",      "ats": "workday", "tenant": "adobe",      "wd_host": "wd5",  "site": "external_experienced"},
]

# A title must contain one of these (case-insensitive) to be a match.
TITLE_INCLUDE = [
    "product manager",
    "associate product manager",
    "apm",
    "product management",
    "rotational product",
    "product analyst",
    "technical program manager",
    "tpm",
    "product operations",
    "product ops",
    "junior product manager",
    "product owner",
]
# Note on "Associate Product ___": we don't list "associate product" on its own,
# because it also catches "Associate Product Engineer/Designer". "Associate
# Product Manager/Analyst/Owner" already match via the phrases above.

# A title is rejected if it contains any of these. Note: NO intern/co-op here,
# because you want grad-eligible internships kept.
TITLE_EXCLUDE_WORDS = [
    "senior", "sr.", "sr ", "staff", "principal", "lead", "director",
    "head of", "group product", "vp", "vice president", "manager iii",
]
# Level tokens rejected only as whole words (so "ii" won't hit "hawaii").
TITLE_EXCLUDE_TOKENS = {"ii", "iii", "iv"}

# If the description contains any of these, the role requires you to still be a
# student, so it's dropped. This is the "not returning to school" filter.
DESCRIPTION_EXCLUDE = [
    "currently enrolled",
    "must be enrolled",
    "actively enrolled",
    "will be enrolled",
    "enrolled throughout",
    "enrolled for the duration",
    "enrolled in a degree",
    "returning to school",
    "returning to campus",
    "return to campus",
    "returning to your degree",
    "returning to a degree",
    "rising senior",
    "rising junior",
    "expected graduation",
    "expected to graduate",
    "anticipated graduation",
    "graduating in 2027",
    "graduating in 2028",
    "must be pursuing",
    "currently pursuing a degree",
    "currently pursuing a bachelor",
    "currently pursuing a master",
]

# Drop roles that clearly want a lot of experience. Lenient by default: only 5+
# years and up, so ambiguous 1-3 year roles still surface. Empty this list to
# turn the experience filter off entirely.
EXPERIENCE_EXCLUDE = [
    "5+ years", "6+ years", "7+ years", "8+ years", "9+ years", "10+ years",
    "5-7 years", "5 to 7 years", "5-8 years", "at least 5 years",
    "minimum of 5 years", "minimum 5 years",
]

# Search terms used by the search-endpoint adapters (amazon/google/microsoft/workday).
SEARCH_QUERIES = [
    "product manager",
    "product analyst",
    "technical program manager",
    "product operations",
]

# Location PRIORITY (not a filter). Roles anywhere are kept; those matching one
# of these get flagged "Yes" in the Priority column and floated to the top of
# each run and each email. Covers all of California (the "ca" state code), your
# named cities, the East Coast hubs, and remote.
# Short codes (sf, nyc, ny, ca, dc) match as whole words so "ca" hits ", CA"
# but not "Canada", and we spell out "los angeles" to avoid the "LA" state code.
LOCATION_PRIORITY = [
    "remote", "california", "ca", "san francisco", "sf", "bay area", "san jose",
    "palo alto", "mountain view", "sunnyvale", "oakland", "san diego",
    "sacramento", "irvine", "los angeles", "new york", "nyc", "ny", "brooklyn",
    "chicago", "austin", "boston", "washington", "dc", "philadelphia",
]
_SHORT_LOC = {"sf", "nyc", "ny", "ca", "dc"}

# Google Sheet. SHEET_ID (from the sheet URL) is preferred: opening by key needs
# only the spreadsheets scope and no Drive API call. Falling back to the name
# costs a Drive lookup per run and needs the Drive API enabled.
SHEET_ID = os.environ.get("SHEET_ID", "")
SHEET_NAME = "Job Tracker"
WORKSHEET_NAME = "Jobs"
HEADER = ["Date added", "Priority", "Company", "Title", "Location", "URL", "Applied?"]
URL_COL = 6  # column F holds the URL (used for dedup)

# Email (all read from env / GitHub secrets; email is skipped if unset)
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")
EMAIL_TO = os.environ.get("EMAIL_TO", "")

REQ_HEADERS = {"User-Agent": "Mozilla/5.0 (job-tracker)"}


# ===========================================================================
# Helpers
# ===========================================================================
def _strip_html(raw):
    if not raw:
        return ""
    text = html.unescape(raw)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _join(*parts):
    return " ".join(p for p in parts if p)


# ===========================================================================
# Adapters -> each returns list of {title, location, url, description}
# ===========================================================================
def fetch_greenhouse(c):
    url = f"https://boards-api.greenhouse.io/v1/boards/{c['slug']}/jobs?content=true"
    r = requests.get(url, timeout=25, headers=REQ_HEADERS)
    r.raise_for_status()
    return [{
        "title": j.get("title", ""),
        "location": (j.get("location") or {}).get("name", ""),
        "url": j.get("absolute_url", ""),
        "description": _strip_html(j.get("content", "")),
    } for j in r.json().get("jobs", [])]


def fetch_lever(c):
    url = f"https://api.lever.co/v0/postings/{c['slug']}?mode=json"
    r = requests.get(url, timeout=25, headers=REQ_HEADERS)
    r.raise_for_status()
    out = []
    for j in r.json():
        desc = j.get("descriptionPlain") or _strip_html(j.get("description", ""))
        for block in j.get("lists", []) or []:
            desc = _join(desc, _strip_html(block.get("content", "")))
        out.append({
            "title": j.get("text", ""),
            "location": (j.get("categories") or {}).get("location", ""),
            "url": j.get("hostedUrl", ""),
            "description": desc,
        })
    return out


def fetch_ashby(c):
    url = f"https://api.ashbyhq.com/posting-api/job-board/{c['slug']}?includeCompensation=true"
    r = requests.get(url, timeout=25, headers=REQ_HEADERS)
    r.raise_for_status()
    return [{
        "title": j.get("title", ""),
        "location": j.get("location", ""),
        "url": j.get("jobUrl", ""),
        "description": _strip_html(j.get("descriptionPlain") or j.get("description", "")),
    } for j in r.json().get("jobs", [])]


def fetch_workday(c):
    tenant, host, site = c["tenant"], c.get("wd_host", "wd1"), c["site"]
    base = f"https://{tenant}.{host}.myworkdayjobs.com"
    api = f"{base}/wday/cxs/{tenant}/{site}/jobs"
    seen, out = set(), []
    for q in SEARCH_QUERIES:
        offset = 0
        while offset <= 40:
            body = {"appliedFacets": {}, "limit": 20, "offset": offset, "searchText": q}
            r = requests.post(api, json=body, timeout=25, headers=REQ_HEADERS)
            r.raise_for_status()
            postings = r.json().get("jobPostings", [])
            if not postings:
                break
            for j in postings:
                path = j.get("externalPath", "")
                if path in seen:
                    continue
                seen.add(path)
                out.append({
                    "title": j.get("title", ""),
                    "location": j.get("locationsText", ""),
                    "url": f"{base}/en-US/{site}{path}" if path else base,
                    # Workday listing carries no description; title filter only.
                    "description": "",
                })
            offset += 20
    return out


def fetch_amazon(c):
    out = []
    for q in SEARCH_QUERIES:
        r = requests.get("https://www.amazon.jobs/en/search.json",
                         params={"base_query": q, "result_limit": 100, "sort": "recent"},
                         timeout=25, headers=REQ_HEADERS)
        r.raise_for_status()
        for j in r.json().get("jobs", []):
            out.append({
                "title": j.get("title", ""),
                "location": j.get("normalized_location") or j.get("location", ""),
                "url": "https://www.amazon.jobs" + (j.get("job_path") or ""),
                "description": _join(_strip_html(j.get("description", "")),
                                     _strip_html(j.get("basic_qualifications", "")),
                                     _strip_html(j.get("preferred_qualifications", ""))),
            })
    return out


def fetch_google(c):
    out = []
    for q in SEARCH_QUERIES:
        r = requests.get("https://careers.google.com/api/v3/search/",
                         params={"q": q, "page_size": 100}, timeout=25, headers=REQ_HEADERS)
        r.raise_for_status()
        for j in r.json().get("jobs", []):
            locs = j.get("locations") or []
            loc = ", ".join(l.get("display", "") for l in locs if l.get("display"))
            quals = j.get("qualifications", "")
            if isinstance(quals, list):
                quals = " ".join(quals)
            out.append({
                "title": j.get("title", ""),
                "location": loc,
                "url": j.get("apply_url") or "",
                "description": _join(_strip_html(j.get("description", "")), _strip_html(quals)),
            })
    return out


def fetch_microsoft(c):
    out = []
    for q in SEARCH_QUERIES:
        r = requests.get("https://gcsservices.careers.microsoft.com/search/api/v1/search",
                         params={"q": q, "l": "en_us", "pg": 1, "pgSz": 50, "o": "Recent"},
                         timeout=25, headers=REQ_HEADERS)
        r.raise_for_status()
        result = ((r.json().get("operationResult") or {}).get("result") or {})
        for j in result.get("jobs", []):
            jid = j.get("jobId") or j.get("id") or ""
            props = j.get("properties") or {}
            out.append({
                "title": j.get("title", ""),
                "location": j.get("primaryLocation") or props.get("primaryLocation", ""),
                "url": f"https://jobs.careers.microsoft.com/global/en/job/{jid}",
                "description": _strip_html(props.get("description", "")),
            })
    return out


NEWGRAD_FEED_URL = (
    "https://raw.githubusercontent.com/SimplifyJobs/"
    "New-Grad-Positions/dev/.github/scripts/listings.json"
)


def fetch_newgrad_feed(c):
    """A maintained new-grad job list across hundreds of companies. Carries no
    description, so title/location filters do the work here (fine, since the
    whole feed is new-grad full-time by construction)."""
    r = requests.get(NEWGRAD_FEED_URL, timeout=60, headers=REQ_HEADERS)
    r.raise_for_status()
    out = []
    for j in r.json():
        if not (j.get("active") and j.get("is_visible")):
            continue
        out.append({
            "title": j.get("title", ""),
            "location": ", ".join(j.get("locations") or []),
            "url": j.get("url", ""),
            "description": "",
        })
    return out


FETCHERS = {
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "ashby": fetch_ashby,
    "workday": fetch_workday,
    "amazon": fetch_amazon,
    "google": fetch_google,
    "microsoft": fetch_microsoft,
    "newgrad_feed": fetch_newgrad_feed,
}


# ===========================================================================
# Filtering
# ===========================================================================
def _title_excluded(title):
    t = title.lower()
    for w in TITLE_EXCLUDE_WORDS:
        if w in t:
            return True
    for tok in TITLE_EXCLUDE_TOKENS:
        if re.search(rf"\b{re.escape(tok)}\b", t):
            return True
    return False


def is_priority_location(loc):
    """True if the role is in one of your preferred spots (all of California,
    your named cities, East Coast hubs, or remote). Used for ranking, not
    filtering."""
    loc = (loc or "").lower()
    if not loc:
        return False
    for term in LOCATION_PRIORITY:
        if term in _SHORT_LOC:
            if re.search(rf"\b{re.escape(term)}\b", loc):
                return True
        elif term in loc:
            return True
    return False


def matches(job):
    # Location is NOT a filter anymore, so roles anywhere are kept. Location
    # only affects priority ranking (see is_priority_location).
    title = (job.get("title") or "").lower()
    desc = (job.get("description") or "").lower()

    if not any(k in title for k in TITLE_INCLUDE):
        return False
    if _title_excluded(title):
        return False
    if any(p in desc for p in DESCRIPTION_EXCLUDE):
        return False
    if any(p in desc for p in EXPERIENCE_EXCLUDE):
        return False
    return True


# ===========================================================================
# Google Sheets
# ===========================================================================
def get_credentials():
    # drive.readonly is only exercised by the open-by-name fallback below; the
    # SHEET_ID path never touches Drive.
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive.readonly",
    ]
    raw = os.environ.get("GOOGLE_CREDENTIALS")
    if raw:
        return Credentials.from_service_account_info(json.loads(raw), scopes=scopes)
    path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if path:
        return Credentials.from_service_account_file(path, scopes=scopes)
    raise RuntimeError("Set GOOGLE_CREDENTIALS (JSON string) or GOOGLE_APPLICATION_CREDENTIALS (file path).")


def get_worksheet():
    client = gspread.authorize(get_credentials())
    if SHEET_ID:
        sh = client.open_by_key(SHEET_ID)
    else:
        # Name lookup goes through the Drive API, which must be enabled on the
        # Cloud project. Set SHEET_ID to skip it.
        sh = client.open(SHEET_NAME)
    try:
        ws = sh.worksheet(WORKSHEET_NAME)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=WORKSHEET_NAME, rows=5000, cols=len(HEADER))
    # gspread returns [[]] for an empty worksheet, which is truthy, so test for
    # real cell content instead of list emptiness.
    if not any(cell for row in ws.get_all_values() for cell in row):
        ws.append_row(HEADER, value_input_option="USER_ENTERED")
    return ws


# ===========================================================================
# Email digest
# ===========================================================================
def send_email(rows):
    if not (SMTP_USER and SMTP_PASS and EMAIL_TO):
        print("[email] SMTP not configured, skipping email.")
        return
    # Priority roles first in the digest.
    ordered = sorted(rows, key=lambda r: r[1] != "Yes")
    n_prio = sum(1 for r in rows if r[1] == "Yes")
    lines = [f"{len(rows)} new role(s) ({n_prio} in your preferred locations):\n"]
    html_items = []
    for _date, prio, company, title, location, url, _a in ordered:
        star = "\u2b50 " if prio == "Yes" else ""
        lines.append(f"- {star}{company}: {title} ({location})\n  {url}")
        badge = ("<span style='color:#c47f00'>\u2b50 priority</span> " if prio == "Yes" else "")
        html_items.append(
            f"<li>{badge}<b>{html.escape(company)}</b>: "
            f"<a href='{html.escape(url)}'>{html.escape(title)}</a> "
            f"<span style='color:#666'>{html.escape(location)}</span></li>"
        )
    msg = EmailMessage()
    msg["Subject"] = f"{len(rows)} new product role(s), {n_prio} priority"
    msg["From"] = SMTP_USER
    msg["To"] = EMAIL_TO
    msg.set_content("\n".join(lines))
    msg.add_alternative(
        f"<h3>{len(rows)} new product role(s)</h3>"
        f"<p>{n_prio} in your preferred locations (shown first).</p>"
        f"<ul>{''.join(html_items)}</ul>",
        subtype="html",
    )
    ctx = ssl.create_default_context()
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
        s.starttls(context=ctx)
        s.login(SMTP_USER, SMTP_PASS)
        s.send_message(msg)
    print(f"[email] Sent digest to {EMAIL_TO}.")


# ===========================================================================
# Main
# ===========================================================================
def main():
    ws = get_worksheet()
    existing_urls = set(ws.col_values(URL_COL))  # column F = URL
    today = datetime.date.today().isoformat()
    new_rows = []

    for c in COMPANIES:
        fetcher = FETCHERS.get(c["ats"])
        if not fetcher:
            print(f"[warn] unknown ats '{c['ats']}' for {c['name']}, skipping")
            continue
        try:
            jobs = fetcher(c)
        except Exception as e:
            print(f"[warn] {c['name']} ({c['ats']}) failed: {e}")
            continue

        kept = 0
        for j in jobs:
            url = j.get("url") or ""
            if not url or url in existing_urls:
                continue
            if not matches(j):
                continue
            prio = "Yes" if is_priority_location(j.get("location")) else ""
            new_rows.append([today, prio, c["name"], j["title"], j["location"], url, ""])
            existing_urls.add(url)
            kept += 1
        print(f"{c['name']}: {kept} new match(es)")

    if new_rows:
        # Append priority roles first so they sit higher in the sheet.
        new_rows.sort(key=lambda r: r[1] != "Yes")
        ws.append_rows(new_rows, value_input_option="USER_ENTERED")
        try:
            send_email(new_rows)
        except Exception as e:
            print(f"[warn] email failed: {e}")
    n_prio = sum(1 for r in new_rows if r[1] == "Yes")
    print(f"Done. Added {len(new_rows)} new job(s) ({n_prio} priority).")


if __name__ == "__main__":
    main()
