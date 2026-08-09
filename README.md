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
- DROP: roles asking for 5+ years of experience.
- DROP: Europe-only roles, remote or on-site. A role that also lists a US
  location is kept, so "San Francisco, New York, London" survives.
- Location is otherwise NOT a filter. Roles anywhere else (Canada, LATAM, APAC)
  are kept; preferred locations are just flagged and floated to the top.

Every rule is a plain list at the top of `scrape_jobs.py`
(`TITLE_INCLUDE`, `TITLE_EXCLUDE_WORDS`, `DESCRIPTION_EXCLUDE`,
`EXPERIENCE_EXCLUDE`, `LOCATION_PRIORITY`, `EUROPE_TERMS`, `US_TERMS`).
Edit freely.

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

| Source | What it is | Reliability | Config |
|--------|-----------|-------------|--------|
| **New-Grad Feed** | Maintained list across hundreds of companies (startups + big cos) with apply links. The wide net. | Solid | none |
| Greenhouse / Lever / Ashby | Named companies you want followed closely | Solid | `slug` |
| Workday | Salesforce, Adobe, Cisco, Nvidia, etc. | Good | `tenant`, `wd_host`, `site` |
| Amazon / Google / Microsoft | Their own search endpoints | Best-effort | none |

The New-Grad Feed does the broad "search everything" work. The per-company
sources add depth for places you specifically care about. Amazon/Google/Microsoft
endpoints are undocumented and may need occasional patching. Each source is
isolated: if one fails, the run logs a warning and continues.

## Filling in companies

### Greenhouse / Lever / Ashby
```bash
python find_source.py "Scale AI" "Vercel" "Plaid"
```
Paste the printed lines into `COMPANIES`.

### Workday
Open the careers page, read the URL `https://TENANT.wdN.myworkdayjobs.com/SITE`,
and fill `tenant` / `wd_host` (wdN) / `site` by hand.

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
