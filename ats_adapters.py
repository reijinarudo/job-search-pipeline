#!/usr/bin/env python3
"""
ats_adapters.py

Pull adapters for applicant tracking systems that publish no simple JSON
feed. Greenhouse, Lever, Ashby and Workday are handled in job_monitor.py.
Everything here covers the other vendors that large employers commonly run.

Stdlib only. Imported by both probe_ats.py and job_monitor.py.

Each adapter exposes one function, pull_<ats>(entry) -> list of records in the
common shape job_monitor.py already uses:

    {id, company, title, location, url, body, updated}

Adapters that scrape server-rendered HTML return an empty body from the list
step. job_monitor.py screens on title first and only fetches detail pages for
survivors, so an empty body at list time is expected and cheap.
"""

import html
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request

TIMEOUT = 25
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

POLITE = 0.4


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _open(url, data=None, headers=None):
    hdrs = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, data=data, headers=hdrs)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return resp.read().decode("utf-8", errors="replace")


def get_text(url, headers=None):
    return _open(url, headers=headers)


def get_json(url, headers=None):
    hdrs = {"Accept": "application/json"}
    if headers:
        hdrs.update(headers)
    return json.loads(_open(url, headers=hdrs))


def post_json(url, body, headers=None):
    hdrs = {"Accept": "application/json", "Content-Type": "application/json"}
    if headers:
        hdrs.update(headers)
    return json.loads(_open(url, data=json.dumps(body).encode(), headers=hdrs))


def strip_html(raw):
    if not raw:
        return ""
    text = html.unescape(raw)
    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", text)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def jsonld_location(page):
    """Pull the posting location out of the schema.org JobPosting block.

    Nearly every ATS emits one so that Google for Jobs can index the posting,
    which makes it far more reliable than scraping whatever the template calls
    its location label this quarter. Returns "" when there is no such block,
    and the caller decides what an unknown location means.
    """
    for m in re.finditer(
            r'(?is)<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>',
            page):
        try:
            data = json.loads(m.group(1).strip())
        except (json.JSONDecodeError, ValueError):
            continue
        for node in (data if isinstance(data, list) else [data]):
            if not isinstance(node, dict):
                continue
            if node.get("@type") not in ("JobPosting", None):
                continue
            locs = node.get("jobLocation")
            if not locs:
                continue
            parts = []
            for loc in (locs if isinstance(locs, list) else [locs]):
                addr = (loc or {}).get("address") or {}
                if isinstance(addr, dict):
                    bits = [addr.get("addressLocality"),
                            addr.get("addressRegion")]
                    got = ", ".join(b for b in bits if b)
                    if got:
                        parts.append(got)
            if node.get("jobLocationType") == "TELECOMMUTE":
                parts.append("Remote")
            if parts:
                return "; ".join(dict.fromkeys(parts))
    return ""


def unslug(slug):
    """iCIMS and SuccessFactors put a readable title in the URL path."""
    s = urllib.parse.unquote(slug).replace("-", " ").replace("_", " ")
    return re.sub(r"\s+", " ", s).strip()


# ---------------------------------------------------------------------------
# Workable
# Verified: GET returns JSON with a jobs array including full descriptions.
# ---------------------------------------------------------------------------

def pull_workable(entry):
    account = entry.get("account", entry["slug"])
    url = (f"https://apply.workable.com/api/v1/widget/accounts/"
           f"{account}?details=true")
    payload = get_json(url)
    out = []
    for j in payload.get("jobs", []):
        city = j.get("city") or ""
        country = j.get("country") or ""
        loc = ", ".join(p for p in (city, j.get("state") or "", country) if p)
        if j.get("telecommuting"):
            loc = (loc + " Remote").strip()
        out.append({
            "id": f"wk-{account}-{j.get('shortcode')}",
            "company": entry["slug"],
            "title": j.get("title", ""),
            "location": loc,
            "url": j.get("url", ""),
            "body": strip_html(j.get("description", "")),
            "updated": j.get("published_on", ""),
        })
    return out


# ---------------------------------------------------------------------------
# Eightfold
# The API is public but rejects requests without a Referer from the careers
# host, which is why a plain fetch returns 403.
# ---------------------------------------------------------------------------

def pull_eightfold(entry):
    host = entry["host"]
    domain = entry.get("domain", host.split(".", 1)[-1])
    ref = f"https://{host}/careers"
    out, start = [], 0

    while start < 1000:
        url = (f"https://{host}/api/apply/v2/jobs?domain={domain}"
               f"&start={start}&num=100&exclude_pid=&sort_by=relevance")
        payload = get_json(url, headers={"Referer": ref})
        positions = payload.get("positions", [])
        if not positions:
            break
        for p in positions:
            locs = p.get("locations") or []
            out.append({
                "id": f"ef-{entry['slug']}-{p.get('id')}",
                "company": entry["slug"],
                "title": p.get("name", ""),
                "location": "; ".join(locs) or (p.get("location") or ""),
                "url": p.get("canonicalPositionUrl")
                       or f"https://{host}/careers?pid={p.get('id')}",
                "body": strip_html(p.get("job_description", "")),
                "updated": str(p.get("t_create", "")),
            })
        start += 100
        if len(positions) < 100:
            break
        time.sleep(POLITE)
    return out


# ---------------------------------------------------------------------------
# Oracle Cloud Recruiting
# ---------------------------------------------------------------------------

def _oracle_list(host, site, limit, offset):
    finder = (f"findReqs;siteNumber={site},limit={limit},offset={offset},"
              f"sortBy=POSTING_DATES_DESC")
    url = (f"https://{host}/hcmRestApi/resources/latest/"
           f"recruitingCEJobRequisitions?onlyData=true"
           f"&expand=requisitionList"
           f"&finder={urllib.parse.quote(finder, safe='=;,')}")
    return get_json(url, headers={"Referer": f"https://{host}/"})


def pull_oracle_cx(entry):
    host, site = entry["host"], entry.get("site", "CX_1")
    slug = entry["slug"]
    out, offset = [], 0

    while offset < 2000:
        payload = _oracle_list(host, site, 200, offset)
        items = payload.get("items") or []
        if not items:
            break
        reqs = items[0].get("requisitionList") or []
        if not reqs:
            break
        for r in reqs:
            rid = r.get("Id") or r.get("RequisitionId")
            out.append({
                "id": f"or-{slug}-{rid}",
                "req_id": rid,
                "company": slug,
                "title": r.get("Title", ""),
                "location": r.get("PrimaryLocation", "")
                            or r.get("Location", ""),
                "url": (f"https://{host}/hcmUI/CandidateExperience/en/sites/"
                        f"{entry.get('site_path', site)}/job/{rid}"),
                "body": strip_html(r.get("ShortDescriptionStr", "")),
                "updated": r.get("PostedDate", ""),
            })
        total = items[0].get("TotalJobsCount") or len(reqs)
        offset += len(reqs)
        if offset >= total:
            break
        time.sleep(POLITE)
    return out


def oracle_detail(host, site, req_id):
    finder = 'ById;Id="%s",siteNumber=%s' % (req_id, site)
    encoded = urllib.parse.quote(finder, safe='=;,"')
    url = (f"https://{host}/hcmRestApi/resources/latest/"
           f"recruitingCEJobRequisitionDetails?onlyData=true&expand=all"
           f"&finder={encoded}")
    payload = get_json(url, headers={"Referer": f"https://{host}/"})
    items = payload.get("items") or []
    if not items:
        return ""
    return strip_html(items[0].get("ExternalDescriptionStr", "")
                      + " " + (items[0].get("ExternalQualificationsStr") or ""))


# ---------------------------------------------------------------------------
# SAP SuccessFactors RMK
# Server-rendered HTML. Job links are /job/<slug>/<id>/.
# ---------------------------------------------------------------------------

# The optional leading segment and "-en_US" suffix cover SuccessFactors
# sites that host several portals under one domain, for example
# /ExamplePortal/job/<slug>/37713-en_US.
SF_JOB = re.compile(
    r'href="(?:https?://[^"/]+)?(?P<path>(?:/[\w-]+)?/job/(?P<slug>[^"/]+)/'
    r'(?P<id>\d+)(?:-[a-z]{2}_[A-Z]{2})?/?)"', re.IGNORECASE)


# SuccessFactors truncates the state field in the URL slug, so these are
# matched on prefix rather than on a full name.
STATE_PREFIXES = {
    "geor": "GA", "nort": "NC", "sout": "SC", "virg": "VA", "alab": "AL",
    "tenn": "TN", "flor": "FL", "kent": "KY", "miss": "MS", "cali": "CA",
    "texa": "TX", "ohio": "OH", "indi": "IN", "illi": "IL", "penn": "PA",
    "mary": "MD", "arka": "AR", "okla": "OK", "colo": "CO", "arizo": "AZ",
    "wisc": "WI", "mich": "MI", "minn": "MN", "orego": "OR", "wash": "WA",
    "utah": "UT", "nevad": "NV", "iowa": "IA", "kansa": "KS", "nebr": "NE",
}


def sf_location_from_title(title):
    """SuccessFactors packs the location into the URL slug.

    The shape is "<City> <Job Title> <ST> <ZIP>", for example
    "Springfield Specialist II, Training Operations IL 62701". Recovering city and
    state here gives the geography filter something to work with before any
    detail page is fetched, instead of these defaulting to "location unknown"
    and passing the radius test by accident. Returns "" when the slug does
    not match the pattern, which is the honest answer.
    """
    head = re.match(r"([A-Z][A-Za-z.'\-]{2,20})\s", title)
    if not head:
        return ""

    # The slug ends either "... IL 62701" or, when the field was truncated,
    # "... Geor" for Georgia and "... Nort" for North Carolina. Both forms
    # have to resolve, because "Raleigh ... Nort" is a placed role that
    # may need to be rejected and would otherwise read as location unknown.
    tail = re.search(r"\b([A-Za-z.]{2,14})\s+\d{5}\s*$", title) \
        or re.search(r"\s([A-Za-z.]{2,14})\s*$", title)
    state = ""
    if tail:
        tok = tail.group(1).lower().rstrip(".")
        if len(tok) == 2 and tok.isalpha():
            state = tok.upper()
        else:
            for prefix, abbr in STATE_PREFIXES.items():
                if tok.startswith(prefix):
                    state = abbr
                    break
    if not state:
        return ""      # a city with no state is a guess, not a location
    return f"{head.group(1)}, {state}"


def pull_successfactors(entry):
    host = entry["host"]
    slug = entry["slug"]
    out, seen, startrow = [], set(), 0

    while startrow < 2000:
        q = urllib.parse.quote_plus(entry.get("query", ""))
        url = (f"https://{host}{entry.get('search_prefix', '')}/search/"
               f"?q={q}&sortColumn=referencedate"
               f"&sortDirection=desc&startrow={startrow}")
        try:
            page = get_text(url, headers={"Referer": f"https://{host}/"})
        except Exception:
            break

        found = 0
        for m in SF_JOB.finditer(page):
            jid = m.group("id")
            if jid in seen:
                continue
            seen.add(jid)
            found += 1
            title = unslug(m.group("slug"))
            loc = sf_location_from_title(title)

            out.append({
                "id": f"sf-{slug}-{jid}",
                "company": slug,
                "title": title,
                "location": loc,
                "url": f"https://{host}{m.group('path')}",
                "body": "",
                "updated": "",
            })
        if found == 0:
            break
        startrow += 25
        time.sleep(POLITE)
    return out


def successfactors_detail(url):
    page = get_text(url)
    body = re.search(r'(?is)<div[^>]+class="[^"]*jobdescription[^"]*"[^>]*>'
                     r'(.*?)</div>\s*<div', page)
    return strip_html(body.group(1) if body else page), jsonld_location(page)


# ---------------------------------------------------------------------------
# iCIMS
# Server-rendered search results. Job links are /jobs/<id>/<slug>/job.
# ---------------------------------------------------------------------------

ICIMS_JOB = re.compile(
    r'href="(?:https?://[^"/]+)?(?P<path>/jobs/(?P<id>\d+)/'
    r'(?P<slug>[^"/]+)/job)', re.IGNORECASE)


def pull_icims(entry):
    host = entry["host"]
    slug = entry["slug"]
    out, seen, page_no = [], set(), 1

    while page_no <= 40:
        url = (f"https://{host}/jobs/search?ss=1&searchRelation=keyword_all"
               f"&in_iframe=1&pr={page_no}")
        try:
            page = get_text(url, headers={"Referer": f"https://{host}/"})
        except Exception:
            break

        found = 0
        for m in ICIMS_JOB.finditer(page):
            jid = m.group("id")
            if jid in seen:
                continue
            seen.add(jid)
            found += 1
            out.append({
                "id": f"ic-{slug}-{jid}",
                "company": slug,
                "title": unslug(m.group("slug")),
                "location": "",
                "url": f"https://{host}{m.group('path')}",
                "body": "",
                "updated": "",
            })
        if found == 0:
            break
        page_no += 1
        time.sleep(POLITE)
    return out


def icims_detail(url):
    page = get_text(url)
    loc = jsonld_location(page)
    if not loc:
        m = re.search(r'(?is)Job Location[s]?\s*</[^>]+>\s*(.{0,200}?)</', page)
        if m:
            loc = strip_html(m.group(1))
    return strip_html(page), loc


# ---------------------------------------------------------------------------
# Phenom People CareerConnect
# The search-results page is a shell; the data comes from a POST to /widgets.
# ---------------------------------------------------------------------------

def _phenom_body(offset, size, site_type="external"):
    return {
        "lang": "en_us",
        "deviceType": "desktop",
        "country": "us",
        "pageName": "search-results",
        "ddoKey": "refineSearch",
        "sortBy": "",
        "subsearch": "",
        "from": offset,
        "jobs": True,
        "counts": True,
        "all_fields": ["category", "country", "state", "city", "type"],
        "size": size,
        "clearAll": False,
        "jdsource": "facets",
        "isSliderEnable": False,
        "pageId": "page11",
        "siteType": site_type,
        "keywords": "",
        "global": True,
    }


def pull_phenom(entry):
    host = entry["host"]
    slug = entry["slug"]
    url = f"https://{host}/widgets"
    ref = f"https://{host}/us/en/search-results"
    out, offset = [], 0

    while offset < 4000:
        payload = post_json(url, _phenom_body(offset, 100),
                            headers={"Referer": ref,
                                     "Origin": f"https://{host}"})
        # Phenom returns the payload under the ddoKey that was requested.
        # Older tenants use eagerLoadRefineSearch, current ones use
        # refineSearch, so accept either rather than assuming.
        block = (payload.get("refineSearch")
                 or payload.get("eagerLoadRefineSearch") or {})
        data = block.get("data") or {}
        jobs = data.get("jobs") or []
        if not jobs:
            break

        for j in jobs:
            jid = j.get("jobId") or j.get("reqId") or j.get("jobSeqNo")
            city = j.get("city") or ""
            state = j.get("state") or ""
            loc = (j.get("cityState")
                   or ", ".join(p for p in (city, state) if p)
                   or j.get("location") or "")
            if str(j.get("type", "")).lower() == "remote":
                loc = (loc + " Remote").strip()
            out.append({
                "id": f"ph-{slug}-{jid}",
                "company": slug,
                "title": j.get("title", ""),
                "location": loc,
                "url": j.get("applyUrl") or j.get("jobUrl") or "",
                "body": strip_html(j.get("descriptionTeaser", "")),
                "updated": j.get("postedDate", "") or j.get("dateCreated", ""),
            })

        total = block.get("totalHits") or len(jobs)
        offset += len(jobs)
        if offset >= total:
            break
        time.sleep(POLITE)
    return out


def phenom_detail(url):
    return strip_html(get_text(url))


# ---------------------------------------------------------------------------
# Madgex RSS  (Inside Higher Ed Careers, Chronicle of Higher Education Jobs)
# Used for the faculty lane. Both job boards run
# on Madgex, which publishes a keyword RSS feed capped at 20 items per query,
# so the board entry carries a list of narrow queries and the results are
# merged and de-duplicated by job id. Titles arrive as "Employer: Title".
# These are employer-posted listings, but they are still a job board, so the
# digest links the board page; verify on the employer's own site.
# ---------------------------------------------------------------------------

import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

MADGEX_MAX_AGE_DAYS = 120

MADGEX_ID = re.compile(r"/job/(\d+)/")


def pull_madgex_rss(entry):
    host, slug = entry["host"], entry["slug"]
    out, seen = [], set()
    for q in entry.get("queries", []):
        url = (f"https://{host}/jobsrss/?"
               + urllib.parse.urlencode({"keywords": q}))
        try:
            root = ET.fromstring(get_text(url).encode("utf-8"))
        except Exception:
            continue
        for item in root.iter("item"):
            link = (item.findtext("link") or "").strip()
            m = MADGEX_ID.search(link)
            if not m or m.group(1) in seen:
                continue
            seen.add(m.group(1))
            # Keyword feeds are ordered by relevance, not date, and carry
            # postings years old. Anything past MADGEX_MAX_AGE_DAYS is dropped.
            pub = item.findtext("pubDate") or ""
            try:
                age = (datetime.now(timezone.utc)
                       - parsedate_to_datetime(pub)).days
            except (TypeError, ValueError, IndexError):
                age = 0
            if age > entry.get("max_age_days", MADGEX_MAX_AGE_DAYS):
                continue
            raw = (item.findtext("title") or "").strip()
            employer, _, title = raw.partition(": ")
            if not title:
                employer, title = "", raw
            clean = link.split("?")[0]
            out.append({
                "id": f"mx-{slug}-{m.group(1)}",
                "company": f"{employer} (via {slug})" if employer else slug,
                "title": title,
                "location": "",
                "url": clean,
                "body": strip_html(item.findtext("description") or ""),
                "updated": item.findtext("pubDate") or "",
            })
        time.sleep(POLITE)
    return out


def madgex_detail(job):
    """Read the posting from its schema.org JobPosting block.

    The whole page cannot be used as the body: Madgex sidebars list facet
    labels such as "Part Time" and "Remote" on every posting, which would
    poison the full-time and online checks. The JSON-LD block carries only
    this posting's description, employment type, and location type.
    """
    page = get_text(job["url"])
    loc = jsonld_location(page)
    for m in re.finditer(
            r'(?is)<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>',
            page):
        try:
            data = json.loads(m.group(1).strip())
        except (json.JSONDecodeError, ValueError):
            continue
        for node in (data if isinstance(data, list) else [data]):
            if not isinstance(node, dict) or node.get("@type") != "JobPosting":
                continue
            parts = [strip_html(node.get("description") or "")]
            et = node.get("employmentType")
            if et:
                et = et if isinstance(et, list) else [et]
                parts.insert(0, "Employment type: "
                             + ", ".join(str(x) for x in et) + ".")
            sal = node.get("baseSalary")
            if isinstance(sal, dict):
                val = sal.get("value") or {}
                if isinstance(val, dict):
                    lo, hi = val.get("minValue"), val.get("maxValue")
                    def money(v):
                        try:
                            return f"${int(float(v)):,}"
                        except (TypeError, ValueError):
                            return ""
                    band = " - ".join(x for x in (money(lo), money(hi)) if x)
                    if band:
                        parts.insert(0, f"Salary: {band}.")
            return " ".join(p for p in parts if p), loc
    return job.get("body", ""), loc


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

PULLERS = {
    "workable": pull_workable,
    "eightfold": pull_eightfold,
    "oracle_cx": pull_oracle_cx,
    "successfactors": pull_successfactors,
    "icims": pull_icims,
    "phenom": pull_phenom,
    "madgex_rss": pull_madgex_rss,
}

# Adapters whose list step returns no description. job_monitor.py screens on
# title first, then calls the matching detail function for survivors only.
DETAIL_NEEDED = {"successfactors", "icims", "phenom",
                 "oracle_cx", "madgex_rss"}


def fetch_detail(entry, job):
    ats = entry["ats"]
    if ats == "successfactors":
        # already returns (body, location), do not re-wrap it
        return successfactors_detail(job["url"])
    if ats == "icims":
        return icims_detail(job["url"])
    if ats == "oracle_cx":
        return (oracle_detail(entry["host"], entry.get("site", "CX_1"),
                              job.get("req_id", "")), job["location"])
    if ats == "phenom":
        # The listing teaser is a couple of sentences. A nursing licensure
        # requirement lives in the full posting, so pull that.
        return phenom_detail(job["url"]), job["location"]
    if ats == "madgex_rss":
        return madgex_detail(job)
    return job["body"], job["location"]
