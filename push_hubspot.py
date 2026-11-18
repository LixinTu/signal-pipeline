"""Create or update the scored accounts as companies in HubSpot (step 4).

Reads data/accounts.csv (step 3). For each row it searches HubSpot for a company with the
same domain and updates it, or creates one if there is none, so running again creates no
duplicates. The score, the reasons for it and the doubts the rules had (signal_flags) go
into custom company properties named signal_*, which the script creates if they are missing.

`signal_stage` is the reviewer's working status (new, reviewed, ready_to_contact, contacted,
rejected). It is set to "new" only when a company is created. Later runs never change it,
and they never change a company's name or domain either, so edits made in HubSpot survive.

HubSpot's search can take a few moments to list a new company. The ids of the companies
this script creates are therefore kept in data/raw/hubspot_company_ids.json and checked
when the search finds nothing, so a second run right after the first creates nothing.

Request safety:
- Nothing is sent without HUBSPOT_SERVICE_KEY in .env; --dry-run needs no key.
- Requests start at least 0.5 seconds apart, far below HubSpot's limits (100 requests per
  10 seconds on a free account, 5 searches a second). HTTP 429 is retried after a wait.
- A create is never sent twice: if HubSpot gives no clear answer, the row fails instead.
- A row HubSpot rejects is logged and skipped; the run goes on and exits with code 1.
- Every request and response of a run is saved to data/raw/, without the key.

The service key needs four scopes: crm.objects.companies.read, crm.objects.companies.write,
crm.schemas.companies.read and crm.schemas.companies.write.

Usage:
    python push_hubspot.py --dry-run     # print what would be sent; send nothing
    python push_hubspot.py --limit 5     # first test: the 5 top-scored accounts
    python push_hubspot.py               # every account
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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
ACCOUNTS_CSV = ROOT / "data" / "accounts.csv"
RAW_DIR = ROOT / "data" / "raw"
RAW_PREFIX = "hubspot_push"
# Domain -> id of each company this script created or updated, for when HubSpot's search lags
IDS_JSON = RAW_DIR / "hubspot_company_ids.json"
KEY_ENV = "HUBSPOT_SERVICE_KEY"
API_BASE = "https://api.hubapi.com"
COMPANIES_URL = f"{API_BASE}/crm/v3/objects/companies"
SEARCH_URL = f"{COMPANIES_URL}/search"
PROPERTIES_URL = f"{API_BASE}/crm/v3/properties/companies"

CONFIG: dict[str, Any] = {
    "request_interval_s": 0.5,   # at most 2 requests a second; HubSpot allows 5 searches a second
    "max_retries": 5,            # after HTTP 429, or a 5xx or network error on a request safe to repeat
    "max_wait_s": 60,            # stop instead of waiting longer than this
    "timeout_s": 30,
    "search_limit": 10,          # companies read per domain; more than one is a duplicate in HubSpot
    "stage_on_create": "new",    # signal_stage of a company this script creates
    "property_group": "companyinformation",  # used only if HubSpot's own "name" property names no group
    "max_text_chars": 60_000,    # HubSpot limits a text value to 65,536 characters
}

# The scope each kind of request needs, to name it when HubSpot refuses one (HTTP 403).
SCOPES: dict[str, str] = {
    "list_properties": "crm.schemas.companies.read",
    "create_property": "crm.schemas.companies.write",
    "search": "crm.objects.companies.read",
    "read": "crm.objects.companies.read",
    "create": "crm.objects.companies.write",
    "update": "crm.objects.companies.write",
}
ACTION_TEXT: dict[str, str] = {
    "list_properties": "list the company properties",
    "create_property": "create a company property",
    "search": "search companies by domain",
    "read": "read a company",
    "create": "create a company",
    "update": "update a company",
}

STAGES = ["new", "reviewed", "ready_to_contact", "contacted", "rejected"]
# Company properties this script owns. The group name is added when they are created.
PROPERTIES: list[dict[str, Any]] = [
    {"name": "signal_score", "label": "Signal score", "type": "number", "fieldType": "number",
     "description": "Account score from the signal pipeline (funding recency, open roles)."},
    {"name": "signal_stage", "label": "Signal stage", "type": "enumeration", "fieldType": "select",
     "description": "Working status, set by hand. The pipeline sets it to New only on create.",
     "options": [{"label": stage.replace("_", " ").capitalize(), "value": stage, "displayOrder": order,
                  "hidden": False} for order, stage in enumerate(STAGES)]},
    {"name": "signal_tier", "label": "Signal tier", "type": "string", "fieldType": "text",
     "description": "Which signals the account has, from the signal pipeline."},
    {"name": "signal_reason", "label": "Signal reason", "type": "string", "fieldType": "textarea",
     "description": "The funding facts and every part of the score, one per line."},
    {"name": "signal_flags", "label": "Signal flags", "type": "string", "fieldType": "textarea",
     "description": "Doubts the pipeline's rules had about this account, one per line. Empty when it has none."},
    {"name": "signal_roles", "label": "Signal roles", "type": "string", "fieldType": "textarea",
     "description": "Relevant open roles, one per line, and where they are posted."},
    {"name": "signal_sources", "label": "Signal sources", "type": "string", "fieldType": "textarea",
     "description": "Links to the SEC filings and the job posts behind the score, one per line."},
    {"name": "signal_last_seen", "label": "Signal last seen", "type": "date", "fieldType": "date",
     "description": "Date (UTC) of the last pipeline run that included this account."},
]

REQUIRED_COLUMNS = [
    "company_name", "domain", "hq_location", "filing_date", "amount_sold_usd", "role_titles",
    "fde_opening", "job_locations", "score", "score_reason", "source_urls",
]
OPTIONAL_COLUMNS = ["signal_tier", "flags"]   # missing in files written before tiers and flags existed
SCHEME = re.compile(r"^[a-z][a-z0-9+.\-]*://")
DOMAIN = re.compile(r"[a-z0-9\-]+(\.[a-z0-9\-]+)+")
SCOPE_NAME = re.compile(r"[a-z][a-z0-9_\-]*(\.[a-z0-9_\-]+)+")

log = logging.getLogger("push_hubspot")


class HubSpotError(RuntimeError):
    """HubSpot refused one request; the row is skipped and the run goes on."""


class FatalError(HubSpotError):
    """Nothing more can be sent in this run: a missing scope, a rejected key, the daily
    limit, or HubSpot failing after every retry."""


def utc_stamp() -> str:
    """The current UTC time for file names, e.g. 2026-11-16T203000Z."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")


def write_atomic(path: Path, text: str) -> None:
    """Write through a temporary file, so a stopped run never leaves half a file behind."""
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_name(path.name + ".part")
    part.write_text(text, encoding="utf-8")
    part.replace(path)


def normalise_domain(value: Any) -> str:
    """A bare lowercase domain ("https://www.Example.com/jobs" -> "example.com"), or "" if
    the value does not hold one."""
    text = SCHEME.sub("", str(value or "").strip().lower())
    text = re.split(r"[/?#:\s]", text, maxsplit=1)[0]
    text = text.removeprefix("www.").strip(".")
    return text if DOMAIN.fullmatch(text) else ""


def number(value: Any) -> float:
    """A header or cell as a non-negative number; 0 if it is missing or not a number."""
    try:
        found = float(str(value).strip())
    except ValueError:
        return 0.0
    return found if math.isfinite(found) and found > 0 else 0.0


def retry_wait(headers: Any, policy: str, attempt: int) -> float:
    """Seconds to wait after HTTP 429: Retry-After if HubSpot sends it, else the length of
    the rate-limit window it names, else exponential backoff; whichever is longest."""
    asked = number(headers.get("Retry-After"))
    window = number(headers.get("X-HubSpot-RateLimit-Interval-Milliseconds")) / 1000
    if not window and policy.upper() == "TEN_SECONDLY_ROLLING":
        window = 10.0
    return max(asked, window, float(2 ** attempt))


def missing_scopes(data: Any) -> list[str]:
    """Scope names listed anywhere in a HubSpot error body, under a key that mentions scopes."""
    found: list[str] = []

    def walk(node: Any, in_scopes: bool) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                walk(value, in_scopes or "scope" in str(key).lower())
        elif isinstance(node, list):
            for item in node:
                walk(item, in_scopes)
        elif in_scopes and isinstance(node, str) and SCOPE_NAME.fullmatch(node):
            found.append(node)

    walk(data, False)
    return list(dict.fromkeys(found))


def property_list() -> str:
    """The properties this script needs, for the reviewer to create by hand if they prefer."""
    return "; ".join(f"{spec['name']} ({spec['fieldType']})" for spec in PROPERTIES)


def scope_message(action: str, data: Any) -> str:
    """What to tell the reviewer when HubSpot refuses a request with HTTP 403."""
    scope = SCOPES[action]
    said = data.get("message", "") if isinstance(data, dict) else str(data)[:300]
    listed = [name for name in missing_scopes(data) if name != scope]
    text = (f"HubSpot refused to {ACTION_TEXT[action]} (HTTP 403). The service key needs the scope "
            f"{scope}: in HubSpot open Development > Keys > Service keys, open the key, click Edit, "
            f"add the scope and run again.")
    if action == "create_property":
        text += (" Or create the company properties by hand in HubSpot (Settings > Properties > "
                 f"Company properties), with these internal names: {property_list()}; signal_stage is a "
                 f"dropdown with the option values {', '.join(STAGES)}.")
    if listed and scope not in missing_scopes(data):
        text += f" HubSpot's answer names: {', '.join(listed[:5])}."
    return f"{text} HubSpot said: {said}" if said else text


class HubSpotClient:
    """Sends spaced-out requests to HubSpot and keeps a record of each one, without the key."""

    def __init__(self, api_key: str) -> None:
        self.session = requests.Session()
        self.session.headers["Authorization"] = f"Bearer {api_key}"
        self.last_start = -math.inf
        self.exchanges: list[dict[str, Any]] = []

    def call(self, action: str, method: str, url: str, body: dict[str, Any] | None = None, *,
             repeat_safe: bool = True, accept: tuple[int, ...] = ()) -> tuple[int, Any]:
        """Send one request and return (status, JSON body) for a 2xx answer or a status in `accept`.

        HTTP 429 is retried after a wait. A 5xx answer or a network error is retried only
        if `repeat_safe`: sending a create again could make a second company. Raises
        FatalError when no later request can work, HubSpotError for any other refusal.
        """
        for attempt in range(CONFIG["max_retries"] + 1):
            self._wait_turn()
            try:
                resp = self.session.request(method, url, json=body, timeout=CONFIG["timeout_s"])
            except requests.RequestException as err:
                problem = f"{type(err).__name__} ({err})"
                self._record(action, method, url, body, None, problem)
                if not repeat_safe:
                    raise HubSpotError(f"could not {ACTION_TEXT[action]}: {problem}. Not sent again, "
                                       "because the first request may have gone through; run again "
                                       "later.") from err
                wait = float(2 ** attempt)
            else:
                data = self._body(resp)
                status = resp.status_code
                self._record(action, method, url, body, status, data)
                said = data.get("message", "") if isinstance(data, dict) else str(data)[:300]
                if status == 429:
                    policy = str(data.get("policyName", "")) if isinstance(data, dict) else ""
                    if policy.upper() == "DAILY":
                        raise FatalError("HubSpot's daily request limit is reached; it resets at midnight "
                                         f"in the account's time zone. HubSpot said: {said}")
                    problem, wait = "HTTP 429", retry_wait(resp.headers, policy, attempt)
                elif status >= 500 and repeat_safe:
                    problem, wait = f"HTTP {status}", float(2 ** attempt)
                elif 200 <= status < 300 or status in accept:
                    return status, data
                elif status == 401:
                    raise FatalError(f"HubSpot rejected the key (HTTP 401). Check {KEY_ENV} in .env; a "
                                     f"rotated or expired key must be replaced. HubSpot said: {said}")
                elif status == 403:
                    raise FatalError(scope_message(action, data))
                elif status >= 500:
                    raise HubSpotError(f"could not {ACTION_TEXT[action]}: HTTP {status}. Not sent again, "
                                       "because the first request may have gone through; run again later.")
                else:
                    raise HubSpotError(f"could not {ACTION_TEXT[action]}: HTTP {status}: {said}")
            if attempt == CONFIG["max_retries"] or wait > CONFIG["max_wait_s"]:
                raise FatalError(f"{problem} from HubSpot after {attempt + 1} attempt(s) to "
                                 f"{ACTION_TEXT[action]}; try again later.")
            log.warning("%s from HubSpot; waiting %.0fs before retry %d.", problem, wait, attempt + 1)
            time.sleep(wait)
        raise FatalError("Retries exhausted.")

    def _wait_turn(self) -> None:
        """Sleep until request_interval_s has passed since the previous request started."""
        gap = CONFIG["request_interval_s"] - (time.monotonic() - self.last_start)
        if gap > 0:
            time.sleep(gap)
        self.last_start = time.monotonic()

    @staticmethod
    def _body(resp: requests.Response) -> Any:
        """The response as JSON, or the start of its text if it is not JSON."""
        try:
            return resp.json()
        except ValueError:
            return str(resp.text)[:2000]

    def _record(self, action: str, method: str, url: str, body: Any, status: int | None,
                response: Any) -> None:
        """Keep one request and its answer for the raw log. Headers, and so the key, are left out."""
        self.exchanges.append({
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "action": action,
            "method": method,
            "url": url, "request_body": body, "status": status, "response": response,
        })


def save_raw(stamp: str, exchanges: list[dict[str, Any]], counts: dict[str, int]) -> Path:
    """Save a run's requests and responses to data/raw/hubspot_push_<UTC time>.json."""
    path = RAW_DIR / f"{RAW_PREFIX}_{stamp}.json"
    copy = 2
    while path.exists():   # two runs in the same second
        path = RAW_DIR / f"{RAW_PREFIX}_{stamp}_{copy}.json"
        copy += 1
    record = {"saved_at": stamp, "summary": counts, "exchanges": exchanges}
    write_atomic(path, json.dumps(record, indent=1, ensure_ascii=False))
    return path


def load_ids() -> dict[str, str]:
    """Domain -> HubSpot company id, saved by earlier runs; empty if there is no readable file."""
    if not IDS_JSON.exists():
        return {}
    try:
        data = json.loads(IDS_JSON.read_text(encoding="utf-8"))
        return {str(domain): str(company_id) for domain, company_id in data.items()}
    except (OSError, ValueError, AttributeError) as err:
        log.warning("Ignoring unreadable %s: %s", IDS_JSON.name, err)
        return {}


def save_ids(ids: dict[str, str]) -> None:
    """Save the domain -> company id map."""
    write_atomic(IDS_JSON, json.dumps(dict(sorted(ids.items())), indent=1))


def dollars(value: str) -> str:
    """An amount as "$30,020,303", or "" if the cell is empty or not a number."""
    try:
        return f"${int(float(value)):,}"
    except (ValueError, OverflowError):
        return ""


def signal_properties(row: dict[str, str], today: str) -> dict[str, str]:
    """The signal_* values for one accounts.csv row, as strings. signal_stage is not among
    them: it is set only on create. Raises ValueError if the score is not a number."""
    score = str(row["score"]).strip()
    if not math.isfinite(float(score)):   # float() raises ValueError for text and for an empty cell
        raise ValueError(f"score {score!r} is not a number")
    sold = dollars(row["amount_sold_usd"])
    facts = [text for text in (
        f"Form D filed {row['filing_date']}" if row["filing_date"] else "",
        f"{sold} sold (Form D has no round name)" if sold else "",
        f"HQ {row['hq_location']}" if row["hq_location"] else "",
    ) if text]
    reason_lines = (["; ".join(facts)] if facts else []) + [
        part.strip() for part in row["score_reason"].split("; ") if part.strip()]
    role_lines = [title.strip() for title in row["role_titles"].split("; ") if title.strip()]
    if str(row["fde_opening"]).strip().lower() in ("true", "1", "yes"):
        role_lines.append("Forward deployed engineer opening: yes")
    if row["job_locations"]:
        role_lines.append(f"Job locations: {row['job_locations']}")
    limit = CONFIG["max_text_chars"]
    return {
        "signal_score": score,
        "signal_tier": str(row.get("signal_tier") or "").strip()[:limit],
        "signal_reason": "\n".join(reason_lines)[:limit],
        "signal_flags": "\n".join(flag.strip() for flag in str(row.get("flags") or "").split("; ")
                                  if flag.strip())[:limit],
        "signal_roles": "\n".join(role_lines)[:limit],
        "signal_sources": "\n".join(row["source_urls"].split())[:limit],
        "signal_last_seen": today,
    }


def prepare(rows: list[dict[str, str]], today: str) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    """Turn accounts.csv rows into pushes; return (pushes, rows skipped, bad rows), the last
    two as messages. No network."""
    pushes: list[dict[str, Any]] = []
    skipped: list[str] = []
    bad: list[str] = []
    seen: set[str] = set()
    for line, row in enumerate(rows, start=2):
        name = str(row["company_name"]).strip()
        domain = normalise_domain(row["domain"])
        label = name or f"line {line}"
        if not domain:
            skipped.append(f"{label}: no usable domain ({row['domain']!r}), so it cannot be matched "
                           "in HubSpot")
        elif domain in seen:
            skipped.append(f"{label}: {domain} is already in an earlier row")
        else:
            try:
                properties = signal_properties(row, today)
            except (ValueError, KeyError, AttributeError) as err:
                bad.append(f"{label} ({domain}): {err}")
                continue
            seen.add(domain)
            pushes.append({"company_name": name or domain, "domain": domain, "properties": properties})
    return pushes, skipped, bad


def ensure_properties(client: HubSpotClient) -> None:
    """Create the signal_* company properties that HubSpot does not have yet."""
    _, data = client.call("list_properties", "GET", PROPERTIES_URL)
    listed = data.get("results") if isinstance(data, dict) else None
    existing = {item.get("name"): item for item in listed or [] if isinstance(item, dict)}
    group = (existing.get("name") or {}).get("groupName") or CONFIG["property_group"]
    for spec in PROPERTIES:
        have = existing.get(spec["name"])
        if have is None:
            status, _ = client.call("create_property", "POST", PROPERTIES_URL, {**spec, "groupName": group},
                                    accept=(409,))
            log.info("Company property %s %s.", spec["name"],
                     "already exists (HTTP 409)" if status == 409 else f"created in group {group}")
            continue
        if have.get("type") != spec["type"]:
            log.warning("Company property %s exists with type %s, not %s; HubSpot may reject its values.",
                        spec["name"], have.get("type"), spec["type"])
        values = {option.get("value") for option in have.get("options") or [] if isinstance(option, dict)}
        if spec["name"] == "signal_stage" and CONFIG["stage_on_create"] not in values:
            log.warning("Company property signal_stage has no option %r; creating companies will fail.",
                        CONFIG["stage_on_create"])


def find_companies(client: HubSpotClient, domain: str) -> list[dict[str, Any]]:
    """Companies in HubSpot with this domain, oldest first."""
    body = {
        "filterGroups": [{"filters": [{"propertyName": "domain", "operator": "EQ", "value": domain}]}],
        "properties": ["name", "domain", "createdate", "signal_stage"],
        "sorts": [{"propertyName": "createdate", "direction": "ASCENDING"}],
        "limit": CONFIG["search_limit"],
    }
    _, data = client.call("search", "POST", SEARCH_URL, body)
    results = data.get("results") if isinstance(data, dict) else None
    found = []
    for item in results or []:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        theirs = (item.get("properties") or {}).get("domain")
        if theirs and normalise_domain(theirs) != domain:
            log.warning("HubSpot's search for %s returned company %s with domain %s; ignored.",
                        domain, item["id"], theirs)
            continue
        found.append(item)

    def age(item: dict[str, Any]) -> tuple[str, int]:
        created = item.get("createdAt") or (item.get("properties") or {}).get("createdate") or ""
        return str(created), int(item["id"]) if str(item["id"]).isdigit() else 0

    return sorted(found, key=age)


def known_company(client: HubSpotClient, domain: str, ids: dict[str, str]) -> list[dict[str, Any]]:
    """The company an earlier run saved for this domain, if HubSpot still has it with that
    domain; used when the search finds nothing, since new companies reach the search late."""
    company_id = ids.get(domain)
    if not company_id:
        return []
    url = f"{COMPANIES_URL}/{company_id}?properties=domain"
    status, data = client.call("read", "GET", url, accept=(404,))
    theirs = (data.get("properties") or {}).get("domain") if isinstance(data, dict) else None
    still_there = status != 404 and isinstance(data, dict) and not data.get("archived")
    if not still_there or normalise_domain(theirs) != domain:
        log.info("Company %s, saved for %s by an earlier run, is gone or has another domain now.",
                 company_id, domain)
        del ids[domain]
        return []
    log.info("%s: found by the id saved in an earlier run; HubSpot's search does not list it yet.", domain)
    return [data]


def push_row(client: HubSpotClient, push: dict[str, Any], ids: dict[str, str]) -> str:
    """Update the company with this domain, or create it; return "updated" or "created"."""
    domain = push["domain"]
    matches = find_companies(client, domain) or known_company(client, domain, ids)
    if len(matches) > 1:
        log.warning("%s: HubSpot has %d companies with this domain (ids %s); updating the oldest, %s. "
                    "Merge the others in HubSpot.", domain, len(matches),
                    ", ".join(str(item["id"]) for item in matches), matches[0]["id"])
    if matches:
        company_id = str(matches[0]["id"])
        client.call("update", "PATCH", f"{COMPANIES_URL}/{company_id}", {"properties": push["properties"]})
        ids[domain] = company_id
        return "updated"
    properties = {"name": push["company_name"], "domain": domain, **push["properties"],
                  "signal_stage": CONFIG["stage_on_create"]}
    _, data = client.call("create", "POST", COMPANIES_URL, {"properties": properties}, repeat_safe=False)
    company_id = str(data.get("id") or "") if isinstance(data, dict) else ""
    if not company_id:
        raise HubSpotError("HubSpot answered the create without a company id")
    ids[domain] = company_id
    save_ids(ids)   # at once, so a run stopped later still knows this company
    return "created"


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    """Read and check the command line."""
    parser = argparse.ArgumentParser(
        description="Create or update the scored accounts as HubSpot companies.")
    parser.add_argument("--limit", type=int,
                        help="push at most the first N rows of accounts.csv (first test: 5)")
    parser.add_argument("--dry-run", action="store_true",
                        help="print what would be sent and exit without calling HubSpot")
    args = parser.parse_args(argv)
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be 1 or more")
    return args


def shown(path: Path) -> Path:
    """A path relative to the project, for log lines."""
    return path.relative_to(ROOT) if path.is_relative_to(ROOT) else path


def main(argv: list[str] | None = None) -> int:
    """Push data/accounts.csv to HubSpot; exit code 1 if any row could not be written."""
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if not ACCOUNTS_CSV.exists():
        log.error("%s is missing; run build_accounts.py first.", shown(ACCOUNTS_CSV))
        return 1
    table = pd.read_csv(ACCOUNTS_CSV, dtype=str, keep_default_na=False)
    absent = [column for column in REQUIRED_COLUMNS if column not in table.columns]
    if absent:
        log.error("%s lacks the column(s) %s; run build_accounts.py again.", shown(ACCOUNTS_CSV),
                  ", ".join(absent))
        return 1
    for column in OPTIONAL_COLUMNS:
        if column not in table.columns:
            table[column] = ""
    rows = table.to_dict("records")[: args.limit]
    today = datetime.now(timezone.utc).date().isoformat()
    pushes, skipped, bad = prepare(rows, today)
    for message in skipped:
        log.warning("Skipping %s.", message)
    for message in bad:
        log.error("Bad row, not sent: %s.", message)
    counts = {"created": 0, "updated": 0, "skipped": len(skipped), "failed": len(bad)}

    if args.dry_run:
        ids = load_ids()
        for push in pushes:
            known = ids.get(push["domain"])
            push["then"] = (f"update company {known} if HubSpot still has it" if known else
                            "search by domain; update if found, else create with signal_stage "
                            f"{CONFIG['stage_on_create']!r}")
        plan = {"would_push": pushes, "would_skip": skipped, "bad_rows": bad}
        print(json.dumps(plan, indent=2, ensure_ascii=False))
        log.info("Dry run: nothing sent. Would push %d row(s), skip %d, and %d row(s) are bad.",
                 len(pushes), len(skipped), len(bad))
        return 1 if bad else 0

    load_dotenv(ROOT / ".env")
    api_key = os.getenv(KEY_ENV, "").strip()
    if not api_key:
        log.error("%s is not set. Copy .env.example to .env and add your HubSpot service key.", KEY_ENV)
        return 1
    client = HubSpotClient(api_key)
    ids = load_ids()
    ids_before = dict(ids)
    stamp, stopped, done = utc_stamp(), False, 0
    try:
        ensure_properties(client)
        for push in pushes:
            try:
                counts[push_row(client, push, ids)] += 1
            except FatalError:
                raise
            except HubSpotError as err:
                counts["failed"] += 1
                log.error("Skipping %s (%s): %s", push["company_name"], push["domain"], err)
            done += 1
    except FatalError as err:
        stopped = True
        counts["failed"] += len(pushes) - done
        log.error("Stopped: %s", err)
        if len(pushes) - done:
            log.error("%d row(s) were not written.", len(pushes) - done)
    finally:
        if ids != ids_before:
            save_ids(ids)
        raw_path = save_raw(stamp, client.exchanges, counts)
    log.info("Requests and responses saved to %s.", shown(raw_path))
    log.info("HubSpot push: created %d, updated %d, skipped %d, failed %d.",
             counts["created"], counts["updated"], counts["skipped"], counts["failed"])
    return 1 if stopped or counts["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
