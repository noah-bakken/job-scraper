# Product-role tracker

Polls job sources every 20 minutes, keeps post-undergrad entry-level product
roles in your target cities, drops student-only and clearly-senior roles,
appends new hits to a Google Sheet, and emails you a digest.

## What it keeps and drops

- KEEP: PM, associate PM, APM, junior PM, product analyst, product owner,
  technical program manager, product ops. Entry level. Startups and big cos.
- KEEP: internships/co-ops open to graduates.
- KEEP: roles with blank/remote/unspecified locations (so nothing is lost).
- DROP: senior / staff / principal / lead / director and level II+.
- DROP: product *engineer* / *designer* roles (not product management).
- DROP: roles requiring current enrollment or returning to school.
- DROP: roles targeting a graduation window later than yours (`GRADUATED`,
  default May 2026). "You will graduate in Fall 2026 or Spring 2027" is closed
  to a Spring 2026 grad. This is a date comparison, **not** a ban on
  internships: internships that accept recent grads still come through.
- DROP: roles asking for more than `MAX_YEARS_EXPERIENCE` (default **0**) years
  of experience. Tuned for a May 2026 grad with internships but no full-time
  experience, so any posting wanting real full-time years ("1+ years", "3+
  years") is dropped, while "0-2 years" and internship-satisfiable requirements
  are kept.
- DROP: any role naming a country outside the US (`is_non_us`) — Europe,
  Canada, LATAM, APAC, Middle East. A role that also lists a US location is
  kept, so "San Francisco, New York, London" survives.
- A listing that names no country at all (blank, or a bare "Remote") is KEPT.
  The filter drops on a positive non-US signal rather than requiring a positive
  US one, because `US_TERMS` doesn't recognize "SF" and `US_STATE_CODES` omits
  ID/LA/IN/OR — requiring a US match would delete real US roles.
- Preferred US locations are flagged and floated to the top of the sheet.

Every rule is a plain list at the top of `scrape_jobs.py`
(`TITLE_INCLUDE`, `TITLE_EXCLUDE_WORDS`, `DESCRIPTION_EXCLUDE`,
`EXPERIENCE_EXCLUDE`, `LOCATION_PRIORITY`, `EUROPE_TERMS`, `US_TERMS`).
Edit freely.

### How the experience filter works
`years_required()` parses the years a posting demands instead of matching phrase
strings, so it handles "3+ years", "3-5 years", "at least three years" and
"minimum of 3 yrs" with one rule. Compare that against `MAX_YEARS_EXPERIENCE`
(default 0; set to `None` to disable).

Four deliberate choices:
- It takes the **maximum**, so "5+ years as a full-time PM, 2+ years of
  analytics" is judged on the 5 and a senior role can't sneak through on its
  smaller secondary number.
- A **range keeps its floor**, so "0-2 years" reads as 0 and survives. Ranges
  are matched before bare numbers and their span is then excluded from the bare
  pass, so the "2 years" tail can't be re-read and undo the floor.
- **Internship years don't count.** A requirement an internship satisfies ("1+
  years of internship experience", "1-2 years including internships") isn't a
  full-time-years requirement, so it's skipped rather than counted.
- **Company self-description doesn't count.** "We have been building this for 10
  years" is history, not a requirement. This is the `been ... for N years` shape
  specifically; "we are looking for 3+ years of experience" still counts.
- **Age questions don't count.** "Are you at least 18 years of age?" is an
  application-form field phrased exactly like a requirement. Common once full
  posting pages are fetched, and it scored those roles as needing 18 years.
- **Encoding is normalized first.** A page served as UTF-8 but decoded as
  Latin-1 turns "0–2 years" into mojibake; the range then fails to match, the
  bare "2 years" tail is read instead, and a posting advertising itself as
  entry level scores as a 2-year requirement.

It also skips retrospective phrasing ("over the past 5 years we have grown",
"founded 10 years ago") and requires a bare number to sit near a word like
*experience* or *background*, so a "5 year vision" isn't mistaken for a
requirement.

At a ceiling of 0 the filter is strict by construction: every stated full-time
year drops the posting, so a description that mentions years in some phrasing
not covered above will over-drop. Raise `MAX_YEARS_EXPERIENCE` to 1 or 2 if the
sheet starts looking thin.

**Sources that ship no description get one fetched.** Workday and the New-Grad
Feed both list roles with no description, which used to blind this filter
entirely: `years_required()` saw nothing, returned `None`, and the role was
kept regardless of what it actually required. That made hand-deleting such
roles from the sheet pointless — they came back on the very next run. `main()`
now calls `_fetch_description()` for any listing that arrives without one, but
only after the free title and location gates, so it costs a handful of requests
per run rather than thousands.

### Graduation windows
`earliest_graduation_window()` pulls the graduation date a posting targets and
compares it to `GRADUATED` (default `(2026, 5)`). Seasons resolve to months
(spring→May, summer→July, fall→September), and a bare year resolves to January
as the most inclusive reading. It takes the **earliest** window mentioned, so
"December 2025 through June 2026" is judged on its opening edge.

A year only counts when graduation-ish words sit near it, so "founded in 2013"
and "our 2026 roadmap" are ignored. **A posting that names no window is kept** —
silence isn't a disqualification, which is what lets grad-friendly internships
through while "graduating in Fall 2027" gets dropped.

### How the Europe exclusion avoids false positives
`is_europe_only()` checks for a **US signal first** and only then tests for
Europe. That ordering is the whole trick: `Vienna, VA`, `Dublin, CA`,
`Cambridge, MA`, `Berlin, CT` and `Paris, TX` all match a US term and never
reach the Europe test, so listing bare city names in `EUROPE_TERMS` is safe.
**Don't reorder those two checks.** Two-letter codes match as whole words only,
and `US_STATE_CODES` deliberately omits codes that collide with European names
or English words (`DE`, `IN`, `OR`, `IT`, `ME`, `OK`, `HI`, `ID`, `LA`).
A blank or bare `Remote` location matches nothing and is kept.

### Locations (priority, not a filter)
Every role is kept regardless of location. The ones matching `LOCATION_PRIORITY`
(all of California via the "CA" state code, plus NYC, SF, LA, Chicago, Austin,
Boston, DC, Philadelphia, and remote) get a "Yes" in the sheet's **Priority**
column and are floated to the top of each run and each email digest. Short codes
(sf, nyc, ny, ca, dc) match as whole words so "ca" hits ", CA" but not "Canada".
Sort or filter the Priority column in the sheet to focus on your cities, or read
the top of each email where priority roles are listed first.

## Sources

100 sources, ~17,700 listings a run, about 5 minutes.

| Source | What it is | Reliability | Config |
|--------|-----------|-------------|--------|
| **New-Grad Feed** | Maintained list across hundreds of companies (startups + big cos) with apply links. The wide net. | Solid | none |
| Greenhouse / Lever / Ashby | 84 named companies, followed closely | Solid | `slug` |
| Workday | 12 big employers not on the above: Salesforce, Adobe, Nvidia, PayPal, eBay, Mastercard, Autodesk, Workday, T-Mobile, Zillow, Comcast, Target | Good | `tenant`, `wd_host`, `site` |
| Amazon / Google | Their own search endpoints | Best-effort | none |
| Microsoft | Broken upstream, see Known limits | Failing | none |

The named companies span AI/ML (Anthropic, OpenAI, Scale AI, Perplexity, Cohere,
Sierra, Harvey, Replit, ElevenLabs…), fintech (Stripe, Plaid, Chime, Affirm,
Mercury, Carta, Gusto, Block…), dev tools and infra (Figma, Databricks, Notion,
Vercel, Linear, Datadog, Cloudflare, Snowflake, MongoDB, GitLab, Palantir…),
consumer (Airbnb, Instacart, Lyft, Pinterest, Discord, Spotify, Roblox…), and
health (Oscar, Ro, Hims & Hers, Benchling…).

The New-Grad Feed does the broad "search everything" work. The per-company
sources add depth for places you specifically care about. Amazon/Google
endpoints are undocumented and may need occasional patching. Each source is
isolated: if one fails, the run logs a warning and continues.

**LinkedIn and Indeed are deliberately absent.** Indeed answers `403` to
automated requests and retired its public job API to partners only. LinkedIn's
guest search is reachable but its terms prohibit scraping, its cards carry no
description (which both the experience and graduation filters need, so every
card would need a second fetch — exactly the volume that trips its authwall),
and GitHub's datacenter IPs are the first thing it rate-limits. Both are mostly
indexes of the same Greenhouse/Lever/Ashby/Workday postings read directly above,
where the data is better: exact dates, direct apply links, full descriptions, no
ghost-job reposts. To cover companies posting *only* there, use their own job
alert emails as a separate channel.

## Filling in companies

### Greenhouse / Lever / Ashby
```bash
python find_source.py "Scale AI" "Vercel" "Plaid"
```
Paste the printed lines into `COMPANIES`.

**Confirm the board is who you think it is before pasting.** `find_source.py`
reports the first slug that returns postings, and a slug that resolves is not a
slug that belongs to your company: the greenhouse `disney` board is a stub whose
only listing is titled "MASTER TEMPLATE", lever `capital` is not Capital One,
and greenhouse `figure` is Figure Lending, not Figure AI. Open the printed board
and read a posting or two.

### Workday
Open the careers page, read the URL `https://TENANT.wdN.myworkdayjobs.com/SITE`,
and fill `tenant` / `wd_host` (wdN) / `site` by hand. If the careers page is a
JavaScript app that never shows that URL, POST to the API directly to confirm a
guess — that is the exact call the scraper makes:
```bash
curl -s -X POST -H 'Content-Type: application/json' \
  -d '{"appliedFacets":{},"limit":20,"offset":0,"searchText":"product manager"}' \
  https://TENANT.wdN.myworkdayjobs.com/wday/cxs/TENANT/SITE/jobs | head -c 400
```
Real `jobPostings` in the response means the combination is right.

## One-time setup

1. **Google Sheet** named **Job Tracker**, left blank. Copy its ID out of the
   URL (`/spreadsheets/d/<ID>/edit`) and set `SHEET_ID`. Without `SHEET_ID` the
   script falls back to looking the sheet up by name, which requires the
   **Google Drive API** to be enabled on the Cloud project as well.
2. **Service account**: Google Cloud project, enable Google Sheets API, create a
   service account + JSON key, then share the sheet with the account's
   `client_email` as Editor.
3. **Gmail app password**: turn on 2-Step Verification, create an App Password;
   that value is `SMTP_PASS`.
4. **Push to GitHub**, then add repo secrets under Settings → Secrets and
   variables → Actions:

   | Secret | Value |
   |--------|-------|
   | `GOOGLE_CREDENTIALS` | the whole service-account JSON |
   | `SHEET_ID` | the sheet's ID from its URL (`/spreadsheets/d/<ID>/edit`) |
   | `SMTP_USER` | the Gmail you send from |
   | `SMTP_PASS` | the app password |
   | `EMAIL_TO` | where digests go |

5. Runs every 20 minutes automatically. Test now via Actions → Scrape jobs →
   Run workflow. Email only sends when there's something new.

### Verifying the email actually works
Because the digest only goes out when a run finds something new, a wrong
`SMTP_PASS` or `EMAIL_TO` stays invisible while the sheet happens to be current
— and a send failure is caught and logged as a warning, so the run still passes.
To test the channel on demand, run Actions → Scrape jobs → Run workflow with
**Send a sample digest** checked (or `python scrape_jobs.py --test-email`
locally). It sends one fake role and scrapes nothing.

A quiet inbox is usually not a broken scraper. Every posting is deduped on URL,
so once the sheet is caught up you only hear about genuinely new listings —
which, at `MAX_YEARS_EXPERIENCE = 0`, is a slow trickle. Check the Actions log:
`Added 0 new job(s)` with sources reporting matches means it's working.

## Cost and cadence
Free. Make the repo **public** for unlimited Actions minutes (secrets stay
private either way). 20 minutes keeps worst-case detection well under an hour,
which hourly can't guarantee once you account for scheduler delays.

## Run locally
```bash
pip install -r requirements.txt
export GOOGLE_APPLICATION_CREDENTIALS=/path/to/service-account.json
export SHEET_ID=your_sheet_id_from_the_url
export SMTP_USER=you@gmail.com SMTP_PASS=app_password EMAIL_TO=you@gmail.com
python scrape_jobs.py
```

## Known limits
- First run will populate a batch of currently-open matches; after that, only
  genuinely new postings are added (deduped on URL).
- **Date posted** quality varies by source, and is blank when a source gives
  nothing usable rather than being filled with a guess. Greenhouse
  (`first_published`) and Ashby (`publishedAt`) are exact. Amazon
  (`posted_date`) is day-accurate. The New-Grad Feed's `date_posted` is when the
  *feed* picked the job up, which can lag the company's own posting. Workday is
  always blank: it only exposes relative text like "Posted 5 Days Ago". So sort
  on it loosely, and treat a blank as "unknown", not "old".
- A **stale-looking Date posted doesn't mean a stale job.** Companies that
  recycle one requisition every hiring cycle keep their original
  `first_published`, so a currently-open Databricks "Summer 2027" internship
  reads as posted 2023-08-17. Switch the greenhouse adapter to `updated_at` if
  you'd rather the column track recency than true first-posting.
- Workday listings have no description, so the enrollment and experience filters
  are title-only there.
- Filters are keyword-based. If a good role gets dropped, remove the phrase that
  caught it; if noise gets in, add a phrase.
- `apm` and `tpm` in `TITLE_INCLUDE` are matched as whole words, and a title
  that matched on nothing but one of those acronyms is rejected if it also
  contains an `ACRONYM_FALSE_FRIENDS` word. APM is Application Performance
  Monitoring at Datadog, Grafana, Sentry, Elastic and Confluent, and TPM is
  Trusted Platform Module on hardware boards — without the guard, "Manager I,
  Engineering - APM Serverless" reads as an APM role.
