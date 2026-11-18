"""Offline checks for apollo_sequence.py. Apollo here is SIMULATED in memory; a real network
request is impossible, because the script's session is replaced. The project's data/ is never
read or written.

Run: python tests/check_apollo_sequence.py
"""
import contextlib
import io
import json
import logging
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
import apollo_sequence as ap  # noqa: E402
import requests  # noqa: E402

tmp = Path(tempfile.mkdtemp(prefix="sim_apollo_"))
ap.RAW_DIR = tmp / "raw"
ap.IDS_JSON = ap.RAW_DIR / "apollo_sequence_ids.json"
ap.load_dotenv = lambda *args, **kwargs: False     # never load a real .env
SLEEPS, results = [], []
ap.time.sleep = lambda seconds: SLEEPS.append(seconds)
os.environ.pop(ap.KEY_ENV, None)
KEY = "SIMULATED-apollo-key-0123456789"


def check(name, cond):
    results.append(bool(cond))
    print("PASS" if cond else "FAIL", "-", name)


class Collect(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines = []

    def emit(self, record):
        self.lines.append(f"{record.levelname} {record.getMessage()}")


H = Collect()
ap.log.addHandler(H)
ap.log.setLevel(logging.INFO)


def logged(text):
    return any(text in line for line in H.lines)


class FakeResp:
    def __init__(self, status, body, headers=None):
        self.status_code, self._body, self.headers = status, body, headers or {}
        self.ok = 200 <= status < 300
        self.text = body if isinstance(body, str) else json.dumps(body)

    def json(self):
        if isinstance(self._body, str):
            raise ValueError("not JSON")
        return self._body


SCHEDULES = {"emailer_schedules": [{"id": "sched-other", "name": "SIMULATED Afternoons", "default": False},
                                   {"id": "sched-default", "name": "SIMULATED Business Hours", "default": True}]}


def created(active=False, with_id=True):
    campaign = {"active": active, "num_steps": 3}
    if with_id:
        campaign["id"] = "seq-123"
    return {"emailer_campaign": campaign, "emailer_touches": [{"status": "to_be_reviewed"}] * 3}


class SimApollo:
    """A SIMULATED Apollo: answers the schedule list and the sequence create, and records every call."""
    sessions, calls, planned = 0, [], []

    def __init__(self):
        SimApollo.sessions += 1
        self.headers = {}

    def request(self, method, url, json=None, timeout=None):
        SimApollo.calls.append({"method": method, "url": url, "body": json, "headers": dict(self.headers)})
        for index, (wanted, answer) in enumerate(SimApollo.planned):
            if wanted == method:
                del SimApollo.planned[index]
                if isinstance(answer, Exception):
                    raise answer
                return answer
        if method == "GET" and url == ap.SCHEDULES_URL:
            return FakeResp(200, SCHEDULES)
        if method == "POST" and url == ap.SEQUENCES_URL:
            return FakeResp(200, created())
        return FakeResp(404, "SIMULATED: no such endpoint")


ap.requests.Session = SimApollo


def fresh(planned=()):
    SimApollo.sessions, SimApollo.calls, SimApollo.planned = 0, [], list(planned)
    H.lines.clear()
    SLEEPS.clear()
    shutil.rmtree(ap.RAW_DIR, ignore_errors=True)


def run(args):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = ap.main(args)
    return code, out.getvalue()


def methods():
    return [call["method"] for call in SimApollo.calls]


# 1. the definition in the repo
definition = ap.load_definition()
steps = definition["steps"]
check("the definition has three steps: one new thread, then two replies",
      [step["thread"] for step in steps] == ["new_thread", "reply_to_thread", "reply_to_thread"])
check("the first email is sent by hand; the follow-ups are automatic",
      [step["type"] for step in steps] == ["manual_email", "auto_email", "auto_email"])
check("waits: none, then 3 days, then 4 days",
      [(step["wait_time"], step["wait_mode"]) for step in steps] == [(0, "minute"), (3, "day"), (4, "day")])
text = ap.DEFINITION_JSON.read_text(encoding="utf-8")
check("the only variables are the first name and the company",
      set(re.findall(r"\{\{(\w+)\}\}", text)) == {"first_name", "company"})
check("the definition holds no email address and sets neither 'active' nor a status",
      not re.search(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[a-z]{2,}", text) and "active" not in definition
      and not any("status" in step for step in steps))
check("the first email lets the person say no", "tell me and I won't write again" in steps[0]["body_html"])
check("6 hand-written parts are still to fill in", ap.placeholders(definition).count("[link]") == 2
      and ap.placeholders(definition).count("[Your name]") == 3 and len(ap.placeholders(definition)) == 6)

# 2. the request body
payload = ap.build_payload(definition, "sched-default")
touches = [step["emailer_touches"][0] for step in payload["emailer_steps"]]
check("the sequence is created inactive, on the given schedule, and stops on a reply",
      payload["active"] is False and payload["emailer_schedule_id"] == "sched-default"
      and payload["mark_finished_if_reply"] is True and payload["max_emails_per_day"] == 5)
check("every email waits for review, so Apollo cannot send it",
      len(touches) == 3 and all(touch["status"] == "to_be_reviewed" for touch in touches)
      and all(len(step["emailer_touches"]) == 1 for step in payload["emailer_steps"]))
check("steps keep their type, wait and thread",
      [(s["type"], s["wait_time"], s["wait_mode"], s["emailer_touches"][0]["type"]) for s in payload["emailer_steps"]]
      == [("manual_email", 0, "minute", "new_thread"), ("auto_email", 3, "day", "reply_to_thread"),
          ("auto_email", 4, "day", "reply_to_thread")])
check("subject and body go into the template; no signature is added",
      touches[0]["emailer_template"] == {"subject": steps[0]["subject"], "body_html": steps[0]["body_html"]}
      and touches[1]["emailer_template"]["subject"] == "" and not any(touch["include_signature"] for touch in touches))

# 3. definitions the script refuses
good = {"name": "SIMULATED", "steps": [{"type": "auto_email", "wait_time": 0, "wait_mode": "minute",
                                         "thread": "new_thread", "subject": "s", "body_html": "<p>b</p>"}]}
bad_file = tmp / "bad.json"
for problem, change in (
        ("'active' set to true", {"active": True}),
        ("no name", {"name": " "}),
        ("no steps", {"steps": []}),
        ("an unknown step type", {"steps": [{**good["steps"][0], "type": "linkedin_step_message"}]}),
        ("a negative wait", {"steps": [{**good["steps"][0], "wait_time": -1}]}),
        ("a wait that is not a whole number", {"steps": [{**good["steps"][0], "wait_time": True}]}),
        ("an unknown wait mode", {"steps": [{**good["steps"][0], "wait_mode": "week"}]}),
        ("a first step that replies to a thread", {"steps": [{**good["steps"][0], "thread": "reply_to_thread"}]}),
        ("an empty body", {"steps": [{**good["steps"][0], "body_html": "  "}]}),
        ("a new thread without a subject", {"steps": [{**good["steps"][0], "subject": ""}]}),
        ("a status in a step", {"steps": [{**good["steps"][0], "status": "approved"}]})):
    bad_file.write_text(json.dumps({**good, **change}))
    try:
        ap.load_definition(bad_file)
        check(f"definition refused: {problem}", False)
    except ValueError:
        check(f"definition refused: {problem}", True)
bad_file.write_text(json.dumps(good))
check("a correct definition is accepted", ap.load_definition(bad_file)["name"] == "SIMULATED")

# 4. what the script is able to do at all
source = (PROJECT / "apollo_sequence.py").read_text(encoding="utf-8")
check("the script knows two Apollo endpoints only: list schedules and create a sequence",
      sorted(set(re.findall(r"https://api\.apollo\.io[^\"'\s)]*", source)))
      == ["https://api.apollo.io/api/v1/emailer_schedules", "https://api.apollo.io/api/v1/sequences"])
check("it has no code for adding contacts, activating a sequence or approving an email",
      not re.search(r"add_contact_ids|/activate|/approve|contacts/|\"approved\"|'approved'", source)
      and '"active": False' in source and ap.CONFIG["touch_status"] == "to_be_reviewed")

# 5. the command, against the SIMULATED Apollo
fresh()
code, out = run(["--dry-run"])
check("--dry-run prints the body, sends nothing and needs no key",
      code == 0 and SimApollo.sessions == 0 and json.loads(out)["active"] is False and logged("Dry run: nothing sent"))
fresh()
code, _ = run([])
check("without the key nothing is sent", code == 1 and SimApollo.calls == [] and logged("is not set in .env"))
os.environ[ap.KEY_ENV] = KEY
fresh()
code, _ = run([])
check("a run sends two requests: the schedule list, then one create", code == 0 and methods() == ["GET", "POST"]
      and [call["url"] for call in SimApollo.calls] == [ap.SCHEDULES_URL, ap.SEQUENCES_URL])
sent = SimApollo.calls[-1]["body"]
check("the create asks for an inactive sequence on the default schedule, every email waiting for review",
      sent["active"] is False and sent["emailer_schedule_id"] == "sched-default"
      and {t["status"] for s in sent["emailer_steps"] for t in s["emailer_touches"]} == {"to_be_reviewed"})
check("the key travels in the x-api-key header", all(call["headers"].get("x-api-key") == KEY for call in SimApollo.calls))
raw_files = sorted(ap.RAW_DIR.glob("apollo_sequence_2*.json"))
check("the sequence id is recorded, and the saved exchange does not hold the key",
      json.loads(ap.IDS_JSON.read_text()) == {definition["name"]: "seq-123"} and len(raw_files) == 1
      and KEY not in raw_files[0].read_text() and KEY not in ap.IDS_JSON.read_text())
check("the log says that nothing was sent and no contact was added",
      logged("inactive") and logged("Nothing was sent and no contact was added") and logged("6 placeholder(s)"))
SimApollo.calls.clear()
H.lines.clear()
code, _ = run([])
check("a second run for the same name sends nothing", code == 1 and SimApollo.calls == [] and logged("already created"))

fresh([("GET", FakeResp(200, {"emailer_schedules": [{"id": "a", "name": "A", "default": False},
                                                    {"id": "b", "name": "B", "default": False}]}))])
code, _ = run([])
check("two schedules and no default: nothing is created", code == 1 and methods() == ["GET"] and logged("none is marked as default"))
fresh([("GET", FakeResp(200, {"emailer_schedules": [{"id": "only", "name": "Only", "default": False}]}))])
code, _ = run([])
check("a single schedule is used even if it is not marked as default",
      code == 0 and SimApollo.calls[-1]["body"]["emailer_schedule_id"] == "only")
fresh([("GET", FakeResp(401, "Invalid access credentials."))])
code, _ = run([])
check("HTTP 401: the key is named as the problem, nothing is created", code == 1 and methods() == ["GET"] and logged("rejected the API key"))
fresh([("GET", FakeResp(403, {"error": "SIMULATED: api/v1/emailer_schedules/index is not accessible with this api_key"}))])
code, _ = run([])
check("HTTP 403: Apollo's own message is shown, nothing is created",
      code == 1 and methods() == ["GET"] and logged("HTTP 403") and logged("is not accessible with this api_key"))
fresh([("POST", FakeResp(422, {"error": "SIMULATED: Your plan is limited to 2 sequences."}))])
code, _ = run([])
check("HTTP 422 on the create: the plan limit is shown and no id is recorded",
      code == 1 and logged("Your plan is limited to 2 sequences") and not ap.IDS_JSON.exists()
      and len(list(ap.RAW_DIR.glob("apollo_sequence_2*.json"))) == 1)
fresh([("GET", FakeResp(429, {"message": "SIMULATED: too many requests"}, {"Retry-After": "2"}))])
code, _ = run([])
check("HTTP 429 is retried after the wait Apollo asks for", code == 0 and methods() == ["GET", "GET", "POST"] and SLEEPS == [2.0])
fresh([("GET", FakeResp(503, "SIMULATED outage"))])
code, _ = run([])
check("a server error on the schedule list is retried", code == 0 and methods() == ["GET", "GET", "POST"])
fresh([("POST", FakeResp(502, "SIMULATED bad gateway"))])
code, _ = run([])
check("a server error on the create is not retried", code == 1 and methods() == ["GET", "POST"] and not ap.IDS_JSON.exists())
fresh([("POST", requests.ConnectionError("SIMULATED drop"))])
code, _ = run([])
check("a network error on the create is not retried", code == 1 and methods() == ["GET", "POST"]
      and logged("the request was not repeated"))
fresh([("POST", FakeResp(200, created(with_id=False)))])
code, _ = run([])
check("an answer without a sequence id is a failure, and nothing is recorded", code == 1 and not ap.IDS_JSON.exists())
fresh([("POST", FakeResp(200, created(active=True)))])
code, _ = run([])
check("if Apollo reports the new sequence as active, the run fails loudly", code == 1 and logged("ACTIVE"))
check("no log line of these runs holds the key", not any(KEY in line for line in H.lines))

print(f"\n{sum(results)}/{len(results)} checks passed")
if all(results):
    shutil.rmtree(tmp, ignore_errors=True)
sys.exit(0 if all(results) else 1)
