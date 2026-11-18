"""Run the weekly digest workflow in n8n, an automation platform (after step 4).

The workflow is automation/n8n/weekly_digest.json. It searches HubSpot for companies whose
`signal_stage` is "new" and that were created in the last 7 days, builds a digest, and
creates one HubSpot task holding it. Since step 3 decides uncertain matches by rule, this
digest is how the reviewer sees what the rules listed, without a review step.

n8n runs in a throwaway Docker container: the workflow file is imported, executed once
from the command line and the container is removed. Nothing is installed and no n8n
server stays running.

Request safety:
- The workflow sends two requests, both to api.hubapi.com: one search and one create.
  It never updates or deletes a company and never sends an email or a message.
- HUBSPOT_SERVICE_KEY is read from the environment or .env and handed to the container by
  name, so it is not in the command line. It is removed from everything this script
  prints or saves.
- This script prints counts only, never company names: its output ends up in the public
  log of the weekly run. The full execution, without the key, is saved in data/raw/.

The service key needs crm.objects.companies.read for the search, and a scope that allows
creating tasks; if HubSpot refuses, the error this script prints names what is missing.

Usage:
    python digest_n8n.py --dry-run    # print the command and the plan; run nothing
    python digest_n8n.py --no-task    # search and build the digest, create no task
    python digest_n8n.py              # search, build, and create the task
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
WORKFLOW_JSON = ROOT / "automation" / "n8n" / "weekly_digest.json"
RAW_DIR = ROOT / "data" / "raw"
KEY_ENV = "HUBSPOT_SERVICE_KEY"
SEARCH_NODE, BUILD_NODE, CREATE_NODE = "Search new companies", "Build digest", "Create digest task"
RESULT_MARKER = "===================================="   # n8n prints this before the execution JSON

CONFIG: dict[str, Any] = {
    "image": "n8nio/n8n:2.48.4",   # pinned: the version this workflow was tested with
    "timeout_s": 600,              # first use downloads the image
    # Settings for the throwaway n8n: let the workflow read the key, send no usage data
    "container_env": {
        "N8N_BLOCK_ENV_ACCESS_IN_NODE": "false",
        "N8N_DIAGNOSTICS_ENABLED": "false",
        "N8N_VERSION_NOTIFICATIONS_ENABLED": "false",
        "N8N_PERSONALIZATION_ENABLED": "false",
    },
}

log = logging.getLogger("digest_n8n")


def shown(path: Path) -> Path:
    """A path relative to the project, for log lines."""
    return path.relative_to(ROOT) if path.is_relative_to(ROOT) else path


def workflow_id(path: Path | None = None) -> str:
    """The id inside the workflow file, which n8n keeps on import."""
    return str(json.loads((path or WORKFLOW_JSON).read_text(encoding="utf-8"))["id"])


def docker_command(no_task: bool, path: Path | None = None) -> list[str]:
    """The docker command that imports the workflow into a fresh n8n and executes it once.
    The key is passed by name only (-e NAME), so its value is never part of the command."""
    path = path or WORKFLOW_JSON
    command = ["docker", "run", "--rm", "--entrypoint", "sh", "-e", KEY_ENV]
    for name, value in CONFIG["container_env"].items():
        command += ["-e", f"{name}={value}"]
    if no_task:
        command += ["-e", "DIGEST_NO_TASK=1"]
    command += ["-v", f"{path.parent}:/work:ro", CONFIG["image"], "-c",
                f"n8n import:workflow --input=/work/{path.name} && n8n execute --id {workflow_id(path)}"]
    return command


def scrub(text: str, secret: str) -> str:
    """The text without the key."""
    return text.replace(secret, "[key removed]") if secret else text


def parse_execution(output: str) -> dict[str, Any] | None:
    """The execution n8n printed after its marker line, or None if there is none."""
    _, marker, rest = output.partition(RESULT_MARKER)
    start = rest.find("{")
    if not marker or start < 0:
        return None
    try:
        execution, _ = json.JSONDecoder().raw_decode(rest[start:])
    except ValueError:
        return None
    return execution if isinstance(execution, dict) else None


def node_output(execution: dict[str, Any], node: str) -> list[dict[str, Any]]:
    """The items one node produced, or [] if it did not run."""
    try:
        runs = execution["data"]["resultData"]["runData"][node]
        return [item.get("json", {}) for item in runs[0]["data"]["main"][0]]
    except (KeyError, IndexError, TypeError):
        return []


def summarize(execution: dict[str, Any], no_task: bool) -> tuple[bool, str]:
    """Whether the run did what was asked, and one line of counts for the log (no names)."""
    error = (execution.get("data", {}).get("resultData", {}) or {}).get("error")
    if error or execution.get("status") != "success":
        detail = " ".join(str(error.get(key)) for key in ("httpCode", "message", "description")
                          if isinstance(error, dict) and error.get(key))
        failed_at = (execution.get("data", {}).get("resultData", {}) or {}).get("lastNodeExecuted", "unknown node")
        return False, f"n8n digest failed at '{failed_at}': {detail or 'no error text'}"
    search = node_output(execution, SEARCH_NODE)
    if not search or "total" not in search[0]:
        return False, "n8n digest failed: the company search returned no result"
    found = search[0]["total"]
    flagged = sum(1 for company in search[0].get("results", []) if (company.get("properties") or {}).get("signal_flags"))
    created = node_output(execution, CREATE_NODE)
    if no_task:
        if created:
            return False, "n8n digest failed: a task was created in a run that should create none"
        return True, f"n8n digest: {found} new account(s), {flagged} flagged; no task created (--no-task)."
    if not created or not created[0].get("id"):
        return False, f"n8n digest failed: {found} new account(s) found, but HubSpot returned no task id"
    return True, f"n8n digest: {found} new account(s), {flagged} flagged; 1 HubSpot task created."


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    """Read the command line."""
    parser = argparse.ArgumentParser(description="Run the weekly digest workflow in n8n.")
    parser.add_argument("--dry-run", action="store_true", help="print the command and the plan; run nothing")
    parser.add_argument("--no-task", action="store_true", help="search and build the digest, but create no task")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Run the workflow once and report counts."""
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if not WORKFLOW_JSON.exists():
        log.error("%s is missing.", shown(WORKFLOW_JSON))
        return 1
    command = docker_command(args.no_task)
    if args.dry_run:
        log.info("Dry run: nothing is run. The command would be: %s", " ".join(command))
        log.info("It would search HubSpot for companies with signal_stage 'new' created in the last 7 days%s.",
                 "" if not args.no_task else " and stop before creating a task")
        return 0
    load_dotenv(ROOT / ".env")
    key = os.getenv(KEY_ENV, "").strip()
    if not key:
        log.error("%s is not set (in the environment or in .env); nothing was run.", KEY_ENV)
        return 1
    if not shutil.which("docker"):
        log.error("Docker is not installed or not on PATH; n8n runs in a container. Nothing was run.")
        return 1
    try:
        run = subprocess.run(command, capture_output=True, text=True, timeout=CONFIG["timeout_s"],
                             env={**os.environ, KEY_ENV: key})
    except subprocess.TimeoutExpired:
        log.error("n8n did not finish within %d seconds; the run was stopped.", CONFIG["timeout_s"])
        return 1
    output = scrub(run.stdout + "\n" + run.stderr, key)
    execution = parse_execution(output)
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")
    saved = RAW_DIR / f"n8n_digest_{stamp}.json"
    saved.write_text(json.dumps({"exit_code": run.returncode, "no_task": args.no_task, "execution": execution,
                                 "output_tail": output[-4000:] if execution is None else ""}, indent=1),
                     encoding="utf-8")
    if execution is None:
        log.error("n8n printed no execution result (exit code %d). Its last lines are in %s.", run.returncode,
                  shown(saved))
        return 1
    ok, line = summarize(execution, args.no_task)
    (log.info if ok else log.error)("%s", line)
    return 0 if ok and run.returncode == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
