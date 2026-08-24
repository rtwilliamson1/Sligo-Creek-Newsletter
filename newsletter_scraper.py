#!/usr/bin/env python3
"""
Scrape a Smore newsletter (or similar single-page flyer) and email the key
info to yourself.

Usage:
    python newsletter_scraper.py
    python newsletter_scraper.py --url https://app.smore.com/n/dpguw --dry-run

Configuration is read from environment variables (see .env.example):
    NEWSLETTER_URL      Smore flyer URL to scrape (default: the one below)
    EMAIL_TO            Recipient address (default: rtwilliamson@gmail.com)
    EMAIL_FROM          "From" address (defaults to SMTP_USERNAME)
    SMTP_HOST           SMTP server, e.g. smtp.gmail.com
    SMTP_PORT           SMTP port, e.g. 587
    SMTP_USERNAME        SMTP login
    SMTP_PASSWORD       SMTP password / app password
    ANTHROPIC_API_KEY   Optional. If set, Claude is used to turn the raw
                         scraped text into a clean "key info" digest. If
                         unset, a simple heuristic extractor is used instead.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import smtplib
import sys
from dataclasses import dataclass, field
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import requests
from bs4 import BeautifulSoup

DEFAULT_URL = "https://app.smore.com/n/dpguw"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


@dataclass
class ScrapedNewsletter:
    url: str
    title: str
    description: str
    raw_text: str
    links: list[tuple[str, str]] = field(default_factory=list)  # (text, href)
    images: list[str] = field(default_factory=list)  # alt text / captions


def fetch_html(url: str, timeout: int = 20) -> str:
    resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=timeout)
    resp.raise_for_status()
    return resp.text


def _extract_embedded_json_text(soup: BeautifulSoup) -> str:
    """Smore (and many SPA-built sites) embed page content as JSON inside a
    <script> tag rather than plain HTML. Pull any string-like values out of
    those blobs so we don't miss content that isn't server-rendered."""
    chunks: list[str] = []
    for script in soup.find_all("script"):
        text = script.string or script.get_text() or ""
        if not text or ("{" not in text and "[" not in text):
            continue
        # Try to find a JSON object/array anywhere in the script body.
        for match in re.finditer(r"[\{\[].*[\}\]]", text, re.DOTALL):
            candidate = match.group(0)
            try:
                data = json.loads(candidate)
            except (json.JSONDecodeError, ValueError):
                continue
            chunks.extend(_walk_json_strings(data))
    return "\n".join(chunks)


def _walk_json_strings(node, depth: int = 0) -> list[str]:
    if depth > 12:
        return []
    out: list[str] = []
    if isinstance(node, str):
        s = node.strip()
        # Skip URLs, hex colors, class-name-ish tokens, and other noise.
        if len(s) >= 3 and not s.startswith(("http://", "https://", "#", "data:")):
            out.append(s)
    elif isinstance(node, dict):
        for v in node.values():
            out.extend(_walk_json_strings(v, depth + 1))
    elif isinstance(node, list):
        for v in node:
            out.extend(_walk_json_strings(v, depth + 1))
    return out


def scrape(url: str) -> ScrapedNewsletter:
    html = fetch_html(url)
    soup = BeautifulSoup(html, "html.parser")

    def meta(name: str, prop: bool = False) -> str:
        tag = soup.find("meta", attrs={"property" if prop else "name": name})
        return (tag.get("content") or "").strip() if tag else ""

    title = meta("og:title", prop=True) or (soup.title.string.strip() if soup.title else "")
    description = meta("og:description", prop=True) or meta("description")

    # Strip script/style before grabbing visible body text.
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    visible_text = re.sub(r"\n{3,}", "\n\n", soup.get_text("\n").strip())
    visible_text = re.sub(r"[ \t]{2,}", " ", visible_text)

    embedded_text = _extract_embedded_json_text(BeautifulSoup(html, "html.parser"))

    raw_text = visible_text
    if len(embedded_text) > len(visible_text):
        # SPA-rendered page: the real content lives in a JSON blob, not the
        # server-rendered DOM. Prefer it.
        raw_text = embedded_text

    links: list[tuple[str, str]] = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        text = a.get_text(strip=True)
        if not href or href.startswith(("javascript:", "#")):
            continue
        if "smore.com" in href and not text:
            continue
        links.append((text or href, href))

    images = [img.get("alt", "").strip() for img in soup.find_all("img") if img.get("alt", "").strip()]

    return ScrapedNewsletter(
        url=url,
        title=title,
        description=description,
        raw_text=raw_text,
        links=links,
        images=images,
    )


def summarize_with_claude(newsletter: ScrapedNewsletter) -> str:
    """Ask Claude to turn the raw scrape into a clean key-info digest.
    Falls back to naive extraction on any error (missing key, API failure)."""
    try:
        import anthropic
    except ImportError:
        return summarize_naively(newsletter)

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return summarize_naively(newsletter)

    client = anthropic.Anthropic(api_key=api_key)
    prompt = (
        "Below is raw scraped text from a Smore newsletter/flyer. Extract the "
        "key information a busy reader needs: headline/purpose, important "
        "dates or deadlines, events, action items, and any names, links, or "
        "contact info. Write it as short, skimmable bullet points grouped "
        "under headers. Omit boilerplate, navigation text, and Smore's own "
        "UI chrome (e.g. 'Create your own Smore flyer'). If a detail isn't "
        "present, don't invent it.\n\n"
        f"URL: {newsletter.url}\n"
        f"Title: {newsletter.title}\n\n"
        f"Raw text:\n{newsletter.raw_text[:12000]}"
    )
    try:
        message = client.messages.create(
            model="claude-sonnet-5",
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        return "".join(block.text for block in message.content if hasattr(block, "text"))
    except Exception as exc:  # noqa: BLE001 - fall back on any API problem
        print(f"Claude summarization failed ({exc}); falling back to naive extraction.", file=sys.stderr)
        return summarize_naively(newsletter)


def summarize_naively(newsletter: ScrapedNewsletter) -> str:
    """Heuristic fallback used when no ANTHROPIC_API_KEY is configured."""
    lines = [ln.strip() for ln in newsletter.raw_text.splitlines() if ln.strip()]
    # De-dupe while preserving order (Smore often repeats headline text).
    seen = set()
    deduped = []
    for ln in lines:
        if ln not in seen:
            seen.add(ln)
            deduped.append(ln)

    date_pattern = re.compile(
        r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2}"
        r"|\b\d{1,2}/\d{1,2}/\d{2,4}\b"
        r"|\b\d{1,2}:\d{2}\s*(?:AM|PM|am|pm)?\b",
    )
    key_dates = [ln for ln in deduped if date_pattern.search(ln)][:10]

    body = "\n".join(f"- {ln}" for ln in deduped[:40])
    out = [f"Summary (auto-extracted, no ANTHROPIC_API_KEY set):\n{body}"]
    if key_dates:
        out.append("\nPossible dates/times mentioned:\n" + "\n".join(f"- {d}" for d in key_dates))
    if newsletter.links:
        out.append(
            "\nLinks:\n"
            + "\n".join(f"- {text}: {href}" for text, href in newsletter.links[:20])
        )
    return "\n".join(out)


def build_email_body(newsletter: ScrapedNewsletter, digest: str) -> tuple[str, str]:
    """Return (plain_text, html) email bodies."""
    plain = (
        f"{newsletter.title or 'Newsletter update'}\n"
        f"{newsletter.url}\n\n"
        f"{digest}\n"
    )

    def esc(s: str) -> str:
        return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    digest_html = esc(digest).replace("\n", "<br>")
    html = f"""\
<html><body style="font-family: -apple-system, Arial, sans-serif; max-width: 640px; margin: 0 auto;">
  <h2>{esc(newsletter.title or 'Newsletter update')}</h2>
  <p><a href="{esc(newsletter.url)}">{esc(newsletter.url)}</a></p>
  <div>{digest_html}</div>
</body></html>"""
    return plain, html


def send_email(subject: str, plain_body: str, html_body: str) -> None:
    smtp_host = os.environ["SMTP_HOST"]
    smtp_port = int(os.environ.get("SMTP_PORT", "587"))
    smtp_username = os.environ["SMTP_USERNAME"]
    smtp_password = os.environ["SMTP_PASSWORD"]
    email_from = os.environ.get("EMAIL_FROM", smtp_username)
    email_to = os.environ.get("EMAIL_TO", "rtwilliamson@gmail.com")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = email_from
    msg["To"] = email_to
    msg.attach(MIMEText(plain_body, "plain"))
    msg.attach(MIMEText(html_body, "html"))

    with smtplib.SMTP(smtp_host, smtp_port) as server:
        server.starttls()
        server.login(smtp_username, smtp_password)
        server.sendmail(email_from, [email_to], msg.as_string())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=os.environ.get("NEWSLETTER_URL", DEFAULT_URL))
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Scrape and print the digest instead of sending an email.",
    )
    args = parser.parse_args()

    print(f"Fetching {args.url} ...", file=sys.stderr)
    newsletter = scrape(args.url)

    print("Summarizing ...", file=sys.stderr)
    digest = summarize_with_claude(newsletter)

    plain, html = build_email_body(newsletter, digest)
    subject = f"Newsletter digest: {newsletter.title or args.url}"

    if args.dry_run:
        print(f"Subject: {subject}\n")
        print(plain)
        return 0

    send_email(subject, plain, html)
    print(f"Email sent to {os.environ.get('EMAIL_TO', 'rtwilliamson@gmail.com')}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
