# Sligo Creek Newsletter Scraper

Scrapes the Sligo Creek Smore newsletter flyer and emails you a digest of
the key info.

`newsletter_scraper.py` fetches a Smore flyer URL (default:
`https://app.smore.com/n/dpguw`), pulls out its content, and emails a
digest of the key info (headline, dates/deadlines, action items, links).

Two extraction paths are supported:

- **With `ANTHROPIC_API_KEY` set** — the raw scraped text is sent to Claude,
  which returns a clean, skimmable bullet-point digest. This is the
  recommended path since it doesn't depend on Smore's exact HTML/CSS
  structure, which can change or vary by flyer template.
- **Without it** — a simpler heuristic extractor pulls out de-duplicated
  text lines, anything that looks like a date/time, and outbound links.

The scraper also handles the common case where a Smore flyer's real content
is embedded as JSON in a `<script>` tag rather than plain server-rendered
HTML (typical of SPA-built pages) — it walks any JSON blobs it finds and
falls back to that text if it's richer than the visible DOM text.

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env   # then fill in SMTP + (optionally) ANTHROPIC_API_KEY
```

For Gmail, use an [App Password](https://myaccount.google.com/apppasswords)
as `SMTP_PASSWORD` — a normal account password will not work.

## Run it

```bash
# Preview the digest without sending an email
python newsletter_scraper.py --dry-run

# Scrape + send
set -a; source .env; set +a
python newsletter_scraper.py
```

## Scheduling it

`.github/workflows/newsletter_scraper.yml` runs the scraper on a daily
cron schedule (adjust the cron expression to match how often the
newsletter actually updates — Smore flyers are often weekly). To enable it
in GitHub Actions, set these in this repo's **Settings → Secrets and
variables → Actions**:

- Secrets: `SMTP_USERNAME`, `SMTP_PASSWORD`, and optionally
  `ANTHROPIC_API_KEY`
- Variables (or hardcode in the workflow): `NEWSLETTER_URL`, `EMAIL_TO`,
  `EMAIL_FROM`, `SMTP_HOST`, `SMTP_PORT`

You can also trigger a run manually from the Actions tab
(`workflow_dispatch`), or run it locally via cron/launchd if you'd rather
not use GitHub Actions.
