#!/usr/bin/env python3
"""
job_monitor.py

Pulls live postings from public ATS feeds, screens them against the match
and disqualifier rules in a screening profile (see screening_example.py), and
emails a digest of anything new.

Every posting in the digest came from the employer's own board within the last
few minutes, so "is it still open" is answered by construction.

Setup:
    1. python3 probe_boards.py       -> produces boards.json
    2. copy screening_example.py to screening_local.py and edit it
    3. set the SMTP environment variables (see .env.example)
    4. python3 job_monitor.py        -> first run seeds seen.json, sends nothing
    5. add to cron:
       0 3 * * * cd /path/to/dir && /usr/bin/python3 job_monitor.py >> monitor.log 2>&1

Files:
    boards.json  -- validated company/ATS config from probe_boards.py
    seen.json    -- posting IDs already reported, so you only get new ones
    applied.json -- postings you applied to, so they stop resurfacing
    digest.md    -- last digest, also written to disk
"""

import html
import json
import os
import re
import smtplib
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path

import envfile

envfile.load()

import ats_adapters as A  # noqa: E402
import indeed_email as IE  # noqa: E402

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

HERE = Path(__file__).resolve().parent
BOARDS = HERE / "boards.json"
SEEN = HERE / "seen.json"
APPLIED = HERE / "applied.json"
DIGEST = HERE / "digest.md"

SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "465"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")   # app password, not your login
MAIL_TO = os.environ.get("MAIL_TO", SMTP_USER)

# Screening profile. Everything personal to one search (target titles,
# salary floor, home area, fields held and never held) lives in a profile
# module. screening_local.py is used when present; otherwise the example
# profile is loaded. JOB_MONITOR_PROFILE overrides both.
import importlib


def _load_profile():
    name = os.environ.get("JOB_MONITOR_PROFILE", "").strip()
    if name:
        return importlib.import_module(name)
    try:
        return importlib.import_module("screening_local")
    except ModuleNotFoundError:
        return importlib.import_module("screening_example")


P = _load_profile()

MIN_SALARY = P.MIN_SALARY
TITLE_SIGNALS = P.TITLE_SIGNALS
TITLE_KILL = P.TITLE_KILL
NEVER_HELD_FIELDS = P.NEVER_HELD_FIELDS
HELD_FIELDS = P.HELD_FIELDS
TARGET_FIELDS = P.TARGET_FIELDS
YEARS_GATE = P.YEARS_GATE
VIABLE_LOCAL = P.LOCAL_PLACES
LOCATION_KILL = P.LOCATION_KILL
HOME = re.compile(P.HOME_STATE_PATTERN, re.IGNORECASE)
FACULTY_MIN_SALARY = P.FACULTY_MIN_SALARY
FACULTY_TITLE_KILL = P.FACULTY_TITLE_KILL
FACULTY_DISCIPLINES = P.FACULTY_DISCIPLINES
FACULTY_CRED_KILL = P.FACULTY_CRED_KILL
SCHEDULE_CONFLICTS = P.SCHEDULE_CONFLICTS
SCHEDULE_FLEX = P.SCHEDULE_FLEX

# Structural blockers in the body, with the plain-English label the digest
# prints for each, so a human can audit why a posting was withheld.
BODY_KILL = [pat for pat, _ in P.BODY_KILL]
BODY_KILL_LABELS = {pat: label for pat, label in P.BODY_KILL}

# Phrases that contain a signal word but mean something else entirely.
# "Machine Learning Engineer" is not a learning-and-development role, and
# "Assistant Store Manager in Training" is not a training role. These are
# removed from the title before signals are matched, which is why they are
# patterns rather than kill words: the point is to stop the signal firing,
# not to reject any title that mentions ML.
TITLE_DECOYS = re.compile(
    r"\b(machine|deep|reinforcement|federated|supervised|unsupervised)\s+"
    r"learning\b|\bml\s+(systems|engineer|infrastructure|ops)\b"
    r"|\bin[-\s]training\b|\btrainee\b|\blearning\s+management\s+"
    r"system\b|\bwork\s+based\s+learning\b", re.IGNORECASE)

YEARS_RE = re.compile(
    r"(\d{1,2})\s*(?:\+|\s*(?:-|–|to)\s*\d{1,2}\s*\+?)?\s*"
    r"(?:or more\s*)?year", re.IGNORECASE)


def years_blocker(body):
    """Return a reason when the posting demands long tenure in a field never
    held, else "".

    Looks at the text around each "N years" mention rather than the whole
    posting, because a body can name many fields and only some of them are
    attached to the requirement. "Must be 18 years of age" is excluded; real
    postings say exactly that.
    """
    for m in YEARS_RE.finditer(body):
        n = int(m.group(1))
        window = body[m.start():m.start() + 160]
        if "of age" in window[:40]:
            continue
        if n < YEARS_GATE:
            continue
        held = [f for f in HELD_FIELDS if f in window]
        never = [f for f in NEVER_HELD_FIELDS if f in window]
        target = [f for f in TARGET_FIELDS if f in window]
        if never and not held:
            if target:
                # The role is in a target function with a high bar.
                return f"STRETCH: {n}+ years, alongside {target[0]}"
            return f"{n}+ years in {never[0]}"
    return ""


# Workday shows "2 Locations" in search results when a requisition carries
# more than one location anchor. That is a count, not a place, so it has to be
# replaced with the real set from the posting's detail record.
MULTI_LOC_RE = re.compile(r"^\d+\s+locations?$", re.IGNORECASE)

# A posting whose location is only a country-wide marker is treated as remote
# eligible. Anything that also names a specific place is judged on that place.
US_WIDE = ["united states", "usa", "u.s.", "nationwide", "anywhere",
           "remote", "virtual"]

# Non-US markers. Several boards here are global and most of their learning
# roles sit in Bengaluru, Warsaw or Manila.
NON_US = [
    "united kingdom", "london", "ireland", "dublin", "canada", "ontario",
    "toronto", "india", "bengaluru", "bangalore", "hyderabad", "pune",
    "gurugram", "mumbai", "chennai", "poland", "warsaw", "krakow", "egypt",
    "cairo", "france", "paris", "brazil", "sao paulo", "barueri",
    "indonesia", "jakarta", "thailand", "bangkok", "philippines", "manila",
    "mauritius", "ebene", "germany", "berlin", "spain", "madrid", "japan",
    "tokyo", "mexico", "costa rica", "emea", "apac", "singapore",
    "australia", "netherlands", "portugal", "romania", "argentina", "chile",
    "colombia", "south africa", "china", "korea", "italy", "sweden",
]

# ---------------------------------------------------------------------------
# Faculty lane
#
# Runs on its own rules and only on boards tagged "lane": "faculty" in
# boards.json. Corporate boards are untouched: a "Technical Instructor" at a
# retailer is still screened by the corporate rules. The thresholds and
# discipline lists come from the screening profile.
# ---------------------------------------------------------------------------

# "Evaluator" and "course mentor" cover roles where the work is grading and
# student support on the instructor's own hours.
FACULTY_TITLE = re.compile(
    r"\b(faculty|instructor|professor|lecturer|teaching|evaluator|"
    r"course mentor|faculty mentor)\b", re.IGNORECASE)

# Faculty development and faculty support are corporate roles, so they are
# not routed to the teaching lane.
FACULTY_NOT_TEACHING = re.compile(
    r"faculty (development|support|affairs|success|services|recruit)",
    re.IGNORECASE)

FT_EVIDENCE = re.compile(
    r"full[- ]time|fulltime|core faculty|tenure|\bfte\b|"
    r"employment type: [^.]*full", re.IGNORECASE)
PT_EVIDENCE = re.compile(
    r"part[- ]time|parttime|adjunct|per[- ]course|per[- ]credit|"
    r"course[- ]by[- ]course|employment type: [^.]*part", re.IGNORECASE)

ONLINE_EVIDENCE = re.compile(
    r"\bremote\b|\bvirtual\b|\(online\)|fully online|100% online|"
    r"teach(ing)? online|online (faculty|instructor|course|class|teaching|"
    r"program|modality|delivery|environment|learning|format|university)|"
    r"telecommute|work from home", re.IGNORECASE)



# ---------------------------------------------------------------------------
# Residency check for "remote" postings
#
# A posting tagged Remote often says, deeper in the text, that candidates must
# live in California, or within 50 miles of an office in Ohio. The target is
# remote roles that can be held from the home state in the screening profile,
# or roles within the local area. The location field cannot tell these apart,
# so the description is read for the restriction and the place it names is
# checked.
# ---------------------------------------------------------------------------

RESIDENCY_PATTERNS = [
    r"(?:must|required to|need to|expected to|will need to)\s+(?:currently\s+)?"
    r"(?:live|reside|be located|be based|be physically located)\s+"
    r"(?:in|within|near)\s+([^.;]{3,200})",
    r"(?:candidates|applicants|employees)\s+(?:must\s+be\s+|should\s+be\s+)?"
    r"(?:located|based|residing|living)\s+(?:in|within|near)\s+([^.;]{3,200})",
    r"within\s+(?:a\s+)?\d{1,3}[- ]mile(?:s)?\s+(?:radius\s+)?of\s+([^.;]{3,120})",
    r"(?:remote|telework|work from home)[^.]{0,60}?\b(?:in|from)\s+(?:one of\s+)?"
    r"the following (?:states|locations)\s*:?\s*([^.]{3,400})",
    r"(?:open|available)\s+(?:only\s+)?to\s+(?:candidates|applicants|residents)"
    r"\s+(?:in|of|from)\s+([^.;]{3,200})",
    r"(?:must|should|required to)\s+be\s+(?:a\s+)?(?:legal\s+)?resident(?:s)?"
    r"\s+of\s+([^.;]{3,200})",
    r"resid(?:e|ency|ing)\s+(?:with)?in\s+the\s+state\s+of\s+([^.;]{3,120})",
    r"(?:only|currently)\s+(?:hiring|considering)\s+(?:candidates\s+)?"
    r"(?:in|from)\s+([^.;]{3,200})",
]
RESIDENCY_RE = [re.compile(p, re.IGNORECASE) for p in RESIDENCY_PATTERNS]

# States some employers cannot hire in. If the home state is on that list,
# the role is closed whatever the location field says.
EXCLUDED_STATES_RE = re.compile(
    r"(?:unable|not able|cannot|can ?not|do not|does not)\s+(?:to\s+)?"
    r"(?:hire|employ|consider)[^.]{0,60}?(?:in|from|residing in)\s+"
    r"(?:the following states\s*:?\s*)?([^.]{3,300})", re.IGNORECASE)

# A named place that is acceptable.
RESIDENCY_OK = re.compile(
    "(?:" + P.HOME_STATE_PATTERN + ")|"
    r"united states|\bu\.?s\.?a?\b|\bus\b|"
    r"anywhere|any (?:u\.?s\.? )?state|all 50|contiguous|lower 48|"
    r"continental|southeast|eastern time|\best\b|\bet zone|"
    + "|".join(re.escape(t) for t in VIABLE_LOCAL), re.IGNORECASE)


def residency_blocker(body):
    """Return a short reason when a remote posting restricts where the hire
    must live and the home state is not allowed, else ""."""
    for m in EXCLUDED_STATES_RE.finditer(body):
        if HOME.search(m.group(1)):
            return "home state is on the employer's excluded-states list"
    for rx in RESIDENCY_RE:
        for m in rx.finditer(body):
            place = m.group(1).strip()
            # State abbreviations carry periods ("Louisville, KY. Denver,
            # CO., or Mahwah, NJ."), which end the regex capture early, so
            # the check reads a fixed window past the match as well. When in
            # doubt this keeps the posting, which is the safer error.
            window = body[m.start(1):m.start(1) + 160]
            if RESIDENCY_OK.search(place) or RESIDENCY_OK.search(window):
                continue
            return place[:70].rstrip(" ,:;")
    return ""



# ---------------------------------------------------------------------------
# Remote-only rule
#
# Only remote roles count as matches. A role within the local area that is
# on-site or hybrid is shown in a short FYI list, never as a match. "Remote" has to be stated as a work arrangement: the word appearing
# somewhere in the text ("remote monitoring", "remote students") is not
# enough, and "online" in a faculty title is not enough either, because
# several online universities hire "online" faculty who work on campus.
# ---------------------------------------------------------------------------

REMOTE_STRONG = re.compile(
    r"\b(?:fully|100%|completely|entirely|permanently)\s+remote\b|"
    r"\bremote\s+(?:position|role|opportunity|employee|job|first|"
    r"eligible|faculty|instructor|teaching|evaluator|mentor|team member)"
    r"\b(?![- ]led)|\bremote-first\b|"
    r"\bwork\s+(?:from|at)\s+home\b|\bwork\s+from\s+anywhere\b|"
    r"\btelecommut|\b(?:this|the)\s+(?:role|position|job)\s+is\s+"
    r"(?:a\s+)?(?:fully\s+)?remote\b|\blocation\s*:?\s*(?:remote|virtual)\b|"
    r"\bremote\s*[,(-]\s*(?:us|u\.s\.|usa|united states)\b|\bus[- ]remote\b|"
    r"\bvirtual\s+(?:position|role)\b|\(remote\)|"
    r"work setting\s*:\s*remote|\bremote\s+within\s+the\s+(?:us|united states)",
    re.IGNORECASE)

NOT_REMOTE = re.compile(
    r"\bhybrid\s+(?:role|position|schedule|work|model|arrangement|"
    r"environment)\b|"
    r"\b(?:\d|one|two|three|four|five)\s+days?\s+(?:a|per)\s+week\s+"
    r"(?:in|on|at)\s+(?:the\s+)?(?:office|site|campus|our)|"
    r"\b(?:this|the)\s+(?:role|position|job)\s+is\s+(?:an?\s+)?"
    r"(?:on[- ]?site|in[- ]office|in[- ]person|hybrid)\b|"
    r"\bnot\s+(?:a\s+)?remote\b|\bno\s+remote\b|"
    r"remote\s+work\s+(?:is|will)\s+not|remote\s+(?:work\s+)?(?:is\s+)?not\s+"
    r"(?:available|an?\s+option|offered|permitted|considered)|"
    r"require[sd]?\s+(?:you\s+)?to\s+be\s+(?:on[- ]?site|in\s+the\s+office|"
    r"in\s+person)|"
    r"\bmust\s+report\s+(?:to|in\s+person)|"
    r"\b(?:on[- ]?site|in[- ]office|in[- ]person)\s+(?:role|position|"
    r"requirement|presence)\b|work setting\s*:\s*(?:in[- ]person|hybrid)",
    re.IGNORECASE)

CAMPUS_REQUIRED = re.compile(
    r"(?:on[- ]campus|in[- ]person|face[- ]to[- ]face)[^.]{0,80}"
    r"(?:required|mandatory|expected|must)|"
    r"must\s+(?:attend|be\s+available|be\s+present)[^.]{0,60}"
    r"(?:campus|in\s+person|on[- ]site)|"
    r"within\s+(?:a\s+)?(?:reasonable\s+)?(?:commuting|driving)\s+distance|"
    r"(?:work|teach)\s+(?:on|from)\s+(?:our|the)\s+[a-z ]{0,20}campus",
    re.IGNORECASE)

SALARY_RE = re.compile(
    r"\$\s?(\d{2,3})[,.]?(\d{3})?\s*(?:k\b)?", re.IGNORECASE)

TIMEOUT = 20
UA = "Mozilla/5.0 (compatible; job-monitor/1.0)"


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------

def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def strip_html(raw):
    if not raw:
        return ""
    text = html.unescape(raw)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def normalise(ats, slug, payload):
    """Flatten each ATS's shape into one common record."""
    out = []

    if ats == "greenhouse":
        for j in payload.get("jobs", []):
            out.append({
                "id": f"gh-{slug}-{j.get('id')}",
                "company": slug,
                "title": j.get("title", ""),
                "location": (j.get("location") or {}).get("name", ""),
                "url": j.get("absolute_url", ""),
                "body": strip_html(j.get("content", "")),
                "updated": j.get("updated_at", ""),
            })

    elif ats == "lever":
        for j in payload if isinstance(payload, list) else []:
            cats = j.get("categories") or {}
            out.append({
                "id": f"lv-{slug}-{j.get('id')}",
                "company": slug,
                "title": j.get("text", ""),
                "location": cats.get("location", "") or "",
                "url": j.get("hostedUrl", ""),
                "body": strip_html(j.get("descriptionPlain")
                                   or j.get("description", "")),
                "updated": str(j.get("createdAt", "")),
            })

    elif ats == "ashby":
        for j in payload.get("jobs", []):
            out.append({
                "id": f"ab-{slug}-{j.get('id')}",
                "company": slug,
                "title": j.get("title", ""),
                "location": j.get("location", "") or "",
                "url": j.get("jobUrl", "") or j.get("applyUrl", ""),
                "body": strip_html(j.get("descriptionPlain")
                                   or j.get("descriptionHtml", "")),
                "updated": j.get("publishedAt", ""),
            })

    return out


def clean_title(title):
    """Remove phrases that carry a signal word but a different meaning."""
    return TITLE_DECOYS.sub(" ", (title or "").lower())


def title_ok(title, lane=None):
    """Cheap first-pass screen. Used to avoid fetching detail pages for
    postings that could never match anyway."""
    if lane == "faculty":
        return faculty_title_ok(title)
    t = clean_title(title)
    return (any(s in t for s in TITLE_SIGNALS)
            and not any(k in t for k in TITLE_KILL))


def post_json(url, body, referer):
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers={
        "User-Agent": UA,
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Accept-Language": "en-US",
        "Referer": referer,
    })
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


# Per-board health for the run, keyed by slug. The nightly digest only ever
# reports postings, which means a board that quietly stops answering looks
# exactly like a board with no new roles. This is what makes the difference
# visible.
BOARD_STATS = {}


def note_board(slug, ats, listed=None, matched=None, error=""):
    rec = BOARD_STATS.setdefault(slug, {"ats": ats, "listed": None,
                                        "matched": None, "error": ""})
    if listed is not None:
        rec["listed"] = listed
    if matched is not None:
        rec["matched"] = matched
    if error:
        rec["error"] = error


def pull_workday(entry):
    """Workday's list endpoint is POST-paginated and omits descriptions, so
    we page through titles, screen them, then GET details only for hits."""
    tenant, dc, site = entry["tenant"], entry["dc"], entry["site"]
    base = f"https://{tenant}.{dc}.myworkdayjobs.com"
    listing = f"{base}/wday/cxs/{tenant}/{site}/jobs"
    referer = f"{base}/en-US/{site}"

    candidates, offset, total = [], 0, None

    while total is None or offset < total:
        try:
            page = post_json(listing,
                             {"appliedFacets": {}, "limit": 20,
                              "offset": offset, "searchText": ""},
                             referer)
        except Exception as exc:
            print(f"  warn: {tenant} page at {offset} failed: {exc}",
                  file=sys.stderr)
            note_board(entry.get("slug", tenant), "workday",
                       error=f"page {offset}: {type(exc).__name__}")
            break

        if total is None:
            total = page.get("total", 0)

        postings = page.get("jobPostings", [])
        if not postings:
            break

        for p in postings:
            if title_ok(p.get("title", ""), entry.get("lane")):
                candidates.append(p)

        offset += 20
        time.sleep(0.3)
        if offset > 4000:      # safety valve on very large boards
            break

    jobs = []
    for p in candidates:
        path = p.get("externalPath", "")
        detail_url = f"{base}/wday/cxs/{tenant}/{site}{path}"
        body = ""
        all_locs = []
        try:
            detail = fetch(detail_url)
            info = detail.get("jobPostingInfo") or {}
            body = strip_html(info.get("jobDescription", ""))
            # The detail record carries the full anchor set. This matters
            # because a nationwide "US, Remote" is usually an *additional*
            # location sitting behind a headquarters address, and the search
            # listing collapses the whole set to "2 Locations".
            all_locs = [info.get("location") or ""]
            all_locs += [x for x in (info.get("additionalLocations") or [])
                         if isinstance(x, str)]
            all_locs = [x.strip() for x in all_locs if x and x.strip()]
        except Exception:
            pass   # keep the posting; screen on what we have

        # Some tenants omit locationsText entirely.
        # The location is in the URL path either way: /job/So-Paulo---Barueri/
        loc = p.get("locationsText", "") or ""
        if MULTI_LOC_RE.match(loc.strip()) and all_locs:
            loc = all_locs[0]
        if not loc.strip():
            m = re.match(r"/job/([^/]+)/", path or "")
            if m:
                loc = urllib.parse.unquote(m.group(1)).replace("-", " ")
                loc = re.sub(r"\s+", " ", loc).strip()

        jobs.append({
            "id": f"wd-{tenant}-{path or p.get('title')}",
            "company": entry.get("slug", tenant),
            "title": p.get("title", ""),
            "location": loc,
            "locations": all_locs,
            "url": f"{base}/en-US/{site}{path}",
            "body": body,
            "updated": p.get("postedOn", ""),
            "lane": entry.get("lane", ""),
        })
        time.sleep(0.3)

    note_board(entry.get("slug", tenant), "workday",
               listed=total or 0, matched=len(candidates))
    return jobs


def pull_adapter(entry):
    """Drive one of the ats_adapters pullers.

    Some of those boards are server-rendered HTML and expose no description in
    the listing, the same constraint Workday has. Screen on title first, then
    fetch detail pages only for the handful that survive. On a 500-role board
    that is a few requests instead of five hundred.
    """
    ats = entry["ats"]
    try:
        listed = A.PULLERS[ats](entry)
    except Exception as exc:
        print(f"  warn: {entry['slug']} ({ats}) failed: {exc}",
              file=sys.stderr)
        note_board(entry["slug"], ats, listed=0, matched=0,
                   error=f"{type(exc).__name__}: {exc}"[:80])
        return []

    for job in listed:
        job["lane"] = entry.get("lane", "")

    if ats not in A.DETAIL_NEEDED:
        note_board(entry["slug"], ats, listed=len(listed),
                   matched=len(listed))
        return listed

    jobs = []
    for job in listed:
        if not title_ok(job["title"], entry.get("lane")):
            continue
        try:
            body, loc = A.fetch_detail(entry, job)
            # Adapters are the most likely thing to drift when a vendor
            # changes a template. Coerce rather than trust, so a bad record
            # can never take down a run that has already done the slow part.
            job["body"] = body if isinstance(body, str) else str(body)
            if isinstance(loc, str) and loc:
                job["location"] = loc
        except Exception as exc:
            print(f"  warn: detail fetch failed for {job['url'][:70]}: {exc}",
                  file=sys.stderr)
        jobs.append(job)
        time.sleep(0.3)

    note_board(entry["slug"], ats, listed=len(listed), matched=len(jobs))
    return jobs


def pull_all(boards):
    templates = {
        "greenhouse":
            "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true",
        "lever": "https://api.lever.co/v0/postings/{slug}?mode=json",
        "ashby": "https://api.ashbyhq.com/posting-api/job-board/{slug}"
                 "?includeCompensation=true",
    }

    jobs = []
    for entry in boards:
        slug, ats = entry["slug"], entry["ats"]

        if ats == "indeed_email":
            # Indeed blocks automated access to its job pages, so the only
            # text available is the alert email itself.
            try:
                got, n_mail = IE.pull_indeed_email(IE.IMAP_USER or SMTP_USER,
                                                   IE.IMAP_PASS or SMTP_PASS)
                for job in got:
                    job["lane"] = ("faculty" if faculty_title_ok(job["title"])
                                   else "")
                    job["indeed_note"] = True
                note_board(slug, ats, listed=len(got), matched=len(got))
            except Exception as exc:
                got = []
                print(f"  warn: {slug} failed: {exc}", file=sys.stderr)
                note_board(slug, ats, listed=0, matched=0,
                           error=f"{type(exc).__name__}: {exc}"[:80])
            jobs.extend(got)
            print(f"  {slug:<28} {len(got):>4} jobs from Indeed email")
            continue

        if ats == "workday":
            got = pull_workday(entry)
            jobs.extend(got)
            print(f"  {slug:<28} {len(got):>4} title matches (Workday)")
            continue

        if ats in A.PULLERS:
            got = pull_adapter(entry)
            jobs.extend(got)
            print(f"  {slug:<28} {len(got):>4} title matches ({ats})")
            continue

        if ats not in templates:
            print(f"  warn: {slug} has unknown ats '{ats}', skipped",
                  file=sys.stderr)
            note_board(slug, ats, listed=0, matched=0,
                       error=f"unknown ats '{ats}'")
            continue

        url = templates[ats].format(slug=slug)
        try:
            payload = fetch(url)
        except Exception as exc:
            print(f"  warn: {slug} ({ats}) failed: {exc}", file=sys.stderr)
            note_board(slug, ats, listed=0, matched=0,
                       error=f"{type(exc).__name__}: {exc}"[:80])
            continue
        got = normalise(ats, slug, payload)
        note_board(slug, ats, listed=len(got), matched=len(got))
        jobs.extend(got)
        print(f"  {slug:<28} {len(got):>4} postings")
        time.sleep(0.4)
    return jobs


# ---------------------------------------------------------------------------
# Screening
# ---------------------------------------------------------------------------

def parse_salary(text, min_value=40_000):
    """Return (low, high) of the stated band, or (0, 0) if none is stated.

    The earlier version returned only the highest figure, which was actively
    misleading: a band of $70,000 to $130,000 was reported as "$130,000+" as
    though that were the floor. On a wide band the realistic offer is nearer
    the middle, so the range is what matters and the caller gets both ends.

    Deliberately permissive about missing bands. Most postings publish none,
    and dropping those would hide real matches.
    """
    vals = []
    for m in SALARY_RE.finditer(text[:6000]):
        whole, thousands = m.group(1), m.group(2)
        val = int(whole + thousands) if thousands else int(whole) * 1000
        if min_value <= val <= 600_000:
            vals.append(val)
    if not vals:
        return 0, 0
    return min(vals), max(vals)


def _text(job, key):
    """Field values come from six different vendors' JSON and HTML. Anything
    that is not a string is a bug upstream, but it must not be fatal here."""
    v = job.get(key, "")
    if isinstance(v, str):
        return v.lower()
    if isinstance(v, (list, tuple)):
        return " ".join(str(x) for x in v).lower()
    return str(v or "").lower()


def location_verdict(loc, is_remote):
    """Return None if the location is acceptable, else a rejection reason.

    Ordered deliberately. A north-metro address fails even when the posting
    also says remote, because those roles are usually hybrid. A non-US
    location fails even when it says remote, because "EMEA Remote" cannot be
    held from the US.
    """
    if any(k in loc for k in LOCATION_KILL):
        return f"excluded area: {loc}"
    if any(c in loc for c in NON_US) or re.search(r"\buk\b", loc):
        return f"outside the US: {loc}"
    if any(v in loc for v in VIABLE_LOCAL):
        return None
    if is_remote:
        return None
    if not loc.strip():
        return "UNKNOWN"
    # "USA - Missouri - St. Louis" contains "usa" but is not a US-wide
    # posting. Strip the country-wide words and see whether an actual place
    # is left behind.
    residue = loc
    for word in US_WIDE:
        residue = residue.replace(word, " ")
    residue = re.sub(r"[^a-z]+", " ", residue).strip()
    if not residue:
        return None
    if HOME.search(loc):
        # Home state, but not in the local places list. Surfaced separately rather
        # than dropped, because the table is a list of towns, not a circle.
        return f"OUTSIDE RADIUS: {loc}"
    return f"location: {loc}"


def load_applied():
    """Postings already applied to, so they stop resurfacing.

    Kept separate from seen.json on purpose. seen.json is "the monitor has
    shown me this"; applied.json is "I acted on this and here is what
    happened". A role can requalify and reappear months later, and without
    this the digest would offer it again as though it were new.
    """
    if not APPLIED.exists():
        return []
    try:
        return json.loads(APPLIED.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        print("applied.json is not valid JSON, ignoring it", file=sys.stderr)
        return []


def _norm(text):
    return re.sub(r"[^a-z0-9]+", "", (text or "").lower())


def already_applied(job, applied):
    """Match on requisition URL first, then on company plus title.

    URL matching alone is not enough: the same requisition can surface with a
    different locale prefix or tracking suffix, and a reposted role gets a new
    id entirely while being the same job already applied to.
    """
    url = _norm(job.get("url"))
    title = _norm(job.get("title"))
    company = _norm(job.get("company"))
    for a in applied:
        au = _norm(a.get("url"))
        if au and url and (au in url or url in au):
            return a
        if (a.get("req") and job.get("url")
                and a["req"].lower() in job["url"].lower()):
            return a
        if (title and company
                and _norm(a.get("title")) == title
                and _norm(a.get("company")) == company):
            return a
    return None


def backfill_location(job):
    """Recover a missing location from the posting URL or title.

    This runs at screening time, not at pull time, on purpose. Derived data
    computed during the pull is frozen into raw_jobs.json, which means a cached
    replay would screen differently from a live run. Doing it here is
    idempotent and keeps --from-cache faithful.
    """
    have = (job.get("location") or "").strip()
    if have and not MULTI_LOC_RE.match(have):
        return

    jid = job.get("id") or ""

    # Workday: some tenants omit locationsText, but every posting URL carries
    # the location as a path segment.
    if jid.startswith("wd-"):
        m = re.search(r"/job/([^/]+)/", job.get("url") or "")
        if m:
            loc = urllib.parse.unquote(m.group(1)).replace("-", " ")
            job["location"] = re.sub(r"\s+", " ", loc).strip()
            return

    # SuccessFactors packs "<City> <Title> <ST> <ZIP>" into the URL slug.
    if jid.startswith("sf-"):
        job["location"] = A.sf_location_from_title(job.get("title", ""))


FACULTY_KILL_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(k) for k in FACULTY_TITLE_KILL) + ")",
    re.IGNORECASE)

# "Science" alone is a fit; these compounds are other fields.
OTHER_SCIENCES = re.compile(
    r"\b(computer|political|data|social|exercise|fire|animal|veterinary|"
    r"nursing|health ?care|library|information|actuarial|culinary|"
    r"behavioral|cognitive|sport|sports|food)\s+sciences?\b", re.IGNORECASE)


def faculty_title_ok(title):
    t = title or ""
    return (bool(FACULTY_TITLE.search(t))
            and not FACULTY_NOT_TEACHING.search(t)
            and not FACULTY_KILL_RE.search(t))


def screen_faculty(job):
    """Screen a posting from a faculty-lane board. Same (passed, reason)
    contract as screen()."""
    backfill_location(job)
    title_raw = job.get("title") or ""
    title = title_raw.lower()
    body = _text(job, "body")
    loc = _text(job, "location")

    if not FACULTY_TITLE.search(title) or FACULTY_NOT_TEACHING.search(title):
        return False, "title not a match"
    m = FACULTY_KILL_RE.search(title)
    if m:
        return False, f"disqualifying title: {m.group(0)}"

    # Full-time. A part-time signal with no full-time signal is a rejection.
    # Neither signal is let through with a note, since many postings never
    # say, and hiding them would cost real roles.
    head = title + " " + body[:4000]
    ft, pt = FT_EVIDENCE.search(head), PT_EVIDENCE.search(head)
    if pt and not ft:
        return False, "part-time or adjunct"
    if not ft:
        job["ft_unclear"] = True

    # Remote only: "online" teaching is not enough, the work itself has to be
    # remote. Some universities hire "online" faculty who work on campus.
    remote = (bool(REMOTE_STRONG.search(title + " " + loc + " " + body[:5000]))
              or "remote" in loc or "virtual" in loc or "telecommute" in loc
              or loc.strip() in ("united states", "usa", "us",
                                 "united states of america"))
    if not remote or NOT_REMOTE.search(body[:8000]):
        return False, "not remote (campus-based or remote not stated)"
    if CAMPUS_REQUIRED.search(body[:8000]):
        return False, "on-campus presence required"
    if any(k in loc for k in LOCATION_KILL):
        return False, f"excluded area: {loc}"
    if any(c in loc for c in NON_US):
        return False, f"outside the US: {loc}"
    where = residency_blocker(body)
    if where:
        return False, f"residency requirement: {where}"

    # Credentials. Title first, since a faculty title nearly always names
    # the discipline; the opening of the body only when the title does not.
    def disciplines(text):
        text = OTHER_SCIENCES.sub(" ", text)
        return [(label, note) for pat, label, note in FACULTY_DISCIPLINES
                if re.search(pat, text, re.IGNORECASE)]
    fits = disciplines(title) or disciplines(body[:600])
    if not fits:
        return False, "discipline outside credentials"
    job["credential"] = "; ".join(dict.fromkeys(l for l, _ in fits))
    notes = [n for _, n in fits if n]
    if notes:
        job["credential_note"] = notes[0]

    for pat, label in FACULTY_CRED_KILL:
        if re.search(pat, body):
            return False, f"structural blocker: {label}"

    # Faculty bands run lower than corporate ones, so read figures down to
    # $25K. A stray benefit figure can trip this; those land in the
    # "filtered out on my judgement" list where a human can overrule them.
    low, high = parse_salary(body, min_value=25_000)
    if high and high < FACULTY_MIN_SALARY:
        return False, f"band tops at ${high:,}"
    job["salary"] = high
    job["salary_low"] = low
    job["salary_high"] = high
    if low and high and (low + high) // 2 < FACULTY_MIN_SALARY:
        job["band_warning"] = (f"midpoint ${(low + high) // 2:,} is below "
                               f"${FACULTY_MIN_SALARY:,}")

    conflicts = [label for pat, label in SCHEDULE_CONFLICTS
                 if re.search(pat, body, re.IGNORECASE)]
    flex = [label for pat, label in SCHEDULE_FLEX
            if re.search(pat, body, re.IGNORECASE)]
    if conflicts:
        job["schedule_flags"] = list(dict.fromkeys(conflicts))
    if flex:
        job["schedule_flex"] = list(dict.fromkeys(flex))
    job["remote"] = True
    return True, ""


def screen(job):
    """Return (passed, reason). Reason explains a rejection, for the log."""
    if job.get("lane") == "faculty":
        return screen_faculty(job)
    backfill_location(job)
    title = clean_title(_text(job, "title"))
    body = _text(job, "body")
    loc = _text(job, "location")

    if not any(sig in title for sig in TITLE_SIGNALS):
        return False, "title not a match"

    if any(kill in title for kill in TITLE_KILL):
        return False, "disqualifying title"

    # A Workday requisition can carry several location anchors. The state in
    # "US, CA, Remote" is a payroll anchor that sets the posted band, and
    # many employers say nothing about residency, so a state-anchored
    # remote req is not a geographic disqualifier. Judge every anchor and keep
    # the most favourable verdict, since one bad anchor should not sink a
    # posting that also lists a nationwide remote option.
    anchors = [str(x or "").lower().strip()
               for x in (job.get("locations") or [])]
    anchors = [a for a in anchors if a] or [loc]

    def us_wide_only(a):
        rest = a
        for w in US_WIDE:
            rest = rest.replace(w, " ")
        return bool(a.strip()) and not re.sub(r"[^a-z]+", " ", rest).strip()
    anchor_remote = (any("remote" in a or "virtual" in a or us_wide_only(a)
                         for a in anchors)
                     or "remote" in title or "flex location" in title)
    is_remote = ((anchor_remote or bool(REMOTE_STRONG.search(body[:5000])))
                 and not NOT_REMOTE.search(body[:8000]))

    verdicts = [location_verdict(a, is_remote) for a in anchors]
    if any(v is None for v in verdicts):
        verdict = None
        passing = [a for a, v in zip(anchors, verdicts) if v is None]
        # Name the anchor that can be acted on, so the digest shows the
        # remote or local option ahead of a distant headquarters.
        loc = next((a for a in passing
                    if "remote" in a or "virtual" in a
                    or any(g in a for g in VIABLE_LOCAL)), passing[0])
        job["location"] = loc
    elif any(v == "UNKNOWN" for v in verdicts):
        verdict = "UNKNOWN"
    else:
        # Prefer a radius note over a flat rejection when both are present.
        verdict = next((v for v in verdicts
                        if str(v).startswith("OUTSIDE RADIUS")), verdicts[0])

    if verdict == "UNKNOWN":
        # No parseable location at all. Dropping these would silently hide
        # real roles, so let them through flagged for a manual check.
        job["location_unknown"] = True
    elif verdict is not None:
        return False, verdict

    # Remote only. An on-site or hybrid role in the local area goes to a
    # short FYI list; anywhere else it is dropped.
    if not is_remote:
        # The home state must be named: a local town name can also be a
        # street or a town in another state.
        home = HOME.search(loc)
        if verdict is None and home and any(g in loc for g in VIABLE_LOCAL):
            return False, f"LOCAL ONSITE: {loc}"
        return False, f"not remote: {loc or 'location not stated'}"

    # A "remote" posting only counts if it can be held from the home state.
    # Skipped when the passing location is already in the local area.
    if is_remote and not any(g in loc for g in VIABLE_LOCAL):
        where = residency_blocker(body)
        if where:
            return False, f"residency requirement: {where}"

    for pattern in BODY_KILL:
        if re.search(pattern, body):
            return False, ("structural blocker: "
                           + BODY_KILL_LABELS.get(pattern, pattern[:40]))

    gate = years_blocker(body)
    if gate.startswith("STRETCH"):
        # Surfaced in its own section rather than dropped or mixed in with
        # the clean matches, so the experience bar is visible up front.
        job["stretch"] = gate.split(": ", 1)[1]
    elif gate:
        return False, f"never-held field: {gate}"

    low, high = parse_salary(body)   # the normalised copy, never the raw field
    if high and high < MIN_SALARY:
        return False, f"band tops at ${high:,}"

    job["salary"] = high
    job["salary_low"] = low
    job["salary_high"] = high
    # A wide band whose midpoint sits under the floor will usually be offered
    # under the floor. Worth seeing, not worth hiding.
    if low and high and (low + high) // 2 < MIN_SALARY:
        job["band_warning"] = (f"midpoint ${(low + high) // 2:,} is below "
                               f"${MIN_SALARY:,}")
    job["remote"] = is_remote
    return True, ""


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

# Rejection reasons that represent a judgement about the candidate. A job in
# Warsaw is a plain fact about the posting. "Ten years in a field you have
# never worked in" is a judgement call, and job descriptions are often
# aspirational, so these are listed for a human to see and overrule.
AUDITABLE = {"never-held field", "structural blocker", "band tops at",
             "residency requirement"}


def build_digest(matches, near=(), overruled=()):
    today = datetime.now().strftime("%A, %B %d %Y")
    lines = [f"# New matches, {today}", ""]

    faculty_header_done = False
    for j in sorted(matches, key=lambda x: (x.get("lane") == "faculty",
                                            bool(x.get("stretch")),
                                            x["company"])):
        if j.get("lane") == "faculty" and not faculty_header_done:
            faculty_header_done = True
            lines.append("# Full-time online faculty")
            lines.append("")
            lines.append(P.FACULTY_LANE_SUMMARY)
            lines.append("")
        lo, hi = j.get("salary_low"), j.get("salary_high")
        if lo and hi and lo != hi:
            pay = f"${lo:,} to ${hi:,}"
        elif hi:
            pay = f"${hi:,}"
        else:
            pay = "band not stated"
        if j.get("remote"):
            tag = "Remote"
        elif j.get("location_unknown"):
            tag = "location not published, verify before applying"
        else:
            tag = j["location"]
        lines.append(f"## {j['title']}")
        lines.append(f"**{j['company']}** | {tag} | {pay}")
        if j.get("stretch"):
            lines.append("")
            lines.append(f"_Stretch: {j['stretch']}. Right function, high "
                         f"experience bar._")
        if j.get("band_warning"):
            lines.append("")
            lines.append(f"_Band caution: {j['band_warning']}._")
        if j.get("indeed_note"):
            lines.append("")
            lines.append("_From an Indeed email. Only the email text was "
                         "screened; read the full description for state or "
                         "residency limits before applying._")
        if j.get("lane") == "faculty":
            lines.append("")
            fit = f"_Credential fit: {j.get('credential', '?')}"
            if j.get("credential_note"):
                fit += f" ({j['credential_note']})"
            lines.append(fit + "._")
            if j.get("ft_unclear"):
                lines.append("")
                lines.append("_Full-time status not stated. Verify._")
            lines.append("")
            if j.get("schedule_flags"):
                lines.append(f"**Possible conflict with {P.PRIMARY_JOB_LABEL}: "
                             + "; ".join(j["schedule_flags"]) + ".**")
            else:
                lines.append("_No fixed-hours or live-session language "
                             "found. Confirm expectations at interview._")
            if j.get("schedule_flex"):
                lines.append("")
                lines.append("_Flexibility signals: "
                             + "; ".join(j["schedule_flex"]) + "._")
            if "(via " in j.get("company", "") and not j.get("indeed_note"):
                lines.append("")
                lines.append("_Listed on a job board. Confirm on the "
                             "institution's own careers site before "
                             "applying._")
        lines.append("")
        snippet = j["body"][:280].rsplit(" ", 1)[0]
        lines.append(f"{snippet}...")
        lines.append("")
        lines.append(j["url"])
        lines.append("")
        lines.append("---")
        lines.append("")

    if overruled:
        lines.append("## Filtered out on my judgement, not yours")
        lines.append("")
        lines.append("Job descriptions are frequently aspirational rather "
                     "than binding. These were rejected on a stated "
                     "requirement. Overrule any of them.")
        lines.append("")
        for j, why in overruled[:25]:
            lines.append(f"- **{j['title']}**, {j['company']}: {why}")
            lines.append(f"  {j['url']}")
        if len(overruled) > 25:
            lines.append(f"- ...and {len(overruled) - 25} more")
        lines.append("")

    if near:
        lines.append("## Near you, but not remote (FYI only)")
        lines.append("")
        lines.append("On-site or hybrid roles in your area. They are not "
                     "remote, so they are listed for awareness only.")
        lines.append("")
        for j, where in near:
            lines.append(f"- **{j['title']}**, {j['company']}: {where}")
            lines.append(f"  {j['url']}")
        lines.append("")

    return "\n".join(lines)


def board_health(boards):
    """Render per-board results, flagging anything that answered with nothing.

    A board returning zero is not automatically broken. Some are small and
    genuinely have no matching titles. But a board that returned hundreds
    last week and zero today is the failure this whole report exists to
    surface, so the previous count from boards.json is shown beside it.
    """
    prior = {b["slug"]: b.get("open_at_probe") for b in boards}
    quiet = {b["slug"]: b.get("quiet_ok") for b in boards}
    lines = ["| board | listed | title matches | at setup | note |",
             "|---|---|---|---|---|"]
    suspects = []

    for slug in sorted(BOARD_STATS):
        r = BOARD_STATS[slug]
        listed = r["listed"]
        was = prior.get(slug)
        note = r["error"]
        if r["error"]:
            suspects.append(f"{slug}: {r['error']}")
        elif listed == 0 and quiet.get(slug):
            note = "no new emails (normal)"
        elif listed == 0:
            note = "returned nothing"
            if was:
                note += f", had {was} at setup"
            suspects.append(f"{slug}: returned nothing (had {was} at setup)")
        elif was and listed < was * 0.5:
            note = "less than half the postings seen at setup"
            suspects.append(f"{slug}: {listed} now vs {was} at setup")
        lines.append(f"| {slug} | {listed if listed is not None else '?'} "
                     f"| {r['matched'] if r['matched'] is not None else '?'} "
                     f"| {was if was else '-'} | {note} |")

    missing = [b["slug"] for b in boards if b["slug"] not in BOARD_STATS]
    for slug in missing:
        suspects.append(f"{slug}: never reported, it may have crashed")

    return "\n".join(lines), suspects


def heartbeat_due():
    """True when the run should report in even with nothing to show.

    Daily by default. The whole status goes in the subject line, so a routine
    "nothing today" never needs opening, and its absence tomorrow is visible
    at a glance in the inbox list. That is the point: silence has to mean
    something is wrong, not "probably fine".

    HEARTBEAT_DAY accepts "daily" (default), a weekday 0=Monday..6=Sunday,
    or "never".
    """
    setting = os.environ.get("HEARTBEAT_DAY", "daily").strip().lower()
    if setting in ("never", "off", "none"):
        return False
    if setting in ("daily", "always", ""):
        return True
    try:
        return datetime.now().weekday() == int(setting)
    except ValueError:
        return True


def send(subject, body):
    if not (SMTP_USER and SMTP_PASS):
        print("SMTP not configured, digest written to disk only.")
        return
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = SMTP_USER
    msg["To"] = MAIL_TO
    msg.set_content(body)
    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=ctx) as s:
        s.login(SMTP_USER, SMTP_PASS)
        s.send_message(msg)
    print(f"Digest emailed to {MAIL_TO}")


# ---------------------------------------------------------------------------

def main():
    if not BOARDS.exists():
        sys.exit("boards.json missing. Run probe_boards.py first.")

    if "--applied" in sys.argv:
        i = sys.argv.index("--applied")
        rest = sys.argv[i + 1:]
        if not rest:
            sys.exit('Usage: py job_monitor.py --applied "<url>" '
                     '[--outcome rejected] [--title "..."] '
                     '[--company "..."]')
        entry = {"url": rest[0], "date": datetime.now().strftime("%Y-%m-%d"),
                 "outcome": "applied"}
        for key in ("outcome", "title", "company", "req"):
            flag = f"--{key}"
            if flag in sys.argv:
                entry[key] = sys.argv[sys.argv.index(flag) + 1]
        applied = load_applied()
        applied.append(entry)
        APPLIED.write_text(json.dumps(applied, indent=2), encoding="utf-8")
        print(f"Recorded: {entry.get('title') or entry['url'][:70]}")
        print(f"          outcome={entry['outcome']}  "
              f"({len(applied)} tracked)")
        return

    if "--applications" in sys.argv:
        applied = load_applied()
        if not applied:
            print("Nothing recorded yet.")
            return
        print(f"{len(applied)} applications tracked:\n")
        for a in sorted(applied, key=lambda x: x.get("date", "")):
            print(f"  {a.get('date','?'):<12} {a.get('outcome','applied'):<10} "
                  f"{(a.get('company') or '?'):<14} "
                  f"{(a.get('title') or a.get('url',''))[:56]}")
        return

    if "--test-email" in sys.argv:
        # Confirms SMTP works before the first unattended 3am run, rather
        # than discovering a bad app password by getting no mail for a week.
        if not SMTP_USER:
            sys.exit("SMTP_USER is not set. Open a NEW terminal after setx, "
                     "or the environment variables will not be visible.")
        if not SMTP_PASS:
            sys.exit("SMTP_PASS is not set. Same reason.")
        print(f"Sending a test message as {SMTP_USER} to {MAIL_TO} ...")
        try:
            send("Job monitor test", "If you are reading this, SMTP works "
                 "and the nightly digest will reach you.")
            print("Sent. Check your inbox.")
        except Exception as exc:
            print(f"FAILED: {type(exc).__name__}: {exc}")
            print("\nMost likely causes: the app password still has spaces "
                  "in it, 2-Step Verification is off, or this terminal was "
                  "opened before setx ran.")
        return

    if "--seed-from-cache" in sys.argv:
        # Seeds seen.json from the postings already pulled tonight, so the
        # first scheduled run reports genuinely new listings instead of
        # repeating everything, and without a second full pull.
        cache = HERE / "raw_jobs.json"
        if not cache.exists():
            sys.exit("raw_jobs.json missing. Run --report first.")
        ids = sorted({j["id"] for j in
                      json.loads(cache.read_text(encoding="utf-8"))})
        SEEN.write_text(json.dumps(ids, indent=0), encoding="utf-8")
        print(f"seen.json seeded with {len(ids)} postings from the cache. "
              f"The next scheduled run will report only what is new.")
        return

    # --report answers "what is open right now" instead of "what is new".
    # It never touches seen.json, so it can be run any number of times
    # without affecting the nightly diff.
    report = "--report" in sys.argv

    boards = json.loads(BOARDS.read_text())
    seen = set(json.loads(SEEN.read_text())) if SEEN.exists() else set()
    first_run = not SEEN.exists()

    if report:
        cache = HERE / "raw_jobs.json"
        if "--from-cache" in sys.argv:
            jobs = json.loads(cache.read_text(encoding="utf-8"))
            print(f"Loaded {len(jobs)} postings from {cache.name}, "
                  f"no network calls.")
        else:
            print(f"Pulling {len(boards)} boards for a full report...")
            jobs = pull_all(boards)
            # Save before screening. The pull is the slow, expensive part and
            # it should never have to be repeated because of a bug in the
            # cheap part that follows it.
            cache.write_text(json.dumps(jobs, indent=1), encoding="utf-8")
            print(f"\n{len(jobs)} postings retrieved, cached to {cache.name}")

        applied = load_applied()
        matches, near, overruled, seen_before, reasons = [], [], [], [], {}
        for job in jobs:
            hit = already_applied(job, applied)
            if hit:
                seen_before.append((job, hit))
                continue
            try:
                ok, reason = screen(job)
            except Exception as exc:
                print(f"  warn: screen failed on "
                      f"{str(job.get('title'))[:50]}: {exc}", file=sys.stderr)
                continue
            if ok:
                matches.append(job)
            elif reason.startswith(("OUTSIDE RADIUS", "LOCAL ONSITE")):
                # In the home state but outside the local places list. The
                # list is a set of towns, not a circle, so these are shown rather than
                # dropped and the commute call is left to a human.
                near.append((job, reason.split(": ", 1)[-1]))
            else:
                key = reason.split(":")[0]
                reasons[key] = reasons.get(key, 0) + 1
                # Structural rejections are judgement calls made on the
                # candidate's behalf. Listing them lets a human overrule
                # the filter.
                if key in AUDITABLE:
                    overruled.append((job, reason))
        digest = build_digest(matches, near, overruled)
        DIGEST.write_text(digest, encoding="utf-8")
        print(f"\n{len(matches)} currently open matches, written to "
              f"{DIGEST.name}")
        print("\nScreened out:")
        for k, v in sorted(reasons.items(), key=lambda kv: -kv[1]):
            print(f"  {v:>5}  {k}")
        clean = [j for j in matches if not j.get("stretch")]
        stretch = [j for j in matches if j.get("stretch")]
        for j in sorted(clean, key=lambda x: x["company"]):
            tag = "Remote" if j.get("remote") else (j["location"] or "?")
            print(f"\n  {j['company']}: {j['title']}")
            print(f"    {tag}")
            print(f"    {j['url']}")

        if stretch:
            print(f"\n\nStretch, right function but a high experience bar "
                  f"({len(stretch)}):")
            for j in sorted(stretch, key=lambda x: x["company"]):
                tag = "Remote" if j.get("remote") else (j["location"] or "?")
                print(f"\n  {j['company']}: {j['title']}")
                print(f"    {tag}  |  {j['stretch']}")
                print(f"    {j['url']}")

        if seen_before:
            print(f"\n\nAlready applied, suppressed ({len(seen_before)}):")
            for j, a in seen_before:
                print(f"  {a.get('outcome','applied'):<10} "
                      f"{j['company']:<14} {j['title'][:48]}")

        if overruled:
            print(f"\n\nFiltered out on my judgement ({len(overruled)}). "
                  f"Overrule any of these:")
            for j, why in overruled[:20]:
                print(f"  {j['company']:<14} {j['title'][:44]:<46} {why}")
            if len(overruled) > 20:
                print(f"  ...and {len(overruled) - 20} more")

        if near:
            print(f"\n\nNear you, but not remote ({len(near)}). "
                  f"Judge the commute yourself:")
            for j, where in near:
                print(f"\n  {j['company']}: {j['title']}")
                print(f"    {where}")
                print(f"    {j['url']}")
        return

    print(f"Pulling {len(boards)} boards...")
    jobs = pull_all(boards)
    print(f"\n{len(jobs)} postings retrieved")

    applied = load_applied()
    matches, near, overruled, rejected = [], [], [], 0
    for job in jobs:
        if job["id"] in seen:
            continue
        if already_applied(job, applied):
            continue
        try:
            ok, reason = screen(job)
        except Exception as exc:
            print(f"  warn: screen failed on "
                  f"{str(job.get('title'))[:50]}: {exc}", file=sys.stderr)
            continue
        if ok:
            matches.append(job)
        elif reason.startswith(("OUTSIDE RADIUS", "LOCAL ONSITE")):
            near.append((job, reason.split(": ", 1)[-1]))
        else:
            rejected += 1
            if reason.split(":")[0] in AUDITABLE:
                overruled.append((job, reason))

    seen.update(j["id"] for j in jobs)
    SEEN.write_text(json.dumps(sorted(seen), indent=0))

    print(f"{len(matches)} new matches, {rejected} new postings screened out")

    if first_run:
        print("First run: seeding seen.json. No digest sent.")
        return

    health, suspects = board_health(boards)
    today = datetime.now().strftime("%A, %B %d %Y")

    if not matches:
        print("Nothing new today.")

        # Silence is ambiguous: "ran, nothing new" and "never ran" look
        # identical from an inbox. On the heartbeat day, say so out loud, and
        # say it immediately if a board looks broken rather than waiting.
        if heartbeat_due() or suspects:
            ok_boards = len(boards) - len(suspects)
            if suspects:
                subject = (f"Job monitor: 0 new, {len(suspects)} BOARD"
                           f"{'S' if len(suspects) > 1 else ''} FAILING")
            else:
                subject = f"Job monitor: 0 new, {ok_boards} boards OK"
            body = [f"# {subject}", "", today, "",
                    f"No new matches. {len(jobs)} postings pulled across "
                    f"{len(boards)} boards.", ""]
            if suspects:
                body += ["## Boards worth a look", ""]
                body += [f"- {x}" for x in suspects]
                body += [""]
            else:
                body += ["All boards answered normally.", ""]
            body += ["## Board detail", "", health, "",
                     "No mail at all tomorrow means the scheduled task did "
                     "not run. Check monitor.log."]
            send(subject, "\n".join(body))
        return

    digest = build_digest(matches, near, overruled)
    if suspects:
        digest += ("\n\n## Boards worth a look\n\n"
                   + "\n".join(f"- {x}" for x in suspects) + "\n")
    if heartbeat_due():
        digest += f"\n\n## Board detail\n\n{health}\n"
    DIGEST.write_text(digest, encoding="utf-8")
    subject = f"Job monitor: {len(matches)} new match" \
              f"{'es' if len(matches) != 1 else ''}"
    if suspects:
        subject += f", {len(suspects)} board issue" \
                   f"{'s' if len(suspects) > 1 else ''}"
    send(subject, digest)


if __name__ == "__main__":
    main()
