"""Offline checks for fetch_jobs.py. Uses SIMULATED API responses only; never calls the network.

Run: python tests/check_fetch_jobs.py
"""
import contextlib, io, json, logging, os, shutil, sys, tempfile
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
import fetch_jobs as fj
import pandas as pd

tmp = Path(tempfile.mkdtemp(prefix="sim_jobs_"))
fj.RAW_DIR = tmp / "raw"; fj.JOBS_CSV = tmp / "jobs.csv"      # keep the project's data/ untouched
fj.build_accounts.FUNDING_CSV = tmp / "funding.csv"; fj.build_accounts.RAW_DIR = tmp / "sec_raw"
fj.build_accounts.REVIEW_DECISIONS_CSV = tmp / "manual" / "review_decisions.csv"  # never the reviewer's file
fj.time.sleep = lambda s: SLEEPS.append(s)                      # no real waiting
fj.load_dotenv = lambda *a, **k: False                          # never load a real .env
os.environ.pop("THEIRSTACK_API_KEY", None)
class NoNetwork:                                                # any real request is a failure
    def __init__(self, *a, **k): self.headers = {}
    def post(self, *a, **k): raise AssertionError("network used")
    def get(self, *a, **k): raise AssertionError("network used")
fj.requests.Session = NoNetwork

SLEEPS, results = [], []
def check(name, cond):
    results.append(bool(cond)); print("PASS" if cond else "FAIL", "-", name)

# 1. role categories
cases = {"Senior AI Engineer": "ai", "Applied AI Engineer": "applied_ai", "Engineer, Applied AI": "applied_ai",
         "Machine Learning Engineer, Ranking": "ml", "ML Engineer": "ml", "AI/ML Engineer": "ml",
         "Forward Deployed Engineer": "fde", "Forward-Deployed Software Engineer, AI": "fde",
         "Robotics Software Engineer": "robotics", "HTML Engineer": "other", "AI Researcher": "other",
         "Robotic Process Automation Engineer": "other", "LLM Engineer": "ai"}
for title, want in cases.items():
    check(f"classify {title!r} -> {want}", fj.classify_role(title) == want)

# 2. HQ ZIP check
check("94107 -> bay_area", fj.hq_zip_check("94107") == "bay_area")
check("94025 as a number -> bay_area", fj.hq_zip_check(94025) == "bay_area")
check("08837 (Menlo Park, NJ) -> outside", fj.hq_zip_check("08837") == "outside")
check("None -> missing", fj.hq_zip_check(None) == "missing")

# fake HTTP pieces
class FakeResp:
    def __init__(self, status, body, headers=None):
        self.status_code, self._body, self.headers = status, body, headers or {}
        self.ok = 200 <= status < 300; self.text = str(body)
    def json(self): return self._body
    def raise_for_status(self):
        if not self.ok: raise fj.requests.HTTPError(f"HTTP {self.status_code}")
class FakeSession:
    def __init__(self, responses): self.responses, self.bodies = list(responses), []
    def post(self, url, json, timeout): self.bodies.append(json); return self.responses.pop(0)
def job(i, posted="2026-11-10", zip_code="94107", title="AI Engineer"):
    return {"id": i, "job_title": title, "date_posted": posted,
            "company_object": {"id": f"c{i}", "name": f"SimCo {i}", "domain": f"simco{i}.example", "city": "San Francisco", "postal_code": zip_code}}

base = fj.base_body(14, None)
key5 = fj.run_key(base, 5)

# 3. 429 then success; fewer results than asked -> stop
s = FakeSession([FakeResp(429, {"error": "per-minute limit"}, {"RateLimit-Reset": "12"}),
                 FakeResp(200, {"metadata": {"total_results": 3, "total_companies": 3},
                                "data": [job(1), job(2, zip_code="08837"), job(3, title="HTML Engineer")]})])
got = fj.fetch_new_jobs(s, base, key5, 5, set())
check("429 is retried once, then succeeds", len(s.bodies) == 2)
check("waited for RateLimit-Reset (12s)", SLEEPS[-1] == 12)
check("stops when fewer jobs than asked come back", got == 3)
check("first request asks for totals", s.bodies[0]["include_total_results"] is True)
check("first request limit is 5, not 25", s.bodies[0]["limit"] == 5)
check("response saved to raw before use", len(list(fj.RAW_DIR.glob("theirstack_jobs_*.json"))) == 1)
check("API key never stored in raw files", all("Bearer" not in p.read_text() for p in fj.RAW_DIR.glob("*.json")))

# 4. two pages: 25 then 5, known ids excluded, totals only on first request
SLEEPS.clear()
s = FakeSession([FakeResp(200, {"data": [job(100 + i) for i in range(25)]}),
                 FakeResp(200, {"data": [job(200 + i) for i in range(5)]})])
known = {1, 2, 3}
got = fj.fetch_new_jobs(s, base, fj.run_key(base, 30), 30, known)
check("page sizes are 25 then 5", [b["limit"] for b in s.bodies] == [25, 5])
check("both requests ask for page 0", [b["page"] for b in s.bodies] == [0, 0])
check("known ids excluded from the first request", {1, 2, 3} <= set(s.bodies[0]["job_id_not"]))
check("second request excludes the first page's ids", set(range(100, 125)) <= set(s.bodies[1]["job_id_not"]))
check("totals only on the first request", s.bodies[1]["include_total_results"] is False)
check("7 seconds between requests", SLEEPS == [7])

# 5. guards
s = FakeSession([FakeResp(200, {"data": [job(1)]})])
try: fj.fetch_new_jobs(s, base, "dupkey", 5, {1}); check("stops if the API returns a job already received", False)
except fj.ApiError: check("stops if the API returns a job already received", True)
SLEEPS.clear()
s = FakeSession([FakeResp(429, {"error": "hourly limit"}, {"RateLimit-Reset": "3600"})])
try: fj.fetch_new_jobs(s, base, "waitkey", 5, set()); check("refuses to wait an hour", False)
except fj.ApiError: check("refuses to wait an hour (no sleep)", SLEEPS == [])
s = FakeSession([FakeResp(401, {"detail": "Invalid API key"})])
try: fj.fetch_new_jobs(s, base, "authkey", 5, set()); check("HTTP 401 stops with a clear error", False)
except fj.ApiError as e: check("HTTP 401 stops with a clear error", "401" in str(e))

# 6. same parameters, same day -> cache reused, no network, no key needed
code = fj.main(["--limit", "5"])
check("re-run with same parameters returns 0 without network or key", code == 0)
table = pd.read_csv(fj.JOBS_CSV)
check("jobs.csv has the planned columns", list(table.columns) == fj.COLUMNS)
check("one row per job id", table["job_id"].is_unique)
check("NJ ZIP flagged outside", table.loc[table.job_id == 2, "hq_zip_check"].item() == "outside")
check("HTML Engineer labelled other", table.loc[table.job_id == 3, "role_category"].item() == "other")

# 7. new parameters and no key -> clear error, nothing sent
code = fj.main(["--limit", "7"])
check("new parameters without a key exit 1 before any request", code == 1)

# 8. a corrupt cache file is skipped, not fatal
(fj.RAW_DIR / "theirstack_jobs_2000-01-01T000000Z_bad_p00.json").write_text("{not json")
check("corrupt cache file skipped", len(fj.build_jobs_table()) == len(table))

# 9. credits report. Live balance on 2026-11-16: api_credits stayed 200 while used_api_credits
#    went 0 -> 5, so api_credits is the cycle's allowance and the cost is the change in used.
class Collect(logging.Handler):
    def __init__(self): super().__init__(); self.lines = []
    def emit(self, record): self.lines.append((record.levelname, record.getMessage()))
h = Collect(); fj.log.addHandler(h); fj.log.setLevel(logging.INFO)
fj.report_credits({"api_credits": 200, "used_api_credits": 0}, {"api_credits": 200, "used_api_credits": 5}, 5)
check("credits report: 5 used by the run, 195 of 200 left",
      any("used by this run: 5." in m and "195 of 200" in m for _, m in h.lines))
check("credits report: no warning when credits used equal jobs received",
      not any(level == "WARNING" for level, _ in h.lines))
h.lines.clear()
fj.report_credits({"api_credits": 200, "used_api_credits": 0}, {"api_credits": 200, "used_api_credits": 7}, 5)
check("credits report warns when credits used differ from jobs received",
      any(level == "WARNING" and "differ" in m for level, m in h.lines))
h.lines.clear()
fj.report_credits(None, {"api_credits": 200, "used_api_credits": 5}, 5)
check("credits report warns when a balance read failed",
      any(level == "WARNING" and "unavailable" in m for level, m in h.lines))
fj.log.removeHandler(h)

# 10. --funded-companies: only Form D companies that can become accounts are searched
check("no company filter unless asked, so earlier run keys stay the same",
      "company_name_case_insensitive_or" not in fj.base_body(14, None))
check("company names change the run key",
      fj.run_key(fj.base_body(30, None, ["A"]), 1) != fj.run_key(fj.base_body(30, None, ["B"]), 1))
check("--funded-companies without funding.csv exits 1 before any request",
      fj.main(["--funded-companies", "--days", "30", "--limit", "1"]) == 1)
from fetch_funding import COLUMNS as FUNDING_COLUMNS  # noqa: E402
def sim_filing(n, issuer, sold):  # SIMULATED Form D row
    row = dict.fromkeys(FUNDING_COLUMNS, "")
    row.update({"accession_number": f"0000000000-26-{n:06d}", "cik": str(n), "issuer_name": issuer,
                "state": "CA", "zip_code": "94107", "hq_zip_check": "bay_area", "industry_excluded": "False",
                "amount_sold_usd": sold, "stage_estimate": "Seed", "first_sale_date": "2026-11-01"})
    return row
pd.DataFrame([sim_filing(1, "SimCo Labs, Inc.", "2000000"), sim_filing(2, "Tiny Sim Inc.", "5000")],
             columns=FUNDING_COLUMNS).to_csv(fj.build_accounts.FUNDING_CSV, index=False)
out = io.StringIO()
with contextlib.redirect_stdout(out):
    code = fj.main(["--funded-companies", "--days", "30", "--limit", "1", "--dry-run"])
check("dry run shows the name count, not every name",
      code == 0 and "<2 names, e.g. SimCo, SimCo Labs>" in out.getvalue())
class RecordingSession:                                          # SIMULATED API: balance and 0 jobs
    last = None
    def __init__(self, *a, **k): self.headers, self.bodies = {}, []; RecordingSession.last = self
    def get(self, url, timeout): return FakeResp(200, {"api_credits": 200, "used_api_credits": 0})
    def post(self, url, json, timeout):
        self.bodies.append(json)
        return FakeResp(200, {"metadata": {"total_results": 0, "total_companies": 0}, "data": []})
fj.requests.Session = RecordingSession
os.environ["THEIRSTACK_API_KEY"] = "sim-key-not-real"
code = fj.main(["--funded-companies", "--days", "30", "--limit", "1"])
os.environ.pop("THEIRSTACK_API_KEY"); fj.requests.Session = NoNetwork
body = RecordingSession.last.bodies[0] if RecordingSession.last and RecordingSession.last.bodies else {}
check("funded search sends only the spellings of the company that can become an account",
      code == 0 and body.get("company_name_case_insensitive_or") == ["SimCo", "SimCo Labs"])
check("funded search keeps the other filters and uses the 30-day window",
      body.get("posted_at_max_age_days") == 30 and body.get("company_type") == "direct_employer"
      and body.get("company_location_pattern_or") == fj.CONFIG["hq_cities"])

# 11. --any-hq-city and the columns added for step 3
body = fj.base_body(30, None, ["A"], any_hq_city=True)
check("--any-hq-city drops only the HQ city filter",
      "company_location_pattern_or" not in body and body["company_name_case_insensitive_or"] == ["A"]
      and body["max_employee_count_or_null"] == fj.CONFIG["max_employees"])
check("--any-hq-city changes the run key", fj.run_key(body, 1) != fj.run_key(fj.base_body(30, None, ["A"]), 1))
with contextlib.redirect_stderr(io.StringIO()):
    try:
        fj.parse_args(["--limit", "1", "--any-hq-city"])
        refused = False
    except SystemExit:
        refused = True
check("--any-hq-city without --funded-companies is refused", refused)
fj.requests.Session = RecordingSession
os.environ["THEIRSTACK_API_KEY"] = "sim-key-not-real"
code = fj.main(["--funded-companies", "--any-hq-city", "--days", "30", "--limit", "1"])
os.environ.pop("THEIRSTACK_API_KEY"); fj.requests.Session = NoNetwork
body = RecordingSession.last.bodies[0] if RecordingSession.last and RecordingSession.last.bodies else {}
check("--any-hq-city run sends the company names without the HQ city filter",
      code == 0 and "company_location_pattern_or" not in body
      and body.get("company_name_case_insensitive_or") == ["SimCo", "SimCo Labs"])
check("a forward deployed title is marked as one in its row, and another title is not",
      fj.job_to_row({"id": 7, "job_title": "Senior Forward-Deployed Engineer"}, "2026-11-16T00:00:00Z")["is_fde"] is True
      and fj.job_to_row({"id": 8, "job_title": "AI Engineer"}, "2026-11-16T00:00:00Z")["is_fde"] is False)
row = fj.job_to_row({"id": 9, "job_title": "AI Engineer",
                     "final_url": "https://jobs.example.com/acme/123/?utm_source=x#apply",
                     "min_annual_salary_usd": 150000.0, "max_annual_salary_usd": 200000.0,
                     "company_object": {"name": "Acme", "num_jobs_last_30_days": 42, "industry": "Robotics",
                                        "seo_description": "  Robots   for everyone. ", "long_description": "Long  text"}},
                    "2026-11-16")
check("posting_url drops the query string, fragment and final slash",
      row["posting_url"] == "https://jobs.example.com/acme/123")
check("annual salary range kept", (row["salary_min_usd"], row["salary_max_usd"]) == (150000.0, 200000.0))
check("the company's job count for the last 30 days kept", row["theirstack_jobs_last_30_days"] == 42)
check("short description preferred, spaces tidied", row["company_description"] == "Robots for everyone.")
check("industry and the start of the long description kept for step 3's review hints",
      (row["company_industry"], row["company_long_description"]) == ("Robotics", "Long text"))
row = fj.job_to_row({"id": 10, "job_title": "AI Engineer", "company_object": {"long_description": "x" * 400}},
                    "2026-11-16")
check("a long description is cut to 300 characters",
      len(row["company_description"]) == 300 and row["company_description"].endswith("…"))
check("no URL -> empty posting_url", row["posting_url"] == "")

# 12. hiring-first search: core titles, rejected names, one credit per new company
body = fj.base_body(14, None, core_roles=True, excluded_names=["Zeta", "Acme"])
check("--core-roles sends only the core titles", body["job_title_or"] == fj.CONFIG["core_title_keywords"])
check("rejected company names are excluded", body.get("company_name_not") == ["Acme", "Zeta"])
check("no exclusion field unless a name is rejected", "company_name_not" not in fj.base_body(14, None))
check("one-per-company changes the run key", fj.run_key(base, 5, True) != fj.run_key(base, 5))
def company_job(i, company_id):                                  # SIMULATED job at a given company
    record = job(i); record["company_object"]["id"] = company_id; return record
SLEEPS.clear()
s = FakeSession([FakeResp(200, {"metadata": {"total_results": 9, "total_companies": 2}, "data": [company_job(301, "co-a")]}),
                 FakeResp(200, {"data": [company_job(302, "co-b")]}),
                 FakeResp(200, {"data": []})])
companies = {"co-known"}
got = fj.fetch_new_jobs(s, base, "onekey", 5, set(), known_companies=companies)
check("one-per-company asks for one job per request", [b["limit"] for b in s.bodies] == [1, 1, 1])
check("companies already received are left out, and each new one is added",
      [b.get("company_id_not") for b in s.bodies] == [["co-known"], ["co-a", "co-known"], ["co-a", "co-b", "co-known"]])
check("stops when no new company is left: 2 jobs for 2 credits",
      got == 2 and companies == {"co-known", "co-a", "co-b"})
s = FakeSession([FakeResp(200, {"data": [company_job(303, "co-c")]})])
fj.fetch_new_jobs(s, base, "plainkey", 5, set())
check("without the option no company filter is sent, and pages hold up to 5",
      "company_id_not" not in s.bodies[0] and s.bodies[0]["limit"] == 5)
check("companies already received are read from the cache", {"co-a", "co-b", "co-c"} <= fj.known_company_ids())
fj.build_accounts.REVIEW_DECISIONS_CSV.parent.mkdir(parents=True, exist_ok=True)
fj.build_accounts.REVIEW_DECISIONS_CSV.write_text("company_name,form_d_issuer,decision,reason\n"
                                                  "BadCo,,reject,SIMULATED\nOkCo,OkCo Inc.,accept,SIMULATED\n")
out = io.StringIO()
with contextlib.redirect_stdout(out):
    code = fj.main(["--core-roles", "--one-per-company", "--days", "14", "--limit", "3", "--dry-run"])
preview = json.loads(out.getvalue()) if code == 0 else {}
check("dry run: core titles, the rejected name left out, one job per request",
      preview.get("job_title_or") == fj.CONFIG["core_title_keywords"] and preview.get("company_name_not") == ["BadCo"]
      and preview.get("limit") == 1 and "companies already received" in str(preview.get("company_id_not")))
fj.build_accounts.REVIEW_DECISIONS_CSV.write_text("company_name,decision\nBadCo,reject\n")
check("a malformed decisions file stops the run before any request",
      fj.main(["--core-roles", "--days", "14", "--limit", "3", "--dry-run"]) == 1)
fj.build_accounts.REVIEW_DECISIONS_CSV.unlink()

print(f"\n{sum(results)}/{len(results)} checks passed")
if all(results):
    shutil.rmtree(tmp, ignore_errors=True)
sys.exit(0 if all(results) else 1)
