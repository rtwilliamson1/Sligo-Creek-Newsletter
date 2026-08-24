#!/usr/bin/env python3
"""
Find the latest Sligo Creek newsletter (a Smore flyer whose URL changes with
every issue), scrape it, and email the key info to yourself.

Each issue is announced by a ParentSquare email whose "view newsletter"
button is a tracking link that redirects (through one or more hops) to the
actual app.smore.com flyer. Rather than hardcoding a URL, this script reads
the most recent matching email from your inbox via IMAP and follows the
redirect chain to find the current flyer.

Usage:
    python newsletter_scraper.py
    python newsletter_scraper.py --dry-run
    python newsletter_scraper.py --url https://app.smore.com/n/xxxxx --dry-run
    python newsletter_scraper.py --force   # re-send even if already processed

Configuration is read from environment variables (see .env.example):
    NEWSLETTER_SENDER  Sender address of the newsletter notification email
                        (default: ParentSquare's Sligo Creek sender)
    IMAP_HOST          IMAP server, e.g. imap.gmail.com (default: imap.gmail.com)
    IMAP_PORT          IMAP port (default: 993)
    EMAIL_TO           Recipient address (default: rtwilliamson@gmail.com)
    EMAIL_FROM         "From" address (defaults to SMTP_USERNAME)
    SMTP_HOST          SMTP server, e.g. smtp.gmail.com
    SMTP_PORT          SMTP port, e.g. 587
    SMTP_USERNAME      Gmail login, reused for both SMTP (send) and IMAP (read)
    SMTP_PASSWORD      Gmail App Password, reused for both SMTP and IMAP
    STATE_FILE         Where the last-processed flyer slug is recorded, to
                        avoid re-sending an email for the same issue
                        (default: .state/last_slug.txt)
"""

from __future__ import annotations

import argparse
import email as email_lib
import imaplib
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

DEFAULT_SENDER = "donotreply+c75680a1-bc11-5aab-8d22-8381a21f2834@parentsquare.com"
STATE_FILE = os.environ.get("STATE_FILE", ".state/last_slug.txt")
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
LINK_BLOCKLIST_KEYWORDS = (
    "unsubscribe",
    "mailto:",
    "facebook.com",
    "twitter.com",
    "x.com",
    "instagram.com",
    "linkedin.com",
    "parentsquare.com/settings",
    "parentsquare.com/notification",
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


def _extract_links_from_email(msg: email_lib.message.Message) -> list[str]:
    html_body = None
    text_body = None
    parts = msg.walk() if msg.is_multipart() else [msg]
    for part in parts:
        if part.get_content_maintype() == "multipart":
            continue
        content_type = part.get_content_type()
        if content_type not in ("text/html", "text/plain"):
            continue
        payload = part.get_payload(decode=True)
        if not payload:
            continue
        decoded = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        if content_type == "text/html" and html_body is None:
            html_body = decoded
        elif content_type == "text/plain" and text_body is None:
            text_body = decoded

    links: list[str] = []
    if html_body:
        soup = BeautifulSoup(html_body, "html.parser")
        links.extend(
            a["href"].strip()
            for a in soup.find_all("a", href=True)
            if a["href"].strip().startswith(("http://", "https://"))
        )
    if text_body:
        links.extend(re.findall(r"https?://\S+", text_body))

    seen: set[str] = set()
    filtered: list[str] = []
    for link in links:
        link = link.rstrip(").,;\"'")
        if link in seen or any(kw in link.lower() for kw in LINK_BLOCKLIST_KEYWORDS):
            continue
        seen.add(link)
        filtered.append(link)
    return filtered


def _resolve_smore_url(candidate_links: list[str], max_candidates: int = 15) -> str | None:
    """Follow each candidate link's redirect chain (tracking links often hop
    through more than one service) until one lands on a smore.com URL."""
    for link in candidate_links[:max_candidates]:
        try:
            resp = requests.get(
                link, headers={"User-Agent": USER_AGENT}, allow_redirects=True, timeout=15
            )
        except requests.RequestException:
            continue
        if "smore.com" in resp.url:
            return resp.url
        # Some hops land on an interstitial page rather than redirecting
        # straight through; fall back to scanning it for the real link.
        match = re.search(r"https?://app\.smore\.com/n/[A-Za-z0-9]+", resp.text)
        if match:
            return match.group(0)
    return None


def find_latest_newsletter_url(sender: str | None = None) -> str | None:
    sender = sender or os.environ.get("NEWSLETTER_SENDER", DEFAULT_SENDER)
    host = os.environ.get("IMAP_HOST", "imap.gmail.com")
    port = int(os.environ.get("IMAP_PORT", "993"))
    username = os.environ["SMTP_USERNAME"]
    password = os.environ["SMTP_PASSWORD"]

    with imaplib.IMAP4_SSL(host, port) as imap:
        imap.login(username, password)
        imap.select("INBOX")
        status, data = imap.search(None, f'(FROM "{sender}")')
        if status != "OK" or not data or not data[0]:
            return None
        uids = data[0].split()
        latest_uid = uids[-1]  # IMAP UIDs increase with arrival order
        status, msg_data = imap.fetch(latest_uid, "(RFC822)")
        if status != "OK" or not msg_data or not msg_data[0]:
            return None
        raw_email = msg_data[0][1]

    msg = email_lib.message_from_bytes(raw_email)
    links = _extract_links_from_email(msg)
    return _resolve_smore_url(links)


def extract_slug(url: str) -> str:
    match = re.search(r"/n/([A-Za-z0-9]+)", url)
    return match.group(1) if match else url


def read_state() -> str | None:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return f.read().strip()
    except FileNotFoundError:
        return None


def write_state(slug: str) -> None:
    state_dir = os.path.dirname(STATE_FILE)
    if state_dir:
        os.makedirs(state_dir, exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        f.write(slug)


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


def build_email_body(newsletter: ScrapedNewsletter) -> tuple[str, str]:
    """Return (plain_text, html) email bodies containing the full scraped text."""
    plain = (
        f"{newsletter.title or 'Newsletter update'}\n"
        f"{newsletter.url}\n\n"
        f"{newsletter.raw_text}\n"
    )

    def esc(s: str) -> str:
        return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    body_html = esc(newsletter.raw_text).replace("\n", "<br>")
    html = f"""\
<html><body style="font-family: -apple-system, Arial, sans-serif; max-width: 640px; margin: 0 auto;">
  <h2>{esc(newsletter.title or 'Newsletter update')}</h2>
  <p><a href="{esc(newsletter.url)}">{esc(newsletter.url)}</a></p>
  <div>{body_html}</div>
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
    parser.add_argument(
        "--url",
        default=None,
        help="Scrape this URL directly instead of discovering it from the inbox.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Scrape and print the digest instead of sending an email.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Send even if this issue's slug matches the last one processed.",
    )
    args = parser.parse_args()

    if args.url:
        url = args.url
    else:
        print("Searching inbox for the latest newsletter link ...", file=sys.stderr)
        url = find_latest_newsletter_url()
        if not url:
            # Not finding a matching email yet is the normal steady state
            # between issues (or before the first one has arrived) - it's
            # not a failure, so a scheduled run shouldn't show as red.
            print(
                "No newsletter email found yet (or couldn't resolve its "
                "link to a smore.com URL). Nothing to send. Try --url to "
                "scrape a known link directly if you want to test the "
                "scrape/email steps without inbox discovery.",
                file=sys.stderr,
            )
            return 0
        print(f"Resolved: {url}", file=sys.stderr)

    slug = extract_slug(url)
    if not args.force and not args.dry_run and read_state() == slug:
        print(f"Already sent the digest for this issue (slug {slug}); nothing new.", file=sys.stderr)
        return 0

    print(f"Fetching {url} ...", file=sys.stderr)
    newsletter = scrape(url)

    plain, html = build_email_body(newsletter)
    subject = f"Newsletter: {newsletter.title or url}"

    if args.dry_run:
        print(f"Subject: {subject}\n")
        print(plain)
        return 0

    send_email(subject, plain, html)
    write_state(slug)
    print(f"Email sent to {os.environ.get('EMAIL_TO', 'rtwilliamson@gmail.com')}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
