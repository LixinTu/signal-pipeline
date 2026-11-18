# Signal Pipeline — GTM portfolio project

## What this project is
A small GTM pipeline that finds SF Bay Area AI startups that are hiring AI or forward deployed engineers and checks which of them recently raised money: the accounts a recruiting platform like Brix would want to contact now. This is a public portfolio project. The code, this file and the write-up go on GitHub, so keep everything clean and honest.

## Working with me
- Reply in the language I write in. Write all code, comments, commit messages and docs in English.
- Plan before coding: for each step, show a short plan and the files you will touch, then wait for my OK.
- Explain non-obvious steps in one or two plain sentences.
- After each step, run it, show the first 5 rows of output, and point out anything in the data that looks wrong.

## Target accounts
- HQ in the SF Bay Area (NYC is a later extension)
- Hiring for: AI engineer, ML engineer, applied AI engineer, forward deployed engineer, robotics engineer
- Raised money in the last 90 days, roughly Seed to Series C. Since 2026-11-16 this adds points instead of deciding who is listed, because Form D misses many rounds: a company with no Form D filing is still an account (tier `hiring_only`) if it is hiring a forward deployed or applied AI engineer
- Bonus: one client-specific signal that I define for the client at hand. Fill this only from evidence I give you, written next to the company in `data/manual/client_bonus.csv`, or from my manual check. Never infer it, from names or from anything else.

## Data sources (free tiers first)
- **Hiring: TheirStack Jobs API.** `POST https://api.theirstack.com/v1/jobs/search`. Read the docs first: https://theirstack.com/en/docs. Free plan: 200 API credits a month, 1 credit per job returned, and repeated requests are charged again. At most 25 results per page and 5 pages per search. Rate limits: 4 requests a second, 10 a minute, 50 an hour, 400 a day. Always set a small limit and a short posted-date window.
- **Funding: SEC EDGAR Form D.** Full-text search JSON at `https://efts.sec.gov/LATEST/search-index` with `forms=D`, then each filing's XML for issuer address, industry group, amounts and related persons. No key, but every request must send the User-Agent stored in `SEC_USER_AGENT` in `.env`. Stay far below 10 requests per second. Form D has no round name: treat amount sold as a rough stage estimate and label it as an estimate.
- **CRM: HubSpot API.** `https://api.hubapi.com/crm/v3/...` with a service key sent as a Bearer token. Free accounts allow 100 requests per 10 seconds.
- **Contacts and emails** are looked up by `find_contacts.py` through Hunter's documented API (free plan: 50 credits a month). Names and emails stay in git-ignored files on my machine and are never committed; logs and write-ups show counts only. To measure coverage, run it with `--outcomes-only`, which writes no name, email or position at all. Clay (Apollo first, Hunter as fallback) is the by-hand alternative.
- **Posting check: the public job-board endpoints of Ashby, Greenhouse and Lever**, which need no key. `verify_postings.py` asks each board once whether a posting is still listed. LinkedIn postings are never fetched.
- **Automation platform: n8n**, run from the command line in a throwaway Docker container, never as a server. The workflow file is `automation/n8n/weekly_digest.json` and may only read companies and create one task in HubSpot.
- **Sequencing: Apollo.** `apollo_sequence.py` may only create an inactive draft sequence whose emails wait for review. Adding contacts, approving emails and activating a sequence are mine, in Apollo.

## Rules
- Secrets live only in `.env`, never in code or commits. Keep a `.env.example` with variable names and placeholders. Add `.env` and `data/raw/` to `.gitignore` before the first commit.
- Do not scrape LinkedIn or any site whose terms forbid it. Use the APIs above.
- This repo and the AI agent never send emails or messages. Outreach is sent only from a sequencing tool, in my own name, after I approve each batch.
- Before any run that spends credits, estimate the credits and wait for my OK. Test on 5 records first.
- Cache every raw API response in `data/raw/` with a timestamp, so re-runs don't spend credits again.
- On HTTP 429, wait and retry with backoff. Log and skip bad records instead of crashing.
- The scheduled job makes no paid LLM API calls. "Why now" lines and email drafts are written during review, in Clay or in a Claude Code session.
- Keep it simple: Python 3.11+, `requests`, `pandas`, `python-dotenv`. Small readable scripts, type hints, short docstrings. No frameworks.

## Build order
1. `fetch_jobs.py` → `data/jobs.csv`. With `--funded-companies` it searches only the step 2 companies that can become accounts, so step 2 runs first. Add `--any-hq-city` to drop TheirStack's HQ city filter, since the Form D address places HQ. `--core-roles --one-per-company` is the hiring-first search: forward deployed and applied AI titles only, one job per new company, so one credit buys one company.
2. `fetch_funding.py` → `data/funding.csv` (Form D filings from the last 90 days, California issuers)
3. `build_accounts.py` → `data/accounts.csv`. Normalize company names (lowercase, drop Inc/LLC/Corp and punctuation), join jobs and funding, dedupe, score. Rules decide uncertain matches, so no run waits for me: a doubt about a listed company goes into its `flags` column, every company or filing the rules left out is listed with the reason in `data/review_matches.csv`, and `data/funnel.csv` counts where each company went. My overrides (accept or reject) go in `data/manual/review_decisions.csv` and a corrected website in `data/manual/domain_overrides.csv`; step 3 reads both on every run and labels what they change as hand-edited.
4. `push_hubspot.py`. Create or update companies in HubSpot. Search by domain first so re-runs don't create duplicates. Store the score and a `signal_stage` property: my working status (new, reviewed, ready_to_contact, contacted, rejected), set to `new` when a company is created and never overwritten. If this needs a scope the service key lacks, tell me which one to add.
5. `.github/workflows/weekly.yml`. Run steps 2, 1, 3 and 4, in that order, every Monday with keys from GitHub Secrets, with `verify_postings.py` after step 3, then run `digest_n8n.py` (the n8n workflow that writes the week's new accounts into one HubSpot task), append one row of counts to `docs/weekly_runs.csv` with `weekly_row.py`, commit that one file, and write a short run summary. The schedule spends at most 10 + 10 TheirStack credits a run.

## Scoring v1
Keep the weights in one config dict, and explain every score in a `score_reason` column.
- Funding recency: up to 30 days +3, 31 to 60 days +2, 61 to 90 days +1; no Form D filing found +0
- Relevant open roles: +1 each, max +3
- Has a forward deployed engineer opening: +1
- The client-specific bonus, confirmed in review: +2

## accounts.csv columns
company_name, domain, signal_tier, hq_location, filing_date, amount_sold_usd, theirstack_funding, relevant_roles_count, role_titles, fde_opening, job_locations, salary_ranges, theirstack_jobs_last_30_days, company_description, client_bonus_evidence, score, score_reason, flags, source_urls

## Portfolio docs (update as we go)
- `README.md`: problem → what I built → tools → results (companies found, match rate, credits used, run time) → what I'd change. Add a Mermaid architecture diagram and screenshots.
- `DECISIONS.md`: one or two lines per choice and its trade-off, for example why Form D instead of a paid funding database.
- Label any simulated or hand-edited data as such.
