"""Offline checks for fetch_funding.py and bay_area.py.
Uses SIMULATED SEC responses only; any real network call fails the run.

Run: python tests/check_fetch_funding.py
"""
import json, logging, os, shutil, sys, tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
import bay_area, fetch_jobs, fetch_funding as ff
import pandas as pd
import requests

tmp = Path(tempfile.mkdtemp(prefix="sim_funding_"))
LOG = tmp / "run.log"
logging.basicConfig(filename=LOG, level=logging.INFO, format="%(levelname)s %(message)s")
TODAY = datetime.now(ZoneInfo("America/New_York")).date()   # EDGAR's calendar day
AGENT = "Sim Tester sim@simmail.test"

def use_dirs(name):
    """Point the script at a fresh simulated data folder; the project's data/ is never touched."""
    base = tmp / name
    ff.RAW_DIR = base / "raw"; ff.XML_DIR = ff.RAW_DIR / "sec_formd"; ff.FUNDING_CSV = base / "funding.csv"
    return base

CLOCK, SLEEPS = [1000.0], []                      # fake clock: sleeping advances it
def fake_sleep(s): SLEEPS.append(s); CLOCK[0] += s
ff.time.sleep = fake_sleep
ff.time.monotonic = lambda: CLOCK[0]
ff.load_dotenv = lambda *a, **k: False            # never read a real .env

def acc(i): return f"{9990000000 + i:010d}-26-{i:06d}"
def folder(a): return a.replace("-", "")

def form_d_xml(a, name="SimCo AI, Inc.", zip_code="94107", industry="Other Technology", first_sale="2026-11-01",
               offering="20000000", sold="18500000", investors="12",
               year="<withinFiveYears>true</withinFiveYears><value>2023</value>",
               persons="<relatedPersonInfo><relatedPersonName><firstName>Ada</firstName><lastName>Sim</lastName>"
                       "</relatedPersonName><relatedPersonRelationshipList><relationship>Executive Officer</relationship>"
                       "<relationship>Director</relationship></relatedPersonRelationshipList>"
                       "<relationshipClarification>CEO</relationshipClarification></relatedPersonInfo>"):
    sale = f"<value>{first_sale}</value>" if first_sale else "<yetToOccur>true</yetToOccur>"
    return f"""<?xml version="1.0"?>
<edgarSubmission><schemaVersion>X0708</schemaVersion><submissionType>D</submissionType><testOrLive>LIVE</testOrLive>
<primaryIssuer><cik>{a[:10]}</cik><entityName>{name}</entityName><issuerAddress><street1>1 Sim St</street1>
<city>SAN FRANCISCO</city><stateOrCountry>CA</stateOrCountry><zipCode>{zip_code}</zipCode></issuerAddress>
<jurisdictionOfInc>DELAWARE</jurisdictionOfInc><entityType>Corporation</entityType><yearOfInc>{year}</yearOfInc></primaryIssuer>
<relatedPersonsList>{persons}</relatedPersonsList>
<offeringData><industryGroup><industryGroupType>{industry}</industryGroupType></industryGroup>
<typeOfFiling><newOrAmendment><isAmendment>false</isAmendment></newOrAmendment><dateOfFirstSale>{sale}</dateOfFirstSale></typeOfFiling>
<offeringSalesAmounts><totalOfferingAmount>{offering}</totalOfferingAmount><totalAmountSold>{sold}</totalAmountSold>
<totalRemaining>0</totalRemaining></offeringSalesAmounts>
<investors><hasNonAccreditedInvestors>false</hasNonAccreditedInvestors><totalNumberAlreadyInvested>{investors}</totalNumberAlreadyInvested></investors>
</offeringData></edgarSubmission>""".encode()

class FakeResp:
    def __init__(self, status=200, body=None, content=None, headers=None):
        self.status_code, self.headers, self._body = status, headers or {}, body
        self.content = content if content is not None else (json.dumps(body).encode() if body is not None else b"")
        self.text = self.content.decode("utf-8", "replace"); self.ok = status < 400
    def json(self):
        if self._body is None: raise ValueError("not JSON")
        return self._body

def hit(a, form="D"):
    # Shape seen live on 2026-11-15: an amendment has form and file_type "D/A" but root_forms ["D"].
    return {"_index": "edgar_file", "_id": f"{a}:primary_doc.xml", "_score": None, "sort": [0, a],
            "_source": {"ciks": [a[:10]], "file_date": "2026-11-13", "form": form, "file_type": form,
                        "adsh": a, "root_forms": ["D"]}}
def search(total, accs, relation="eq", amended=()):
    return FakeResp(200, {"took": 1, "timed_out": False,
                          "hits": {"total": {"value": total, "relation": relation}, "max_score": None,
                                   "hits": [hit(a, "D/A" if a in amended else "D") for a in accs]}})
def new_log_lines(since):
    """Log lines written after byte offset `since`."""
    return LOG.read_text()[since:].splitlines()

class FakeSession:
    """Answers SEC URLs from scripted queues and records every call with the fake time."""
    def __init__(self, searches, xml=None):
        self.headers, self.calls, self.searches, self.xml = {}, [], list(searches), dict(xml or {})
    def get(self, url, params=None, timeout=None):
        self.calls.append((url, dict(params or {}), CLOCK[0])); CLOCK[0] += 0.1   # each request takes 0.1 s
        if url == ff.SEARCH_URL:
            item = self.searches.pop(0)
        else:
            f = url.split("/")[-2]; a = f"{f[:10]}-{f[10:12]}-{f[12:]}"
            queue = self.xml.get(a)
            item = queue.pop(0) if queue else FakeResp(200, content=form_d_xml(a))
        if isinstance(item, Exception): raise item
        return item
    def split(self):
        return ([c for c in self.calls if c[0] == ff.SEARCH_URL], [c for c in self.calls if c[0] != ff.SEARCH_URL])

def run(session, argv):
    ff.requests.Session = lambda: session
    return ff.main(argv)
def csv(): return pd.read_csv(ff.FUNDING_CSV, dtype=str, keep_default_na=False)

results = []
def check(name, cond): results.append(bool(cond)); print("PASS" if cond else "FAIL", "-", name)

# 1. one ZIP rule for both scripts
check("fetch_jobs uses the shared ZIP rule", fetch_jobs.hq_zip_check is bay_area.hq_zip_check)
check("fetch_funding uses the shared ZIP rule", ff.hq_zip_check is bay_area.hq_zip_check)
check("ZIP+4 94107-1234 -> bay_area", bay_area.hq_zip_check("94107-1234") == "bay_area")
check("Glendale 91206 -> outside", bay_area.hq_zip_check("91206") == "outside")
check("blank ZIP -> missing", bay_area.hq_zip_check("  ") == "missing")
check("Los Angeles, San Diego, Fresno, Sacramento, New York and Seattle ZIP codes -> outside",
      [bay_area.hq_zip_check(code) for code in ("90012", "92101", "93721", "95814", "10001", "98101")] == ["outside"] * 6)
check("San Francisco, Palo Alto, Oakland, San Jose and Santa Rosa ZIP codes -> bay_area",
      [bay_area.hq_zip_check(code) for code in ("94105", "94301", "94612", "95113", "95401")] == ["bay_area"] * 5)
other_file = hit("0009990000-26-000099")
other_file["_id"] = other_file["_id"].replace("primary_doc.xml", "primary_doc.html")
check("a search hit whose document is not an XML file is not read as a filing",
      ff.hit_to_filing(hit("0009990000-26-000099")) is not None and ff.hit_to_filing(other_file) is None)

# 2. stage estimate cut-offs
for amount, want in {None: "Unknown", 0: "Unknown", 4_999_999: "Seed", 5_000_000: "Series A",
                     24_999_999: "Series A", 25_000_000: "Series B", 75_000_000: "Series C",
                     199_999_999: "Series C", 200_000_000: "Later"}.items():
    check(f"stage_estimate({amount}) -> {want}", ff.stage_estimate(amount) == want)

# 3. industry flag
for group, want in {"Pooled Investment Fund": True, "REITS & Finance": True, "REITS and Finance": True,
                    " pooled  investment FUND ": True, "Commercial Banking": True, "Insurance": True,
                    "Other Technology": False, "Construction": False, "Other": False, None: False}.items():
    check(f"industry_excluded({group!r}) -> {want}", ff.industry_excluded(group) == want)

# 4. User-Agent guard; the placeholder is read from the real .env.example
placeholder = next(l.split("=", 1)[1] for l in (PROJECT / ".env.example").read_text().splitlines()
                   if l.startswith("SEC_USER_AGENT="))
check("empty User-Agent refused", ff.user_agent_problem("") is not None)
check(f".env.example placeholder {placeholder!r} refused", ff.user_agent_problem(placeholder) is not None)
check("email without a name refused", ff.user_agent_problem("sim@simmail.test") is not None)
check("name and email accepted", ff.user_agent_problem(AGENT) is None)

use_dirs("no_agent")
s = FakeSession([])
os.environ.pop("SEC_USER_AGENT", None)
check("no SEC_USER_AGENT: exit 1", run(s, ["--limit", "5"]) == 1)
os.environ["SEC_USER_AGENT"] = placeholder
check("placeholder SEC_USER_AGENT: exit 1", run(s, ["--limit", "5"]) == 1)
check("... and not a single request sent", s.calls == [])
os.environ["SEC_USER_AGENT"] = AGENT

# 5. the planned test run: --limit 5 sends 1 search and 5 XML requests
base = use_dirs("limit5")
page1 = [acc(i) for i in range(1, 101)]
page2 = [acc(i) for i in range(100, 151)]          # acc(100) repeats across pages
s = FakeSession([search(150, page1)])
code = run(s, ["--limit", "5"])
searches, xmls = s.split()
check("--limit 5: exit 0", code == 0)
check("--limit 5: exactly 1 search request", len(searches) == 1)
check("--limit 5: exactly 5 XML requests, newest first", [c[0].split("/")[-2] for c in xmls] == [folder(acc(i)) for i in range(1, 6)])
p = searches[0][1]
check("search: forms=D, plural locationCodes=CA, from=0", p["forms"] == "D" and p["locationCodes"] == "CA" and "locationCode" not in p and p["from"] == 0)
check("search: 90-day window ending today (Eastern time)", p["enddt"] == TODAY.isoformat() and p["startdt"] == (TODAY - timedelta(days=90)).isoformat())
check("User-Agent header set from SEC_USER_AGENT", s.headers.get("User-Agent") == AGENT)
starts = [c[2] for c in s.calls]
check("requests start at least 0.5 s apart", all(b - a >= 0.5 - 1e-9 for a, b in zip(starts, starts[1:])))
t = csv()
check("funding.csv has the planned columns", list(t.columns) == ff.COLUMNS)
check("funding.csv has 5 rows", len(t) == 5)
check("search page saved to data/raw", len(list(ff.RAW_DIR.glob("sec_search_*_from00000.json"))) == 1)
check("5 XMLs cached in data/raw/sec_formd", len(list(ff.XML_DIR.glob("*.xml"))) == 5)
check("no half-written .part files", not list(base.rglob("*.part")))
check("User-Agent not stored in raw files", all("simmail" not in f.read_text() for f in ff.RAW_DIR.glob("*.json")))

# 6. row contents
r = t.set_index("accession_number").loc[acc(1)]
check("issuer name", r["issuer_name"] == "SimCo AI, Inc.")
check("ZIP and Bay Area flag", r["zip_code"] == "94107" and r["hq_zip_check"] == "bay_area")
check("first sale 2026-11-01, filed 2026-11-13 -> 12 days", r["first_sale_date"] == "2026-11-01" and r["days_first_sale_to_filing"] == "12")
check("amounts written as whole numbers", r["total_offering_usd"] == "20000000" and r["amount_sold_usd"] == "18500000")
check("$18.5M sold -> Series A estimate", r["stage_estimate"] == "Series A")
check("investor count and year of incorporation", r["investor_count"] == "12" and r["year_of_inc"] == "2023")
check("related persons with roles", r["related_persons"] == "Ada Sim (Executive Officer, Director - CEO)")
check("filing URL is the EDGAR index page", r["filing_url"] == f"https://www.sec.gov/Archives/edgar/data/9990000001/{folder(acc(1))}/{acc(1)}-index.htm")
check("industry not excluded", r["industry_excluded"] == "False")
check("fetched_at is the download time", r["fetched_at"].endswith("Z") and r["fetched_at"][:4] == str(TODAY.year))

# 7. odd values: no first sale yet, "Indefinite" offering, over five years old, a fund outside the Bay Area
odd = tmp / f"2026-11-15T000000Z_{acc(7)}.xml"
odd.write_bytes(form_d_xml(acc(7), first_sale=None, offering="Indefinite", sold="0", zip_code="91206",
                           year="<overFiveYears>true</overFiveYears>", industry="Pooled Investment Fund", persons=""))
row = ff.filing_row({"accession_number": acc(7), "cik": "9990000007", "filing_date": "2026-11-13", "file_name": "primary_doc.xml"}, odd)
check("first sale yet to occur -> no date, no days", row["first_sale_date"] is None and row["days_first_sale_to_filing"] is None)
check("'Indefinite' offering -> empty", row["total_offering_usd"] is None)
check("$0 sold -> Unknown stage", row["stage_estimate"] == "Unknown")
check("over five years -> text answer", row["year_of_inc"] == "over five years ago")
check("pooled fund flagged, Glendale outside", row["industry_excluded"] is True and row["hq_zip_check"] == "outside")
check("no related persons -> empty", row["related_persons"] == "")

# 8. full run pages through, removes the repeated hit, reuses the 5 cached XMLs
s = FakeSession([search(150, page1), search(150, page2)])
code = run(s, [])
searches, xmls = s.split()
t = csv()
check("full run: exit 0", code == 0)
check("full run: 2 search pages, from=0 then from=100", [c[1]["from"] for c in searches] == [0, 100])
check("full run: 145 XML requests (5 already cached)", len(xmls) == 145)
check("full run: 150 rows, one per filing", len(t) == 150 and t["accession_number"].is_unique)

# 9. same-day re-run: searches again, downloads nothing
s = FakeSession([search(150, page1), search(150, page2)])
code = run(s, [])
check("re-run: 0 XML requests, same 150 rows", code == 0 and len(s.split()[1]) == 0 and len(csv()) == 150)

# 10. retries: Retry-After honored, then backoff after a dropped connection
use_dirs("retry")
SLEEPS.clear()
s = FakeSession([FakeResp(429, {"error": "slow down"}, headers={"Retry-After": "12"}),
                 requests.ConnectionError("simulated drop"), search(2, [acc(1), acc(2)])])
code = run(s, [])
check("429 and a dropped connection are retried, then the run succeeds", code == 0 and len(s.split()[0]) == 3 and len(csv()) == 2)
check("waited 12 s (Retry-After), then 10 s (backoff)", SLEEPS[:2] == [12, 10])

# 11. HTTP 403 stops at once
use_dirs("blocked")
s = FakeSession([search(3, [acc(1), acc(2), acc(3)])],
                xml={acc(2): [FakeResp(403, content=b"<html>Request Rate Threshold Exceeded</html>")]})
code = run(s, [])
searches, xmls = s.split()
check("403: exit 1, nothing sent after the 403", code == 1 and len(xmls) == 2 and folder(acc(2)) in s.calls[-1][0])
check("403: the filing downloaded before it is still written", len(csv()) == 1)

# 12. a server that keeps failing: 6 attempts, then stop; an empty funding.csv is still written
use_dirs("down")
SLEEPS.clear()
s = FakeSession([search(2, [acc(1), acc(2)])], xml={acc(1): [FakeResp(503)] * 6})
code = run(s, [])
check("503 six times: exit 1 after 6 attempts", code == 1 and len(s.split()[1]) == 6)
check("backoff 5, 10, 20, 40, 80 s", [w for w in SLEEPS if w >= 5] == [5, 10, 20, 40, 80])
check("empty funding.csv keeps its header", ff.FUNDING_CSV.exists() and list(csv().columns) == ff.COLUMNS and len(csv()) == 0)

# 13. bad records are skipped, not fatal
use_dirs("bad_records")
four = [acc(i) for i in range(1, 5)]
s = FakeSession([search(4, four)], xml={acc(2): [FakeResp(404, content=b"Not Found")],
                                         acc(3): [FakeResp(200, content=b"<html>maintenance</html>")]})
code = run(s, [])
check("404 and a non-XML page are skipped; exit 0", code == 0)
check("only the 2 good filings are cached and written", len(csv()) == 2 and len(list(ff.XML_DIR.glob("*.xml"))) == 2)
(ff.XML_DIR / f"2000-01-01T000000Z_{acc(2)}.xml").write_bytes(b"<edgarSubmission><broken")
s = FakeSession([search(4, four)], xml={acc(3): [FakeResp(200, content=b"<html>maintenance</html>")]})
code = run(s, [])
check("a corrupt cached XML is skipped; exit 0, still 2 rows", code == 0 and len(csv()) == 2)
check("the skipped filing is tried again on the next run", any(folder(acc(3)) in c[0] for c in s.calls))

# 14. hits without the expected fields are skipped with a warning; an amendment is not a bad hit
use_dirs("bad_hits")
page = search(4, [acc(1)])
page._body["hits"]["hits"] += [{"_id": "junk"}, hit(acc(8), form="D/A"),
                               {"_id": f"{acc(9)}:primary_doc.xml", "_source": {"ciks": [], "file_date": "2026-11-13", "form": "D"}},
                               hit(acc(10), form="10-K")]
mark = len(LOG.read_text())
code = run(FakeSession([page]), [])
lines = new_log_lines(mark)
check("junk, CIK-less and non-Form-D hits skipped; the good one kept", code == 0 and len(csv()) == 1)
check("... a warning for each of those 3, none for the D/A", sum("Skipping a search hit" in l for l in lines) == 3)

# 14b. amendments (D/A): SEC's forms=D search returns them too; counted and skipped, never downloaded
use_dirs("amendments")
seven = [acc(i) for i in range(1, 8)]
s = FakeSession([search(7, seven, amended={acc(2), acc(4)})])
mark = len(LOG.read_text())
code = run(s, ["--limit", "3"])
lines = new_log_lines(mark)
check("--limit 3 with D/A hits: 3 XML requests, new notices only",
      code == 0 and [c[0].split("/")[-2] for c in s.split()[1]] == [folder(acc(i)) for i in (1, 3, 5)])
check("... no per-hit warning for an amendment", not any("Skipping a search hit" in l for l in lines))
check("... one line counts kept and skipped", any("5 new notices (Form D) kept, 2 amendments (D/A) skipped" in l for l in lines))
check("... the SEC count is labeled as D and D/A", any("7 Form D and D/A filings" in l for l in lines))
s = FakeSession([search(7, seven, amended={acc(2), acc(4)})])
code = run(s, [])
t = csv()
check("full run: the 2 missing new notices downloaded, no D/A",
      code == 0 and [c[0].split("/")[-2] for c in s.split()[1]] == [folder(acc(i)) for i in (6, 7)])
check("full run: 5 rows, no D/A accession in funding.csv",
      len(t) == 5 and not ({acc(2), acc(4)} & set(t["accession_number"])))

# 14c. the window follows EDGAR's Eastern-time calendar, not UTC. Live case on 2026-11-15:
# 4:44 pm in California is already Monday 00:44 UTC, but still Sunday in New York.
class SundayEvening(datetime):
    @classmethod
    def now(cls, tz=None):
        instant = datetime(2026, 11, 16, 0, 44, 51, tzinfo=timezone.utc)
        return instant.astimezone(tz) if tz else instant.replace(tzinfo=None)
use_dirs("eastern_time")
real_datetime, ff.datetime = ff.datetime, SundayEvening
try:
    s = FakeSession([search(1, [acc(1)])])
    code = run(s, ["--limit", "1"])
finally:
    ff.datetime = real_datetime
p = s.split()[0][0][1]
check("Sunday 4:44 pm Pacific -> window 2026-08-17 to 2026-11-15",
      code == 0 and (p["startdt"], p["enddt"]) == ("2026-08-17", "2026-11-15"))

# 15. searches too big to page through stop before any download
use_dirs("too_many")
s = FakeSession([search(12_000, [acc(1)])])
check("12,000 hits: exit 1, no XML requests", run(s, []) == 1 and not s.split()[1])
s = FakeSession([search(10_000, [acc(1)], relation="gte")])
check("count marked 'gte': exit 1", run(s, []) == 1)
s = FakeSession([FakeResp(200, content=b"<html>not json</html>")])
check("search answer that is not JSON: exit 1", run(s, []) == 1)

# 16. no filings at all
use_dirs("empty")
check("no filings: exit 0 and an empty funding.csv", run(FakeSession([search(0, [])]), []) == 0 and len(csv()) == 0)

# 17. bad command lines
for argv in (["--limit", "0"], ["--days", "0"]):
    try:
        ff.main(argv); check(f"{argv} rejected", False)
    except SystemExit as e:
        check(f"{argv} rejected", e.code == 2)

# 18. the email in SEC_USER_AGENT never reaches the log
check("User-Agent never logged", "simmail" not in LOG.read_text())

print(f"\n{sum(results)}/{len(results)} checks passed")
if all(results):
    logging.shutdown()
    shutil.rmtree(tmp, ignore_errors=True)
else:
    print(f"log: {LOG}")
sys.exit(0 if all(results) else 1)
