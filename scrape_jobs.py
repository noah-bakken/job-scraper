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
import argparse
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
# Every slug below was probed live and confirmed to return real postings for
# the company it is named after -- not just to return 200. That check matters:
# the greenhouse "disney" board is a stub whose only posting is titled "MASTER
# TEMPLATE", and lever "capital" is not Capital One. Both were rejected here.
COMPANIES = [
    # --- AI / ML ---
    {"name": "Anthropic",   "ats": "greenhouse", "slug": "anthropic"},
    {"name": "OpenAI",      "ats": "ashby",      "slug": "openai"},
    {"name": "Scale AI",    "ats": "greenhouse", "slug": "scaleai"},
    {"name": "Perplexity",  "ats": "ashby",      "slug": "perplexity"},
    {"name": "Cohere",      "ats": "ashby",      "slug": "cohere"},
    {"name": "Together AI", "ats": "greenhouse", "slug": "togetherai"},
    {"name": "Sierra",      "ats": "ashby",      "slug": "sierra"},
    {"name": "Harvey",      "ats": "ashby",      "slug": "harvey"},
    {"name": "Replit",      "ats": "ashby",      "slug": "replit"},
    {"name": "Modal",       "ats": "ashby",      "slug": "modal"},
    {"name": "Baseten",     "ats": "ashby",      "slug": "baseten"},
    {"name": "ElevenLabs",  "ats": "ashby",      "slug": "elevenlabs"},
    {"name": "Character.AI", "ats": "ashby",     "slug": "character"},
    {"name": "Pinecone",    "ats": "ashby",      "slug": "pinecone"},

    # --- Fintech ---
    {"name": "Stripe",      "ats": "greenhouse", "slug": "stripe"},
    {"name": "Plaid",       "ats": "ashby",      "slug": "plaid"},
    {"name": "Chime",       "ats": "greenhouse", "slug": "chime"},
    {"name": "Affirm",      "ats": "greenhouse", "slug": "affirm"},
    {"name": "Marqeta",     "ats": "greenhouse", "slug": "marqeta"},
    {"name": "Mercury",     "ats": "greenhouse", "slug": "mercury"},
    {"name": "Betterment",  "ats": "greenhouse", "slug": "betterment"},
    {"name": "Carta",       "ats": "greenhouse", "slug": "carta"},
    {"name": "Gusto",       "ats": "greenhouse", "slug": "gusto"},
    {"name": "Airwallex",   "ats": "ashby",      "slug": "airwallex"},
    {"name": "Wealthfront", "ats": "lever",      "slug": "wealthfront"},
    {"name": "Block",       "ats": "greenhouse", "slug": "block"},
    {"name": "Brex",        "ats": "greenhouse", "slug": "brex"},
    {"name": "Ramp",        "ats": "ashby",      "slug": "ramp"},
    {"name": "Coinbase",    "ats": "greenhouse", "slug": "coinbase"},
    {"name": "Robinhood",   "ats": "greenhouse", "slug": "robinhood"},
    # greenhouse "figure" is Figure Lending (Reno fintech), not Figure AI.
    {"name": "Figure Lending", "ats": "greenhouse", "slug": "figure"},

    # --- Dev tools / infra / SaaS ---
    {"name": "Figma",       "ats": "greenhouse", "slug": "figma"},
    {"name": "Databricks",  "ats": "greenhouse", "slug": "databricks"},
    {"name": "Notion",      "ats": "ashby",      "slug": "notion"},
    {"name": "Vercel",      "ats": "greenhouse", "slug": "vercel"},
    {"name": "Netlify",     "ats": "greenhouse", "slug": "netlify"},
    {"name": "Linear",      "ats": "ashby",      "slug": "linear"},
    {"name": "Airtable",    "ats": "greenhouse", "slug": "airtable"},
    {"name": "Asana",       "ats": "greenhouse", "slug": "asana"},
    {"name": "Amplitude",   "ats": "greenhouse", "slug": "amplitude"},
    {"name": "Postman",     "ats": "greenhouse", "slug": "postman"},
    {"name": "Datadog",     "ats": "greenhouse", "slug": "datadog"},
    {"name": "Confluent",   "ats": "ashby",      "slug": "confluent"},
    {"name": "GitLab",      "ats": "greenhouse", "slug": "gitlab"},
    {"name": "Sentry",      "ats": "ashby",      "slug": "sentry"},
    {"name": "Grafana Labs", "ats": "greenhouse", "slug": "grafanalabs"},
    {"name": "Supabase",    "ats": "ashby",      "slug": "supabase"},
    {"name": "Render",      "ats": "ashby",      "slug": "render"},
    {"name": "Cloudflare",  "ats": "greenhouse", "slug": "cloudflare"},
    {"name": "Snowflake",   "ats": "ashby",      "slug": "snowflake"},
    {"name": "MongoDB",     "ats": "greenhouse", "slug": "mongodb"},
    {"name": "Elastic",     "ats": "greenhouse", "slug": "elastic"},
    {"name": "Okta",        "ats": "greenhouse", "slug": "okta"},
    {"name": "Twilio",      "ats": "greenhouse", "slug": "twilio"},
    {"name": "PlanetScale", "ats": "greenhouse", "slug": "planetscale"},
    {"name": "Temporal",    "ats": "ashby",      "slug": "temporal"},
    {"name": "Vanta",       "ats": "ashby",      "slug": "vanta"},
    {"name": "Webflow",     "ats": "greenhouse", "slug": "webflow"},
    {"name": "Miro",        "ats": "ashby",      "slug": "miro"},
    {"name": "Dropbox",     "ats": "greenhouse", "slug": "dropbox"},
    {"name": "Palantir",    "ats": "lever",      "slug": "palantir"},

    # --- Consumer / marketplace ---
    {"name": "Airbnb",      "ats": "greenhouse", "slug": "airbnb"},
    {"name": "Instacart",   "ats": "greenhouse", "slug": "instacart"},
    {"name": "Lyft",        "ats": "greenhouse", "slug": "lyft"},
    {"name": "Pinterest",   "ats": "greenhouse", "slug": "pinterest"},
    {"name": "Discord",     "ats": "greenhouse", "slug": "discord"},
    {"name": "Spotify",     "ats": "lever",      "slug": "spotify"},
    {"name": "Roblox",      "ats": "greenhouse", "slug": "roblox"},
    {"name": "Twitch",      "ats": "greenhouse", "slug": "twitch"},
    {"name": "Reddit",      "ats": "greenhouse", "slug": "reddit"},
    {"name": "Duolingo",    "ats": "greenhouse", "slug": "duolingo"},
    {"name": "Strava",      "ats": "ashby",      "slug": "strava"},
    {"name": "Thumbtack",   "ats": "ashby",      "slug": "thumbtack"},
    {"name": "Faire",       "ats": "greenhouse", "slug": "faire"},
    {"name": "Squarespace", "ats": "greenhouse", "slug": "squarespace"},
    {"name": "Patreon",     "ats": "ashby",      "slug": "patreon"},
    {"name": "Substack",    "ats": "ashby",      "slug": "substack"},

    # --- Health / bio ---
    {"name": "Oscar Health",    "ats": "greenhouse", "slug": "oscar"},
    {"name": "Ro",              "ats": "lever",      "slug": "ro"},
    {"name": "Hims & Hers",     "ats": "ashby",      "slug": "hims-and-hers"},
    {"name": "Included Health", "ats": "lever",      "slug": "includedhealth"},
    {"name": "Benchling",       "ats": "ashby",      "slug": "benchling"},
    {"name": "Color Health",    "ats": "ashby",      "slug": "color-health"},
    {"name": "Komodo Health",   "ats": "greenhouse", "slug": "komodohealth"},

    # --- Broad "search anything" feed: a maintained new-grad list spanning
    #     hundreds of companies (startups + big cos), with apply links. This is
    #     what casts the wide net; the per-company sources above add depth. ---
    {"name": "New-Grad Feed", "ats": "newgrad_feed"},

    # --- Marquee search endpoints (best-effort; verify on first run) ---
    {"name": "Amazon",    "ats": "amazon"},
    {"name": "Google",    "ats": "google"},
    {"name": "Microsoft", "ats": "microsoft"},

    # --- Workday: big employers that aren't on Greenhouse/Lever/Ashby. ---
    # A Workday careers URL looks like:
    #   https://TENANT.wdN.myworkdayjobs.com/SITE
    # Each of these was confirmed against the same CXS endpoint fetch_workday()
    # calls. Workday ships no description, so these lean on _fetch_description().
    {"name": "Salesforce", "ats": "workday", "tenant": "salesforce", "wd_host": "wd12", "site": "External_Career_Site"},
    {"name": "Adobe",      "ats": "workday", "tenant": "adobe",      "wd_host": "wd5",  "site": "external_experienced"},
    {"name": "Nvidia",     "ats": "workday", "tenant": "nvidia",     "wd_host": "wd5",  "site": "NVIDIAExternalCareerSite"},
    {"name": "PayPal",     "ats": "workday", "tenant": "paypal",     "wd_host": "wd1",  "site": "jobs"},
    {"name": "eBay",       "ats": "workday", "tenant": "ebay",       "wd_host": "wd5",  "site": "apply"},
    {"name": "Mastercard", "ats": "workday", "tenant": "mastercard", "wd_host": "wd1",  "site": "CorporateCareers"},
    {"name": "Autodesk",   "ats": "workday", "tenant": "autodesk",   "wd_host": "wd1",  "site": "Ext"},
    {"name": "Workday",    "ats": "workday", "tenant": "workday",    "wd_host": "wd5",  "site": "Workday"},
    {"name": "T-Mobile",   "ats": "workday", "tenant": "tmobile",    "wd_host": "wd1",  "site": "External"},
    {"name": "Zillow",     "ats": "workday", "tenant": "zillow",     "wd_host": "wd5",  "site": "Zillow_Group_External"},
    {"name": "Comcast",    "ats": "workday", "tenant": "comcast",    "wd_host": "wd5",  "site": "Comcast_Careers"},
    {"name": "Target",     "ats": "workday", "tenant": "target",     "wd_host": "wd5",  "site": "targetcareers"},
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

# Drop roles asking for full-time experience you don't have. Listing phrasings
# ("3+ years", "3-5 years", "at least three years", "minimum of 3 yrs") never
# ends, so years_required() parses the years a posting demands and we compare
# against this ceiling.
#
# You graduated May 2026 with four internships and no full-time experience, so
# the ceiling is 0: a posting asking for even "1+ years" is asking for something
# you do not have. Postings whose requirement internships satisfy ("1+ years of
# internship experience") are NOT counted -- see years_required. A posting
# stating "0-2 years" reads as 0 and still comes through.
#
# Raise to 1 or 2 to allow a stretch, or set to None to disable the filter.
MAX_YEARS_EXPERIENCE = 0

# When you graduated, as (year, month). Roles that target a LATER graduation
# window aren't open to you: "you will graduate in Fall 2026 or Spring 2027"
# excludes a Spring 2026 grad, and "graduating in Fall 2027" is an internship
# for someone still two years from finishing.
#
# This is deliberately a date comparison and NOT a ban on internships. Plenty of
# internships and co-ops accept recent grads, and those keep whatever window
# they state, so they still come through. Set to None to disable.
GRADUATED = (2026, 5)

_SEASON_MONTH = {"winter": 1, "spring": 5, "summer": 7, "fall": 9, "autumn": 9}
# A year only counts as a graduation window if graduation-ish words sit near it,
# so "founded in 2013" and "our 2026 roadmap" are ignored.
_GRAD_CONTEXT = re.compile(
    r"\b(graduat\w*|pursu\w*|enroll\w*|degree|class of|commencement|"
    r"bachelor\w*|master\w*|undergrad\w*)\b", re.I)
_GRAD_WINDOW = re.compile(
    r"\b(?:(winter|spring|summer|fall|autumn)\s+)?((?:19|20)\d{2})\b", re.I)

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
# A company describing itself -- "we have been building this for 10 years",
# "we've been working in this industry for over 8 years" -- is the most common
# way a description mentions years without asking for any. The "been ... for N
# years" shape is checked over a wider lookback than _BACKWARD_LOOKING uses,
# because the "been" can sit a clause away from the number; it stops at sentence
# punctuation so it can't reach across into an unrelated sentence.
#
# This deliberately does not swallow "we are looking for 3+ years of
# experience", a real requirement using the same "for N years" shape. The cost
# is that a requirement phrased "you must have been in a product role for 3
# years" is missed -- far rarer than the boilerplate, and at a ceiling of 0 a
# false positive silently hides a job you could actually get.
_COMPANY_HISTORY = re.compile(
    r"\bbeen\b[^.;!?]{0,60}\bfor\s+(?:over\s+|more than\s+|nearly\s+|almost\s+)?$", re.I)
# Requirement-shaped phrasings. These are counted wherever they appear, with no
# nearby-keyword requirement: "5+ years in performance engineering" states a
# requirement even though it never says the word "experience".
_YEARS_EXPLICIT = [
    re.compile(rf"\b(?:at least|minimum(?: of)?|min\.?)\s+{_NUM}\s*\+?\s*(?:years?|yrs?)\b", re.I),
    re.compile(rf"\b{_NUM}\s*(?:\+|or more)\s*(?:years?|yrs?)\b", re.I),
    # A range contributes its FLOOR, so "1-3 years" reads as 1 and survives.
    re.compile(rf"\b{_NUM}\s*(?:-|–|—|to)\s*\d{{1,2}}\s*(?:years?|yrs?)\b", re.I),
]
# A bare "3 years" is ambiguous ("our 5 year vision"), so it only counts when a
# word below sits nearby.
_YEARS_BARE = re.compile(rf"\b{_NUM}\s*(?:years?|yrs?)\b", re.I)
_EXPERIENCE_CONTEXT = re.compile(
    r"\b(experience|exp|background|working|industry|professional|track record)\b", re.I)
# Years that internships or co-ops can satisfy are not full-time years, so a
# posting wanting "1+ years of internship experience" is open to you and the
# requirement is ignored rather than counted against the ceiling.
_INTERNSHIP_CONTEXT = re.compile(
    r"\b(interns?|internships?|co-?ops?|apprenticeships?)\b", re.I)
# "Are you at least 18 years of age?" is an application-form eligibility
# question, not a requirement -- but it is phrased exactly like one, so
# _YEARS_EXPLICIT matches it and the role scores as needing 18 years. Fetching
# full posting pages made this common, since that is where such forms live.
_AGE_CONTEXT = re.compile(r"\s*(of age|years? old|or older)\b", re.I)

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

# Non-US regions outside Europe. Same precedence rule as EUROPE_TERMS: a US
# signal wins first, so bare city names are safe to list here.
#
# The filter drops on a positive NON-US signal instead of requiring a positive
# US one, and that direction is deliberate. US_TERMS is tuned for the Europe
# test, where a missed US signal is harmless, so it does not recognize "SF" and
# US_STATE_CODES omits ID/LA/IN/OR because they collide with words and European
# names. Requiring a US match would therefore delete real US roles -- "SF" and
# "Boise, ID" both fail it. Dropping only on a positive non-US match keeps
# those, at the cost of keeping a role whose country is simply never named.
NON_US_TERMS = [
    # Canada
    "canada", "ontario", "quebec", "alberta", "british columbia", "manitoba",
    "saskatchewan", "nova scotia", "new brunswick", "toronto", "vancouver",
    "montreal", "ottawa", "calgary", "edmonton", "waterloo", "north york",
    "mississauga", "saint john",
    # Latin America
    "brazil", "brasil", "sao paulo", "são paulo", "rio de janeiro", "mexico",
    "guadalajara", "monterrey", "argentina", "buenos aires", "chile",
    "santiago", "colombia", "bogota", "bogotá", "medellin", "peru", "lima",
    "costa rica", "san jose, cr", "uruguay", "montevideo",
    # Asia-Pacific
    "india", "bengaluru", "bangalore", "hyderabad", "gurgaon", "gurugram",
    "noida", "mumbai", "pune", "chennai", "delhi", "kolkata",
    "singapore", "japan", "tokyo", "osaka", "china", "shanghai", "beijing",
    "shenzhen", "hong kong", "taiwan", "taipei", "korea", "seoul",
    "australia", "sydney", "melbourne", "brisbane", "perth", "canberra",
    "new zealand", "auckland", "wellington",
    "philippines", "manila", "vietnam", "hanoi", "ho chi minh",
    "thailand", "bangkok", "indonesia", "jakarta", "malaysia",
    "kuala lumpur", "nsw", "victoria, au",
    # Middle East / Africa
    "israel", "tel aviv", "tel-aviv", "haifa", "jerusalem", "turkey",
    "istanbul", "united arab emirates", "dubai", "abu dhabi", "qatar", "doha",
    "saudi", "riyadh", "egypt", "cairo", "nigeria", "lagos", "kenya",
    "nairobi", "south africa", "johannesburg", "cape town",
]
# Three-letter country codes, as Amazon writes them ("Sao Paulo, BRA").
NON_US_CODES = {
    "bra", "jpn", "can", "ind", "sgp", "aus", "mex", "gbr", "deu", "fra",
    "chn", "kor", "isr", "are", "zaf", "phl", "vnm", "tha", "idn", "mys",
    "nzl", "col", "arg", "chl", "esp", "ita", "nld", "pol", "irl", "che",
    "swe", "tur", "egy", "sau", "qat", "ken", "per", "ury",
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


def is_non_us(loc):
    """True if the role names a country outside the US.

    You only want US roles, so this is a hard filter in matches(). It drops on
    a positive non-US signal rather than requiring a positive US one -- see the
    note above NON_US_TERMS for why requiring a US match would delete real US
    roles like "SF" and "Boise, ID".

    The consequence to know: a listing that never names a country is KEPT. A
    blank location, or a bare "Remote" with no country, has nothing to match,
    and dropping those would lose US-remote roles.
    """
    text = (loc or "").lower()
    if not text.strip():
        return False
    if _has_term(text, US_TERMS, US_STATE_CODES):
        return False
    return (_has_term(text, EUROPE_TERMS, EUROPE_CODES)
            or _has_term(text, NON_US_TERMS, NON_US_CODES))


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


GOOGLE_BASE = "https://www.google.com/about/careers/applications/"
GOOGLE_RESULTS_URL = GOOGLE_BASE + "jobs/results"
GOOGLE_PAGE_SIZE = 20   # what the results page returns per request
GOOGLE_MAX_PAGES = 5    # 100 roles per query; the list is relevance-ordered
# Cards are found by the job link plus its "Learn more about <title>" label
# rather than by CSS class: Google's class names are obfuscated build artifacts
# ("lLd3Je") that change without notice, while these two are structural.
_G_CARD = re.compile(r'href="(jobs/results/[^"]+)"[^>]*aria-label="Learn more about ([^"]+)"')
_G_LOC = re.compile(r'place</i>.{0,200}?<span[^>]*>([^<]{3,80})</span>', re.S)


def fetch_google(c):
    """Scrape the Google careers results page.

    The old careers.google.com/api/v3 JSON endpoint was retired and now 404s.
    Its replacement is server-rendered HTML, so this parses the results page
    directly. Each card carries its own qualifications text, which means the
    experience filter still has a description to read and no per-job detail
    request is needed.

    No posted date is exposed on the listing, so "posted" is left blank -- the
    sheet's own Date added column still records when a role first showed up.
    """
    out = []
    seen = set()
    for q in SEARCH_QUERIES:
        for page in range(1, GOOGLE_MAX_PAGES + 1):
            r = requests.get(GOOGLE_RESULTS_URL, params={"q": q, "page": page},
                             timeout=25, headers=REQ_HEADERS)
            r.raise_for_status()
            page_html = r.text
            cards = list(_G_CARD.finditer(page_html))
            if not cards:
                break
            prev = 0
            for m in cards:
                body = page_html[prev:m.start()]
                prev = m.end()
                # Keep only the card's own <li>, so the page header above the
                # first card can't leak its text into that card's description
                # and trip the experience filter on unrelated numbers. Match
                # '<li class="' specifically: the card container carries a
                # class, while the qualification bullets inside it are bare
                # <li>, and cutting at those would strip the location and all
                # but the last bullet of the description.
                cut = body.rfind('<li class="')
                if cut != -1:
                    body = body[cut:]
                url = GOOGLE_BASE + m.group(1).split("?")[0]
                if url in seen:
                    continue
                seen.add(url)
                loc = _G_LOC.search(body)
                out.append({
                    "title": html.unescape(m.group(2)),
                    "location": html.unescape(loc.group(1)) if loc else "",
                    "url": url,
                    "posted": "",
                    "description": _strip_html(body),
                })
            if len(cards) < GOOGLE_PAGE_SIZE:
                break
    return out


def fetch_microsoft(c):
    """Currently broken on Microsoft's side -- expect this to fail every run.

    gcsservices.careers.microsoft.com is CNAME'd to an Azure Front Door
    endpoint that serves a certificate for *.azureedge.net, so the TLS
    handshake fails hostname verification. That is a misconfiguration only
    Microsoft can fix, and working around it would mean disabling certificate
    verification, which is not worth doing for a job scraper.

    Their careers site has also moved to an Eightfold-hosted SPA
    (apply.careers.microsoft.com) whose HTML contains no job data and whose
    API answers 403 without a session, so there is no drop-in replacement to
    scrape either. This is left in place, rather than deleted, so it recovers
    on its own if Microsoft fixes the certificate; main() reports it as a
    failed source on every run so the gap stays visible.
    """
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
            # This source aggregates hundreds of employers, so the actual
            # hiring company has to come from the row. Without it every entry
            # lands in the sheet as "New-Grad Feed", which says where the role
            # was found but not who is offering it.
            "company": j.get("company_name", ""),
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
# Anything separating two digits that isn't a digit, a letter, or a plain
# space: real dashes, but also the debris of an encoding mix-up. A page served
# as UTF-8 and decoded as Latin-1 turns "0-2 years" into "0\xe2\x80\x932
# years", and that matters more than it looks: the range pattern stops
# matching, the bare "2 years" tail is read instead, and a posting advertising
# itself as 0-2 years scores as a 2-year requirement. At a ceiling of 0 that
# silently deletes exactly the entry-level roles this scraper exists to find.
_DIGIT_GAP = re.compile(r"(?<=\d)[\s]*[-‐-―−\xe2\xc3\x80-\x9f~/]+[\s]*(?=\d)")


def _normalize_ranges(text):
    """Rewrite the separator in "0-2 years" to a plain hyphen."""
    return _DIGIT_GAP.sub("-", text)


def years_required(desc):
    """Highest number of years of experience a posting demands.

    Returns None when the description says nothing about years, which includes
    every source shipping no description at all (Workday, the New-Grad Feed).

    Takes the *highest* stated requirement on purpose. Figma's "Product Manager,
    CMS" asks for "5+ years of experience as a full-time Product Manager" and
    also mentions "2+ years" of something narrower; it is gated by the 5, so
    judging it on the 2 lets a senior role through. Ranges are the exception and
    contribute their floor, so "0-2 years" reads as 0 and survives.

    Years an internship can satisfy are skipped rather than counted, so "1+
    years of internship experience" does not read as a full-time requirement.
    """
    if not desc:
        return None
    text = _normalize_ranges(re.sub(r"\s+", " ", desc))
    best = None
    claimed = []  # spans a requirement-shaped pattern has already adjudicated

    def _consider(m, need_context):
        nonlocal best
        # Skip retrospective and forward-looking phrasing: "over the past 3
        # years", "founded 10 years ago", "in the next 3 years".
        if _BACKWARD_LOOKING.search(text[max(0, m.start() - 30):m.start()]):
            return
        if _COMPANY_HISTORY.search(text[max(0, m.start() - 80):m.start()]):
            return
        # "18 years of age" is an eligibility question, not experience.
        if _AGE_CONTEXT.match(text[m.end():m.end() + 20]):
            return
        window = text[max(0, m.start() - 40):m.end() + 50]
        if _INTERNSHIP_CONTEXT.search(window):
            return
        if need_context and not _EXPERIENCE_CONTEXT.search(window):
            return
        tok = m.group(1)
        val = int(tok) if tok.isdigit() else _WORD_NUMBERS[tok.lower()]
        if best is None or val > best:
            best = val

    # Requirement-shaped phrasings count anywhere; a bare "3 years" needs a
    # nearby experience word to count at all.
    for pat in _YEARS_EXPLICIT:
        for m in pat.finditer(text):
            claimed.append((m.start(), m.end()))
            _consider(m, need_context=False)
    for m in _YEARS_BARE.finditer(text):
        # A range is counted as its floor above, and its text ends in a bare
        # "3 years" that this pattern would otherwise re-read -- and since the
        # winner is the max, that would silently undo the floor.
        if any(start < m.end() and m.start() < end for start, end in claimed):
            continue
        _consider(m, need_context=True)
    return best


def earliest_graduation_window(desc):
    """Earliest graduation date a posting targets, as (year, month).

    Returns None when the description names no graduation window, in which case
    the role is kept -- silence is not a disqualification. Takes the earliest of
    several windows, so "Fall 2026 or Spring 2027" is judged on Fall 2026 and a
    range like "December 2025 through June 2026" is judged on its opening edge.
    A bare year with no season resolves to January, the most inclusive reading.
    """
    if not desc:
        return None
    text = re.sub(r"\s+", " ", desc)
    best = None
    for m in _GRAD_WINDOW.finditer(text):
        window = text[max(0, m.start() - 90):m.end() + 90]
        if not _GRAD_CONTEXT.search(window):
            continue
        season, year = m.group(1), int(m.group(2))
        month = _SEASON_MONTH.get((season or "").lower(), 1)
        cand = (year, month)
        if best is None or cand < best:
            best = cand
    return best


def _fetch_description(url):
    """Best-effort: read a posting's text straight off its page.

    Some sources ship listings with no description at all -- the New-Grad Feed
    and Workday both do -- which blinds the experience filter: years_required()
    finds nothing, returns None, and the role is kept no matter what it
    actually requires. That is not a small gap. It is why roles deleted from
    the sheet by hand reappear on the very next run, since the scraper cannot
    see the requirement the deletion was based on.

    Returns "" on any failure, which preserves the old keep-on-silence
    behaviour rather than dropping a role because its page happened to 404.
    """
    try:
        r = requests.get(url, timeout=20, headers=REQ_HEADERS)
        if not r.ok:
            return ""
        # Sniff the encoding instead of taking the default. requests falls back
        # to Latin-1 when a page declares no charset, which turns "0-2 years"
        # into mojibake the range pattern can't read -- and a posting that
        # advertises itself as entry level then scores as a 2-year requirement.
        r.encoding = r.apparent_encoding or "utf-8"
        return _strip_html(r.text)
    except Exception:
        return ""


# "apm" and "tpm" are the only TITLE_INCLUDE entries short enough to be someone
# else's acronym, and both collide with product names on boards now being
# watched: APM is Application Performance Monitoring (Datadog, Grafana, Sentry,
# Elastic, Confluent all sell one) and TPM is Trusted Platform Module on
# hardware boards like Nvidia's. Matched as whole words, and a title that
# matched *only* on one of these is rejected if it also reads as an engineering
# role -- "Manager I, Engineering - APM Serverless" is not a product job.
TITLE_ACRONYMS = {"apm", "tpm"}
ACRONYM_FALSE_FRIENDS = [
    "engineering", "engineer", "software", "serverless", "observability",
    "monitoring", "firmware", "silicon", "platform module",
]


def passes_title(job):
    """Title-only gate, split out of matches() so main() can apply it before
    paying for a description fetch."""
    title = (job.get("title") or "").lower()
    if _title_excluded(title):
        return False
    matched = set()
    for k in TITLE_INCLUDE:
        if k in TITLE_ACRONYMS:
            if re.search(rf"\b{re.escape(k)}\b", title):
                matched.add(k)
        elif k in title:
            matched.add(k)
    if not matched:
        return False
    # Only a bare acronym carried the match, so make sure it's really ours.
    if matched <= TITLE_ACRONYMS:
        return not any(w in title for w in ACRONYM_FALSE_FRIENDS)
    return True


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
    # US-only: anything naming a country outside the US is dropped. A listing
    # that names no country at all is kept (see is_non_us), and location
    # otherwise drives priority ranking (see is_priority_location).
    title = (job.get("title") or "").lower()
    desc = (job.get("description") or "").lower()

    if is_non_us(job.get("location")):
        return False
    if not passes_title(job):
        return False
    if any(p in desc for p in DESCRIPTION_EXCLUDE):
        return False
    if MAX_YEARS_EXPERIENCE is not None:
        years = years_required(desc)
        if years is not None and years > MAX_YEARS_EXPERIENCE:
            return False
    if GRADUATED is not None:
        grad = earliest_graduation_window(desc)
        if grad is not None and grad > GRADUATED:
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


def get_worksheet(create=True):
    """Open the tracker worksheet, creating it and its header row if missing.

    Pass create=False for a read-only open (--dry-run): a missing worksheet
    returns None and a missing header is left alone, so opening the sheet to
    read existing URLs can't itself write to it.
    """
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
        if not create:
            return None
        ws = sh.add_worksheet(title=WORKSHEET_NAME, rows=5000, cols=len(HEADER))
    # gspread returns [[]] for an empty worksheet, which is truthy, so test for
    # real cell content instead of list emptiness.
    if create and not any(cell for row in ws.get_all_values() for cell in row):
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
def main(dry_run=False):
    ws = get_worksheet(create=not dry_run)
    if ws is None:
        print("[dry-run] Worksheet does not exist yet; treating it as empty.")
        existing_urls = set()
    else:
        existing_urls = set(ws.col_values(URL_COL))  # column G = URL
    today = datetime.date.today().isoformat()
    new_rows = []

    failed = []
    for c in COMPANIES:
        fetcher = FETCHERS.get(c["ats"])
        if not fetcher:
            print(f"[warn] unknown ats '{c['ats']}' for {c['name']}, skipping")
            failed.append((c["name"], f"unknown ats '{c['ats']}'"))
            continue
        try:
            jobs = fetcher(c)
        except Exception as e:
            print(f"[warn] {c['name']} ({c['ats']}) failed: {e}")
            failed.append((c["name"], str(e).split("\n")[0][:120]))
            continue

        kept = 0
        for j in jobs:
            url = j.get("url") or ""
            if not url or url in existing_urls:
                continue
            # Gate on title and location first: both are free, and this keeps
            # the fetch below to the handful of roles that could still qualify.
            if not passes_title(j) or is_non_us(j.get("location")):
                continue
            # A listing with no description can't be judged on experience, so
            # go get one. Without this, description-less sources bypass the
            # experience filter entirely (see _fetch_description).
            if not (j.get("description") or "").strip():
                j["description"] = _fetch_description(url)
            if not matches(j):
                continue
            prio = "Yes" if is_priority_location(j.get("location")) else ""
            # Aggregator sources name the real employer per row; single-company
            # sources don't set this and fall back to the source name, which is
            # the company anyway.
            company = (j.get("company") or "").strip() or c["name"]
            new_rows.append([today, _norm_date(j.get("posted")), prio, company,
                             j["title"], j["location"], url, ""])
            existing_urls.add(url)
            kept += 1
        print(f"{c['name']}: {kept} new match(es)")

    if new_rows:
        # Append priority roles first so they sit higher in the sheet.
        new_rows.sort(key=lambda r: r[PRIORITY_IDX] != "Yes")
        if dry_run:
            print("\n--- would add ---")
            for _added, posted, prio, company, title, location, url, _a in new_rows:
                star = "* " if prio == "Yes" else "  "
                when = f", posted {posted}" if posted else ""
                print(f"{star}{company}: {title} ({location}{when})\n    {url}")
        else:
            ws.append_rows(new_rows, value_input_option="USER_ENTERED")
            try:
                send_email(new_rows)
            except Exception as e:
                print(f"[warn] email failed: {e}")
    n_prio = sum(1 for r in new_rows if r[PRIORITY_IDX] == "Yes")
    verb = "Would add" if dry_run else "Added"
    print(f"\nDone. {verb} {len(new_rows)} new job(s) ({n_prio} priority).")
    # A source that dies is caught above so one bad endpoint can't sink the
    # run, but that also means coverage can quietly drop to zero for months.
    # Say so out loud: "0 new jobs" and "0 new jobs because 2 sources are
    # down" deserve very different reactions.
    if failed:
        print(f"\n[!] {len(failed)} of {len(COMPANIES)} source(s) FAILED and "
              f"contributed nothing:")
        for name, err in failed:
            print(f"    - {name}: {err}")
    if dry_run:
        print("\nDry run: nothing was written to the sheet and no email was sent.")


def send_test_email():
    """Send a digest built from one fake row, to prove the mail path works.

    The digest only goes out when a run finds something new, so a broken
    SMTP_PASS or EMAIL_TO stays invisible for as long as the sheet happens to
    be up to date -- and main() swallows send failures as warnings, so the run
    still reports success. This exercises the same send_email() path on demand
    so the alert channel can be verified without waiting for a new posting.
    """
    row = [datetime.date.today().isoformat(), "2026-08-09", "Yes",
           "Test", "Test: if you can read this, email works",
           "San Francisco, CA", "https://example.com/test-email", ""]
    send_email([row])


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true",
                    help="print what would be added without writing to the "
                         "sheet or sending the digest email")
    ap.add_argument("--test-email", action="store_true",
                    help="send a single sample digest and exit, to verify "
                         "SMTP_USER / SMTP_PASS / EMAIL_TO actually work")
    args = ap.parse_args()
    if args.test_email:
        send_test_email()
    else:
        main(dry_run=args.dry_run)
