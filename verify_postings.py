"""Check whether the job postings behind the listed accounts are still open (optional step).

Reads data/accounts.csv (step 3): each account's `source_urls` holds the links of its job
postings. It also reads data/jobs.csv, if it exists, for each posting's title. For a posting
on a job board with a documented public API, the script asks that board for its published
postings and looks for this one:

- Ashby:      GET https://api.ashbyhq.com/posting-api/job-board/<board>
- Greenhouse: GET https://boards-api.greenhouse.io/v1/boards/<board>/jobs
- Lever:      GET https://api.lever.co/v0/postings/<site>?mode=json

Postings on LinkedIn or any other site are not checked. This project does not scrape, so
those stay a hand check and are counted as "not checkable".

Writes data/postings_check.csv (company_name, posting_url, board, status, checked_at). The
status is one of:
- "open": the posting is on the board's published list;
- "reposted": the posting's own link is gone, but the board lists a posting with the same
  title, so the role is still open under a new link;
- "closed": the board answered and neither the posting nor its title is on the list. A
  posting the company took off its list but left reachable by direct link also counts as
  closed;
- "unknown": the board gave no usable answer, for example because the company left it;
- "not checkable": the link is not on one of the three boards.

Requests: one per board, without a key, about one a second. Each answer is saved in
data/raw/ with its time, without the long description text of the postings, and an answer
younger than --max-age-hours is reused. HTTP 429 is retried after a wait; any other failure
marks that board's postings "unknown" and the run goes on. Counts only are logged, because
the weekly run's log is public.

Usage:
    python verify_postings.py --dry-run   # count what would be asked; send nothing
    python verify_postings.py
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parent
ACCOUNTS_CSV = ROOT / "data" / "accounts.csv"
JOBS_CSV = ROOT / "data" / "jobs.csv"
CHECK_CSV = ROOT / "data" / "postings_check.csv"
RAW_DIR = ROOT / "data" / "raw"

CONFIG: dict[str, Any] = {
    # Job board hosts whose postings can be checked, and the kind of board behind each
    "boards": {"jobs.ashbyhq.com": "ashby", "boards.greenhouse.io": "greenhouse",
               "job-boards.greenhouse.io": "greenhouse", "jobs.lever.co": "lever"},
    # Each board's documented endpoint that lists its published postings
    "list_urls": {"ashby": "https://api.ashbyhq.com/posting-api/job-board/{board}",
                  "greenhouse": "https://boards-api.greenhouse.io/v1/boards/{board}/jobs",
                  "lever": "https://api.lever.co/v0/postings/{board}?mode=json"},
    "filing_hosts": ["sec.gov"],            # Form D links in source_urls are not postings
    "user_agent": "signal-pipeline posting check (python-requests)",   # generic: no personal details
    "long_fields": ["descriptionHtml", "descriptionPlain", "description", "additional", "additionalPlain",
                    "lists", "content"],    # posting text that is not needed and not saved
    "default_max_age_hours": 20.0,
    "request_interval_s": 1.0,
    "max_retries": 3,                       # for HTTP 429 only
    "max_wait_s": 60,
    "timeout_s": 30,
}
COLUMNS = ["company_name", "posting_url", "board", "status", "checked_at"]

log = logging.getLogger("verify_postings")


class BoardError(RuntimeError):
    """One board gave no usable answer; its postings are marked "unknown" and the run goes on."""


def shown(path: Path) -> Path:
    """A path relative to the project, for log lines."""
    return path.relative_to(ROOT) if path.is_relative_to(ROOT) else path


def host_of(url: str) -> str:
    """The host of a link, lower-cased and without "www."."""
    return urlsplit(str(url)).netloc.lower().removeprefix("www.")


def posting_links(accounts: pd.DataFrame) -> list[dict[str, str]]:
    """One row per job posting link of every listed account. Form D filing links are left out."""
    rows = []
    for account in accounts.itertuples():
        for url in str(account.source_urls).split():
            host = host_of(url)
            if host and not any(host == filing or host.endswith("." + filing) for filing in CONFIG["filing_hosts"]):
                rows.append({"company_name": account.company_name, "posting_url": url})
    return rows


def tidy_title(title: Any) -> str:
    """A job title without case and extra spaces, so that two spellings of one title compare equal."""
    return " ".join(str(title or "").split()).lower()


def posting_titles() -> dict[str, str]:
    """Posting link (without its query string) -> tidied job title, from data/jobs.csv."""
    if not JOBS_CSV.exists():
        return {}
    jobs = pd.read_csv(JOBS_CSV, dtype=str, keep_default_na=False)
    titles: dict[str, str] = {}
    for column in ("job_url", "posting_url"):    # the posting's own link wins over a copy's
        if {column, "job_title"} <= set(jobs.columns):
            titles.update({url.split("?")[0]: tidy_title(title) for url, title in zip(jobs[column], jobs["job_title"]) if url})
    return titles


def locate(url: str) -> tuple[str, str, str] | None:
    """(kind of board, board name, posting id) for a link on a board this script can ask, or
    None for any other link."""
    kind = CONFIG["boards"].get(host_of(url))
    parts = [unquote(part) for part in urlsplit(str(url)).path.split("/") if part]
    if kind == "greenhouse":
        if "jobs" not in parts[1:-1]:
            return None
        return kind, parts[0], parts[parts.index("jobs", 1) + 1]
    if kind in ("ashby", "lever") and len(parts) >= 2:
        return kind, parts[0], parts[1]
    return None


def open_ids(kind: str, body: Any) -> set[str]:
    """The ids of every posting on a board's published list, lower-cased. Raises ValueError
    when the answer does not have the documented shape."""
    try:
        if kind == "greenhouse":
            return {str(job["id"]).lower() for job in body["jobs"]}
        if kind == "lever":
            if not isinstance(body, list):
                raise TypeError("not a list")
            return {str(posting["id"]).lower() for posting in body}
        ids: set[str] = set()
        for job in body["jobs"]:      # Ashby: the id is the last part of the posting's own link
            ids.update(str(job[field]).lower() for field in ("id",) if job.get(field))
            for field in ("jobUrl", "applyUrl"):
                ids.update(part.lower() for part in urlsplit(str(job.get(field) or "")).path.split("/")[2:] if part)
            if not (job.get("id") or job.get("jobUrl") or job.get("applyUrl")):
                raise KeyError("a posting without an id or a link")
        return ids
    except (KeyError, TypeError, AttributeError) as err:
        raise ValueError(f"unexpected answer from a {kind} board ({type(err).__name__})") from err


def open_titles(kind: str, body: Any) -> set[str]:
    """The tidied titles of every posting on a board's published list. Call open_ids() first:
    it refuses an answer that does not have the documented shape."""
    postings = body if kind == "lever" else body["jobs"]
    return {tidy_title(posting.get("text" if kind == "lever" else "title")) for posting in postings} - {""}


def without_long_text(body: Any) -> Any:
    """A board's answer without the long description text of each posting."""
    def trim(posting: Any) -> Any:
        if not isinstance(posting, dict):
            return posting
        return {key: value for key, value in posting.items() if key not in CONFIG["long_fields"]}
    if isinstance(body, list):
        return [trim(posting) for posting in body]
    if isinstance(body, dict) and isinstance(body.get("jobs"), list):
        return {**body, "jobs": [trim(posting) for posting in body["jobs"]]}
    return body


def cache_name(kind: str, board: str) -> str:
    """The start of a saved answer's file name for one board."""
    return f"job_board_{kind}_{re.sub(r'[^a-z0-9.-]+', '_', board.lower())}_"


def read_cache(kind: str, board: str, now: datetime, max_age_hours: float) -> tuple[set[str], set[str]] | None:
    """The posting ids and titles in the newest saved answer for this board, if it is recent enough."""
    for path in sorted(RAW_DIR.glob(cache_name(kind, board) + "*.json"), reverse=True):
        try:
            saved = json.loads(path.read_text(encoding="utf-8"))
            age = now - datetime.fromisoformat(saved["saved_at"])
            if saved["board"] == board and age <= timedelta(hours=max_age_hours):
                return set(saved["open_ids"]), set(saved["open_titles"])
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return None


def ask_board(session: requests.Session, kind: str, board: str, now: datetime) -> tuple[set[str], set[str]]:
    """Ask one board for its published postings, save the answer and return the open ids
    and titles. Raises BoardError when the board gives no usable answer."""
    url = CONFIG["list_urls"][kind].format(board=requests.utils.quote(board, safe=""))
    for attempt in range(CONFIG["max_retries"] + 1):
        try:
            response = session.get(url, timeout=CONFIG["timeout_s"])
        except requests.RequestException as err:
            raise BoardError(f"no answer ({type(err).__name__})") from err
        if response.status_code == 429 and attempt < CONFIG["max_retries"]:
            try:
                wait = float(response.headers.get("Retry-After", ""))
            except (TypeError, ValueError):
                wait = 2.0 ** (attempt + 1)
            time.sleep(min(wait, CONFIG["max_wait_s"]))
            continue
        if response.status_code != 200:
            raise BoardError(f"HTTP {response.status_code}")
        try:
            body = response.json()
            ids = open_ids(kind, body)
            titles = open_titles(kind, body)
        except (ValueError, AttributeError) as err:
            raise BoardError(str(err)) from err
        RAW_DIR.mkdir(parents=True, exist_ok=True)
        (RAW_DIR / f"{cache_name(kind, board)}{now.strftime('%Y-%m-%dT%H%M%SZ')}.json").write_text(
            json.dumps({"saved_at": now.isoformat(), "kind": kind, "board": board, "url": url,
                        "open_ids": sorted(ids), "open_titles": sorted(titles), "body": without_long_text(body)},
                       indent=1, ensure_ascii=False),
            encoding="utf-8")
        return ids, titles
    raise BoardError("HTTP 429 after every retry")


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    """Read the command line."""
    parser = argparse.ArgumentParser(description="Check which postings of the listed accounts are still open.")
    parser.add_argument("--dry-run", action="store_true", help="count what would be asked; send nothing")
    parser.add_argument("--max-age-hours", type=float, default=CONFIG["default_max_age_hours"],
                        help=f"reuse a board's saved answer up to this age (default {CONFIG['default_max_age_hours']:g})")
    args = parser.parse_args(argv)
    if args.max_age_hours < 0:
        parser.error("--max-age-hours cannot be negative")
    return args


def main(argv: list[str] | None = None) -> int:
    """Check the postings of every listed account and write data/postings_check.csv."""
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if not ACCOUNTS_CSV.exists():
        log.error("%s is missing; run build_accounts.py first.", shown(ACCOUNTS_CSV))
        return 1
    accounts = pd.read_csv(ACCOUNTS_CSV, dtype=str, keep_default_na=False)
    rows = posting_links(accounts)
    titles_by_link = posting_titles()
    boards: dict[tuple[str, str], list[dict[str, str]]] = {}
    for row in rows:
        found = locate(row["posting_url"])
        row.update({"board": found[0] if found else "", "status": "" if found else "not checkable", "checked_at": ""})
        if found:
            row["_id"], row["_title"] = found[2].lower(), titles_by_link.get(row["posting_url"].split("?")[0], "")
            boards.setdefault(found[:2], []).append(row)
    checkable = sum(len(group) for group in boards.values())
    if args.dry_run:
        log.info("Dry run: nothing sent. %d posting link(s) at %d listed account(s); %d are on a job board with a "
                 "public API and would take %d request(s), one per board. %d cannot be checked.", len(rows),
                 len(accounts), checkable, len(boards), len(rows) - checkable)
        return 0

    session = requests.Session()
    session.headers["User-Agent"] = CONFIG["user_agent"]
    asked = from_cache = 0
    for (kind, board), group in boards.items():
        now = datetime.now(timezone.utc)
        listed = read_cache(kind, board, now, args.max_age_hours)
        if listed is not None:
            from_cache += 1
        else:
            try:
                listed = ask_board(session, kind, board, now)
            except BoardError as err:
                log.warning("One board gave no usable answer (%s); its %d posting(s) are marked unknown.", err, len(group))
            asked += 1
            time.sleep(CONFIG["request_interval_s"])
        for row in group:
            if listed is None:
                row["status"] = "unknown"
            elif row["_id"] in listed[0]:
                row["status"] = "open"
            else:
                row["status"] = "reposted" if row["_title"] and row["_title"] in listed[1] else "closed"
            row["checked_at"] = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    CHECK_CSV.parent.mkdir(parents=True, exist_ok=True)
    table = pd.DataFrame(rows, columns=COLUMNS)
    table.to_csv(CHECK_CSV, index=False)
    count = table["status"].value_counts().to_dict()
    checked = table[table["status"].isin(["open", "reposted", "closed"])]
    all_closed = sum(bool((statuses == "closed").all()) for _, statuses in checked.groupby("company_name")["status"])
    # Counts only: these lines also end up in the public log of the weekly run
    log.info("Posting check: %d posting link(s) at %d listed account(s). On a job board with a public API: %d, at %d "
             "account(s): %d open, %d reposted under a new link, %d closed, %d unknown. Not checkable (LinkedIn or "
             "another site): %d.", len(table), len(accounts), checkable,
             table.loc[table["board"] != "", "company_name"].nunique(), count.get("open", 0), count.get("reposted", 0),
             count.get("closed", 0), count.get("unknown", 0), count.get("not checkable", 0))
    log.info("Accounts whose every checked posting is closed: %d. Boards asked: %d, read from saved answers: %d. "
             "Results are in %s.", all_closed, asked, from_cache, shown(CHECK_CSV))
    return 0


if __name__ == "__main__":
    sys.exit(main())
