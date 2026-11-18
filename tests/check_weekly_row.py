"""Offline checks for weekly_row.py. Every log line, file and company here is SIMULATED and
written to a temporary folder. The project's data/, run_logs/ and docs/ are never read or
written, apart from reading the scripts' own source text.

Run: python tests/check_weekly_row.py
"""
import csv
import json
import logging
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
import weekly_row as wr  # noqa: E402

tmp = Path(tempfile.mkdtemp(prefix="sim_weekly_"))
wr.RUNS_CSV, wr.LOG_DIR = tmp / "docs" / "weekly_runs.csv", tmp / "run_logs"
wr.ACCOUNTS_CSV, wr.FUNNEL_CSV, wr.CHECK_CSV = tmp / "data" / "accounts.csv", tmp / "data" / "funnel.csv", tmp / "data" / "postings_check.csv"
wr.SEEN_JSON = tmp / "data" / "raw" / "accounts_seen.json"
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
wr.log.addHandler(H)
wr.log.setLevel(logging.INFO)

LOGS = {
    "funding": "INFO SEC search: 2011 Form D and D/A filings by CA issuers\nINFO Wrote 1347 filings to data/funding.csv. Skipped this run: 0.\n",
    "jobs_funded": "INFO Jobs received this run: 4, so the expected cost is 4 credits.\nINFO API credits used by this run: 4. Left this billing cycle: 134 of 200.\n",
    "jobs_hiring": "INFO API credits used by this run: 10. Left this billing cycle: 124 of 200.\n",
    "hubspot": "INFO HubSpot push: created 7, updated 30, skipped 0, failed 0.\n",
    "digest": "INFO n8n digest: 9 new account(s), 2 flagged; 1 HubSpot task created.\n",
}
ACCOUNTS = [("Simco Alpha, Inc.", "funded_and_hiring", ""), ("Simco Bravo", "hiring_only", "SIMULATED flag"),
            ("Simco Charlie", "hiring_only", ""), ("Simco Delta", "hiring_only", " ")]


def write_files(logs=LOGS, accounts=ACCOUNTS, statuses=("open", "open", "reposted", "closed", "unknown", "not checkable")):
    shutil.rmtree(wr.LOG_DIR, ignore_errors=True)   # the private list of accounts seen stays, as it does between runs
    wr.LOG_DIR.mkdir(parents=True)
    wr.ACCOUNTS_CSV.parent.mkdir(parents=True, exist_ok=True)
    for file in (wr.ACCOUNTS_CSV, wr.FUNNEL_CSV, wr.CHECK_CSV):
        file.unlink(missing_ok=True)
    for name, text in logs.items():
        (wr.LOG_DIR / f"{name}.log").write_text(text, encoding="utf-8")
    if accounts is not None:
        with wr.ACCOUNTS_CSV.open("w", newline="") as handle:
            csv.writer(handle).writerows([("company_name", "signal_tier", "flags")] + list(accounts))
        wr.FUNNEL_CSV.write_text(f"step,companies\ncompanies with a relevant posting,{len(accounts) + 2}\nlisted in accounts.csv,{len(accounts)}\n")
    if statuses is not None:
        wr.CHECK_CSV.write_text("company_name,status\n" + "".join(f"Simco,{status}\n" for status in statuses))


def rows():
    with wr.RUNS_CSV.open(newline="") as handle:
        return list(csv.DictReader(handle))


def run(*args):
    H.lines.clear()
    return wr.main(list(args))


# 1. one run: every number comes from a log line or a file
write_files()
started = time.time() - 600
code = run("--status", "success", "--trigger", "schedule", "--started-at", str(started),
           "--run-url", "https://github.com/sim-owner/sim-repo/actions/runs/123")
row = rows()[0] if wr.RUNS_CSV.exists() else {}
check("the run exits 0 and writes the planned columns", code == 0 and list(row) == wr.COLUMNS and len(rows()) == 1)
check("date, trigger, status and the run's page are recorded, and the minutes come from the start time",
      re.fullmatch(r"\d{4}-\d{2}-\d{2}", row["run_date"]) and (row["trigger"], row["status"]) == ("schedule", "success")
      and row["run_url"].endswith("/actions/runs/123") and 9.9 <= float(row["minutes"]) <= 10.2)
check("filings, HubSpot and digest counts are read from the steps' own log lines",
      (row["filings_read"], row["hubspot_created"], row["hubspot_updated"], row["digest_listed"]) == ("1347", "7", "30", "9"))
check("credits are added up over both job searches", row["credits_spent"] == "14")
check("accounts, tiers and flags are counted from accounts.csv; a flag of spaces is not a flag",
      (row["companies_with_posting"], row["accounts"], row["funded_and_hiring"], row["hiring_only"], row["flagged"]) == ("6", "4", "1", "3", "1"))
check("postings: open and reposted count as still open; unknown and not checkable are not counted as checked",
      (row["postings_checked"], row["postings_still_open"]) == ("4", "3"))
check("on the first run there is no earlier list to compare with, so the new accounts are left empty", row["new_accounts"] == "")
public = wr.RUNS_CSV.read_text()
check("the public row holds counts only: no company name", "Simco" not in public and "simco" not in public
      and not any("Simco" in line for line in H.lines))
check("the names of the accounts seen go to the private file, cleaned",
      json.loads(wr.SEEN_JSON.read_text()) == ["simco alpha", "simco bravo", "simco charlie", "simco delta"]
      and "raw" in wr.SEEN_JSON.parts)

# 2. the next run appends, and counts only the accounts it has not seen
write_files(accounts=ACCOUNTS[1:] + [("SIMCO  ALPHA INC", "funded_and_hiring", ""), ("Simco Echo", "hiring_only", "")])
code = run("--status", "failure", "--trigger", "workflow_dispatch")
check("a second run appends a second row and keeps the first", code == 0 and len(rows()) == 2 and rows()[0] == row)
check("an account seen before under another spelling is not new", rows()[1]["new_accounts"] == "1" and rows()[1]["accounts"] == "5")
check("a failed run is recorded as failed, without minutes or a page when none is given",
      (rows()[1]["status"], rows()[1]["trigger"], rows()[1]["minutes"], rows()[1]["run_url"]) == ("failure", "workflow_dispatch", "", ""))

# 3. a number that cannot be read is left empty
write_files(logs={key: text for key, text in LOGS.items() if key != "jobs_hiring"}, statuses=None)
run("--status", "failure")
last = rows()[-1]
check("credits are empty when one of the two searches left no line: a partial sum is not written",
      last["credits_spent"] == "" and last["filings_read"] == "1347")
check("no posting check file: both posting counts are empty", (last["postings_checked"], last["postings_still_open"]) == ("", ""))
write_files(logs={"funding": "ERROR SEC did not answer\n", "hubspot": "INFO HubSpot push: created 0, updated 0"}, accounts=None, statuses=None)
run("--status", "failure")
last = rows()[-1]
check("a run that stopped early still gets a row, with empty cells where a step did not get that far",
      (last["filings_read"], last["credits_spent"], last["accounts"], last["new_accounts"], last["digest_listed"]) == ("", "", "", "", "")
      and (last["hubspot_created"], last["hubspot_updated"]) == ("0", "0") and len(rows()) == 4)

# 4. --dry-run and refusals
before, seen_before = wr.RUNS_CSV.read_text(), wr.SEEN_JSON.read_text()
write_files(accounts=[("Simco Foxtrot", "hiring_only", "")])
code = run("--dry-run")
check("--dry-run prints the row and writes nothing, the private list included",
      code == 0 and wr.RUNS_CSV.read_text() == before and wr.SEEN_JSON.read_text() == seen_before
      and any("new_accounts=1," in line for line in H.lines) and any("Dry run: nothing written." in line for line in H.lines))
wr.RUNS_CSV.write_text("run_date,something_else\n2026-01-01,1\n")
code = run("--status", "success")
check("a file with other columns is left alone and the command exits 1",
      code == 1 and wr.RUNS_CSV.read_text() == "run_date,something_else\n2026-01-01,1\n")
wr.RUNS_CSV.write_text(before)
refused = 0
for bad in (["--status", "great"], ["--trigger", "Schedule; rm"], ["--run-url", "https://example.com/x"],
            ["--run-url", "https://github.com/a/b/actions/runs/1?x=y"]):
    try:
        wr.main(bad)
    except SystemExit as stop:
        refused += stop.code == 2
check("an unknown status, an odd trigger and a page that is not a GitHub run are refused", refused == 4 and wr.RUNS_CSV.read_text() == before)

# 5. the lines this script looks for are the lines the steps print
sources = {name: (PROJECT / f"{name}.py").read_text(encoding="utf-8") for name in ("fetch_funding", "fetch_jobs", "push_hubspot", "digest_n8n")}
check("each pattern still matches what its step logs",
      "Wrote %d filings" in sources["fetch_funding"] and "API credits used by this run: %s." in sources["fetch_jobs"]
      and "HubSpot push: created %d, updated %d" in sources["push_hubspot"]
      and "n8n digest: {found} new account(s)" in sources["digest_n8n"])
workflow = (PROJECT / ".github" / "workflows" / "weekly.yml").read_text(encoding="utf-8")
check("the workflow writes a log for every name this script reads",
      all(f"run_logs/{name}.log" in workflow for names, _ in wr.LOG_COUNTS.values() for name in names))
# The log of every run is public. GitHub prints the value of a repository variable in the log
# header of each step, so nothing private may reach the workflow through `vars`.
check("the workflow reads no repository variable, and the two hand-edited files come from secrets",
      "vars." not in workflow and "${{ secrets.REVIEW_DECISIONS_CSV }}" in workflow
      and "${{ secrets.DOMAIN_OVERRIDES_CSV }}" in workflow)
job_env = workflow.split("    env:\n", 1)[1].split("    steps:\n", 1)[0]
check("only keys and the credit caps sit in the job's own environment, each key as a secret",
      sorted(line.split(":")[0].strip() for line in job_env.splitlines() if line.strip() and not line.strip().startswith("#"))
      == ["FUNDED_CREDITS", "HIRING_CREDITS", "HUBSPOT_SERVICE_KEY", "SEC_USER_AGENT", "THEIRSTACK_API_KEY"]
      and job_env.count("${{ secrets.") == 3)
source = (PROJECT / "weekly_row.py").read_text(encoding="utf-8")
check("the script sends nothing anywhere", not re.search(r"\brequests\b|urllib|http\.client|socket|subprocess", source.split('"""', 2)[2]))

print(f"\n{sum(results)}/{len(results)} checks passed")
if all(results):
    shutil.rmtree(tmp, ignore_errors=True)
sys.exit(0 if all(results) else 1)
