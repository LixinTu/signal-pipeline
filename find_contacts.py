"""Look up one contact per account through Hunter's API (optional, run by hand).

Reads data/accounts.csv (step 3) and data/jobs.csv (for headcount). For each company it asks
Hunter's Domain Search for the person that company should be contacted through: under 50
people a founder or the CEO, 50 to 199 the engineering lead, 200 or more the talent lead.

Each company gets up to two searches, the second only if the first comes back empty:
1. "targeted": people in the departments where the target role sits;
2. "wide": anyone Hunter holds for the domain.
Hunter returns up to 10 people per search. The script picks one and says how well it fits:
"title match" (the position names the target role, such as "VP Engineering"), "function
match" (the right team but not its lead, such as "Solutions Engineer") or "other role".
Among equal fits the more senior and then the more confident comes first. It keeps Hunter's
own verification status for that email and discards the others before anything is saved.
It is the code version of the "find people, then find the work email" columns of a Clay
table, with one data source; a second source is added only if the hit rate calls for it.

Personal data:
- One name and email per company is written, only to data/contacts/contacts.csv and
  data/raw/. The script refuses to run unless git ignores both, so they cannot be
  committed by accident. The people who were not picked are never written anywhere.
- Phone numbers and social profiles are dropped as soon as an answer arrives.
- It prints counts only, and it sends nothing to anyone: this script only reads.

Credits and requests:
- Hunter's free plan has 50 credits a month. Its pricing page says one credit per email
  found; every run so far was charged one credit per search that returned anything. The
  real cost is read from Hunter's X-Credits-Used header, a run stops at --max-credits, and
  it stops at once if a single search costs more than expected.
- A search already answered in the last 30 days is read from data/raw/ and costs nothing.
- Hunter's status codes differ from most APIs: 403 means "too many requests, wait" and is
  retried; 429 means the month's credits are used up, so the run stops.
- Nothing is sent without HUNTER_API_KEY in .env; --dry-run needs no key.

Usage:
    python find_contacts.py --dry-run                    # show the plan; send nothing
    python find_contacts.py                              # the first 5 accounts
    python find_contacts.py --companies "Sim,Labelbox"   # the named accounts
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
ACCOUNTS_CSV = ROOT / "data" / "accounts.csv"
JOBS_CSV = ROOT / "data" / "jobs.csv"
CONTACTS_CSV = ROOT / "data" / "contacts" / "contacts.csv"
RAW_DIR = ROOT / "data" / "raw"
KEY_ENV = "HUNTER_API_KEY"
SEARCH_URL = "https://api.hunter.io/v2/domain-search"
ACCOUNT_URL = "https://api.hunter.io/v2/account"

CONFIG: dict[str, Any] = {
    "ask_for": 10,             # people read per search: the most Hunter's free plan returns
    "expected_cost": 1,        # credits one search with results has cost in every run so far
    "default_limit": 5,        # companies per run unless --limit says otherwise: test on 5 first
    "default_max_credits": 10,
    "cache_days": 30,
    "request_interval_s": 1.0,
    "max_retries": 3,          # for 403 (rate limit) and 5xx only
    "max_wait_s": 30,
    "timeout_s": 30,
    # Who to look for, by headcount: the departments to search first; `role` words name the target by
    # themselves, `function` words name its team and need a `lead_words` word to count as the target
    "targets": [
        (50, {"label": "founder or CEO", "department": "executive,management",
              "role": ["founder", "ceo", "chief executive", "president", "owner"], "function": []}),
        (200, {"label": "engineering lead", "department": "it,executive,management",
               "role": ["cto", "chief technology"],
               "function": ["engineering", "technology", "solutions", "forward deployed"]}),
        (None, {"label": "talent lead", "department": "hr", "role": ["chief people"],
                "function": ["talent", "recruit", "people", "human resources"]}),
    ],
    "lead_words": ["head", "vp", "vice president", "director", "chief", "lead", "manager"],
    "fit_labels": ["title match", "function match", "other role"],
    "unknown_headcount_target": 1,   # index into targets when TheirStack has no headcount
    "seniority_rank": {"executive": 0, "senior": 1},   # anything else ranks last
}
KEPT_FIELDS = ("value", "type", "confidence", "first_name", "last_name", "position", "seniority", "department")
COLUMNS = ["company_name", "domain", "headcount", "target", "result", "query", "contacts_returned", "first_name",
           "last_name", "position", "email", "confidence", "verification_status", "sources", "credits_used", "source",
           "searched_at"]

log = logging.getLogger("find_contacts")


class HunterError(RuntimeError):
    """One company could not be searched; the run goes on."""


class FatalError(HunterError):
    """The run cannot go on: a bad key, or no credits left."""


def shown(path: Path) -> Path:
    """A path relative to the project, for log lines."""
    return path.relative_to(ROOT) if path.is_relative_to(ROOT) else path


def git_ignored(path: Path) -> bool:
    """True only if git says it ignores this path. False if it does not, or if git cannot say."""
    try:
        return subprocess.run(["git", "-C", str(ROOT), "check-ignore", "-q", str(path)],
                              capture_output=True).returncode == 0
    except OSError:
        return False


def target_for(headcount: Any) -> dict[str, Any]:
    """Who to look for at a company of this size."""
    try:
        people = int(float(headcount))
    except (TypeError, ValueError):
        return CONFIG["targets"][CONFIG["unknown_headcount_target"]][1]
    return next(target for below, target in CONFIG["targets"] if below is None or people < below)


def search_plan(domain: str, target: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """The searches for one company, in order. The second runs only if the first finds nobody."""
    base = {"domain": domain, "type": "personal", "limit": CONFIG["ask_for"]}
    return [("targeted", {**base, "department": target["department"]}), ("wide", base)]


def trim(body: dict[str, Any]) -> dict[str, Any]:
    """Hunter's answer cut down to what this script uses. Phone numbers, social profiles and
    the pages an email was found on are dropped; only the number of sources is kept."""
    emails = []
    for email in (body.get("data") or {}).get("emails") or []:
        kept = {field: email.get(field) for field in KEPT_FIELDS}
        kept["verification_status"] = (email.get("verification") or {}).get("status")
        kept["sources"] = len(email.get("sources") or [])
        emails.append(kept)
    return {"emails": emails, "held": (body.get("meta") or {}).get("results")}


def choose(emails: list[dict[str, Any]], target: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
    """The one contact to keep and how well it fits: "title match" when the position names
    the target role, "function match" when it names the right team but not its lead, "other
    role" otherwise. Among equal fits the more senior comes first, then the more confident."""
    if not emails:
        return None, "none"

    def fit(email: dict[str, Any]) -> int:
        position = str(email.get("position") or "").lower()
        has = lambda words: any(re.search(rf"\b{re.escape(word)}", position) for word in words)  # noqa: E731
        if has(target["role"]) or (has(target["function"]) and has(CONFIG["lead_words"])):
            return 0
        return 1 if has(target["function"]) else 2

    ranked = sorted(emails, key=lambda email: (fit(email), CONFIG["seniority_rank"].get(email.get("seniority"), 2),
                                               -(email.get("confidence") or 0)))
    return ranked[0], CONFIG["fit_labels"][fit(ranked[0])]


def cache_file(domain: str) -> str:
    """The start of a cache file's name for one domain."""
    return f"hunter_domain_search_{re.sub(r'[^a-z0-9.-]+', '_', domain.lower())}_"


def read_cache(domain: str, params: dict[str, Any], now: datetime) -> dict[str, Any] | None:
    """The newest saved answer for the same query, if it is recent enough."""
    for path in sorted(RAW_DIR.glob(cache_file(domain) + "*.json"), reverse=True):
        try:
            saved = json.loads(path.read_text(encoding="utf-8"))
            age = now - datetime.fromisoformat(saved["searched_at"])
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if saved.get("params") == params and age <= timedelta(days=CONFIG["cache_days"]):
            return saved
    return None


class HunterClient:
    """Sends GET requests to Hunter and adds up what they cost."""

    def __init__(self, api_key: str) -> None:
        self.session = requests.Session()
        self.session.headers["X-API-KEY"] = api_key
        self.credits_used = 0.0
        self.cost_is_measured = True
        self.requests_sent = 0

    def get(self, url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """One request. 403 (rate limit) and 5xx are retried after a wait; a network error is
        not, because Hunter may already have charged for the answer that was lost."""
        for attempt in range(CONFIG["max_retries"] + 1):
            self.requests_sent += 1
            try:
                response = self.session.get(url, params=params, timeout=CONFIG["timeout_s"])
            except requests.RequestException as err:
                raise HunterError(f"no answer from Hunter ({type(err).__name__}); not repeated") from err
            if response.status_code in (403,) or response.status_code >= 500:
                if attempt < CONFIG["max_retries"]:
                    time.sleep(min(2 ** attempt, CONFIG["max_wait_s"]))
                    continue
                raise HunterError(f"Hunter kept answering HTTP {response.status_code}")
            if response.status_code == 401:
                raise FatalError("Hunter rejected the API key (HTTP 401). Check HUNTER_API_KEY in .env.")
            if response.status_code == 429:
                raise FatalError("Hunter says this month's credits are used up (HTTP 429). The run stops here.")
            if response.status_code == 451:
                raise HunterError("Hunter was asked not to process this person's data (HTTP 451)")
            if not response.ok:
                raise HunterError(f"Hunter answered HTTP {response.status_code}")
            if url == SEARCH_URL:
                used = response.headers.get("X-Credits-Used")
                try:
                    self.credits_used += float(used)
                except (TypeError, ValueError):   # no header: assume the usual cost of a search with results
                    self.cost_is_measured = False
                    found = (response.json().get("data") or {}).get("emails") or []
                    self.credits_used += CONFIG["expected_cost"] if found else 0
            return response.json()
        raise HunterError("Hunter gave no usable answer")

    def credits(self) -> tuple[float, float] | None:
        """(used, available) credits this month, or None if Hunter's answer has another shape."""
        try:
            requests_info = (self.get(ACCOUNT_URL).get("data") or {}).get("requests") or {}
            pool = requests_info.get("credits") or requests_info.get("searches") or {}
            return float(pool["used"]), float(pool["available"])
        except (HunterError, KeyError, TypeError, ValueError):
            return None


def load_accounts(limit: int, companies: list[str]) -> list[dict[str, str]]:
    """The accounts to search, each with its domain and headcount. Raises ValueError for a
    name that is not an account."""
    accounts = pd.read_csv(ACCOUNTS_CSV, dtype=str, keep_default_na=False)
    headcount = {}
    if JOBS_CSV.exists():
        jobs = pd.read_csv(JOBS_CSV, dtype=str, keep_default_na=False)
        headcount = jobs.groupby("company_name")["employee_count"].first().to_dict()
    if companies:
        unknown = [name for name in companies if name not in set(accounts["company_name"])]
        if unknown:
            raise ValueError(f"not in {ACCOUNTS_CSV.name}: {', '.join(unknown)}")
        accounts = accounts.set_index("company_name").loc[companies].reset_index()
    else:
        accounts = accounts.head(limit)
    return [{"company_name": row.company_name, "domain": row.domain.strip(),
             "headcount": headcount.get(row.company_name, "")} for row in accounts.itertuples()]


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    """Read the command line."""
    parser = argparse.ArgumentParser(description="Look up one contact per account through Hunter.")
    parser.add_argument("--dry-run", action="store_true", help="show the plan; send nothing")
    parser.add_argument("--limit", type=int, default=CONFIG["default_limit"],
                        help=f"how many accounts, from the top of accounts.csv (default {CONFIG['default_limit']})")
    parser.add_argument("--companies", default="", help="comma-separated account names, instead of --limit")
    parser.add_argument("--max-credits", type=float, default=CONFIG["default_max_credits"],
                        help=f"stop before a search that could pass this many credits (default {CONFIG['default_max_credits']})")
    args = parser.parse_args(argv)
    if args.limit < 1 or args.max_credits <= 0:
        parser.error("--limit and --max-credits must be above 0")
    return args


def main(argv: list[str] | None = None) -> int:
    """Search Hunter for the chosen accounts and write data/contacts/contacts.csv."""
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if not ACCOUNTS_CSV.exists():
        log.error("%s is missing; run build_accounts.py first.", shown(ACCOUNTS_CSV))
        return 1
    try:
        accounts = load_accounts(args.limit, [name.strip() for name in args.companies.split(",") if name.strip()])
    except (ValueError, KeyError) as err:
        log.error("%s", err)
        return 1
    if args.dry_run:
        for account in accounts:
            target = target_for(account["headcount"])
            log.info("Would search %s (headcount %s) for the %s: first the departments %s, then the whole domain if "
                     "nobody comes back.", account["domain"] or "(no domain)", account["headcount"] or "unknown",
                     target["label"], target["department"])
        log.info("Dry run: nothing sent. %d companies would cost about %d credit(s), one per company with a result; "
                 "the cap is %g. Up to %d people are read per search and one is kept.", len(accounts),
                 len(accounts) * CONFIG["expected_cost"], args.max_credits, CONFIG["ask_for"])
        return 0
    for path in (CONTACTS_CSV, RAW_DIR / "hunter_domain_search_check.json"):
        if not git_ignored(path):
            log.error("git does not ignore %s, so names and emails could be committed. Nothing was sent.", shown(path))
            return 1
    load_dotenv(ROOT / ".env")
    key = os.getenv(KEY_ENV, "").strip()
    if not key:
        log.error("%s is not set in .env; nothing was sent.", KEY_ENV)
        return 1

    client = HunterClient(key)
    before = client.credits()
    rows, stopped, failed = [], "", False
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    for account in accounts:
        now = datetime.now(timezone.utc)
        target = target_for(account["headcount"])
        row = {**dict.fromkeys(COLUMNS, ""), **account, "target": target["label"], "credits_used": 0,
               "searched_at": now.strftime("%Y-%m-%dT%H:%M:%SZ")}
        rows.append(row)
        if not account["domain"]:
            row["result"] = "skipped: no domain"
            continue
        answer = None
        for query, params in search_plan(account["domain"], target):
            saved = read_cache(account["domain"], params, now)
            if saved:
                answer, row["source"] = saved["answer"], row["source"] or "cache"
            else:
                if client.credits_used + CONFIG["expected_cost"] > args.max_credits:
                    row["result"], stopped = "skipped: credit cap", "the credit cap was reached"
                    answer = None
                    break
                spent = client.credits_used
                try:
                    full = trim(client.get(SEARCH_URL, params))
                except FatalError as err:
                    row["result"], stopped, failed, answer = "skipped: run stopped", str(err), True, None
                    log.error("%s", err)
                    break
                except HunterError as err:
                    row["result"], answer = f"skipped: {err}", None
                    log.warning("One company was skipped: %s", err)
                    break
                cost = client.credits_used - spent
                row["credits_used"] += cost
                row["source"] = "hunter"
                kept, _ = choose(full["emails"], target)   # the others are dropped here and never saved
                answer = {"emails": [kept] if kept else [], "returned": len(full["emails"]), "held": full["held"]}
                stamp = now.strftime("%Y-%m-%dT%H%M%SZ")
                (RAW_DIR / f"{cache_file(account['domain'])}{query}_{stamp}.json").write_text(
                    json.dumps({"searched_at": now.isoformat(), "query": query, "params": params, "answer": answer},
                               indent=1, ensure_ascii=False), encoding="utf-8")
                time.sleep(CONFIG["request_interval_s"])
                if cost > CONFIG["expected_cost"]:
                    stopped, failed = (f"one search cost {cost:g} credits, more than the {CONFIG['expected_cost']} "
                                       "expected"), True
            row["query"] = query
            if answer["returned"] or failed:
                break
        if answer is not None:
            chosen, row["result"] = choose(answer["emails"], target)
            row["contacts_returned"] = answer["returned"]
            if chosen:
                row.update({"first_name": chosen.get("first_name") or "", "last_name": chosen.get("last_name") or "",
                            "position": chosen.get("position") or "", "email": chosen.get("value") or "",
                            "confidence": chosen.get("confidence") or "",
                            "verification_status": chosen.get("verification_status") or "",
                            "sources": chosen.get("sources")})
        if failed:
            break

    CONTACTS_CSV.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows, columns=COLUMNS).to_csv(CONTACTS_CSV, index=False)
    after = client.credits()
    found = [row for row in rows if row["result"] in CONFIG["fit_labels"]]
    statuses = [row["verification_status"] or "not given" for row in found]
    # Counts only: no name and no address is ever logged
    log.info("Hunter: %d of %d companies searched (%d from the cache). A contact in the target role: %d. In the "
             "right team but not its lead: %d. In another role: %d. No contact: %d. Skipped: %d.",
             sum(bool(row["source"]) for row in rows), len(accounts), sum(row["source"] == "cache" for row in rows),
             *(sum(row["result"] == label for row in rows) for label in CONFIG["fit_labels"]),
             sum(row["result"] == "none" for row in rows), sum(row["result"].startswith("skipped") for row in rows))
    log.info("Found by the targeted search: %d. Found only by the wide search: %d. Email status of the chosen "
             "contacts, as Hunter reports it: %s.", sum(row["query"] == "targeted" for row in found),
             sum(row["query"] == "wide" for row in found),
             ", ".join(f"{status} {statuses.count(status)}" for status in sorted(set(statuses))) or "none")
    log.info("Hunter credits used by this run: %g (%s). %s", client.credits_used,
             "from Hunter's headers" if client.cost_is_measured else "estimated, no cost header",
             f"This month: {after[0]:g} used of {after[1]:g}." if after else
             "The balance could not be read." + (f" Before the run: {before[0]:g} used." if before else ""))
    if stopped:
        log.warning("The run stopped early: %s.", stopped)
    log.info("One name and email per company is in %s, which git ignores. Nothing was sent to anyone.",
             shown(CONTACTS_CSV))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
