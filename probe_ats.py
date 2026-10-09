#!/usr/bin/env python3
"""
probe_ats.py

Validates the non-Workday adapters against real boards and, for the ones that
answer, appends them to boards.json so job_monitor.py picks them up.

Why this exists as a separate step: the endpoints in ats_adapters.py were
identified from each company's own careers page, but identification is not
verification. Run this once and trust only what it marks OK.

Usage:
    python3 probe_ats.py                 # probe every candidate in the list
    python3 probe_ats.py --dry           # report only, do not touch boards.json
    python3 probe_ats.py --only icims    # probe one ATS family

Output:
    boards.json      -- validated entries appended
    ats_probe.log    -- full result, including the failures and why
"""

import json
import sys
import traceback
from pathlib import Path

import ats_adapters as A

HERE = Path(__file__).resolve().parent
BOARDS = HERE / "boards.json"
LOG = HERE / "ats_probe.log"

# Candidate boards come from ats_candidates.json when it exists (listed in
# .gitignore), otherwise from ats_candidates.example.json. Each entry should
# be read off the employer's own careers page; the probe decides whether it
# is real.
CANDIDATES_FILE = HERE / "ats_candidates.json"
if not CANDIDATES_FILE.exists():
    CANDIDATES_FILE = HERE / "ats_candidates.example.json"
CANDIDATES = json.loads(CANDIDATES_FILE.read_text())

# Boards that refuse non-browser clients. In testing, some iCIMS tenants
# returned 405 from CloudFront and some Eightfold tenants returned 403 to
# Python urllib, Windows curl.exe (Schannel) and PowerShell
# Invoke-WebRequest alike, on every path, with no cookie set. Three
# unrelated TLS stacks with an identical result points to a deliberate block
# at the CDN edge. Those employers need email job alerts or a browser
# session instead.

# Fallbacks worth trying automatically when the first guess fails, because the
# variable part is a site code that is not published anywhere.
ORACLE_SITES = ["CX_1", "CX_2", "CX_3", "CX_1001"]


def try_entry(entry):
    """Return (ok, count, sample_title, error)."""
    try:
        jobs = A.PULLERS[entry["ats"]](entry)
    except Exception as exc:
        return False, 0, "", f"{type(exc).__name__}: {exc}"
    if not jobs:
        return False, 0, "", "endpoint answered but returned zero postings"
    return True, len(jobs), jobs[0]["title"], ""


def probe(entry):
    ok, n, sample, err = try_entry(entry)

    # Oracle's siteNumber is not discoverable from the careers URL, so walk
    # the small set of values Oracle actually assigns.
    if not ok and entry["ats"] == "oracle_cx":
        for site in ORACLE_SITES:
            if site == entry.get("site"):
                continue
            trial = dict(entry, site=site)
            ok, n, sample, err = try_entry(trial)
            if ok:
                entry["site"] = site
                break

    return ok, n, sample, err


def main():
    dry = "--dry" in sys.argv
    only = None
    if "--only" in sys.argv:
        only = sys.argv[sys.argv.index("--only") + 1]

    todo = [c for c in CANDIDATES if only is None or c["ats"] == only]

    existing = []
    if BOARDS.exists():
        try:
            existing = json.loads(BOARDS.read_text())
        except json.JSONDecodeError:
            existing = []
    known = {e["slug"] for e in existing}

    lines, added = [], 0

    for entry in todo:
        label = f"{entry['slug']} ({entry['ats']})"
        try:
            ok, n, sample, err = probe(entry)
        except Exception:
            ok, n, sample, err = False, 0, "", traceback.format_exc(limit=1)

        if ok:
            print(f"  OK    {label:<44} {n:>5} postings   e.g. {sample[:44]}")
            lines.append(f"OK\t{label}\t{n}\t{sample}")
            entry = dict(entry, open_at_probe=n)
            if entry["slug"] not in known:
                existing.append(entry)
                known.add(entry["slug"])
                added += 1
        else:
            print(f"  --    {label:<44} {err[:70]}")
            lines.append(f"FAIL\t{label}\t{err}")

    LOG.write_text("\n".join(lines) + "\n")

    if dry:
        print("\n--dry: boards.json not modified.")
        return

    BOARDS.write_text(json.dumps(existing, indent=2))
    print(f"\n{added} new boards added. boards.json now has "
          f"{len(existing)} entries. Detail in ats_probe.log.")


if __name__ == "__main__":
    main()
