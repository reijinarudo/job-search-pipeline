#!/usr/bin/env python3
"""Regression checks for Workday multi-anchor location handling.

Written alongside the fix for the "2 Locations" gap, in which a requisition
carrying more than one location anchor was rejected outright because
Workday's search listing reports a count instead of a place. The checks run
against screening_example.py (a fictional profile near Denver, Colorado), so
they pass the same way on every machine. Two things are covered:

  screening   -- screen() judges every anchor and keeps the best verdict, and
                 the existing rejections (excluded area, non-US, out of area)
                 still fire.
  adapter     -- pull_workday() replaces "N Locations" with the real anchor
                 set from the posting's detail record.

Run it from this folder, with no network access required:

    python3 test_screen.py

Exit status is 0 when every check passes, 1 otherwise, so it can go straight
into run_monitor.bat as a pre-flight if that ever seems worth doing.
"""

import os

# Pin the example profile so a personal screening_local.py cannot change the
# expected results.
os.environ["JOB_MONITOR_PROFILE"] = "screening_example"

import job_monitor as J  # noqa: E402

TITLE = "Learning Program Manager"
BODY = "Lead the launch and growth of learning programs. " * 20

results = []


def check(name, job, should_pass, expect_loc=None):
    """Screen one synthetic posting and report whether it behaved."""
    j = dict(job)
    j.setdefault("body", BODY)
    j.setdefault("id", "wd-exampleco-/job/x/y_R1")
    j.setdefault(
        "url",
        "https://exampleco.wd5.myworkdayjobs.com/en-US/S/job/US-CA-Remote/T_R1")

    passed, reason = J.screen(j)
    ok = passed == should_pass
    detail = f"reason={reason!r}" if reason else ""

    if passed and expect_loc is not None:
        detail = f"location={j.get('location')!r}"
        if j.get("location") != expect_loc:
            ok = False
            detail += f" (expected {expect_loc!r})"

    print(f"  [{'ok' if ok else 'FAILED'}] {name}: {detail}")
    results.append(ok)


def screening_checks():
    print("screening")

    # The case behind the whole change. A state-anchored remote requisition is
    # a payroll anchor, so it must survive screening.
    check("state-anchored remote passes",
          {"title": TITLE, "location": "US, CA, Remote"},
          True, "us, ca, remote")

    # The gap this closes. The listing said "2 Locations" and hid a
    # nationwide remote anchor behind a headquarters address.
    check("collapsed listing finds the hidden US, Remote",
          {"title": TITLE, "location": "2 Locations",
           "locations": ["US, CA, Santa Clara", "US, Remote"]},
          True, "us, remote")

    # One unusable anchor must not sink a posting that also lists a good one.
    check("bad anchor does not sink a good one",
          {"title": TITLE, "location": "2 Locations",
           "locations": ["India, Bengaluru", "US, Remote"]},
          True, "us, remote")

    # Remote-only rule: an on-site local anchor does not pass. It goes to the
    # "near you, but not remote" FYI list instead.
    check("on-site in-radius goes to the FYI list, not matches",
          {"title": TITLE, "location": "2 Locations",
           "locations": ["US, CA, Santa Clara", "US, CO, Denver"]},
          False)
    j = {"title": TITLE, "location": "US, CO, Denver", "body": BODY,
         "id": "x", "url": "u"}
    ok = J.screen(j)[1].startswith("LOCAL ONSITE")
    print(f"  [{'ok' if ok else 'FAILED'}] local on-site is labelled LOCAL ONSITE")
    results.append(ok)

    check("UK remote fails",
          {"title": TITLE, "location": "UK, Remote",
           "locations": ["UK, Remote"]},
          False)

    check("remote in the title but hybrid in the body fails",
          {"title": TITLE + " (Remote)", "location": "US, Remote",
           "body": BODY + " This role is hybrid with 3 days a week in the office."},
          False)

    print("existing rejections still fire")

    check("on-site outside the local area still fails",
          {"title": TITLE, "location": "US, CA, Santa Clara",
           "locations": ["US, CA, Santa Clara"]},
          False)

    check("non-US remote still fails",
          {"title": TITLE, "location": "Australia, Remote",
           "locations": ["Australia, Remote"]},
          False)

    check("excluded area still fails",
          {"title": TITLE, "location": "US, CO, Boulder",
           "locations": ["US, CO, Boulder"]},
          False)

    print("records predating the change")

    check("no locations key, remote",
          {"title": TITLE, "location": "Remote - Anywhere USA"}, True)

    check("no locations key, Warsaw",
          {"title": TITLE, "location": "Poland, Warsaw"}, False)

    # A stale raw_jobs.json entry has the count but no anchor set, so it has
    # to fall back to the location in the posting URL.
    check("cached 2 Locations falls back to the URL",
          {"title": TITLE, "location": "2 Locations"}, True)


def adapter_check():
    """Drive pull_workday against canned Workday-shaped API responses."""
    print("adapter")

    listing = {"total": 2, "jobPostings": [
        {"title": "Learning Program Manager",
         "locationsText": "US, CA, Remote",
         "externalPath": "/job/US-CA-Remote/Learning-PM_R1001-1",
         "postedOn": "Posted 2 Days Ago"},
        {"title": "Training Manager, Customer Education",
         "locationsText": "2 Locations",
         "externalPath": "/job/US-CA-Santa-Clara/Training-Mgr_R1002-1",
         "postedOn": "Posted 30+ Days Ago"},
    ]}
    details = {
        "R1001": {"location": "US, CA, Remote",
                  "additionalLocations": []},
        "R1002": {"location": "US, CA, Santa Clara",
                  "additionalLocations": ["US, Remote"]},
    }

    def fake_detail(url):
        key = next(k for k in details if k in url)
        info = dict(details[key])
        info["jobDescription"] = "<p>Lead learning programs.</p>"
        return {"jobPostingInfo": info}

    saved = J.post_json, J.fetch, J.time.sleep
    J.post_json = lambda url, payload, referer=None: (
        listing if payload["offset"] == 0
        else {"total": 2, "jobPostings": []})
    J.fetch = fake_detail
    J.time.sleep = lambda *_: None
    try:
        jobs = J.pull_workday({"slug": "exampleco", "tenant": "exampleco",
                               "dc": "wd5",
                               "site": "ExampleCareerSite"})
    finally:
        J.post_json, J.fetch, J.time.sleep = saved

    ok = (len(jobs) == 2
          and jobs[1]["location"] == "US, CA, Santa Clara"
          and jobs[1]["locations"] == ["US, CA, Santa Clara", "US, Remote"])
    print(f"  [{'ok' if ok else 'FAILED'}] anchor set recovered from detail: "
          f"{jobs[1]['locations'] if len(jobs) > 1 else jobs}")
    results.append(ok)


def main():
    screening_checks()
    adapter_check()
    print()
    print(f"{sum(results)}/{len(results)} checks passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
