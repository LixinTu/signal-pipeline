"""Offline checks for why_now_check.py. Every company, posting, filing and line here is
SIMULATED. The project's data/ is never read or written.

The second half plants known mistakes, one at a time, in lines that pass, and confirms that
each one is caught: a guard that cannot fail proves nothing.

Run: python tests/check_why_now.py
"""
import logging
import re
import shutil
import sys
import tempfile
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
import why_now_check as wn  # noqa: E402
import pandas as pd  # noqa: E402

tmp = Path(tempfile.mkdtemp(prefix="sim_why_now_"))
wn.ACCOUNTS_CSV, wn.JOBS_CSV = tmp / "accounts.csv", tmp / "jobs.csv"
LINES = tmp / "lines.csv"
results = []


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
wn.log.addHandler(H)
wn.log.setLevel(logging.INFO)

# SIMULATED data: one funded account with two postings, one hiring-only account
accounts = pd.DataFrame([
    {"company_name": "Simco Robotics", "filing_date": "2026-11-13", "amount_sold_usd": "30000000",
     "score_reason": "first sale 2026-10-29, 18 days before 2026-11-16: +3; 1 relevant role title(s): +1",
     "theirstack_jobs_last_30_days": "15", "salary_ranges": "Forward Deployed Engineer $150k-$200k",
     "company_description": "SIMULATED: builds robots for 3 industries."},
    {"company_name": "Simco Voice", "filing_date": "", "amount_sold_usd": "",
     "score_reason": "no Bay Area Form D filing found in the funding window: +0; 1 relevant role title(s): +1",
     "theirstack_jobs_last_30_days": "21", "salary_ranges": "",
     "company_description": "SIMULATED: voice agents for 2,500 customers."},
])
jobs = pd.DataFrame([
    {"company_name": "Simco Robotics", "date_posted": "2026-11-12", "employee_count": "60", "company_long_description": "SIMULATED long text."},
    {"company_name": "Simco Robotics", "date_posted": "2025-08-20", "employee_count": "60", "company_long_description": "SIMULATED long text."},
    {"company_name": "Simco Voice", "date_posted": "2026-11-10", "employee_count": "25", "company_long_description": "SIMULATED: 12 languages."},
])
GOOD = {
    "Simco Robotics": "Filed a Form D on Nov 13 reporting $30.0M sold (first sale Oct 29) and posted a Forward Deployed Engineer "
                      "role on Nov 12 ($150k-$200k); a 60-person company with 15 postings in 30 days, hiring since Aug 2025.",
    "Simco Voice": "Posted an Applied AI Engineer role in San Francisco on Nov 10; a 25-person company serving 2,500 customers "
                   "in 12 languages, with 21 postings in 30 days.",
}
account = accounts.set_index("company_name", drop=False)


def problems(company, line, banned=None):
    return wn.check_line(line, account.loc[company], jobs[jobs["company_name"] == company], banned)


# 1. lines that state only what the data says pass
check("a line with a filing date, a first-sale date, a posting date, an amount, a salary, a headcount and counts passes",
      problems("Simco Robotics", GOOD["Simco Robotics"]) == [])
check("numbers from the company's own description pass, with or without a thousands comma",
      problems("Simco Voice", GOOD["Simco Voice"]) == [])
check("the first-sale date is read from the score reason; a hiring-only account has none",
      wn.first_sale(account.loc["Simco Robotics"]) == "2026-10-29" and wn.first_sale(account.loc["Simco Voice"]) == "")
check("the dates a line may use are the company's own posting, filing and first-sale dates",
      len(wn.row_dates(account.loc["Simco Robotics"], jobs[jobs["company_name"] == "Simco Robotics"])) == 4
      and len(wn.row_dates(account.loc["Simco Voice"], jobs[jobs["company_name"] == "Simco Voice"])) == 1)

# 2. planted mistakes: each one must be caught, and for the stated reason
PLANTED = [  # (what is wrong, company, text in the good line, text that replaces it, a word of the expected reason)
    ("a posting date one day off", "Simco Robotics", "on Nov 12", "on Nov 11", 'date "Nov 11"'),
    ("a filing date one day off", "Simco Robotics", "on Nov 13", "on Nov 14", 'date "Nov 14"'),
    ("a wrong first-sale date", "Simco Robotics", "first sale Oct 29", "first sale Oct 28", 'date "Oct 28"'),
    ("a wrong month and year", "Simco Robotics", "Aug 2025", "Sep 2025", 'date "Sep 2025"'),
    ("the other company's posting date", "Simco Robotics", "on Nov 12", "on Nov 10", 'date "Nov 10"'),
    ("a wrong amount sold", "Simco Robotics", "$30.0M", "$31.0M", 'number "$31.0M"'),
    ("a wrong salary bound", "Simco Robotics", "$150k-$200k", "$150k-$210k", 'number "$210k"'),
    ("a wrong headcount", "Simco Robotics", "60-person", "61-person", 'number "61"'),
    ("a wrong 30-day count", "Simco Robotics", "15 postings", "16 postings", 'number "16"'),
    ("the other company's customer count", "Simco Robotics", "a 60-person company", "a 60-person company with 2,500 customers", 'number "2,500"'),
    ("a number from nowhere", "Simco Voice", "25-person company", "25-person company that doubled to 50", 'number "50"'),
    ("a round name", "Simco Robotics", "a 60-person company", "a 60-person Series B company", "names a round"),
    ("the word raised", "Simco Robotics", "reporting $30.0M sold", "after it raised $30.0M", "names a round"),
    ("a seed round", "Simco Voice", "a 25-person company", "a 25-person seed company", "names a round"),
    ("a person", "Simco Voice", "a 25-person company", "a 25-person company whose CEO is hiring", "talks about a person"),
    ("a founder", "Simco Voice", "a 25-person company", "a 25-person company led by its co-founders", "talks about a person"),
    ("a line that is too long", "Simco Voice", "in 30 days.", "in 30 days, " + "and " * 20 + "more.", "over the limit of 40"),
]
caught = 0
for what, company, old, new, reason in PLANTED:
    assert old in GOOD[company], (what, old)
    found = problems(company, GOOD[company].replace(old, new))
    hit = any(reason in problem for problem in found)
    caught += hit
    if not hit:
        print(f"MISSED - {what}: {found}")
check(f"every planted mistake is caught for the stated reason ({caught} of {len(PLANTED)})", caught == len(PLANTED))
check("a line of exactly 40 words passes and one of 41 does not",
      problems("Simco Voice", " ".join(["word"] * 40)) == [] and len(problems("Simco Voice", " ".join(["word"] * 41))) == 1)
check("banned words are matched as words, in any case: 'Seedling' passes, 'SERIES A' does not",
      problems("Simco Voice", "Posted a role for its Seedling product.") == []
      and problems("Simco Voice", "A SERIES A company.") != [])
extra = [(r"\bunicorn\b", "SIMULATED extra rule")]
check("a caller can pass its own banned words, which replace the built-in ones",
      problems("Simco Voice", "A unicorn that raised money.", extra) == ["SIMULATED extra rule"]
      and problems("Simco Voice", "A unicorn that raised money.") == [wn.CONFIG["banned"][0][1]])

# 3. every account needs one line, and only accounts have lines
lines = pd.DataFrame({"company_name": list(GOOD), "why_now": list(GOOD.values())})
check("two good lines: 2 checked, 0 problems", wn.check_all(accounts, jobs, lines) == (2, []))
check("an account without a line, or with an empty one, is a problem",
      wn.check_all(accounts, jobs, lines.head(1)) == (1, ["Simco Voice: no why-now line"])
      and wn.check_all(accounts, jobs, lines.assign(why_now=[GOOD["Simco Robotics"], "  "]))[1] == ["Simco Voice: no why-now line"])
stray = pd.concat([lines, pd.DataFrame({"company_name": ["Simco Gone"], "why_now": ["A line."]})], ignore_index=True)
check("a line for a company that is not an account is a problem", wn.check_all(accounts, jobs, stray)[1] == ["Simco Gone: has a line but is not in accounts.csv"])
check("a company named twice is a problem", "the lines file names a company twice" in wn.check_all(accounts, jobs, pd.concat([lines, lines.head(1)]))[1])
check("an account with no posting rows cannot be checked, and that is said",
      wn.check_all(accounts, jobs[jobs["company_name"] != "Simco Voice"], lines) == (1, ["Simco Voice: no rows in jobs.csv"]))
check("problems carry the company's name", wn.check_all(accounts, jobs, lines.assign(why_now=[GOOD["Simco Robotics"].replace("Nov 12", "Nov 11"), GOOD["Simco Voice"]]))[1]
      == ['Simco Robotics: date "Nov 11" is not in the data'])

# 4. the command
accounts.to_csv(wn.ACCOUNTS_CSV, index=False)
jobs.to_csv(wn.JOBS_CSV, index=False)
lines.to_csv(LINES, index=False)
H.lines.clear()
check("good lines: exit 0 and the count is logged",
      wn.main(["--lines", str(LINES)]) == 0 and any("Checked 2 why-now line(s) against the data: 0 problem(s)." in line for line in H.lines))
lines.assign(why_now=[GOOD["Simco Robotics"].replace("$30.0M", "$3.0M"), GOOD["Simco Voice"]]).to_csv(LINES, index=False)
H.lines.clear()
check("one bad line: exit 1, the problem is shown and counted",
      wn.main(["--lines", str(LINES)]) == 1 and any('WARNING Simco Robotics: number "$3.0M" is not in the data' in line for line in H.lines)
      and any("1 problem(s)" in line for line in H.lines))
pd.DataFrame({"company": ["x"], "line": ["y"]}).to_csv(LINES, index=False)
check("a lines file with other columns: exit 1", wn.main(["--lines", str(LINES)]) == 1)
check("a missing lines file: exit 1", wn.main(["--lines", str(tmp / "nothing.csv")]) == 1)
wn.JOBS_CSV.unlink()
lines.to_csv(LINES, index=False)
check("a missing jobs.csv: exit 1", wn.main(["--lines", str(LINES)]) == 1)

# 5. what the script is able to do at all
source = (PROJECT / "why_now_check.py").read_text(encoding="utf-8").split('"""', 2)[2]
check("the script calls no model and no API, and writes no file",
      not re.search(r"requests|urllib|anthropic|openai|http|subprocess|to_csv|write_text|open\(", source))

print(f"\n{sum(results)}/{len(results)} checks passed")
if all(results):
    shutil.rmtree(tmp, ignore_errors=True)
sys.exit(0 if all(results) else 1)
