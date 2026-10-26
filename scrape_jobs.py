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
import time
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

# Two independently-maintained new-grad job lists, referenced by COMPANIES
# below and read by fetch_newgrad_feed(). Defined up here (rather than next to
# the adapter, further down the file) because COMPANIES is evaluated at import
# time and needs them to already exist.
#
# Both share the identical schema (company_name, title, locations, url,
# date_posted, active, is_visible), so one adapter handles both -- only the
# URL differs. Confirmed live to barely overlap: of vanshb03's active
# listings, only ~2% share a URL with Simplify's, so running both is close to
# double the unique companies, not a duplicate of the same list.
NEWGRAD_FEED_URL = (
    "https://raw.githubusercontent.com/SimplifyJobs/"
    "New-Grad-Positions/dev/.github/scripts/listings.json"
)
NEWGRAD_FEED_URL_VANSHB03 = (
    "https://raw.githubusercontent.com/vanshb03/"
    "New-Grad-2026/main/.github/scripts/listings.json"
)

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
    # Moved off Greenhouse (old board 404s; redirects to a stub with no API
    # data) -- confirmed live on Ashby instead, 42 real current postings.
    {"name": "Marqeta",     "ats": "ashby", "slug": "marqeta-inc"},
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
    # Moved from Greenhouse (404) to Ashby.
    {"name": "Amplitude",   "ats": "ashby", "slug": "amplitude"},
    # Postman: its Greenhouse board 404s and no replacement board could be found on
    # Greenhouse/Lever/Ashby/Workable/SmartRecruiters, so it is not scraped.
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

    # --- Robotics / IoT: added for the customer-support scoping (see
    #     ROBOTICS_IOT_TERMS), but also surfaces any product-track role at
    #     these companies same as everywhere else. Every slug below was
    #     probed live and confirmed to be the real company, not a stub or
    #     wrong board -- "figure" on greenhouse is Figure Lending, not this
    #     Figure (the humanoid-robotics one), same trap as the Disney/
    #     Capital One examples above; the real one is "figureai".
    #
    #     "robotics": True marks every company in this section for the
    #     Jobs 2.0 "any role" exception (see ROBOTICS_COMPANY_NAMES): ANY
    #     new-grad-eligible posting at one of these companies is wanted,
    #     not just the product/support titles matched everywhere else in
    #     this file. Requested specifically because of the Boston Dynamics
    #     connection -- extended to the whole roster, not just that one
    #     company, since the reasoning ("worth a look even in a role you
    #     wouldn't otherwise consider") applies equally to any of them. ---
    {"name": "Skydio",           "ats": "ashby",      "slug": "skydio", "robotics": True},
    {"name": "Figure AI",        "ats": "greenhouse", "slug": "figureai", "robotics": True},
    {"name": "Agility Robotics", "ats": "greenhouse", "slug": "agilityrobotics", "robotics": True},
    {"name": "Nuro",             "ats": "greenhouse", "slug": "nuro", "robotics": True},
    {"name": "Samsara",          "ats": "greenhouse", "slug": "samsara", "robotics": True},
    {"name": "Verkada",          "ats": "greenhouse", "slug": "verkada", "robotics": True},
    {"name": "Simbe Robotics",   "ats": "lever",      "slug": "SimbeRobotics", "robotics": True},
    # Found via the New-Grad Feeds (see README) rather than hand-researched --
    # every one of these showed up there under a company you'd never have
    # named yourself, which is the whole point of running the feeds. Added
    # explicitly anyway for fuller coverage of each company's *own* postings,
    # not just whatever the aggregator happens to tag "new grad".
    {"name": "Serve Robotics",       "ats": "ashby",      "slug": "serverobotics", "robotics": True},
    {"name": "Corvus Robotics",      "ats": "ashby",      "slug": "corvus-robotics", "robotics": True},
    {"name": "Gradient Robotics",    "ats": "ashby",      "slug": "gradientrobotics", "robotics": True},
    {"name": "Allen Control Systems","ats": "ashby",      "slug": "allen-control-systems", "robotics": True},
    {"name": "FS Studio",            "ats": "ashby",      "slug": "fs-studio", "robotics": True},
    {"name": "Sunday Robotics",      "ats": "ashby",      "slug": "sunday", "robotics": True},
    {"name": "Torc Robotics",        "ats": "greenhouse", "slug": "torcrobotics", "robotics": True},
    {"name": "Path Robotics",        "ats": "greenhouse", "slug": "pathrobotics", "robotics": True},
    {"name": "Skild AI",             "ats": "greenhouse", "slug": "skildai-careers", "robotics": True},
    {"name": "Rocket Lab",           "ats": "greenhouse", "slug": "rocketlab", "robotics": True},
    {"name": "Avride",               "ats": "greenhouse", "slug": "avride", "robotics": True},
    # Second pass, added to widen the customer/technical-support net beyond
    # the original 15 -- every slug below was live-fetched and confirmed to
    # return real, current postings for that exact company (not a stub or a
    # name-collision board). Collisions caught and rejected this pass:
    # greenhouse "archer" is a veterinary clinic, not Archer Aviation;
    # greenhouse "kettle" is a marketing agency; ashby "notion" is the
    # productivity software company, not the IoT sensor startup; ashby "arlo"
    # is a healthcare company, not Arlo Technologies; lever "latch" is an
    # effectively-empty stub board.
    {"name": "Apptronik",             "ats": "greenhouse", "slug": "apptronik", "robotics": True},
    {"name": "Kodiak Robotics",       "ats": "greenhouse", "slug": "kodiak", "robotics": True},
    {"name": "Locus Robotics",        "ats": "greenhouse", "slug": "locusrobotics", "robotics": True},
    {"name": "Diligent Robotics",     "ats": "greenhouse", "slug": "diligentrobotics", "robotics": True},
    {"name": "Carbon Robotics",       "ats": "ashby",      "slug": "carbon-robotics", "robotics": True},
    {"name": "May Mobility",          "ats": "greenhouse", "slug": "maymobility", "robotics": True},
    {"name": "Skyryse",               "ats": "greenhouse", "slug": "skyryse", "robotics": True},
    {"name": "SimpliSafe",            "ats": "greenhouse", "slug": "simplisafe", "robotics": True},
    {"name": "Anduril Industries",    "ats": "greenhouse", "slug": "andurilindustries", "robotics": True},
    {"name": "Brain Corp",            "ats": "greenhouse", "slug": "braincorporation", "robotics": True},
    {"name": "Epirus",                "ats": "greenhouse", "slug": "epirus", "robotics": True},
    {"name": "Motional",              "ats": "greenhouse", "slug": "motional", "robotics": True},
    {"name": "Zipline",               "ats": "greenhouse", "slug": "flyzipline", "robotics": True},
    {"name": "Physical Intelligence", "ats": "ashby",      "slug": "physicalintelligence", "robotics": True},
    {"name": "1X Technologies",       "ats": "ashby",      "slug": "1x", "robotics": True},
    {"name": "Gecko Robotics",        "ats": "ashby",      "slug": "gecko-robotics", "robotics": True},
    {"name": "Hadrian",               "ats": "ashby",      "slug": "hadrian-automation", "robotics": True},
    {"name": "Eight Sleep",           "ats": "ashby",      "slug": "eightsleep", "robotics": True},
    {"name": "Applied Intuition",     "ats": "ashby",      "slug": "applied", "robotics": True},
    {"name": "Dexterity",             "ats": "lever",      "slug": "dexterity", "robotics": True},
    {"name": "Cobalt Robotics",       "ats": "lever",      "slug": "cobaltrobotics", "robotics": True},
    {"name": "Waabi",                 "ats": "lever",      "slug": "waabi", "robotics": True},
    {"name": "Boston Dynamics",       "ats": "workday", "tenant": "bostondynamics", "wd_host": "wd1", "site": "Boston_Dynamics", "robotics": True},

    # --- Tech consulting: for the Jobs 2.0 "tech consulting" category (see
    #     TITLE_INCLUDE_V2). Every slug below was live-fetched and confirmed
    #     to return real, current postings for that exact company. Most
    #     marquee strategy/Big-4 firms (McKinsey, Bain, BCG, Deloitte, PwC,
    #     EY, KPMG, Accenture) run proprietary applicant systems, not one of
    #     the 4 ATS platforms this scraper can read, and aren't here for
    #     that reason -- not an oversight. One name collision caught and
    #     rejected: greenhouse "bcg" is "Bohen Consulting Group" per its own
    #     API payload, not Boston Consulting Group. Crowe and RSM US are
    #     primarily audit/tax firms with a real but minority tech-consulting
    #     practice mixed into a much larger non-tech posting volume --
    #     included anyway since TITLE_INCLUDE_V2 only pulls the tech/
    #     consulting-analyst-titled postings out of that volume, same as
    #     everywhere else in this file. ---
    {"name": "ThoughtWorks",             "ats": "greenhouse", "slug": "thoughtworks"},
    {"name": "AlixPartners",             "ats": "greenhouse", "slug": "alixpartners"},
    {"name": "Charles River Associates", "ats": "greenhouse", "slug": "charlesriverassociates"},
    {"name": "West Monroe",              "ats": "greenhouse", "slug": "westmonroe4"},
    {"name": "Point B",                  "ats": "lever",      "slug": "pointb"},
    {"name": "Bounteous",                "ats": "lever",      "slug": "bounteous"},
    {"name": "Coalfire",                 "ats": "lever",      "slug": "coalfire"},
    {"name": "Guidehouse",       "ats": "workday", "tenant": "guidehouse",     "wd_host": "wd1",   "site": "External"},
    {"name": "Protiviti",        "ats": "workday", "tenant": "roberthalf",     "wd_host": "wd1",   "site": "ProtivitiNA"},
    {"name": "Crowe",            "ats": "workday", "tenant": "crowe",          "wd_host": "wd12",  "site": "External_Careers"},
    {"name": "RSM US",           "ats": "workday", "tenant": "rsm",            "wd_host": "wd1",   "site": "RSMCareers"},
    {"name": "Huron Consulting", "ats": "workday", "tenant": "huron",          "wd_host": "wd1",   "site": "huroncareers"},
    {"name": "FTI Consulting",   "ats": "workday", "tenant": "fticonsulting",  "wd_host": "wd108", "site": "FTIConsultingCareers"},
    {"name": "Baker Tilly",      "ats": "workday", "tenant": "bakertilly",     "wd_host": "wd5",   "site": "BTCareers"},
    {"name": "Booz Allen Hamilton", "ats": "workday", "tenant": "bah",         "wd_host": "wd1",   "site": "BAH_Jobs"},

    # --- Broad "search anything" feeds: maintained new-grad lists spanning
    #     hundreds of companies each (startups + big cos), with apply links.
    #     This is what casts the wide net; the per-company sources above add
    #     depth. Two independent feeds, not one -- confirmed live to overlap
    #     on barely 2% of active listings, so running both is close to double
    #     the unique companies, not a duplicate of the same list. ---
    {"name": "New-Grad Feed (Simplify)", "ats": "newgrad_feed",
     "feed_url": NEWGRAD_FEED_URL},
    {"name": "New-Grad Feed (vanshb03)", "ats": "newgrad_feed",
     "feed_url": NEWGRAD_FEED_URL_VANSHB03},

    # --- Curated early-career lists. These exist because a posting can be
    #     on a company's own board and still be missed (a long-lived APM
    #     program that ages out of a freshness window, or a board we don't
    #     scrape at all); a list that someone is actively maintaining for
    #     exactly this kind of role is a second, independent way to see it.
    #     apmlist.org: APM / new-grad product-management programs (open ones
    #     only). jobright.ai: three daily lists of roles posted in the last 7
    #     days -- product management, business analyst, and data analysis. ---
    {"name": "apmlist.org (APM New-Grad Roles)", "ats": "apmlist",
     "page": "https://apmlist.org/APM-New-Grad-Roles"},
    {"name": "Jobright (Product Management New Grad)", "ats": "jobright",
     "readme": "https://raw.githubusercontent.com/jobright-ai/2026-Product-Management-New-Grad/master/README.md"},
    {"name": "Jobright (Business Analyst New Grad)", "ats": "jobright",
     "readme": "https://raw.githubusercontent.com/jobright-ai/2026-Business-Analyst-New-Grad/master/README.md"},
    {"name": "Jobright (Data Analysis New Grad)", "ats": "jobright",
     "readme": "https://raw.githubusercontent.com/jobright-ai/2026-Data-Analysis-New-Grad/master/README.md"},

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
    {"name": "Comcast",    "ats": "workday", "tenant": "comcast",    "wd_host": "wd115", "site": "Comcast_Careers"},
    {"name": "Target",     "ats": "workday", "tenant": "target",     "wd_host": "wd5",  "site": "targetcareers"},
]

# Sources added in bulk live in sources_extra.json (same shape as the entries
# above) so this file stays readable: boards found by mining the real posting
# links in the new-grad feeds, plus the transportation / mobility / IoT /
# hardware-and-software companies. An entry already configured above (same
# ATS and board) is skipped.
def _load_extra_sources():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sources_extra.json")
    if not os.path.exists(path):
        # Running without it silently drops ~1,800 sources while looking
        # healthy (a blanket *.json .gitignore rule once did exactly that).
        import sys
        print(f"[WARN] {path} not found -- running with only the {len(COMPANIES)} "
              f"built-in sources", file=sys.stderr)
        return []
    with open(path, encoding="utf-8") as f:
        extra = json.load(f)

    def ident(c):
        if c["ats"] == "workday":
            return ("workday", c["tenant"].lower(), c.get("wd_host", "wd1"),
                    c["site"].lower(), c.get("wd_domain", ""))
        if c["ats"] == "oracle":
            return ("oracle", c["host"].lower(), c["site"].lower())
        return (c["ats"], (c.get("slug") or c.get("feed_url") or c.get("page")
                           or c.get("readme") or c["name"]).lower())

    have = {ident(c) for c in COMPANIES}
    out = []
    for c in extra:
        k = ident(c)
        if k not in have:
            have.add(k)
            out.append(c)
    return out


COMPANIES += _load_extra_sources()

# Every company tagged "robotics": True above, by name. Used by the Jobs 2.0
# "any role" exception (see classify_job) to recognize a robotics/IoT
# company's own postings regardless of source -- a direct per-company fetch
# (where the company is known from the COMPANIES entry itself) or a New-Grad
# Feed row that happens to name one of these companies as the employer.
ROBOTICS_COMPANY_NAMES = {c["name"] for c in COMPANIES if c.get("robotics")}

# Eightfold sites: the public careers host -> (API host, employer domain), so a
# posting URL alone is enough to fetch its description later.
_EIGHTFOLD_SITES = {}
_SF_HOSTS, _JAZZ_HOSTS = set(), set()
for _c in COMPANIES:
    if _c["ats"] == "sfcsb":
        _SF_HOSTS.add(re.match(r"https?://([^/]+)", _c["base"]).group(1))
    elif _c["ats"] == "jazzhr":
        _JAZZ_HOSTS.add(_c["host"])
    elif _c["ats"] == "eightfold":
        _EIGHTFOLD_SITES[_c.get("job_host", _c["host"])] = (_c["host"], _c["domain"])

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
    "associate project manager",
    "associate program manager",
    "customer success associate",
    "customer success specialist",
    "customer success coordinator",
    "customer success representative",
    "associate customer success manager",
    "junior customer success manager",
    # "customer support" titles are also scoped down separately in matches()
    # to only robotics/IoT companies -- see ROBOTICS_IOT_TERMS and
    # is_robotics_or_iot() below. Generic customer support (retail, telecom,
    # general SaaS) is out of scope; these titles still need to pass
    # TITLE_INCLUDE to get the free title/location gate and a description
    # fetch before that check can run.
    "customer support associate",
    "customer support specialist",
    "customer support coordinator",
    "customer support representative",
    # "Technical support" is the same robotics/IoT-scoped category as
    # "customer support" above, added after Simbe Robotics' "Technical
    # Support Analyst - Contract to Hire" -- a real posting that would
    # otherwise have been missed entirely (no "customer support" substring
    # in the title at all). "Analyst" is included here specifically because
    # that's the real title; not added to the customer-success/support list
    # above since it hasn't shown up on a customer success/support posting.
    "technical support analyst",
    "technical support associate",
    "technical support specialist",
    "technical support representative",
    "technical support coordinator",
]
# Note on "Associate Product ___": we don't list "associate product" on its own,
# because it also catches "Associate Product Engineer/Designer". "Associate
# Product Manager/Analyst/Owner" already match via the phrases above.
#
# Note on "associate project/program manager": deliberately NOT bare "project
# manager" / "program manager" -- those alone pull in a lot of roles that
# aren't entry level (construction PMs, "Program Manager IV") without saying
# "senior" anywhere in the title for TITLE_EXCLUDE_WORDS to catch. Requiring
# "associate" keeps this to the entry-level rotational-style roles you
# actually want, matching "technical program manager"/"tpm" already above.
#
# Note on "customer success"/"customer support": a second, separate category
# from product roles, opened up on request. Deliberately NOT bare "customer
# success manager" / "customer support manager": checked live and bare
# "Customer Success Manager" postings at GitLab, Harvey, and Airwallex were
# all real experienced-hire roles (own a book of accounts, lead a team,
# require specific technical or industry depth) with no number, "track
# record", or "experienced professional" phrase to catch them -- "Customer
# Success/Support Manager" is industry convention for an experienced-hire
# title, not an entry-level one, unlike "Product Manager" where "Associate
# Product Manager" is the well-established junior program name. The actual
# entry-level titles in this field are associate/specialist/coordinator/
# representative, so those are what's listed, matching the same "require the
# junior-signaling word" approach as "associate project manager" above.
# "Customer Success ___" carries no company restriction -- entry-level
# customer success is wanted anywhere. "Customer Support ___" is narrower:
# wanted only at a robotics or IoT company, not customer support generally.

# Titles matching one of these read as the core ask -- straight-up entry-level
# product management, analyst, owner, or ops work -- and are ranked above
# every other TITLE_INCLUDE match (technical program manager, associate
# project manager, associate program manager) when sorting the sheet and the
# email digest. All are still KEPT either way; this only affects ordering.
TITLE_INCLUDE_CORE = [
    "product manager",
    "associate product manager",
    "apm",
    "product management",
    "rotational product",
    "product analyst",
    "product operations",
    "product ops",
    "junior product manager",
    "product owner",
]

# A title is rejected if it contains any of these. Note: NO intern/co-op here,
# because you want grad-eligible internships kept.
TITLE_EXCLUDE_WORDS = [
    "senior", "sr.", "sr ", "staff", "principal", "lead", "director",
    "head of", "group product", "vp", "vice president", "manager iii",
    # "HBM Design Architect New Grad - MS or PhD": an advanced degree named in
    # the title itself (the description filters only catch the written-out
    # "Master's or PhD" form). And "Classified IT Asset & Procurement
    # Administrator": a cleared role named in the title, which the clearance
    # phrases in DESCRIPTION_EXCLUDE only catch if the body repeats it.
    "phd", "classified",
    # A C-suite title (Chief Information Security Officer, CTO, ...) has no
    # years-of-experience number for years_required() to catch, and neither
    # senior-signal detector caught it either -- confirmed live on a real
    # Zipline "Chief Information Security Officer" posting that slipped
    # through as a Jobs 2.0 match with no other signal blocking it at all.
    # This is a global fix (not scoped to any one category/fallback): no
    # legitimate entry-level posting is ever titled "Chief ___".
    "chief",
    # "Actuarial Opportunities - Pet Insurance Product Management" matches
    # TITLE_INCLUDE on "product management", but it's an actuarial role filed
    # under a Product Management department tag, not a product job. Checked
    # against every actuarial/actuary title currently seen: this is the only
    # one that would otherwise pass -- "Associate, Actuarial" and "Senior
    # Actuarial Analyst" never matched TITLE_INCLUDE in the first place.
    "actuarial",
    # Microsoft's own people-manager track for Program Management -- see
    # MICROSOFT_TITLE_INCLUDE below for why bare "program manager" is
    # accepted at all, scoped to Microsoft only. "senior"/"principal" above
    # already catch Microsoft's other senior PM titles.
    "group program manager",
    # Sales Engineering's people-manager track (TITLE_INCLUDE_V2 below) --
    # "senior"/"staff"/"principal"/"lead"/"director" above don't reliably
    # appear on this specific title.
    "sales engineering manager",
]

# Microsoft calls its product-management discipline "Program Manager", not
# "Product Manager" -- including for new-grad hires (e.g. "Program Manager,
# University Grad"). Bare "program manager" is deliberately NOT in
# TITLE_INCLUDE above: at every other company it pulls in a lot of
# non-entry-level roles that don't say "senior" in the title (see the note
# on "associate project/program manager" below TITLE_INCLUDE). So this is
# its own list, checked only when the job's company is literally "Microsoft"
# (passes_title() below) -- it can't affect matching for any other company.
MICROSOFT_TITLE_INCLUDE = ["program manager"]
# Level tokens rejected only as whole words (so "ii" won't hit "hawaii").
# "v" added after a live Boston Dynamics posting, "Facilities Maintenance
# Technician, V" -- caught this run only because its description happened to
# state a years-required number; a posting without one would have slipped
# through. No real job title uses a bare "V" for anything other than a level
# suffix, so this is a safe addition, not a risk of a new false-positive drop.
TITLE_EXCLUDE_TOKENS = {"ii", "iii", "iv", "v"}

# ===========================================================================
# Jobs 2.0: a second, broader category -- entry-level roles that bridge
# technical teams and business goals (sales engineering, business analysis,
# project coordination, QA, technical support) plus tech consulting analyst/
# associate-consultant tracks. Written to a separate sheet tab with its own
# email digest; see classify_job() for how a posting is routed to this
# category vs. the product/support one above -- a posting only ever lands in
# one tab, never both. TITLE_EXCLUDE_WORDS/TITLE_EXCLUDE_TOKENS above still
# apply to these (checked via passes_title_v2 below), so "Senior Business
# Analyst", "Business Analyst III", etc. are excluded the same way.
#
# Not scoped with "associate"/"junior" qualifiers the way TITLE_INCLUDE's
# product-adjacent titles are (associate project/program manager): unlike
# "program manager" or "project manager", none of these five are a
# well-established senior-IC or people-manager title elsewhere, so the bare
# term itself reads as entry-to-mid, and TITLE_EXCLUDE_WORDS above already
# drops the genuinely senior postings.
TITLE_INCLUDE_V2 = [
    "sales engineer",
    "sales engineering",
    "business analyst",
    "project coordinator",
    "quality assurance analyst",
    "qa analyst",
    # Tech consulting: entry-level rotational/analyst titles. Deliberately
    # not bare "consultant" -- that alone is used for senior/experienced
    # hires industry-wide with no qualifying word to catch on (unlike
    # "Associate Consultant" or "Consulting Analyst", which are the actual
    # entry-level titles at firms that run campus/new-grad programs).
    "technology consultant",
    "technical consultant",
    "associate consultant",
    "business technology analyst",
    "consulting analyst",
    # From a real accepted match at an untracked company ("Entry Level Data
    # Analyst - Business Analyst") -- kept bare since, like "Business
    # Analyst" above, it isn't a well-established senior title elsewhere.
    "data analyst",
]

# "Technical Support Specialist" is also in TITLE_INCLUDE above (part of the
# customer/technical-support cluster), but scoped there to robotics/IoT
# companies only. At a non-robotics company, that same exact phrase belongs
# in Jobs 2.0 instead of being dropped outright -- classify_job() checks
# this by name; it's called out here as its own constant rather than folded
# into TITLE_INCLUDE_V2 so that scoping logic stays visible in one place.
TECHNICAL_SUPPORT_SPECIALIST_V2 = "technical support specialist"

# The robotics "any role" exception (classify_job) turned out, in practice,
# to mean "any role" too literally -- checked live against a real batch of
# rejections at real robotics companies. Every title word below was a
# confirmed miss, not a guess:
#   manager     - Engineering Manager, Manager Logistics, Manager Technical
#                 Support, Aviation Regulatory Program Manager: a people-
#                 manager/leadership title with no reliable years-required
#                 number in its description for years_required() to catch.
#   engineer(ing) - Flight Test Engineer, Systems Engineer (Cybersecurity),
#                 Production Engineer, Perception Engineer, Forward
#                 Deployed Software Engineer: real engineering roles
#                 requiring a CS/engineering background this user does not
#                 have. Does not touch TITLE_INCLUDE_V2's "sales engineer"/
#                 "sales engineering" -- that's a separate, earlier-checked
#                 branch in classify_job, matched (or not) before this
#                 fallback is ever reached.
#   scientist   - Research Scientist: PhD-level qualifications.
#   technician, factory, mechanic, assembler - Robot Service Technician
#                 Assistant (14 near-duplicate postings alone), Autonomous
#                 Semi Technician, Factory Technician: hourly/no-degree
#                 roles ("doesn't need college diploma" was the recorded
#                 reason on every one of these).
#   recruiter, recruiting - Recruiting Coordinator: an explicit "don't want
#                 recruiting" rejection.
#   sales       - Sales Development Representative, seasonal Sales Agent:
#                 "sales and pay too low" / "straight sales don't want".
#                 Same non-interference with TITLE_INCLUDE_V2's "sales
#                 engineer" as "engineer" above.
#   warehouse, seasonal - the same shift-work/hourly shape as technician.
#   programmer, crane, rigging, machinist, welder, cnc - manufacturing-floor
#                 titles found live at Hadrian ("autonomous factories"): "CAM
#                 Programmer" and "Lift Specialist" (rigging cranes and heavy
#                 lift equipment, "hands-on, build-it-from-zero"), neither
#                 catchable by the words above since neither title contains
#                 "technician"/"factory"/etc.
#   account executive - Zipline "Enterprise Account Executive": a
#                 quota-carrying sales role not caught by bare "sales" since
#                 the title doesn't contain that word.
# Requiring an explicit "Bachelor's" mention in the description was tried
# first and rejected: a real accepted match (Motional's "Associate Vehicle
# Test Specialist") has no degree language in it at all, so that check
# would have dropped a job that works. Title-based exclusion of what this
# user has actually rejected is the more precise signal available so far.
# Only guards this one fallback in classify_job -- a title already accepted
# through passes_title/passes_title_v2/the technical-support-specialist
# carve-out (Product Manager, Sales Engineer, Associate Program Manager,
# TPM, etc.) is unaffected, matched via an earlier, independent check.
_ROBOTICS_ANY_ROLE_EXCLUDE_TITLE_RE = re.compile(
    r"\b(manager|engineer(?:ing)?|scientist|technician|factory|mechanic|"
    r"assembler|recruiters?|recruiting|sales|warehouse|seasonal|programmer|"
    # Added after the exception was extended to large industrial employers
    # (Avis, Caterpillar, Magna, GE, Cummins) and let through: summer interns
    # marked "Returning Students", GM's PhD-style "Researcher" roles, and
    # hourly plant/rental work. Interns/co-ops are NOT excluded here: roles
    # that don't require being in college are wanted, and the ones that do are
    # caught by the enrollment phrases in DESCRIPTION_EXCLUDE.
    r"apprentice\w*|trainee|researcher|operator|"
    r"driver|handler|inspector|detailer|cleaner|attendant|cashier|clerk|"
    # Seen in the second full-scale run: construction "Laborer", sanitation,
    # field installers, architect/developer (engineering by another name),
    # and recruiting by its HR names ("Talent Acquisition Specialist").
    r"laborer|labourer|worker|helper|custodian|janitor|sanitation|installers?|"
    r"installations?|architect|developer|talent acquisition|talent advisor|"
    r"talent partner|line specialist)\b",
    re.IGNORECASE,
)
# At a tagged (mobility / robotics / IoT / hardware) company, ANY non-excluded
# title used to qualify. That was sized for a ~40-company roster of small
# robotics startups; applied to ~190 companies including Avis, Caterpillar,
# Magna, GE and Cummins it let in production operators, car detailers, rental
# agents and drivers -- 72% of the Jobs 2.0 rows from those companies had no
# business-role word in the title at all. A title must now look like the kind
# of role this search is for: the shape of everything accepted so far
# (Business Analyst, Project Coordinator, Purchasing Agent, Associate Vehicle
# Test Specialist, Data Analyst), plus UX/design, since that's the background.
# Deliberately NOT "consultant": ADT's 16 "Smart Home Consultant" postings are
# in-home sales. Real consulting titles still match through TITLE_INCLUDE_V2
# (technology/technical/associate consultant), which has its own scoping.
# Early-career programs are always eligible (see _is_program_title).
_ANY_ROLE_ALLOW_RE = re.compile(
    r"\b(analyst|coordinator|specialist|planner|buyer|purchasing|procurement|"
    r"sourcing|program|project|strategy|strategic|business|logistics|"
    r"supply chain|financ\w*|marketing|account\w*|customer success|ux|designer|design|"
    r"analytics|insights|data)\b",
    re.IGNORECASE,
)


def _any_role_title_ok(title):
    # "Entry-Level - Physical Metallurgist" is a level, not a program, so
    # "entry level" alone must not unlock the any-role path (it did, and let
    # in GM's science-research roles); a real program word still does.
    without_level = re.sub(r"entry[- ]level", "", title or "", flags=re.IGNORECASE)
    return bool(_is_program_title(without_level) or _ANY_ROLE_ALLOW_RE.search(title or ""))
# These five are specific enough to also check against the description, not
# just the title -- Hadrian's "Lift Specialist" says none of this in its own
# title, only in the body ("critical lifts involving cranes and heavy
# rigging"). The broader words above stay title-only: checking THOSE against
# description text produced a real false exclusion live -- Motional's own
# accepted "Associate Vehicle Test Specialist" match mentions "provide
# feedback to engineering teams" in passing, which isn't the job itself
# being an engineering role. "crane"/"rigging"/"machinist"/"welder"/"cnc"
# don't have that everyday-collaboration-language problem.
_ROBOTICS_ANY_ROLE_EXCLUDE_ANYWHERE_RE = re.compile(
    r"\b(crane|rigging|machinist|welders?|cnc)\b", re.IGNORECASE)
# Phrases, not single words -- "part-time"/"part time" needs its own check
# since the hyphenated form isn't a clean \b-bounded word, and "account
# executive" doesn't contain any of the single words above. Same evidence
# base: every Avride/Diligent Robotics rejection had "part time" in the
# title; Zipline's "Enterprise Account Executive" is the account-executive
# case (see above). Title-only, same reasoning as the broad word list.
_ROBOTICS_ANY_ROLE_EXCLUDE_PHRASES = ("part time", "part-time", "account executive")
# Round-the-clock operational roles (Kodiak's "Operations Specialist" --
# "weekend and night shifts" -- and Zipline's "Visual Observer" -- "day,
# night, and weekend shifts", "operational readiness") have a
# professional-sounding title and no degree language at all, so nothing
# above catches them. Requiring BOTH a bare "shift(s)" mention AND a
# night/weekend/overnight word is a real, if imperfect, proxy for that
# specific shape of role; checked against Motional's own accepted match, a
# genuinely different case (a single incidental "shift" mention with
# neither word nearby) to make sure this doesn't just repeat that mistake.
_SHIFT_WORD_RE = re.compile(r"\bshifts?\b", re.IGNORECASE)
_ROUND_THE_CLOCK_RE = re.compile(
    r"\b(night|overnight|weekend|graveyard|swing shift)\b", re.IGNORECASE)


def _robotics_any_role_excluded(title, desc=""):
    if _ROBOTICS_ANY_ROLE_EXCLUDE_TITLE_RE.search(title):
        return True
    if any(p in title for p in _ROBOTICS_ANY_ROLE_EXCLUDE_PHRASES):
        return True
    text = f"{title} {desc}"
    if _ROBOTICS_ANY_ROLE_EXCLUDE_ANYWHERE_RE.search(text):
        return True
    return bool(_SHIFT_WORD_RE.search(text) and _ROUND_THE_CLOCK_RE.search(text))

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
    "returning student",
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
    # Security clearance requirement -- a hard disqualifier, not a
    # nice-to-have. "security clearance" alone catches most phrasings
    # ("SECRET Security Clearance Required", "must possess an active
    # security clearance", "eligible to obtain a security clearance");
    # the rest catch the ones that don't say "security" right next to it.
    "security clearance",
    "top secret",
    "ts/sci",
    "sci clearance",
    "clearance required",
    "clearance eligib",
    "active clearance",
    "polygraph",
    # An advanced degree stated as the actual bar, not a nice-to-have --
    # confirmed live on a Tower Research Capital "Data Analyst" posting's
    # own "Qualifications" bullet: "Master's or PhD in Computer Science,
    # Engineering, Mathematics, Statistics, Physics, Economics, or a
    # related quantitative discipline". Deliberately not bare "master's" or
    # "phd" -- those alone would also catch "Bachelor's required, Master's
    # a plus," which is a real Bachelor's-level posting.
    "master's or phd",
    "phd or master's",
    "master's degree or phd",
    # A prior internship the candidate would already need to have
    # completed, usually at this same company specifically -- confirmed
    # live on a CenturyLink/Lumen "Business Analyst" posting: "Must have
    # completed an internship within Data Analytics at Lumen Technologies."
    # Not open to an external new grad without that exact prior internship.
    "must have completed an internship",
]

# ---------------------------------------------------------------------------
# Early-career PROGRAMS (APM, new-grad, rotational, launch programs).
# ---------------------------------------------------------------------------
# A program role is exempt from three filters that were silently dropping the
# exact roles that matter most -- confirmed live against real misses:
#   * Staleness (MAX_POSTING_AGE_DAYS). Programs stay open for weeks or months
#     and keep their ORIGINAL posted date: Roblox's APM (posted Sept 2), Stripe's
#     "Product Manager: New Grad Accelerator" (posted March), and Databricks'
#     APM (first posted 2024, recycled every cycle) were all dropped as "stale"
#     while still open and accepting applications. The 14-day limit was meant
#     for ordinary roles; for a program the question is "is it open", and every
#     board here only lists open postings.
#   * "Currently enrolled" / "graduating between ..." language. Robinhood's APM
#     New Grad program said "Currently enrolled in a full-time, degree-seeking
#     program ... graduating between Winter 2026 and Summer 2027" -- standard
#     wording in a new-grad program's own posting, which also takes recent
#     graduates. The enrollment phrases below hit it, and so did the
#     graduation-window cutoff, so it never reached the sheet.
# Internships keep every one of these checks: an internship that requires you
# to be enrolled really does require it.
_PROGRAM_TITLE_RE = re.compile(
    r"\bassociate product manager\b|\bapm\b|\bnew[- ]grad\w*|\buniversity\b|"
    r"\bgraduate\b|\bearly[- ]career\b|\brotational\b|\brotation program\b|"
    r"\blaunch (?:graduate )?program\b|\bleadership (?:development )?program\b|"
    r"\baccelerator\b|\bcampus\b|\bclass of 20\d\d\b|\bentry[- ]level\b|"
    r"\bproduct management analyst program\b|\btraining program\b|\bgrad program\b|"
    r"\bassociate (?:development|rotational|leadership|early[- ]career) program\b",
    re.I)
_INTERN_TITLE_RE = re.compile(r"\bintern(?:ship)?s?\b|\bco-?op\b", re.I)


def _is_program_title(title):
    t = title or ""
    return bool(_PROGRAM_TITLE_RE.search(t)) and not _INTERN_TITLE_RE.search(t)


# The DESCRIPTION_EXCLUDE entries a program role is NOT held to (see above).
_ENROLLMENT_SOFT_PHRASES = frozenset({
    "currently enrolled", "must be enrolled", "actively enrolled",
    "will be enrolled", "enrolled throughout", "enrolled for the duration",
    "enrolled in a degree", "expected graduation", "expected to graduate",
    "anticipated graduation", "graduating in 2027", "graduating in 2028",
    "rising senior", "rising junior",
})


def _description_excluded(title, desc):
    """DESCRIPTION_EXCLUDE check, with the enrollment phrases waived for
    early-career programs (see _is_program_title)."""
    program = _is_program_title(title)
    for p in DESCRIPTION_EXCLUDE:
        if program and p in _ENROLLMENT_SOFT_PHRASES:
            continue
        if p in desc:
            return True
    return False


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

# Customer success/support commonly asks for 1-2 years even at the genuine
# entry-level tier -- unlike product APM programs, which are built to be
# 0-experience by design. Checked live after opening that category: every
# currently-open posting matching the entry-level titles (associate/
# specialist/coordinator/representative/analyst) wanted at least 1 year, so
# MAX_YEARS_EXPERIENCE=0 meant zero matches there. Raised specifically for
# this category on request; product roles keep the stricter ceiling above.
# See _years_ceiling_for().
MAX_YEARS_EXPERIENCE_SUPPORT = 2

# When you graduated, as (year, month). Roles that target a LATER graduation
# window aren't open to you: "you will graduate in Fall 2026 or Spring 2027"
# excludes a Spring 2026 grad, and "graduating in Fall 2027" is an internship
# for someone still two years from finishing.
#
# This is deliberately a date comparison and NOT a ban on internships. Plenty of
# internships and co-ops accept recent grads, and those keep whatever window
# they state, so they still come through. Set to None to disable.
GRADUATED = (2026, 5)

# Drop a role if its own posted date is older than this many days -- you want
# only genuinely new postings, not old ones sitting in the feed. Set to None
# to disable.
#
# This looks at a date the SOURCE supplied (Greenhouse's first_published,
# Ashby's publishedAt, Amazon's posted_date), or -- for a source whose API
# gives none at all (Workday; also the New-Grad Feed, whose "url" leads to
# the real company's own page) -- a "datePosted" pulled out of the fetched
# description's JSON-LD block, once one is available (see
# _extract_json_ld_date). Google and Microsoft still give nothing usable
# either way, and _norm_date() normalizes a missing/unparseable date to "",
# so this filter never touches those two, and they keep contributing every
# currently-open match regardless of age.
#
# Caveat worth knowing up front, from Known limits: even a "real" date is a
# first-posted date, not "currently open since." A company that recycles one
# requisition every hiring cycle never updates it, so a role that is
# genuinely open today can still read as older than this ceiling and get
# dropped here (the README's own Databricks "Summer 2027 internship posted
# 2023" example). That is a real, accepted trade for this filter existing at
# all: a handful of false drops on recycled listings, in exchange for the
# sheet not filling up with old reposts.
#
# Tightened from 45 to 14 on request ("I only want jobs just as they're
# posted for the first time and very recently") -- confirmed live this was
# too loose in practice: an Adobe Workday posting from ~80 days earlier was
# still getting surfaced, though that specific case also needed the JSON-LD
# date fallback above to be checked against ANY ceiling at all. Raise the
# number, or set to None, if the sheet starts looking too thin.
MAX_POSTING_AGE_DAYS = 14

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
# \d{1,2}(?:\.\d+)? also catches a decimal like "1.5 years" (seen live on a
# Datadog posting, "1.5 years as sales engineer") -- consider()'s float(tok)
# handles the non-digit-string case this introduces.
_NUM = r"(\d{1,2}(?:\.\d+)?|" + "|".join(_WORD_NUMBERS) + r")"
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
    # The qualifier can also come AFTER "years" instead of before it --
    # confirmed live on a Datadog posting: "Ideally 1.5 years or greater as
    # a Sales Engineer" (decimal handled by _NUM above). The pattern
    # directly above this one only catches "N+/or more years", not
    # "N years or more/greater".
    re.compile(rf"\b{_NUM}\s*(?:years?|yrs?)\s*(?:or more|or greater)\b", re.I),
    # A range contributes its FLOOR, so "1-3 years" reads as 1 and survives.
    re.compile(rf"\b{_NUM}\s*(?:-|–|—|to)\s*\d{{1,2}}\s*(?:years?|yrs?)\b", re.I),
    # "WORD (DIGIT)" convention: a spelled-out number immediately re-stated
    # as a digit in parentheses, sometimes with a trailing "+" inside the
    # parens too -- confirmed live on Guidehouse Workday postings:
    # "MINIMUM of THREE (3) years", "Two (2+) plus years", "FOUR (4) or
    # more years". The parenthetical breaks every pattern above's
    # assumption that the number sits immediately next to "years" or its
    # own qualifier; the digit inside the parens is captured directly and
    # is authoritative, so the spelled-out word never needs parsing.
    re.compile(
        rf"\b(?:{'|'.join(_WORD_NUMBERS)})\s*\(\s*(\d{{1,2}})\s*\+?\s*\)"
        rf"\s*(?:\+|or more|plus)?\s*(?:years?|yrs?)\b", re.I),
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
# "A valid driver's license is required with a minimum of ten (10) years of
# driving experience" / "maintain a clean driving record for the previous
# 10 years" -- a driving-record eligibility bar on a vehicle-testing role
# (Motional), not a work-experience requirement. Confirmed live: this read
# as a 10-year requirement and dropped an otherwise-genuinely-entry-level
# posting. "driving" right next to "experience"/"record" is specific enough
# not to also swallow a real requirement phrased as, say, "experience
# driving business outcomes."
_DRIVING_RECORD_CONTEXT = re.compile(r"\bdriving\s+(?:experience|record)\b", re.I)

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
    # Added after a live Zipline leak: "Abidjan, Côte d'Ivoire" alone
    # matched nothing here (a different posting's combined "...; Lagos,
    # Nigeria" string happened to catch it via "nigeria" instead, masking
    # the gap for the single-country postings). "côte d'ivoire" is listed
    # in both apostrophe styles since the source uses a curly one
    # ("Côte d’Ivoire") but a straight one is just as likely elsewhere;
    # "abidjan" alone is enough regardless of apostrophe/accent handling.
    # Ghana/Rwanda added too, from the same multi-country postings on this
    # board.
    "abidjan", "côte d'ivoire", "côte d’ivoire", "ivory coast",
    "ghana", "accra", "rwanda", "kigali",
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
# Jobs 2.0: the broader tech/business-bridge + tech-consulting + robotics-
# any-role category (see classify_job). Same sheet, same column layout
# (HEADER), a separate tab the user already created by hand -- same name
# used here must match that tab's actual name exactly (gspread lookup is
# case-sensitive).
WORKSHEET_NAME_V2 = "Jobs 2.0"
HEADER = ["Date added", "Date posted", "Priority", "Company", "Title", "Location",
          "URL", "Applied?"]
URL_COL = 7       # column G holds the URL (used for dedup)
PRIORITY_IDX = 2  # index of the Priority cell within a row
TITLE_IDX = 4     # index of the Title cell within a row
URL_IDX = 6       # index of the URL cell within a row

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
    # Some Greenhouse postings ship double- (or triple-) encoded content --
    # confirmed live on an Anduril posting whose raw API content contained
    # literal "&amp;nbsp;" -- a single html.unescape() only resolves the
    # outer "&amp;" -> "&", leaving "&nbsp;" behind as literal text rather
    # than the actual non-breaking-space character. That surviving "&nbsp;"
    # isn't whitespace to any of years_required()'s regexes, silently
    # breaking "Minimum of  2 – 4  years" into something
    # unmatchable. Looping until unescaping stabilizes handles any encoding
    # depth without needing to detect it; a normally-encoded (or plain)
    # string is unaffected since the second call is a no-op.
    text = raw
    for _ in range(3):
        unescaped = html.unescape(text)
        if unescaped == text:
            break
        text = unescaped
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


# Co-ops and internships stay open for weeks while a company fills a cohort
# (SharkNinja's co-op pools sit on the board for months), so they get a longer
# window than ordinary roles.
INTERN_MAX_POSTING_AGE_DAYS = 60


def is_stale(posted_norm, title=""):
    """True if a normalized YYYY-MM-DD posted date is older than
    MAX_POSTING_AGE_DAYS. A blank date -- the source gave nothing usable, or
    gives no date at all -- is never stale, since there's nothing to judge it
    against; see MAX_POSTING_AGE_DAYS for which sources that covers."""
    if MAX_POSTING_AGE_DAYS is None or not posted_norm:
        return False
    try:
        posted = datetime.date.fromisoformat(posted_norm)
    except ValueError:
        return False
    limit = INTERN_MAX_POSTING_AGE_DAYS if _INTERN_TITLE_RE.search(title or "") else MAX_POSTING_AGE_DAYS
    return (datetime.date.today() - posted).days > max(limit, MAX_POSTING_AGE_DAYS)


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
    r = _request_retry("GET", url, tries=3, timeout=40, headers=REQ_HEADERS)
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
    r = _request_retry("GET", url, tries=3, timeout=40, headers=REQ_HEADERS)
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
    r = _request_retry("GET", url, tries=3, timeout=40, headers=REQ_HEADERS)
    return [{
        "title": j.get("title", ""),
        "location": j.get("location", ""),
        "url": j.get("jobUrl", ""),
        "posted": j.get("publishedAt", ""),
        "description": _strip_html(j.get("descriptionPlain") or j.get("description", "")),
    } for j in r.json().get("jobs", [])]


# Early-career-flavored searches, used ON TOP of SEARCH_QUERIES by the sources
# that can only be searched rather than read in full (Amazon, Google,
# Microsoft, and any Workday board past its result window). The product-only
# SEARCH_QUERIES never surfaced things like Mastercard's "Launch Program"
# rotations, whose titles contain none of those four phrases.
EARLY_CAREER_QUERIES = [
    "associate product manager", "new grad", "university graduate",
    "early career", "rotational program", "launch program", "analyst",
    "business analyst", "associate", "specialist", "coordinator",
]
WORKDAY_PAGE = 20       # Workday rejects anything bigger with HTTP 400
WORKDAY_NEWEST = 400    # how many of a board's newest postings to read
WORKDAY_QUERY_PAGES = 2  # pages (x20) read for each early-career search


import threading
_HOST_GATES = {}
_HOST_GATES_LOCK = threading.Lock()


def _host_gate(url):
    """Cap simultaneous requests to one host. Workday and Oracle hosts belong
    to a single employer (Boeing has three Workday sites on one tenant; Intel,
    Northrop and Boeing got HTTP 429 in the full-scale run when their boards
    were hit at full speed), so those get a tight limit. API hosts shared by
    hundreds of employers (Greenhouse, Lever, Ashby...) get a loose one --
    a tight gate there would funnel 600 boards through three slots."""
    host = re.sub(r"^https?://", "", url).split("/")[0]
    limit = 3 if re.search(r"myworkday|oraclecloud", host) else 16
    with _HOST_GATES_LOCK:
        return _HOST_GATES.setdefault(host, threading.Semaphore(limit))


def _request_retry(method, url, tries=5, **kw):
    """Send a request, retrying 429/5xx/network failures with growing backoff
    (3s, 6s, 12s...). A big board takes dozens of requests, so one blip or one
    rate-limit must not sink the lot."""
    last = None
    for i in range(tries):
        try:
            with _host_gate(url):
                r = requests.request(method, url, **kw)
            if r.status_code in (429, 500, 502, 503, 504):
                last = requests.HTTPError(f"HTTP {r.status_code}")
                time.sleep(min(30, 3 * 2 ** i))
                continue
            r.raise_for_status()
            return r
        except requests.RequestException as e:
            last = e
            time.sleep(min(30, 3 * 2 ** i))
    raise last


def _post_json_retry(url, body, headers, tries=5):
    return _request_retry("POST", url, tries=tries, json=body, timeout=30, headers=headers).json()


def fetch_workday(c):
    """Read a Workday board: its newest WORKDAY_NEWEST postings, plus
    early-career searches across the whole board.

    This used to run four product-flavored searches and keep the first 60
    hits of each, which meant a role only appeared if its title/text matched
    "product manager", "product analyst", "technical program manager" or
    "product operations". Confirmed live: Mastercard's board holds 1,122
    postings and we saw 177 of them; its rotational "Launch Program" roles
    match none of those four searches, so they were invisible.

    Workday lists a board NEWEST FIRST (checked on Mastercard, Salesforce and
    Target: posting dates fall steadily with the offset), so a run that reads
    the top of the list sees everything posted recently -- and since the
    scraper runs hourly and remembers what it has seen, a posting only needs
    to be inside that window once. Small boards are simply read in full. For
    the long-lived early-career programs that sink below the newest window
    while still open, the early-career searches reach into the whole board.
    """
    tenant, host, site = c["tenant"], c.get("wd_host", "wd1"), c["site"]
    if c.get("wd_domain") == "myworkdaysite":
        # Some employers sit on Workday's shared myworkdaysite.com instead of
        # their own {tenant}.myworkdayjobs.com subdomain.
        base = f"https://{host}.myworkdaysite.com"
        link_prefix = f"{base}/en-US/recruiting/{tenant}/{site}"
    else:
        base = f"https://{tenant}.{host}.myworkdayjobs.com"
        link_prefix = f"{base}/en-US/{site}"
    api = f"{base}/wday/cxs/{tenant}/{site}/jobs"
    headers = {**REQ_HEADERS, "Content-Type": "application/json"}
    seen, out = set(), []

    def add(postings):
        for j in postings:
            path = j.get("externalPath", "")
            # No path means no real per-job URL -- confirmed live on a Boston
            # Dynamics posting that fell back to the bare
            # "https://bostondynamics.wd1.myworkdayjobs.com" domain and got
            # added to the sheet as a dead link. Skip it outright rather
            # than keep a row nobody can click through on.
            if not path or path in seen:
                continue
            seen.add(path)
            out.append({
                "title": j.get("title", ""),
                "location": j.get("locationsText", ""),
                "url": f"{link_prefix}{path}",
                # Workday only gives relative text ("Posted 5 Days Ago"),
                # which won't normalize to a real date, so leave it blank --
                # see _extract_json_ld_date() for the fallback main() applies
                # once the description is fetched.
                "posted": "",
                # Workday listing carries no description; title filter only.
                "description": "",
            })

    def crawl(search_text, limit):
        first = _post_json_retry(api, {"appliedFacets": {}, "limit": WORKDAY_PAGE,
                                       "offset": 0, "searchText": search_text}, headers)
        total = first.get("total", 0) or 0  # only reported on the first page
        add(first.get("jobPostings", []))
        for offset in range(WORKDAY_PAGE, min(total, limit), WORKDAY_PAGE):
            try:
                page = _post_json_retry(api, {"appliedFacets": {}, "limit": WORKDAY_PAGE,
                                              "offset": offset, "searchText": search_text}, headers)
            except Exception as e:
                # Keep what was read rather than lose the whole board.
                print(f"[warn] {c['name']} workday: stopped at offset {offset}: {e}")
                break
            postings = page.get("jobPostings", [])
            if not postings:
                break
            add(postings)
        return total

    total = crawl("", WORKDAY_NEWEST)
    if total > WORKDAY_NEWEST:
        for q in SEARCH_QUERIES + EARLY_CAREER_QUERIES:
            crawl(q, WORKDAY_QUERY_PAGES * WORKDAY_PAGE)
    return out


AMAZON_PAGE = 100          # the API's max page size
AMAZON_MAX_PER_QUERY = 500  # stop paging a single search after this many hits


def fetch_amazon(c):
    """US roles from Amazon's search API, paged to the end of each search.

    Previously this took ONE page of 100 per search, so any search with more
    than 100 hits (a plain "product manager" has ~500) silently dropped the
    rest, and nothing early-career-flavored was searched at all. country=USA
    keeps it to roles you can actually take; offset pages through the hits.
    """
    out, seen = [], set()
    for q in SEARCH_QUERIES + EARLY_CAREER_QUERIES:
        offset = 0
        while offset < AMAZON_MAX_PER_QUERY:
            r = requests.get("https://www.amazon.jobs/en/search.json",
                             params={"base_query": q, "result_limit": AMAZON_PAGE,
                                     "sort": "recent", "country": "USA",
                                     "offset": offset},
                             timeout=25, headers=REQ_HEADERS)
            r.raise_for_status()
            data = r.json()
            jobs = data.get("jobs", [])
            if not jobs:
                break
            for j in jobs:
                path = j.get("job_path") or ""
                if not path or path in seen:
                    continue
                seen.add(path)
                out.append({
                    "title": j.get("title", ""),
                    "location": j.get("normalized_location") or j.get("location", ""),
                    "url": "https://www.amazon.jobs" + path,
                    "posted": j.get("posted_date", ""),
                    "description": _join(_strip_html(j.get("description", "")),
                                         _strip_html(j.get("basic_qualifications", "")),
                                         _strip_html(j.get("preferred_qualifications", ""))),
                })
            offset += AMAZON_PAGE
            if offset >= (data.get("hits") or 0):
                break
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
    # Each search is restricted to US roles. The first is Google's own
    # "early career" level filter with no keyword at all: there are only ~120
    # such roles in the US, so every one of them is read instead of hoping a
    # keyword search happens to rank the APM program onto page 1-5 (it did
    # not -- the US "Associate Product Manager" program was never fetched).
    plans = [({"target_level": "EARLY", "location": "United States"}, 10)]
    plans += [({"q": q, "location": "United States"}, GOOGLE_MAX_PAGES)
              for q in SEARCH_QUERIES + EARLY_CAREER_QUERIES]
    for base_params, max_pages in plans:
        for page in range(1, max_pages + 1):
            r = requests.get(GOOGLE_RESULTS_URL, params={**base_params, "page": page},
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


MS_CAREERS = "https://apply.careers.microsoft.com"
MS_MAX_PER_QUERY = 300


def fetch_microsoft(c):
    """US roles from Microsoft's current careers search (an Eightfold site).

    The old gcsservices.careers.microsoft.com endpoint serves a certificate for
    the wrong hostname, so every request failed TLS verification -- a
    misconfiguration only Microsoft can fix, and one not worth working around
    by switching verification off. Their careers site has since moved to
    apply.careers.microsoft.com, whose JSON search answers normally once a
    session cookie has been picked up from the main page (without one it
    returns 429/403, which is why an earlier attempt concluded it was
    closed). Search results carry no description; _fetch_description reads it
    per posting from the detail endpoint, only for roles that pass the title
    gate.

    filter_seniority=Entry is Microsoft's own entry-level facet -- the ~45 US
    roles it holds are read in full, then the product and early-career
    searches run on top of that.
    """
    s = requests.Session()
    s.headers.update({**REQ_HEADERS, "Accept": "application/json",
                      "Referer": MS_CAREERS + "/careers"})
    s.get(MS_CAREERS + "/careers", timeout=25)
    out, seen = [], set()
    plans = [{"filter_seniority": "Entry"}]
    plans += [{"query": q} for q in SEARCH_QUERIES + EARLY_CAREER_QUERIES]
    for extra in plans:
        start = 0
        while start < MS_MAX_PER_QUERY:
            data = None
            for attempt in range(3):
                r = s.get(MS_CAREERS + "/api/pcsx/search",
                          params={"domain": "microsoft.com", "location": "United States",
                                  "start": start, **extra}, timeout=25)
                if r.status_code == 429:
                    time.sleep(2 * (attempt + 1))
                    continue
                r.raise_for_status()
                data = (r.json().get("data") or {})
                break
            if data is None:
                raise requests.HTTPError("Microsoft search kept answering 429")
            positions = data.get("positions") or []
            if not positions:
                break
            for p in positions:
                pid = p.get("id")
                if not pid or pid in seen:
                    continue
                seen.add(pid)
                locs = p.get("standardizedLocations") or p.get("locations") or []
                out.append({
                    "title": p.get("name", ""),
                    "location": "; ".join(locs[:3]),
                    "url": f"{MS_CAREERS}/careers/job/{pid}",
                    "posted": p.get("postedTs", ""),  # epoch seconds
                    "description": "",
                })
            start += len(positions)
            if start >= (data.get("count") or 0):
                break
            time.sleep(0.25)
    return out


def fetch_newgrad_feed(c):
    """A maintained new-grad job list across hundreds of companies. Carries no
    description, so title/location filters do the work here (fine, since the
    whole feed is new-grad full-time by construction). c["feed_url"] picks
    which maintained list to read; see NEWGRAD_FEED_URL_VANSHB03 above."""
    r = requests.get(c.get("feed_url", NEWGRAD_FEED_URL), timeout=60, headers=REQ_HEADERS)
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


ORACLE_NEWEST = 600   # newest postings read per Oracle careers site
ORACLE_PAGE = 200     # the API accepts at least this many per request


def fetch_oracle(c):
    """Oracle HCM "Candidate Experience" careers sites (c["host"], c["site"]).

    A large share of big employers (JPMorgan, Honeywell, Cummins, Amex, ...)
    run their careers pages on Oracle, which none of the other adapters can
    read. The public requisitions API sorts newest-first and returns each
    posting's qualifications and responsibilities inline, so there is no
    per-posting request and the experience filters have real text to read.
    Non-US postings are dropped here (the API reports a country code), since
    most of a global employer's newest postings are overseas. The list carries
    no qualifications; _fetch_oracle_description reads them per posting.
    """
    host, site = c["host"], c["site"]
    api = f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
    out, offset = [], 0
    while offset < ORACLE_NEWEST:
        r = requests.get(api, params={
            "onlyData": "true",
            "expand": "requisitionList.secondaryLocations",
            "finder": f"findReqs;siteNumber={site},limit={ORACLE_PAGE},offset={offset},"
                      f"sortBy=POSTING_DATES_DESC"},
            timeout=45, headers={**REQ_HEADERS, "Accept": "application/json"})
        r.raise_for_status()
        item = (r.json().get("items") or [{}])[0]
        reqs = item.get("requisitionList") or []
        if not reqs:
            break
        for q in reqs:
            country = q.get("PrimaryLocationCountry")
            if country and country != "US":
                continue
            out.append({
                "title": q.get("Title", ""),
                "location": q.get("PrimaryLocation", ""),
                "url": f"https://{host}/hcmUI/CandidateExperience/en/sites/{site}/job/{q['Id']}",
                "posted": q.get("PostedDate", ""),
                # The list view leaves the qualification fields empty (checked on
                # Amex); the real text comes from the per-posting detail call in
                # _fetch_description, only for roles that pass the title gate.
                "description": "",
            })
        offset += ORACLE_PAGE
        if offset >= (item.get("TotalJobsCount") or 0):
            break
    return out


SR_NEWEST = 500  # newest postings read per SmartRecruiters company


def fetch_smartrecruiters(c):
    """SmartRecruiters public postings API (c["slug"] is the company
    identifier, which is case-sensitive and sometimes suffixed -- "BoschGroup",
    "Ubisoft2"). US roles only. The list carries no description;
    _fetch_description reads it per posting, for roles that pass the title gate."""
    out, offset, page = [], 0, 100
    while True:
        r = requests.get(f"https://api.smartrecruiters.com/v1/companies/{c['slug']}/postings",
                         params={"limit": page, "offset": offset, "country": "us"},
                         timeout=25, headers=REQ_HEADERS)
        r.raise_for_status()
        data = r.json()
        content = data.get("content", [])
        if not content:
            break
        for p in content:
            loc = p.get("location") or {}
            out.append({
                "title": p.get("name", ""),
                "location": loc.get("fullLocation")
                            or ", ".join(x for x in (loc.get("city"), loc.get("region")) if x),
                "url": f"https://jobs.smartrecruiters.com/{c['slug']}/{p['id']}",
                "posted": p.get("releasedDate", ""),
                "description": "",
            })
        offset += page
        # Newest first (checked on ServiceNow and Bosch), so the top of the
        # list is everything recent; the hourly run only needs to see a
        # posting once.
        if offset >= min(data.get("totalFound") or 0, SR_NEWEST):
            break
    return out


_ORACLE_URL_RE = re.compile(
    r"https?://([a-z0-9.-]+\.oraclecloud\.com)/hcmUI/CandidateExperience/[^/]+/sites/([A-Za-z0-9_]+)/job/(\d+)")


def _fetch_oracle_description(host, site, req_id):
    r = requests.get(f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails",
                     params={"expand": "all", "onlyData": "true",
                             "finder": f'ById;Id="{req_id}",siteNumber={site}'},
                     timeout=30, headers={**REQ_HEADERS, "Accept": "application/json"})
    r.raise_for_status()
    d = (r.json().get("items") or [{}])[0]
    return _join(_strip_html(d.get("ExternalDescriptionStr", "")),
                 _strip_html(d.get("ExternalResponsibilitiesStr", "")),
                 _strip_html(d.get("ExternalQualificationsStr", "")))


_SR_URL_RE = re.compile(r"jobs\.smartrecruiters\.com/([^/?#]+)/(\d+)")


def _fetch_smartrecruiters_description(company, posting_id):
    r = requests.get(f"https://api.smartrecruiters.com/v1/companies/{company}/postings/{posting_id}",
                     timeout=20, headers=REQ_HEADERS)
    r.raise_for_status()
    sections = ((r.json().get("jobAd") or {}).get("sections") or {})
    return _join(*[_strip_html((sections.get(k) or {}).get("text", ""))
                   for k in ("jobDescription", "qualifications", "additionalInformation")])


def fetch_workable(c):
    """Workable's public widget API (c["slug"] is the account). The list is
    cheap; the full text (including the separate "requirements" field, where
    clearance and experience bars tend to live) is read per posting by
    _fetch_workable_description, for roles that pass the title gate."""
    r = requests.get(f"https://apply.workable.com/api/v1/widget/accounts/{c['slug']}",
                     params={"details": "true"}, timeout=25, headers=REQ_HEADERS)
    r.raise_for_status()
    out = []
    for j in r.json().get("jobs", []):
        if not j.get("shortcode"):
            continue
        loc = ", ".join(x for x in (j.get("city"), j.get("state"), j.get("country")) if x)
        out.append({
            "title": j.get("title", ""),
            "location": loc,
            "url": f"https://apply.workable.com/{c['slug']}/j/{j['shortcode']}/",
            "posted": j.get("published_on") or j.get("created_at") or "",
            "description": "",
        })
    return out


# ---------------------------------------------------------------------------
# Curated early-career lists (aggregators)
# ---------------------------------------------------------------------------
_RSC_CHUNK_RE = re.compile(r'self\.__next_f\.push\(\[1,"(.*?)"\]\)</script>', re.S)
_RSC_JOB_RE = re.compile(r'\{"id":"([0-9a-f-]{36})","title":"')


def fetch_apmlist(c):
    """apmlist.org's APM / new-grad product-management programs (c["page"]).

    A Next.js page with its data embedded in the HTML, built on Simplify's
    database. It lists every program it has ever tracked (closed ones too), so
    only entries flagged active AND visible are kept -- about 25 at a time.
    Each entry's link is a Simplify click-through that redirects to the
    employer's own posting; that real URL is what lands in the sheet, so the
    same job found directly on the company's board is recognized as a
    duplicate. The requirement/responsibility bullets stand in for a
    description so the experience filters have something to read.
    """
    r = requests.get(c["page"], timeout=60, headers=REQ_HEADERS)
    r.raise_for_status()
    blob = "".join(_RSC_CHUNK_RE.findall(r.text))
    blob = blob.encode("utf-8").decode("unicode_escape", errors="ignore")
    dec = json.JSONDecoder()
    out, seen, parsed = [], set(), 0
    for m in _RSC_JOB_RE.finditer(blob):
        try:
            o, _ = dec.raw_decode(blob[m.start():])
        except ValueError:
            continue
        parsed += 1
        if o.get("id") in seen or not (o.get("active") and o.get("visible")):
            continue
        seen.add(o.get("id"))
        company = (((o.get("job") or {}).get("company")) or {}).get("name", "")
        click = o.get("url") or ""
        real = click
        try:
            rr = requests.get(click, allow_redirects=False, timeout=20, headers=REQ_HEADERS)
            if rr.status_code in (301, 302, 303, 307, 308) and rr.headers.get("location"):
                real = rr.headers["location"]
        except requests.RequestException:
            pass
        # Drop the tracking tail so the link reads (and dedups) as the employer's own.
        real = re.sub(r"[?&]utm_source=Simplify\b.*$", "", real)
        locs = [l.get("value", "") for l in (o.get("locations") or [])]
        # mojibake in titles ("â" for an en dash) comes from the page's own encoding
        title = o.get("title", "").replace("\u00e2\u0080\u0093", "-").replace("\u00e2\u0080\u0094", "-")
        desc = _join(" ".join(o.get("subtitles") or []),
                     " ".join(o.get("requirements") or []),
                     " ".join(o.get("responsibilities") or []))
        out.append({
            "title": title,
            "location": "; ".join(l for l in locs if l),
            "url": real,
            "posted": o.get("start_date", ""),
            "description": desc,
            "company": company,
        })
    # The page tracks hundreds of programs, closed ones included. Parsing none
    # at all means the page's format changed, not that nothing is open -- say
    # so (it lands in the failed-sources report) rather than quietly return [].
    if not parsed:
        raise RuntimeError("apmlist: no program records found -- the page format may have changed")
    return out


_JR_ROW_RE = re.compile(
    r"^\|\s*(?P<company>.+?)\s*\|\s*\*\*\[(?P<title>.+?)\]\((?P<url>https://jobright\.ai/jobs/info/[0-9a-f]+)[^)]*\)\*\*"
    r"\s*\|\s*(?P<loc>.*?)\s*\|\s*(?P<model>.*?)\s*\|\s*(?P<date>.*?)\s*\|\s*$")
_JR_COMPANY_RE = re.compile(r"\*\*\[(?P<name>.+?)\]")


def _jobright_posted(text):
    """"Oct 20" -> an ISO date. The table gives no year, so a date that would
    land in the future must belong to the previous year."""
    try:
        d = datetime.datetime.strptime(text.strip(), "%b %d").date()
    except ValueError:
        return ""
    today = datetime.date.today()
    d = d.replace(year=today.year)
    if d > today:
        d = d.replace(year=today.year - 1)
    return d.isoformat()


def fetch_jobright(c):
    """A jobright.ai daily new-grad list (c["readme"] is the raw README): a
    markdown table of roles posted in the last 7 days. The row's link is
    jobright's own page for the role (it exposes no direct employer link);
    _fetch_description reads qualifications and minimum years from that page.
    A "↳" company cell means "same company as the row above"."""
    r = requests.get(c["readme"], timeout=60, headers=REQ_HEADERS)
    r.raise_for_status()
    out, company = [], ""
    for line in r.text.splitlines():
        m = _JR_ROW_RE.match(line)
        if not m:
            continue
        cm = _JR_COMPANY_RE.search(m.group("company"))
        if cm:
            company = cm.group("name")
        out.append({
            "title": m.group("title"),
            "location": m.group("loc"),
            "url": m.group("url"),
            # Deliberately blank: jobright lists a role when IT finds it, which can
            # be days after the employer posted it. A real date comes from the
            # employer's own posting when main() can match one (see
            # _resolve_direct_link); otherwise the date is left unknown.
            "posted": "",
            "description": "",
            "company": company,
        })
    if not out:
        raise RuntimeError("jobright: no rows parsed from the README -- its table format may have changed")
    return out


_JOBRIGHT_URL_RE = re.compile(r"jobright\.ai/jobs/info/([0-9a-f]+)")
_NEXT_DATA_RE = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)


def _fetch_jobright_description(url):
    r = requests.get(url, timeout=25, headers=REQ_HEADERS)
    r.raise_for_status()
    m = _NEXT_DATA_RE.search(r.text)
    data = json.loads(m.group(1))["props"]["pageProps"]["dataSource"]
    job = data["jobResult"]
    _JOBRIGHT_COMPANY_URL[url] = ((data.get("companyResult") or {}).get("companyURL") or "")
    qual = job.get("qualifications") or {}
    seniority = job.get("jobSeniority") or ""
    return _join(
        job.get("jobSummary", ""),
        ("Seniority: " + seniority + ".") if seniority else "",
        "Responsibilities: " + " ".join(job.get("coreResponsibilities") or []),
        "Qualifications: " + " ".join(qual.get("mustHave") or []),
        "Preferred: " + " ".join(qual.get("preferredHave") or []),
    )


# ---------------------------------------------------------------------------
# Automaker / mobility careers systems. Each was checked live against the real
# company site (Honda, Hyundai, Kia, Stellantis, Mercedes-Benz, Porsche, PACCAR,
# Harley, VW, Volvo, Rivian, AMD, Garmin, Qualcomm, Deere, Joby, Ouster). All are
# public with an ordinary User-Agent; none needs a cookie or a browser.
# ---------------------------------------------------------------------------
BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}
_US_LOC_RE = re.compile(r",\s*(?:US|USA|United States)\b|\bUnited States\b", re.I)


def fetch_jibe(c):
    """Jibe (iCIMS) career sites -- AMD, Garmin, Navistar, Rivian
    (c["host"]). One JSON call per page carries the full description,
    qualifications and responsibilities, so no per-posting request is needed."""
    host = c["host"]
    path = c.get("job_path", "/jobs")
    out, page, got = [], 1, 0
    while True:
        r = _request_retry("GET", f"https://{host}/api/jobs", tries=3, timeout=40,
                           params={"page": page, "limit": 100, "country": "United States"},
                           headers=REQ_HEADERS)
        d = r.json()
        jobs = d.get("jobs") or []
        if not jobs:
            break
        got += len(jobs)
        for j in jobs:
            x = j.get("data") or {}
            if x.get("country_code") and x["country_code"] != "US":
                continue
            out.append({
                "title": x.get("title", ""),
                "location": x.get("full_location") or "",
                "url": f"https://{host}{path}/{x.get('slug') or x.get('req_id')}?lang=en-us",
                "posted": x.get("posted_date", ""),
                "description": _join(_strip_html(x.get("description", "")),
                                     _strip_html(x.get("responsibilities", "")),
                                     _strip_html(x.get("qualifications", ""))),
            })
        page += 1
        if got >= (d.get("totalCount") or 0) or page > 30:
            break
    return out




def fetch_eightfold(c):
    """Eightfold "PCSX" career sites -- Qualcomm, Deere (c["host"] is the
    tenant's eightfold.ai host, c["domain"] the employer domain, c["job_host"]
    the public careers host). Page size is fixed at 10; the description comes
    from the position_details call in _fetch_description."""
    host, domain = c["host"], c["domain"]
    job_host = c.get("job_host", host)
    out, start = [], 0
    while True:
        r = _request_retry("GET", f"https://{host}/api/pcsx/search", tries=3, timeout=40,
                           params={"domain": domain, "query": "", "location": "United States",
                                   "start": start, "sort_by": "timestamp"},
                           headers=REQ_HEADERS)
        d = (r.json().get("data") or {})
        pos = d.get("positions") or []
        if not pos:
            break
        for p in pos:
            locs = p.get("standardizedLocations") or p.get("locations") or []
            loc = "; ".join(locs)
            if locs and not any(re.search(r"(?:,|\b)\s*US\b|United States", l) for l in locs):
                continue
            out.append({
                "title": p.get("name", ""),
                "location": loc,
                "url": f"https://{job_host}/careers/job/{p['id']}",
                "posted": p.get("postedTs") or "",
                "description": "",
            })
        start += len(pos)
        if start >= (d.get("count") or 0) or start >= 1500:
            break
    return out


_EIGHTFOLD_URL_RE = re.compile(r"https?://([^/]+)/careers/job/(\d+)")


def _fetch_eightfold_description(host, position_id):
    api_host, domain = _EIGHTFOLD_SITES[host]
    r = requests.get(f"https://{api_host}/api/pcsx/position_details",
                     params={"position_id": position_id, "domain": domain, "hl": "en"},
                     timeout=30, headers=REQ_HEADERS)
    r.raise_for_status()
    return _strip_html((r.json().get("data") or {}).get("jobDescription", ""))


_SF_ROW_RE = re.compile(r'<tr class="data-row">(.*?)</tr>', re.S)
_SF_TITLE_RE = re.compile(r'<a href="([^"]+)" class="jobTitle-link">(.*?)</a>', re.S)
_SF_LOC_RE = re.compile(r'<span class="jobLocation">\s*(.*?)\s*</span>', re.S)
_SF_DATE_RE = re.compile(r'<span class="jobDate">\s*(.*?)\s*</span>', re.S)
_SF_TOTAL_RES = (re.compile(r"of\s*<b>(\d+)"), re.compile(r"Results\s+\d+\s+to\s+\d+\s+of\s+(\d+)"))


def fetch_sfcsb(c):
    """SAP SuccessFactors "career site builder" pages -- Hyundai, Kia, PACCAR,
    Harley-Davidson, VW Group, Volvo (c["base"]). Server-rendered HTML with no
    JSON API; the root path lists every sub-site of a group at once. US rows
    only (the location cell reads "City, ST, US, zip"). The posting page holds
    the full text, which _fetch_description reads."""
    base = c["base"].rstrip("/")
    origin = re.match(r"https?://[^/]+", base).group(0)
    out, start, total = [], 0, None
    while True:
        r = _request_retry("GET", f"{base}/search/", tries=3, timeout=40,
                           params={"q": "", "startrow": start, "sortColumn": "referencedate",
                                   "sortDirection": "desc",
                                   **({"locationsearch": c["locationsearch"]} if c.get("locationsearch") else {})},
                           headers=BROWSER_HEADERS)
        t = r.text
        if total is None:
            for rx in _SF_TOTAL_RES:
                m = rx.search(t)
                if m:
                    total = int(m.group(1))
                    break
            total = total or 0
        rows = _SF_ROW_RE.findall(t)
        if not rows:
            break
        for row in rows:
            m = _SF_TITLE_RE.search(row)
            if not m:
                continue
            loc = _SF_LOC_RE.search(row)
            loc = " ".join(html.unescape(loc.group(1)).split()) if loc else ""
            if not re.search(r",\s*US\b", loc):
                continue
            dt = _SF_DATE_RE.search(row)
            out.append({
                "title": _strip_html(m.group(2)),
                "location": loc,
                "url": origin + html.unescape(m.group(1)),
                "posted": dt.group(1).strip() if dt else "",
                "description": "",
            })
        start += len(rows)
        if start >= total or start >= 1500:
            break
    return out


def fetch_phenom(c):
    """Phenom People career sites -- Honda (c["base"]). A POST to /widgets
    returns the search page as JSON; US-only because the request is scoped to
    the US site. The description comes from the job-detail widget, which also
    carries the posting's stated years of experience."""
    base = c["base"].rstrip("/")
    out, frm, size = [], 0, 50
    while True:
        body = {"lang": "en_us", "deviceType": "desktop", "country": "us",
                "pageName": "search-results", "ddoKey": "refineSearch", "sortBy": "",
                "subsearch": "", "from": frm, "jobs": True, "counts": True,
                "all_fields": ["category", "country", "state", "city", "type"],
                "size": size, "clearAll": False, "jdsource": "facets",
                "isSliderEnable": False, "pageId": "page4", "siteType": "external",
                "keywords": "", "global": True, "selected_fields": {},
                "sort": {"order": "desc", "field": "postedDate"}, "locationData": {}}
        d = (_post_json_retry(f"{base}/widgets", body, {**BROWSER_HEADERS, "Content-Type": "application/json"})
             .get("refineSearch") or {})
        jobs = (d.get("data") or {}).get("jobs") or []
        if not jobs:
            break
        for j in jobs:
            slug = re.sub(r"[^A-Za-z0-9]+", "-", j.get("title", "")).strip("-")
            out.append({
                "title": j.get("title", ""),
                "location": j.get("location") or j.get("locationName") or "",
                "url": f"{base}/us/en/job/{j['jobSeqNo']}/{slug}",
                "posted": j.get("postedDate", ""),
                "description": "",
            })
        frm += len(jobs)
        if frm >= (d.get("totalHits") or 0):
            break
    return out


_PHENOM_URL_RE = re.compile(r"(https://careers\.honda\.com)/us/en/job/([A-Za-z0-9_-]+)/")


def _fetch_phenom_description(base, job_id):
    body = {"lang": "en_us", "deviceType": "desktop", "country": "us", "pageName": "job-detail",
            "ddoKey": "jobDetail", "jobId": job_id, "pageId": "page11", "siteType": "external",
            "global": True}
    r = _post_json_retry(f"{base}/widgets", body, {**BROWSER_HEADERS, "Content-Type": "application/json"})
    job = ((r.get("jobDetail") or {}).get("data") or {}).get("job") or {}
    yrs = job.get("yearsExpReqd")
    return _join(_strip_html(job.get("description", "")),
                 ("Experience required: " + yrs + ".") if yrs else "")


STELLANTIS_ORG = "companies/16115603-6c1b-4c45-b544-238a4e6c51b3"


def fetch_mcloud(c):
    """m-cloud Google-Jobs API behind careers.stellantis.com (FCA US, Chrysler,
    Jeep, Dodge, Ram). Needs the careers site as Origin/Referer. The response
    holds the full text, so there is no detail call."""
    site = c["site"]
    h = {**BROWSER_HEADERS, "Origin": site, "Referer": site + "/"}
    out, off = [], 0
    while True:
        r = _request_retry("GET", "https://jobsapi-google.m-cloud.io/api/job/search", tries=3,
                           timeout=40, headers=h,
                           params={"companyName": c.get("org", STELLANTIS_ORG), "pageSize": 100,
                                   "offset": off, "sortfield": "open_date",
                                   "locationFilter": json.dumps({"address": "United States"})})
        d = r.json()
        rs = [s["job"] for s in d.get("searchResults") or []]
        if not rs:
            break
        for j in rs:
            if j.get("primary_country") and j["primary_country"] != "US":
                continue
            out.append({
                "title": j.get("title", ""),
                "location": ", ".join(x for x in (j.get("primary_city"), j.get("primary_state")) if x),
                "url": j.get("url", ""),
                "posted": j.get("open_date", ""),
                "description": _join(_strip_html(j.get("description", "")),
                                     _strip_html(j.get("education", ""))),
            })
        off += len(rs)
        if off >= (d.get("totalHits") or 0):
            break
    return out


def fetch_mercedes(c):
    """Mercedes-Benz's BeeSite search API (MBUSA, MBUSI, Vans, Financial
    Services). Each requisition is listed once per publication, so it is
    de-duplicated on PositionID. Descriptions are inline."""
    api = "https://jobs.api.mercedes-benz.com/search"
    h = {**BROWSER_HEADERS, "Content-Type": "application/json",
         "Origin": "https://jobs.mercedes-benz.com", "Referer": "https://jobs.mercedes-benz.com/"}
    seen, first = {}, 1
    while True:
        body = {"LanguageCode": "EN",
                "SearchParameters": {"FirstItem": first, "CountItem": 100,
                                     "Sort": [{"Criterion": "PublicationStartDate", "Direction": "DESC"}]},
                "SearchCriteria": [{"CriterionName": "PositionLocation.Country", "CriterionValue": ["475"]}]}
        sr = _post_json_retry(api, body, h).get("SearchResult") or {}
        for i in sr.get("SearchResultItems") or []:
            d = i["MatchedObjectDescriptor"]
            seen.setdefault(d["PositionID"], d)
        first += 100
        if first > (sr.get("SearchResultCountAll") or 0):
            break
    out = []
    for d in seen.values():
        loc = (d.get("PositionLocation") or [{}])[0]
        desc = ""
        for blk in d.get("PositionFormattedDescription") or []:
            desc = _join(desc, _strip_html(blk.get("Tasks", "")), _strip_html(blk.get("Qualifications", "")))
        groups = ", ".join(g.get("Name", "") for g in d.get("TargetGroup") or [])
        out.append({
            "title": d.get("PositionTitle", ""),
            "location": loc.get("CityName", ""),
            "url": d.get("PositionURI", ""),
            "posted": d.get("PublicationStartDate", ""),
            "description": _join(desc, ("Target group: " + groups + ".") if groups else ""),
        })
    return out


PORSCHE_API = "https://porsche-beesite-production-gjb.app.beesite.de/search/"


def fetch_porsche(c):
    """Porsche's BeeSite search API (Porsche Cars North America and affiliates).
    The list carries no description; the posting page does (server-rendered)."""
    out, first = [], 1
    while True:
        data = {"LanguageCode": "EN",
                "SearchParameters": {"FirstItem": first, "CountItem": 100,
                                     "Sort": [{"Criterion": "PublicationStartDate", "Direction": "DESC"}]},
                "SearchCriteria": [{"CriterionName": "PublicationChannel.Code", "CriterionValue": ["12"]},
                                   {"CriterionName": "PositionLocation.Country", "CriterionValue": ["231"]}]}
        r = _request_retry("GET", PORSCHE_API, tries=3, timeout=40,
                           params={"data": json.dumps(data)}, headers=REQ_HEADERS)
        sr = r.json().get("SearchResult") or {}
        items = sr.get("SearchResultItems") or []
        if not items:
            break
        for i in items:
            d = i["MatchedObjectDescriptor"]
            loc = (d.get("PositionLocation") or [{}])[0]
            out.append({
                "title": d.get("PositionTitle", ""),
                "location": loc.get("CityName", ""),
                "url": d.get("PositionURI", ""),
                "posted": d.get("PublicationStartDate", ""),
                "description": "",
            })
        first += 100
        if first > (sr.get("SearchResultCountAll") or 0):
            break
    return [j for j in out if not j["title"].lower().startswith("test ")
            and not j["title"].lower().startswith("test-")]


_ICIMS_LIST_RE = re.compile(r'href="(https://[^"]*/jobs/(\d+)/[^"/]*/job)[^"]*"')


def fetch_icims(c):
    """iCIMS career portals (c["host"]) -- Joby. The search page is HTML; each
    posting's JSON-LD block holds its title, location and full description, so
    every posting is read here (a few hundred small requests, in parallel).
    iCIMS gives no usable posted date, so it is left blank."""
    host = c["host"]
    urls, seen, pr = [], set(), 0
    while pr < 40:
        r = _request_retry("GET", f"https://{host}/jobs/search", tries=3, timeout=40,
                           params={"ss": 1, "in_iframe": 1, "pr": pr}, headers=BROWSER_HEADERS)
        new = [(u, i) for u, i in _ICIMS_LIST_RE.findall(r.text) if i not in seen]
        if not new:
            break
        for u, i in new:
            seen.add(i)
            urls.append(u)
        m = re.search(r"Page \d+ of (\d+)", r.text)
        pr += 1
        if m and pr >= int(m.group(1)):
            break

    def one(u):
        try:
            title, location, desc = _fetch_icims_posting(u)
        except Exception:
            return None
        return {"title": title, "location": location, "url": u, "posted": "", "description": desc}

    import concurrent.futures as cf
    with cf.ThreadPoolExecutor(max_workers=8) as ex:
        return [j for j in ex.map(one, urls) if j and j["title"]]


_ICIMS_URL_RE = re.compile(r"https://([a-z0-9.-]+\.icims\.com)/jobs/(\d+)/")


def _fetch_icims_posting(url):
    """(title, location, description) from an iCIMS posting's JSON-LD block."""
    r = requests.get(url, params={"in_iframe": 1}, timeout=30, headers=BROWSER_HEADERS)
    r.raise_for_status()
    m = re.search(r'<script type="application/ld\+json">(.*?)</script>', r.text, re.S)
    if not m:
        return "", "", _strip_html(r.text)
    d = json.loads(m.group(1))
    loc = d.get("jobLocation")
    loc = (loc[0] if isinstance(loc, list) and loc else loc) or {}
    a = (loc.get("address") or {}) if isinstance(loc, dict) else {}
    location = ", ".join(x for x in (a.get("addressLocality"), a.get("addressRegion"),
                                     a.get("addressCountry")) if x)
    return d.get("title", ""), location, _strip_html(d.get("description", ""))


def _fetch_icims_description(url):
    return _fetch_icims_posting(url)[2]


def fetch_jazzhr(c):
    """JazzHR boards (c["host"], e.g. ouster.applytojob.com) -- one HTML page
    listing every opening; the posting page carries the JSON-LD text/date."""
    host = c["host"]
    r = _request_retry("GET", f"https://{host}/apply", tries=3, timeout=40, headers=BROWSER_HEADERS)
    out = []
    for it in re.findall(r'<li class="list-group-item">(.*?)</li>', r.text, re.S):
        m = re.search(rf'href="(https://{re.escape(host)}/apply/[A-Za-z0-9]+/[^"]*)"[^>]*>\s*([^<]+?)\s*</a>', it)
        if not m:
            continue
        loc = re.search(r"fa-map-marker['\"]></i>\s*([^<]*)", it)
        out.append({"title": html.unescape(m.group(2)), "url": m.group(1),
                    "location": (loc.group(1).strip() if loc else ""),
                    "posted": "", "description": ""})
    return out



def _fetch_sf_description(url):
    """Description of a SuccessFactors posting: only the job-description span
    (the full page is 100-300 KB of script and navigation, which would feed
    stray numbers to the experience filters), plus its posted date written the
    way _extract_json_ld_date reads it."""
    r = requests.get(url, timeout=30, headers=BROWSER_HEADERS)
    r.raise_for_status()
    t = r.text
    i = t.find('class="jobdescription"')
    if i < 0:
        return ""
    seg = t[i:]
    end = seg.find("<script")
    desc = _strip_html(re.sub(r"<style.*?</style>", " ", seg[:end] if end > 0 else seg[:60000], flags=re.S))
    m = re.search(r'itemprop="datePosted"[^>]*content="([^"]+)"', t)
    iso = ""
    if m:
        try:
            iso = datetime.datetime.strptime(
                re.sub(r"\s+[A-Z]{3,4}\s+(?=\d{4})", " ", m.group(1)), "%a %b %d %H:%M:%S %Y").date().isoformat()
        except ValueError:
            iso = ""
    return _join(desc, f'"datePosted": "{iso}"' if iso else "")


def _fetch_jazzhr_description(url):
    r = requests.get(url, timeout=30, headers=BROWSER_HEADERS)
    r.raise_for_status()
    for blk in re.findall(r'<script type="application/ld\+json">(.*?)</script>', r.text, re.S):
        try:
            d = json.loads(blk)
        except ValueError:
            continue
        if isinstance(d, dict) and d.get("@type") == "JobPosting":
            return _join(_strip_html(d.get("description", "")),
                         f'"datePosted": "{str(d["datePosted"])[:10]}"' if d.get("datePosted") else "")
    return ""


# ---------------------------------------------------------------------------
# Real apply links for jobright rows.
# ---------------------------------------------------------------------------
# jobright only gives its own page for a role -- the employer link behind its
# Apply button needs a logged-in jobright account (the endpoint answers "Cookie
# not found"), so it can't be read. A row that passes every filter is therefore
# matched to the employer's own job board instead: find the company's
# Greenhouse / Lever / Ashby / Workable / SmartRecruiters board (guessed from its
# name, or read off the company's own website) and take the posting with the same
# title. Rows with no match are not written or emailed -- a jobright link is
# never shown.
_JOBRIGHT_COMPANY_URL = {}   # jobright page -> the employer's website
_BOARD_CACHE = {}            # normalized company -> [(title, location, url)]
_ATS_LINK_RES = (
    ("greenhouse", re.compile(r"(?:boards|job-boards)(?:\.eu)?\.greenhouse\.io/(?:embed/job_board\?for=)?([A-Za-z0-9_-]+)")),
    ("lever", re.compile(r"jobs\.lever\.co/([A-Za-z0-9_.-]+)")),
    ("ashby", re.compile(r"jobs\.ashbyhq\.com/([A-Za-z0-9_.-]+)")),
    ("workable", re.compile(r"apply\.workable\.com/([A-Za-z0-9_-]+)")),
    ("smartrecruiters", re.compile(r"(?:jobs|careers)\.smartrecruiters\.com/([A-Za-z0-9_-]+)")),
)
_NOT_SLUGS = {"embed", "api", "v1", "j", "company", "jobs", "careers"}
_QUICK_PROBES = {
    "greenhouse": "https://boards-api.greenhouse.io/v1/boards/{}/jobs",
    "lever": "https://api.lever.co/v0/postings/{}?mode=json",
    "ashby": "https://api.ashbyhq.com/posting-api/job-board/{}",
}


def _title_key(t):
    return re.sub(r"[^a-z0-9]+", " ", (t or "").lower()).strip()


def _slug_guesses(company):
    n = re.sub(r"\(.*?\)", "", company or "").lower()
    n = re.sub(r"[,.&'\u2019]", " ", n)
    words = [w for w in n.split() if w not in
             ("inc", "llc", "corp", "corporation", "co", "company", "ltd", "the", "group", "holdings")]
    out = []
    if words:
        out += ["".join(words), "-".join(words), words[0]]
        if len(words) > 1:
            out.append("".join(words[:2]))
    return list(dict.fromkeys(out))


def _board_jobs(ats, slug):
    """[(title, location, url, posted)] for one board, or [] when it doesn't exist.
    Probes with a single quick request first so a wrong guess costs ~1s
    rather than the full retry/backoff of the regular fetchers."""
    probe = _QUICK_PROBES.get(ats)
    if probe:
        try:
            if requests.get(probe.format(slug), timeout=12, headers=REQ_HEADERS).status_code != 200:
                return []
        except requests.RequestException:
            return []
    try:
        jobs = FETCHERS[ats]({"name": slug, "ats": ats, "slug": slug})
    except Exception:
        return []
    return [(j.get("title", ""), j.get("location", ""), j.get("url", ""), j.get("posted", ""))
            for j in jobs if j.get("url")]


def _company_board_jobs(company, company_url=""):
    key = _norm_company(company)
    if key in _BOARD_CACHE:
        return _BOARD_CACHE[key]
    found, tried = [], set()
    for slug in _slug_guesses(company):
        for ats in _QUICK_PROBES:
            if (ats, slug) in tried:
                continue
            tried.add((ats, slug))
            found += _board_jobs(ats, slug)
        if found:
            break
    if not found and company_url:
        # Read the employer's own site for a link to the board it really uses.
        base = company_url.rstrip("/")
        pages = []
        for path in ("", "/careers", "/jobs", "/company/careers"):
            try:
                r = requests.get(base + path, timeout=12, headers=BROWSER_HEADERS)
                if r.ok:
                    pages.append(r.text)
            except requests.RequestException:
                continue
        for ats, rx in _ATS_LINK_RES:
            for text in pages:
                for slug in dict.fromkeys(rx.findall(text)):
                    if slug.lower() in _NOT_SLUGS or (ats, slug) in tried:
                        continue
                    tried.add((ats, slug))
                    found += _board_jobs(ats, slug)
    _BOARD_CACHE[key] = found
    return found


def _same_company(a, b):
    return bool(a and b and (a == b or (min(len(a), len(b)) >= 5 and (a in b or b in a))))


def _resolve_direct_link(company, title, location, company_url="", title_index=None):
    """(url, posted) of the employer's own posting for a jobright row, or
    ("", "") if none found. The posted date is the employer's, not jobright's.

    First looks among every posting this run already read from company boards
    (title_index: title -> [(company, location, url, posted)]), then at the
    employer's own board if it is on a platform we can find."""
    want = _title_key(title)
    if not want:
        return "", ""
    loc_words = set(re.findall(r"[a-z]{3,}", (location or "").lower()))

    def best(cands):
        j = max(cands, key=lambda j: len(loc_words & set(re.findall(r"[a-z]{3,}", (j[1] or "").lower()))))
        return j[2], j[3]

    nc = _norm_company(company)
    hits = [x for x in (title_index or {}).get(want, []) if _same_company(nc, x[0])]
    if hits:
        return best([(h[0], h[1], h[2], h[3]) for h in hits])
    exact = [j for j in _company_board_jobs(company, company_url) if _title_key(j[0]) == want]
    return best(exact) if exact else ("", "")


FETCHERS = {
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "ashby": fetch_ashby,
    "workday": fetch_workday,
    "amazon": fetch_amazon,
    "google": fetch_google,
    "microsoft": fetch_microsoft,
    "newgrad_feed": fetch_newgrad_feed,
    "smartrecruiters": fetch_smartrecruiters,
    "oracle": fetch_oracle,
    "workable": fetch_workable,
    "apmlist": fetch_apmlist,
    "jobright": fetch_jobright,
    "jibe": fetch_jibe,
    "eightfold": fetch_eightfold,
    "sfcsb": fetch_sfcsb,
    "phenom": fetch_phenom,
    "mcloud": fetch_mcloud,
    "mercedes": fetch_mercedes,
    "porsche": fetch_porsche,
    "icims": fetch_icims,
    "jazzhr": fetch_jazzhr,
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
        if _DRIVING_RECORD_CONTEXT.search(window):
            return
        if need_context and not _EXPERIENCE_CONTEXT.search(window):
            return
        tok = m.group(1)
        # tok.isdigit() is False for a decimal ("1.5"); the replace() check
        # accepts exactly one "." among otherwise-digit characters.
        if tok.isdigit():
            val = int(tok)
        elif tok.replace(".", "", 1).isdigit():
            val = float(tok)
        else:
            val = _WORD_NUMBERS[tok.lower()]
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


# Some postings never state a number of years but still describe a clearly
# senior bar in prose: "Bring substantial technical program management
# leadership..., with a track record of owning complex programs end to end"
# is a staff/lead-level requirement that years_required() can't see, because
# it never says "5 years" -- it says "track record" instead.
#
# An intensity word (strong/substantial/extensive/proven/exceptional/
# significant/deep) within ~150 chars of "track record" is the whole signal.
# This is NOT a ban on the phrase "track record" -- "you'll build a track
# record of success here" is exactly the kind of forward-looking, entry-
# level-friendly phrasing this must not catch, and across every posting
# checked so far, it never does. An earlier version also required a scale
# word (complex/large-scale/high-impact/etc.) right after "track record", on
# the theory that intensity + track record alone might be too loose --
# dropped after it missed real cases: Thumbtack's "a proven track record of
# launching impactful... products" (no scale word in that list) and a Chime
# lead PM's "significant experience... a proven track record of owning
# complex product areas" both read as clearly senior without one.
#
# "demonstrated" was in the intensity list and got removed: Harper Group's
# posting explicitly says "What we're looking for: 1-3 years in product, or
# an early-career operator... who's been doing the work without the title"
# -- genuinely entry-level-friendly -- and still tripped on "Demonstrated
# end-to-end ownership of a product... and a track record of going deep on a
# domain." "Demonstrated" can describe aptitude shown in a class project or
# internship, not just years of professional practice, unlike the remaining
# words here. Confirmed removing it doesn't lose any of the other real
# catches -- none of them were triggered by "demonstrated".
#
# Known rough edge, checked and accepted: postings list requirements as
# scraped-flat bullet points with no period between them, so this can match
# across two adjacent-but-unrelated bullets (an intensity word in one, "track
# record" in the next) rather than one coherent requirement. Confirmed live
# against 268 title+location-matching postings across every source: this
# happened twice (both Anthropic TPM postings), and both were independently,
# obviously senior anyway -- so the final verdict was never wrong, even though
# the match itself was coincidental. Revisit if a genuinely entry-level
# posting ever gets caught this way.
_SENIOR_TRACK_RECORD = re.compile(
    r"\b(strong|substantial|extensive|proven|exceptional|significant|deep)\b"
    r"[^.;!?]{0,150}\btrack record\b",
    re.I,
)


_COMPANY_BLURB_LEAD = re.compile(
    r"\b(?:company|firm|brands?|group|team|we|they)\s+(?:has|have|had|with)\s+an?\s*$", re.I)


def has_senior_track_record_signal(desc):
    """True if the description carries the qualitative-seniority pattern
    _SENIOR_TRACK_RECORD matches -- a senior/staff-level bar stated in prose
    instead of (or alongside) a number years_required() could catch. See the
    comment above the regex for exactly what this does and doesn't match."""
    if not desc:
        return False
    for m in _SENIOR_TRACK_RECORD.finditer(desc):
        # "...the company has a proven track record of bringing disruptive
        # innovation to market" is the employer's own blurb (SharkNinja's
        # "About Us"), not a bar set for the applicant.
        if _COMPANY_BLURB_LEAD.search(desc[max(0, m.start() - 30):m.start()]):
            continue
        return True
    return False


# Turned up opening the Customer Success category: Harvey's "WHAT YOU HAVE -
# Experienced professionals with a background in Enterprise SaaS, legal (big
# law) or top tier management consulting firms" is a clear senior bar, but
# has neither a number (years_required) nor "track record"
# (has_senior_track_record_signal) -- a third disguise shape distinct from
# both. Confirmed live across 500 title+location-matching postings: exactly
# 6 contain this phrase, all 6 genuinely senior (the 2 not already caught by
# something else are both Harvey CSM postings), zero false positives.
_EXPERIENCED_PROFESSIONAL = re.compile(r"\bexperienced professionals?\b", re.I)


def has_experienced_professional_signal(desc):
    """True if the description opens a requirement with "experienced
    professional(s)" -- see _EXPERIENCED_PROFESSIONAL above for the evidence
    behind this exact phrase."""
    if not desc:
        return False
    return bool(_EXPERIENCED_PROFESSIONAL.search(desc))


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


# Workable's own job pages (apply.workable.com/{account}/j/{shortcode}) are a
# JS-rendered SPA -- a plain GET returns only the loading shell, no real
# content. Confirmed live: an Avalore "Data Analyst" posting's "SECRET
# Security Clearance Required" line (in the API's separate "requirements"
# field, not even "description") was completely invisible to the generic
# scrape below, so nothing could ever have caught it there.
_WORKABLE_URL_RE = re.compile(r"apply\.workable\.com/([\w-]+)/j/([A-Za-z0-9]+)")


def _fetch_workable_description(account, shortcode):
    """Workable's API returns the real text directly, no rendering needed.
    Combines description + requirements + benefits, since a real disqualifier
    (a clearance requirement, an experience-level breakdown) can live in any
    of the three -- confirmed live that Avalore's was in "requirements"."""
    api = f"https://apply.workable.com/api/v1/accounts/{account}/jobs/{shortcode}"
    r = requests.get(api, timeout=20, headers=REQ_HEADERS)
    r.raise_for_status()
    d = r.json()
    return _join(_strip_html(d.get("description", "")),
                 _strip_html(d.get("requirements", "")),
                 _strip_html(d.get("benefits", "")))


# Microsoft's careers pages are a JS-rendered SPA like Workable's: a plain GET
# returns an empty shell, so the real text comes from the detail endpoint.
_MS_JOB_URL_RE = re.compile(r"apply\.careers\.microsoft\.com/careers/job/(\d+)")


def _fetch_microsoft_description(position_id):
    r = requests.get(MS_CAREERS + "/api/pcsx/position_details",
                     params={"position_id": position_id, "domain": "microsoft.com", "hl": "en"},
                     timeout=20, headers=REQ_HEADERS)
    r.raise_for_status()
    return _strip_html(((r.json().get("data") or {}).get("jobDescription")) or "")


def _fetch_description(url):
    """Best-effort: read a posting's text straight off its page.

    Some sources ship listings with no description at all -- the New-Grad Feed
    and Workday both do -- which blinds the experience filter: years_required()
    finds nothing, returns None, and the role is kept no matter what it
    actually requires. That is not a small gap. It is why roles deleted from
    the sheet by hand reappear on the very next run, since the scraper cannot
    see the requirement the deletion was based on.

    A Workable URL is delegated to _fetch_workable_description() instead of
    the generic scrape below, which cannot see real Workable content at all
    (see _WORKABLE_URL_RE). Falls through to the generic scrape if that API
    call itself fails, rather than giving up outright.

    Retries a couple times on anything that looks transient (a network
    error, a timeout, or a non-404 bad status) before giving up: confirmed
    live that GitHub Actions' shared runner IPs occasionally get a fetch
    failure here that a normal connection doesn't (three Comcast Workday
    postings that plainly require 5-7 years fetched fine on every attempt
    from a regular connection, yet got emailed as new "0 years" matches from
    an Actions run -- the only way that happens is this function returning
    ""  that run, years_required("") coming back None, and the years gate
    reading "nothing stated" instead of "couldn't check"). Same fix shape as
    _gspread_retry, for the same reason: a single blip shouldn't silently
    turn off a filter.

    Still returns "" after retries are exhausted (or immediately on a 404,
    which retrying can't fix), preserving the old keep-on-silence behaviour
    rather than dropping a role because its page is genuinely gone.
    """
    m = _WORKABLE_URL_RE.search(url)
    if m:
        try:
            return _fetch_workable_description(*m.groups())
        except Exception:
            pass  # fall through to the generic scrape below
    m = _MS_JOB_URL_RE.search(url)
    if m:
        try:
            return _fetch_microsoft_description(m.group(1))
        except Exception:
            pass
    m = _SR_URL_RE.search(url)
    if m:
        try:
            return _fetch_smartrecruiters_description(*m.groups())
        except Exception:
            pass
    m = _ORACLE_URL_RE.search(url)
    if m:
        try:
            return _fetch_oracle_description(*m.groups())
        except Exception:
            pass
    if _JOBRIGHT_URL_RE.search(url):
        try:
            return _fetch_jobright_description(url)
        except Exception:
            pass
    host = re.match(r"https?://([^/]+)", url)
    host = host.group(1) if host else ""
    if host in _SF_HOSTS and "/job/" in url:
        try:
            d = _fetch_sf_description(url)
            if d:
                return d
        except Exception:
            pass
    if host in _JAZZ_HOSTS:
        try:
            d = _fetch_jazzhr_description(url)
            if d:
                return d
        except Exception:
            pass
    m = _PHENOM_URL_RE.search(url)
    if m:
        try:
            return _fetch_phenom_description(*m.groups())
        except Exception:
            pass
    m = _EIGHTFOLD_URL_RE.search(url)
    if m and m.group(1) in _EIGHTFOLD_SITES:
        try:
            return _fetch_eightfold_description(*m.groups())
        except Exception:
            pass
    if _ICIMS_URL_RE.search(url):
        try:
            return _fetch_icims_description(url)
        except Exception:
            pass
    delays = (1, 2)
    for i, delay in enumerate((*delays, None)):
        try:
            r = requests.get(url, timeout=20, headers=REQ_HEADERS)
            if r.ok:
                # Sniff the encoding instead of taking the default. requests
                # falls back to Latin-1 when a page declares no charset, which
                # turns "0-2 years" into mojibake the range pattern can't read
                # -- and a posting that advertises itself as entry level then
                # scores as a 2-year requirement.
                r.encoding = r.apparent_encoding or "utf-8"
                return _strip_html(r.text)
            if r.status_code == 404 or delay is None:
                return ""
        except Exception:
            if delay is None:
                return ""
        time.sleep(delay)
    return ""


# Many career pages -- Workday's included -- embed a schema.org JobPosting
# JSON-LD block for SEO, with a real "datePosted" field. _strip_html() only
# removes tags, so this JSON text survives as plain text in whatever
# _fetch_description() returns; confirmed live on Guidehouse and Adobe
# Workday postings (Adobe's read "datePosted": "2026-06-23", ~80 days
# before this fix -- Workday's API gives no date at all, so nothing had
# ever judged that posting's age).
_JSON_LD_DATE_POSTED = re.compile(r'"datePosted"\s*:\s*"(\d{4}-\d{2}-\d{2})')


_JSON_LD_COUNTRY = re.compile(r'"addressCountry"\s*:\s*"([^"]+)"')
_US_COUNTRY_NAMES = {"us", "usa", "united states", "united states of america", "u.s.", "u.s.a."}


def _extract_non_us_country(desc):
    """The country named in a posting page's JSON-LD block, when it isn't the
    US. Workday's listing gives only a city or "3 Locations", so a German or
    Czech plant posting passes the location check; the page itself states the
    country. Returns None when absent or US."""
    m = _JSON_LD_COUNTRY.search(desc or "")
    if m and m.group(1).strip().lower() not in _US_COUNTRY_NAMES:
        return m.group(1).strip()
    return None


def _extract_json_ld_date(desc):
    """Best-effort fallback posted date for a source whose API gives none
    (Workday; also the New-Grad Feed, whose "url" leads to the real
    company's own posting page). Returns None when absent, which is every
    source that already had a real "posted" value and never reaches this."""
    m = _JSON_LD_DATE_POSTED.search(desc or "")
    return m.group(1) if m else None


def is_closed_posting(desc):
    """True if the page itself says the listing is no longer open.

    Workday embeds the page's own state as JS, including a literal
    "postingAvailable: true" or "postingAvailable: false", and _strip_html()
    only removes HTML tags -- not the text between them -- so that value
    survives into the fetched description as plain text. A requisition can
    close while still showing up in a company's own Workday search results
    for a while after (confirmed live: Ancestry's "Associate Technical
    Product Manager - Data, AI & Analytics Platforms" is exactly this --
    postingAvailable: false, still returned by search). Harmless no-op for
    every other source: this exact string only ever appears in a Workday
    page's own JS."""
    return "postingAvailable: false" in (desc or "")


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
        # See MICROSOFT_TITLE_INCLUDE: bare "program manager" only counts at
        # Microsoft. job["company"] is only populated for Microsoft's own
        # source and for aggregator rows that name Microsoft as the
        # employer -- every other source leaves it unset, so this can't
        # match anywhere else.
        if job.get("company") == "Microsoft" and any(
            k in title for k in MICROSOFT_TITLE_INCLUDE
        ):
            return True
        return False
    # Only a bare acronym carried the match, so make sure it's really ours.
    if matched <= TITLE_ACRONYMS:
        return not any(w in title for w in ACRONYM_FALSE_FRIENDS)
    return True


def passes_title_v2(job):
    """Title gate for the Jobs 2.0 category (TITLE_INCLUDE_V2) -- its own
    category, independent of passes_title() above. Does not include
    "technical support specialist" (see TECHNICAL_SUPPORT_SPECIALIST_V2,
    handled separately in classify_job) or the robotics-company "any role"
    exception (also classify_job) -- both need context (company/robotics
    status) this function doesn't have."""
    title = (job.get("title") or "").lower()
    if _title_excluded(title):
        return False
    return any(k in title for k in TITLE_INCLUDE_V2)


def passes_title_any(job, is_robotics=False):
    """Cheap title(+robotics-flag)-only gate covering BOTH tabs, so main()
    knows whether a job is worth paying for a description fetch before
    classify_job() decides its actual category. Every way a job can end up
    in either tab needs a branch here, or it's dropped before a description
    is ever fetched for it."""
    title = (job.get("title") or "").lower()
    if passes_title(job) or passes_title_v2(job):
        return True
    if TECHNICAL_SUPPORT_SPECIALIST_V2 in title and not _title_excluded(title):
        return True
    if (is_robotics and not _title_excluded(title) and _any_role_title_ok(title)
            and not _ROBOTICS_ANY_ROLE_EXCLUDE_TITLE_RE.search(title)
            and not any(p in title for p in _ROBOTICS_ANY_ROLE_EXCLUDE_PHRASES)):
        return True
    return False


def is_core_title(title):
    """True if the title matches TITLE_INCLUDE_CORE -- the roles called out as
    highest priority (product manager/analyst/owner/ops), as opposed to the
    rest of TITLE_INCLUDE (technical program manager, associate project/
    program manager) that are wanted but rank behind those. Ranking only --
    passes_title() already gated on the full TITLE_INCLUDE above."""
    title = (title or "").lower()
    for k in TITLE_INCLUDE_CORE:
        if k in TITLE_ACRONYMS:
            if re.search(rf"\b{re.escape(k)}\b", title):
                return True
        elif k in title:
            return True
    return False


# Signals that a role leans technical/analytical -- scripting, telemetry,
# data, automation -- rather than purely relationship-management-style
# customer-facing work. Added after you flagged Simbe Robotics' "Technical
# Support Analyst" as a perfect fit: Python/SQL/bash, telemetry, logs,
# diagnostics, automation tooling, AI-assisted workflows, no people-
# management or account-ownership language. Ranking only, same as
# is_core_title() -- a support/success role missing all of these still
# passes if it clears every other gate, it just sorts lower.
TECHNICAL_RELEVANCE_TERMS = [
    "python", "sql", "bash", "scripting", "script", "telemetry", "log data",
    "logs", "diagnostic", "troubleshoot", "automation", "root cause",
    "debugging", "data analysis", "api",
]


def technical_relevance_score(desc):
    text = (desc or "").lower()
    return sum(1 for term in TECHNICAL_RELEVANCE_TERMS if term in text)


# Shift work and part-time/temporary hours, named in the title itself
# ("Production Coordinator - 2nd Shift", "Box Truck Driver - PT", "Customer
# Service Representative (Casual)"). Every one of those was a role of the
# kind already rejected by hand, at every category -- not just the any-role
# fallback.
_HOURS_TITLE_RE = re.compile(
    r"\bshifts?\b|\bpart[- ]time\b|(?:^|[-\u2013(|,/]\s*)pt\b\)?\s*$|\(pt\)|"
    r"\bcasual\b|\bprn\b|\btemp(?:orary)?\b|\b(?:back|front) days\b", re.IGNORECASE)
# A title written for another country: German/French gender tags ("(m/w/x)",
# "(F/H)"), or an accented letter ("Mecanicien", "Tecnico", "Operator ve
# vyrobe"). Workday often reports only a city or "3 Locations", so the
# location check alone misses these -- confirmed live on Magna, Caterpillar,
# Avis and GE Vernova postings.
_FOREIGN_TITLE_RE = re.compile(
    r"\((?:[mfwdxh])\s*/\s*[mfwdxh](?:\s*/\s*[mfwdxh])?\)|\bbac\s*\+\s*\d|"
    r"\b(?:stagiaire|alternance|praktikum|werkstudent|ingeniero|tecnico|mecanico)\b",
    re.IGNORECASE)


def _is_foreign_title(title):
    t = title or ""
    return bool(_FOREIGN_TITLE_RE.search(t)) or any(ch.isalpha() and ord(ch) > 127 for ch in t)


def _title_excluded(title):
    t = title.lower()
    for w in TITLE_EXCLUDE_WORDS:
        if w in t:
            return True
    for tok in TITLE_EXCLUDE_TOKENS:
        if re.search(rf"\b{re.escape(tok)}\b", t):
            return True
    if _HOURS_TITLE_RE.search(title) or _is_foreign_title(title):
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


# "Customer support" only counts at a robotics or IoT company -- checked
# against the title, description, and company name together, since a
# generic "Customer Support Specialist" posting rarely spells out the
# product category in the title alone. Keyword-based like everything else
# here, so a robotics/IoT company whose posting happens not to use any of
# these words (or use different ones) won't be recognized -- a real, known
# limit, not chased further without a concrete missed example to work from.
ROBOTICS_IOT_TERMS = [
    "robotics", "internet of things", "smart home",
    "connected device", "embedded system", "autonomous vehicle", "drone",
]
# "robot" and "iot" are matched as whole words only (via _has_term's `codes`
# argument), not substrings: "iot" is a real substring of "Marriott" (mar-
# **riot**-t) and "robot" of a handful of unrelated words, so plain
# substring matching would silently wave through any company whose name or
# description happens to contain the letters, robotics or not. Confirmed
# live: "Marriott International" postings from the New-Grad Feed matched
# before this fix, from "iot" hiding inside the company name.
ROBOTICS_IOT_CODES = ["robot", "iot"]


def is_robotics_or_iot(title, desc, company):
    text = f"{title} {desc or ''} {company or ''}".lower()
    return _has_term(text, ROBOTICS_IOT_TERMS, ROBOTICS_IOT_CODES)


def _is_support_category_title(title):
    """True for the customer success/support/technical support category,
    as opposed to product roles -- see MAX_YEARS_EXPERIENCE_SUPPORT.

    Any parenthetical is stripped before checking: "Sales Engineer 2
    (Customer Success)" is a Sales Engineer role with a department tag, not
    a support role. Confirmed live as a real miss without this -- a 1.5
    year requirement passed under the support ceiling's 2 instead of the
    correct base ceiling of 0 for TITLE_INCLUDE_V2's "sales engineer"."""
    title = re.sub(r"\([^)]*\)", "", title)
    return (
        "customer success" in title
        or "customer support" in title
        or "technical support" in title
    )


def _years_ceiling_for(title):
    """Which MAX_YEARS_EXPERIENCE* ceiling applies to a given (lowercased)
    title. See MAX_YEARS_EXPERIENCE_SUPPORT for why this category gets its
    own, looser number."""
    if _is_support_category_title(title):
        return MAX_YEARS_EXPERIENCE_SUPPORT
    return MAX_YEARS_EXPERIENCE


def matches(job, is_robotics=False):
    # US-only: anything naming a country outside the US is dropped. A listing
    # that names no country at all is kept (see is_non_us), and location
    # otherwise drives priority ranking (see is_priority_location).
    title = (job.get("title") or "").lower()
    desc = (job.get("description") or "").lower()

    if is_non_us(job.get("location")):
        return False
    if not passes_title(job):
        return False
    if ("customer support" in title or "technical support" in title) and "customer success" not in title:
        # is_robotics is only ever passed by classify_job(), and only True
        # for companies tagged "robotics": True in COMPANIES -- a more
        # reliable signal than is_robotics_or_iot()'s text search, which
        # depends on "company" being set on the job dict (unset for most
        # direct-fetch sources) or the posting's own title/description
        # happening to spell out "robotics"/"IoT"/etc. Every existing call
        # site omits this argument, so its default leaves this check
        # byte-for-byte identical to before.
        if not (is_robotics or is_robotics_or_iot(title, desc, job.get("company"))):
            return False
    if _description_excluded(title, desc):
        return False
    ceiling = _years_ceiling_for(title)
    if ceiling is not None:
        years = years_required(desc)
        if years is not None and years > ceiling:
            return False
        if has_senior_track_record_signal(desc):
            return False
        if has_experienced_professional_signal(desc):
            return False
    # An early-career program's own graduation window ("graduating between
    # Winter 2026 and Summer 2027") is the program's target cohort, not a
    # reason to hide it -- see _is_program_title.
    if GRADUATED is not None and not _is_program_title(title):
        grad = earliest_graduation_window(desc)
        if grad is not None and grad > GRADUATED:
            return False
    return True


# ===========================================================================
# Seniority gate: catches experienced-hire roles that never state a years
# number, so years_required() returns None and nothing else stops them.
# ===========================================================================
# Confirmed live against four real postings -- two Mastercard "Manager, ..."
# roles, OpenAI's "Product Manager, GTM Innovation" and "Technical Program
# Manager, Multimodal". None states a years-required number; the Mastercard
# pair list pay ranges of $130K-$254K, and all four are plainly experienced-
# hire roles. The tell is the title: a bare "Manager" (Product Manager,
# Technical Program Manager, "Manager, Product Operations") at an employer
# that doesn't say early-career anywhere.
#
# Rules (from the criteria you gave):
#   - A role with people-management responsibility is excluded.
#   - A "Manager" title is excluded UNLESS the title or description clearly
#     says early-career (associate/junior/APM/university/new grad/0-2 years).
#     "Associate Product Manager" and "Program Manager, University Grad" pass;
#     "Product Manager" and "Manager, Product Management" do not.
#   - Senior/Lead/Principal/Director are already hard title excludes
#     (TITLE_EXCLUDE_WORDS), with no early-career exception.
#   - Roles that DO read early-career are ranked first (early_career_score).
# The years-of-experience ceiling (MAX_YEARS_EXPERIENCE = 0) is unchanged and
# already stricter than "3+ years".
_MANAGER_WORD_RE = re.compile(r"\bmanager\b", re.I)
# "associate" alone isn't enough when it's "Associate Manager" -- that's a
# mid-level title at large companies, not an entry-level one.
_EARLY_CAREER_TITLE_RE = re.compile(
    r"\bassociate\b(?!\s+manager)|\bjunior\b|\bjr\b|\bapm\b|\brotational\b|"
    r"\bnew[- ]grad\w*|\buniversity\b|\bearly[- ]career\b|\bentry[- ]level\b|"
    r"\bgraduate\b|\bintern(?:ship)?\b|\btrainee\b", re.I)
_EARLY_CAREER_DESC_PHRASES = (
    "new grad", "new graduate", "recent graduate", "recent grad",
    "recent college graduate", "recent university graduate", "early career",
    "early-career", "entry level", "entry-level", "university grad",
    "university program", "graduate program", "no prior experience",
    "no experience required", "class of 2026", "class of 2027",
    "2026 graduate", "2027 graduate",
)
_ZERO_TO_TWO_RE = re.compile(r"\b0\s*(?:-|–|—|to)\s*2\s*(?:years?|yrs?)\b", re.I)
# Title words that rank a role up without exempting a "Manager" title.
_RANK_TITLE_RE = re.compile(r"\b(analyst|coordinator)\b", re.I)
# Deliberately NOT bare "manage teams"/"lead teams" (no article) -- confirmed
# live as a false positive on a real Associate Product Manager posting:
# "Ability to work effectively across functions and lead teams" is generic
# cross-functional soft-skill boilerplate, not an actual management
# responsibility. "manage/lead A team" (singular, definite) is concretely
# about owning one specific team and doesn't have that problem.
_PEOPLE_MGMT_PHRASES = (
    "direct reports", "direct report", "people management", "people manager",
    "manage a team", "manage and mentor", "manage and develop",
    "managing a team", "manage the team",
    "lead a team", "leading a team",
    "lead and mentor", "lead and develop", "supervise a team",
    "supervisory responsibilit", "supervise staff", "hire and manage",
    "hiring and managing", "hire, develop", "build and manage a team",
    "build and lead a team", "manage a group",
)
# A people-management phrase doesn't count when the words just before it
# negate or soften it: "no direct reports", "without people management",
# "an opportunity to grow into managing a team".
_SOFTENER_RE = re.compile(
    r"\b(no|not|without|never|opportunit\w*|grow\w*|eventual\w*|future|"
    r"path to|potential|aspire\w*|learn\w*)\b|n't", re.I)
_NEGATION_RE = re.compile(r"\b(not|non|no)\b|n't", re.I)


def _phrase_hit(text, phrases, blocker_re, lookback):
    """True if any phrase occurs in text at least once with none of
    blocker_re's words in the `lookback` characters before it."""
    for p in phrases:
        start = 0
        while True:
            i = text.find(p, start)
            if i < 0:
                break
            if not blocker_re.search(text[max(0, i - lookback):i]):
                return True
            start = i + len(p)
    return False


def has_people_management_signal(desc):
    return _phrase_hit((desc or "").lower(), _PEOPLE_MGMT_PHRASES, _SOFTENER_RE, 45)


def has_early_career_desc_signal(desc):
    """True when the description itself clearly says early-career / new grad /
    0-2 years. "not an entry-level role" does not count."""
    text = (desc or "").lower()
    if _ZERO_TO_TWO_RE.search(text):
        return True
    return _phrase_hit(text, _EARLY_CAREER_DESC_PHRASES, _NEGATION_RE, 20)


def seniority_blocked(title, desc):
    """True for a role that reads as experienced-hire even though nothing
    states a years number. See the block comment above."""
    title = (title or "").lower()
    desc = (desc or "").lower()
    if has_people_management_signal(desc):
        return True
    if _MANAGER_WORD_RE.search(title):
        years = years_required(desc)
        clearly_early = (
            _EARLY_CAREER_TITLE_RE.search(title)
            or has_early_career_desc_signal(desc)
            or (years is not None and years <= 2)
        )
        if not clearly_early:
            return True
    return False


def early_career_score(title, desc):
    """0-2 ranking boost: one point for an early-career-shaped title (associate,
    junior, university, new grad, analyst, coordinator...), one for an
    early-career description. Ranking only -- nothing is excluded by it."""
    title = (title or "").lower()
    score = 0
    if _EARLY_CAREER_TITLE_RE.search(title) or _RANK_TITLE_RE.search(title):
        score += 1
    if has_early_career_desc_signal(desc):
        score += 1
    return score


def classify_job(job, is_robotics=False):
    """Routes a job to 'core' or 'v2' (see _classify_job_raw), then applies the
    seniority gate above to whichever it picked -- so a role that passed every
    title/description/years check but reads as experienced-hire is dropped."""
    category = _classify_job_raw(job, is_robotics=is_robotics)
    if category and seniority_blocked(job.get("title"), job.get("description")):
        return None
    return category


def _classify_job_raw(job, is_robotics=False):
    """Routes a job to 'core' (product/support tab), 'v2' (Jobs 2.0 tab), or
    None (no match, drop it). A job lands in exactly one tab, never both:
    'core' is checked first via matches() above -- left completely
    unmodified, so every existing core-tab behavior is unchanged -- and only
    a job matches() rejects gets a chance at 'v2'.

    is_robotics is True when the job's employer is one of the companies
    tagged "robotics": True in COMPANIES (see ROBOTICS_COMPANY_NAMES): ANY
    new-grad-eligible role there qualifies for Jobs 2.0, not just a
    title-category match -- the "even a role you wouldn't otherwise
    consider" ask.
    """
    if matches(job, is_robotics=is_robotics):
        return "core"

    title = (job.get("title") or "").lower()
    desc = (job.get("description") or "").lower()

    if is_non_us(job.get("location")):
        return None

    v2_title_ok = (
        passes_title_v2(job)
        or (TECHNICAL_SUPPORT_SPECIALIST_V2 in title and not _title_excluded(title))
        or (is_robotics and not _title_excluded(title) and _any_role_title_ok(title)
            and not _robotics_any_role_excluded(title, desc))
    )
    if not v2_title_ok:
        return None

    # Same shared exclusions matches() applies for 'core', re-run here for
    # 'v2': still-enrolled-student descriptions, the years-of-experience
    # ceiling (and its senior/experienced-professional signals), and the
    # graduation-window cutoff. Kept as a direct copy of matches()'s tail
    # rather than factored out, so matches() itself never has to change.
    if _description_excluded(title, desc):
        return None
    ceiling = _years_ceiling_for(title)
    if ceiling is not None:
        years = years_required(desc)
        if years is not None and years > ceiling:
            return None
        if has_senior_track_record_signal(desc):
            return None
        if has_experienced_professional_signal(desc):
            return None
    if GRADUATED is not None and not _is_program_title(title):
        grad = earliest_graduation_window(desc)
        if grad is not None and grad > GRADUATED:
            return None
    return "v2"


# ===========================================================================
# Google Sheets
# ===========================================================================
def _gspread_retry(fn, *args, **kwargs):
    """Retry a gspread call through a transient Google API outage.

    Every failed run so far (3 for 3, per the Actions log) died on the same
    error -- a 503 "The service is currently unavailable" from Google's side,
    not a bug here -- at whatever gspread call happened to run first. A short
    retry with backoff turns that into a slower success instead of a lost
    20-minute cycle.
    """
    delays = (2, 5, 10)
    for i, delay in enumerate(delays):
        try:
            return fn(*args, **kwargs)
        except gspread.exceptions.APIError as e:
            status = e.response.status_code
            if status not in (500, 502, 503, 504) or i == len(delays) - 1:
                raise
            print(f"[warn] Google API {status}, retrying in {delay}s...")
            time.sleep(delay)


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


def get_spreadsheet():
    """Open the spreadsheet itself (not a specific worksheet), so callers that
    need more than one tab -- the job tracker and the page watch -- don't each
    authorize and open it separately."""
    client = gspread.authorize(get_credentials())
    if SHEET_ID:
        return _gspread_retry(client.open_by_key, SHEET_ID)
    # Name lookup goes through the Drive API, which must be enabled on the
    # Cloud project. Set SHEET_ID to skip it.
    return _gspread_retry(client.open, SHEET_NAME)


def get_worksheet(create=True, name=WORKSHEET_NAME, header=HEADER):
    """Open a tracker worksheet (default: the main "Jobs" tab), creating it
    and its header row if missing. `name`/`header` let Jobs 2.0 (see
    WORKSHEET_NAME_V2) reuse this same logic against its own tab -- every
    existing call site omits both and keeps the original "Jobs" behavior
    unchanged.

    Pass create=False for a read-only open (--dry-run): a missing worksheet
    returns None and a missing header is left alone, so opening the sheet to
    read existing URLs can't itself write to it.
    """
    sh = get_spreadsheet()
    try:
        ws = _gspread_retry(sh.worksheet, name)
    except gspread.WorksheetNotFound:
        if not create:
            return None
        ws = _gspread_retry(sh.add_worksheet, title=name, rows=5000, cols=len(header))
    # gspread returns [[]] for an empty worksheet, which is truthy, so test for
    # real cell content instead of list emptiness. A worksheet the user
    # already created by hand (e.g. Jobs 2.0) and left blank gets the header
    # written here on first run, same as a brand-new one; one with existing
    # content of its own is left alone either way.
    if create and not any(cell for row in _gspread_retry(ws.get_all_values) for cell in row):
        _gspread_retry(ws.append_row, header, value_input_option="USER_ENTERED")
    return ws


def _next_data_row(ws):
    """Row index (1-based) right after the last row with real data in the
    tracked columns A-G.

    Deliberately ignores column H (Applied?). That column is checkbox-
    formatted in the sheet, and a checkbox's unchecked state is a real FALSE
    value written to the cell, not a blank one. append_rows() (used
    previously here) never passes a table_range, so it relies entirely on
    the Sheets API's own "find the end of the table" heuristic, which counts
    any row with so much as one non-empty cell as part of the table --
    including a long stretch of otherwise-empty rows that carry nothing but
    an unchecked checkbox. That silently pushed new rows out past your real
    data, past what you'd ever scroll to see: postings kept matching and
    emailing correctly (main() never touches this function's result before
    the write, so the digest was never wrong) while the sheet itself
    appeared stuck. Computing the target row explicitly from A-G sidesteps
    the heuristic entirely.
    """
    values = _gspread_retry(ws.get_all_values)
    last = 0
    for i, row in enumerate(values, start=1):
        if any((row[j] if j < len(row) else "") for j in range(7)):  # A-G
            last = i
    return last + 1


# ===========================================================================
# Page watch -- non-job pages you want to hear about the instant they change,
# checked on the same run as the job scrape so "asap" means "within 20 min"
# rather than needing a second workflow.
# ===========================================================================
# Each entry is fetched, stripped to plain text, and sliced to the window
# between "start" and "end" (case-sensitive, first match of each -- see
# fetch_watch_text). That scoping is what lets this watch a single card on a
# page with several --
# Synchrony's /programs/ page also has an Externship and a Summer Internship
# card, and without scoping, an edit to either of those would trigger a false
# alert for the Full-Time BLP watch. Omit "end" to watch to the end of the
# page; omit both to watch the whole page.
PAGE_WATCHES = [
    {
        "label": "Synchrony Full-Time BLP",
        "url": "https://www.synchronyuniversity.com/programs/",
        "start": "FULL-TIME BLP",
        "end": "Summer Internship",
    },
]
PAGE_WATCH_WORKSHEET = "Page Watch"
PAGE_WATCH_HEADER = ["Label", "URL", "Last checked", "Last changed", "Snapshot"]


def fetch_watch_text(w):
    """Fetch a PAGE_WATCHES entry and return its watched window as plain text.

    Returns None when "start" is given but not found on the page -- a fetch
    hiccup (an error page, a loading skeleton) or a site redesign that moved
    the marker. Either way the honest move is to skip the comparison for this
    run rather than diff against a garbage snapshot and fire a false alert.
    """
    r = requests.get(w["url"], timeout=25, headers=REQ_HEADERS)
    r.raise_for_status()
    r.encoding = r.apparent_encoding or "utf-8"
    text = _strip_html(r.text)
    start = w.get("start")
    if not start:
        return text
    # Case-SENSITIVE on purpose: Synchrony's page also mentions "the Full-Time
    # BLP" in title case in its intro paragraph, well before the all-caps
    # "FULL-TIME BLP" card header this watch actually wants. A case-insensitive
    # match would lock onto that first, unrelated mention instead.
    m = re.search(re.escape(start), text)
    if not m:
        return None
    window = text[m.start():]
    end = w.get("end")
    if end:
        m2 = re.search(re.escape(end), window[len(start):])
        if m2:
            window = window[:len(start) + m2.start()]
    return window.strip()


def get_page_watch_worksheet(sh, create=True):
    """Open the page-watch worksheet, creating it and its header if missing.
    Mirrors get_worksheet()'s create=False contract for --dry-run."""
    try:
        ws = _gspread_retry(sh.worksheet, PAGE_WATCH_WORKSHEET)
    except gspread.WorksheetNotFound:
        if not create:
            return None
        ws = _gspread_retry(sh.add_worksheet, title=PAGE_WATCH_WORKSHEET, rows=100,
                            cols=len(PAGE_WATCH_HEADER))
        _gspread_retry(ws.append_row, PAGE_WATCH_HEADER, value_input_option="USER_ENTERED")
    return ws


def check_page_watches(dry_run=False):
    """Check every PAGE_WATCHES entry against its last known snapshot and
    email when one has changed. A watch with no prior snapshot (first run, or
    a newly added entry) just records a baseline -- there is nothing to alert
    on yet, since nothing has actually changed.
    """
    if not PAGE_WATCHES:
        return
    sh = get_spreadsheet()
    ws = get_page_watch_worksheet(sh, create=not dry_run)
    rows = _gspread_retry(ws.get_all_values) if ws else []
    existing = {r[0]: r for r in rows[1:] if r}  # label -> row
    today = datetime.date.today().isoformat()

    for w in PAGE_WATCHES:
        label = w["label"]
        try:
            text = fetch_watch_text(w)
        except Exception as e:
            print(f"[warn] page watch '{label}' failed: {e}")
            continue
        if text is None:
            print(f"[warn] page watch '{label}': marker not found on page, "
                  f"skipping this run")
            continue

        prev = existing.get(label)
        prev_snapshot = prev[4] if prev and len(prev) > 4 else None
        changed = prev_snapshot is not None and prev_snapshot != text

        if prev_snapshot is None:
            print(f"[page watch] '{label}': establishing baseline, no alert.")
        elif changed:
            print(f"[page watch] '{label}': CHANGED.")
            if not dry_run:
                try:
                    send_page_watch_email(label, w["url"], text)
                except Exception as e:
                    print(f"[warn] page watch email failed: {e}")
        else:
            print(f"[page watch] '{label}': no change.")

        if dry_run:
            continue
        last_changed = today if changed else (prev[3] if prev and len(prev) > 3 else "")
        new_row = [label, w["url"], today, last_changed, text]
        if prev:
            cell = _gspread_retry(ws.find, label, in_column=1)
            _gspread_retry(ws.update, f"A{cell.row}:E{cell.row}", [new_row],
                           value_input_option="USER_ENTERED")
        else:
            _gspread_retry(ws.append_row, new_row, value_input_option="USER_ENTERED")


def send_page_watch_email(label, url, snapshot):
    if not (SMTP_USER and SMTP_PASS and EMAIL_TO):
        print("[email] SMTP not configured, skipping page-watch email.")
        return
    msg = EmailMessage()
    msg["Subject"] = f"Page changed: {label}"
    msg["From"] = SMTP_USER
    msg["To"] = EMAIL_TO
    msg.set_content(f"{label} changed:\n{url}\n\n{snapshot}")
    msg.add_alternative(
        f"<h3>{html.escape(label)} changed</h3>"
        f"<p><a href='{html.escape(url)}'>{html.escape(url)}</a></p>"
        f"<pre style='white-space:pre-wrap'>{html.escape(snapshot)}</pre>",
        subtype="html",
    )
    ctx = ssl.create_default_context()
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
        s.starttls(context=ctx)
        s.login(SMTP_USER, SMTP_PASS)
        s.send_message(msg)
    print(f"[email] Sent page-watch alert for '{label}' to {EMAIL_TO}.")


# ===========================================================================
# Email digest
# ===========================================================================
def send_email(rows, category_label="product"):
    """category_label customizes the subject/heading text only (e.g. "Jobs
    2.0" for the second tab's digest) -- every existing call site omits it
    and keeps the original "product" wording unchanged."""
    if not (SMTP_USER and SMTP_PASS and EMAIL_TO):
        print("[email] SMTP not configured, skipping email.")
        return
    # Rows arrive already ranked by _write_new_rows (priority location, then
    # early-career, then core title, then technical relevance). This used to
    # re-sort by priority + core title, which would now undo the early-career
    # ordering, so it keeps the caller's order instead.
    ordered = list(rows)
    n_prio = sum(1 for r in rows if r[PRIORITY_IDX] == "Yes")
    lines = [f"{len(rows)} new role(s) ({n_prio} in your preferred locations):\n"]
    html_items = []
    # A first run after adding sources can find hundreds. The best-ranked
    # MAX_EMAIL_ROWS are listed; the rest are all in the sheet.
    hidden = max(0, len(ordered) - MAX_EMAIL_ROWS)
    for _added, posted, prio, company, title, location, url, _a in ordered[:MAX_EMAIL_ROWS]:
        star = "\u2b50 " if prio == "Yes" else ""
        when = f", posted {posted}" if posted else ""
        lines.append(f"- {star}{company}: {title} ({location}{when})\n  {url}")
        badge = ("<span style='color:#c47f00'>\u2b50 priority</span> " if prio == "Yes" else "")
        html_items.append(
            f"<li>{badge}<b>{html.escape(company)}</b>: "
            f"<a href='{html.escape(url)}'>{html.escape(title)}</a> "
            f"<span style='color:#666'>{html.escape(location)}{html.escape(when)}</span></li>"
        )
    more_text = (f"\n...and {hidden} more -- all of them are in the sheet." if hidden else "")
    more_html = (f"<p><b>...and {hidden} more</b> -- all of them are in the sheet.</p>" if hidden else "")
    msg = EmailMessage()
    msg["Subject"] = f"{len(rows)} new {category_label} role(s), {n_prio} priority"
    msg["From"] = SMTP_USER
    msg["To"] = EMAIL_TO
    msg.set_content("\n".join(lines) + more_text)
    msg.add_alternative(
        f"<h3>{len(rows)} new {html.escape(category_label)} role(s)</h3>"
        f"<p>{n_prio} in your preferred locations (shown first).</p>"
        f"<ul>{''.join(html_items)}</ul>{more_html}",
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
def _write_new_rows(ws, rows, relevance_by_url, dry_run, tab_label, email_label):
    """Sort, then either print (--dry-run) or write to `ws` and email a
    digest. Shared by both the "Jobs" and "Jobs 2.0" tabs -- identical logic,
    just pointed at a different worksheet/row-set/email label."""
    if not rows:
        return
    # Priority location first, then roles that read early-career (new grad,
    # university program, associate, analyst, coordinator, 0-2 years -- see
    # early_career_score), then core product titles (a no-op tiebreaker for
    # Jobs 2.0 rows, which never match TITLE_INCLUDE_CORE), then roles
    # leaning technical/analytical over purely relationship-management --
    # see technical_relevance_score(). All four are ranking only; nothing
    # here was excluded by this sort.
    def _rank(r):
        early, tech = relevance_by_url.get(r[URL_IDX], (0, 0))
        return (r[PRIORITY_IDX] != "Yes", -early,
                not is_core_title(r[TITLE_IDX]), -tech)
    rows.sort(key=_rank)
    if dry_run:
        print(f"\n--- would add ({tab_label}) ---")
        for _added, posted, prio, company, title, location, url, _a in rows:
            star = "* " if prio == "Yes" else "  "
            when = f", posted {posted}" if posted else ""
            print(f"{star}{company}: {title} ({location}{when})\n    {url}")
    else:
        row = _next_data_row(ws)
        _gspread_retry(ws.update, rows, f"A{row}", value_input_option="USER_ENTERED")
        try:
            send_email(rows, category_label=email_label)
        except Exception as e:
            print(f"[warn] email failed ({tab_label}): {e}")


# ---------------------------------------------------------------------------
# Dedup keys: the same posting reaches us from several places (a company's own
# board, the new-grad feeds, apmlist, jobright) under different URLs, and a
# raw-string URL match treats those as different jobs. _url_key reduces a URL
# to the posting's identity so a job already in the sheet isn't added again
# just because a different source linked it differently.
# ---------------------------------------------------------------------------
_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def _url_key(url):
    u = (url or "").strip().lower()
    u = re.sub(r"^https?://(www\.)?", "", u).split("#")[0]
    m = re.search(r"[?&]gh_jid=(\d+)", u)
    if m:
        return "gh:" + m.group(1)
    host, _, rest = u.partition("/")
    path = rest.split("?")[0].rstrip("/")
    if host.endswith("greenhouse.io"):
        m = re.search(r"jobs/(\d+)", path)
        if m:
            return "gh:" + m.group(1)
    if host in ("jobs.lever.co", "jobs.ashbyhq.com"):
        m = _UUID_RE.search(path)
        if m:
            return host.split(".")[1] + ":" + m.group(0)
    path = re.sub(r"/apply$", "", path)
    # A Workday posting is identified by its /job/... path; the same posting
    # can be reachable under two career sites of one tenant (an external site
    # and a campus site), so the site name must not be part of the key.
    if "myworkday" in host:
        m = re.search(r"/job/.+$", "/" + path)
        if m:
            return "wd:" + host.split(".")[0] + m.group(0)
    path = re.sub(r"^[a-z]{2}-[a-z]{2}/", "", path)  # locale prefix (Workday's /en-US/)
    return host + "/" + path


def _norm_company(name):
    n = re.sub(r"[^a-z0-9 ]", "", (name or "").lower())
    n = re.sub(r"\b(inc|llc|corp|corporation|co|ltd|the|company)\b", "", n)
    return re.sub(r"\s+", "", n)


def _job_sig(company, title, location):
    """(company, title, city) -- used ONLY to catch an aggregator row (a feed
    or list) repeating a job a company's own board already supplied; two real
    postings from the same board are never merged on this."""
    city = re.split(r"[,;]", location or "")[0]
    return (_norm_company(company), re.sub(r"[^a-z0-9]", "", (title or "").lower()),
            re.sub(r"[^a-z0-9]", "", city.lower()))


AGGREGATOR_ATS = {"newgrad_feed", "apmlist", "jobright"}
MAX_EMAIL_ROWS = 150


# ---------------------------------------------------------------------------
# "Seen" worksheet: postings already evaluated and rejected, so an hourly run
# doesn't re-download (and re-reject) the same few thousand descriptions every
# time. Keyed to a hash of this file: ANY change to the code -- a new filter,
# a loosened rule, a new company -- changes the hash and makes every old
# rejection invisible, so a rule change can never leave a job hidden by a
# verdict an older version of the rules reached.
# ---------------------------------------------------------------------------
SEEN_WORKSHEET = "Seen"
SEEN_HEADER = ["Key", "Rules hash", "Date"]


def _rules_hash():
    import hashlib
    h = hashlib.sha1()
    with open(os.path.abspath(__file__), "rb") as f:
        h.update(f.read())
    # Which companies get the Jobs 2.0 "any role" treatment lives in the
    # source list, not in this file -- a change there changes verdicts too.
    h.update(",".join(sorted(ROBOTICS_COMPANY_NAMES)).encode())
    return h.hexdigest()[:10]


def _load_seen(sh, create):
    """Returns (worksheet|None, set of rejected keys under the CURRENT rules,
    whether the sheet holds rows from other rule versions)."""
    try:
        ws = _gspread_retry(sh.worksheet, SEEN_WORKSHEET)
    except gspread.WorksheetNotFound:
        if not create:
            return None, set(), False
        ws = _gspread_retry(sh.add_worksheet, title=SEEN_WORKSHEET, rows=1000, cols=len(SEEN_HEADER))
        _gspread_retry(ws.append_row, SEEN_HEADER, value_input_option="RAW")
        return ws, set(), False
    rules = _rules_hash()
    keys, other = set(), False
    for row in _gspread_retry(ws.get_all_values)[1:]:
        if len(row) < 2 or not row[0]:
            continue
        if row[1] == rules:
            keys.add(row[0])
        else:
            other = True
    return ws, keys, other


def _save_seen(ws, current_keys, new_keys, had_other_versions):
    today = datetime.date.today().isoformat()
    rules = _rules_hash()
    if had_other_versions:
        # Rules changed since the last run: rewrite the sheet with only the
        # current version rather than let dead rows pile up forever.
        rows = [SEEN_HEADER] + [[k, rules, today] for k in sorted(current_keys | new_keys)]
        _gspread_retry(ws.clear)
        for i in range(0, len(rows), 5000):
            chunk = rows[i:i + 5000]
            _gspread_retry(ws.update, chunk, f"A{i + 1}", value_input_option="RAW")
    elif new_keys:
        rows = [[k, rules, today] for k in sorted(new_keys)]
        for i in range(0, len(rows), 5000):
            _gspread_retry(ws.append_rows, rows[i:i + 5000], value_input_option="RAW")


def _iter_sources(chunk=40):
    """Yield (company, jobs|None, error) for every source: fetched concurrently,
    a chunk at a time, with each chunk's postings released once the caller
    moves on (about 2,000 sources hold far too much to keep in memory at
    once). Aggregators (feeds/lists) come LAST: they repeat jobs that company
    boards also carry, and the dedup is only reliable if the board's copy was
    seen first."""
    import concurrent.futures as cf

    def one(c):
        fetcher = FETCHERS.get(c["ats"])
        if not fetcher:
            return c, None, f"unknown ats '{c['ats']}'"
        try:
            return c, fetcher(c), None
        except Exception as e:
            return c, None, str(e).split("\n")[0][:120]

    ordered = ([c for c in COMPANIES if c["ats"] not in AGGREGATOR_ATS]
               + [c for c in COMPANIES if c["ats"] in AGGREGATOR_ATS])
    with cf.ThreadPoolExecutor(max_workers=12) as ex:
        for i in range(0, len(ordered), chunk):
            for result in list(ex.map(one, ordered[i:i + chunk])):
                yield result


def _prefetch_descriptions(jobs, workers=16):
    """Fill in missing descriptions concurrently (these are one HTTP request
    each, and a first run can need thousands)."""
    import concurrent.futures as cf
    need = [j for j in jobs if not (j.get("description") or "").strip()]
    if not need:
        return
    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        for j, d in zip(need, ex.map(lambda j: _fetch_description(j["url"]), need)):
            j["description"] = d


def main(dry_run=False):
    ws = get_worksheet(create=not dry_run)
    ws_v2 = get_worksheet(create=not dry_run, name=WORKSHEET_NAME_V2)
    sh = get_spreadsheet()
    seen_ws, seen_rejected, seen_had_other = _load_seen(sh, create=not dry_run)
    seen_current = set(seen_rejected)
    newly_rejected = set()

    existing_urls, existing_keys, existing_sigs = set(), set(), set()
    for w in (ws, ws_v2):
        if w is None:
            continue
        for row in _gspread_retry(w.get_all_values)[1:]:
            if len(row) > URL_IDX and row[URL_IDX]:
                existing_urls.add(row[URL_IDX])
                existing_keys.add(_url_key(row[URL_IDX]))
                if len(row) > 5:
                    existing_sigs.add(_job_sig(row[3], row[4], row[5]))
    if ws is None:
        print("[dry-run] Worksheet does not exist yet; treating it as empty.")
    today = datetime.date.today().isoformat()
    new_rows = []
    new_rows_v2 = []
    # Row tuples don't carry description text, so technical_relevance_score()
    # -- needed for sorting -- is stashed here by URL and looked up at sort
    # time instead. Separate per tab since a URL can only ever land in one.
    relevance_by_url = {}
    relevance_by_url_v2 = {}

    failed = []
    unresolved_jobright = []
    # Every posting read from a company's own board this run, by title -- how a
    # jobright row is matched to the employer's real link (see
    # _resolve_direct_link). Aggregators are read last, so it is complete by then.
    title_index = {}
    for c, jobs, err in _iter_sources():
        if err is not None:
            print(f"[warn] {c['name']} ({c['ats']}) failed: {err}")
            failed.append((c["name"], err))
            continue

        # Microsoft's own fetcher doesn't set "company" (single-company
        # sources never do), but passes_title() needs it to scope the bare
        # "program manager" carve-out to Microsoft only. Scoped to this one
        # ats so no other source's jobs gain a "company" field they didn't
        # already have.
        if c["ats"] == "microsoft":
            for j in jobs:
                j["company"] = c["name"]

        # True for a company tagged "robotics": True in COMPANIES -- see
        # ROBOTICS_COMPANY_NAMES and classify_job() for the Jobs 2.0 "any
        # role" exception this feeds.
        is_robotics = bool(c.get("robotics"))
        is_aggregator = c["ats"] in AGGREGATOR_ATS
        if not is_aggregator:
            for j in jobs:
                if j.get("url"):
                    title_index.setdefault(_title_key(j.get("title")), []).append(
                        (_norm_company(j.get("company") or c["name"]), j.get("location") or "", j["url"],
                         j.get("posted") or ""))

        # ---- cheap gates: title, location, freshness, already-known ----
        candidates = []
        for j in jobs:
            url = j.get("url") or ""
            if not url or url in existing_urls:
                continue
            key = _url_key(url)
            if key in existing_keys or key in seen_rejected:
                continue
            company = (j.get("company") or "").strip() or c["name"]
            # A list/feed row repeating a job a company board already gave us.
            if is_aggregator and _job_sig(company, j.get("title"), j.get("location")) in existing_sigs:
                continue
            # An aggregator row (New-Grad Feed) names its own employer per
            # row, which can itself be a tracked robotics/IoT company even
            # though the source (the feed) isn't one.
            job_is_robotics = is_robotics or (
                (j.get("company") or "") in ROBOTICS_COMPANY_NAMES
            )
            # Gate on title and location first: both are free, and this keeps
            # the fetch below to the handful of roles that could still
            # qualify for either tab.
            if not passes_title_any(j, is_robotics=job_is_robotics) or is_non_us(j.get("location")):
                continue
            # Also free: drop it if the source's own date already says it's
            # old, before spending a request on its description. See
            # MAX_POSTING_AGE_DAYS for exactly which sources this applies to.
            posted = _norm_date(j.get("posted"))
            # Programs keep their original posted date for months while still
            # open -- exempt from the freshness limit (see _is_program_title).
            program = _is_program_title(j.get("title"))
            if is_stale(posted, j.get("title")) and not program:
                continue
            # Only a posting whose description had to be DOWNLOADED is worth
            # remembering as rejected -- that download is what the Seen list
            # exists to avoid repeating. One that arrived with its text costs
            # nothing to re-judge, so it isn't stored.
            needed_fetch = not (j.get("description") or "").strip()
            candidates.append((j, key, company, job_is_robotics, posted, program, needed_fetch))

        # A listing with no description can't be judged on experience, so go
        # get one (concurrently). Without this, description-less sources
        # bypass the experience filter entirely (see _fetch_description).
        _prefetch_descriptions([x[0] for x in candidates])

        kept = 0
        kept_v2 = 0
        for j, key, company, job_is_robotics, posted, program, needed_fetch in candidates:
            url = j["url"]
            desc = j.get("description") or ""
            # No description even after the fetch: don't judge it on nothing
            # and don't remember it -- the next run tries again.
            fetched = bool(desc.strip()) and needed_fetch
            if is_closed_posting(desc):
                if fetched:
                    newly_rejected.add(key)
                continue
            if _extract_non_us_country(desc):
                if fetched:
                    newly_rejected.add(key)
                continue
            # The source gave no "posted" date (Workday; also the New-Grad
            # Feed before its real page was fetched above) -- try the
            # JSON-LD fallback now that a description exists, and re-check
            # staleness with it. See _extract_json_ld_date and
            # MAX_POSTING_AGE_DAYS.
            if not posted:
                extracted = _extract_json_ld_date(desc)
                if extracted:
                    posted = extracted
                    if is_stale(posted, j.get("title")) and not program:
                        if fetched:
                            newly_rejected.add(key)
                        continue
            category = classify_job(j, is_robotics=job_is_robotics)
            if category is None:
                if fetched:
                    newly_rejected.add(key)
                continue
            if _JOBRIGHT_URL_RE.search(url):
                # Prefer the employer's own posting, and with it the employer's
                # real posted date -- jobright's listing time is when jobright
                # found the role, which can be well after the employer did.
                direct, real_posted = _resolve_direct_link(
                    company, j["title"], j["location"],
                    _JOBRIGHT_COMPANY_URL.get(url, ""), title_index)
                if fetched:
                    newly_rejected.add(key)   # the jobright page itself is done either way
                if direct:
                    url = direct
                    key = _url_key(url)
                    # The employer's own copy may already be in the sheet, or have
                    # been judged and rejected on its real description.
                    if url in existing_urls or key in existing_keys or key in seen_rejected or key in newly_rejected:
                        continue
                    real_posted = _norm_date(real_posted)
                    if real_posted:
                        posted = real_posted
                        if is_stale(posted, j.get("title")) and not program:
                            continue
                else:
                    unresolved_jobright.append((company, j["title"]))
            prio = "Yes" if is_priority_location(j.get("location")) else ""
            row = [today, posted, prio, company, j["title"], j["location"], url, ""]
            # (early-career score, technical relevance) -- see
            # _write_new_rows for how each is used in the sort.
            relevance = (early_career_score(j["title"], desc),
                         technical_relevance_score(desc))
            if category == "core":
                new_rows.append(row)
                relevance_by_url[url] = relevance
                kept += 1
            else:
                new_rows_v2.append(row)
                relevance_by_url_v2[url] = relevance
                kept_v2 += 1
            existing_urls.add(url)
            existing_keys.add(key)
            existing_sigs.add(_job_sig(company, j["title"], j["location"]))
        print(f"{c['name']}: {kept} new match(es), {kept_v2} Jobs 2.0 match(es)")

    _write_new_rows(ws, new_rows, relevance_by_url, dry_run, "Jobs", "product")
    _write_new_rows(ws_v2, new_rows_v2, relevance_by_url_v2, dry_run, "Jobs 2.0", "Jobs 2.0")
    if not dry_run and seen_ws is not None:
        try:
            _save_seen(seen_ws, seen_current, newly_rejected, seen_had_other)
        except Exception as e:
            print(f"[warn] could not save the Seen list: {e}")

    n_prio = sum(1 for r in new_rows if r[PRIORITY_IDX] == "Yes")
    n_prio_v2 = sum(1 for r in new_rows_v2 if r[PRIORITY_IDX] == "Yes")
    verb = "Would add" if dry_run else "Added"
    print(f"\nDone. {verb} {len(new_rows)} new job(s) ({n_prio} priority) to Jobs, "
          f"{len(new_rows_v2)} new job(s) ({n_prio_v2} priority) to Jobs 2.0.")
    if unresolved_jobright:
        print(f"\n[info] {len(unresolved_jobright)} jobright role(s) passed the filters but the "
              f"employer's own posting couldn't be found; they were added with the jobright link "
              f"and no posted date:")
        for co, t in unresolved_jobright[:40]:
            print(f"    - {co}: {t}")
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

    try:
        check_page_watches(dry_run=dry_run)
    except Exception as e:
        print(f"[warn] page watch check failed: {e}")


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
