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

# Drop roles asking for more full-time experience than you have. Listing
# phrasings ("3+ years", "3-5 years", "at least three years", "minimum of 3
# yrs") never ends, so min_years_required() parses the smallest number of years
# a posting asks for and we compare against this ceiling.
#
# You graduated May 2026 with four internships and no full-time experience, so
# 3+ years is out of reach while 1-2 years is a reasonable stretch. Lower to 1
# to be stricter, or set to None to turn the experience filter off entirely.
MAX_YEARS_EXPERIENCE = 2

# Spelled-out numbers seen in the wild ("at least three years").
_WORD_NUMBERS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}
_NUM = r"(\d{1,2}|" + "|".join(_WORD_NUMBERS) + r")"
# Phrases that look backward at history instead of stating a requirement
# ("over the past 3 years we have..."), which would otherwise false-positive.
_BACKWARD_LOOKING = re.compile(
    r"\b(past|last|next|previous|prior to|within the|over the|ago)\b", re.I)
# Ordered most-explicit first; the first pattern that matches anything wins, so
# a bare "3 years" can't undercut an explicit "at least 5 years" elsewhere.
_YEARS_PATTERNS = [
    re.compile(rf"\b(?:at least|minimum(?: of)?|min\.?)\s+{_NUM}\s*\+?\s*(?:years?|yrs?)\b", re.I),
    re.compile(rf"\b{_NUM}\s*(?:\+|or more)\s*(?:years?|yrs?)\b", re.I),
    re.compile(rf"\b{_NUM}\s*(?:-|–|—|to)\s*\d{{1,2}}\s*(?:years?|yrs?)\b", re.I),
    re.compile(rf"\b{_NUM}\s*(?:years?|yrs?)\b", re.I),
]
# The match must sit near one of these or it isn't about experience at all.
_EXPERIENCE_CONTEXT = re.compile(
    r"\b(experience|exp|background|working|industry|professional)\b", re.I)

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

# Europe-only roles are DROPPED, remote or on-site. A role that also lists a US
# location is kept, so "San Francisco, New York, London" survives.
#
# Order matters: a US signal wins outright, and only then do we test for Europe.
# That precedence is what makes bare city names safe to list below -- "Vienna,
# VA", "Dublin, CA" and "Cambridge, MA" are US roles that match a US term first
# and never reach the Europe test. Never reorder these two checks.
US_TERMS = [
    "usa", "u.s.", "united states", "us-remote", "remote us", "remote - us",
    # Full state names are unambiguous.
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado",
    "connecticut", "delaware", "florida", "georgia", "hawaii", "idaho",
    "illinois", "indiana", "iowa", "kansas", "kentucky", "louisiana", "maine",
    "maryland", "massachusetts", "michigan", "minnesota", "mississippi",
    "missouri", "montana", "nebraska", "nevada", "new hampshire", "new jersey",
    "new mexico", "north carolina", "north dakota", "ohio", "oklahoma",
    "oregon", "pennsylvania", "rhode island", "south carolina", "south dakota",
    "tennessee", "texas", "utah", "vermont", "virginia", "washington",
    "west virginia", "wisconsin", "wyoming",
    # Big US metros, so "New York, London" reads as US.
    "new york", "nyc", "brooklyn", "san francisco", "bay area", "san jose",
    "palo alto", "mountain view", "sunnyvale", "oakland", "san diego",
    "los angeles", "seattle", "bellevue", "redmond", "chicago", "austin",
    "boston", "denver", "atlanta", "philadelphia", "pittsburgh", "phoenix",
    "dallas", "houston", "miami", "detroit", "minneapolis", "portland",
    "salt lake", "nashville", "charlotte", "arlington", "sacramento", "irvine",
]
# State codes match as whole words only. Deliberately omits the codes that
# collide with European names or English words: DE (Delaware/Deutschland),
# IN, OR, IT, ME, OK, HI, ID, LA, NO, FI, PL, AT, BE, SE, CH, IE.
US_STATE_CODES = {
    "al", "ak", "az", "ar", "ca", "co", "ct", "dc", "fl", "ga", "ia", "il",
    "ks", "ky", "ma", "md", "mi", "mn", "mo", "ms", "mt", "nc", "nd", "nh",
    "nj", "nm", "nv", "ny", "oh", "pa", "ri", "sc", "sd", "tn", "tx", "ut",
    "va", "vt", "wa", "wi", "wv", "wy",
}

EUROPE_TERMS = [
    # Regions
    "europe", "european", "emea",
    # Countries
    "united kingdom", "england", "scotland", "wales", "northern ireland",
    "ireland", "germany", "deutschland", "france", "spain", "portugal",
    "italy", "netherlands", "holland", "belgium", "luxembourg",
    "switzerland", "austria", "sweden", "norway", "denmark", "finland",
    "iceland", "poland", "czech", "czechia", "slovakia", "slovenia",
    "hungary", "romania", "bulgaria", "greece", "croatia", "serbia",
    "estonia", "latvia", "lithuania", "ukraine", "malta", "cyprus",
    # Cities. Safe to list because a US term already won above.
    "london", "dublin", "berlin", "munich", "hamburg", "frankfurt", "cologne",
    "dusseldorf", "stuttgart", "paris", "lyon", "toulouse", "marseille",
    "madrid", "barcelona", "valencia", "seville", "lisbon", "porto",
    "milan", "rome", "turin", "bologna", "amsterdam", "rotterdam",
    "eindhoven", "brussels", "antwerp", "zurich", "geneva", "basel",
    "vienna", "stockholm", "gothenburg", "malmo", "oslo", "bergen",
    "copenhagen", "aarhus", "helsinki", "tampere", "warsaw", "krakow",
    "wroclaw", "gdansk", "poznan", "prague", "brno", "budapest",
    "bucharest", "cluj", "sofia", "athens", "thessaloniki", "zagreb",
    "tallinn", "riga", "vilnius", "edinburgh", "glasgow", "belfast", "cork",
]
# Country codes matched as whole words only (ISO-ish, as ATSes emit them).
EUROPE_CODES = {
    "uk", "gbr", "gb", "irl", "deu", "ger", "fra", "esp", "prt", "ita",
    "nld", "bel", "lux", "che", "aut", "swe", "nor", "dnk", "fin", "isl",
    "pol", "cze", "svk", "svn", "hun", "rou", "bgr", "grc", "hrv", "srb",
    "est", "lva", "ltu", "ukr", "mlt", "cyp",
}

# Google Sheet. SHEET_ID (from the sheet URL) is preferred: opening by key needs
# only the spreadsheets scope and no Drive API call. Falling back to the name
# costs a Drive lookup per run and needs the Drive API enabled.
SHEET_ID = os.environ.get("SHEET_ID", "")
SHEET_NAME = "Job Tracker"
WORKSHEET_NAME = "Jobs"
HEADER = ["Date added", "Date posted", "Priority", "Company", "Title", "Location",
          "URL", "Applied?"]
URL_COL = 7       # column G holds the URL (used for dedup)
PRIORITY_IDX = 2  # index of the Priority cell within a row

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


def _norm_date(value):
    """Normalize a source's posted date to YYYY-MM-DD.

    Returns "" when a source gives nothing usable rather than inventing a date,
    so an empty Date posted cell means "this source didn't say", not "today".
    Handles epoch seconds/ms (New-Grad Feed, Lever), ISO 8601 with or without a
    timezone (Greenhouse, Ashby, Microsoft), and Amazon's "August  7, 2026".
    """
    if value is None or value == "":
        return ""
    if isinstance(value, bool):
        return ""
    if isinstance(value, (int, float)):
        ts = float(value)
        if ts > 1e11:  # milliseconds, not seconds
            ts /= 1000.0
        try:
            return datetime.datetime.fromtimestamp(
                ts, datetime.timezone.utc).date().isoformat()
        except (ValueError, OSError, OverflowError):
            return ""
    s = re.sub(r"\s+", " ", str(value)).strip()
    if not s:
        return ""
    if s.isdigit():  # epoch arriving as a string
        return _norm_date(int(s))
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return m.group(0)
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%m/%d/%Y", "%d %B %Y"):
        try:
            return datetime.datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return ""


def _has_term(text, terms, codes):
    """Substring match on terms, whole-word match on short codes."""
    for t in terms:
        if t in text:
            return True
    for code in codes:
        if re.search(rf"\b{re.escape(code)}\b", text):
            return True
    return False


def is_europe_only(loc):
    """True if the role looks European with no US location alongside it.

    A US signal short-circuits, which is what keeps "Vienna, VA" and
    "San Francisco, New York, London" out of the Europe bucket. A blank or bare
    "Remote" location matches nothing and is kept.
    """
    text = (loc or "").lower()
    if not text.strip():
        return False
    if _has_term(text, US_TERMS, US_STATE_CODES):
        return False
    return _has_term(text, EUROPE_TERMS, EUROPE_CODES)


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
        # first_published, not updated_at: a reposted job updates the latter.
        "posted": j.get("first_published", ""),
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
            "posted": j.get("createdAt", ""),  # epoch ms
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
        "posted": j.get("publishedAt", ""),
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
                    # Workday only gives relative text ("Posted 5 Days Ago"),
                    # which won't normalize to a real date, so leave it blank.
                    "posted": "",
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
                "posted": j.get("posted_date", ""),
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
                "posted": j.get("publish_date", ""),
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
                "posted": props.get("postingDate", ""),
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
            # Epoch seconds. Reflects when the feed picked the job up, which
            # can lag the company's own posting date.
            "posted": j.get("date_posted", ""),
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
def min_years_required(desc):
    """Smallest number of years of experience a posting asks for.

    Returns None when the description says nothing about years, which includes
    every source that ships no description at all (Workday, the New-Grad Feed).
    Taking the *minimum* is deliberate: a posting listing "2+ years required,
    5+ preferred" is judged on the 2, so we under-drop rather than over-drop.
    """
    if not desc:
        return None
    text = re.sub(r"\s+", " ", desc)
    for pat in _YEARS_PATTERNS:
        best = None
        for m in pat.finditer(text):
            # Skip retrospective phrasing like "over the past 3 years".
            if _BACKWARD_LOOKING.search(text[max(0, m.start() - 30):m.start()]):
                continue
            window = text[max(0, m.start() - 40):m.end() + 50]
            if not _EXPERIENCE_CONTEXT.search(window):
                continue
            tok = m.group(1)
            val = int(tok) if tok.isdigit() else _WORD_NUMBERS[tok.lower()]
            if best is None or val < best:
                best = val
        if best is not None:
            return best
    return None


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
    # Location is only a filter in one direction: Europe-only roles are dropped.
    # Everywhere else is kept, and location otherwise just drives priority
    # ranking (see is_priority_location).
    title = (job.get("title") or "").lower()
    desc = (job.get("description") or "").lower()

    if is_europe_only(job.get("location")):
        return False
    if not any(k in title for k in TITLE_INCLUDE):
        return False
    if _title_excluded(title):
        return False
    if any(p in desc for p in DESCRIPTION_EXCLUDE):
        return False
    if MAX_YEARS_EXPERIENCE is not None:
        years = min_years_required(desc)
        if years is not None and years > MAX_YEARS_EXPERIENCE:
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
    ordered = sorted(rows, key=lambda r: r[PRIORITY_IDX] != "Yes")
    n_prio = sum(1 for r in rows if r[PRIORITY_IDX] == "Yes")
    lines = [f"{len(rows)} new role(s) ({n_prio} in your preferred locations):\n"]
    html_items = []
    for _added, posted, prio, company, title, location, url, _a in ordered:
        star = "\u2b50 " if prio == "Yes" else ""
        when = f", posted {posted}" if posted else ""
        lines.append(f"- {star}{company}: {title} ({location}{when})\n  {url}")
        badge = ("<span style='color:#c47f00'>\u2b50 priority</span> " if prio == "Yes" else "")
        html_items.append(
            f"<li>{badge}<b>{html.escape(company)}</b>: "
            f"<a href='{html.escape(url)}'>{html.escape(title)}</a> "
            f"<span style='color:#666'>{html.escape(location)}{html.escape(when)}</span></li>"
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
            new_rows.append([today, _norm_date(j.get("posted")), prio, c["name"],
                             j["title"], j["location"], url, ""])
            existing_urls.add(url)
            kept += 1
        print(f"{c['name']}: {kept} new match(es)")

    if new_rows:
        # Append priority roles first so they sit higher in the sheet.
        new_rows.sort(key=lambda r: r[PRIORITY_IDX] != "Yes")
        ws.append_rows(new_rows, value_input_option="USER_ENTERED")
        try:
            send_email(new_rows)
        except Exception as e:
            print(f"[warn] email failed: {e}")
    n_prio = sum(1 for r in new_rows if r[PRIORITY_IDX] == "Yes")
    print(f"Done. Added {len(new_rows)} new job(s) ({n_prio} priority).")


if __name__ == "__main__":
    main()
