#!/usr/bin/env python3
"""
indeed_email.py

Reads Indeed job emails over IMAP (Gmail by default) and turns each job into the
common record shape job_monitor.py screens:

    {id, company, title, location, url, body, updated, lane, source}

Indeed blocks automated access to its job pages, so the email is the only
text available. Postings are screened on that text alone, and each one is
marked so the digest can say the full description was not read.

Formats handled:
  * single-job "match" emails from donotreply@match.indeed.com
    (title, company, location, then Salary / Job type / Work setting lines,
    then a "View job:" link). Verified against a real email.
    fixtures/indeed_match_example.txt shows the layout.
  * multi-job saved-search alerts are parsed by the same block logic: any
    paragraph carrying Salary / Job type / Work setting lines, or followed by
    a job link, is read as one job. Check the first real alert digest and
    adjust if Indeed lays it out differently.

Credentials: IMAP_USER / IMAP_PASS when set, otherwise the SMTP_USER /
SMTP_PASS pair the digest is sent with. A Gmail app password works for both.
IMAP_MAILBOX selects the folder to search (Gmail's "All Mail" by default).
"""

import email
import hashlib
import imaplib
import os
import re
from datetime import datetime, timedelta
from email.header import decode_header, make_header

import envfile

envfile.load()

IMAP_HOST = os.environ.get("IMAP_HOST", "imap.gmail.com")
IMAP_USER = os.environ.get("IMAP_USER", "")
IMAP_PASS = os.environ.get("IMAP_PASS", "")
IMAP_MAILBOX = os.environ.get("IMAP_MAILBOX", '"[Gmail]/All Mail"')
LOOKBACK_DAYS = int(os.environ.get("INDEED_LOOKBACK_DAYS", "3"))

ATTR_RE = re.compile(r"^(salary|pay|job type|work setting|estimated pay)\s*:",
                     re.IGNORECASE)
LINK_RE = re.compile(r"^(view job|apply now|view details|see job)\s*:\s*(\S+)",
                     re.IGNORECASE)
NOISE = re.compile(r"^(hi |do you want|keep your indeed|no:|maybe:|yes:|"
                   r"edit profile|minimum base pay|this job match|manage email|"
                   r"help center|privacy|terms|unsubscribe|indeed$|benefits:)",
                   re.IGNORECASE)


def _plain_text(msg):
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                return part.get_payload(decode=True).decode(
                    part.get_content_charset() or "utf-8", errors="replace")
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                raw = part.get_payload(decode=True).decode(
                    part.get_content_charset() or "utf-8", errors="replace")
                raw = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>", "\n", raw)
                return re.sub(r"<[^>]+>", "", raw)
        return ""
    return msg.get_payload(decode=True).decode(
        msg.get_content_charset() or "utf-8", errors="replace")


def parse_jobs(text, received=""):
    """Split an Indeed email into job records. Pure function, testable."""
    lines = [l.rstrip() for l in text.replace("\r", "").split("\n")]
    paras, cur = [], []
    for l in lines:
        if l.strip():
            cur.append(l.strip())
        elif cur:
            paras.append(cur)
            cur = []
    if cur:
        paras.append(cur)

    intro = ""
    if paras and paras[0] and paras[0][0].lower().startswith("hi "):
        intro = " ".join(paras[1]) if len(paras) > 1 else ""

    jobs = []
    for i, p in enumerate(paras):
        attrs = [l for l in p if ATTR_RE.match(l)]
        head = [l for l in p if not ATTR_RE.match(l)]
        if not attrs or len(head) < 2 or NOISE.match(head[0]):
            continue
        title, company = head[0], head[1]
        location = head[2] if len(head) > 2 else ""
        url = ""
        for later in paras[i + 1:i + 4]:
            for l in later:
                m = LINK_RE.match(l)
                if m:
                    url = m.group(2)
                    break
            if url:
                break
        setting = next((l.split(":", 1)[1].strip() for l in attrs
                        if l.lower().startswith("work setting")), "")
        loc = location
        if setting and setting.lower() not in loc.lower():
            loc = f"{location}; {setting}" if location else setting
        key = hashlib.sha1(f"{title}|{company}|{location}".lower()
                           .encode()).hexdigest()[:16]
        jobs.append({
            "id": f"in-{key}",
            "company": f"{company} (via Indeed)",
            "title": title,
            "location": loc,
            "url": url,
            "body": " ".join([intro] + attrs),
            "updated": received,
            "source": "indeed_email",
        })
    return jobs


def pull_indeed_email(user, password, since_days=LOOKBACK_DAYS):
    """Return (jobs, emails_read). Raises on login or IMAP failure so the
    caller can report the board as broken."""
    since = (datetime.now() - timedelta(days=since_days)).strftime("%d-%b-%Y")
    jobs, seen, n = [], set(), 0
    box = imaplib.IMAP4_SSL(IMAP_HOST)
    try:
        box.login(user, password)
        box.select(IMAP_MAILBOX, readonly=True)
        typ, data = box.search(None, "SINCE", since, "FROM", "indeed.com")
        for num in (data[0].split() if data and data[0] else []):
            typ, msg_data = box.fetch(num, "(RFC822)")
            msg = email.message_from_bytes(msg_data[0][1])
            sender = str(make_header(decode_header(msg.get("From", ""))))
            # Sign-in codes and account notices carry no jobs.
            if "login@" in sender.lower():
                continue
            n += 1
            for job in parse_jobs(_plain_text(msg), msg.get("Date", "")):
                if job["id"] not in seen:
                    seen.add(job["id"])
                    jobs.append(job)
    finally:
        try:
            box.logout()
        except Exception:
            pass
    return jobs, n


if __name__ == "__main__":
    import json, sys
    if len(sys.argv) > 1:
        print(json.dumps(parse_jobs(open(sys.argv[1], encoding="utf-8")
                                    .read()), indent=2))
    else:
        js, n = pull_indeed_email(IMAP_USER or os.environ["SMTP_USER"],
                                  IMAP_PASS or os.environ["SMTP_PASS"])
        print(f"{n} Indeed emails, {len(js)} jobs")
        for j in js:
            print(f"  {j['title']} | {j['company']} | {j['location']}")
