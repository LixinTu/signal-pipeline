"""Offline checks for digest_n8n.py and the n8n workflow file. No Docker and no network: what
n8n prints is SIMULATED, and the workflow's JavaScript runs in Node.js on SIMULATED companies.

Run: python tests/check_digest_n8n.py   (needs Node.js for the JavaScript checks)
"""
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import digest_n8n as dn  # noqa: E402

tmp = Path(tempfile.mkdtemp(prefix="sim_digest_"))
dn.RAW_DIR = tmp / "raw"
dn.load_dotenv = lambda *args, **kwargs: False    # the real .env is never read here
FAKE_KEY = "SIMULATED-key-0123456789abcdef"
results = []


def check(name, cond):
    results.append(bool(cond))
    print("PASS" if cond else "FAIL", "-", name)


class Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines = []

    def emit(self, record):
        self.lines.append(f"{record.levelname} {record.getMessage()}")


capture = Capture()
logging.getLogger("digest_n8n").addHandler(capture)

# 1. the workflow file
workflow = json.loads(dn.WORKFLOW_JSON.read_text(encoding="utf-8"))
nodes = {node["name"]: node for node in workflow["nodes"]}
http_nodes = [node for node in workflow["nodes"] if node["type"] == "n8n-nodes-base.httpRequest"]
check("the workflow has the four planned nodes, in order",
      [node["name"] for node in workflow["nodes"]] == ["Start", dn.SEARCH_NODE, dn.BUILD_NODE, dn.CREATE_NODE])
check("it starts from a manual trigger and is not active, so only this script runs it",
      nodes["Start"]["type"] == "n8n-nodes-base.manualTrigger" and workflow["active"] is False)
check("the nodes are connected in one line",
      [workflow["connections"][name]["main"][0][0]["node"] for name in ("Start", dn.SEARCH_NODE, dn.BUILD_NODE)]
      == [dn.SEARCH_NODE, dn.BUILD_NODE, dn.CREATE_NODE] and dn.CREATE_NODE not in workflow["connections"])
check("it sends exactly two requests: one company search and one task create",
      [(node["parameters"]["method"], node["parameters"]["url"]) for node in http_nodes]
      == [("POST", "https://api.hubapi.com/crm/v3/objects/companies/search"),
          ("POST", "https://api.hubapi.com/crm/v3/objects/tasks")])
check("every request goes to api.hubapi.com over https",
      all(urlsplit(node["parameters"]["url"]).scheme == "https"
          and urlsplit(node["parameters"]["url"]).netloc == "api.hubapi.com" for node in http_nodes))
text = dn.WORKFLOW_JSON.read_text(encoding="utf-8")
check("the key comes from the environment; the file holds no key",
      all(node["parameters"]["headerParameters"]["parameters"] == [
          {"name": "Authorization", "value": "=Bearer {{ $env.HUBSPOT_SERVICE_KEY }}"}] for node in http_nodes)
      and not re.search(r"pat-[a-z0-9-]{10,}|Bearer [A-Za-z0-9_-]{16,}", text))
search_body = nodes[dn.SEARCH_NODE]["parameters"]["jsonBody"]
check("the search asks for stage 'new', created in the last 7 days, at most 100",
      "propertyName: 'signal_stage', operator: 'EQ', value: 'new'" in search_body
      and "propertyName: 'createdate', operator: 'GTE'" in search_body and "7 * 24 * 60 * 60 * 1000" in search_body
      and "limit: 100" in search_body)
check("the workflow id in the file is the one the command executes", dn.workflow_id() == workflow["id"])

# 2. the JavaScript of the Code node, run in Node.js on SIMULATED search results
node_bin = shutil.which("node")
check("Node.js is available for the JavaScript checks", node_bin is not None)
harness = tmp / "run_code_node.js"
harness.write_text(
    "const SIM = JSON.parse(process.argv[2]);\nconst $env = JSON.parse(process.argv[3]);\n"
    "const $input = { first: () => ({ json: SIM }) };\n"
    "const out = (function () {\n" + nodes[dn.BUILD_NODE]["parameters"]["jsCode"] + "\n})();\n"
    "console.log(JSON.stringify(out));\n", encoding="utf-8")


def build(search_result, env=None):
    """What the Code node returns for one SIMULATED search result, or None if Node.js fails."""
    if not node_bin:
        return None
    run = subprocess.run([node_bin, str(harness), json.dumps(search_result), json.dumps(env or {})],
                         capture_output=True, text=True)
    return json.loads(run.stdout) if run.returncode == 0 else None


def company(name, domain, score="2", tier="hiring_only", flags=None):
    properties = {"name": name, "domain": domain, "signal_score": score, "signal_tier": tier}
    if flags is not None:
        properties["signal_flags"] = flags
    return {"id": "1", "properties": properties}


two = build({"total": 2, "results": [
    company("SIMULATED A&B <Labs>", "ab.example", "5", "funded_and_hiring", "also filed 2 filings\nHQ label"),
    company("SIMULATED Plain", "plain.example")]})
task = (two or [{}])[0].get("json", {}).get("task", {}).get("properties", {})
check("two companies -> one task, counted in the subject",
      two is not None and len(two) == 1
      and re.fullmatch(r"Signal digest \d{4}-\d{2}-\d{2}: 2 new account\(s\)", task.get("hs_task_subject", "")))
check("the task is due now, not started, low priority",
      re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z", task.get("hs_timestamp", ""))
      and (task.get("hs_task_status"), task.get("hs_task_priority"), task.get("hs_task_type")) == ("NOT_STARTED", "LOW", "TODO"))
body = task.get("hs_task_body", "")
check("company names are HTML-escaped", "SIMULATED A&amp;B &lt;Labs&gt;" in body and "<Labs>" not in body)
check("each company has one line with its domain, score and tier",
      body.count("<li>") == 2 and "(ab.example): score 5, funded_and_hiring." in body
      and "(plain.example): score 2, hiring_only." in body)
check("flags are shown on the company's line, joined on one line; a company without flags has none",
      "Flags: also filed 2 filings; HQ label." in body and body.count("Flags:") == 1)
check("the counts travel with the task", (two or [{}])[0].get("json", {}).get("new_accounts") == 2
      and (two or [{}])[0].get("json", {}).get("flagged") == 1 and "2 new account(s) in the last 7 days, 1 with a flag" in body)
none = build({"total": 0, "results": []})
check("no new company still makes one task, which says so",
      none is not None and len(none) == 1
      and none[0]["json"]["task"]["properties"]["hs_task_body"] == "<p>No new accounts in the last 7 days.</p>"
      and none[0]["json"]["task"]["properties"]["hs_task_subject"].endswith(": 0 new account(s)"))
many = build({"total": 150, "results": [company(f"SIMULATED {n}", f"c{n}.example") for n in range(100)]})
check("more than 100 new companies: the task says how many are shown",
      many is not None and "150 new account(s)" in many[0]["json"]["task"]["properties"]["hs_task_subject"]
      and "Showing the first 100." in many[0]["json"]["task"]["properties"]["hs_task_body"])
bare = build({"total": 1, "results": [{"id": "9", "properties": {"name": None, "domain": None}}]})
check("a company with empty fields does not break the digest", bare is not None and len(bare) == 1)
check("with DIGEST_NO_TASK=1 nothing goes on to the node that creates the task",
      build({"total": 2, "results": [company("SIMULATED X", "x.example")]}, {"DIGEST_NO_TASK": "1"}) == [])

# 3. the docker command
command = dn.docker_command(no_task=False)
check("n8n runs in a throwaway container from a pinned image",
      command[:3] == ["docker", "run", "--rm"] and re.fullmatch(r"n8nio/n8n:\d+\.\d+\.\d+", dn.CONFIG["image"])
      and dn.CONFIG["image"] in command)
check("the key is passed by name only, never by value",
      command[command.index(dn.KEY_ENV) - 1] == "-e" and not any(part.startswith(dn.KEY_ENV + "=") for part in command))
check("the workflow folder is mounted read-only",
      f"{dn.WORKFLOW_JSON.parent}:/work:ro" in command)
check("the container imports the workflow and executes it once",
      command[-1] == f"n8n import:workflow --input=/work/{dn.WORKFLOW_JSON.name} && n8n execute --id {workflow['id']}")
check("usage data and env blocking are switched off explicitly",
      "N8N_DIAGNOSTICS_ENABLED=false" in command and "N8N_BLOCK_ENV_ACCESS_IN_NODE=false" in command)
check("--no-task tells the workflow to stop before the task; the normal run does not",
      "DIGEST_NO_TASK=1" in dn.docker_command(no_task=True) and "DIGEST_NO_TASK=1" not in command)


# 4. reading what n8n prints (SIMULATED)
def execution(total=2, created=True, status="success", error=None, flags=("x", None), with_search=True):
    run_data = {"Start": [{"data": {"main": [[{"json": {}}]]}}]}
    if with_search:
        found = [company(f"SIMULATED Secret Name {n}", f"s{n}.example", flags=flag) for n, flag in enumerate(flags)]
        run_data[dn.SEARCH_NODE] = [{"data": {"main": [[{"json": {"total": total, "results": found}}]]}}]
        run_data[dn.BUILD_NODE] = [{"data": {"main": [[{"json": {"task": {}}}] if created is not None else []]}}]
    if created:
        run_data[dn.CREATE_NODE] = [{"data": {"main": [[{"json": {"id": "777"} if created is True else created}]]}}]
    result = {"runData": run_data, "lastNodeExecuted": dn.CREATE_NODE}
    if error:
        result["error"] = error
    return {"data": {"resultData": result}, "status": status, "finished": status == "success"}


def printed(run):
    return ("Migrations in progress\nImporting 1 workflows...\nExecution was successful:\n" + dn.RESULT_MARKER + "\n"
            + json.dumps(run, indent=2) + "\n")


check("the execution is read from after n8n's marker line", dn.parse_execution(printed(execution())) == execution())
check("no marker, or broken JSON after it: no execution",
      dn.parse_execution("Migrations in progress\n") is None
      and dn.parse_execution(dn.RESULT_MARKER + "\n{ not json") is None)
ok, line = dn.summarize(execution(), no_task=False)
check("a full run: counts and one task", ok and line == "n8n digest: 2 new account(s), 1 flagged; 1 HubSpot task created.")
check("the line names no company", "Secret Name" not in line and "example" not in line)
ok, line = dn.summarize(execution(created=None), no_task=True)
check("a read-only run: counts, no task", ok and line == "n8n digest: 2 new account(s), 1 flagged; no task created (--no-task).")
ok, line = dn.summarize(execution(created=True), no_task=True)
check("a task in a read-only run is a failure", not ok and "should create none" in line)
ok, line = dn.summarize(execution(created={"status": "error"}), no_task=False)
check("no task id from HubSpot is a failure", not ok and "no task id" in line)
ok, line = dn.summarize(execution(created=None), no_task=False)
check("a full run that created nothing is a failure", not ok)
ok, line = dn.summarize(execution(status="error", created=None, error={
    "httpCode": "403", "message": "Forbidden", "description": "SIMULATED: needs the scope crm.objects.tasks.write"}),
    no_task=False)
check("an error from HubSpot fails the run and shows the status and what is missing",
      not ok and "403" in line and "crm.objects.tasks.write" in line and "Secret Name" not in line)
ok, line = dn.summarize(execution(with_search=False, created=None), no_task=True)
check("a run without a search result is a failure", not ok and "returned no result" in line)
check("the key is removed from text", dn.scrub(f"a {FAKE_KEY} b", FAKE_KEY) == "a [key removed] b"
      and dn.scrub("no key here", "") == "no key here")


# 5. the command, with SIMULATED docker
class FakeRun:
    calls = []
    stdout, returncode = "", 0

    def __call__(self, command, **kwargs):
        FakeRun.calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, FakeRun.returncode, stdout=FakeRun.stdout, stderr="")


dn.subprocess.run = FakeRun()
dn.shutil.which = lambda name: "/usr/bin/docker"
os.environ.pop(dn.KEY_ENV, None)
check("--dry-run exits 0 and runs nothing", dn.main(["--dry-run"]) == 0 and FakeRun.calls == [])
check("without the key nothing is run", dn.main([]) == 1 and FakeRun.calls == []
      and any("is not set" in entry for entry in capture.lines))
os.environ[dn.KEY_ENV] = FAKE_KEY
dn.shutil.which = lambda name: None
check("without Docker nothing is run", dn.main([]) == 1 and FakeRun.calls == [])
dn.shutil.which = lambda name: "/usr/bin/docker"
FakeRun.stdout = printed(execution()) + f"debug line with {FAKE_KEY}\n"
capture.lines.clear()
code = dn.main([])
saved = sorted(dn.RAW_DIR.glob("n8n_digest_*.json"))
check("a full run exits 0 and logs one line of counts",
      code == 0 and capture.lines == ["INFO n8n digest: 2 new account(s), 1 flagged; 1 HubSpot task created."])
check("the key reaches docker through the environment, not the command",
      FakeRun.calls[-1][1]["env"][dn.KEY_ENV] == FAKE_KEY and FAKE_KEY not in " ".join(FakeRun.calls[-1][0]))
check("the execution is saved, without the key",
      len(saved) == 1 and FAKE_KEY not in saved[0].read_text() and json.loads(saved[0].read_text())["execution"] == execution())
FakeRun.stdout, FakeRun.returncode = f"Error: could not start. key={FAKE_KEY}\n", 1
capture.lines.clear()
code = dn.main([])
latest = sorted(dn.RAW_DIR.glob("n8n_digest_*.json"), key=lambda path: path.stat().st_mtime)[-1]
check("no execution from n8n -> exit 1, and its last lines are saved without the key",
      code == 1 and any("printed no execution result" in entry for entry in capture.lines)
      and "[key removed]" in latest.read_text() and FAKE_KEY not in latest.read_text())
FakeRun.stdout, FakeRun.returncode = printed(execution()), 1
check("a non-zero exit from the container fails the run even with a good execution", dn.main([]) == 1)
FakeRun.stdout, FakeRun.returncode = printed(execution(created=None)), 0
check("--no-task passes the switch to the container and exits 0",
      dn.main(["--no-task"]) == 0 and "DIGEST_NO_TASK=1" in FakeRun.calls[-1][0])
check("nothing printed during these runs names a company or holds the key",
      not any("Secret Name" in entry or FAKE_KEY in entry for entry in capture.lines))

print(f"\n{sum(results)}/{len(results)} checks passed")
if all(results):
    shutil.rmtree(tmp, ignore_errors=True)
sys.exit(0 if all(results) else 1)
