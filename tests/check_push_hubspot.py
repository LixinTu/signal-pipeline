"""Offline checks for push_hubspot.py (step 4). HubSpot here is SIMULATED in memory and every
account row is SIMULATED; a real network request fails the checks. The project's data/ is
never read or written.

Run: python tests/check_push_hubspot.py
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
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
import pandas as pd  # noqa: E402
import push_hubspot as ph  # noqa: E402

tmp = Path(tempfile.mkdtemp(prefix="sim_hubspot_"))
ph.ACCOUNTS_CSV = tmp / "accounts.csv"                           # keep the project's data/ untouched
ph.RAW_DIR = tmp / "raw"
ph.IDS_JSON = ph.RAW_DIR / "hubspot_company_ids.json"
SLEEPS, results = [], []
ph.time.sleep = lambda seconds: SLEEPS.append(seconds)           # no real waiting
ph.load_dotenv = lambda *a, **k: False                           # never load a real .env
os.environ.pop("HUBSPOT_SERVICE_KEY", None)
logging.getLogger().addHandler(logging.NullHandler())            # keep the script's log lines off the screen
KEY = "sim-key-not-real"
READ, WRITE = "crm.objects.companies.read", "crm.objects.companies.write"
SCHEMA_READ, SCHEMA_WRITE = "crm.schemas.companies.read", "crm.schemas.companies.write"
FIELD_TYPES = {"string": {"text", "textarea"}, "number": {"number"}, "date": {"date"}, "enumeration": {"select"}}


def check(name, cond):
    results.append(bool(cond))
    print("PASS" if cond else "FAIL", "-", name)


class Collect(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines = []

    def emit(self, record):
        self.lines.append((record.levelname, record.getMessage()))


H = Collect()
ph.log.addHandler(H)
ph.log.setLevel(logging.INFO)


def logged(text, level=None):
    return any(text in message and level in (None, found) for found, message in H.lines)


def summary():
    return next((message for _, message in H.lines if message.startswith("HubSpot push:")), "")


class FakeResp:
    def __init__(self, status, body, headers=None):
        self.status_code, self._body, self.headers = status, body, headers or {}
        self.ok = 200 <= status < 300
        self.text = body if isinstance(body, str) else json.dumps(body)

    def json(self):
        if isinstance(self._body, str):
            raise ValueError("not JSON")
        return self._body


class SimHubSpot:
    """SIMULATED HubSpot account. It enforces what the docs say about scopes, the property
    schema and property values, so a wrong request from the script fails here."""

    def __init__(self):
        self.scopes = {READ, WRITE, SCHEMA_READ, SCHEMA_WRITE}
        self.properties = {name: {"name": name, "label": name.title(), "type": "string", "fieldType": "text",
                                  "groupName": "companyinformation"} for name in ("name", "domain")}
        self.companies = {}          # id -> company
        self.unlisted = set()        # ids the search does not list yet
        self.index_new = True        # False: a new company stays out of the search (indexing delay)
        self.ignore_sorts = False    # True: the search answers newest first whatever was asked
        self.refuse_domains = set()  # creating these fails with HTTP 400
        self.planned = []            # (matches(method, path), response): answered once, in order
        self.calls = []              # (method, path, body)
        self.stray = []              # requests to any other host
        self.next_id, self.tick = 5001, 0

    def sent(self, method, ending=""):
        return [call for call in self.calls if call[0] == method and call[1].split("?")[0].endswith(ending)]

    @staticmethod
    def error(status, category, message, **more):
        return FakeResp(status, {"status": "error", "message": message, "category": category,
                                 "correlationId": "sim-correlation-id", **more})

    def need(self, scope):
        """A 403 if the key lacks `scope`. The body's shape is recalled from HubSpot's real
        answers, not confirmed in its docs; the script must not depend on it."""
        if scope in self.scopes:
            return None
        return self.error(403, "MISSING_SCOPES", "This app hasn't been granted all required scopes to make this "
                          "call. Read more about required scopes here: https://developers.hubspot.com/scopes.",
                          errors=[{"message": "One or more of the following scopes are required.",
                                   "context": {"requiredGranularScopes": [scope]}}],
                          links={"scopes": "https://developers.hubspot.com/scopes"})

    def bad_values(self, properties):
        if not isinstance(properties, dict) or not all(
                isinstance(key, str) and isinstance(value, str) for key, value in properties.items()):
            return self.error(400, "VALIDATION_ERROR", "properties must be an object of strings")
        for name, value in properties.items():
            spec = self.properties.get(name)
            problem = None
            if spec is None:
                problem = f"PROPERTY_DOESNT_EXIST: {name}"
            elif value == "":
                continue
            elif len(value) > 65536:
                problem = f"{name} is longer than 65,536 characters"
            elif spec["type"] == "number" and not re.fullmatch(r"-?\d+(\.\d+)?", value):
                problem = f"INVALID_INTEGER: {name}={value}"
            elif spec["type"] == "date" and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                problem = f"INVALID_DATE: {name}={value}"
            elif spec["type"] == "enumeration" and value not in {o["value"] for o in spec["options"]}:
                problem = f"INVALID_OPTION: {name}={value}"
            if problem:
                return self.error(400, "VALIDATION_ERROR", f"Property values were not valid: {problem}")
        return None

    def company(self, properties, created_at=None):
        self.tick += 1
        stamp = created_at or (datetime(2026, 11, 16, tzinfo=timezone.utc) + timedelta(minutes=self.tick)).isoformat()
        company = {"id": str(self.next_id), "properties": dict(properties), "createdAt": stamp,
                   "updatedAt": stamp, "archived": False}
        self.companies[company["id"]] = company
        self.next_id += 1
        return company

    def view(self, company, wanted):
        shown = {name: company["properties"].get(name) for name in wanted}
        return {**company, "properties": {**shown, "hs_object_id": company["id"], "createdate": company["createdAt"]}}

    def create_property(self, body):
        body = body if isinstance(body, dict) else {}
        absent = [field for field in ("groupName", "name", "label", "type", "fieldType") if not body.get(field)]
        if absent:
            return self.error(400, "VALIDATION_ERROR", f"missing required field(s): {', '.join(absent)}")
        if body["name"] in self.properties:
            return self.error(409, "OBJECT_ALREADY_EXISTS", f"A property named {body['name']} already exists.")
        if body["groupName"] != "companyinformation":
            return self.error(400, "VALIDATION_ERROR", f"property group {body['groupName']} does not exist")
        if body["fieldType"] not in FIELD_TYPES.get(body["type"], ()):
            return self.error(400, "VALIDATION_ERROR", f"fieldType {body['fieldType']} does not fit {body['type']}")
        options = body.get("options") or []
        if body["type"] == "enumeration" and (not options or not all(
                isinstance(o, dict) and isinstance(o.get("label"), str) and isinstance(o.get("value"), str)
                and isinstance(o.get("hidden"), bool) for o in options)):
            return self.error(400, "VALIDATION_ERROR", "an enumeration needs options with label, value and hidden")
        self.properties[body["name"]] = dict(body)
        return FakeResp(201, dict(body))

    def search(self, body):
        try:
            (group,) = body["filterGroups"]
            (rule,) = group["filters"]
            assert rule["propertyName"] == "domain" and rule["operator"] == "EQ" and isinstance(rule["value"], str)
            assert 1 <= body.get("limit", 10) <= 200 and isinstance(body.get("properties", []), list)
            sorts = body.get("sorts", [])
            assert all(s["direction"] in ("ASCENDING", "DESCENDING") and s["propertyName"] for s in sorts)
        except (KeyError, TypeError, ValueError, AssertionError):
            return self.error(400, "VALIDATION_ERROR", "the search body is not valid")
        found = [c for c in self.companies.values() if not c["archived"] and c["id"] not in self.unlisted
                 and str(c["properties"].get("domain", "")).lower() == rule["value"].lower()]
        oldest_first = bool(sorts) and sorts[0] == {"propertyName": "createdate", "direction": "ASCENDING"}
        found.sort(key=lambda c: c["createdAt"], reverse=self.ignore_sorts or not oldest_first)
        page = found[: body.get("limit", 10)]
        return FakeResp(200, {"total": len(found), "results": [self.view(c, body.get("properties", [])) for c in page]})

    def handle(self, method, url, body, headers):
        if not url.startswith("https://api.hubapi.com/"):
            self.stray.append(url)
            return FakeResp(599, "SIMULATED: not HubSpot")
        path = url.removeprefix("https://api.hubapi.com")
        self.calls.append((method, path, body))
        if headers.get("Authorization") != f"Bearer {KEY}":
            return self.error(401, "INVALID_AUTHENTICATION", "Authentication credentials not found.")
        for position, (matches, response) in enumerate(self.planned):
            if matches(method, path):
                del self.planned[position]
                return response
        if path == "/crm/v3/properties/companies" and method == "GET":
            return self.need(SCHEMA_READ) or FakeResp(200, {"results": list(self.properties.values())})
        if path == "/crm/v3/properties/companies" and method == "POST":
            return self.need(SCHEMA_WRITE) or self.create_property(body)
        if path == "/crm/v3/objects/companies/search" and method == "POST":
            return self.need(READ) or self.search(body)
        if path == "/crm/v3/objects/companies" and method == "POST":
            if self.need(WRITE):
                return self.need(WRITE)
            properties = body.get("properties") if isinstance(body, dict) and set(body) <= {"properties"} else None
            if self.bad_values(properties):
                return self.bad_values(properties)
            if not (properties.get("name") or properties.get("domain")):
                return self.error(400, "VALIDATION_ERROR", "a company needs a name or a domain")
            if properties.get("domain") in self.refuse_domains:
                return self.error(400, "VALIDATION_ERROR", "SIMULATED refusal for this domain")
            company = self.company(properties)
            if not self.index_new:
                self.unlisted.add(company["id"])
            return FakeResp(201, self.view(company, list(properties)))
        by_id = re.fullmatch(r"/crm/v3/objects/companies/(\d+)(?:\?properties=([\w,]+))?", path)
        if by_id and method in ("GET", "PATCH"):
            refusal = self.need(READ if method == "GET" else WRITE)
            company = self.companies.get(by_id.group(1))
            if refusal:
                return refusal
            if company is None or company["archived"]:
                return self.error(404, "OBJECT_NOT_FOUND", "resource not found")
            if method == "GET":
                return FakeResp(200, self.view(company, (by_id.group(2) or "").split(",")))
            properties = body.get("properties") if isinstance(body, dict) and set(body) <= {"properties"} else None
            if self.bad_values(properties):
                return self.bad_values(properties)
            company["properties"].update(properties)
            return FakeResp(200, self.view(company, list(properties)))
        return self.error(404, "NOT_FOUND", f"SIMULATED: no such endpoint {method} {path}")


SIM = SimHubSpot()


class SimSession:                                                # stands in for requests.Session
    def __init__(self, *a, **k):
        self.headers = {}

    def request(self, method, url, json=None, timeout=None):
        return SIM.handle(method, url, json, self.headers)


class NoNetwork:                                                 # any request is a failure
    used = 0

    def __init__(self, *a, **k):
        self.headers = {}

    def request(self, *a, **k):
        NoNetwork.used += 1
        raise AssertionError("network used")


ph.requests.Session = NoNetwork


def run(args, key=KEY, session=SimSession):
    """Run the script's main(); return its exit code (or how it crashed) and what it printed."""
    ph.requests.Session = session
    if key:
        os.environ["HUBSPOT_SERVICE_KEY"] = key
    H.lines.clear()
    SLEEPS.clear()
    printed = io.StringIO()
    try:
        with contextlib.redirect_stdout(printed), contextlib.redirect_stderr(io.StringIO()):
            code = ph.main(args)
    except BaseException as err:  # noqa: BLE001 - AssertionError from NoNetwork, SystemExit from argparse
        code = f"crashed: {type(err).__name__}: {err}"
    finally:
        os.environ.pop("HUBSPOT_SERVICE_KEY", None)
        ph.requests.Session = NoNetwork
    return code, printed.getvalue()


COLUMNS = ["company_name", "domain", "signal_tier", "hq_location", "filing_date", "amount_sold_usd",
           "relevant_roles_count", "role_titles", "fde_opening", "job_locations", "salary_ranges",
           "theirstack_jobs_last_30_days", "company_description", "client_bonus_evidence", "score",
           "score_reason", "flags", "source_urls"]


def account(name, domain, score="4", **more):                    # SIMULATED accounts.csv row
    row = dict.fromkeys(COLUMNS, "")
    row.update({"company_name": name, "domain": domain, "score": score, "signal_tier": "funded_and_hiring",
                "hq_location": "San Francisco, CA 94107", "filing_date": "2026-11-13", "amount_sold_usd": "30020303",
                "role_titles": "Forward Deployed Engineer; ML Engineer", "fde_opening": "True",
                "job_locations": "San Mateo, CA", "score_reason": "first sale 18 days before: +3; FDE opening: +1",
                "source_urls": "https://sec.example/filing/1 https://jobs.example/1"})
    row.update(more)
    return row


def write_accounts(rows, columns=COLUMNS):
    pd.DataFrame(rows, columns=columns).to_csv(ph.ACCOUNTS_CSV, index=False)


def fresh(rows=None, **settings):
    """A new SIMULATED HubSpot account and an empty raw folder."""
    global SIM
    SIM = SimHubSpot()
    for name, value in settings.items():
        setattr(SIM, name, value)
    shutil.rmtree(ph.RAW_DIR, ignore_errors=True)
    if rows is not None:
        write_accounts(rows)
    return SIM


def with_properties(**settings):
    """A new SIMULATED account that already has the signal_* properties."""
    sim = fresh(**settings)
    for spec in ph.PROPERTIES:
        sim.properties[spec["name"]] = {**spec, "groupName": "companyinformation"}
    return sim


def by_domain(domain):
    return [c for c in SIM.companies.values() if c["properties"].get("domain") == domain and not c["archived"]]


def raw_logs():
    return sorted(ph.RAW_DIR.glob("hubspot_push_*.json"))


# 1. helpers
for given, want in {"https://www.Example.com/jobs?x=1": "example.com", " WWW.Acme-Robotics.ai ": "acme-robotics.ai",
                    "http://sub.example.co.uk:8080/": "sub.example.co.uk", "example.com.": "example.com",
                    "": "", "not a domain": "", "localhost": "", None: ""}.items():
    check(f"normalise_domain({given!r}) -> {want!r}", ph.normalise_domain(given) == want)
check("429 wait: Retry-After wins when it is longest", ph.retry_wait({"Retry-After": "7"}, "", 0) == 7)
check("429 wait: the rate-limit window HubSpot names",
      ph.retry_wait({"X-HubSpot-RateLimit-Interval-Milliseconds": "10000"}, "", 0) == 10)
check("429 wait: 10 seconds for the 10-second policy without headers", ph.retry_wait({}, "TEN_SECONDLY_ROLLING", 0) == 10)
check("429 wait: backoff 1, 2, 4 seconds without any hint", [ph.retry_wait({}, "", n) for n in range(3)] == [1, 2, 4])
check("scope names are read from an error body, links are not",
      ph.missing_scopes(SIM.need("x.y.z").json()) == ["x.y.z"] and ph.missing_scopes(
          {"errors": [{"context": {"requiredGranularScopes": [SCHEMA_WRITE, READ]}}],
           "links": {"scopes": "https://developers.hubspot.com/scopes"}}) == [SCHEMA_WRITE, READ]
      and ph.missing_scopes("Forbidden") == [] and ph.missing_scopes({"message": "needs crm.objects.companies.read"}) == [])
values = ph.signal_properties(account("Alpha", "alpha.example", "6"), "2026-11-16")
check("signal_reason: funding facts first, then one score part per line", values["signal_reason"].split("\n") == [
    "Form D filed 2026-11-13; $30,020,303 sold (Form D has no round name); HQ San Francisco, CA 94107",
    "first sale 18 days before: +3", "FDE opening: +1"])
check("signal_roles: one title per line, the FDE flag and the job locations", values["signal_roles"].split("\n") == [
    "Forward Deployed Engineer", "ML Engineer", "Forward deployed engineer opening: yes", "Job locations: San Mateo, CA"])
flagged = ph.signal_properties(account("Flagged", "flagged.example", flags=(
    'the description suggests a staffing or services firm ("recruit"); 74 postings in 30 days against 9 employees')),
    "2026-11-16")
check("signal_flags: one flag per line, and empty for an account without flags",
      flagged["signal_flags"].split("\n") == ['the description suggests a staffing or services firm ("recruit")',
                                              "74 postings in 30 days against 9 employees"]
      and values["signal_flags"] == "")
check("signal_sources: one link per line",
      values["signal_sources"] == "https://sec.example/filing/1\nhttps://jobs.example/1")
check("score, tier and date as strings; signal_stage is not an update value",
      (values["signal_score"], values["signal_tier"], values["signal_last_seen"]) == ("6", "funded_and_hiring", "2026-11-16")
      and "signal_stage" not in values and all(isinstance(v, str) for v in values.values()))
empty = ph.signal_properties(account("NoFunding", "nf.example", filing_date="", amount_sold_usd="", hq_location="",
                                     fde_opening="False", job_locations="", signal_tier=""), "2026-11-16")
check("a row without funding facts has no funding line and no FDE line",
      empty["signal_reason"].startswith("first sale") and empty["signal_roles"] == "Forward Deployed Engineer\nML Engineer")
try:
    ph.signal_properties(account("Bad", "bad.example", "high"), "2026-11-16")
    check("a score that is not a number is refused", False)
except ValueError:
    check("a score that is not a number is refused", True)

# 2. dry run: no key, no network, nothing written
ROWS = [
    account("Alpha Robotics", "alpharobotics.example", "6"),
    account("Bravo AI", "HTTPS://WWW.BravoAI.example/careers", "4"),
    account("Charlie Labs", "charlielabs.example", "3"),
    account("No Domain Co", "", "2"),
    account("Bravo Again", "bravoai.example", "1"),
]
fresh(ROWS)
code, printed = run(["--dry-run"], key=None, session=NoNetwork)
plan = json.loads(printed) if printed.strip().startswith("{") else {}
check("dry run exits 0 without a key or the network", code == 0 and NoNetwork.used == 0 and not SIM.calls)
check("dry run prints the 3 rows it would push, with normalised domains",
      [p["domain"] for p in plan.get("would_push", [])] == ["alpharobotics.example", "bravoai.example", "charlielabs.example"])
check("dry run names the 2 rows it would skip",
      len(plan.get("would_skip", [])) == 2 and "No Domain Co" in plan["would_skip"][0] and "Bravo Again" in plan["would_skip"][1])
check("dry run writes no raw log and no id file", not raw_logs() and not ph.IDS_JSON.exists())

# 3. no key, missing file, missing column
code, _ = run([], key=None, session=NoNetwork)
check("no key -> exit 1 before any request", code == 1 and NoNetwork.used == 0 and logged("HUBSPOT_SERVICE_KEY", "ERROR"))
ph.ACCOUNTS_CSV.unlink()
code, _ = run([], session=NoNetwork)
check("no accounts.csv -> exit 1 before any request", code == 1 and NoNetwork.used == 0 and logged("build_accounts.py", "ERROR"))
write_accounts(ROWS, [c for c in COLUMNS if c != "score_reason"])
code, _ = run([], session=NoNetwork)
check("a missing column -> exit 1, named", code == 1 and NoNetwork.used == 0 and logged("score_reason", "ERROR"))

# 4. first run: properties created, 3 companies created, 2 rows skipped
fresh(ROWS)
code, _ = run([])
created = SIM.sent("POST", "/properties/companies")
check("first run exits 0", code == 0)
check("the 8 missing properties are created, in HubSpot's own company group",
      [c[2]["name"] for c in created] == [s["name"] for s in ph.PROPERTIES] and len(created) == 8
      and all(c[2]["groupName"] == "companyinformation" for c in created))
stage = SIM.properties.get("signal_stage", {})
check("signal_stage is a dropdown with the 5 stages",
      (stage.get("type"), stage.get("fieldType")) == ("enumeration", "select")
      and [o["value"] for o in stage.get("options", [])] == ["new", "reviewed", "ready_to_contact", "contacted", "rejected"])
check("property types: number, text, 4 multi-line texts, date",
      [(SIM.properties[n]["type"], SIM.properties[n]["fieldType"]) for n in (
          "signal_score", "signal_tier", "signal_reason", "signal_flags", "signal_roles", "signal_sources",
          "signal_last_seen")]
      == [("number", "number"), ("string", "text"), ("string", "textarea"), ("string", "textarea"),
          ("string", "textarea"), ("string", "textarea"), ("date", "date")])
check("summary line: created 3, updated 0, skipped 2, failed 0",
      summary() == "HubSpot push: created 3, updated 0, skipped 2, failed 0.")
check("one company per domain, 3 in all", len(SIM.companies) == 3 and all(
    len(by_domain(d)) == 1 for d in ("alpharobotics.example", "bravoai.example", "charlielabs.example")))
alpha = by_domain("alpharobotics.example")[0]["properties"] if by_domain("alpharobotics.example") else {}
check("a created company gets its name, domain, stage 'new' and the signal values",
      (alpha.get("name"), alpha.get("signal_stage"), alpha.get("signal_score"), alpha.get("signal_tier"))
      == ("Alpha Robotics", "new", "6", "funded_and_hiring") and re.fullmatch(r"\d{4}-\d{2}-\d{2}", alpha.get("signal_last_seen", "")))
check("the row without a domain and the repeated domain are skipped with a warning",
      logged("No Domain Co", "WARNING") and logged("Bravo Again", "WARNING"))
check("the domain is searched before every create",
      [c[1].rsplit("/", 1)[-1] for c in SIM.calls if "/objects/companies" in c[1]] == ["search", "companies"] * 3)
check("requests start 0.5 seconds apart", len([s for s in SLEEPS if 0.4 < s <= 0.5]) == len(SIM.calls) - 1)
check("no request left HubSpot's API host", not SIM.stray and NoNetwork.used == 0)
check("the ids of the created companies are saved",
      json.loads(ph.IDS_JSON.read_text()) == {c["properties"]["domain"]: c["id"] for c in SIM.companies.values()})

# 5. second run: creates nothing, keeps the reviewer's edits
alpha_id = by_domain("alpharobotics.example")[0]["id"]
SIM.companies[alpha_id]["properties"].update({"signal_stage": "contacted", "name": "Alpha Robotics (edited by the reviewer)"})
ROWS[0]["score"] = "7"
write_accounts(ROWS)
first_run_calls = len(SIM.calls)
code, _ = run([])
second = SIM.calls[first_run_calls:]
alpha = SIM.companies[alpha_id]["properties"]
check("second run exits 0 and creates 0 companies",
      code == 0 and summary() == "HubSpot push: created 0, updated 3, skipped 2, failed 0." and len(SIM.companies) == 3)
check("second run creates no property", not [c for c in second if c[0] == "POST" and c[1].endswith("/properties/companies")])
check("an update changes the score", alpha["signal_score"] == "7")
check("an update keeps the reviewer's stage and name",
      alpha["signal_stage"] == "contacted" and alpha["name"] == "Alpha Robotics (edited by the reviewer)")
check("updates never send name, domain or signal_stage",
      all(not {"name", "domain", "signal_stage"} & set(c[2]["properties"]) for c in SIM.sent("PATCH")) and len(SIM.sent("PATCH")) == 3)

# 6. HubSpot's search lists a new company late: the saved ids stop a second create
fresh(ROWS[:3], index_new=False)
code, _ = run([])
check("run 1 with a slow search index creates 3", code == 0 and len(SIM.companies) == 3)
code, _ = run([])
check("run 2 right after creates 0, although the search finds nothing",
      code == 0 and summary() == "HubSpot push: created 0, updated 3, skipped 0, failed 0." and len(SIM.companies) == 3)
check("it read each company by its saved id", len(SIM.sent("GET")) - 2 == 3 and logged("does not list it yet"))
gone = by_domain("charlielabs.example")[0]
gone["archived"] = True                                          # the reviewer deleted it in HubSpot
code, _ = run([])
check("a company deleted in HubSpot is created again, not updated",
      code == 0 and summary() == "HubSpot push: created 1, updated 2, skipped 0, failed 0."
      and len(by_domain("charlielabs.example")) == 1 and by_domain("charlielabs.example")[0]["id"] != gone["id"])
moved = by_domain("bravoai.example")[0]
moved["properties"].update({"domain": "renamed.example", "signal_score": "99"})   # the reviewer gave it another domain
code, _ = run([])
check("a saved id whose company now has another domain is not updated",
      code == 0 and moved["properties"]["signal_score"] == "99" and len(by_domain("bravoai.example")) == 1
      and by_domain("bravoai.example")[0]["id"] != moved["id"]
      and summary() == "HubSpot push: created 1, updated 2, skipped 0, failed 0.")

# 7. two companies with one domain in HubSpot: the oldest is updated, with a warning
for ignore_sorts in (False, True):
    with_properties(ignore_sorts=ignore_sorts)
    old = SIM.company({"name": "Dup (older)", "domain": "dup.example"}, "2025-01-01T00:00:00+00:00")
    new = SIM.company({"name": "Dup (newer)", "domain": "dup.example"}, "2026-06-01T00:00:00+00:00")
    write_accounts([account("Dup", "dup.example", "5")])
    code, _ = run([])
    label = "newest first whatever was asked" if ignore_sorts else "in the order asked"
    check(f"two companies, search answering {label}: the oldest is updated, the other untouched",
          code == 0 and old["properties"].get("signal_score") == "5" and "signal_score" not in new["properties"]
          and len(SIM.companies) == 2)
    check(f"two companies, search answering {label}: the warning names both ids",
          logged(old["id"], "WARNING") and logged(new["id"], "WARNING") and logged("oldest", "WARNING"))
with_properties()
SIM.company({"name": "Upper", "domain": "Upper.Example"})
write_accounts([account("Upper", "upper.example", "5")])
code, _ = run([])
check("an existing company whose domain differs only in case is updated, not doubled",
      code == 0 and len(SIM.companies) == 1 and summary().startswith("HubSpot push: created 0, updated 1"))

# 8. HTTP 429, 5xx and network errors
limited = {"status": "error", "message": "You have reached your ten_secondly_rolling limit.", "errorType": "RATE_LIMIT",
           "correlationId": "sim", "policyName": "TEN_SECONDLY_ROLLING", "requestId": "sim"}
with_properties(planned=[(lambda m, p: p.endswith("/search"),
                          FakeResp(429, limited, {"X-HubSpot-RateLimit-Interval-Milliseconds": "10000"}))])
write_accounts(ROWS[:1])
code, _ = run([])
check("429 on a search: waits the 10-second window, retries, then creates",
      code == 0 and 10 in SLEEPS and len(SIM.sent("POST", "/search")) == 2 and len(SIM.companies) == 1)
with_properties(planned=[(lambda m, p: m == "POST" and p.endswith("/objects/companies"),
                          FakeResp(429, {**limited, "policyName": "SECONDLY"}))])
code, _ = run([])
check("429 on a create is retried, and the company is created once",
      code == 0 and 1 in SLEEPS and len(SIM.sent("POST", "/objects/companies")) == 2 and len(SIM.companies) == 1)
with_properties(planned=[(lambda m, p: True, FakeResp(429, {**limited, "policyName": "DAILY",
                                                               "message": "You have reached your daily limit."}))])
code, _ = run([])
check("the daily limit stops the run at once, without waiting",
      code == 1 and logged("daily", "ERROR") and len(SIM.calls) == 1 and not [s for s in SLEEPS if s >= 1]
      and summary() == "HubSpot push: created 0, updated 0, skipped 0, failed 1.")
with_properties(planned=[(lambda m, p: p.endswith("/search"), FakeResp(429, limited))] * 6)
code, _ = run([])
check("a 429 that never clears stops the run after 5 retries",
      code == 1 and len(SIM.sent("POST", "/search")) == 6 and not SIM.companies and logged("try again later", "ERROR"))
with_properties(planned=[(lambda m, p: p.endswith("/search"), FakeResp(502, "Bad Gateway"))])
code, _ = run([])
check("502 on a search is retried", code == 0 and len(SIM.sent("POST", "/search")) == 2 and len(SIM.companies) == 1)
with_properties(planned=[(lambda m, p: m == "POST" and p.endswith("/objects/companies"), FakeResp(502, "Bad Gateway"))])
write_accounts(ROWS[:3])
code, _ = run([])
check("502 on a create is not retried: that row fails, the rest go on",
      code == 1 and summary() == "HubSpot push: created 2, updated 0, skipped 0, failed 1."
      and len(SIM.sent("POST", "/objects/companies")) == 3 and logged("Not sent again", "ERROR"))


class BrokenOnce(SimSession):                                    # SIMULATED network failure on the first search
    failed = False

    def request(self, method, url, json=None, timeout=None):
        if url.endswith("/search") and not BrokenOnce.failed:
            BrokenOnce.failed = True
            raise ph.requests.ConnectionError("SIMULATED connection reset")
        return super().request(method, url, json, timeout)


with_properties()
write_accounts(ROWS[:1])
code, _ = run([], session=BrokenOnce)
check("a network error on a search is retried", code == 0 and BrokenOnce.failed and len(SIM.companies) == 1)

# 9. a row HubSpot rejects is skipped; the exit code is 1
with_properties(refuse_domains={"bravoai.example"})
write_accounts(ROWS)
code, _ = run([])
check("a rejected row is skipped and the others are written",
      code == 1 and summary() == "HubSpot push: created 2, updated 0, skipped 2, failed 1."
      and not by_domain("bravoai.example") and len(SIM.companies) == 2)
check("the rejected row is named with HubSpot's reason", logged("Bravo AI", "ERROR") and logged("SIMULATED refusal", "ERROR"))
with_properties()
write_accounts([account("Good", "good.example"), account("Bad Score", "badscore.example", "n/a"),
                account("Good Two", "goodtwo.example")])
code, _ = run([])
check("a row with a bad score is not sent, the others are, exit 1",
      code == 1 and summary() == "HubSpot push: created 2, updated 0, skipped 0, failed 1."
      and not by_domain("badscore.example") and logged("Bad Score", "ERROR"))

# 10. missing scopes, a rejected key
for scope, wanted_calls, by_hand in ((SCHEMA_WRITE, 2, True), (SCHEMA_READ, 1, False)):
    sim = fresh(ROWS[:3])
    sim.scopes.discard(scope)
    code, _ = run([])
    check(f"without {scope}: exit 1, the scope is named, nothing is created",
          code == 1 and logged(scope, "ERROR") and not SIM.companies and len(SIM.calls) == wanted_calls
          and summary() == "HubSpot push: created 0, updated 0, skipped 0, failed 3.")
    check(f"without {scope}: the message {'says' if by_hand else 'does not say'} the properties can be made by hand",
          logged("by hand", "ERROR") == by_hand and logged("Service keys", "ERROR")
          and (not by_hand or all(logged(spec["name"], "ERROR") for spec in ph.PROPERTIES)))
for scope, action in ((READ, "search"), (WRITE, "create a company")):
    sim = with_properties()
    sim.scopes.discard(scope)
    write_accounts(ROWS[:3])
    code, _ = run([])
    check(f"without {scope}: the run stops at the first row and names the scope",
          code == 1 and logged(scope, "ERROR") and logged(action, "ERROR") and not SIM.companies
          and len(SIM.sent("POST", "/search")) == 1 and summary().endswith("failed 3."))
with_properties(planned=[(lambda m, p: p.endswith("/search"), FakeResp(403, "Forbidden"))])
code, _ = run([])
check("a 403 without a scope list still names the scope the request needs", code == 1 and logged(READ, "ERROR"))
with_properties()
code, _ = run([], key="another-key")
check("a key HubSpot rejects (401) stops the run with a clear error",
      code == 1 and logged("rejected the key", "ERROR") and len(SIM.calls) == 1 and not SIM.companies)

# 11. the raw log
with_properties()
write_accounts(ROWS)
code, _ = run([])
logs = raw_logs()
record = json.loads(logs[-1].read_text()) if logs else {}
check("one raw log per run, named hubspot_push_<UTC time>.json",
      len(logs) == 1 and re.fullmatch(r"hubspot_push_\d{4}-\d{2}-\d{2}T\d{6}Z(_\d+)?\.json", logs[-1].name))
check("the raw log holds every request with its answer",
      len(record.get("exchanges", [])) == len(SIM.calls) and all(
          {"at", "action", "method", "url", "request_body", "status", "response"} <= set(e) for e in record["exchanges"])
      and record.get("summary") == {"created": 3, "updated": 0, "skipped": 2, "failed": 0})
code, _ = run([])
check("a second run in the same second gets its own raw log", len(raw_logs()) == 2)
everything = "".join(path.read_text() for path in ph.RAW_DIR.iterdir())
check("no key, Authorization header or Bearer token in any file the script wrote",
      KEY not in everything and "Authorization" not in everything and "Bearer" not in everything)
sim = fresh(ROWS[:3])
sim.scopes.discard(SCHEMA_WRITE)
run([])
check("a stopped run still saves its raw log", len(raw_logs()) == 1 and "MISSING_SCOPES" in raw_logs()[0].read_text())

# 12. --limit
with_properties()
write_accounts(ROWS)
code, _ = run(["--limit", "2"])
check("--limit 2 pushes only the first 2 rows",
      code == 0 and summary() == "HubSpot push: created 2, updated 0, skipped 0, failed 0."
      and sorted(c["properties"]["domain"] for c in SIM.companies.values()) == ["alpharobotics.example", "bravoai.example"])
code, _ = run(["--limit", "0"], session=NoNetwork)
check("--limit 0 is refused", str(code).startswith("crashed: SystemExit") and NoNetwork.used == 0)

# 13. older files, properties made by hand
with_properties()
write_accounts(ROWS[:1], [c for c in COLUMNS if c != "signal_tier"])
code, _ = run([])
check("accounts.csv without signal_tier still works; the tier is empty",
      code == 0 and by_domain("alpharobotics.example")[0]["properties"]["signal_tier"] == "")
sim = with_properties()
sim.scopes.discard(SCHEMA_WRITE)                                 # properties made by hand, key without the write scope
write_accounts(ROWS[:3])
code, _ = run([])
check("with the properties made by hand, the schema write scope is not needed",
      code == 0 and len(SIM.companies) == 3 and not SIM.sent("POST", "/properties/companies"))
sim = with_properties()
sim.properties["signal_score"] = {**sim.properties["signal_score"], "type": "string", "fieldType": "text"}
code, _ = run([])
check("a hand-made property of another type is reported", logged("signal_score exists with type string", "WARNING"))
def made_meanwhile(method, path):                                # SIMULATED: someone creates signal_score first
    hit = method == "POST" and path.endswith("/properties/companies")
    if hit:
        SIM.properties["signal_score"] = {**ph.PROPERTIES[0], "groupName": "companyinformation"}
    return hit


fresh(ROWS[:1], planned=[(made_meanwhile, SimHubSpot.error(409, "OBJECT_ALREADY_EXISTS", "SIMULATED: made a moment ago"))])
code, _ = run([])
check("HTTP 409 on creating a property counts as 'already there' and the run goes on",
      code == 0 and logged("already exists (HTTP 409)")
      and len(SIM.sent("POST", "/properties/companies")) == len(ph.PROPERTIES) == 8
      and summary() == "HubSpot push: created 1, updated 0, skipped 0, failed 0.")

print(f"\n{sum(results)}/{len(results)} checks passed")
if all(results):
    shutil.rmtree(tmp, ignore_errors=True)
sys.exit(0 if all(results) else 1)
