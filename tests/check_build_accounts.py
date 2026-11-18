"""Offline checks for build_accounts.py (step 3). Every filing, job and SEC search page here
is SIMULATED, in a temporary folder; the project's data/ is never read or written.

The top of this fixture's output is also the SIMULATED sample a reader finds in
docs/sample_output/accounts_SIMULATED.csv. One check compares the two, so the sample cannot
drift from what the script writes.

Run: python tests/check_build_accounts.py
     python tests/check_build_accounts.py --write-sample   # rewrite the sample after a wanted change
"""
import io
import json
import shutil
import sys
import tempfile
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import build_accounts as ba  # noqa: E402
import pandas as pd  # noqa: E402
from bay_area import hq_zip_check  # noqa: E402
from fetch_funding import COLUMNS as FUNDING_COLUMNS  # noqa: E402
from fetch_jobs import COLUMNS as JOB_COLUMNS  # noqa: E402

tmp = Path(tempfile.mkdtemp(prefix="sim_accounts_"))
ba.FUNDING_CSV, ba.JOBS_CSV, ba.RAW_DIR = tmp / "funding.csv", tmp / "jobs.csv", tmp / "raw"
ba.CLIENT_BONUS_CSV = tmp / "manual" / "client_bonus.csv"
ba.DOMAIN_OVERRIDES_CSV = tmp / "manual" / "domain_overrides.csv"
ba.REVIEW_DECISIONS_CSV = tmp / "manual" / "review_decisions.csv"
ba.ACCOUNTS_CSV, ba.REVIEW_CSV, ba.FUNNEL_CSV = tmp / "accounts.csv", tmp / "review_matches.csv", tmp / "funnel.csv"
AS_OF = date(2026, 11, 16)
results = []


def check(name, cond):
    results.append(bool(cond))
    print("PASS" if cond else "FAIL", "-", name)


def filing(n, issuer, first_sale, sold="2000000", stage="Seed", zip_code="94107", state="CA",
           excluded="False", cik=None):
    row = dict.fromkeys(FUNDING_COLUMNS, "")
    row.update({
        "accession_number": f"0000000000-26-{n:06d}", "cik": cik or str(1000 + n), "issuer_name": issuer,
        "filing_date": first_sale, "first_sale_date": first_sale, "city": "SAN FRANCISCO", "state": state,
        "zip_code": zip_code, "hq_zip_check": hq_zip_check(zip_code), "industry_group": "Other Technology",
        "industry_excluded": excluded, "amount_sold_usd": sold, "stage_estimate": stage,
        "filing_url": f"https://sec.example/filing/{n}",
    })
    return row


def job(i, company, title, role, company_id=None, zip_code="94107"):
    row = dict.fromkeys(JOB_COLUMNS, "")
    row.update({
        "job_id": str(i), "job_title": title, "role_category": role, "is_fde": str(role == "fde"),
        "company_name": company, "company_domain": company.lower().replace(" ", "") + ".example",
        "company_city": "San Francisco", "company_postal_code": zip_code, "hq_zip_check": hq_zip_check(zip_code),
        "job_url": f"https://jobs.example/{i}", "theirstack_company_id": company_id or f"ts-{company}",
    })
    return row


def adds_up(counts):
    """True if the funnel's parts sum to its first row."""
    rows = counts["funnel"]
    return rows[0][1] == sum(count for _, count in rows[1:])


# SIMULATED Form D filings. First-sale dates sit on the recency boundaries (as of 2026-11-16).
funding = pd.DataFrame([
    filing(1, "Alpha Robotics, Inc.", "2026-10-17"),            # 30 days
    filing(2, "Bravo AI Corp", "2026-10-16"),                   # 31 days
    filing(3, "Charlie Labs Inc.", "2026-09-17"),               # 60 days
    filing(4, "Delta Systems Inc.", "2026-09-16"),              # 61 days
    filing(5, "Echo Inc.", "2026-08-18"),                       # 90 days
    filing(6, "Foxtrot Inc.", "2026-08-17"),                    # 91 days
    filing(7, "Golf Inc.", "2026-07-01", sold="1000000", cik="7777"),
    filing(8, "Golf Inc.", "2026-11-01", sold="5000000", cik="7777"),
    filing(9, "Hotel Tiny Inc.", "2026-11-01", sold="50000"),
    filing(10, "India Big Inc.", "2026-11-01", sold="300000000", stage="Later"),
    filing(11, "Juliet Fund LP", "2026-11-01", excluded="True"),
    filing(12, "Kilo Software Inc.", "2026-11-01", zip_code="10001", state="NY"),
    filing(13, "Lima Inc.", "2026-11-01", zip_code="10001", state="NY"),
    filing(14, "Mike Labs Inc.", "2026-11-01"),
    filing(15, "November Inc.", "2026-11-01"),
    filing(16, "Oscar AI Inc.", "2026-11-01"),
    filing(17, "Oscar Inc.", "2026-11-01"),
    filing(18, "Papa Inc.", "2026-11-01"),
    filing(19, "Quebec Inc.", "2026-11-01"),
], columns=FUNDING_COLUMNS)

# SIMULATED TheirStack jobs
jobs = pd.DataFrame([
    job(1, "Alpha Robotics", "Robotics Engineer", "robotics"),
    job(2, "Alpha Robotics", "Forward Deployed Engineer", "fde"),
    job(3, "Bravo AI", "AI Engineer", "ai"),
    job(4, "Charlie Labs", "ML Engineer", "ml"),
    job(5, "Charlie Labs", "ml engineer", "ml"),                 # same title, other case
    job(6, "Charlie Labs", "ML  Engineer", "ml"),                # same title, extra space
    job(7, "Charlie Labs", "Machine Learning Engineer", "ml"),
    job(8, "Charlie Labs", "Applied AI Engineer", "applied_ai"),
    job(9, "Charlie Labs", "AI Engineer, Agents", "ai"),
    job(10, "Delta Systems", "AI Engineer", "ai"),
    job(11, "Echo", "AI Engineer", "ai"),
    job(12, "Foxtrot", "AI Engineer", "ai"),
    job(13, "Golf", "AI Engineer", "ai"),
    job(14, "Hotel Tiny", "AI Engineer", "ai"),                  # its filing is too small
    job(15, "Kilo Software", "AI Engineer", "ai"),               # Form D address in New York
    job(16, "Mike", "AI Engineer", "ai"),                        # short form of "Mike Labs", nothing confirms it
    job(17, "November", "AI Engineer", "ai", company_id="ts-november-1"),
    job(18, "November", "ML Engineer", "ml", company_id="ts-november-2"),
    job(19, "Oscar", "AI Engineer", "ai"),                       # "Oscar Inc." and short "Oscar AI"
    job(20, "Papa", "AI Engineer", "ai", zip_code="10001"),      # TheirStack HQ in New York
    job(21, "Quebec", "HTML Engineer", "other"),                 # no relevant role
    job(22, "Zulu", "AI Engineer", "ai"),                        # no Form D filing
], columns=JOB_COLUMNS)

# SIMULATED SEC search pages: EDGAR's business city for the two New York filings
ba.RAW_DIR.mkdir(parents=True)
page = {"response": {"hits": {"hits": [
    {"_id": "0000000000-26-000012:primary_doc.xml", "_source": {"biz_locations": ["San Francisco, CA"]}},
    {"_id": "0000000000-26-000013:primary_doc.xml", "_source": {"biz_locations": ["New York, NY"]}},
]}}}
(ba.RAW_DIR / "sec_search_2026-11-16T000000Z_sim_from00000.json").write_text(json.dumps(page))
(ba.RAW_DIR / "sec_search_2026-11-16T000001Z_bad_from00100.json").write_text("{not json")

check("a Bay Area city name in another state is not a Bay Area city",
      ba.is_bay_area_city("Oakland, CA") and not ba.is_bay_area_city("Oakland, NJ") and not ba.is_bay_area_city("San Jose, NM")
      and not ba.is_bay_area_city("Oakland"))

# 1. candidate filings
locations = ba.edgar_locations()
check("EDGAR locations read; a corrupt page is skipped", len(locations) == 2)
candidates = ba.candidate_filings(funding, locations)
names = set(candidates["issuer_name"])
check("too small, too large, excluded industry, and New York by both addresses are left out",
      not names & {"Hotel Tiny Inc.", "India Big Inc.", "Juliet Fund LP", "Lima Inc."})
check("New York address with a San Francisco EDGAR city is kept, marked edgar_city",
      candidates.loc[candidates["issuer_name"] == "Kilo Software Inc.", "form_d_hq"].tolist() == ["edgar_city"])
check("15 candidate filings from 14 companies", (len(candidates), candidates["cik"].nunique()) == (15, 14))

# 2. names sent to TheirStack
funding.to_csv(ba.FUNDING_CSV, index=False)
spellings, companies = ba.search_list()
check("search list covers 14 companies", companies == 14)
check("full names and short forms are sent", {"Alpha Robotics", "Alpha", "Kilo Software", "Kilo"} <= set(spellings))
check("filings that cannot become accounts are not searched", "Hotel Tiny" not in spellings)
check("search list is sorted", spellings == sorted(spellings, key=str.lower))

# 3. join and score: rules decide, nothing waits for a person
bonus = {"charlie labs": "SIMULATED evidence: named in the client's brief, https://news.example/1"}
accounts, review, counts = ba.build(candidates, jobs, bonus, AS_OF)
order = accounts["company_name"].tolist()
check("accounts.csv has the planned columns, flags among them",
      list(accounts.columns) == ba.ACCOUNT_COLUMNS and "flags" in ba.ACCOUNT_COLUMNS)
check("accounts sorted by score, ties by newer first sale, then name",
      order == ["Charlie Labs", "Alpha Robotics", "Golf", "Kilo Software", "Papa", "Bravo AI", "Delta Systems",
                "Echo", "Foxtrot"])
SAMPLE = Path(__file__).resolve().parent.parent / "docs" / "sample_output" / "accounts_SIMULATED.csv"
sample_text = accounts.head(5).to_csv(index=False)
if "--write-sample" in sys.argv:
    SAMPLE.parent.mkdir(parents=True, exist_ok=True)
    SAMPLE.write_text(sample_text, encoding="utf-8")
as_table = lambda source: pd.read_csv(source, dtype=str, keep_default_na=False)  # noqa: E731  (cells compared, not bytes)
check("docs/sample_output/accounts_SIMULATED.csv is the first 5 rows of this output (rewrite it with --write-sample)",
      SAMPLE.exists() and as_table(SAMPLE).equals(as_table(io.StringIO(sample_text))) and len(as_table(SAMPLE)) == 5)
row = accounts.set_index("company_name")
reason = row["score_reason"]
check("30 days -> +3", "30 days before 2026-11-16: +3" in reason["Alpha Robotics"])
check("31 days -> +2", "31 days before 2026-11-16: +2" in reason["Bravo AI"])
check("60 days -> +2", "60 days before 2026-11-16: +2" in reason["Charlie Labs"])
check("61 days -> +1", "61 days before 2026-11-16: +1" in reason["Delta Systems"])
check("90 days -> +1", "90 days before 2026-11-16: +1" in reason["Echo"])
check("91 days -> +0", "91 days before 2026-11-16: +0" in reason["Foxtrot"])
check("FDE opening adds 1", row.loc["Alpha Robotics", "fde_opening"] and row.loc["Alpha Robotics", "score"] == 6)
check("repeated titles count once", row.loc["Charlie Labs", "relevant_roles_count"] == 4)
check("role points capped at 3", "4 relevant role title(s): +3 (capped)" in reason["Charlie Labs"])
check("client bonus points only with evidence, labeled hand-edited",
      row.loc["Charlie Labs", "score"] == 7 and "(hand-edited): +2" in reason["Charlie Labs"]
      and row.loc["Charlie Labs", "client_bonus_evidence"].startswith("SIMULATED"))
check("no evidence -> no client bonus points", "no client-specific bonus evidence: +0" in reason["Bravo AI"])
check("newest filing gives the date and amount", (row.loc["Golf", "filing_date"], row.loc["Golf", "amount_sold_usd"])
      == ("2026-11-01", "5000000"))
check("every filing link and the job link are kept", all(u in row.loc["Golf", "source_urls"] for u in (
    "https://sec.example/filing/7", "https://sec.example/filing/8", "https://jobs.example/13")))
check("HQ location from Form D", row.loc["Alpha Robotics", "hq_location"] == "San Francisco, CA 94107")
check("a certain match carries no flag", row.loc["Alpha Robotics", "flags"] == "" and row.loc["Golf", "flags"] == "")
check("full-name match with a TheirStack HQ ZIP outside the Bay Area: listed on the Form D address, flagged",
      row.loc["Papa", "signal_tier"] == "funded_and_hiring" and row.loc["Papa", "hq_location"] == "San Francisco, CA 94107"
      and row.loc["Papa", "flags"] == "TheirStack HQ ZIP is outside the Bay Area (the Form D address is used)")
check("out-of-state Form D address with a Bay Area EDGAR city: listed, flagged, HQ from EDGAR",
      row.loc["Kilo Software", "flags"] == "Form D address is in SAN FRANCISCO, NY, and EDGAR's business city is San Francisco, CA"
      and row.loc["Kilo Software", "hq_location"] == "San Francisco, CA (EDGAR business address)")

rev = review.groupby("theirstack_company")
reasons = rev["reasons"].apply(" | ".join)
check("review file has the planned columns, outcome first",
      list(review.columns) == ba.REVIEW_COLUMNS and ba.REVIEW_COLUMNS[0] == "outcome")
check("5 rows the rules did not use, all for companies that are not listed",
      len(review) == 5 and set(review["outcome"]) == {"not listed"} and not set(review["theirstack_company"]) & set(order))
check("an unconfirmed short-form match is not used", "no other field confirms it" in reasons.get("Mike", "")
      and review.loc[review["theirstack_company"] == "Mike", "form_d_issuer"].tolist() == ["Mike Labs Inc."])
check("two TheirStack companies for one issuer: the filing is used for neither",
      (review["theirstack_company"] == "November").sum() == 2 and "several TheirStack" in reasons["November"])
check("a name matching two issuers: neither filing is used, one row each",
      (review["theirstack_company"] == "Oscar").sum() == 2 and "several Form D issuers" in reasons["Oscar"])
check("a match that is not used leaves a company with no core role out, and says so",
      all("no core role" in text for text in review["reasons"]))
check("no relevant role, no Form D, or a filing too small: in neither file",
      not {"Quebec", "Zulu", "Hotel Tiny"} & (set(order) | set(review["theirstack_company"])))
check("counts", (counts["candidate_companies"], counts["edgar_city_companies"], counts["matched_companies"],
                 counts["accounts"], counts["review_rows"], counts["flagged_accounts"]) == (14, 1, 13, 9, 5, 2))
check("funnel: 15 companies = 9 listed + 6 with no usable filing and no core role",
      adds_up(counts) and counts["funnel"][0][1] == 15 and counts["no_filing_no_core_role"] == 6
      and counts["funnel"][-1] == ("listed in accounts.csv", 9))

# 4. the reviewer's client bonus file
check("no file -> no evidence", ba.client_bonus_evidence() == {})
ba.CLIENT_BONUS_CSV.parent.mkdir(parents=True)
ba.CLIENT_BONUS_CSV.write_text("company_name,evidence\nCharlie Labs,SIMULATED link\nEcho,\n")
check("rows without evidence are ignored", ba.client_bonus_evidence() == {"charlie labs": "SIMULATED link"})

# 5. the command
check("missing jobs.csv -> exit 1", ba.main(["--as-of", "2026-11-16"]) == 1)
jobs.to_csv(ba.JOBS_CSV, index=False)
code = ba.main(["--as-of", "2026-11-16"])
written = pd.read_csv(ba.ACCOUNTS_CSV, dtype=str, keep_default_na=False)
check("full run exits 0 and writes the three files",
      code == 0 and ba.REVIEW_CSV.exists() and ba.FUNNEL_CSV.exists() and len(written) == 9)
check("written accounts.csv has the planned columns", list(written.columns) == ba.ACCOUNT_COLUMNS)
funnel_file = pd.read_csv(ba.FUNNEL_CSV)
check("funnel.csv: steps and counts, the listed row equal to the accounts written",
      list(funnel_file.columns) == ["step", "companies"] and len(funnel_file) == 6
      and funnel_file["companies"].iloc[-1] == len(written)
      and funnel_file["companies"].iloc[0] == funnel_file["companies"].iloc[1:].sum())
ba.CLIENT_BONUS_CSV.write_text("company,proof\nCharlie Labs,x\n")
check("client bonus file with wrong columns -> exit 1", ba.main(["--as-of", "2026-11-16"]) == 1)

# 6. SIMULATED reviewer decisions, copies of one posting, the added columns, TheirStack HQ city
check("no decisions file -> no decisions", ba.review_decisions() == {})
ba.REVIEW_DECISIONS_CSV.write_text("company_name,form_d_issuer,decision,reason\n"
                                   "Romeo,Romeo Labs Inc.,accept,SIMULATED: website spells the Form D name\n"
                                   "Sierra,,reject,SIMULATED: a competitor\n")
decisions = ba.review_decisions()
check("decisions keyed by cleaned company and issuer",
      decisions == {("romeo", "romeo labs"): ("accept", "SIMULATED: website spells the Form D name"),
                    ("sierra", ""): ("reject", "SIMULATED: a competitor")})
funding2 = pd.DataFrame([filing(n, issuer, "2026-11-01") for n, issuer in (
    (31, "Romeo Labs Inc."), (32, "Sierra Inc."), (33, "Tango Inc."), (34, "Uniform Inc."), (35, "Victor Inc."),
    (36, "Wander Inc."))],
    columns=FUNDING_COLUMNS)
ats = "https://ats.example/"


def posting(i, company, title, role, url, source, location, low="", high="", city="San Francisco", **more):
    row = job(i, company, title, role)
    row.update({"job_url": url + ("?utm_source=sim" if "indeed" in source else ""), "posting_url": url,
                "source_url": source, "job_location": location, "company_city": city,
                "salary_min_usd": low, "salary_max_usd": high, "theirstack_jobs_last_30_days": "12.0",
                "company_description": f"SIMULATED: {company} builds robots."})
    row.update(more)
    return row


jobs2 = pd.DataFrame([
    posting(41, "Romeo", "AI Engineer", "ai", ats + "romeo/1", ats + "romeo/1", "San Mateo, CA"),
    posting(42, "Sierra", "AI Engineer", "ai", ats + "sierra/1", ats + "sierra/1", "San Mateo, CA"),
    posting(43, "Tango", "Forward Deployed Engineer", "fde", ats + "tango/1", "https://indeed.example/a",
            "Somerville, MA", "150000", "200000"),                # copy of the next posting, found on Indeed
    posting(44, "Tango", "Forward Deployed Engineer", "fde", ats + "tango/1", ats + "tango/1",
            "Boston (Somerville)", "150000", "200000"),
    posting(45, "Tango", "Forward Deployed Engineer", "fde", ats + "tango/2", ats + "tango/2",
            "San Mateo, CA", "140000", "210000"),
    posting(46, "Tango", "ML Engineer", "ml", ats + "tango/3", ats + "tango/3", "San Mateo, CA"),
    posting(47, "Uniform", "AI Engineer", "ai", ats + "uniform/1", ats + "uniform/1", "Austin, TX", city="Austin"),
    posting(48, "Victor", "AI Engineer", "ai", ats + "victor/1", ats + "victor/1", "", city=""),
    posting(49, "Wander", "AI Engineer", "ai", ats + "wander/1", ats + "wander/1", "Austin, TX", city="Austin",
            company_postal_code="73301", hq_zip_check="outside"),
], columns=JOB_COLUMNS)
candidates2 = ba.candidate_filings(funding2, {})
accounts2, review2, counts2 = ba.build(candidates2, jobs2, {}, AS_OF, decisions)
row2 = accounts2.set_index("company_name")
check("an accepted short-form match goes to accounts, labeled hand-edited",
      "Romeo" in row2.index
      and "match confirmed by the reviewer (hand-edited): SIMULATED" in row2.loc["Romeo", "score_reason"])
check("a company on the block list is in neither file",
      "Sierra" not in set(row2.index) | set(review2["theirstack_company"]))
check("copies of one posting count once: 3 postings, 2 titles",
      row2.loc["Tango", "relevant_roles_count"] == 2 and row2.loc["Tango", "source_urls"].count(ats + "tango/") == 3)
check("job links have no tracking parameters", "utm_source" not in row2.loc["Tango", "source_urls"])
check("the copy from the posting's own site gives the location",
      row2.loc["Tango", "job_locations"] == "Boston (Somerville); San Mateo, CA")
check("salary range per title across postings", row2.loc["Tango", "salary_ranges"] == "Forward Deployed Engineer $140k-$210k")
check("TheirStack's 30-day job count and the description are kept",
      row2.loc["Tango", "theirstack_jobs_last_30_days"] == 12
      and row2.loc["Tango", "company_description"] == "SIMULATED: Tango builds robots.")
check("a full-name match whose TheirStack HQ city is outside the Bay Area: listed, flagged",
      "Uniform" in row2.index and row2.loc["Uniform", "signal_tier"] == "funded_and_hiring"
      and row2.loc["Uniform", "flags"] == "TheirStack HQ city is Austin, not a Bay Area city (the Form D address is used)")
check("two HQ doubts make one flag, so no flag contains the separator",
      row2.loc["Wander", "flags"] == "TheirStack HQ ZIP is outside the Bay Area and TheirStack HQ city is Austin, "
                                     "not a Bay Area city (the Form D address is used)")
check("a full-name match with no TheirStack HQ city: listed, flagged",
      "Victor" in row2.index and "no HQ city" in row2.loc["Victor", "flags"])
check("nothing was left out by the rules here", len(review2) == 0)
check("counts: 9 relevant records, 8 postings, 1 accepted, 1 on the block list",
      (counts2["relevant_jobs"], counts2["relevant_postings"], len(counts2["accepted_by_reviewer"]),
       len(counts2["rejected_by_reviewer"]), counts2["unused_decisions"]) == (9, 8, 1, 1, 0))
check("funnel: 6 companies = 5 listed + 1 on the block list",
      adds_up(counts2) and counts2["funnel"][1] == ("on the reviewer's block list", 1) and counts2["accounts"] == 5)
_, _, counts3 = ba.build(candidates2, jobs2, {}, AS_OF, {**decisions, ("nobody", ""): ("reject", "SIMULATED")})
check("a decision that matches no company is counted", counts3["unused_decisions"] == 1)
header = "company_name,form_d_issuer,decision,reason\n"
for content, problem in ((header + ",Romeo Labs Inc.,accept,x\n", "no company name"),
                         (header + "Romeo,Romeo Labs Inc.,maybe,x\n", "a decision other than accept or reject"),
                         (header + "Sierra,,reject,x\nSierra Inc,,reject,y\n", "the same company twice"),
                         ("company,decision\nRomeo,accept\n", "missing columns")):
    ba.REVIEW_DECISIONS_CSV.write_text(content)
    try:
        ba.review_decisions()
        check(f"decisions file refused: {problem}", False)
    except ValueError:
        check(f"decisions file refused: {problem}", True)
ba.CLIENT_BONUS_CSV.write_text("company_name,evidence\n")
check("decisions file with wrong columns -> exit 1", ba.main(["--as-of", "2026-11-16"]) == 1)
ba.REVIEW_DECISIONS_CSV.unlink()
check("without a decisions file the run exits 0", ba.main(["--as-of", "2026-11-16"]) == 0)

# 7. SIMULATED hiring-first cases: no Form D filing, filings above the amount rule, staffing hints
funding3 = pd.DataFrame([
    filing(51, "Whiskey Inc.", "2026-10-16"),                                              # 31 days: +2
    filing(52, "Xray Inc.", "2026-09-01", sold="20000000", stage="Series A", cik="5252"),  # 76 days: +1
    filing(53, "Xray Inc.", "2026-11-05", sold="900000000", stage="Later", cik="5252"),    # above the rule
    filing(54, "Yankee Inc.", "2026-11-01", sold="500000000", stage="Later"),              # only above the rule
    filing(55, "Tiny Inc.", "2026-11-01", sold="50000"),                                   # under the minimum
], columns=FUNDING_COLUMNS)
rows3 = [
    posting(61, "Whiskey", "AI Engineer", "ai", ats + "whiskey/1", ats + "whiskey/1", "San Mateo, CA"),
    posting(62, "Xray", "AI Engineer", "ai", ats + "xray/1", ats + "xray/1", "San Mateo, CA"),
    posting(63, "Yankee", "AI Engineer", "ai", ats + "yankee/1", ats + "yankee/1", "San Mateo, CA"),
    posting(64, "Zebra", "Forward Deployed Engineer", "fde", ats + "zebra/1", ats + "zebra/1", "San Mateo, CA"),
    posting(65, "Zebra", "ML Engineer", "ml", ats + "zebra/2", ats + "zebra/2", "San Mateo, CA"),
    posting(66, "Yoyo", "ML Engineer", "ml", ats + "yoyo/1", ats + "yoyo/1", "San Mateo, CA"),       # no core role
    posting(67, "Quokka", "Applied AI Engineer", "applied_ai", ats + "quokka/1", ats + "quokka/1", "Menlo Park"),
    posting(68, "Staffly", "Forward Deployed Engineer", "fde", ats + "staffly/1", ats + "staffly/1", "San Mateo, CA"),
    posting(69, "Tiny", "ML Engineer", "ml", ats + "tiny/1", ats + "tiny/1", "San Mateo, CA"),       # filing too small
]
rows3[6].update({"company_postal_code": "08837", "hq_zip_check": "outside"})               # Menlo Park, NJ
rows3[7]["company_description"] = "SIMULATED: We help companies build elite engineering teams."
rows3 += [
    posting(73, "Longtext", "Forward Deployed Engineer", "fde", ats + "longtext/1", ats + "longtext/1", "San Mateo, CA"),
    posting(74, "Agencia", "Forward Deployed Engineer", "fde", ats + "agencia/1", ats + "agencia/1", "San Mateo, CA"),
]
rows3[9]["company_long_description"] = "SIMULATED: your AI headhunter, matching candidates with startups."
rows3[10]["company_industry"] = "Staffing and Recruiting"
rows3[3].update({"theirstack_funding_stage": "series_a", "theirstack_last_funding_date": "2026-04-25T00:00:00"})
jobs3 = pd.DataFrame(rows3, columns=JOB_COLUMNS)
filings3 = ba.bay_area_filings(funding3, {})
check("filings above the amount rule are kept; those under the minimum are not",
      set(filings3["issuer_name"]) == {"Whiskey Inc.", "Xray Inc.", "Yankee Inc."}
      and filings3.loc[filings3["issuer_name"] == "Yankee Inc.", "in_amount_range"].tolist() == [False])
check("candidate filings are the ones inside the amount rule",
      set(ba.candidate_filings(funding3, {})["amount_sold_usd"]) == {"2000000", "20000000"})
accounts3, review3, counts3 = ba.build(filings3, jobs3, {}, AS_OF)
row3, rev3 = accounts3.set_index("company_name"), review3.set_index("theirstack_company")
names3 = accounts3["company_name"].tolist()
check("a company with a core role and no filing is a hiring-only account",
      "Zebra" in row3.index and row3.loc["Zebra", "signal_tier"] == "hiring_only"
      and (row3.loc["Zebra", "filing_date"], row3.loc["Zebra", "amount_sold_usd"], row3.loc["Zebra", "flags"]) == ("", "", ""))
check("hiring-only score: no funding points, 2 titles and an FDE opening -> 3",
      "Zebra" in row3.index and row3.loc["Zebra", "score"] == 3
      and "no Bay Area Form D filing found in the funding window: +0" in row3.loc["Zebra", "score_reason"])
check("hiring-only HQ is labeled as TheirStack's",
      "Zebra" in row3.index and row3.loc["Zebra", "hq_location"] == "San Francisco (TheirStack, ZIP 94107)")
check("order: score, then funded before hiring-only, then name",
      names3 == ["Yankee", "Whiskey", "Zebra", "Xray", "Agencia", "Longtext", "Staffly"]
      and list(accounts3["score"]) == [4, 3, 3, 2, 2, 2, 2] and row3.loc["Whiskey", "signal_tier"] == "funded_and_hiring")
check("no filing and no core role: not listed, but counted",
      "Yoyo" not in set(row3.index) | set(rev3.index) and "Tiny" not in set(row3.index) | set(rev3.index)
      and counts3["no_filing_no_core_role"] == 2)
check("a filing above the amount rule next to a smaller one: listed on the smaller one, flagged",
      "Xray" in row3.index and (row3.loc["Xray", "amount_sold_usd"], row3.loc["Xray", "filing_date"])
      == ("20000000", "2026-09-01") and "76 days before 2026-11-16: +1" in row3.loc["Xray", "score_reason"]
      and row3.loc["Xray", "flags"] == "also filed 1 Form D filing(s) above the amount rule (largest sold $900,000,000)")
check("only filings above the amount rule: listed, flagged",
      "Yankee" in row3.index and row3.loc["Yankee", "amount_sold_usd"] == "500000000"
      and row3.loc["Yankee", "flags"] == "only filed 1 Form D filing(s) above the amount rule (largest sold $500,000,000)")
check("hiring-only with an HQ ZIP outside the Bay Area is not listed, without Form D columns",
      "Quokka" not in row3.index and "Quokka" in rev3.index and rev3.loc["Quokka", "outcome"] == "not listed"
      and rev3.loc["Quokka", "signal_tier"] == "hiring_only" and "ZIP is outside" in rev3.loc["Quokka", "reasons"]
      and rev3.loc["Quokka", "form_d_issuer"] == "" and counts3["dropped_no_bay_area_hq"] == 1)
check("rows the rules left out carry the job locations and the description",
      "Quokka" in rev3.index and rev3.loc["Quokka", "job_locations"] == "Menlo Park"
      and rev3.loc["Quokka", "company_description"].startswith("SIMULATED"))
check("a description that suggests a staffing firm is listed with a flag",
      "Staffly" in row3.index and "staffing or services firm" in row3.loc["Staffly", "flags"])
check("the long description is checked for staffing hints too",
      "Longtext" in row3.index and '("headhunt")' in row3.loc["Longtext", "flags"])
check("a staffing industry is listed with a flag",
      "Agencia" in row3.index and row3.loc["Agencia", "flags"] == "TheirStack industry is Staffing and Recruiting")
check("TheirStack's funding fields are shown as a reference and add no points",
      "Zebra" in row3.index and row3.loc["Zebra", "theirstack_funding"] == "series_a, last round 2026-04-25"
      and row3.loc["Zebra", "score"] == 3 and row3.loc["Whiskey", "theirstack_funding"] == "")
check("counts by tier, and nothing waits",
      (counts3["funded_accounts"], counts3["hiring_only_accounts"], counts3["review_rows"], counts3["flagged_accounts"])
      == (3, 4, 1, 5))
check("funnel: 10 companies = 7 listed + 2 without a core role + 1 without a Bay Area HQ", adds_up(counts3)
      and [count for _, count in counts3["funnel"]] == [10, 0, 2, 1, 0, 7])
edge_funding = pd.DataFrame([{**filing(56, "Zulu Two Inc.", "2026-10-16"), "first_sale_date": ""}], columns=FUNDING_COLUMNS)
edge_jobs = pd.DataFrame([
    posting(70, "Zulu Two", "AI Engineer", "ai", ats + "zulutwo/1", ats + "zulutwo/1", "San Mateo, CA"),
    posting(71, "Aardvark", "Forward Deployed Engineer", "fde", ats + "aardvark/1", ats + "aardvark/1", "San Mateo, CA"),
    posting(72, "Aardvark", "ML Engineer", "ml", ats + "aardvark/2", ats + "aardvark/2", "San Mateo, CA"),
], columns=JOB_COLUMNS)
edge_accounts, _, _ = ba.build(ba.bay_area_filings(edge_funding, {}), edge_jobs, {}, AS_OF)
check("a funded account without a first-sale date still comes before a hiring-only one on equal scores",
      edge_accounts["company_name"].tolist() == ["Zulu Two", "Aardvark"] and list(edge_accounts["score"]) == [3, 3])
decisions3 = {("xray", "xray"): ("accept", "SIMULATED"), ("yankee", "yankee"): ("accept", ""),
              ("quokka", ""): ("accept", "SIMULATED: HQ checked"), ("staffly", ""): ("reject", "SIMULATED")}
accounts4, review4, counts4 = ba.build(filings3, jobs3, {}, AS_OF, decisions3)
row4 = accounts4.set_index("company_name")
check("an accepted match counts its newest filing, even above the amount rule",
      "Xray" in row4.index and (row4.loc["Xray", "amount_sold_usd"], row4.loc["Xray", "filing_date"])
      == ("900000000", "2026-11-05") and "11 days before 2026-11-16: +3" in row4.loc["Xray", "score_reason"]
      and row4.loc["Xray", "source_urls"].count("https://sec.example/filing/") == 2)
check("accepted with only filings above the rule -> account", "Yankee" in row4.index)
check("a blank-issuer accept keeps a company the HQ rule would drop, labeled hand-edited and flagged",
      "Quokka" in row4.index and "accepted by the reviewer (hand-edited): SIMULATED" in row4.loc["Quokka", "score_reason"]
      and "ZIP is outside" in row4.loc["Quokka", "flags"])
check("a hiring-only company on the block list is in neither file",
      "Staffly" not in set(row4.index) | set(review4["theirstack_company"]) and len(review4) == 0
      and counts4["unused_decisions"] == 0 and adds_up(counts4))
ba.REVIEW_DECISIONS_CSV.write_text(header + "Alpha,,reject,SIMULATED\nBravo AI,Bravo AI Corp,reject,SIMULATED\n"
                                   "Zebra,,accept,SIMULATED\n")
check("names rejected for every issuer, as the reviewer wrote them", ba.rejected_company_names() == ["Alpha"])
spellings, _ = ba.search_list()
check("a rejected company's spelling is not searched; the issuer's other spellings stay",
      "Alpha" not in spellings and "Alpha Robotics" in spellings and "Bravo AI" in spellings)
ba.REVIEW_DECISIONS_CSV.unlink()
check("no decisions file -> no rejected names", ba.rejected_company_names() == [])

# 8. SIMULATED short-form matches: a second field must confirm the Form D name
funding5 = pd.DataFrame([filing(n, issuer, "2026-11-01") for n, issuer in (
    (81, "Nova Robotics, Inc."), (82, "Orbit AI Corp"), (83, "Pulse Labs Inc."), (84, "Quill Labs Inc."),
    (85, "vex - Vector Exchange, Inc."), (86, "Ridge Systems Inc."), (87, "Sable AI Inc."), (88, "Terra AI Inc."),
    (89, "Lambda Labs Inc."), (90, "Bravo AI Corp"))], columns=FUNDING_COLUMNS)
board = "https://jobs.ashbyhq.com/Orbit%20AI%20Corp/1"
jobs5 = pd.DataFrame([
    posting(101, "Nova", "AI Engineer", "ai", ats + "nova/1", ats + "nova/1", "San Mateo, CA",
            company_domain="novarobotics.example"),                                  # the domain spells the name
    posting(102, "Orbit", "AI Engineer", "ai", board, board, "San Mateo, CA"),       # the job board spells the name
    posting(103, "Pulse", "AI Engineer", "ai", ats + "pulse/1", ats + "pulse/1", "San Mateo, CA",
            company_linkedin_url="https://www.linkedin.com/company/pulse-labs/?trk=sim"),
    posting(104, "Quill", "AI Engineer", "ai", ats + "quill/1", ats + "quill/1", "San Mateo, CA",
            company_description="SIMULATED: Quill Labs builds pens."),
    posting(105, "vex", "AI Engineer", "ai", ats + "vex/1", ats + "vex/1", "San Mateo, CA",
            company_linkedin_url="https://www.linkedin.com/company/vector-exchange"),  # the second part of the name
    posting(106, "Ridge", "Forward Deployed Engineer", "fde", ats + "ridge/1", ats + "ridge/1", "San Mateo, CA"),
    posting(107, "Sable", "AI Engineer", "ai", ats + "sable/1", ats + "sable/1", "San Mateo, CA",
            company_domain="sableairlines.example"),                                 # "sableai" only inside the domain
    posting(108, "Terra", "Forward Deployed Engineer", "fde", ats + "terra/1", ats + "terra/1", "San Mateo, CA",
            company_description="SIMULATED: Terra aims high."),                      # "terra ai" only across words
    posting(109, "Lambda", "Forward Deployed Engineer", "fde", ats + "lambda/1", ats + "lambda/1", "San Mateo, CA"),
    posting(110, "Bravo AI", "AI Engineer", "ai", ats + "bravo/1", ats + "bravo/1", "San Mateo, CA"),
], columns=JOB_COLUMNS)
filings5 = ba.bay_area_filings(funding5, {})
accounts5, review5, counts5 = ba.build(filings5, jobs5, {}, AS_OF, {("lambda", ""): ("accept", "SIMULATED")})
row5 = accounts5.set_index("company_name")
for company, field in (("Nova", "domain"), ("Orbit", "job board"), ("Pulse", "LinkedIn page"),
                       ("Quill", "description"), ("vex", "LinkedIn page")):
    check(f"a short form confirmed by the company's {field} is a funded account, flagged ({company})",
          company in row5.index and row5.loc[company, "signal_tier"] == "funded_and_hiring"
          and row5.loc[company, "flags"] == f"matched on a short form of the Form D name, confirmed by the company's {field}")
check("an unconfirmed short form with a core role is listed as hiring-only, without the filing",
      "Ridge" in row5.index and (row5.loc["Ridge", "signal_tier"], row5.loc["Ridge", "filing_date"]) == ("hiring_only", "")
      and "sec.example" not in row5.loc["Ridge", "source_urls"])
ridge = review5[review5["theirstack_company"] == "Ridge"]
check("the filing that was not used is written down, with its issuer",
      len(ridge) == 1 and ridge["outcome"].iloc[0] == "listed without this filing"
      and ridge["form_d_issuer"].iloc[0] == "Ridge Systems Inc." and "no other field confirms it" in ridge["reasons"].iloc[0])
check("the longer name inside a longer domain does not confirm", "Sable" not in row5.index
      and review5.loc[review5["theirstack_company"] == "Sable", "outcome"].tolist() == ["not listed"])
check("the longer name across two words of the description does not confirm",
      "Terra" in row5.index and row5.loc["Terra", "signal_tier"] == "hiring_only")
check("a blank-issuer accept never confirms a Form D match",
      "Lambda" in row5.index and row5.loc["Lambda", "signal_tier"] == "hiring_only"
      and "accepted by the reviewer (hand-edited): SIMULATED" in row5.loc["Lambda", "score_reason"]
      and counts5["unused_decisions"] == 0)
check("a full-name match needs no second field", row5.loc["Bravo AI", "signal_tier"] == "funded_and_hiring"
      and row5.loc["Bravo AI", "flags"] == "")
check("funnel: 10 companies = 9 listed + 1 with no usable filing and no core role",
      adds_up(counts5) and [count for _, count in counts5["funnel"]] == [10, 0, 1, 0, 0, 9])
_, _, counts5b = ba.build(filings5, jobs5, {}, AS_OF, {("ridge", "ridge systems"): ("reject", "SIMULATED"),
                                                       ("nova", "nova robotics"): ("reject", "SIMULATED")})
accounts5b, review5b, _ = ba.build(filings5, jobs5, {}, AS_OF, {("nova", "nova robotics"): ("reject", "SIMULATED")})
check("the reviewer's reject of one match leaves the filing unused, and the reason says so",
      "Nova" not in set(accounts5b["company_name"])
      and "the reviewer rejected this match: SIMULATED" in " ".join(review5b.loc[review5b["theirstack_company"] == "Nova", "reasons"])
      and counts5b["unused_decisions"] == 0)

# 9. SIMULATED staffing firms: flagged, and dropped when they post jobs for other companies
hint = "SIMULATED: a global IT recruitment leader."


def firm(i, company, description, employees, recent):
    return posting(i, company, "Forward Deployed Engineer", "fde", ats + f"{company.lower()}/1",
                   ats + f"{company.lower()}/1", "San Mateo, CA", company_description=description,
                   employee_count=employees, theirstack_jobs_last_30_days=recent)


funding6 = pd.DataFrame([filing(91, "Fundshop Inc.", "2026-11-01")], columns=FUNDING_COLUMNS)
jobs6 = pd.DataFrame([
    firm(121, "Bodyshop", hint, "9", "74.0"),                               # 74 postings, 9 people: dropped
    firm(122, "Smallhint", hint, "14", "25"),                               # under 5 postings a person: flagged
    firm(123, "Bigposter", "SIMULATED: Bigposter builds robots.", "8", "173"),  # many postings, no staffing hint
    firm(124, "Nohead", hint, "", "500"),                                   # no headcount: cannot tell
    firm(125, "Edge", hint, "10", "50"),                                    # exactly 5 a person: kept
    firm(126, "Keeper", hint, "9", "74"),                                   # kept by the reviewer
    firm(127, "Fundshop", hint, "5", "60"),                                 # a Form D filing does not save it
], columns=JOB_COLUMNS)
filings6 = ba.bay_area_filings(funding6, {})
accounts6, review6, counts6 = ba.build(filings6, jobs6, {}, AS_OF, {("keeper", ""): ("accept", "SIMULATED")})
row6, rev6 = accounts6.set_index("company_name"), review6.set_index("theirstack_company")
check("a staffing firm with more than 5 postings per employee is not listed",
      "Bodyshop" not in row6.index and rev6.loc["Bodyshop", "outcome"] == "not listed"
      and "posts jobs for other companies (74 postings in 30 days against 9 employees)" in rev6.loc["Bodyshop", "reasons"]
      and '("recruit")' in rev6.loc["Bodyshop", "reasons"])
check("a staffing hint alone only adds a flag",
      "Smallhint" in row6.index and row6.loc["Smallhint", "flags"] == 'the description suggests a staffing or services firm ("recruit")')
check("many postings without a staffing hint: listed, no flag", "Bigposter" in row6.index and row6.loc["Bigposter", "flags"] == "")
check("no headcount: listed with the flag", "Nohead" in row6.index and "staffing" in row6.loc["Nohead", "flags"])
check("exactly 5 postings per employee is kept", "Edge" in row6.index)
check("the reviewer's accept keeps such a firm, and the flag shows the numbers",
      "Keeper" in row6.index and row6.loc["Keeper", "flags"].split("; ") == [
          'the description suggests a staffing or services firm ("recruit")', "74 postings in 30 days against 9 employees"]
      and "accepted by the reviewer (hand-edited)" in row6.loc["Keeper", "score_reason"])
check("the rule also drops a staffing firm that has a Form D filing, and keeps the filing in the row",
      "Fundshop" not in row6.index and rev6.loc["Fundshop", "form_d_issuer"] == "Fundshop Inc.")
check("funnel: 7 companies = 5 listed + 2 posting jobs for other companies",
      adds_up(counts6) and [count for _, count in counts6["funnel"]] == [7, 0, 0, 0, 2, 5]
      and counts6["dropped_posts_for_others"] == 2)

# 10. SIMULATED websites corrected by hand
check("no domain file -> no corrections", ba.domain_overrides() == {})
ba.DOMAIN_OVERRIDES_CSV.parent.mkdir(parents=True, exist_ok=True)
ba.DOMAIN_OVERRIDES_CSV.write_text("company_name,domain,reason\nEdge Inc.,Edge-AI.example,SIMULATED; two parts\nGhost,ghost.example,\n")
fixes = ba.domain_overrides()
check("the domain file is read by cleaned company name, lower-cased, and a reason never holds the flag separator",
      fixes == {"edge": ("edge-ai.example", "SIMULATED, two parts"), "ghost": ("ghost.example", "")})
accounts7, _, counts7 = ba.build(filings6, jobs6, {}, AS_OF, {("keeper", ""): ("accept", "SIMULATED")}, fixes)
row7 = accounts7.set_index("company_name")
check("a corrected website replaces TheirStack's in the row, and a flag names both",
      row7.loc["Edge", "domain"] == "edge-ai.example" and row7.loc["Edge", "flags"].split("; ")[-1]
      == "website set by the reviewer (hand-edited): edge-ai.example instead of TheirStack's edge.example, SIMULATED, two parts")
check("other rows keep their domain, and the list is otherwise the same",
      row7.loc["Smallhint", "domain"] == row6.loc["Smallhint", "domain"] and list(row7.index) == list(row6.index)
      and list(row7["score"]) == list(row6["score"]))
check("a correction for a company that is not listed is counted, not dropped silently",
      (counts7["corrected_domains"], counts7["unused_domain_overrides"]) == (1, 1) and counts6["corrected_domains"] == 0)
for problem, body in (("a missing column", "company_name,domain\nEdge,edge.example\n"),
                      ("a value that is not a domain", "company_name,domain,reason\nEdge,https://edge.example/x,\n"),
                      ("an empty company name", "company_name,domain,reason\n,edge.example,\n"),
                      ("a company listed twice", "company_name,domain,reason\nEdge,a.example,\nEdge Inc.,b.example,\n")):
    ba.DOMAIN_OVERRIDES_CSV.write_text(body)
    try:
        ba.domain_overrides()
        check(f"domain file refused: {problem}", False)
    except ValueError:
        check(f"domain file refused: {problem}", True)
check("a malformed domain file stops the command with exit 1", ba.main(["--as-of", "2026-11-16"]) == 1)
ba.DOMAIN_OVERRIDES_CSV.unlink()

# 11. the funnel table refuses numbers that do not add up
check("funnel table: parts equal to the total are accepted", len(ba.funnel_table(10, 1, 2, 3, 0, 4)) == 6)
try:
    ba.funnel_table(10, 1, 2, 3, 0, 5)
    check("funnel table: a lost or double-counted company raises an error", False)
except RuntimeError:
    check("funnel table: a lost or double-counted company raises an error", True)

print(f"\n{sum(results)}/{len(results)} checks passed")
if all(results):
    shutil.rmtree(tmp, ignore_errors=True)
sys.exit(0 if all(results) else 1)
