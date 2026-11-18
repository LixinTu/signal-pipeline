"""Check AI-drafted "why now" lines against the data they were written from (run by hand).

A "why now" line is one sentence per account that says why to contact the company this
week. The lines are drafted by an AI model during review, under the written rules in
prompts/why_now_rules.md. This script is the guard on that step: before anyone uses a line,
every date, amount and count in it must be found in the account's own data.

Reads data/accounts.csv (step 3), data/jobs.csv (step 1) and a CSV of lines with the columns
company_name and why_now. A line fails when:
- it is longer than CONFIG["max_words"] words;
- it uses a banned word: a round name or "raised" (Form D has an amount sold, not a round),
  or a person's title;
- it names a date ("Nov 12", "Aug 2025") that is not a posting, filing or first-sale date
  of that company;
- it holds a number that is not in the company's data: amount sold, a salary bound,
  headcount, TheirStack's 30-day posting count, or a number in the company's own
  description.
Every account needs one line, and no line may belong to a company that is not an account.

Not checked: the wording, and whether a posting is still open (see verify_postings.py). A
person reads every line. The script calls no model and no API, and it prints its findings
to the terminal only.

Usage:
    python why_now_check.py --lines path/to/why_now_lines.csv
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from datetime import date
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent
ACCOUNTS_CSV = ROOT / "data" / "accounts.csv"
JOBS_CSV = ROOT / "data" / "jobs.csv"

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
MONTH_PATTERN = "|".join(MONTHS)
CONFIG = {
    "max_words": 40,
    # Words a line may not use, and why
    "banned": [
        (r"\bseries [a-z]\b|\bseed\b|\braised\b", "names a round or says 'raised' (Form D gives an amount sold, not a round)"),
        (r"\bfounders?\b|\bco-founders?\b|\bCEO\b|\bCTO\b", "talks about a person"),
    ],
}
NUMBER = re.compile(r"\$?\d[\d,]*(?:\.\d+)?[kKmM]?")

log = logging.getLogger("why_now_check")


def first_sale(account: pd.Series) -> str:
    """First-sale date of the Form D round, read from the score reason, or ""."""
    found = re.search(r"first sale (\d{4}-\d{2}-\d{2})", account["score_reason"])
    return found.group(1) if found else ""


def row_dates(account: pd.Series, company_jobs: pd.DataFrame) -> set[date]:
    """Every date a line about this company may mention: postings, filing and first sale."""
    values = set(company_jobs["date_posted"]) | {account["filing_date"], first_sale(account)}
    return {date.fromisoformat(value) for value in values if value}


def allowed_numbers(account: pd.Series, company_jobs: pd.DataFrame) -> set[str]:
    """Every number a line about this company may mention, in a normal form ("30.0m", "150k", "15")."""
    allowed = {"30", account["theirstack_jobs_last_30_days"]}  # "in 30 days" is TheirStack's window
    allowed |= set(company_jobs["employee_count"])
    if account["amount_sold_usd"]:
        allowed.add(f"{float(account['amount_sold_usd']) / 1e6:.1f}m")
    allowed |= {token.lstrip("$") for token in re.findall(r"\$\d+k", account["salary_ranges"])}
    for text in [account["company_description"], *company_jobs["company_long_description"]]:
        allowed |= {token.replace(",", "") for token in re.findall(r"\d[\d,]*\d|\d", text)}
    allowed.discard("")
    return allowed


def check_line(line: str, account: pd.Series, company_jobs: pd.DataFrame,
               banned: list[tuple[str, str]] | None = None) -> list[str]:
    """Problems with one "why now" line; an empty list means it passed. `banned` replaces
    the banned words in CONFIG, for a caller with a longer list."""
    problems = []
    if len(line.split()) > CONFIG["max_words"]:
        problems.append(f"{len(line.split())} words, over the limit of {CONFIG['max_words']}")
    for pattern, why in CONFIG["banned"] if banned is None else banned:
        if re.search(pattern, line, re.I):
            problems.append(why)
    dates = row_dates(account, company_jobs)
    for month, year in re.findall(rf"\b({MONTH_PATTERN}) (\d{{4}})\b", line):
        if not any(d.month == MONTHS.index(month) + 1 and d.year == int(year) for d in dates):
            problems.append(f'date "{month} {year}" is not in the data')
    rest = re.sub(rf"\b({MONTH_PATTERN}) \d{{4}}\b", " ", line)
    for month, day in re.findall(rf"\b({MONTH_PATTERN}) (\d{{1,2}})\b", rest):
        if not any(d.month == MONTHS.index(month) + 1 and d.day == int(day) for d in dates):
            problems.append(f'date "{month} {day}" is not in the data')
    rest = re.sub(rf"\b({MONTH_PATTERN}) \d{{1,2}}\b", " ", rest)
    allowed = allowed_numbers(account, company_jobs)
    for token in NUMBER.findall(rest):
        if token.replace("$", "").replace(",", "").lower() not in allowed:
            problems.append(f'number "{token}" is not in the data')
    return problems


def check_all(accounts: pd.DataFrame, jobs: pd.DataFrame, lines: pd.DataFrame,
              banned: list[tuple[str, str]] | None = None) -> tuple[int, list[str]]:
    """Check every account's line. Returns how many lines were checked and every problem
    found, each starting with the company's name."""
    problems = []
    written = dict(zip(lines["company_name"], lines["why_now"]))
    if lines["company_name"].duplicated().any():
        problems.append("the lines file names a company twice")
    for name in sorted(set(written) - set(accounts["company_name"])):
        problems.append(f"{name}: has a line but is not in accounts.csv")
    checked = 0
    for _, account in accounts.iterrows():
        name = account["company_name"]
        company_jobs = jobs[jobs["company_name"] == name]
        if not str(written.get(name, "")).strip():
            problems.append(f"{name}: no why-now line")
        elif company_jobs.empty:
            problems.append(f"{name}: no rows in jobs.csv")
        else:
            checked += 1
            problems += [f"{name}: {problem}" for problem in check_line(written[name].strip(), account, company_jobs, banned)]
    return checked, problems


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    """Read the command line."""
    parser = argparse.ArgumentParser(description='Check "why now" lines against the data they were written from.')
    parser.add_argument("--lines", type=Path, required=True, help="CSV with the columns company_name and why_now")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Check the lines in --lines against data/accounts.csv and data/jobs.csv."""
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    for path in (ACCOUNTS_CSV, JOBS_CSV, args.lines):
        if not path.exists():
            log.error("%s is missing.", path)
            return 1
    read = lambda path: pd.read_csv(path, dtype=str).fillna("")  # noqa: E731
    lines = read(args.lines)
    if not {"company_name", "why_now"} <= set(lines.columns):
        log.error("%s needs the columns company_name and why_now.", args.lines.name)
        return 1
    checked, problems = check_all(read(ACCOUNTS_CSV), read(JOBS_CSV), lines)
    for problem in problems:
        log.warning("%s", problem)
    log.info("Checked %d why-now line(s) against the data: %d problem(s).", checked, len(problems))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
