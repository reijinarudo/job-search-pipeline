"""
screening_example.py

Example screening profile for job_monitor.py. Every value here describes one
person's search: the roles they want, the ones they cannot get, where they
live, and the salary floor. The values below are placeholders for a fictional
learning-and-development professional living near Denver, Colorado.

To use your own profile:

    1. Copy this file to screening_local.py (it is listed in .gitignore).
    2. Edit the values in screening_local.py.

job_monitor.py loads screening_local.py when it exists and falls back to this
file otherwise. Set JOB_MONITOR_PROFILE=<module name> to load a different
module, which is how test_screen.py pins this example.

All text matching is done on lowercased text, so write list entries in lower
case. Regular expressions are matched case-insensitively.
"""

# ---------------------------------------------------------------------------
# Corporate lane: the main search
# ---------------------------------------------------------------------------

# Postings whose stated band tops out below this are dropped. Postings with no
# band are kept, because most employers publish none.
MIN_SALARY = 90_000

# A posting must contain at least one of these in its title to be considered.
TITLE_SIGNALS = [
    "learning", "training", "instructional", "curriculum", "enablement",
    "l&d", "talent development", "professional development",
    "workforce development", "education",
]

# Any of these in the title rejects the posting outright.
TITLE_KILL = [
    "sales", "account executive", "account manager", "business development",
    "software engineer", "data scientist", "recruiter", "intern",
    "nurse", "clinical", "technician",
]

# Fields you have never worked in. A long years-of-experience requirement in
# one of these is treated as a hard gate. Keep this list explicit: a wrong
# entry here hides real roles without telling you.
NEVER_HELD_FIELDS = [
    "product management", "software engineering", "data engineering",
    "supply chain", "logistics", "accounting", "actuarial", "underwriting",
    "sales", "account management", "marketing",
    "management consulting", "nursing", "patient care",
]

# Fields you have worked in. A years requirement that names one of these is
# fair game, and a requirement that names both a held and a never-held field
# reads as an either/or, so the posting survives.
HELD_FIELDS = [
    "learning", "training", "instructional design", "curriculum",
    "education", "teaching", "adult learning", "enablement",
    "talent development", "professional development", "l&d",
]

# Functions you are aiming for but have not held as a job title. A posting
# that pairs a long tenure requirement with one of these is shown in a
# "stretch" section of the digest, so you make the call.
TARGET_FIELDS = [
    "learning strategy", "change management", "ai literacy",
]

# Years thresholds below this number are never treated as a hard gate.
YEARS_GATE = 8

# Structural blockers in the posting body: (regex, plain-English label).
# The label is what the digest prints when a posting is withheld, so you can
# audit the filter and overrule it.
BODY_KILL = [
    (r"\b\d+\+?\s*years?[^.]{0,60}\bproduct manage",
     "years of product management required"),
    (r"\b\d+\+?\s*years?[^.]{0,60}\bsoftware engineer",
     "years of software engineering required"),
    (r"active security clearance", "active security clearance required"),
    (r"registered nurse[^.]{0,40}(required|licens)",
     "registered nurse licensure required"),
]

# ---------------------------------------------------------------------------
# Geography
# ---------------------------------------------------------------------------

# Regex that identifies your home state in a location string or a residency
# clause. Used to decide whether a "remote, but must live in X" posting is
# open to you, and whether an on-site role is near you.
HOME_STATE_PATTERN = r"\bcolorado\b|\bco\b|denver"

# Places within your commuting range. A role here that is on-site or hybrid is
# listed in an FYI section of the digest.
LOCAL_PLACES = [
    "denver", "aurora", "lakewood", "englewood", "littleton", "golden",
    "arvada", "westminster", "centennial",
]

# Places in your state that are too far to commute. On-site roles here are
# rejected even when the posting also mentions remote work.
LOCATION_KILL = [
    "boulder", "fort collins", "longmont", "colorado springs", "greeley",
]

# ---------------------------------------------------------------------------
# Faculty lane (optional)
#
# Applies only to boards tagged "lane": "faculty" in boards.json. Meant for a
# full-time online teaching role held alongside another job, so it checks for
# remote work, full-time status, a credential fit, and schedule language that
# could clash with your other job. Leave the faculty boards out of
# boards.json if you do not want this lane.
# ---------------------------------------------------------------------------

FACULTY_MIN_SALARY = 40_000

# Name of your other job, used in the digest's schedule-conflict notes.
PRIMARY_JOB_LABEL = "your current job"

# One-paragraph summary printed above the faculty section of the digest.
FACULTY_LANE_SUMMARY = (
    "Full-time online teaching held alongside your current job: online only, "
    "full-time only, in disciplines your degrees support. Each posting shows "
    "any fixed-hours or live-session language that could clash with your "
    "other work."
)

# Any of these in a faculty title rejects the posting.
FACULTY_TITLE_KILL = [
    "adjunct", "part-time", "part time", "per course", "visiting", "temp",
    "pool", "substitute", "onsite", "on-site", "nursing", "clinical", "dean",
]

# Disciplines your credentials support: (regex, label, note). A non-empty
# note is printed in the digest as a reminder to check something.
FACULTY_DISCIPLINES = [
    (r"\beducation\b|curriculum|instructional (design|tech)|"
     r"adult (learning|education)|teacher",
     "education (example: M.Ed.)", ""),
    (r"psycholog", "psychology (example: M.A. Psychology)",
     "confirm the graduate-hours requirement"),
]

# Credential requirements you do not meet: (regex, label).
FACULTY_CRED_KILL = [
    (r"(ph\.?d\.?|doctorate)[^.]{0,30}\bin (statistics|mathematics)",
     "doctorate in statistics or math required"),
]

# Schedule language reported on every faculty posting. Neither list rejects
# anything; they are printed so you can judge the fit.
SCHEDULE_CONFLICTS = [
    (r"monday\s*(through|thru|-|to)\s*friday", "Monday-Friday availability"),
    (r"business hours|core hours|set schedule|regular office hours",
     "fixed working hours stated"),
    (r"\bsynchronous\b(?![^.]{0,20}asynchronous)",
     "synchronous (live) sessions"),
    (r"live (class|session|lecture)", "live class sessions"),
    (r"on[- ]campus|in[- ]person|face[- ]to[- ]face",
     "on-campus or face-to-face component"),
    (r"outside employment|conflict of commitment|exclusive employ",
     "outside-employment policy"),
]
SCHEDULE_FLEX = [
    (r"\basynchronous", "asynchronous courses"),
    (r"flexible (schedule|hours|work)|set your own (schedule|hours)",
     "flexible schedule stated"),
    (r"work from anywhere|fully remote|100% remote", "fully remote"),
]
