#!/usr/bin/env python3
"""
probe_boards.py

Figures out which ATS each target company uses and whether its public job feed
is reachable. Run this ONCE to build a validated config for job_monitor.py.

Greenhouse, Lever, and Ashby all expose unauthenticated JSON feeds. Workday,
Taleo, SuccessFactors and most proprietary systems do not, and those companies
have to be watched a different way (browser agent or email alerts).

Usage:
    python3 probe_boards.py                 # probe the default candidate list
    python3 probe_boards.py slugs.txt       # probe slugs from a file, one per line

Output:
    boards.json   -- validated config, feed straight into job_monitor.py
    unreachable.txt -- companies needing a non-API approach
"""

import json
import re
import sys
import time
import urllib.error
import urllib.request

TIMEOUT = 15
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# Workday careers URLs look like:
#   https://example.wd1.myworkdayjobs.com/en-US/example_external
#   https://example.wd5.myworkdayjobs.com/ExampleCareerSite
# The three parts we need are tenant, shard, and site.
WORKDAY_URL = re.compile(
    r"https?://(?P<tenant>[\w-]+)\.(?P<dc>wd\d+)\.myworkdayjobs\.com"
    r"(?:/[a-z]{2}-[A-Z]{2})?/(?P<site>[\w-]+)", re.IGNORECASE)

# Example slugs. These are GUESSES based on common naming conventions, and
# the probe is what makes them real. Replace them with your own list, or pass
# a file of slugs on the command line (see slugs.example.txt). Anything that
# fails here needs the company's careers URL inspected by hand, or a
# browser-based watch instead.
CANDIDATES = [
    "gitlab", "duolingo", "khanacademy", "coursera", "examplecompany",
]

ENDPOINTS = {
    "greenhouse": "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs",
    "lever": "https://api.lever.co/v0/postings/{slug}?mode=json",
    "ashby": "https://api.ashbyhq.com/posting-api/job-board/{slug}",
}


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        if resp.status != 200:
            return None
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def count_jobs(ats, payload):
    """Each ATS shapes its response differently."""
    if payload is None:
        return 0
    if ats == "greenhouse":
        return len(payload.get("jobs", []))
    if ats == "lever":
        return len(payload) if isinstance(payload, list) else 0
    if ats == "ashby":
        return len(payload.get("jobs", []))
    return 0


def probe(slug):
    """Try each ATS in turn. Return (ats, job_count) on the first real hit."""
    for ats, template in ENDPOINTS.items():
        url = template.format(slug=slug)
        try:
            payload = fetch(url)
        except (urllib.error.HTTPError, urllib.error.URLError,
                json.JSONDecodeError, TimeoutError):
            continue
        except Exception:
            continue

        n = count_jobs(ats, payload)
        # A board with zero postings is indistinguishable from a wrong slug,
        # so require at least one job before calling it a match.
        if n > 0:
            return ats, n
    return None, 0


def post(url, body, referer):
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


def probe_workday(url_line):
    """Parse a Workday careers URL and confirm its CXS endpoint responds."""
    m = WORKDAY_URL.search(url_line.strip())
    if not m:
        return None, "could not parse tenant/shard/site from URL"

    tenant, dc, site = m.group("tenant"), m.group("dc"), m.group("site")
    base = f"https://{tenant}.{dc}.myworkdayjobs.com"
    endpoint = f"{base}/wday/cxs/{tenant}/{site}/jobs"
    referer = f"{base}/en-US/{site}"

    try:
        payload = post(endpoint,
                       {"appliedFacets": {}, "limit": 1, "offset": 0,
                        "searchText": ""},
                       referer)
    except Exception as exc:
        return None, f"endpoint rejected the request: {exc}"

    total = payload.get("total", 0)
    if not total:
        return None, "endpoint responded but reported zero postings"

    return {"slug": tenant, "ats": "workday", "tenant": tenant,
            "dc": dc, "site": site, "open_at_probe": total}, ""


def workday_mode(path):
    """Read careers URLs from a file, validate each, append to boards.json."""
    # Lines look like "https://tenant.wdN.myworkdayjobs.com/Site  # label".
    # The label is optional and only controls how the company is named in the
    # digest. Without it the Workday tenant is used, which is often opaque
    # (some large employers run on a generic tenant name).
    urls = []
    with open(path) as fh:
        for ln in fh:
            ln = ln.strip()
            if not ln or ln.startswith("#"):
                continue
            url, _, label = ln.partition("#")
            urls.append((url.strip(), label.strip() or None))

    existing = []
    try:
        with open("boards.json") as fh:
            existing = json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        pass

    known = {e["slug"] for e in existing}
    added = 0

    for url, label in urls:
        entry, err = probe_workday(url)
        if entry:
            if label:
                entry["slug"] = label
            shown = f"{entry['slug']} ({entry['tenant']}/{entry['site']})"
            print(f"  OK    {shown:<42} {entry['open_at_probe']} open")
            if entry["slug"] not in known:
                existing.append(entry)
                known.add(entry["slug"])
                added += 1
        else:
            print(f"  --    {url[:42]:<42} {err}")
        time.sleep(0.5)

    with open("boards.json", "w") as fh:
        json.dump(existing, fh, indent=2)

    print(f"\n{added} Workday boards added. boards.json now has "
          f"{len(existing)} entries.")


def main():
    if len(sys.argv) > 2 and sys.argv[1] == "--workday":
        workday_mode(sys.argv[2])
        return

    slugs = CANDIDATES
    if len(sys.argv) > 1:
        with open(sys.argv[1]) as fh:
            slugs = [ln.strip() for ln in fh if ln.strip()
                     and not ln.startswith("#")]

    found, missing = [], []

    for slug in slugs:
        ats, n = probe(slug)
        if ats:
            print(f"  OK    {slug:<32} {ats:<12} {n} open")
            found.append({"slug": slug, "ats": ats, "open_at_probe": n})
        else:
            print(f"  --    {slug:<32} no public feed")
            missing.append(slug)
        time.sleep(0.4)  # be polite to the endpoints

    with open("boards.json", "w") as fh:
        json.dump(found, fh, indent=2)
    with open("unreachable.txt", "w") as fh:
        fh.write("\n".join(missing) + "\n")

    print(f"\n{len(found)} boards reachable, written to boards.json")
    print(f"{len(missing)} need another approach, listed in unreachable.txt")
    print("\nFor the unreachable ones: check the company's careers URL. If it "
          "redirects to boards.greenhouse.io/SOMETHING or jobs.lever.co/"
          "SOMETHING, that path segment is the real slug. Re-run with a "
          "corrected list. Anything on Workday, Taleo or SuccessFactors has "
          "no public feed and needs a browser watch or an email alert.")


if __name__ == "__main__":
    main()
