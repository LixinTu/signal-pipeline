"""Join recent Form D funding with open AI engineering jobs and score the accounts (step 3).

Reads data/funding.csv (step 2), data/jobs.csv (step 1), the SEC search pages saved in
data/raw/ (for EDGAR's business city) and three files the reviewer fills in by hand, if
they exist:
- data/manual/client_bonus.csv (company_name, evidence): evidence for the one bonus signal
  that the client defines. It is confirmed by hand and never inferred.
- data/manual/review_decisions.csv (company_name, form_d_issuer, decision, reason), the
  reviewer's overrides: "accept" confirms one Form D match or, with form_d_issuer blank, keeps
  a company the rules would drop; "reject" refuses one match or, with form_d_issuer blank,
  leaves the company out everywhere (the block list).
- data/manual/domain_overrides.csv (company_name, domain, reason): the website to use when
  TheirStack's is wrong. The account shows the corrected domain and says so in `flags`;
  name matching still uses TheirStack's own data.

Writes three files:
- data/accounts.csv: one row per company, with its score, the reasons for it and, in
  `flags`, every doubt the rules had about it.
- data/review_matches.csv: every company the rules left out and every Form D match they did
  not use, with the reason. Nothing in it waits for a person; it is there to be audited.
- data/funnel.csv: how many companies came in and where each one went.

No run waits for a review. The rules, in CONFIG and in build():
- A short-form name match ("Generalist" for "Generalist AI, Inc.") counts only when a longer
  spelling of the Form D name is the company's domain, job board name or LinkedIn page
  name, or appears in its description. Otherwise the filing is not used.
- A name that fits several issuers, or an issuer that fits several companies, is not used.
- For a confirmed match the Form D address places the HQ, even if TheirStack disagrees.
- A filing above the amount rule does not stop a company; it adds a flag.
- A staffing or services hint adds a flag. The firm is dropped only when its 30-day
  postings exceed CONFIG["max_postings_per_employee"] times its headcount, which means it
  posts jobs for other companies.
- A company with no usable filing needs a core role and a Bay Area HQ in TheirStack.
Copies of one job posting (found on the careers page and on Indeed) count once.

Two tiers, in the `signal_tier` column:
- funded_and_hiring: the company's name matches a Bay Area Form D filing from step 2.
- hiring_only: no filing is used, but the company is hiring for a core role (forward
  deployed or applied AI engineer). Form D misses many rounds, so funding adds points
  instead of deciding who is listed.

Step 1 also uses `search_list()` from here, so it only pays for jobs at companies that
can become accounts.

Usage:
    python build_accounts.py                      # score as of today
    python build_accounts.py --as-of 2026-11-16   # score as of a fixed date
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

import pandas as pd

from bay_area import is_bay_area_city, is_bay_area_city_name
from names import clean_name, search_names

ROOT = Path(__file__).resolve().parent
FUNDING_CSV = ROOT / "data" / "funding.csv"
JOBS_CSV = ROOT / "data" / "jobs.csv"
RAW_DIR = ROOT / "data" / "raw"
CLIENT_BONUS_CSV = ROOT / "data" / "manual" / "client_bonus.csv"
REVIEW_DECISIONS_CSV = ROOT / "data" / "manual" / "review_decisions.csv"
DOMAIN_OVERRIDES_CSV = ROOT / "data" / "manual" / "domain_overrides.csv"
ACCOUNTS_CSV = ROOT / "data" / "accounts.csv"
REVIEW_CSV = ROOT / "data" / "review_matches.csv"
FUNNEL_CSV = ROOT / "data" / "funnel.csv"

CONFIG: dict[str, Any] = {
    # Which Form D filings can make an account
    "min_amount_sold_usd": 100_000,   # smaller filings are rarely a real round
    "excluded_stages": ["Later"],     # step 2's estimate for $200M or more: past Series C
    # A company with no Form D filing is still an account if it hires for one of these
    "hiring_only_roles": ["fde", "applied_ai"],
    # Phrases in a company description that suggest a staffing or services firm (a flag)
    "staffing_hints": [r"\bstaffing\b", r"\brecruit", r"\boutsourc", r"\bnearshor",
                       r"\btalent (marketplace|platform|network)\b", r"\bhiring platform\b",
                       r"\bheadhunt", r"\bconsulting\b", r"\bcandidates\b", r"\bjob seekers?\b",
                       r"\bhelp\w*\b.{0,60}\bbuild\b.{0,90}\bteams\b"],
    # TheirStack industries that are rarely a customer for a recruiting platform (a flag)
    "review_industries": ["Staffing and Recruiting", "IT Services and IT Consulting",
                          "Human Resources Services", "Business Consulting and Services"],
    # A staffing or services firm with more 30-day postings than this many times its headcount
    # is posting jobs for other companies: dropped instead of flagged
    "max_postings_per_employee": 5,
    # Job boards whose URLs start with the employer's own board name: jobs.ashbyhq.com/<board>/...
    "job_board_hosts": ["jobs.ashbyhq.com", "boards.greenhouse.io", "job-boards.greenhouse.io", "jobs.lever.co"],
    # A longer spelling of the Form D name needs this many letters to confirm a short-form match
    "min_confirming_letters": 6,
    # Scoring v1, as set in CLAUDE.md
    "recency_points": [(30, 3), (60, 2), (90, 1)],  # (days since the first sale, points)
    "points_per_role": 1,
    "max_role_points": 3,
    "fde_points": 1,
    "client_bonus_points": 2,   # one signal the client defines, confirmed by hand
}

ACCOUNT_COLUMNS = [
    "company_name", "domain", "signal_tier", "hq_location", "filing_date", "amount_sold_usd",
    "theirstack_funding",
    "relevant_roles_count", "role_titles", "fde_opening", "job_locations", "salary_ranges",
    "theirstack_jobs_last_30_days", "company_description", "client_bonus_evidence",
    "score", "score_reason", "flags", "source_urls",
]
REVIEW_COLUMNS = [
    "outcome", "reasons", "signal_tier", "theirstack_company", "company_domain", "company_description", "theirstack_city",
    "theirstack_postal_code", "form_d_issuer", "form_d_city", "form_d_state", "form_d_zip",
    "edgar_business_location", "first_sale_date", "amount_sold_usd", "relevant_roles_count",
    "role_titles", "job_locations", "job_urls", "filing_url",
]
DECISIONS = ("accept", "reject")

log = logging.getLogger("build_accounts")


def read_table(path: Path) -> pd.DataFrame:
    """Read a CSV as text, with empty cells as empty strings."""
    return pd.read_csv(path, dtype=str, keep_default_na=False)


def edgar_locations(raw_dir: Path | None = None) -> dict[str, str]:
    """EDGAR's business location ("City, ST") per accession number, from the SEC search
    pages that step 2 saved. A Form D address can be a registered agent's instead."""
    locations: dict[str, str] = {}
    for path in sorted((raw_dir or RAW_DIR).glob("sec_search_*.json")):
        try:
            hits = json.loads(path.read_text(encoding="utf-8"))["response"]["hits"]["hits"]
        except (OSError, ValueError, KeyError, TypeError) as err:
            log.warning("Skipping unreadable SEC search page %s: %s", path.name, err)
            continue
        for hit in hits:
            try:
                accession = str(hit["_id"]).partition(":")[0]
                places = hit["_source"].get("biz_locations") or []
            except (KeyError, TypeError, AttributeError):
                continue
            if places:
                locations[accession] = "; ".join(str(place) for place in places)
    return locations


def bay_area_filings(funding: pd.DataFrame, locations: dict[str, str]) -> pd.DataFrame:
    """Form D filings with HQ in the Bay Area, an industry that is not excluded and at least
    the minimum sold. `form_d_hq` says how HQ was placed: "zip" from the filing's ZIP code,
    or "edgar_city" when the address is outside California but EDGAR's business city is in
    the Bay Area (review only). `in_amount_range` is False for a stage above the amount
    rule: such a filing cannot make an account by itself, but it is shown to the reviewer."""
    table = funding.copy()
    sold = pd.to_numeric(table["amount_sold_usd"], errors="coerce")
    table["edgar_business_location"] = table["accession_number"].map(locations).fillna("")
    table["in_amount_range"] = ~table["stage_estimate"].isin(CONFIG["excluded_stages"])
    by_zip = table["hq_zip_check"] == "bay_area"
    by_edgar = (table["state"] != "CA") & table["edgar_business_location"].map(
        lambda location: any(is_bay_area_city(part) for part in location.split(";")))
    table["form_d_hq"] = ""
    table.loc[by_zip, "form_d_hq"] = "zip"
    table.loc[~by_zip & by_edgar, "form_d_hq"] = "edgar_city"
    usable = (table["industry_excluded"] != "True") & (sold >= CONFIG["min_amount_sold_usd"])
    return table[usable & (table["form_d_hq"] != "")].copy()


def candidate_filings(funding: pd.DataFrame, locations: dict[str, str]) -> pd.DataFrame:
    """The Bay Area filings that can make an account by themselves: also inside the amount rule."""
    table = bay_area_filings(funding, locations)
    return table[table["in_amount_range"]].copy()


def search_list(funding_csv: Path | None = None, raw_dir: Path | None = None) -> tuple[list[str], int]:
    """Name spellings to send to TheirStack for every candidate company, sorted, and the
    number of companies. Spellings of a company the reviewer rejected are left out. Raises
    FileNotFoundError if step 2 has not run."""
    candidates = candidate_filings(read_table(funding_csv or FUNDING_CSV), edgar_locations(raw_dir))
    issuers = candidates.drop_duplicates("cik")["issuer_name"]
    rejected = {clean_name(name) for name in rejected_company_names()}
    spellings: dict[str, str] = {}
    for issuer in issuers:
        for spelling in search_names(issuer):
            if clean_name(spelling) not in rejected:
                spellings.setdefault(spelling.lower(), spelling)
    return sorted(spellings.values(), key=str.lower), len(issuers)


def spelling_index(candidates: pd.DataFrame) -> dict[str, list[tuple[str, bool]]]:
    """Cleaned spelling -> [(issuer CIK, whether it is the full name)] for every candidate."""
    index: dict[str, list[tuple[str, bool]]] = {}
    for cik, issuer in candidates.drop_duplicates("cik")[["cik", "issuer_name"]].itertuples(index=False):
        full = clean_name(issuer)
        for spelling in search_names(issuer):
            key = clean_name(spelling)
            entry = (cik, key == full)
            if key and entry not in index.setdefault(key, []):
                index[key].append(entry)
    return index


def client_bonus_evidence(path: Path | None = None) -> dict[str, str]:
    """Evidence for the client-specific bonus, confirmed by the reviewer, by cleaned company
    name. The file is filled in by hand; rows without evidence are ignored."""
    path = path or CLIENT_BONUS_CSV
    if not path.exists():
        return {}
    table = read_table(path)
    if not {"company_name", "evidence"} <= set(table.columns):
        raise ValueError(f"{path.name} needs the columns company_name and evidence")
    return {clean_name(row.company_name): row.evidence.strip()
            for row in table.itertuples() if row.evidence.strip()}


def review_decisions(path: Path | None = None) -> dict[tuple[str, str], tuple[str, str]]:
    """The reviewer's overrides, as (decision, reason) by (cleaned TheirStack company name,
    cleaned Form D issuer). With an issuer, "accept" confirms that Form D match and "reject"
    refuses it. With the issuer blank, "reject" leaves the company out everywhere and
    "accept" keeps a company the rules would drop; a blank-issuer accept never confirms a
    Form D match. The file is filled in by hand."""
    path = path or REVIEW_DECISIONS_CSV
    if not path.exists():
        return {}
    table = read_table(path)
    needed = ["company_name", "form_d_issuer", "decision", "reason"]
    if not set(needed) <= set(table.columns):
        raise ValueError(f"{path.name} needs the columns {', '.join(needed)}")
    decisions: dict[tuple[str, str], tuple[str, str]] = {}
    for line, row in enumerate(table.itertuples(), start=2):
        key = (clean_name(row.company_name), clean_name(row.form_d_issuer))
        decision = row.decision.strip().lower()
        if not key[0] or decision not in DECISIONS:
            raise ValueError(f"{path.name} line {line}: needs a company_name and a decision of accept or reject")
        if key in decisions:
            raise ValueError(f"{path.name} line {line}: this company and issuer are already decided above")
        decisions[key] = (decision, row.reason.strip())
    return decisions


def domain_overrides(path: Path | None = None) -> dict[str, tuple[str, str]]:
    """Websites corrected by the reviewer, as (domain, reason) by cleaned company name. The
    file is filled in by hand. Raises ValueError for a missing column, an empty name, a
    value that is not a domain or a company listed twice."""
    path = path or DOMAIN_OVERRIDES_CSV
    if not path.exists():
        return {}
    table = read_table(path)
    needed = ["company_name", "domain", "reason"]
    if not set(needed) <= set(table.columns):
        raise ValueError(f"{path.name} needs the columns {', '.join(needed)}")
    overrides: dict[str, tuple[str, str]] = {}
    for line, row in enumerate(table.itertuples(), start=2):
        key, domain = clean_name(row.company_name), row.domain.strip().lower()
        if not key or not re.fullmatch(r"[a-z0-9-]+(\.[a-z0-9-]+)+", domain):
            raise ValueError(f"{path.name} line {line}: needs a company_name and a domain such as example.com")
        if key in overrides:
            raise ValueError(f"{path.name} line {line}: this company already has a domain above")
        overrides[key] = (domain, " ".join(row.reason.replace(";", ",").split()))   # a flag never holds "; "
    return overrides


def rejected_company_names(path: Path | None = None) -> list[str]:
    """Company names, as the reviewer wrote them, rejected for every Form D issuer. Step 1
    leaves them out of its searches, so their jobs cost no credits."""
    path = path or REVIEW_DECISIONS_CSV
    if not path.exists():
        return []
    review_decisions(path)  # raises ValueError if the file is malformed
    return sorted({row.company_name.strip() for row in read_table(path).itertuples()
                   if row.decision.strip().lower() == "reject" and not clean_name(row.form_d_issuer)})


def distinct_titles(titles: pd.Series) -> list[str]:
    """Job titles with repeats removed (case and spacing ignored), sorted."""
    unique: dict[str, str] = {}
    for title in titles:
        tidy = " ".join(str(title).split())
        if tidy:
            unique.setdefault(tidy.lower(), tidy)
    return sorted(unique.values(), key=str.lower)


def score_account(first_sale: str, filing_date: str, roles: int, has_fde: bool, bonus_evidence: str,
                  as_of: date) -> tuple[int, str]:
    """Score one account with the weights in CONFIG; return the score and its reasons."""
    reasons = []
    if not first_sale and not filing_date:
        recency, label = 0, "no Bay Area Form D filing found in the funding window"
    else:
        if first_sale:
            days = (as_of - date.fromisoformat(first_sale)).days
            label = f"first sale {first_sale}, {days} days before {as_of}"
        else:
            days = (as_of - date.fromisoformat(filing_date)).days
            label = f"no first-sale date; filed {filing_date}, {days} days before {as_of}"
        recency = next((points for limit, points in CONFIG["recency_points"] if days <= limit), 0)
    reasons.append(f"{label}: +{recency}")
    role_points = min(roles * CONFIG["points_per_role"], CONFIG["max_role_points"])
    capped = " (capped)" if roles * CONFIG["points_per_role"] > CONFIG["max_role_points"] else ""
    reasons.append(f"{roles} relevant role title(s): +{role_points}{capped}")
    fde = CONFIG["fde_points"] if has_fde else 0
    reasons.append(f"FDE opening: +{fde}" if has_fde else "no FDE opening: +0")
    bonus = CONFIG["client_bonus_points"] if bonus_evidence else 0
    reasons.append(f"client-specific bonus confirmed by the reviewer (hand-edited): +{bonus}" if bonus_evidence
                   else "no client-specific bonus evidence: +0")
    return recency + role_points + fde + bonus, "; ".join(reasons)


def own_site_first(jobs: pd.DataFrame) -> pd.DataFrame:
    """Jobs ordered so that, for each posting, the copy found on the posting's own site (such
    as the company's careers page) comes before copies found elsewhere (such as Indeed)."""
    own = [urlsplit(str(source)).netloc == urlsplit(str(posting)).netloc
           for source, posting in zip(jobs["source_url"], jobs["posting_key"])]
    return jobs.assign(_own=own).sort_values(["_own", "job_id"], ascending=[False, True]).drop(columns="_own")


def salary_ranges(postings: pd.DataFrame) -> str:
    """Annual salary range in USD per distinct title, e.g. "Forward Deployed Engineer $150k-$200k"."""
    table = pd.DataFrame({
        "title": postings["job_title"].map(lambda title: " ".join(str(title).split())),
        "low": pd.to_numeric(postings["salary_min_usd"], errors="coerce"),
        "high": pd.to_numeric(postings["salary_max_usd"], errors="coerce"),
    })
    parts = []
    for _, same_title in table.groupby(table["title"].str.lower(), sort=True):
        values = [v for v in (same_title["low"].min(), same_title["high"].max()) if pd.notna(v)]
        if values:
            span = "-".join(dict.fromkeys(f"${value / 1000:,.0f}k" for value in values))
            parts.append(f"{same_title['title'].iloc[0]} {span}")
    return "; ".join(parts)


def staffing_hint(description: str) -> str:
    """The phrase in a company description that suggests a staffing or services firm, or ""."""
    for pattern in CONFIG["staffing_hints"]:
        found = re.search(pattern, description, re.I)
        if found:
            return found.group(0)
    return ""


def squash(text: str) -> str:
    """A name without case, punctuation, spaces or legal suffix: "Generalist AI, Inc." -> "generalistai"."""
    return clean_name(text).replace(" ", "")


def job_facts(company_jobs: pd.DataFrame) -> dict[str, Any]:
    """What one company's relevant postings say, for an accounts or review row."""
    company = company_jobs.iloc[0]
    recent_jobs = pd.to_numeric(company_jobs["theirstack_jobs_last_30_days"], errors="coerce").max()
    headcount = pd.to_numeric(company_jobs["employee_count"], errors="coerce").max()
    domain = next((d for d in company_jobs["company_domain"] if d), "")
    description = next((text for text in company_jobs["company_description"] if text), "")
    long_description = next((text for text in company_jobs["company_long_description"] if text), "")
    boards = {unquote(urlsplit(url).path.strip("/").split("/")[0]) for url in company_jobs["posting_key"]
              if urlsplit(url).netloc.lower() in CONFIG["job_board_hosts"]}
    pages = {unquote(urlsplit(str(url)).path.rstrip("/").rsplit("/", 1)[-1])
             for url in company_jobs["company_linkedin_url"] if url}
    return {
        "name": company["company_name"],
        "domain": domain,
        "city": str(company["company_city"]).strip(),
        "postal_code": company["company_postal_code"],
        "titles": distinct_titles(company_jobs["job_title"]),
        "has_fde": bool((company_jobs["role_category"] == "fde").any()),
        "job_urls": [key for key in company_jobs["posting_key"] if key.startswith("http")],
        "locations": "; ".join(sorted({str(place).strip() for place in company_jobs["job_location"]} - {""},
                                      key=str.lower)),
        "salaries": salary_ranges(company_jobs),
        "recent_jobs": "" if pd.isna(recent_jobs) else int(recent_jobs),
        "headcount": None if pd.isna(headcount) else float(headcount),
        "description": description,
        "long_description": long_description,
        "industry": str(company["company_industry"]).strip(),
        # TheirStack's own funding fields: a reference for the reader, never scored
        "theirstack_funding": ", last round ".join(part for part in (
            company["theirstack_funding_stage"], str(company["theirstack_last_funding_date"])[:10]) if part),
        "zip_outside": bool((company_jobs["hq_zip_check"] == "outside").any()),
        # Where the company names itself, used to confirm a short-form name match
        "identity": {
            "domain": {squash(domain.rsplit(".", 1)[0])} - {""},
            "job board": {squash(board) for board in boards} - {""},
            "LinkedIn page": {squash(page) for page in pages} - {""},
            "description": f" {clean_name(description + ' ' + long_description)} ",
        },
    }


def confirmed_by(issuer_name: str, matched_name: str, identity: dict[str, Any]) -> str:
    """The field that confirms a short-form name match, or "". A spelling of the Form D name
    that is longer than the name that matched must be the end of the company's domain, its
    job board name or its LinkedIn page name, or appear as whole words in its description."""
    matched = squash(matched_name)
    for spelling in search_names(issuer_name):
        key = squash(spelling)
        if len(key) < CONFIG["min_confirming_letters"] or len(key) <= len(matched):
            continue
        if any(value.endswith(key) for value in identity["domain"]):
            return "domain"
        for field in ("job board", "LinkedIn page"):
            if key in identity[field]:
                return field
        if f" {clean_name(spelling)} " in identity["description"]:
            return "description"
    return ""


def hq_doubts(facts: dict[str, Any]) -> list[str]:
    """Reasons to doubt that the company's HQ is in the Bay Area, from TheirStack's own data."""
    doubts = []
    if facts["zip_outside"]:
        doubts.append("TheirStack HQ ZIP is outside the Bay Area")
    if not is_bay_area_city_name(facts["city"]):
        doubts.append(f"TheirStack HQ city is {facts['city']}, not a Bay Area city" if facts["city"]
                      else "TheirStack has no HQ city for this company")
    return doubts


def services_doubts(facts: dict[str, Any]) -> list[str]:
    """Signs in TheirStack's data that the company is a staffing or services firm."""
    doubts = []
    hint = staffing_hint(facts["description"]) or staffing_hint(facts["long_description"])
    if hint:
        doubts.append(f'the description suggests a staffing or services firm ("{hint}")')
    if facts["industry"] in CONFIG["review_industries"]:
        doubts.append(f"TheirStack industry is {facts['industry']}")
    return doubts


def posts_for_others(facts: dict[str, Any]) -> str:
    """How a company's postings compare with its size, when there are too many for it to be
    hiring only for itself ("74 postings in 30 days against 9 employees"), or ""."""
    headcount, recent = facts["headcount"], facts["recent_jobs"]
    if not headcount or recent == "" or recent <= CONFIG["max_postings_per_employee"] * headcount:
        return ""
    return f"{recent} postings in 30 days against {headcount:.0f} employees"


def funnel_table(total: int, block_list: int, no_core_role: int, no_bay_area_hq: int,
                 posts_for_other_companies: int, listed: int) -> list[tuple[str, int]]:
    """Where every company with a relevant posting went. Raises RuntimeError if the parts do
    not add up to the total, which would mean a company was lost or counted twice."""
    rows = [
        ("companies with a relevant posting", total),
        ("on the reviewer's block list", block_list),
        ("dropped: no usable Form D filing and no core role", no_core_role),
        ("dropped: no Bay Area HQ", no_bay_area_hq),
        ("dropped: posts jobs for other companies", posts_for_other_companies),
        ("listed in accounts.csv", listed),
    ]
    if total != sum(count for _, count in rows[1:]):
        raise RuntimeError(f"the funnel does not add up: {rows}")
    return rows


def build(filings: pd.DataFrame, jobs: pd.DataFrame, bonus: dict[str, str], as_of: date,
          decisions: dict[tuple[str, str], tuple[str, str]] | None = None,
          domains: dict[str, tuple[str, str]] | None = None,
          ) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    """Join relevant jobs to Form D filings by name; return the accounts, the rows the rules
    left out or did not use, and counts.

    `filings` comes from bay_area_filings() (or candidate_filings()); `decisions` are the
    reviewer's overrides, from review_decisions(). Rules decide every uncertain case, so no
    company waits for a person: a doubt about a listed company goes into its `flags`.
    `domains` are the hand-corrected websites, from domain_overrides().
    """
    decisions, domains = decisions or {}, domains or {}
    index = spelling_index(filings)
    by_cik = {cik: group.sort_values(["first_sale_date", "filing_date"], ascending=False)
              for cik, group in filings.groupby("cik")}
    relevant = jobs[jobs["role_category"] != "other"].copy()
    relevant["company_key"] = relevant["theirstack_company_id"].where(
        relevant["theirstack_company_id"] != "", relevant["company_name"])
    url = relevant["posting_url"].where(relevant["posting_url"] != "", relevant["job_url"])
    relevant["posting_key"] = url.where(url != "", "job " + relevant["job_id"])
    postings = own_site_first(relevant).drop_duplicates(["company_key", "posting_key"])

    # One group per TheirStack company, with the Form D issuers its name matches
    groups = [(company_jobs, index.get(clean_name(company_jobs["company_name"].iloc[0]), []))
              for _, company_jobs in postings.groupby("company_key")]
    companies_per_cik = Counter(cik for _, matches in groups for cik, _ in matches)

    accounts, review, accepted, rejected, used, corrected = [], [], [], [], set(), set()
    dropped: Counter[str] = Counter()

    def account_row(facts: dict[str, Any], tier: str, hq: str, primary: Any, filing_urls: list[str],
                    score: int, reason: str, evidence: str, flags: list[str]) -> dict[str, Any]:
        return {
            "company_name": facts["name"], "domain": facts["domain"], "signal_tier": tier, "hq_location": hq,
            "filing_date": "" if primary is None else primary["filing_date"],
            "amount_sold_usd": "" if primary is None else primary["amount_sold_usd"],
            "theirstack_funding": facts["theirstack_funding"],
            "relevant_roles_count": len(facts["titles"]), "role_titles": "; ".join(facts["titles"]),
            "fde_opening": facts["has_fde"], "job_locations": facts["locations"],
            "salary_ranges": facts["salaries"], "theirstack_jobs_last_30_days": facts["recent_jobs"],
            "company_description": facts["description"], "client_bonus_evidence": evidence,
            "score": score, "score_reason": reason, "flags": "; ".join(flags),   # no flag contains "; "
            "source_urls": " ".join(filing_urls + facts["job_urls"]),
            "_first_sale": "" if primary is None else primary["first_sale_date"],
            "_tier_rank": 0 if tier == "funded_and_hiring" else 1,
        }

    def review_row(facts: dict[str, Any], tier: str, outcome: str, reasons: list[str], primary: Any) -> dict[str, Any]:
        form_d = {column: "" if primary is None else primary[source] for column, source in (
            ("form_d_issuer", "issuer_name"), ("form_d_city", "city"), ("form_d_state", "state"),
            ("form_d_zip", "zip_code"), ("edgar_business_location", "edgar_business_location"),
            ("first_sale_date", "first_sale_date"), ("amount_sold_usd", "amount_sold_usd"),
            ("filing_url", "filing_url"))}
        return {
            "outcome": outcome, "reasons": "; ".join(reasons), "signal_tier": tier,
            "theirstack_company": facts["name"],
            "company_domain": facts["domain"], "company_description": facts["description"],
            "theirstack_city": facts["city"], "theirstack_postal_code": facts["postal_code"], **form_d,
            "relevant_roles_count": len(facts["titles"]), "role_titles": "; ".join(facts["titles"]),
            "job_locations": facts["locations"], "job_urls": " ".join(facts["job_urls"]),
        }

    for company_jobs, matches in groups:
        facts = job_facts(company_jobs)
        name_key = clean_name(facts["name"])
        company_decision, company_why = decisions.get((name_key, ""), ("", ""))
        if company_decision:
            used.add((name_key, ""))
        if company_decision == "reject":
            rejected.append(f"{facts['name']}: {company_why}" if company_why else facts["name"])
            continue

        # 1. Which Form D issuer, if any, is this company? The reviewer's accept, a full name that
        #    fits one issuer only, or a short form that another field confirms.
        usable: list[tuple[int, str, str, str]] = []   # (rank, cik, how it was confirmed, the reviewer's reason)
        unused: list[tuple[str, str]] = []             # (cik, why the match is not used)
        for cik, is_full in matches:
            issuer = by_cik[cik].iloc[0]["issuer_name"]
            join_key = (name_key, clean_name(issuer))
            decision, why = decisions.get(join_key, ("", ""))
            if decision:
                used.add(join_key)
            if decision == "accept":
                usable.append((0, cik, "reviewer", why))
            elif decision == "reject":
                unused.append((cik, "the reviewer rejected this match" + (f": {why}" if why else "")))
            elif companies_per_cik[cik] > 1:
                unused.append((cik, "several TheirStack companies match this issuer"))
            elif is_full and len(matches) == 1:
                usable.append((1, cik, "full name", ""))
            else:
                field = confirmed_by(issuer, facts["name"], facts["identity"])
                if field:
                    usable.append((1, cik, field, ""))
                elif len(matches) > 1:
                    unused.append((cik, "the name matches several Form D issuers and no other field confirms this one"))
                else:
                    unused.append((cik, "matched on a short form or part of the Form D name, "
                                        "and no other field confirms it"))
        usable.sort()
        best = [item for item in usable if item[0] == usable[0][0]] if usable else []
        if len(best) > 1:   # two issuers fit equally well, so neither is used
            unused += [(cik, "several Form D issuers fit this company equally well") for _, cik, _, _ in usable]
            best = []
        else:
            unused += [(cik, "another Form D issuer fits this company better") for _, cik, _, _ in usable[1:]]
        kept_by_reviewer = company_decision == "accept" or bool(best and best[0][2] == "reviewer")

        def leave_out(tier: str, reasons: list[str], primary: Any) -> None:
            """Record why this company is not listed: one row, or one per unused Form D match."""
            if unused:
                review.extend(review_row(facts, tier, "not listed", [problem] + reasons, by_cik[cik].iloc[0])
                              for cik, problem in unused)
            else:
                review.append(review_row(facts, tier, "not listed", reasons, primary))

        # 2. Funding and HQ
        flags: list[str] = []
        if best:
            _, cik, how, reviewer_why = best[0]
            issuer_filings = by_cik[cik]
            in_range = issuer_filings[issuer_filings["in_amount_range"]]
            above = issuer_filings[~issuer_filings["in_amount_range"]]
            primary = (issuer_filings if how == "reviewer" or in_range.empty else in_range).iloc[0]
            tier, filing_urls = "funded_and_hiring", list(issuer_filings["filing_url"])
            if how not in ("reviewer", "full name"):
                flags.append(f"matched on a short form of the Form D name, confirmed by the company's {how}")
            if not above.empty:
                largest = pd.to_numeric(above["amount_sold_usd"], errors="coerce").max()
                flags.append(f"{'only' if in_range.empty else 'also'} filed {len(above)} Form D filing(s) above "
                             f"the amount rule (largest sold ${largest:,.0f})")
            doubts = hq_doubts(facts)
            if doubts:
                flags.append(" and ".join(doubts) + " (the Form D address is used)")
            if (issuer_filings["form_d_hq"] == "edgar_city").any():
                flags.append(f"Form D address is in {primary['city']}, {primary['state']}, and EDGAR's business "
                             f"city is {primary['edgar_business_location']}")
            hq = (f"{primary['edgar_business_location']} (EDGAR business address)"
                  if primary["form_d_hq"] == "edgar_city" else
                  f"{primary['city'].title()}, {primary['state']} {primary['zip_code']}".strip())
            evidence = bonus.get(name_key) or bonus.get(clean_name(primary["issuer_name"]), "")
        else:
            primary, tier, filing_urls, how, reviewer_why = None, "hiring_only", [], "", ""
            if not company_jobs["role_category"].isin(CONFIG["hiring_only_roles"]).any():
                dropped["no_core_role"] += 1   # no usable Form D filing and no core role: not an account
                review.extend(review_row(facts, tier, "not listed", [problem, "no core role"], by_cik[cik].iloc[0])
                              for cik, problem in unused)
                continue
            doubts = hq_doubts(facts)
            if doubts and not kept_by_reviewer:
                dropped["no_bay_area_hq"] += 1
                leave_out(tier, ["no Form D filing places the HQ, and " + doubts[0]] + doubts[1:], None)
                continue
            flags += doubts
            hq = f"{facts['city']} (TheirStack" + (f", ZIP {facts['postal_code']})" if facts["postal_code"] else ")")
            evidence = bonus.get(name_key, "")

        # 3. Staffing and services firms: flagged, and dropped when they post jobs for others
        services = services_doubts(facts)
        too_many = posts_for_others(facts) if services else ""
        if too_many and not kept_by_reviewer:
            dropped["posts_for_others"] += 1
            leave_out(tier, [f"posts jobs for other companies ({too_many})"] + services, primary)
            continue
        flags += services + ([too_many] if too_many else [])

        # 4. A website corrected by hand replaces TheirStack's in the row, and the flag says so
        if name_key in domains:
            domain, why = domains[name_key]
            flags.append(f"website set by the reviewer (hand-edited): {domain} instead of TheirStack's "
                         f"{facts['domain'] or 'empty domain'}" + (f", {why}" if why else ""))
            facts = {**facts, "domain": domain}
            corrected.add(name_key)

        # 5. Score and list
        first_sale, filing_date = ("", "") if primary is None else (primary["first_sale_date"], primary["filing_date"])
        score, reason = score_account(first_sale, filing_date, len(facts["titles"]), facts["has_fde"], evidence, as_of)
        if how == "reviewer":
            reason += "; match confirmed by the reviewer (hand-edited)" + (f": {reviewer_why}" if reviewer_why else "")
        elif company_decision == "accept":
            reason += "; accepted by the reviewer (hand-edited)" + (f": {company_why}" if company_why else "")
        if kept_by_reviewer:
            accepted.append(facts["name"])
        accounts.append(account_row(facts, tier, hq, primary, filing_urls, score, reason, evidence, flags))
        review.extend(review_row(facts, tier, "listed without this filing", [problem], by_cik[cik].iloc[0])
                      for cik, problem in unused)

    account_table = pd.DataFrame(accounts, columns=ACCOUNT_COLUMNS + ["_first_sale", "_tier_rank"])
    account_table = account_table.sort_values(
        ["score", "_tier_rank", "_first_sale", "company_name"], ascending=[False, True, False, True])[ACCOUNT_COLUMNS]
    review_table = pd.DataFrame(review, columns=REVIEW_COLUMNS)
    in_range_ciks = set(filings.loc[filings["in_amount_range"], "cik"])
    counts = {
        "candidate_filings": int(filings["in_amount_range"].sum()),
        "candidate_companies": len(in_range_ciks),
        "edgar_city_companies": filings.loc[filings["in_amount_range"] & (filings["form_d_hq"] == "edgar_city"),
                                            "cik"].nunique(),
        "jobs": len(jobs),
        "relevant_jobs": len(relevant),
        "relevant_postings": len(postings),
        "companies_with_relevant_jobs": len(groups),
        "matched_companies": len(set(companies_per_cik) & in_range_ciks),
        "accounts": len(account_table),
        "funded_accounts": int((account_table["signal_tier"] == "funded_and_hiring").sum()),
        "hiring_only_accounts": int((account_table["signal_tier"] == "hiring_only").sum()),
        "flagged_accounts": int((account_table["flags"] != "").sum()),
        "no_filing_no_core_role": dropped["no_core_role"],
        "dropped_no_bay_area_hq": dropped["no_bay_area_hq"],
        "dropped_posts_for_others": dropped["posts_for_others"],
        "review_rows": len(review_table),
        "accepted_by_reviewer": accepted,
        "rejected_by_reviewer": rejected,
        "unused_decisions": len(set(decisions) - used),
        "corrected_domains": len(corrected),
        "unused_domain_overrides": len(set(domains) - corrected),
        "funnel": funnel_table(len(groups), len(rejected), dropped["no_core_role"], dropped["no_bay_area_hq"],
                               dropped["posts_for_others"], len(account_table)),
    }
    return account_table, review_table, counts


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    """Read the command line."""
    parser = argparse.ArgumentParser(description="Join Form D funding with AI engineering jobs.")
    parser.add_argument("--as-of", type=date.fromisoformat, default=date.today(),
                        help="date that funding recency is counted to (default: today)")
    return parser.parse_args(argv)


def shown(path: Path) -> Path:
    """A path relative to the project, for log lines."""
    return path.relative_to(ROOT) if path.is_relative_to(ROOT) else path


def main(argv: list[str] | None = None) -> int:
    """Build data/accounts.csv, data/review_matches.csv and data/funnel.csv."""
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    for path, step in ((FUNDING_CSV, "fetch_funding.py"), (JOBS_CSV, "fetch_jobs.py")):
        if not path.exists():
            log.error("%s is missing; run %s first.", shown(path), step)
            return 1
    try:
        bonus = client_bonus_evidence()
        decisions = review_decisions()
        domains = domain_overrides()
    except ValueError as err:
        log.error("%s", err)
        return 1
    filings = bay_area_filings(read_table(FUNDING_CSV), edgar_locations())
    try:
        accounts, review, counts = build(filings, read_table(JOBS_CSV), bonus, args.as_of, decisions, domains)
    except RuntimeError as err:   # the funnel does not add up: write nothing
        log.error("%s", err)
        return 1
    ACCOUNTS_CSV.parent.mkdir(parents=True, exist_ok=True)
    accounts.to_csv(ACCOUNTS_CSV, index=False)
    review.to_csv(REVIEW_CSV, index=False)
    pd.DataFrame(counts["funnel"], columns=["step", "companies"]).to_csv(FUNNEL_CSV, index=False)

    log.info("Form D candidates: %d filings from %d companies (%d placed in the Bay Area only by "
             "EDGAR's business city).", counts["candidate_filings"], counts["candidate_companies"],
             counts["edgar_city_companies"])
    log.info("Jobs read: %d, of which %d are relevant roles: %d distinct postings at %d companies.",
             counts["jobs"], counts["relevant_jobs"], counts["relevant_postings"],
             counts["companies_with_relevant_jobs"])
    rate = counts["matched_companies"] / counts["candidate_companies"] if counts["candidate_companies"] else 0
    log.info("Candidate companies with a relevant job: %d of %d (%.0f%%).",
             counts["matched_companies"], counts["candidate_companies"], 100 * rate)
    # Counts only: these lines also end up in the public log of the weekly run
    log.info("Wrote %d accounts to %s, scored as of %s. Nothing waits for review: %s lists the %d row(s) "
             "the rules left out or did not use.", counts["accounts"], shown(ACCOUNTS_CSV), args.as_of,
             shown(REVIEW_CSV), counts["review_rows"])
    log.info("Accounts by tier: %d funded and hiring, %d hiring only (no Form D filing used). %d account(s) "
             "carry a flag.", counts["funded_accounts"], counts["hiring_only_accounts"], counts["flagged_accounts"])
    log.info("Funnel: %d companies with a relevant posting = %d listed + %d on the reviewer's block list + %d with "
             "no usable filing and no core role + %d without a Bay Area HQ + %d posting jobs for other companies "
             "(%s).", counts["companies_with_relevant_jobs"], counts["accounts"], len(counts["rejected_by_reviewer"]),
             counts["no_filing_no_core_role"], counts["dropped_no_bay_area_hq"], counts["dropped_posts_for_others"],
             shown(FUNNEL_CSV))
    if decisions:
        log.info("Reviewer's decisions (%s): %d kept or confirmed, %d on the block list.", shown(REVIEW_DECISIONS_CSV),
                 len(counts["accepted_by_reviewer"]), len(counts["rejected_by_reviewer"]))
    if counts["unused_decisions"]:
        log.warning("%d row(s) in %s match no company or Form D match in this run; check the names.",
                    counts["unused_decisions"], shown(REVIEW_DECISIONS_CSV))
    if domains:
        log.info("Websites corrected by hand (%s): %d used.", shown(DOMAIN_OVERRIDES_CSV), counts["corrected_domains"])
    if counts["unused_domain_overrides"]:
        log.warning("%d row(s) in %s match no listed company in this run; check the names.",
                    counts["unused_domain_overrides"], shown(DOMAIN_OVERRIDES_CSV))
    if not bonus:
        log.info("No client-specific bonus file (%s), so no company gets those points.", shown(CLIENT_BONUS_CSV))
    return 0


if __name__ == "__main__":
    sys.exit(main())
