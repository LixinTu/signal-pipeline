"""Offline checks for find_contacts.py. Hunter here is SIMULATED in memory and every company,
person and email is SIMULATED; the script's session is replaced, so no real request is
possible. The project's data/ is never read or written.

Run: python tests/check_find_contacts.py
"""
import json
import logging
import os
import re
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
import find_contacts as fc  # noqa: E402
import pandas as pd  # noqa: E402
import requests  # noqa: E402

tmp = Path(tempfile.mkdtemp(prefix="sim_contacts_"))
fc.ACCOUNTS_CSV, fc.JOBS_CSV = tmp / "accounts.csv", tmp / "jobs.csv"
fc.CONTACTS_CSV, fc.RAW_DIR = tmp / "contacts" / "contacts.csv", tmp / "raw"
fc.OUTCOMES_CSV = tmp / "contacts" / "contact_outcomes.csv"
fc.load_dotenv = lambda *args, **kwargs: False      # never load a real .env
SLEEPS, results = [], []
fc.time.sleep = lambda seconds: SLEEPS.append(seconds)
IGNORED = {"value": True}
fc.git_ignored = lambda path: IGNORED["value"]
os.environ.pop(fc.KEY_ENV, None)
KEY = "SIMULATED-hunter-key-0123456789"


def check(name, cond):
    results.append(bool(cond))
    print("PASS" if cond else "FAIL", "-", name)


class Collect(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines = []

    def emit(self, record):
        self.lines.append(f"{record.levelname} {record.getMessage()}")


H = Collect()
fc.log.addHandler(H)
fc.log.setLevel(logging.INFO)


def logged(text):
    return any(text in line for line in H.lines)


class FakeResp:
    def __init__(self, status, body, headers=None):
        self.status_code, self._body, self.headers = status, body, headers or {}
        self.ok = 200 <= status < 300

    def json(self):
        return self._body


def person(first, last, position, email, confidence=90, status="valid", seniority="executive"):
    return {"value": email, "type": "personal", "confidence": confidence, "first_name": first, "last_name": last,
            "position": position, "seniority": seniority, "department": "executive",
            "linkedin": "https://linkedin.example/in/" + first.lower(), "twitter": "sim_" + first.lower(),
            "phone_number": "+1 000 000 0000", "verification": {"date": "2026-11-01", "status": status},
            "sources": [{"uri": "https://site.example/team"}, {"uri": "https://site.example/about"}]}


# SIMULATED answers by domain: what the targeted search (with a department filter) and the wide one return
TARGETED = {
    "tiny.example": [person("Simona", "Alpha", "Head of Sales", "simona@tiny.example", 99),
                     person("Simon", "Bravo", "Co-Founder & CEO", "simon@tiny.example", 80, "accept_all")],
    "big.example": [person("Simeon", "Delta", "Office Manager", "simeon@big.example", 70, "unknown", "senior")],
}
WIDE = {
    "mid.example": [person("Simba", "Echo", "Marketing Manager", "simba@mid.example", 95, seniority="senior"),
                    person("Simran", "Foxtrot", "Engineering Intern", "simran@mid.example", 99, seniority="junior"),
                    person("Simao", "Golf", "Engineering Manager", "simao@mid.example", 99, seniority="senior"),
                    person("Simone", "Charlie", "VP Engineering", "simone@mid.example", 70)],
}


class SimHunter:
    """A SIMULATED Hunter: one credit per search with results, as measured. Records every call."""
    calls, planned, sessions, used = [], [], 0, 4.0

    def __init__(self):
        SimHunter.sessions += 1
        self.headers = {}

    def get(self, url, params=None, timeout=None):
        SimHunter.calls.append({"url": url, "params": dict(params or {}), "headers": dict(self.headers)})
        if url == fc.SEARCH_URL and SimHunter.planned:
            answer = SimHunter.planned.pop(0)
            if isinstance(answer, Exception):
                raise answer
            if answer is not None:
                return answer
        if url == fc.ACCOUNT_URL:
            return FakeResp(200, {"data": {"plan_name": "Free", "requests": {"credits": {"used": SimHunter.used, "available": 50}}}})
        table = TARGETED if "department" in params else WIDE
        emails = table.get(params["domain"], [])[: params["limit"]]
        cost = 1 if emails else 0
        SimHunter.used += cost
        return FakeResp(200, {"data": {"domain": params["domain"], "emails": emails}, "meta": {"results": len(emails)}},
                        {"X-Credits-Used": str(cost), "X-Credits-Unit": "credit"})


fc.requests.Session = SimHunter


def fresh(planned=(), keep_cache=False):
    SimHunter.calls, SimHunter.planned, SimHunter.sessions, SimHunter.used = [], list(planned), 0, 4.0
    H.lines.clear()
    SLEEPS.clear()
    if not keep_cache:
        shutil.rmtree(fc.RAW_DIR, ignore_errors=True)
    shutil.rmtree(fc.CONTACTS_CSV.parent, ignore_errors=True)


def searches():
    return [(call["params"]["domain"], "targeted" if "department" in call["params"] else "wide")
            for call in SimHunter.calls if call["url"] == fc.SEARCH_URL]


def contacts():
    return pd.read_csv(fc.CONTACTS_CSV, dtype=str, keep_default_na=False).set_index("company_name")


# SIMULATED accounts and headcounts
pd.DataFrame([{"company_name": name, "domain": domain} for name, domain in (
    ("Tiny", "tiny.example"), ("Mid", "mid.example"), ("Big", "big.example"), ("Empty", "empty.example"),
    ("NoDomain", ""), ("Sixth", "sixth.example"))]).to_csv(fc.ACCOUNTS_CSV, index=False)
pd.DataFrame([{"company_name": name, "employee_count": count} for name, count in (
    ("Tiny", "12"), ("Mid", "120"), ("Big", "450"), ("Empty", ""), ("NoDomain", "30"), ("Sixth", "8"))]).to_csv(fc.JOBS_CSV, index=False)

# 1. who to look for, and with which searches
labels = [fc.target_for(n)["label"] for n in ("4", "49", "50", "199", "200", "489.0", "", None, "many")]
check("under 50 a founder, 50 to 199 the engineering lead, 200 or more the talent lead, unknown the engineering lead",
      labels == ["founder or CEO", "founder or CEO", "engineering lead", "engineering lead", "talent lead",
                 "talent lead", "engineering lead", "engineering lead", "engineering lead"])
plan = fc.search_plan("tiny.example", fc.target_for("12"))
check("two searches per company: the target's departments first, then the whole domain; 10 personal addresses each",
      plan == [("targeted", {"domain": "tiny.example", "type": "personal", "limit": 10, "department": "executive,management"}),
               ("wide", {"domain": "tiny.example", "type": "personal", "limit": 10})])
check("the talent search looks in HR first", fc.search_plan("big.example", fc.target_for("450"))[0][1]["department"] == "hr")

# 2. which contact is kept
founder, engineering = fc.target_for("12"), fc.target_for("120")
chosen, fit = fc.choose(fc.trim({"data": {"emails": TARGETED["tiny.example"]}})["emails"], founder)
check("a position that names the role beats a higher confidence", fit == "title match" and chosen["last_name"] == "Bravo")
chosen, fit = fc.choose(fc.trim({"data": {"emails": WIDE["mid.example"]}})["emails"], engineering)
check("among positions that name the role, the more senior person is kept, not the more confident",
      fit == "title match" and chosen["position"] == "VP Engineering")
talent = fc.target_for("450")
chosen, fit = fc.choose(fc.trim({"data": {"emails": TARGETED["big.example"]}})["emails"], talent)
check("no position names the role or its team: someone is kept, marked as another role", fit == "other role" and chosen["last_name"] == "Delta")
recruiter = person("Simcha", "Hotel", "Recruiting and Onboarding Specialist", "simcha@big.example", 99)
head = person("Simi", "India", "Head of Talent", "simi@big.example", 60, seniority="senior")
chosen, fit = fc.choose(fc.trim({"data": {"emails": TARGETED["big.example"] + [recruiter]}})["emails"], talent)
check("the right team without a lead's title is a function match, not a title match",
      fit == "function match" and chosen["last_name"] == "Hotel")
chosen, fit = fc.choose(fc.trim({"data": {"emails": [recruiter, head]}})["emails"], talent)
check("the team's lead beats a team member with a higher confidence", fit == "title match" and chosen["last_name"] == "India")
check("a team word alone is not the lead: an intern and an individual engineer are function matches",
      [fc.choose(fc.trim({"data": {"emails": [person("S", "X", title, "s@mid.example")]}})["emails"], engineering)[1]
       for title in ("Engineering Intern", "Solutions Engineer", "Engineering Manager", "CTO", "Marketing Manager")]
      == ["function match", "function match", "title match", "title match", "other role"])
check("no email at all: none", fc.choose([], founder) == (None, "none"))
kept = fc.trim({"data": {"emails": TARGETED["tiny.example"]}, "meta": {"results": 2}})
check("phone numbers, social profiles and source pages are dropped as soon as an answer arrives",
      not re.search(r"linkedin|twitter|phone|site\.example|\+1 000", json.dumps(kept))
      and kept["emails"][0]["sources"] == 2 and kept["emails"][1]["verification_status"] == "accept_all" and kept["held"] == 2)

# 3. the command
fresh()
check("--dry-run exits 0, opens no session and needs no key",
      fc.main(["--dry-run"]) == 0 and SimHunter.sessions == 0 and logged("Dry run: nothing sent. 5 companies would cost about 5 credit(s)"))
fresh()
check("without the key nothing is sent", fc.main([]) == 1 and SimHunter.calls == [] and logged("is not set in .env"))
os.environ[fc.KEY_ENV] = KEY
IGNORED["value"] = False
fresh()
check("if git does not ignore the output, nothing is sent",
      fc.main([]) == 1 and SimHunter.calls == [] and logged("git does not ignore"))
IGNORED["value"] = True
fresh()
code = fc.main([])
table = contacts()
check("the default run takes the first 5 accounts and exits 0", code == 0 and list(table.index) == ["Tiny", "Mid", "Big", "Empty", "NoDomain"])
check("the wide search runs only when the targeted one finds nobody",
      searches() == [("tiny.example", "targeted"), ("mid.example", "targeted"), ("mid.example", "wide"),
                     ("big.example", "targeted"), ("empty.example", "targeted"), ("empty.example", "wide")]
      and SimHunter.calls[0]["url"] == fc.ACCOUNT_URL and SimHunter.calls[-1]["url"] == fc.ACCOUNT_URL)
check("the key travels in the X-API-KEY header, never in the query",
      all(call["headers"].get("X-API-KEY") == KEY for call in SimHunter.calls)
      and not any(KEY in json.dumps(call["params"]) for call in SimHunter.calls))
check("contacts.csv has the planned columns", list(pd.read_csv(fc.CONTACTS_CSV).columns) == fc.COLUMNS)
check("results per company: target role, target role, another role, none, skipped",
      list(table["result"]) == ["title match", "title match", "other role", "none", "skipped: no domain"]
      and list(table["query"]) == ["targeted", "wide", "targeted", "wide", ""])
check("the chosen contact's name, position, email, confidence and status are written",
      tuple(table.loc["Tiny", ["first_name", "last_name", "position", "email", "confidence", "verification_status", "sources"]])
      == ("Simon", "Bravo", "Co-Founder & CEO", "simon@tiny.example", "80", "accept_all", "2")
      and table.loc["Mid", "position"] == "VP Engineering" and table.loc["Mid", "contacts_returned"] == "4")
check("credits per company come from Hunter's header; an empty search costs nothing",
      [float(value or 0) for value in table["credits_used"]] == [1, 1, 1, 0, 0])
check("the log counts and never names anyone",
      logged("4 of 5 companies searched (0 from the cache). A contact in the target role: 2. In the right team but not its lead: 0. In another role: 1. No contact: 1. Skipped: 1.")
      and logged("Found by the targeted search: 2. Found only by the wide search: 1.") and logged("accept_all 1, unknown 1, valid 1")
      and logged("Hunter credits used by this run: 3 (from Hunter's headers). This month: 7 used of 50.")
      and not any(re.search(r"Simon|Bravo|Charlie|Delta|@|example", line) for line in H.lines))
saved_text = "".join(path.read_text() for path in fc.RAW_DIR.glob("hunter_domain_search_*.json")) + fc.CONTACTS_CSV.read_text()
check("one person per company is saved; the people who were not picked are written nowhere",
      len(list(fc.RAW_DIR.glob("hunter_domain_search_*.json"))) == 6
      and not re.search(r"Simona|Alpha|Simba|Echo|Simran|Foxtrot|Simao|Golf", saved_text) and "Bravo" in saved_text and "Charlie" in saved_text)
check("saved answers hold neither the key nor social profiles", KEY not in saved_text and "linkedin" not in saved_text)
check("a wait follows every search", SLEEPS == [1.0] * 6)

# 4. a second run reads the saved answers and spends nothing
fresh(keep_cache=True)
code = fc.main([])
check("a second run sends no search", code == 0 and searches() == [] and logged("(4 from the cache)") and logged("credits used by this run: 0"))
check("and writes the same contacts", list(contacts()["result"]) == ["title match", "title match", "other role", "none", "skipped: no domain"]
      and set(contacts()["source"]) == {"cache", ""} and contacts().loc["Mid", "position"] == "VP Engineering")
old = sorted(fc.RAW_DIR.glob("hunter_domain_search_tiny.example_*.json"))[0]
stale = json.loads(old.read_text())
stale["searched_at"] = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat()
old.write_text(json.dumps(stale))
fresh(keep_cache=True)
fc.main([])
check("an answer older than 30 days is asked again", searches() == [("tiny.example", "targeted")])
jobs_before = fc.JOBS_CSV.read_text()
fc.JOBS_CSV.write_text(jobs_before.replace("Tiny,12", "Tiny,450"))      # the company grew: another role is the target
fresh(keep_cache=True)
fc.main(["--limit", "1"])
check("a saved answer for other departments is not reused",
      [call["params"]["department"] for call in SimHunter.calls if call["url"] == fc.SEARCH_URL] == ["hr"])
fc.JOBS_CSV.write_text(jobs_before)

# 5. limits and errors
fresh()
code = fc.main(["--max-credits", "1"])
check("the credit cap stops new searches before they could pass it, and the run still exits 0",
      code == 0 and searches() == [("tiny.example", "targeted")]
      and list(contacts()["result"])[:3] == ["title match", "skipped: credit cap", "skipped: credit cap"]
      and logged("the credit cap was reached"))
costly = FakeResp(200, {"data": {"emails": TARGETED["tiny.example"]}, "meta": {"results": 2}}, {"X-Credits-Used": "4"})
fresh([costly])
code = fc.main([])
check("a search that costs more than expected stops the run at once, keeps what it found, and exits 1",
      code == 1 and searches() == [("tiny.example", "targeted")] and contacts().loc["Tiny", "result"] == "title match"
      and logged("one search cost 4 credits, more than the 1 expected") and list(contacts().index) == ["Tiny"])
fresh()
code = fc.main(["--companies", "Sixth,Tiny"])
check("--companies takes the named accounts in the given order", code == 0 and list(contacts().index) == ["Sixth", "Tiny"])
fresh()
check("an unknown company name stops the run before any request", fc.main(["--companies", "Nobody"]) == 1 and SimHunter.calls == [])
fresh([FakeResp(429, {"errors": [{"id": "too_many_requests", "code": 429}]})])
code = fc.main([])
check("HTTP 429 means no credits left: the run stops and exits 1",
      code == 1 and len(searches()) == 1 and logged("this month's credits are used up") and contacts().loc["Tiny", "result"] == "skipped: run stopped")
fresh([FakeResp(403, {"errors": [{"id": "rate_limit", "code": 403}]})])
code = fc.main(["--limit", "1"])
check("HTTP 403 means wait: the search is retried and succeeds", code == 0 and len(searches()) == 2 and contacts().loc["Tiny", "result"] == "title match")
fresh([FakeResp(451, {"errors": [{"id": "claimed_email", "code": 451}]})])
code = fc.main(["--limit", "2"])
check("HTTP 451: that company is skipped without a second search, and the run goes on",
      code == 0 and contacts().loc["Tiny", "result"].startswith("skipped: Hunter was asked not to process")
      and searches() == [("tiny.example", "targeted"), ("mid.example", "targeted"), ("mid.example", "wide")]
      and contacts().loc["Mid", "result"] == "title match")
fresh([FakeResp(401, {"errors": [{"id": "authentication_failed", "code": 401}]})])
check("HTTP 401: the key is named as the problem", fc.main([]) == 1 and logged("rejected the API key"))
fresh([requests.ConnectionError("SIMULATED drop")])
code = fc.main(["--limit", "2"])
check("a network error is not repeated; the company is skipped", code == 0
      and searches() == [("tiny.example", "targeted"), ("mid.example", "targeted"), ("mid.example", "wide")]
      and contacts().loc["Tiny", "result"].startswith("skipped: no answer from Hunter"))
fresh([FakeResp(200, {"data": {"emails": TARGETED["tiny.example"]}, "meta": {"results": 2}})])
fc.main(["--limit", "1"])
check("without the cost header the usual cost is assumed, and the log says so",
      logged("credits used by this run: 1 (estimated, no cost header)"))

# 6. --outcomes-only: coverage is measured and no personal data is written anywhere
# every SIMULATED first name, last name, email and position a run could save; "founder or CEO" is the
# target's label, not a person's position, so the position is matched by "Co-Founder"
PERSONAL = r"Simon|Bravo|Simone|Charlie|Simeon|Delta|@|Co-Founder|VP Engineering|Office Manager"


def outcomes():
    return pd.read_csv(fc.OUTCOMES_CSV, dtype=str, keep_default_na=False).set_index("company_name")


def written():
    return "".join(file.read_text() for file in list(fc.RAW_DIR.glob("*.json")) + list(fc.CONTACTS_CSV.parent.glob("*.csv")))


def again():
    """Forget the calls and the log, keep every file."""
    SimHunter.calls, SimHunter.planned, SimHunter.used = [], [], 4.0
    H.lines.clear()


fresh()
code = fc.main(["--outcomes-only"])
table = outcomes()
check("--outcomes-only exits 0 and writes its own file with no personal columns; contacts.csv is not written",
      code == 0 and list(pd.read_csv(fc.OUTCOMES_CSV).columns) == fc.OUTCOME_COLUMNS and not fc.CONTACTS_CSV.exists()
      and not set(fc.PERSONAL_COLUMNS) & set(fc.OUTCOME_COLUMNS))
check("the outcome per company is the same as in a normal run",
      list(table["result"]) == ["title match", "title match", "other role", "none", "skipped: no domain"]
      and list(table["verification_status"]) == ["accept_all", "valid", "unknown", "", ""]
      and list(table["contacts_returned"]) == ["2", "4", "1", "0", ""])
check("no name, email or position is in any saved file, the saved answers included",
      len(list(fc.RAW_DIR.glob("hunter_domain_search_*.json"))) == 6 and not re.search(PERSONAL, written()))
check("the log says so and names nobody",
      logged("Outcomes only: no name, email or position was written.") and logged("5 companies. A contact in the target "
             "role: 2. In the right team but not its lead: 0. In another role: 1. No contact: 1. Skipped: 1.")
      and not any(re.search(PERSONAL.replace("|@", ""), line) for line in H.lines))
again()
code = fc.main(["--outcomes-only"])
check("a second --outcomes-only run reads its own saved answers and spends nothing",
      code == 0 and searches() == [] and logged("credits used by this run: 0")
      and list(outcomes()["result"]) == ["title match", "title match", "other role", "none", "skipped: no domain"])
again()
fc.main(["--outcomes-only", "--companies", "Sixth"])
check("outcomes add up across runs", list(outcomes().index) == ["Tiny", "Mid", "Big", "Empty", "NoDomain", "Sixth"])
shutil.rmtree(fc.RAW_DIR)
again()
fc.main(["--outcomes-only", "--max-credits", "1"])
check("a company skipped in a later run keeps the result it already has",
      searches() == [("tiny.example", "targeted")] and list(outcomes()["result"])[:3] == ["title match", "title match", "other role"])
fresh()
fc.main(["--outcomes-only"])
again()
shutil.rmtree(fc.CONTACTS_CSV.parent)
code = fc.main([])
check("a normal run cannot use a saved answer that holds no person: it asks again, except where nobody was found",
      code == 0 and searches() == [("tiny.example", "targeted"), ("mid.example", "wide"), ("big.example", "targeted")]
      and contacts().loc["Tiny", "last_name"] == "Bravo" and contacts().loc["Empty", "result"] == "none")
again()
shutil.rmtree(fc.CONTACTS_CSV.parent)
code = fc.main(["--outcomes-only"])
check("--outcomes-only can use an answer saved by a normal run, and still writes no personal data of its own",
      code == 0 and searches() == [] and not re.search(PERSONAL, fc.OUTCOMES_CSV.read_text())
      and list(outcomes()["result"]) == ["title match", "title match", "other role", "none", "skipped: no domain"])
IGNORED["value"] = False
fresh()
check("--outcomes-only also refuses to run when git does not ignore its output",
      fc.main(["--outcomes-only"]) == 1 and SimHunter.calls == [] and logged("git does not ignore"))
IGNORED["value"] = True

# 7. what the script is able to do at all
source = (PROJECT / "find_contacts.py").read_text(encoding="utf-8")
check("the script knows two Hunter endpoints only and never posts",
      sorted(set(re.findall(r"https://api\.hunter\.io[^\"'\s)]*", source)))
      == ["https://api.hunter.io/v2/account", "https://api.hunter.io/v2/domain-search"]
      and not re.search(r"\.post\(|\.put\(|\.patch\(|\.delete\(", source))

print(f"\n{sum(results)}/{len(results)} checks passed")
if all(results):
    shutil.rmtree(tmp, ignore_errors=True)
sys.exit(0 if all(results) else 1)
