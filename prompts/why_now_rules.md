# Rules for "why now" lines

A "why now" line is one sentence per account that says why to contact the company this week. It is the one step of this project where a language model writes text that a customer might read, so it has written rules, a script that checks the result against the data, and a person who reads every line.

**Who writes them.** An AI model, during review, in a session where it can read the account data. The weekly job never writes these lines and makes no model call.

**What the model may use.** The company's row in `data/accounts.csv` and its rows in `data/jobs.csv`. Nothing from memory and nothing from the web.

## Rules

1. One sentence of at most 40 words, about the company, not about the client.
2. Lead with the newest dated fact: a posting date or a Form D filing date.
3. Use only facts in the row: job title, job location, salary range, posting date; Form D filing date, first-sale date and amount sold; TheirStack's 30-day posting count; headcount; what the company's own description says it does. A role that is remote, outside the Bay Area or without a city is described as such.
4. Money is written as "Form D ... reporting $X sold". Never a round name and never "raised": Form D has no round name.
5. TheirStack's funding stage is unverified, so it stays out of the line. It may appear in a separate check column, labeled as unverified.
6. No people: no names, no founders, nothing about a person's language or origin.
7. No guesses about pain or intent, such as "struggling to hire" or "scaling fast".
8. Doubts go in a separate check column, not in the line.

## What the script checks

`python why_now_check.py --lines <file>` exits with code 1 when any line fails:

- Every account has one line, and no line belongs to a company that is not an account.
- At most 40 words.
- Every date such as "Nov 12" or "Aug 2025" matches a posting, filing or first-sale date of that company.
- Every number matches the row: amount sold, a salary bound, headcount, the 30-day count, or a number in the company's own description.
- None of the banned words: a round name, "raised", a person's title.

Not checked by code: the wording, the short description of what the company does, rule 7, and whether a posting is still open (`verify_postings.py` checks that for postings on three job boards). A person reads each line.

## Results

- 2026-11-17: 34 lines written for the 34 accounts and checked, 0 problems. The lines stay out of the repository with the rest of the generated data.
- 11 known mistakes were planted in copies of those real lines: a posting date one day off (twice), a wrong amount, salary, headcount, 30-day count and month, another filing's amount, a round name, a person, and a line that was too long. All 11 were rejected.
- The same kinds of mistake, 17 of them on simulated data, are in `tests/check_why_now.py`, so anyone can run them again.

## Example (SIMULATED)

> Filed a Form D on Nov 13 reporting $30.0M sold (first sale Oct 29) and posted a Forward Deployed Engineer role on Nov 12 ($150k-$200k); a 60-person company with 15 postings in 30 days.
