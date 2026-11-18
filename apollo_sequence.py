"""Create one email sequence in Apollo as an inactive draft (outreach tooling, not a pipeline step).

Reads automation/apollo/project2_sequence.json, the three draft emails for the owner's own
outreach, and creates the sequence through Apollo's API so the owner does not have to build
it by hand.

What this script cannot do, on purpose:
- It never sends an email. The sequence is created with `active: false` and every email
  with the status `to_be_reviewed`, which Apollo does not send.
- It has no code that adds contacts to a sequence, activates one or approves an email.
  Those three actions stay with the owner, in Apollo, for every batch.
- It creates a sequence once. The id is kept in data/raw/apollo_sequence_ids.json, and a
  second run for the same name stops instead of making a duplicate.

Request safety:
- Two endpoints only, both on api.apollo.io: GET /api/v1/emailer_schedules (to find the
  team's default sending schedule) and POST /api/v1/sequences.
- Nothing is sent without APOLLO_API_KEY in .env; --dry-run needs no key.
- The create is never sent twice: if Apollo gives no clear answer, the run fails.
- Every request and response is saved to data/raw/, without the key.

If Apollo refuses a request (HTTP 403 or 422), its own message is printed: it says whether
the key's scope or the plan's limits are in the way.

Usage:
    python apollo_sequence.py --dry-run    # print what would be sent; send nothing
    python apollo_sequence.py              # create the inactive draft sequence
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
DEFINITION_JSON = ROOT / "automation" / "apollo" / "project2_sequence.json"
RAW_DIR = ROOT / "data" / "raw"
IDS_JSON = RAW_DIR / "apollo_sequence_ids.json"   # sequence name -> id, so a re-run creates nothing
KEY_ENV = "APOLLO_API_KEY"
SCHEDULES_URL = "https://api.apollo.io/api/v1/emailer_schedules"
SEQUENCES_URL = "https://api.apollo.io/api/v1/sequences"

CONFIG: dict[str, Any] = {
    "timeout_s": 30,
    "max_retries": 3,       # only for the schedule list and for HTTP 429, which Apollo did not process
    "max_wait_s": 60,
    "touch_status": "to_be_reviewed",   # Apollo sends an email only after someone approves it
    "step_types": ("auto_email", "manual_email"),
    "wait_modes": ("minute", "hour", "day"),
    "threads": ("new_thread", "reply_to_thread"),
}

log = logging.getLogger("apollo_sequence")


class ApolloError(RuntimeError):
    """Apollo refused a request or gave no clear answer."""


def shown(path: Path) -> Path:
    """A path relative to the project, for log lines."""
    return path.relative_to(ROOT) if path.is_relative_to(ROOT) else path


def load_definition(path: Path | None = None) -> dict[str, Any]:
    """The sequence definition, checked. Raises ValueError with what is wrong."""
    path = path or DEFINITION_JSON
    definition = json.loads(path.read_text(encoding="utf-8"))
    if definition.get("active"):
        raise ValueError(f"{path.name}: 'active' is set. This script only creates inactive sequences.")
    name, steps = str(definition.get("name", "")).strip(), definition.get("steps")
    if not name:
        raise ValueError(f"{path.name}: the sequence needs a name")
    if not isinstance(steps, list) or not steps:
        raise ValueError(f"{path.name}: the sequence needs at least one step")
    for number, step in enumerate(steps, start=1):
        where = f"{path.name}, step {number}"
        if step.get("type") not in CONFIG["step_types"]:
            raise ValueError(f"{where}: type must be one of {', '.join(CONFIG['step_types'])}")
        if step.get("wait_mode") not in CONFIG["wait_modes"] or not isinstance(step.get("wait_time"), int) \
                or isinstance(step.get("wait_time"), bool) or step["wait_time"] < 0:
            raise ValueError(f"{where}: needs a whole wait_time of 0 or more and a wait_mode of "
                             f"{', '.join(CONFIG['wait_modes'])}")
        if step.get("thread") not in CONFIG["threads"]:
            raise ValueError(f"{where}: thread must be one of {', '.join(CONFIG['threads'])}")
        if number == 1 and step["thread"] != "new_thread":
            raise ValueError(f"{where}: the first step must start a new thread")
        if not str(step.get("body_html", "")).strip():
            raise ValueError(f"{where}: the email has no body")
        if step["thread"] == "new_thread" and not str(step.get("subject", "")).strip():
            raise ValueError(f"{where}: an email that starts a thread needs a subject")
        if "status" in step:
            raise ValueError(f"{where}: 'status' is not allowed here. Emails are approved by the owner in Apollo.")
    return definition


def placeholders(definition: dict[str, Any]) -> list[str]:
    """The hand-written parts still to fill in, such as "[link]", in the order they appear."""
    found = []
    for step in definition["steps"]:
        found += re.findall(r"\[[^\[\]]+\]", f"{step.get('subject', '')} {step['body_html']}")
    return found


def build_payload(definition: dict[str, Any], schedule_id: str) -> dict[str, Any]:
    """The body for POST /api/v1/sequences: inactive, every email waiting for review."""
    payload: dict[str, Any] = {
        "name": definition["name"].strip(),
        "active": False,
        "emailer_schedule_id": schedule_id,
        "mark_finished_if_reply": True,
        "emailer_steps": [{
            "type": step["type"], "wait_time": step["wait_time"], "wait_mode": step["wait_mode"],
            "emailer_touches": [{
                "type": step["thread"], "status": CONFIG["touch_status"], "include_signature": False,
                "emailer_template": {"subject": step.get("subject", ""), "body_html": step["body_html"]},
            }],
        } for step in definition["steps"]],
    }
    if isinstance(definition.get("max_emails_per_day"), int):
        payload["max_emails_per_day"] = definition["max_emails_per_day"]
    return payload


def message_of(response: requests.Response) -> str:
    """Apollo's own words for an error, shortened."""
    try:
        body = response.json()
    except ValueError:
        return response.text.strip()[:300]
    parts = [str(body[key]) for key in ("error", "message", "error_message", "error_details") if body.get(key)]
    return ("; ".join(parts) or json.dumps(body))[:500]


class ApolloClient:
    """Sends the two requests this script may send and records each exchange, without the key."""

    def __init__(self, api_key: str) -> None:
        self.session = requests.Session()
        self.session.headers.update({"x-api-key": api_key, "Content-Type": "application/json",
                                     "Cache-Control": "no-cache"})
        self.exchanges: list[dict[str, Any]] = []

    def call(self, method: str, url: str, body: dict[str, Any] | None = None, *, repeatable: bool) -> dict[str, Any]:
        """One request. HTTP 429 is retried after a wait. A network error or a 5xx is retried
        only for a request that is safe to repeat; a create fails instead of being sent twice."""
        for attempt in range(CONFIG["max_retries"] + 1):
            record: dict[str, Any] = {"method": method, "url": url, "body": body}
            self.exchanges.append(record)
            try:
                response = self.session.request(method, url, json=body, timeout=CONFIG["timeout_s"])
            except requests.RequestException as err:
                record["error"] = type(err).__name__
                if repeatable and attempt < CONFIG["max_retries"]:
                    time.sleep(min(2 ** attempt, CONFIG["max_wait_s"]))
                    continue
                raise ApolloError(f"no answer from Apollo ({type(err).__name__}); the request was not repeated") from err
            record["status"] = response.status_code
            try:
                record["response"] = response.json()
            except ValueError:
                record["response"] = response.text[:1000]
            if response.status_code == 429 and attempt < CONFIG["max_retries"]:
                wait = min(float(response.headers.get("Retry-After") or 2 ** attempt), CONFIG["max_wait_s"])
                log.warning("Apollo answered 429 (too many requests); waiting %.0f s.", wait)
                time.sleep(wait)
                continue
            if response.status_code >= 500 and repeatable and attempt < CONFIG["max_retries"]:
                time.sleep(min(2 ** attempt, CONFIG["max_wait_s"]))
                continue
            if response.status_code == 401:
                raise ApolloError("Apollo rejected the API key (HTTP 401). Check APOLLO_API_KEY in .env.")
            if response.status_code == 403:
                raise ApolloError("Apollo refused this request for this key or plan (HTTP 403): "
                                  f"{message_of(response)}")
            if not response.ok:
                raise ApolloError(f"Apollo answered HTTP {response.status_code}: {message_of(response)}")
            return record["response"] if isinstance(record["response"], dict) else {}
        raise ApolloError("Apollo kept answering 429; stopped.")


def default_schedule(client: ApolloClient) -> tuple[str, str]:
    """The id and name of the team's default sending schedule (or of the only one)."""
    schedules = client.call("GET", SCHEDULES_URL, repeatable=True).get("emailer_schedules") or []
    chosen = next((s for s in schedules if s.get("default")), schedules[0] if len(schedules) == 1 else None)
    if not chosen or not chosen.get("id"):
        raise ApolloError(f"Apollo listed {len(schedules)} sending schedule(s) and none is marked as default; "
                          "create or mark one in Apollo first.")
    return str(chosen["id"]), str(chosen.get("name", ""))


def read_ids() -> dict[str, str]:
    """Sequence name -> id for every sequence this script created."""
    if not IDS_JSON.exists():
        return {}
    return json.loads(IDS_JSON.read_text(encoding="utf-8"))


def save_raw(stamp: str, exchanges: list[dict[str, Any]]) -> Path:
    """Write the run's requests and responses to data/raw/. The key is a header, so it is not in them."""
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    path = RAW_DIR / f"apollo_sequence_{stamp}.json"
    path.write_text(json.dumps({"saved_at": stamp, "exchanges": exchanges}, indent=1, ensure_ascii=False),
                    encoding="utf-8")
    return path


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    """Read the command line."""
    parser = argparse.ArgumentParser(description="Create one inactive draft sequence in Apollo.")
    parser.add_argument("--dry-run", action="store_true", help="print what would be sent; send nothing")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Create the draft sequence, or show what would be created."""
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        definition = load_definition()
    except (OSError, ValueError) as err:
        log.error("%s", err)
        return 1
    todo = placeholders(definition)
    if args.dry_run:
        payload = build_payload(definition, "<the team's default schedule id>")
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        log.info("Dry run: nothing sent. Would create '%s' with %d step(s), inactive, every email waiting for "
                 "review. %d placeholder(s) are still to fill in by hand.", payload["name"],
                 len(payload["emailer_steps"]), len(todo))
        return 0
    load_dotenv(ROOT / ".env")
    key = os.getenv(KEY_ENV, "").strip()
    if not key:
        log.error("%s is not set in .env; nothing was sent.", KEY_ENV)
        return 1
    ids = read_ids()
    name = definition["name"].strip()
    if name in ids:
        log.error("A sequence named '%s' was already created by this script (id recorded in %s). Nothing was "
                  "sent. Delete or rename it in Apollo and remove that line to create it again.", name,
                  shown(IDS_JSON))
        return 1
    client = ApolloClient(key)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")
    try:
        schedule_id, schedule_name = default_schedule(client)
        created = client.call("POST", SEQUENCES_URL, build_payload(definition, schedule_id), repeatable=False)
        campaign = created.get("emailer_campaign") or {}
        if not campaign.get("id"):
            raise ApolloError("Apollo answered without a sequence id; check Apollo before running again.")
        if campaign.get("active"):
            raise ApolloError("Apollo reports the new sequence as ACTIVE. Deactivate it in Apollo now.")
    except ApolloError as err:
        log.error("%s", err)
        log.info("Requests and responses saved to %s.", shown(save_raw(stamp, client.exchanges)))
        return 1
    ids[name] = str(campaign["id"])
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    IDS_JSON.write_text(json.dumps(ids, indent=1, ensure_ascii=False), encoding="utf-8")
    saved = save_raw(stamp, client.exchanges)
    statuses = sorted({str(touch.get("status")) for touch in created.get("emailer_touches") or []})
    log.info("Apollo sequence created: '%s', %d step(s), inactive, schedule '%s'. Email statuses: %s.", name,
             len(definition["steps"]), schedule_name, ", ".join(statuses) or "not reported")
    log.info("Nothing was sent and no contact was added. %d placeholder(s) are still to fill in by hand in "
             "Apollo. Saved to %s.", len(todo), shown(saved))
    return 0


if __name__ == "__main__":
    sys.exit(main())
