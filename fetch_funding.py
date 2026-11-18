"""Fetch new Form D filings by California issuers from the last 90 days from SEC EDGAR
and write data/funding.csv, one row per filing.

Form D is the notice a company files with the SEC after it starts selling shares in a
private round. It has no round name, so `stage_estimate` is only a rough guess from the
amount sold.

Request safety (SEC allows at most 10 requests a second from a client that names itself):
- Nothing is sent unless SEC_USER_AGENT in .env holds a name and a contact email.
- Requests start at least 0.5 seconds apart. HTTP 429, 5xx and network errors are retried
  with backoff. HTTP 403 means SEC has blocked the client, so the run stops at once.
- Every response is saved to data/raw/. A filing's XML never changes, so it is downloaded
  once and reused, and a stopped run resumes where it left off. Searches are re-run every
  time, because new filings arrive every day.

Usage:
    python fetch_funding.py --limit 5   # test: one search request and 5 filings
    python fetch_funding.py             # every filing in the window
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
import requests
from dotenv import load_dotenv

from bay_area import hq_zip_check

ROOT = Path(__file__).resolve().parent
RAW_DIR = ROOT / "data" / "raw"
XML_DIR = RAW_DIR / "sec_formd"
FUNDING_CSV = ROOT / "data" / "funding.csv"
SEARCH_PREFIX = "sec_search"
SEARCH_URL = "https://efts.sec.gov/LATEST/search-index"
FILING_DIR_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{folder}/"
EDGAR_TZ = ZoneInfo("America/New_York")   # EDGAR dates filings in Eastern time

CONFIG: dict[str, Any] = {
    "window_days": 90,
    "state": "CA",               # state of the issuer's principal office
    "max_hits": 9_900,           # stop above this: paging past 10,000 hits is assumed to fail
    "request_interval_s": 0.5,   # at most 2 requests a second; SEC allows 10
    "max_retries": 5,            # retries after HTTP 429, a 5xx error or a network error
    "max_wait_s": 300,           # stop instead of waiting longer than this
    "timeout_s": 30,
    # Form D industry groups that are not operating startups. Matched after lowercasing
    # and writing "&" as "and". Construction is kept, although Form D lists it under
    # real estate, because construction-tech startups may choose it.
    "excluded_industries": [
        "pooled investment fund", "investing", "commercial banking", "investment banking",
        "insurance", "reits and finance", "residential", "commercial", "other real estate",
    ],
    # stage_estimate from amount sold: under $5M is Seed, under $25M Series A, and so on.
    "stage_cutoffs_usd": [
        (5_000_000, "Seed"),
        (25_000_000, "Series A"),
        (75_000_000, "Series B"),
        (200_000_000, "Series C"),
    ],
    "stage_above_cutoffs": "Later",
    "late_filing_days": 15,      # Form D is due 15 days after the first sale
}
RETRY_STATUS = {429, 500, 502, 503, 504}
ACCESSION = re.compile(r"\d{10}-\d{2}-\d{6}")
EMAIL = re.compile(r"[^@\s]+@([^@\s]+\.[A-Za-z]{2,})")

COLUMNS = [
    "accession_number", "cik", "issuer_name", "filing_date", "first_sale_date",
    "days_first_sale_to_filing", "city", "state", "zip_code", "hq_zip_check",
    "industry_group", "industry_excluded", "entity_type", "year_of_inc",
    "total_offering_usd", "amount_sold_usd", "stage_estimate", "investor_count",
    "related_persons", "filing_url", "fetched_at",
]
WHOLE_NUMBER_COLUMNS = [
    "days_first_sale_to_filing", "total_offering_usd", "amount_sold_usd", "investor_count",
]

log = logging.getLogger("fetch_funding")


class SecError(RuntimeError):
    """SEC kept refusing requests or sent something unexpected, so the run stops."""


class SecBlocked(SecError):
    """HTTP 403: SEC blocks clients that send too many requests or no User-Agent."""


def user_agent_problem(value: str) -> str | None:
    """Why SEC_USER_AGENT can't be sent, or None if it holds a name and an email."""
    match = EMAIL.search(value)
    if not match:
        return "SEC_USER_AGENT must hold a name and a contact email, like 'Jane Doe jane@example.com'."
    if match.group(1).lower().startswith("example."):
        return "SEC_USER_AGENT still holds the example address; use your own name and email."
    if not value.replace(match.group(0), "").strip():
        return "SEC_USER_AGENT needs a name next to the email."
    return None


def retry_wait(retry_after: str, attempt: int) -> int:
    """Seconds before a retry: SEC's Retry-After in seconds, or exponential backoff if longer."""
    asked = int(retry_after) if retry_after.strip().isdigit() else 0
    return max(asked, 5 * 2**attempt)


class SecClient:
    """Sends GET requests to SEC with the declared User-Agent, spaced out and retried."""

    def __init__(self, user_agent: str) -> None:
        self.session = requests.Session()
        self.session.headers["User-Agent"] = user_agent
        self.last_start = -math.inf
        self.sent = 0

    def get(self, url: str, params: dict[str, Any] | None = None) -> requests.Response:
        """GET once, retrying HTTP 429, 5xx and network errors with backoff.

        Any other response is returned to the caller. Raises SecBlocked on HTTP 403 and
        SecError when the retries run out.
        """
        for attempt in range(CONFIG["max_retries"] + 1):
            self._wait_turn()
            try:
                resp = self.session.get(url, params=params, timeout=CONFIG["timeout_s"])
            except requests.RequestException as err:
                problem, retry_after = f"{type(err).__name__} ({err})", ""
            else:
                if resp.status_code == 403:
                    raise SecBlocked(
                        f"HTTP 403 from {url}: SEC has blocked this client. Wait at least "
                        f"10 minutes and check SEC_USER_AGENT. {resp.text[:200]}"
                    )
                if resp.status_code not in RETRY_STATUS:
                    return resp
                problem, retry_after = f"HTTP {resp.status_code}", resp.headers.get("Retry-After", "")
            wait = retry_wait(retry_after, attempt)
            if attempt == CONFIG["max_retries"] or wait > CONFIG["max_wait_s"]:
                raise SecError(f"{problem} from {url} after {attempt + 1} attempt(s); try again later.")
            log.warning("%s from %s; waiting %ds before retry %d.", problem, url, wait, attempt + 1)
            time.sleep(wait)
        raise SecError("Retries exhausted.")

    def _wait_turn(self) -> None:
        """Sleep until request_interval_s has passed since the previous request started."""
        gap = CONFIG["request_interval_s"] - (time.monotonic() - self.last_start)
        if gap > 0:
            time.sleep(gap)
        self.last_start = time.monotonic()
        self.sent += 1


def utc_stamp() -> str:
    """The current UTC time for file names, e.g. 2026-11-15T143000Z."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")


def write_atomic(path: Path, data: bytes) -> None:
    """Write through a temporary file, so a stopped run never leaves half a file behind."""
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_name(path.name + ".part")
    part.write_bytes(data)
    part.replace(path)


def search_params(start: date, end: date, offset: int) -> dict[str, Any]:
    """Query for one page (100 hits) of new Form D filings by California issuers."""
    return {
        "forms": "D",                       # matches the original form, so amendments (D/A) come too
        "dateRange": "custom",
        "startdt": start.isoformat(),
        "enddt": end.isoformat(),
        "locationCodes": CONFIG["state"],   # plural: SEC silently ignores "locationCode"
        "from": offset,
    }


def search_pages(client: SecClient, start: date, end: date) -> Iterator[tuple[int, list[Any]]]:
    """Yield (SEC's total count, hits) for each page of results, saving each page first.

    SEC sorts hits by filing date, newest first, then by accession number. A filing that
    arrives during a run can only repeat a hit on the next page, which is deduplicated;
    it cannot push a hit out of the results.
    """
    offset = 0
    while True:
        params = search_params(start, end, offset)
        resp = client.get(SEARCH_URL, params=params)
        if not resp.ok:
            raise SecError(f"Search failed with HTTP {resp.status_code}: {resp.text[:300]}")
        try:
            data = resp.json()
            hits, count = data["hits"]["hits"], data["hits"]["total"]
            total, exact = int(count["value"]), count.get("relation", "eq") == "eq"
            if not isinstance(hits, list):
                raise TypeError("hits is not a list")
        except (ValueError, KeyError, TypeError, AttributeError) as err:
            raise SecError(f"Unexpected search response ({err}): {resp.text[:300]}") from err
        saved_at = utc_stamp()
        name = f"{SEARCH_PREFIX}_{saved_at}_{params['startdt']}_{params['enddt']}_from{offset:05d}.json"
        record = {"saved_at": saved_at, "url": SEARCH_URL, "params": params, "response": data}
        write_atomic(RAW_DIR / name, json.dumps(record, indent=1).encode("utf-8"))
        if not exact or total > CONFIG["max_hits"]:
            raise SecError(f"Search found {total}{'' if exact else '+'} filings, more than the "
                           f"{CONFIG['max_hits']} that can be paged through; use a smaller --days.")
        yield total, hits
        offset += len(hits)
        if not hits or offset >= total:
            return


def hit_to_filing(hit: Any) -> dict[str, str] | None:
    """Accession number, issuer CIK, filing date, form ("D" or "D/A") and XML file name
    of one search hit, or None if the hit lacks them."""
    try:
        source = hit["_source"]
        accession, _, file_name = str(hit["_id"]).partition(":")
        cik = str(int(source["ciks"][0]))
        filing_date = date.fromisoformat(source["file_date"]).isoformat()
        form = source["form"]
    except (KeyError, IndexError, TypeError, ValueError, AttributeError):
        return None
    if not ACCESSION.fullmatch(accession) or not file_name.endswith(".xml") or form not in ("D", "D/A"):
        return None
    return {"accession_number": accession, "cik": cik, "filing_date": filing_date, "form": form,
            "file_name": file_name}


def collect_filings(
    client: SecClient, start: date, end: date, limit: int | None, cached: dict[str, Path]
) -> tuple[int, list[dict[str, str]], int]:
    """Search the window; return SEC's total count, the unique new notices (newest first)
    and the number of amendments skipped.

    SEC's forms=D filter matches the original form, so amendments (D/A) come back too;
    they are counted and skipped. With a limit, paging stops once `limit` new notices
    without a cached XML have been found.
    """
    filings: dict[str, dict[str, str]] = {}
    amendments: set[str] = set()
    total = 0
    for total, hits in search_pages(client, start, end):
        for hit in hits:
            filing = hit_to_filing(hit)
            if filing is None:
                log.warning("Skipping a search hit without the expected fields: %.300r", hit)
            elif filing["form"] == "D/A":
                amendments.add(filing["accession_number"])
            else:
                filings.setdefault(filing["accession_number"], filing)
        if limit is not None and sum(acc not in cached for acc in filings) >= limit:
            break
    return total, list(filings.values()), len(amendments)


def cached_xmls() -> dict[str, Path]:
    """Downloaded filing XMLs by accession number; the newest file wins if there are two."""
    return {path.stem.rsplit("_", 1)[-1]: path for path in sorted(XML_DIR.glob("*_*.xml"))}


def filing_dir_url(filing: dict[str, str]) -> str:
    """URL of the filing's folder in the EDGAR archive."""
    return FILING_DIR_URL.format(cik=filing["cik"], folder=filing["accession_number"].replace("-", ""))


def download_xml(client: SecClient, filing: dict[str, str]) -> Path | None:
    """Save one filing's XML to data/raw/sec_formd/; None if SEC has no usable XML for it."""
    url = filing_dir_url(filing) + filing["file_name"]
    resp = client.get(url)
    if not resp.ok:
        log.warning("Skipping %s: HTTP %d for %s", filing["accession_number"], resp.status_code, url)
        return None
    if b"<edgarSubmission" not in resp.content[:2000]:
        log.warning("Skipping %s: %s is not a Form D XML", filing["accession_number"], url)
        return None
    path = XML_DIR / f"{utc_stamp()}_{filing['accession_number']}.xml"
    write_atomic(path, resp.content)
    return path


def text(node: ET.Element, path: str) -> str | None:
    """Stripped text of the element at `path` under `node`, or None if missing or empty."""
    found = node.find(path)
    value = (found.text or "").strip() if found is not None else ""
    return value or None


def to_number(value: str | None) -> float | None:
    """A dollar amount or count as a number; None if missing or not a number ("Indefinite")."""
    try:
        number = float(value) if value is not None else math.nan
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def days_between(first: str | None, last: str | None) -> int | None:
    """Days from date `first` to date `last` (YYYY-MM-DD), or None if either is missing."""
    try:
        return (date.fromisoformat(last) - date.fromisoformat(first)).days
    except (TypeError, ValueError):
        return None


def year_of_inc(issuer: ET.Element) -> str | None:
    """The year of incorporation, or Form D's answer when it gives no year."""
    year = issuer.find("yearOfInc")
    if year is None:
        return None
    if text(year, "value"):
        return text(year, "value")
    for tag, answer in (("overFiveYears", "over five years ago"), ("yetToBeFormed", "yet to be formed")):
        if (text(year, tag) or "").lower() == "true":
            return answer
    return None


def related_persons(root: ET.Element) -> str:
    """Each related person as "First Last (Relationship, ... - clarification)", joined by "; "."""
    people = []
    for person in root.findall("relatedPersonsList/relatedPersonInfo"):
        parts = ("firstName", "middleName", "lastName")
        name = " ".join(filter(None, (text(person, f"relatedPersonName/{part}") for part in parts)))
        roles = person.findall("relatedPersonRelationshipList/relationship")
        details = ", ".join(filter(None, ((r.text or "").strip() for r in roles)))
        note = text(person, "relationshipClarification")
        if note:
            details = f"{details} - {note}" if details else note
        people.append(f"{name} ({details})" if details else name)
    return "; ".join(filter(None, people))


def normalize_industry(group: str | None) -> str:
    """Lowercase, with "&" written as "and" and single spaces."""
    return " ".join((group or "").lower().replace("&", " and ").split())


def industry_excluded(group: str | None) -> bool:
    """True if the Form D industry group is in the config's excluded list."""
    return normalize_industry(group) in {normalize_industry(g) for g in CONFIG["excluded_industries"]}


def stage_estimate(amount_sold: float | None) -> str:
    """Rough round stage from the amount sold. Only an estimate: Form D has no round name."""
    if amount_sold is None or amount_sold <= 0:
        return "Unknown"
    for cutoff, stage in CONFIG["stage_cutoffs_usd"]:
        if amount_sold < cutoff:
            return stage
    return CONFIG["stage_above_cutoffs"]


def filing_row(filing: dict[str, str], path: Path) -> dict[str, Any]:
    """One funding.csv row from a search hit and its cached Form D XML."""
    root = ET.fromstring(path.read_bytes())
    issuer, offering = root.find("primaryIssuer"), root.find("offeringData")
    if issuer is None or offering is None:
        raise ValueError("no primaryIssuer or offeringData element")
    first_sale = text(offering, "typeOfFiling/dateOfFirstSale/value")
    zip_code = text(issuer, "issuerAddress/zipCode")
    industry = text(offering, "industryGroup/industryGroupType")
    amount_sold = to_number(text(offering, "offeringSalesAmounts/totalAmountSold"))
    return {
        "accession_number": filing["accession_number"],
        "cik": filing["cik"],
        "issuer_name": text(issuer, "entityName"),
        "filing_date": filing["filing_date"],
        "first_sale_date": first_sale,
        "days_first_sale_to_filing": days_between(first_sale, filing["filing_date"]),
        "city": text(issuer, "issuerAddress/city"),
        "state": text(issuer, "issuerAddress/stateOrCountry"),
        "zip_code": zip_code,
        "hq_zip_check": hq_zip_check(zip_code),
        "industry_group": industry,
        "industry_excluded": industry_excluded(industry),
        "entity_type": text(issuer, "entityType"),
        "year_of_inc": year_of_inc(issuer),
        "total_offering_usd": to_number(text(offering, "offeringSalesAmounts/totalOfferingAmount")),
        "amount_sold_usd": amount_sold,
        "stage_estimate": stage_estimate(amount_sold),
        "investor_count": to_number(text(offering, "investors/totalNumberAlreadyInvested")),
        "related_persons": related_persons(root),
        "filing_url": f"{filing_dir_url(filing)}{filing['accession_number']}-index.htm",
        "fetched_at": path.stem.split("_", 1)[0],
    }


def build_funding_table(filings: list[dict[str, str]], cached: dict[str, Path]) -> tuple[pd.DataFrame, int]:
    """Rows for the filings whose XML is cached, newest first, and the number of unreadable XMLs."""
    rows, unreadable = [], 0
    for filing in filings:
        path = cached.get(filing["accession_number"])
        if path is None:
            continue
        try:
            rows.append(filing_row(filing, path))
        except (ET.ParseError, ValueError, OSError) as err:
            unreadable += 1
            log.warning("Skipping unreadable XML %s: %s", path.name, err)
    table = pd.DataFrame(rows, columns=COLUMNS)
    for column in WHOLE_NUMBER_COLUMNS:
        table[column] = pd.to_numeric(table[column]).round().astype("Int64")
    return table.sort_values(["filing_date", "accession_number"], ascending=False), unreadable


def log_summary(table: pd.DataFrame) -> None:
    """Print the counts that show the funnel and help spot bad data."""
    if table.empty:
        return
    log.info("HQ ZIP check: %s", table["hq_zip_check"].value_counts().to_dict())
    log.info("Industry groups: %s", table["industry_group"].value_counts(dropna=False).to_dict())
    kept = table[(table["hq_zip_check"] == "bay_area") & ~table["industry_excluded"].astype(bool)]
    log.info("Bay Area and not an excluded industry: %d filings. Their stage estimates: %s",
             len(kept), kept["stage_estimate"].value_counts().to_dict())
    late = int((table["days_first_sale_to_filing"] > CONFIG["late_filing_days"]).sum())
    log.info("Filed more than %d days after the first sale: %d. No first-sale date given: %d.",
             CONFIG["late_filing_days"], late, int(table["first_sale_date"].isna().sum()))


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    """Read and check the command line."""
    parser = argparse.ArgumentParser(
        description="Fetch recent Form D filings by California issuers from SEC EDGAR.")
    parser.add_argument("--limit", type=int, help="download at most N new filings this run (default: all)")
    parser.add_argument("--days", type=int, default=CONFIG["window_days"],
                        help="filings from the last N days (default %(default)s)")
    args = parser.parse_args(argv)
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be 1 or more")
    if args.days < 1:
        parser.error("--days must be 1 or more")
    return args


def main(argv: list[str] | None = None) -> int:
    """Search the window, download the XMLs not cached yet, then rebuild data/funding.csv."""
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    load_dotenv(ROOT / ".env")
    user_agent = os.getenv("SEC_USER_AGENT", "").strip()
    problem = user_agent_problem(user_agent)
    if problem:
        log.error("%s Set it in .env (see .env.example). No request was sent.", problem)
        return 1

    began = time.monotonic()
    end = datetime.now(EDGAR_TZ).date()
    start = end - timedelta(days=args.days)
    client = SecClient(user_agent)
    cached = cached_xmls()
    try:
        total, filings, amendments = collect_filings(client, start, end, args.limit, cached)
    except SecError as err:
        log.error("Search stopped: %s", err)
        return 1
    log.info("SEC search: %d Form D and D/A filings by %s issuers, filed %s to %s (Eastern time).",
             total, CONFIG["state"], start, end)
    log.info("On the pages read: %d new notices (Form D) kept, %d amendments (D/A) skipped.",
             len(filings), amendments)

    missing = [f for f in filings if f["accession_number"] not in cached]
    to_fetch = missing if args.limit is None else missing[: args.limit]
    seconds = len(to_fetch) * CONFIG["request_interval_s"]
    log.info("Already downloaded: %d. Downloading %d now (at least %s).",
             len(filings) - len(missing), len(to_fetch),
             f"{seconds / 60:.0f} min" if seconds >= 60 else f"{seconds:.0f} s")
    failed, skipped = False, 0
    try:
        for n, filing in enumerate(to_fetch, 1):
            if download_xml(client, filing) is None:
                skipped += 1
            if n % 100 == 0:
                minutes = (time.monotonic() - began) / 60
                log.info("Downloaded %d of %d (%.1f min so far).", n, len(to_fetch), minutes)
    except SecError as err:
        log.error("Downloads stopped: %s", err)
        log.info("Files already downloaded are kept; run again to resume.")
        failed = True

    cached = cached_xmls()
    table, unreadable = build_funding_table(filings, cached)
    FUNDING_CSV.parent.mkdir(parents=True, exist_ok=True)
    table.to_csv(FUNDING_CSV, index=False)
    not_cached = sum(f["accession_number"] not in cached for f in filings)
    shown = FUNDING_CSV.relative_to(ROOT) if FUNDING_CSV.is_relative_to(ROOT) else FUNDING_CSV
    log.info("Wrote %d filings to %s. Skipped this run: %d. Unreadable XML: %d. Not downloaded yet: %d.",
             len(table), shown, skipped, unreadable, not_cached - skipped)
    log_summary(table)
    log.info("Requests sent: %d in %.1f min.", client.sent, (time.monotonic() - began) / 60)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
