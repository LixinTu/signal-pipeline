# Project 1: the Clay table

A step-by-step build of the target-account table in Clay, using Clay's 14-day trial.
Menu names and costs come from Clay's docs as of 2026-11-14 and may have changed; the cost Clay shows in each panel is the one that counts.

## Ground rules

- **Test on 5 rows first.** Read the cost Clay shows before every run, then run the rest.
- **No purchases.** If trial credits run low, stop and shrink the list. Don't buy credits.
- **No LinkedIn scraping.** Use Clay's own data and integrations, not the Chrome extension on LinkedIn pages.
- **No sending.** Email drafts are for review and the portfolio. Never send them in the name of a company you don't work for.
- **Mandarin-speaking lead: evidence only.** Record a link you have checked yourself (Chinese-language media, or the person's own public profile). Never infer it from a name.
- **Nothing personal in public screenshots.** Before a screenshot or the recording goes into the repo or a post, crop or blur your account email and every contact's name and email.
- **Save everything by trial day 12:** CSV exports (free), screenshots, the screen recording. Exports go in `data/clay/`, which git ignores.

## Trial limits and budget

- Trial tables hold at most 50 rows, and the trial unlocks Growth-plan features.
- This trial has **1,000 Data Credits and 5,000 Actions** (Settings → Usage, read on 2026-11-15). Unused credits expire when the trial ends. Automatic top-ups are not offered on trial plans. Write both balances down after each block of work.
- Each enriched record costs 1 Action plus the Data Credits shown next to the enrichment. Sourcing lists, formulas, filters, sending rows between tables and CSV export cost no Actions. Syncing records to a CRM such as HubSpot costs 1 Action per record.
- The news and fundraising signal costs 1 Action plus 6 Data Credits per result. Run it only on companies that are already hiring.
- The work email waterfall only charges for the provider that finds a match. Infer Email is free.

Budget set before any test run. Each cap is an upper limit; replace it with the measured number after each step.

| Step | Rows | Data Credits (cap) | Actions (cap) | Basis |
|---|---|---|---|---|
| Find Companies | 50 | 0 | 0 | Sourcing from Clay's own database; check Usage after import |
| Find Jobs | 50 | 150 | 50 | Cost per row not published; the 5-row test sets it. Above 3 per row, decide again |
| News & fundraising | about 25 hiring | 300 | 50 | 6 per result; about 2 results per company at most |
| Use AI: `funding`, `why_now` | about 25 × 2 | 50 | 50 | At most 1 per run with a low-cost model |
| Find People | about 60 | 0 | 0 | Clay's docs: finding people in its database costs 0 |
| Work email | about 30 | 60 | 30 | Clay's example is 0.5 per email found; the cap allows 2 |
| Use AI: `email_draft` | about 30 | 30 | 30 | At most 1 per run |
| **Total** | | **590 of 1,000** | **210 of 5,000** | |

The remaining 410 Data Credits or more cover re-runs. If the first 50 companies yield few recent raises, they can also cover a second batch of 50 (Find Companies with the first table under Exclude companies).

## Order of work

Cheap filters come first, so credits are spent only on companies that pass them.

```
Table 1 Companies (50) --Find Jobs--> hiring only --> Table 2 Accounts --funding signal, score--> top accounts --> Table 3 People --email, drafts
```

## Day 1: Table 1, Companies

1. **Find Companies.** On the home screen, **Create new → Workbook**. In the workbook, click **+ Add** at the bottom and search for Find Companies. Filters:
   - Location: United States; cities San Francisco, South San Francisco, Oakland, Berkeley, Emeryville, Palo Alto, Menlo Park, Mountain View, Sunnyvale, Santa Clara, San Jose, Redwood City, San Mateo, Burlingame, San Carlos.
   - Company type: Privately Held. Domain: has domain, domain is live.
   - Estimated employee count: 5 to 500.
   - Funding amount: at least $1M, to drop unfunded companies.
   - Keywords (include): artificial intelligence, machine learning, LLM, generative AI, AI agents, robotics.
   - Semantic description: "Venture-backed startup building AI products: LLM applications, AI agents, ML infrastructure, or robotics."
   - Result limit: 50.

   Before saving, write down the number of matching companies if the preview shows it: the README reports it next to the 50-row sample. Then **Save → Save to new table**. In the import options, add no enrichments (they cost credits), turn auto-update off, and keep auto-dedupe on. Check Usage afterwards: sourcing should cost 0 Data Credits.
2. **Find Jobs** (enrichment, company domain as input). Job keywords, at most 4 because Google Jobs accepts no more: `AI engineer`, `machine learning engineer`, `forward deployed engineer`, `robotics engineer`. Save without running, then run 5 rows: column header → Run column → choose the number of rows, or select 5 rows and run the column from the right-click menu. Check the cost and the output, then run the rest.
3. Three formula columns. Write each with Clay's formula generator (type `/` to insert a column), using these prompts:
   - `relevant_roles_count`: "Count the jobs in /Find Jobs whose title contains any of: AI engineer, ML engineer, machine learning engineer, applied AI, forward deployed, robotics engineer, case-insensitive. Return a number, 0 if none."
   - `role_titles`: "Join the matching job titles from /Find Jobs with '; '."
   - `fde_opening`: "Return true if any job title in /Find Jobs contains 'forward deployed', case-insensitive, otherwise false."
4. Filter to `relevant_roles_count` ≥ 1 and send those rows to a new table, **Table 2 Accounts**.

Screenshots: the Find Companies filter panel, and Table 1 with the job columns.

## Day 2: Table 2, Accounts

1. **Tools → Signals → News & fundraising.** Table: Table 2; domain column; topic: fundraising; one result per domain, if offered; frequency: Monthly, so it runs once during the trial. Check the estimated cost Clay shows, then save and run 5 rows before the rest.
2. **Use AI** column `funding`, using a low-cost content model (the dropdown shows each model's cost):
   ```
   Read this news item about {{Company Name}}: {{News & fundraising}}
   Return JSON with these keys:
   - "round": one of "Pre-seed", "Seed", "Series A", "Series B", "Series C", "Later", "Unknown"
   - "amount_usd": the amount raised as a number, or null
   - "announced_date": the announcement date as YYYY-MM-DD, or null
   - "source_url": the article URL, or null
   Use only facts stated in the item. If it does not say, use "Unknown" or null. Do not guess.
   ```
   Add the four keys as output fields if the panel offers it.
3. Two manual columns, filled only after you check the evidence yourself: `mandarin_lead_evidence` (text: link and one-line note) and `mandarin_confirmed` (checkbox).
4. Scoring formulas (same weights as Scoring v1 in CLAUDE.md):
   - `days_since_raise`: `{{announced_date}} ? moment().diff(moment({{announced_date}}), "days") : ""`
   - `funding_points`: `{{days_since_raise}} === "" || {{days_since_raise}} == null ? 0 : ({{days_since_raise}} <= 30 ? 3 : {{days_since_raise}} <= 60 ? 2 : {{days_since_raise}} <= 90 ? 1 : 0)`
   - `roles_points`: `Math.min(Number({{relevant_roles_count}}) || 0, 3)`
   - `fde_points`: `String({{fde_opening}}).toLowerCase() === "true" ? 1 : 0`
   - `mandarin_points`: `String({{mandarin_confirmed}}).toLowerCase() === "true" ? 2 : 0`
   - `score`: `Number({{funding_points}}) + Number({{roles_points}}) + Number({{fde_points}}) + Number({{mandarin_points}})`
   - `score_reason`:
     ```
     [
       Number({{funding_points}}) > 0 ? "Funding news " + {{days_since_raise}} + " days ago (+" + {{funding_points}} + ")" : "No funding news in the last 90 days (+0)",
       {{relevant_roles_count}} + " relevant open roles (+" + {{roles_points}} + ", max 3)",
       Number({{fde_points}}) > 0 ? "Forward deployed engineer opening (+1)" : "",
       Number({{mandarin_points}}) > 0 ? "Mandarin-speaking lead confirmed in review (+2)" : ""
     ].filter(Boolean).join("; ")
     ```
   Check the formulas on 5 rows, including one with no funding news: it should get `funding_points` 0.
5. **Use AI** column `why_now`:
   ```
   Write one sentence (at most 25 words) on why {{Company Name}} may need help hiring AI engineers now.
   Use only these facts: funding {{round}} {{amount_usd}} announced {{days_since_raise}} days ago; open roles: {{role_titles}}.
   If a fact is missing, leave it out. Do not add any other facts.
   ```
6. Sort by `score` and pick the top accounts for Table 3, about 20, fewer if credits are low.

Screenshots: the signal setup with its cost estimate, the scoring formulas, and Table 2 sorted by score.

## Day 3: Table 3, People

1. **Actions → Find People at These Companies**, from the top accounts. Job title "is similar to": Founder, Co-Founder, CEO, CTO, VP of Engineering, Head of Engineering, Head of Talent, Head of Recruiting. Up to 3 per company.
2. Keep 1 or 2 per company before spending on emails. Seed and Series A: founder or CEO. Series B and C: head of talent or CTO.
3. **Work Email**, full configuration: Infer Email on; put Apollo first and Hunter second if both are in the provider list (otherwise keep Clay's order and note it); validation on. Run 5 rows, check the cost, then the rest.
4. **Use AI** column `email_draft`:
   ```
   Draft a short cold email (at most 120 words) to {{First Name}}, {{Job Title}} at {{Company Name}}.
   Purpose: offer to share three pre-screened candidates for their open AI engineering roles.
   Open with this reason, in your own words: {{why_now}}
   End with one question that is easy to answer.
   Plain text, no links, no claims beyond the facts given. Sign off as [Your name], [Company].
   ```
5. Read every draft and fix anything wrong. Mark hand-edited drafts as hand-edited.

Screenshots: the people filters, and the email waterfall configuration showing the provider order.

## Day 4: results

1. Export each table to CSV. Hide helper columns first; hidden columns are left out of the export.
2. Write down the funnel and the cost:

   | Metric | Value |
   |---|---|
   | Companies sourced | |
   | Hiring for a relevant role | |
   | Funding news in the last 90 days | |
   | Contacts found | |
   | Work emails found (find rate) | |
   | Data credits and actions used | |
   | Data credits per contact with an email | |
   | Time spent | |

3. Record a 2-minute screen walk-through: the problem, the table left to right, the funnel numbers, what you'd change.

Days 5 to 12: fix problems, and optionally sync Table 2 to HubSpot from Clay (search by domain, so re-runs don't create duplicates).

## Sources

- [Clay pricing](https://www.clay.com/pricing) and [free trial FAQ](https://clay.com/faq/what-do-i-get-in-the-free-trial)
- [Actions & Data Credits](https://university.clay.com/docs/actions-data-credits)
- [Find Companies](https://university.clay.com/docs/find-companies) and [Find People](https://university.clay.com/docs/find-people-overview)
- [Finding Jobs (Clay 101)](https://university.clay.com/lessons/find-jobs-source-enrichment-clay-101)
- [Signals](https://university.clay.com/docs/signals) and [Monitor for news & fundraising](https://university.clay.com/docs/monitor-for-news-fundraising)
- [Work Email waterfall](https://university.clay.com/docs/work-email-waterfall)
- [How AI is priced](https://university.clay.com/docs/ai-pricing) and [Formulas](https://university.clay.com/docs/formula-generator)
- [Download as a CSV](https://university.clay.com/lessons/download-as-a-csv-clay-101)
