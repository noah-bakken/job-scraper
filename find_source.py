#!/usr/bin/env python3
"""
Find the job source for a company.

Probes Greenhouse, Lever, and Ashby with likely slugs derived from a company
name and prints the ones that return real postings, formatted so you can paste
them straight into COMPANIES in scrape_jobs.py.

Usage:
    python find_source.py "Scale AI" "Vercel" "Anthropic"
    python find_source.py            # falls back to the COMPANIES_TO_CHECK list

Workday can't be brute-forced this way. For Workday companies, open the
company's careers page and read the URL:
    https://TENANT.wdN.myworkdayjobs.com/SITE
and fill tenant / wd_host (wdN) / site into scrape_jobs.py by hand.
"""

import sys
import re
import requests

COMPANIES_TO_CHECK = [
    "Scale AI", "Vercel", "Stripe", "Airtable", "Plaid",
]

HEADERS = {"User-Agent": "Mozilla/5.0 (source-finder)"}


def slug_candidates(name):
    base = name.lower().strip()
    base = re.sub(r"[^a-z0-9 ]", "", base)          # drop punctuation
    compact = base.replace(" ", "")                  # "scaleai"
    hyphen = base.replace(" ", "-")                  # "scale-ai"
    first = base.split(" ")[0]                        # "scale"
    # De-duplicate while preserving order.
    out = []
    for s in (compact, hyphen, first):
        if s and s not in out:
            out.append(s)
    return out


def try_greenhouse(slug):
    r = requests.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs",
                     timeout=15, headers=HEADERS)
    if r.status_code == 200:
        n = len(r.json().get("jobs", []))
        if n:
            return n
    return None


def try_lever(slug):
    r = requests.get(f"https://api.lever.co/v0/postings/{slug}?mode=json",
                     timeout=15, headers=HEADERS)
    if r.status_code == 200:
        data = r.json()
        if isinstance(data, list) and data:
            return len(data)
    return None


def try_ashby(slug):
    r = requests.get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}",
                     timeout=15, headers=HEADERS)
    if r.status_code == 200:
        n = len(r.json().get("jobs", []))
        if n:
            return n
    return None


PROBES = [("greenhouse", try_greenhouse), ("lever", try_lever), ("ashby", try_ashby)]


def find(name):
    for slug in slug_candidates(name):
        for ats, probe in PROBES:
            try:
                n = probe(slug)
            except Exception:
                n = None
            if n:
                return ats, slug, n
    return None


def main():
    names = sys.argv[1:] or COMPANIES_TO_CHECK
    for name in names:
        hit = find(name)
        if hit:
            ats, slug, n = hit
            print(f'{{"name": "{name}", "ats": "{ats}", "slug": "{slug}"}},  # {n} postings')
        else:
            print(f'# {name}: not found on greenhouse/lever/ashby (may use Workday or a custom site)')


if __name__ == "__main__":
    main()
