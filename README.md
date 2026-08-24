# Sligo Creek Newsletter Scraper

Finds the latest Sligo Creek newsletter and emails you a digest of the key
info — automatically, on a schedule.

## Why it reads your inbox

Each newsletter issue is a separate Smore flyer with its own URL
(`app.smore.com/n/<slug>`) — the slug changes every time a new issue comes
out, so a hardcoded link goes stale after one issue. Instead, this script:

1. Logs into your inbox via IMAP and finds the most recent notification
   email from ParentSquare (`NEWSLETTER_SENDER`).
2. Extracts its "view newsletter" link — which is itself a tracking
   redirect that hops through one or more services before landing on the
   real Smore flyer — and follows that redirect chain to resolve the
   current `app.smore.com` URL.
3. Compares that issue's slug to the last one it processed (recorded in
   `.state/last_slug.txt`) so a scheduled run that finds nothing new is a
   silent no-op instead of re-sending the same digest.
4. Scrapes the flyer and emails you a digest of the key info
   (headline, dates/deadlines, action items, links).

Two extraction paths are supported for turning the raw scrape into a
digest:

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
cp .env.example .env   # then fill in SMTP/IMAP + (optionally) ANTHROPIC_API_KEY
```

For Gmail, use an [App Password](https://myaccount.google.com/apppasswords)
as `SMTP_PASSWORD` — a normal account password will not work. The same
App Password is reused for both sending (SMTP) and reading (IMAP), since
it's the same Gmail account.

## Run it

```bash
# Discover the latest issue from your inbox and preview the digest
python newsletter_scraper.py --dry-run

# Discover + send
set -a; source .env; set +a
python newsletter_scraper.py

# Skip inbox discovery and scrape a specific flyer URL directly (useful for
# testing)
python newsletter_scraper.py --url https://app.smore.com/n/xxxxx --dry-run

# Force a re-send even if this issue was already processed
python newsletter_scraper.py --force
```

## Scheduling it

`.github/workflows/newsletter_scraper.yml` runs the scraper on a daily
cron schedule. Because it only emails when the issue's slug is new, a
frequent schedule is safe even though the newsletter itself is published
less often — it's a no-op the rest of the time. The workflow also commits
`.state/last_slug.txt` back to the repo after a successful send, so the
"already processed" check persists across runs.

To enable it in GitHub Actions, set these in this repo's **Settings →
Secrets and variables → Actions**:

- Secrets: `SMTP_USERNAME`, `SMTP_PASSWORD`, and optionally
  `ANTHROPIC_API_KEY`
- Variables (or hardcode in the workflow): `NEWSLETTER_SENDER`, `EMAIL_TO`,
  `EMAIL_FROM`, `SMTP_HOST`, `SMTP_PORT`

You can also trigger a run manually from the Actions tab
(`workflow_dispatch`), or run it locally via cron/launchd if you'd rather
not use GitHub Actions.
