"""Append one row of counts for a run of the pipeline to docs/weekly_runs.csv.

The weekly workflow runs this as one of its last steps and commits the file, so the
repository keeps a public record of every run: one row a week. Counts only: the row never
holds a company name, because the file is public.

Where each number comes from:
- data/accounts.csv, data/funnel.csv and data/postings_check.csv, written by the steps;
- the counts lines the steps log, read from run_logs/*.log: filings read, TheirStack credits
  spent, companies created and updated in HubSpot, accounts in the digest;
- data/raw/accounts_seen.json, the names of the accounts that earlier runs listed, to count
  the new ones. That file stays private, like everything in data/raw/. Without it there is
  nothing to compare with, so the first run leaves `new_accounts` empty.
A number that cannot be read is left empty, never guessed.

Usage (by the workflow):
    python weekly_row.py --status success --trigger schedule --started-at 1763478000 --run-url https://github.com/...
    python weekly_row.py --dry-run     # print the row; write nothing
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from names import clean_name

ROOT = Path(__file__).resolve().parent
RUNS_CSV = ROOT / "docs" / "weekly_runs.csv"
LOG_DIR = ROOT / "run_logs"
ACCOUNTS_CSV = ROOT / "data" / "accounts.csv"
FUNNEL_CSV = ROOT / "data" / "funnel.csv"
CHECK_CSV = ROOT / "data" / "postings_check.csv"
SEEN_JSON = ROOT / "data" / "raw" / "accounts_seen.json"

COLUMNS = ["run_date", "trigger", "status", "minutes", "filings_read", "credits_spent", "companies_with_posting",
           "accounts", "funded_and_hiring", "hiring_only", "flagged", "new_accounts", "hubspot_created",
           "hubspot_updated", "digest_listed", "postings_checked", "postings_still_open", "run_url"]
# column -> (the logs that must each hold the line, the line's pattern with the number in group 1)
LOG_COUNTS: dict[str, tuple[tuple[str, ...], str]] = {
    "filings_read": (("funding",), r"Wrote (\d+) filings"),
    "credits_spent": (("jobs_funded", "jobs_hiring"), r"API credits used by this run: (\d+(?:\.\d+)?)\."),
    "hubspot_created": (("hubspot",), r"HubSpot push: created (\d+), updated \d+"),
    "hubspot_updated": (("hubspot",), r"HubSpot push: created \d+, updated (\d+)"),
    "digest_listed": (("digest",), r"n8n digest: (\d+) new account\(s\)"),
}
STATUSES = ("success", "failure", "cancelled", "")
STILL_OPEN = ("open", "reposted")
CHECKED = STILL_OPEN + ("closed",)

log = logging.getLogger("weekly_row")


def from_logs(column: str) -> str:
    """The number a step logged, added up over the logs named for this column, or "" when
    any of those logs is missing or does not hold the line."""
    names, pattern = LOG_COUNTS[column]
    total = 0.0
    for name in names:
        try:
            found = re.findall(pattern, (LOG_DIR / f"{name}.log").read_text(encoding="utf-8", errors="replace"))
        except OSError:
            return ""
        if not found:
            return ""
        total += float(found[-1])
    return f"{total:g}"


def read_rows(path: Path) -> list[dict[str, str]]:
    """The rows of a CSV file as dictionaries, or [] when the file does not exist."""
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def from_files(remember: bool) -> dict[str, Any]:
    """The counts that come from the files the steps wrote. With `remember`, the names of
    this run's accounts are added to the private list of accounts already seen."""
    counts: dict[str, Any] = dict.fromkeys(
        ("companies_with_posting", "accounts", "funded_and_hiring", "hiring_only", "flagged", "new_accounts",
         "postings_checked", "postings_still_open"), "")
    funnel = read_rows(FUNNEL_CSV)
    if funnel:
        counts["companies_with_posting"] = funnel[0].get("companies", "")
    if ACCOUNTS_CSV.exists():
        accounts = read_rows(ACCOUNTS_CSV)
        tiers = [row.get("signal_tier", "") for row in accounts]
        counts.update({"accounts": len(accounts), "funded_and_hiring": tiers.count("funded_and_hiring"),
                       "hiring_only": tiers.count("hiring_only"),
                       "flagged": sum(bool(row.get("flags", "").strip()) for row in accounts)})
        names = {clean_name(row.get("company_name", "")) for row in accounts} - {""}
        try:
            seen = set(json.loads(SEEN_JSON.read_text(encoding="utf-8")))
            counts["new_accounts"] = len(names - seen)
        except (OSError, ValueError, TypeError):   # no list of earlier accounts: unknown, not "all new"
            seen = set()
        if remember:
            SEEN_JSON.parent.mkdir(parents=True, exist_ok=True)
            SEEN_JSON.write_text(json.dumps(sorted(seen | names), indent=1, ensure_ascii=False), encoding="utf-8")
    if CHECK_CSV.exists():
        statuses = [row.get("status", "") for row in read_rows(CHECK_CSV)]
        counts["postings_checked"] = sum(status in CHECKED for status in statuses)
        counts["postings_still_open"] = sum(status in STILL_OPEN for status in statuses)
    return counts


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    """Read the command line."""
    parser = argparse.ArgumentParser(description="Append one row of counts for this run to docs/weekly_runs.csv.")
    parser.add_argument("--status", default="", choices=STATUSES, help="how the run ended so far")
    parser.add_argument("--trigger", default="local", help="what started the run: schedule, workflow_dispatch or local")
    parser.add_argument("--started-at", type=float, default=None, help="when the run started, in seconds since 1970")
    parser.add_argument("--run-url", default="", help="the public page of the run")
    parser.add_argument("--dry-run", action="store_true", help="print the row; write nothing")
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[a-z_]+", args.trigger):
        parser.error("--trigger takes lower-case letters and underscores only")
    if args.run_url and not re.fullmatch(r"https://github\.com/[\w.-]+/[\w.-]+/actions/runs/\d+", args.run_url):
        parser.error("--run-url must be the page of a GitHub Actions run")
    return args


def main(argv: list[str] | None = None) -> int:
    """Build this run's row and append it to docs/weekly_runs.csv."""
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    now = datetime.now(timezone.utc)
    if RUNS_CSV.exists():
        with RUNS_CSV.open(newline="", encoding="utf-8") as handle:
            header = next(csv.reader(handle), [])
        if header != COLUMNS:
            log.error("%s has other columns than this script writes; nothing was changed.", RUNS_CSV.name)
            return 1
    row: dict[str, Any] = {"run_date": now.strftime("%Y-%m-%d"), "trigger": args.trigger, "status": args.status,
                           "minutes": "", "run_url": args.run_url}
    if args.started_at is not None:
        row["minutes"] = f"{max(now.timestamp() - args.started_at, 0) / 60:.1f}"
    row.update({column: from_logs(column) for column in LOG_COUNTS})
    row.update(from_files(remember=not args.dry_run))
    line = [str(row[column]) for column in COLUMNS]
    log.info("This run's row: %s", ", ".join(f"{column}={value or '(empty)'}" for column, value in zip(COLUMNS, line)))
    if args.dry_run:
        log.info("Dry run: nothing written.")
        return 0
    RUNS_CSV.parent.mkdir(parents=True, exist_ok=True)
    is_new = not RUNS_CSV.exists()
    with RUNS_CSV.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        if is_new:
            writer.writerow(COLUMNS)
        writer.writerow(line)
    log.info("Appended one row to %s.", RUNS_CSV.relative_to(ROOT) if RUNS_CSV.is_relative_to(ROOT) else RUNS_CSV)
    return 0


if __name__ == "__main__":
    sys.exit(main())
