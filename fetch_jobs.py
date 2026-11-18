"""Fetch recent AI, ML, forward deployed and robotics engineer jobs at SF Bay Area
companies from the TheirStack Jobs API and write data/jobs.csv.

Credit safety (TheirStack charges 1 credit per job returned, every time):
- Each API response is saved to data/raw/ before it is used. Running again with the
  same parameters on the same UTC day reads those files and spends no credits.
- Jobs already received are sent in `job_id_not`, so no job is paid for twice.
- --limit is required and caps the new jobs (credits) one run may fetch.

With --funded-companies the search covers only the companies whose Form D filings
can become accounts in step 3 (see build_accounts.py), so step 2 runs first. Adding
--any-hq-city drops the HQ city filter, since the Form D address already places HQ;
step 3 then sends every match whose TheirStack city is not a Bay Area city to review.

Hiring-first search: --core-roles keeps only the titles a company without a Form D filing
needs to become an account (forward deployed and applied AI engineers), and
--one-per-company asks for one job at a time and leaves out every company already
received, so each credit buys one new company. Companies the reviewer rejected in
data/manual/review_decisions.csv are left out of every search.

Usage:
    python fetch_jobs.py --limit 5 --dry-run   # print the request, spend nothing
    python fetch_jobs.py --limit 5             # test run, at most 5 credits
    python fetch_jobs.py --funded-companies --days 30 --limit 1   # count jobs at funded companies
    python fetch_jobs.py --funded-companies --any-hq-city --days 30 --limit 1
    python fetch_jobs.py --core-roles --one-per-company --days 14 --limit 1   # count hiring companies
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import sys
import time
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import pandas as pd
import requests
from dotenv import load_dotenv

import build_accounts
from bay_area import BAY_AREA_CITY_PATTERNS, hq_zip_check

ROOT = Path(__file__).resolve().parent
RAW_DIR = ROOT / "data" / "raw"
JOBS_CSV = ROOT / "data" / "jobs.csv"
RAW_PREFIX = "theirstack_jobs"
SEARCH_URL = "https://api.theirstack.com/v1/jobs/search"
BALANCE_URL = "https://api.theirstack.com/v0/billing/credit-balance"

CONFIG: dict[str, Any] = {
    # Each entry matches titles that contain all of its words, in any order,
    # so "ai engineer" also covers "Applied AI Engineer".
    "title_keywords": [
        "ai engineer",
        "ml engineer",
        "machine learning engineer",
        "forward deployed engineer",
        "robotics engineer",
    ],
    # --core-roles: the titles that make a company an account without a Form D filing
    "core_title_keywords": ["forward deployed engineer", "applied ai engineer"],
    "title_exclude": ["intern", "internship", "sales", "recruiter"],
    # Company HQ city, from the shared list in bay_area.py. TheirStack matches the city
    # name only, not the state, so every row is also checked against the Bay Area ZIP prefixes.
    "hq_cities": list(BAY_AREA_CITY_PATTERNS),
    "max_employees": 500,       # rough Seed to Series C proxy; unknown size is kept
    "posted_max_age_days": 14,  # default window; weekly runs pass --days 7
    "page_size": 25,            # free plan maximum per page
    "max_jobs_per_run": 125,    # free plan: 5 pages of 25 per search
    "request_interval_s": 7,    # free plan allows 10 searches a minute
    "max_retries": 5,           # retries after HTTP 429
    "max_wait_s": 300,          # stop instead of waiting longer than this
    "description_max_chars": 300,  # company description kept in jobs.csv
    "long_description_max_chars": 1000,  # start of the long description, for step 3's review hints
}

# Checked in order; the first match wins. Titles without "engineer" are "other".
ROLE_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("fde", re.compile(r"\bforward[\s-]+deployed\b", re.I)),
    ("robotics", re.compile(r"\brobotics\b", re.I)),
    ("applied_ai", re.compile(r"\bapplied\s+(ai|ml|machine\s+learning)\b", re.I)),
    ("ml", re.compile(r"\b(ml|machine\s+learning)\b", re.I)),
    ("ai", re.compile(r"\b(ai|genai|llm|artificial\s+intelligence)\b", re.I)),
]
ENGINEER = re.compile(r"\bengineer", re.I)

COLUMNS = [
    "job_id", "job_title", "role_category", "is_fde",
    "company_name", "company_domain", "company_city", "company_postal_code", "hq_zip_check",
    "company_country_code", "employee_count",
    "theirstack_funding_stage", "theirstack_last_funding_date", "company_linkedin_url",
    "theirstack_jobs_last_30_days", "company_industry", "company_description", "company_long_description",
    "job_location", "remote", "salary_min_usd", "salary_max_usd",
    "date_posted", "discovered_at", "job_url", "posting_url", "source_url",
    "theirstack_company_id", "fetched_at",
]

log = logging.getLogger("fetch_jobs")


class ApiError(RuntimeError):
    """The API refused a request, or continuing would waste credits."""


def classify_role(title: str) -> str:
    """Return the role category of a job title, or "other" if it is not a target role."""
    if not ENGINEER.search(title):
        return "other"
    for category, pattern in ROLE_PATTERNS:
        if pattern.search(title):
            return category
    return "other"


def base_body(days: int, funded_since: str | None, company_names: list[str] | None = None,
              any_hq_city: bool = False, core_roles: bool = False,
              excluded_names: list[str] | None = None) -> dict[str, Any]:
    """Filters shared by every request of a run (no paging or exclusion fields)."""
    body: dict[str, Any] = {
        "posted_at_max_age_days": days,
        "job_title_or": CONFIG["core_title_keywords" if core_roles else "title_keywords"],
        "job_title_not": CONFIG["title_exclude"],
        "job_country_code_or": ["US"],
        "company_country_code_or": ["US"],
        "company_location_pattern_or": CONFIG["hq_cities"],
        "max_employee_count_or_null": CONFIG["max_employees"],
        "company_type": "direct_employer",
    }
    if funded_since:
        body["last_funding_round_date_gte"] = funded_since
    if company_names:
        # Exact company names, any case: only these companies' jobs are returned (and paid for)
        body["company_name_case_insensitive_or"] = company_names
    if any_hq_city:
        del body["company_location_pattern_or"]  # the Form D address places HQ instead
    if excluded_names:
        body["company_name_not"] = sorted(excluded_names)  # exact names the reviewer rejected
    return body


def run_key(base: dict[str, Any], limit: int, one_per_company: bool = False) -> str:
    """Short hash of a run's parameters; the same parameters on the same day reuse the cache."""
    params: dict[str, Any] = {"filters": base, "limit": limit}
    if one_per_company:
        params["one_per_company"] = True
    blob = json.dumps(params, sort_keys=True)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:10]


def save_raw(key: str, page_no: int, body: dict[str, Any], response: dict[str, Any]) -> Path:
    """Save one API response to data/raw/ before it is used, with a UTC timestamp in the name."""
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    saved_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")
    path = RAW_DIR / f"{RAW_PREFIX}_{saved_at}_{key}_p{page_no:02d}.json"
    record = {"saved_at": saved_at, "request_body": body, "response": response}
    path.write_text(json.dumps(record, indent=1), encoding="utf-8")
    return path


def raw_pages(pattern: str = "*") -> list[dict[str, Any]]:
    """Cached job responses whose file name matches `pattern`, oldest first."""
    pages = []
    for path in sorted(RAW_DIR.glob(f"{RAW_PREFIX}_{pattern}.json")):
        try:
            pages.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError) as err:
            log.warning("Skipping unreadable cache file %s: %s", path.name, err)
    return pages


def page_jobs(page: dict[str, Any]) -> list[Any]:
    """The job records inside one cached response."""
    return (page.get("response") or {}).get("data") or []


def page_limit(page: dict[str, Any]) -> int:
    """How many jobs the cached request asked for."""
    return (page.get("request_body") or {}).get("limit", 0)


def iter_raw_jobs() -> Iterator[tuple[dict[str, Any], str]]:
    """Yield (job, saved_at) for every job in every cached response."""
    for page in raw_pages():
        for job in page_jobs(page):
            if isinstance(job, dict):
                yield job, page.get("saved_at", "")
            else:
                log.warning("Skipping a job record that is not an object: %r", job)


def known_job_ids(days: int) -> set[int]:
    """IDs of jobs already received that could match this posted-date window again."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days + 1)).date().isoformat()
    return {
        job["id"]
        for job, _ in iter_raw_jobs()
        if "id" in job and str(job.get("date_posted") or "9999")[:10] >= cutoff
    }


def known_company_ids() -> set[str]:
    """TheirStack ids of every company with a job already received."""
    ids = {(job.get("company_object") or {}).get("id") for job, _ in iter_raw_jobs()}
    return {str(company_id) for company_id in ids if company_id}


def retry_wait(reset_header: str, attempt: int) -> int:
    """Seconds to wait before a retry: the reset time, or exponential backoff if longer."""
    resets = [int(n) for n in re.findall(r"\d+", reset_header)]
    return max(max(resets, default=0), 5 * 2**attempt)


def post_search(session: requests.Session, body: dict[str, Any]) -> dict[str, Any]:
    """POST one search; on HTTP 429 wait until the limit resets, then retry."""
    for attempt in range(CONFIG["max_retries"] + 1):
        resp = session.post(SEARCH_URL, json=body, timeout=120)
        if resp.status_code != 429:
            if not resp.ok:
                raise ApiError(f"HTTP {resp.status_code}: {resp.text[:500]}")
            return resp.json()
        wait = retry_wait(resp.headers.get("RateLimit-Reset", ""), attempt)
        if attempt == CONFIG["max_retries"] or wait > CONFIG["max_wait_s"]:
            raise ApiError(f"Rate limited (limit resets in {wait}s); try later. {resp.text[:300]}")
        log.warning("HTTP 429, waiting %ds before retry %d: %s", wait, attempt + 1, resp.text[:200])
        time.sleep(wait)
    raise ApiError("Rate limited after all retries.")


def fetch_new_jobs(
    session: requests.Session,
    base: dict[str, Any],
    key: str,
    wanted: int,
    known: set[int],
    start_page: int = 0,
    known_companies: set[str] | None = None,
) -> int:
    """Fetch up to `wanted` jobs not in `known`, saving each response before using it.

    Every request asks for page 0 and excludes the jobs already received instead of
    paging, so a job is never paid for twice. With `known_companies` (a set, possibly
    empty), each request asks for one job and excludes those companies, then adds the
    company it received: one credit per new company. Returns the number of jobs received.
    """
    received, page_no = 0, start_page
    while received < wanted:
        if page_no > start_page:
            time.sleep(CONFIG["request_interval_s"])
        body = {
            **base,
            "page": 0,
            "limit": min(CONFIG["page_size"], wanted - received),
            "job_id_not": sorted(known),
            "include_total_results": page_no == 0,
        }
        if known_companies is not None:
            body["limit"] = 1
            body["company_id_not"] = sorted(known_companies)
        response = post_search(session, body)
        save_raw(key, page_no, body, response)
        jobs = [job for job in response.get("data") or [] if isinstance(job, dict)]
        repeats = {job.get("id") for job in jobs} & known
        if repeats:
            raise ApiError(f"API returned {len(repeats)} job(s) already received; stopping.")
        known.update(job["id"] for job in jobs if "id" in job)
        if known_companies is not None:
            known_companies.update(str(company_id) for company_id in
                                   ((job.get("company_object") or {}).get("id") for job in jobs) if company_id)
        received += len(jobs)
        page_no += 1
        if len(jobs) < body["limit"]:
            break  # no more matching jobs
    return received


def credit_balance(session: requests.Session) -> dict[str, Any] | None:
    """Read the team's credit balance (the response holds no job records)."""
    try:
        resp = session.get(BALANCE_URL, timeout=30)
        resp.raise_for_status()
        return resp.json()
    except (requests.RequestException, ValueError) as err:
        log.warning("Could not read the credit balance: %s", err)
        return None


def report_credits(before: dict[str, Any] | None, after: dict[str, Any] | None, received: int) -> None:
    """Print the credits this run used, from the balance read before and after, next to the
    expected cost. `api_credits` is the billing cycle's allowance, not what is left."""
    log.info("Jobs received this run: %d, so the expected cost is %d credits.", received, received)
    try:
        used = after["used_api_credits"] - before["used_api_credits"]
        left = after["api_credits"] - after["used_api_credits"]
    except (KeyError, TypeError):
        log.warning("Credit balance unavailable; check the TheirStack dashboard.")
        return
    log.info("API credits used by this run: %s. Left this billing cycle: %s of %s.",
             used, left, after["api_credits"])
    if used != received:
        log.warning("Credits used (%s) differ from jobs received (%d); check the TheirStack dashboard.",
                    used, received)


def posting_url(url: str | None) -> str:
    """A job URL without its query string or fragment, so copies of one posting found on
    different sites (the company's careers page, Indeed) share it."""
    if not url:
        return ""
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""))


def short_description(company: dict[str, Any]) -> str:
    """The company's own one-line description, or the start of the long one."""
    text = " ".join(str(company.get("seo_description") or company.get("long_description") or "").split())
    limit = CONFIG["description_max_chars"]
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def job_to_row(job: dict[str, Any], fetched_at: str) -> dict[str, Any]:
    """Flatten one TheirStack job into a jobs.csv row."""
    company = job.get("company_object") or {}
    title = job.get("job_title") or ""
    role = classify_role(title)
    postal_code = company.get("postal_code")
    return {
        "job_id": job["id"],
        "job_title": title,
        "role_category": role,
        "is_fde": role == "fde",
        "company_name": company.get("name") or job.get("company"),
        "company_domain": company.get("domain") or job.get("company_domain"),
        "company_city": company.get("city"),
        "company_postal_code": postal_code,
        "hq_zip_check": hq_zip_check(postal_code),
        "company_country_code": company.get("country_code"),
        "employee_count": company.get("employee_count"),
        "theirstack_funding_stage": company.get("funding_stage"),
        "theirstack_last_funding_date": company.get("last_funding_round_date"),
        "company_linkedin_url": company.get("linkedin_url"),
        "theirstack_jobs_last_30_days": company.get("num_jobs_last_30_days"),
        "company_industry": company.get("industry"),
        "company_description": short_description(company),
        "company_long_description": " ".join(str(company.get("long_description") or "").split())[
            : CONFIG["long_description_max_chars"]],
        "job_location": job.get("short_location") or job.get("location"),
        "remote": job.get("remote"),
        "salary_min_usd": job.get("min_annual_salary_usd"),
        "salary_max_usd": job.get("max_annual_salary_usd"),
        "date_posted": job.get("date_posted"),
        "discovered_at": job.get("discovered_at"),
        "job_url": job.get("final_url") or job.get("url"),
        "posting_url": posting_url(job.get("final_url") or job.get("url")),
        "source_url": job.get("source_url"),
        "theirstack_company_id": company.get("id"),
        "fetched_at": fetched_at,
    }


def build_jobs_table() -> pd.DataFrame:
    """Rebuild the jobs table from every cached response, one row per job id."""
    rows = []
    for job, fetched_at in iter_raw_jobs():
        try:
            rows.append(job_to_row(job, fetched_at))
        except (KeyError, TypeError, AttributeError) as err:
            log.warning("Skipping a bad job record (%s: %s)", type(err).__name__, err)
    table = pd.DataFrame(rows, columns=COLUMNS)
    table = table.sort_values("fetched_at").drop_duplicates("job_id", keep="last")
    return table.sort_values(["date_posted", "job_id"], ascending=False, na_position="last")


def log_summary(table: pd.DataFrame) -> None:
    """Print counts that help spot bad data."""
    shown = JOBS_CSV.relative_to(ROOT) if JOBS_CSV.is_relative_to(ROOT) else JOBS_CSV
    log.info("Wrote %d jobs at %d companies to %s", len(table), table["company_name"].nunique(), shown)
    if len(table):
        log.info("Role categories: %s", table["role_category"].value_counts().to_dict())
        log.info("HQ ZIP check: %s", table["hq_zip_check"].value_counts().to_dict())


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    """Read and check the command line."""
    parser = argparse.ArgumentParser(description="Fetch Bay Area AI engineering jobs from TheirStack.")
    parser.add_argument("--limit", type=int, required=True,
                        help="max new jobs to fetch this run, 1 credit each "
                             f"(1 to {CONFIG['max_jobs_per_run']})")
    parser.add_argument("--days", type=int, default=CONFIG["posted_max_age_days"],
                        help="only jobs posted in the last N days (default %(default)s)")
    parser.add_argument("--funded-since-days", type=int,
                        help="also require TheirStack's last funding round in the last N days")
    parser.add_argument("--funded-companies", action="store_true",
                        help="search only companies whose Form D filings can become accounts (step 2 first)")
    parser.add_argument("--any-hq-city", action="store_true",
                        help="with --funded-companies: drop the HQ city filter (Form D places HQ)")
    parser.add_argument("--core-roles", action="store_true",
                        help="search only forward deployed and applied AI engineer titles")
    parser.add_argument("--one-per-company", action="store_true",
                        help="one job per request, leaving out companies already received: "
                             "1 credit per new company")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the request and exit without calling the API")
    args = parser.parse_args(argv)
    if not 1 <= args.limit <= CONFIG["max_jobs_per_run"]:
        parser.error(f"--limit must be between 1 and {CONFIG['max_jobs_per_run']}")
    if args.days < 0 or (args.funded_since_days is not None and args.funded_since_days < 0):
        parser.error("--days and --funded-since-days must be 0 or more")
    if args.any_hq_city and not args.funded_companies:
        parser.error("--any-hq-city only works with --funded-companies")
    return args


def main(argv: list[str] | None = None) -> int:
    """Reuse today's cache or call the API, then rebuild data/jobs.csv."""
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    today = datetime.now(timezone.utc).date()
    funded_since = None
    if args.funded_since_days is not None:
        funded_since = (today - timedelta(days=args.funded_since_days)).isoformat()
    try:
        excluded_names = build_accounts.rejected_company_names()
    except ValueError as err:
        log.error("%s", err)
        return 1
    if excluded_names:
        log.info("Leaving out %d company name(s) the reviewer rejected.", len(excluded_names))
    company_names = None
    if args.funded_companies:
        try:
            company_names, companies = build_accounts.search_list()
        except FileNotFoundError:
            log.error("data/funding.csv is missing; run fetch_funding.py first.")
            return 1
        if not company_names:
            log.error("No Form D company passes step 3's rules, so there is nothing to search.")
            return 1
        log.info("Searching only %d Form D companies, with %d spellings of their names.",
                 companies, len(company_names))
    base = base_body(args.days, funded_since, company_names, args.any_hq_city, args.core_roles, excluded_names)
    key = run_key(base, args.limit, args.one_per_company)
    todays_pattern = f"{today.isoformat()}T*_{key}_p*"

    pages = raw_pages(todays_pattern)
    cached = sum(len(page_jobs(p)) for p in pages)
    exhausted = any(len(page_jobs(p)) < page_limit(p) for p in pages)
    wanted = 0 if exhausted else max(args.limit - cached, 0)
    known = known_job_ids(args.days)
    known_companies = known_company_ids() if args.one_per_company else None

    if args.dry_run:
        preview = {
            **base,
            "page": 0,
            "limit": min(CONFIG["page_size"], max(wanted, 1)),
            "job_id_not": f"<{len(known)} job ids already received>",
            "include_total_results": not pages,
        }
        if company_names:
            preview["company_name_case_insensitive_or"] = (
                f"<{len(company_names)} names, e.g. {', '.join(company_names[:5])}>")
        if known_companies is not None:
            preview["limit"] = 1
            preview["company_id_not"] = f"<{len(known_companies)} companies already received>"
        print(json.dumps(preview, indent=2))
        log.info("Dry run: nothing sent, 0 credits. Would fetch up to %d new jobs "
                 "(%d already cached today for these parameters).", wanted, cached)
        return 0

    failed = False
    if wanted == 0:
        log.info("Reusing %d cached response(s) from today (%d jobs); no credits spent.", len(pages), cached)
    else:
        load_dotenv(ROOT / ".env")
        api_key = os.getenv("THEIRSTACK_API_KEY", "").strip()
        if not api_key:
            log.error("THEIRSTACK_API_KEY is not set. Copy .env.example to .env and add your key.")
            return 1
        session = requests.Session()
        session.headers["Authorization"] = f"Bearer {api_key}"
        before = credit_balance(session)
        try:
            fetch_new_jobs(session, base, key, wanted, known, start_page=len(pages),
                           known_companies=known_companies)
        except (ApiError, requests.RequestException) as err:
            log.error("Search stopped: %s", err)
            failed = True
        pages = raw_pages(todays_pattern)
        received = sum(len(page_jobs(p)) for p in pages) - cached
        time.sleep(2)  # give the balance a moment to update
        report_credits(before, credit_balance(session), received)

    totals = next(((p.get("response") or {}).get("metadata") or {} for p in pages
                   if (p.get("request_body") or {}).get("include_total_results")), {})
    if totals:
        log.info("Matching jobs not yet received when this run started: %s at %s companies. "
                 "Fetching all of them would cost that many credits.",
                 totals.get("total_results"), totals.get("total_companies"))

    table = build_jobs_table()
    JOBS_CSV.parent.mkdir(parents=True, exist_ok=True)
    table.to_csv(JOBS_CSV, index=False)
    log_summary(table)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
