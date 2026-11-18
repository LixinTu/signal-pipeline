"""Offline checks for verify_postings.py. The job boards here are SIMULATED in memory and
every company, board and posting is SIMULATED; the script's session is replaced, so no real
request is possible. The project's data/ is never read or written.

Run: python tests/check_verify_postings.py
"""
import json
import logging
import re
import shutil
import sys
import tempfile
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
import verify_postings as vp  # noqa: E402
import pandas as pd  # noqa: E402
import requests  # noqa: E402

tmp = Path(tempfile.mkdtemp(prefix="sim_postings_"))
vp.ACCOUNTS_CSV, vp.CHECK_CSV, vp.RAW_DIR = tmp / "accounts.csv", tmp / "postings_check.csv", tmp / "raw"
vp.JOBS_CSV = tmp / "jobs.csv"
SLEEPS, results = [], []
vp.time.sleep = lambda seconds: SLEEPS.append(seconds)


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
vp.log.addHandler(H)
vp.log.setLevel(logging.INFO)


def logged(text):
    return any(text in line for line in H.lines)


class FakeResp:
    def __init__(self, status, body, headers=None):
        self.status_code, self._body, self.headers = status, body, headers or {}

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


LONG = "SIMULATED long description that must not be saved"
ASHBY, GREENHOUSE, LEVER = "https://api.ashbyhq.com/posting-api/job-board/", "https://boards-api.greenhouse.io/v1/boards/", "https://api.lever.co/v0/postings/"
# SIMULATED boards: what each one's list endpoint answers
ANSWERS = {
    ASHBY + "alpha": FakeResp(200, {"apiVersion": "1", "jobs": [
        {"title": "Forward Deployed Engineer", "jobUrl": "https://jobs.ashbyhq.com/alpha/11111111-AAAA",
         "applyUrl": "https://jobs.ashbyhq.com/alpha/11111111-AAAA/application", "isListed": True,
         "descriptionHtml": LONG, "descriptionPlain": LONG},
        {"id": "99999999-zzzz", "title": "Recruiter", "isListed": True}]}),
    GREENHOUSE + "bravo/jobs": FakeResp(200, {"jobs": [{"id": 501, "title": "AI Engineer", "content": LONG}], "meta": {"total": 1}}),
    GREENHOUSE + "charlie/jobs": FakeResp(200, {"jobs": [{"id": 778, "title": "AI Engineer"}], "meta": {"total": 1}}),
    LEVER + "delta?mode=json": FakeResp(200, [{"id": "abc-123", "text": "Applied AI Engineer", "descriptionPlain": LONG,
                                              "lists": [{"text": LONG}]}]),
    ASHBY + "Foxtrot%20Labs": FakeResp(404, {"error": "SIMULATED not found"}),
    LEVER + "golf?mode=json": FakeResp(200, {"ok": False, "error": "SIMULATED: not a list"}),
    ASHBY + "juliet": FakeResp(200, ValueError("SIMULATED: not JSON")),
}


class SimBoards:
    """SIMULATED job boards. Records every call."""
    calls, planned, sessions = [], {}, 0

    def __init__(self):
        SimBoards.sessions += 1
        self.headers = {}

    def get(self, url, timeout=None):
        SimBoards.calls.append({"url": url, "headers": dict(self.headers)})
        queued = SimBoards.planned.get(url) or []
        if queued:
            answer = queued.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return answer
        return ANSWERS.get(url) or FakeResp(404, {"error": "SIMULATED: no such board"})


vp.requests.Session = SimBoards


def fresh(planned=None, keep_cache=False):
    SimBoards.calls, SimBoards.planned, SimBoards.sessions = [], dict(planned or {}), 0
    H.lines.clear()
    SLEEPS.clear()
    if not keep_cache:
        shutil.rmtree(vp.RAW_DIR, ignore_errors=True)
    vp.CHECK_CSV.unlink(missing_ok=True)


def statuses():
    table = pd.read_csv(vp.CHECK_CSV, dtype=str, keep_default_na=False)
    return dict(zip(table["posting_url"].str.replace(r"^https://(www\.)?", "", regex=True), table["status"]))


# SIMULATED accounts: the postings sit in source_urls, next to Form D links
ACCOUNTS = [
    ("Alpha", "https://www.sec.gov/Archives/edgar/data/1/x.xml https://jobs.ashbyhq.com/alpha/11111111-aaaa "
              "https://jobs.ashbyhq.com/alpha/22222222-bbbb/application"),
    ("Bravo", "https://job-boards.greenhouse.io/bravo/jobs/501"),
    ("Charlie", "https://boards.greenhouse.io/charlie/jobs/777"),
    ("Delta", "https://jobs.lever.co/delta/abc-123/apply"),
    ("Echo", "https://www.linkedin.com/jobs/view/123 https://www.workatastartup.com/jobs/9"),
    ("Foxtrot", "https://jobs.ashbyhq.com/Foxtrot%20Labs/33333333-cccc"),
    ("Golf", "https://jobs.lever.co/golf/zzz"),
    ("Hotel", "https://jobs.ashbyhq.com/hotel/44444444-dddd"),
    ("India", "https://boards.greenhouse.io/embed/job_app?for=india&token=5"),
    ("Juliet", "https://jobs.ashbyhq.com/juliet/55555555-eeee"),
    ("Kilo", ""),
]


def write_accounts(rows=ACCOUNTS):
    pd.DataFrame(rows, columns=["company_name", "source_urls"]).to_csv(vp.ACCOUNTS_CSV, index=False)


# 1. which links can be checked, and how a posting is found in them
check("an Ashby link gives the board and the posting id, with or without /application",
      vp.locate("https://jobs.ashbyhq.com/alpha/11111111-aaaa") == ("ashby", "alpha", "11111111-aaaa")
      and vp.locate("https://jobs.ashbyhq.com/alpha/22222222-bbbb/application")[2] == "22222222-bbbb")
check("a board name written with %20 is decoded", vp.locate(ACCOUNTS[5][1]) == ("ashby", "Foxtrot Labs", "33333333-cccc"))
check("both Greenhouse hosts are read the same way",
      vp.locate("https://job-boards.greenhouse.io/bravo/jobs/501") == ("greenhouse", "bravo", "501")
      and vp.locate("https://boards.greenhouse.io/charlie/jobs/777?gh_src=x") == ("greenhouse", "charlie", "777"))
check("a Lever link gives the site and the posting id", vp.locate("https://jobs.lever.co/delta/abc-123/apply") == ("lever", "delta", "abc-123"))
check("LinkedIn, other sites, an embedded form and a board's front page cannot be checked",
      [vp.locate(url) for url in ("https://www.linkedin.com/jobs/view/123", "https://www.workatastartup.com/jobs/9",
                                  ACCOUNTS[8][1], "https://jobs.ashbyhq.com/alpha", "https://boards.greenhouse.io/charlie",
                                  "not a link", "")] == [None] * 7)
check("Form D links are not postings, and an account without links gives no row",
      [row["posting_url"] for row in vp.posting_links(pd.DataFrame(ACCOUNTS[:1] + ACCOUNTS[-1:], columns=["company_name", "source_urls"]))]
      == ["https://jobs.ashbyhq.com/alpha/11111111-aaaa", "https://jobs.ashbyhq.com/alpha/22222222-bbbb/application"])
check("open ids: Ashby from the posting's link or id, Greenhouse and Lever from the id, all lower-cased",
      {"11111111-aaaa", "99999999-zzzz"} <= vp.open_ids("ashby", ANSWERS[ASHBY + "alpha"].json())
      and vp.open_ids("greenhouse", ANSWERS[GREENHOUSE + "bravo/jobs"].json()) == {"501"}
      and vp.open_ids("lever", ANSWERS[LEVER + "delta?mode=json"].json()) == {"abc-123"})
refused = 0
for kind, body in (("lever", {"error": "x"}), ("greenhouse", {"meta": {}}), ("ashby", {"jobs": None}),
                   ("ashby", {"jobs": [{"title": "no id and no link"}]}), ("greenhouse", [1, 2]), ("lever", ["x"])):
    try:
        vp.open_ids(kind, body)
    except ValueError:
        refused += 1
check("an answer without the documented shape is refused, not read as an empty board", refused == 6)
check("an empty board is a valid answer: every posting on it is closed",
      vp.open_ids("greenhouse", {"jobs": []}) == set() and vp.open_ids("lever", []) == set() and vp.open_ids("ashby", {"jobs": []}) == set())

# 2. the command
write_accounts()
fresh()
check("--dry-run exits 0, opens no session and counts what it would ask",
      vp.main(["--dry-run"]) == 0 and SimBoards.sessions == 0 and not vp.CHECK_CSV.exists()
      and logged("Dry run: nothing sent. 12 posting link(s) at 11 listed account(s); 9 are on a job board with a public API "
                 "and would take 8 request(s), one per board. 3 cannot be checked."))
fresh({ASHBY + "hotel": [requests.ConnectionError("SIMULATED drop")]})
code = vp.main([])
found = statuses()
check("the run exits 0 and writes the planned columns", code == 0 and list(pd.read_csv(vp.CHECK_CSV).columns) == vp.COLUMNS)
check("a posting on its board's list is open, whatever the letter case; one that is missing is closed",
      found["jobs.ashbyhq.com/alpha/11111111-aaaa"] == "open" and found["jobs.ashbyhq.com/alpha/22222222-bbbb/application"] == "closed"
      and found["job-boards.greenhouse.io/bravo/jobs/501"] == "open" and found["boards.greenhouse.io/charlie/jobs/777"] == "closed"
      and found["jobs.lever.co/delta/abc-123/apply"] == "open")
check("a board that is gone, answers in another shape, sends no JSON or does not answer: unknown, and the run goes on",
      [found[key] for key in ("jobs.ashbyhq.com/Foxtrot%20Labs/33333333-cccc", "jobs.lever.co/golf/zzz",
                              "jobs.ashbyhq.com/juliet/55555555-eeee", "jobs.ashbyhq.com/hotel/44444444-dddd")] == ["unknown"] * 4
      and sum("gave no usable answer" in line for line in H.lines) == 4)
check("LinkedIn and other links are marked not checkable, and no request goes to them",
      found["linkedin.com/jobs/view/123"] == found["workatastartup.com/jobs/9"] == "not checkable"
      and found["boards.greenhouse.io/embed/job_app?for=india&token=5"] == "not checkable"
      and all(re.match(r"https://(api\.ashbyhq\.com|boards-api\.greenhouse\.io|api\.lever\.co)/", call["url"]) for call in SimBoards.calls))
check("one request per board, even with two postings on it, and a wait after each",
      len(SimBoards.calls) == 8 and sum(call["url"] == ASHBY + "alpha" for call in SimBoards.calls) == 1 and SLEEPS == [1.0] * 8)
check("the client name is generic and no key is sent",
      all(call["headers"] == {"User-Agent": vp.CONFIG["user_agent"]} for call in SimBoards.calls)
      and not re.search(r"@|\.edu|key", vp.CONFIG["user_agent"], re.I))
check("the log counts and names no company, board or link",
      logged("Posting check: 12 posting link(s) at 11 listed account(s). On a job board with a public API: 9, at 8 account(s): "
             "3 open, 0 reposted under a new link, 2 closed, 4 unknown. Not checkable (LinkedIn or another site): 3.")
      and logged("Accounts whose every checked posting is closed: 1. Boards asked: 8, read from saved answers: 0.")
      and not any(re.search(r"alpha|bravo|charlie|delta|foxtrot|golf|hotel|juliet|https?://", line, re.I) for line in H.lines))
saved = sorted(vp.RAW_DIR.glob("job_board_*.json"))
saved_text = "".join(path.read_text() for path in saved)
check("each usable answer is saved with its time and open ids, without the long description text",
      len(saved) == 4 and LONG not in saved_text and "SIMULATED" not in saved_text.replace("SIMULATED not found", "")
      and json.loads(saved[0].read_text())["open_ids"] and json.loads(saved[0].read_text())["open_titles"]
      and "saved_at" in json.loads(saved[0].read_text())
      and "Forward Deployed Engineer" in saved_text)

# 3. saved answers are reused, then asked again when they are too old
fresh(keep_cache=True)
code = vp.main([])
check("a second run reads the saved answers and asks only the boards that gave none",
      code == 0 and len(SimBoards.calls) == 4 and logged("Boards asked: 4, read from saved answers: 4.")
      and statuses()["jobs.ashbyhq.com/alpha/11111111-aaaa"] == "open" and statuses()["boards.greenhouse.io/charlie/jobs/777"] == "closed")
fresh(keep_cache=True)
vp.main(["--max-age-hours", "0"])
check("--max-age-hours 0 asks every board again", len(SimBoards.calls) == 8)

# 4. a role posted again under a new link is not a closed role
pd.DataFrame([
    {"job_title": "AI  engineer", "job_url": "https://boards.greenhouse.io/charlie/jobs/777?gh_src=sim", "posting_url": ""},
    {"job_title": "ML Engineer", "job_url": "", "posting_url": "https://jobs.ashbyhq.com/alpha/22222222-bbbb/application"},
    {"job_title": "Applied AI Engineer", "job_url": "https://jobs.lever.co/delta/abc-123/apply", "posting_url": ""},
]).to_csv(vp.JOBS_CSV, index=False)
check("titles are read from jobs.csv by link without its query string, tidied",
      vp.posting_titles()["https://boards.greenhouse.io/charlie/jobs/777"] == "ai engineer" and len(vp.posting_titles()) == 3)
check("titles on a board are tidied the same way", vp.open_titles("greenhouse", ANSWERS[GREENHOUSE + "charlie/jobs"].json()) == {"ai engineer"}
      and vp.open_titles("lever", ANSWERS[LEVER + "delta?mode=json"].json()) == {"applied ai engineer"}
      and vp.open_titles("ashby", {"jobs": [{"id": "x"}]}) == set())
fresh()
vp.main([])
found = statuses()
check("a posting whose link is gone but whose title is still listed is reposted; without that title it is closed",
      found["boards.greenhouse.io/charlie/jobs/777"] == "reposted" and found["jobs.ashbyhq.com/alpha/22222222-bbbb/application"] == "closed"
      and found["jobs.lever.co/delta/abc-123/apply"] == "open")
check("a reposted role does not count as a closed account",
      logged("3 open, 1 reposted under a new link, 1 closed, 4 unknown.") and logged("Accounts whose every checked posting is closed: 0."))
fresh(keep_cache=True)
vp.main([])
check("the titles are kept in the saved answers too", statuses()["boards.greenhouse.io/charlie/jobs/777"] == "reposted" and len(SimBoards.calls) == 4)
vp.JOBS_CSV.unlink()

# 5. limits and errors
write_accounts(ACCOUNTS[1:2])
fresh({GREENHOUSE + "bravo/jobs": [FakeResp(429, {}, {"Retry-After": "7"}), FakeResp(429, {})]})
code = vp.main([])
check("HTTP 429 is retried after the wait the board asks for, then with a growing wait",
      code == 0 and len(SimBoards.calls) == 3 and SLEEPS[:2] == [7.0, 4.0] and statuses() == {"job-boards.greenhouse.io/bravo/jobs/501": "open"})
fresh({GREENHOUSE + "bravo/jobs": [FakeResp(429, {})] * 9})
code = vp.main([])
check("a board that keeps answering 429 is given up after the retries: unknown, exit 0",
      code == 0 and len(SimBoards.calls) == 4 and statuses() == {"job-boards.greenhouse.io/bravo/jobs/501": "unknown"})
fresh({GREENHOUSE + "bravo/jobs": [FakeResp(500, {})]})
vp.main([])
check("a server error is not repeated", len(SimBoards.calls) == 1 and statuses() == {"job-boards.greenhouse.io/bravo/jobs/501": "unknown"})
fresh({GREENHOUSE + "bravo/jobs": [FakeResp(503, {"jobs": [], "meta": {"total": 0}})]})
vp.main([])
check("an error answer is never read as a board's list, even when its body looks like an empty one",
      statuses() == {"job-boards.greenhouse.io/bravo/jobs/501": "unknown"} and not list(vp.RAW_DIR.glob("job_board_*.json")))
write_accounts(ACCOUNTS[4:5])
fresh()
code = vp.main([])
check("with nothing to check no session call is made, and the counts say so",
      code == 0 and SimBoards.calls == [] and logged("On a job board with a public API: 0, at 0 account(s): 0 open, 0 reposted under "
                                                     "a new link, 0 closed, 0 unknown. Not checkable (LinkedIn or another site): 2."))
vp.ACCOUNTS_CSV.unlink()
fresh()
check("without accounts.csv the command exits 1 and sends nothing", vp.main([]) == 1 and SimBoards.calls == [] and logged("is missing"))
parser_refused = False
try:
    vp.main(["--max-age-hours", "-1"])
except SystemExit as stop:
    parser_refused = stop.code == 2
check("a negative --max-age-hours is refused", parser_refused)

# 6. what the script is able to do at all
source = (PROJECT / "verify_postings.py").read_text(encoding="utf-8")
code_only = source.split('"""', 2)[2]
check("the script only reads: three documented list endpoints, GET only, no key or secret",
      sorted(vp.CONFIG["list_urls"]) == ["ashby", "greenhouse", "lever"]
      and sorted(set(re.findall(r"https://[a-z.-]+", code_only))) == ["https://api.ashbyhq.com", "https://api.lever.co",
                                                                     "https://boards-api.greenhouse.io"]
      and not re.search(r"\.post\(|\.put\(|\.patch\(|\.delete\(|load_dotenv|getenv|API_KEY", code_only))
check("no LinkedIn host is among the boards it can ask", not any("linkedin" in host for host in vp.CONFIG["boards"]))

print(f"\n{sum(results)}/{len(results)} checks passed")
if all(results):
    shutil.rmtree(tmp, ignore_errors=True)
sys.exit(0 if all(results) else 1)
