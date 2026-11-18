"""Plant one bug at a time in a copy of a script and confirm that the script's checks fail.

A check that cannot fail proves nothing. Each entry below is a small, realistic bug: a
guard switched off, a boundary moved by one, a secret written where it should not be. The
runner copies the project's scripts, checks and workflow files to a temporary folder (never
the .env file), first confirms that each check file passes on the unchanged copy, then
applies one change, runs that script's check file and expects it to fail, and undoes the
change. The project itself is never changed, no API is called and nothing is installed.

Run: python tests/planted_bugs.py                    # every planted bug; takes a few minutes
     python tests/planted_bugs.py find_contacts.py   # only the bugs planted in one file
     python tests/planted_bugs.py --list             # print the bugs without running anything

Exit code 1 if a planted bug goes unnoticed, if a check file fails before any bug is
planted, or if the text a bug replaces is no longer in the script exactly once: the script
changed, and the entry has to be updated with it.
tests/run_all.py does not run this file, because it takes minutes instead of seconds.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
COPIED = ["tests", "automation", ".github", "docs", "prompts"]   # folders the checks read, next to the root scripts

# (file the bug is planted in, the check file that must notice, [(what the bug is, old text, new text)])
PLANTED: list[tuple[str, str, list[tuple[str, str, str]]]] = [
    ("build_accounts.py", "check_build_accounts.py", [
        ("an unconfirmed short form of a name is accepted anyway",
         '                if field:\n                    usable.append((1, cik, field, ""))',
         '                if True:\n                    usable.append((1, cik, field or "domain", ""))'),
        ("the longer name may sit anywhere in the domain",
         'if any(value.endswith(key) for value in identity["domain"]):', 'if any(key in value for value in identity["domain"]):'),
        ("the description is matched without word boundaries",
         'if f" {clean_name(spelling)} " in identity["description"]:', 'if key in identity["description"].replace(" ", ""):'),
        ("a shorter spelling is allowed to confirm a match", "or len(key) <= len(matched):", "or False:"),
        ("the block list is ignored", '        if company_decision == "reject":', '        if company_decision == "never":'),
        ("the reviewer's rejected match is used anyway", '            elif decision == "reject":', '            elif decision == "never":'),
        ("an issuer matched by two companies is used for both",
         "            elif companies_per_cik[cik] > 1:", "            elif companies_per_cik[cik] > 99:"),
        ("a full name that fits two issuers is used", "            elif is_full and len(matches) == 1:", "            elif is_full:"),
        ("staffing firms that post for others are kept",
         "        if too_many and not kept_by_reviewer:", "        if False and too_many and not kept_by_reviewer:"),
        ("the posting rule ignores the staffing hint",
         'too_many = posts_for_others(facts) if services else ""', "too_many = posts_for_others(facts)"),
        ("exactly 5 postings per employee is dropped",
         'or recent <= CONFIG["max_postings_per_employee"] * headcount:', 'or recent < CONFIG["max_postings_per_employee"] * headcount:'),
        ("the reviewer's accept does not protect a company",
         '        kept_by_reviewer = company_decision == "accept" or bool(best and best[0][2] == "reviewer")',
         "        kept_by_reviewer = False"),
        ("a hiring-only company outside the Bay Area is listed",
         "            if doubts and not kept_by_reviewer:", "            if False and doubts and not kept_by_reviewer:"),
        ("the TheirStack HQ city is not checked", '    if not is_bay_area_city_name(facts["city"]):', "    if False:"),
        ("flags are not written",
         '"score": score, "score_reason": reason, "flags": "; ".join(flags),', '"score": score, "score_reason": reason, "flags": "",'),
        ("a filing above the amount rule becomes the primary one",
         'primary = (issuer_filings if how == "reviewer" or in_range.empty else in_range).iloc[0]', "primary = issuer_filings.iloc[0]"),
        ("filings above the amount rule are not shown", "            if not above.empty:", "            if False:"),
        ("the funnel check is switched off", "    if total != sum(count for _, count in rows[1:]):", "    if False:"),
        ("a dropped company is not counted", '            dropped["posts_for_others"] += 1\n', ""),
        ("unused filings are not written down",
         '        review.extend(review_row(facts, tier, "listed without this filing", [problem], by_cik[cik].iloc[0])\n'
         "                      for cik, problem in unused)", "        pass"),
        ("the EDGAR city is not shown as HQ", 'if primary["form_d_hq"] == "edgar_city" else', "if False else"),
        ("the core-role rule is dropped",
         '            if not company_jobs["role_category"].isin(CONFIG["hiring_only_roles"]).any():', "            if False:"),
        ("copies of one posting are no longer merged", '.drop_duplicates(["company_key", "posting_key"])', ""),
        ("the copy found elsewhere is kept instead of the careers-page copy",
         'ascending=[False, True]).drop(columns="_own")', 'ascending=[True, True]).drop(columns="_own")'),
        ("the tier is ignored when sorting",
         '["score", "_tier_rank", "_first_sale", "company_name"], ascending=[False, True, False, True]',
         '["score", "_first_sale", "company_name"], ascending=[False, False, True]'),
        ("rejected companies are still searched", "            if clean_name(spelling) not in rejected:", "            if True:"),
        ("job records are counted instead of distinct titles",
         'score_account(first_sale, filing_date, len(facts["titles"]), facts["has_fde"]',
         'score_account(first_sale, filing_date, len(company_jobs), facts["has_fde"]'),
        ("a first sale exactly 30 days ago scores as 31", "if days <= limit), 0)", "if days < limit), 0)"),
        ("role points are not capped",
         'role_points = min(roles * CONFIG["points_per_role"], CONFIG["max_role_points"])', 'role_points = roles * CONFIG["points_per_role"]'),
        ("bonus points are given without evidence",
         '    bonus = CONFIG["client_bonus_points"] if bonus_evidence else 0', '    bonus = CONFIG["client_bonus_points"]'),
        ("a corrected website is used without a flag",
         "            flags.append(f\"website set by the reviewer (hand-edited): {domain} instead of TheirStack's \"\n"
         "                         f\"{facts['domain'] or 'empty domain'}\" + (f\", {why}\" if why else \"\"))\n", ""),
        ("a website correction for an unlisted company goes unnoticed",
         '        "unused_domain_overrides": len(set(domains) - corrected),', '        "unused_domain_overrides": 0,'),
        ("the domain file may name a company twice", "        if key in overrides:", "        if False:"),
        ("a reason may hold the flag separator", 'row.reason.replace(";", ",").split()', "row.reason.split()"),
    ]),
    ("fetch_jobs.py", "check_fetch_jobs.py", [
        ("--any-hq-city keeps the city filter", '        del body["company_location_pattern_or"]', "        pass"),
        ("the posting link keeps its query string", 'parts.path.rstrip("/"), "", ""', 'parts.path.rstrip("/"), parts.query, ""'),
        ("companies already received are not excluded", '            body["company_id_not"] = sorted(known_companies)', "            pass"),
        ("rejected names are not excluded", '        body["company_name_not"] = sorted(excluded_names)', "        pass"),
        ("--core-roles sends every title",
         '"job_title_or": CONFIG["core_title_keywords" if core_roles else "title_keywords"],', '"job_title_or": CONFIG["title_keywords"],'),
        ("jobs already received are not excluded, so they are paid for again", '            "job_id_not": sorted(known),', '            "job_id_not": [],'),
        ("a page asks for more jobs than the limit leaves",
         '            "limit": min(CONFIG["page_size"], wanted - received),', '            "limit": CONFIG["page_size"],'),
        ("a forward deployed title is not marked as one", '        "is_fde": role == "fde",', '        "is_fde": False,'),
    ]),
    ("fetch_funding.py", "check_fetch_funding.py", [
        ("amendments (D/A) are kept as new money", '            elif filing["form"] == "D/A":', "            elif False:"),
        ("window dates are taken in UTC instead of Eastern time", 'EDGAR_TZ = ZoneInfo("America/New_York")', 'EDGAR_TZ = ZoneInfo("UTC")'),
        ("an amount exactly at a stage cutoff falls in the lower stage", "        if amount_sold < cutoff:", "        if amount_sold <= cutoff:"),
        ("excluded industries are not excluded",
         '    return normalize_industry(group) in {normalize_industry(g) for g in CONFIG["excluded_industries"]}', "    return False"),
        ("a filing that sold nothing gets a stage", "    if amount_sold is None or amount_sold <= 0:", "    if amount_sold is None:"),
        ("a file that is not XML is read as a filing",
         'or not file_name.endswith(".xml") or form not in ("D", "D/A"):', 'or form not in ("D", "D/A"):'),
        ("the funding window is a day short", "    start = end - timedelta(days=args.days)", "    start = end - timedelta(days=args.days - 1)"),
    ]),
    ("names.py", "check_names.py", [
        ("legal suffixes stay in the join key", "    while words and words[-1] in LEGAL_SUFFIXES:\n        words.pop()\n", ""),
        ("a short form may be shorter than four letters", ") >= MIN_SHORT_LETTERS else None", ") >= 1 else None"),
        ("'&' is dropped instead of read as 'and'", 'text = str(name).lower().replace("&", " and ")', "text = str(name).lower()"),
    ]),
    ("push_hubspot.py", "check_push_hubspot.py", [
        ("an update resets signal_stage to new",
         'client.call("update", "PATCH", f"{COMPANIES_URL}/{company_id}", {"properties": push["properties"]})',
         'client.call("update", "PATCH", f"{COMPANIES_URL}/{company_id}", {"properties": {**push["properties"], '
         '"signal_stage": CONFIG["stage_on_create"]}})'),
        ("no search by domain: every run creates",
         "matches = find_companies(client, domain) or known_company(client, domain, ids)", "matches = []"),
        ("saved ids are ignored when the search finds nothing",
         "matches = find_companies(client, domain) or known_company(client, domain, ids)", "matches = find_companies(client, domain)"),
        ("the domain is not normalised", '        domain = normalise_domain(row["domain"])', '        domain = str(row["domain"]).strip()'),
        ("a create is sent again after a 5xx answer",
         '{"properties": properties}, repeat_safe=False)', '{"properties": properties}, repeat_safe=True)'),
        ("the raw log keeps the request headers, and with them the key",
         '"url": url, "request_body": body, "status": status, "response": response,',
         '"url": url, "request_body": body, "status": status, "response": response, "headers": dict(self.session.headers),'),
        ("with two companies for one domain, the newest is updated", "    return sorted(found, key=age)", "    return sorted(found, key=age, reverse=True)"),
        ("HTTP 429 is not retried", "                if status == 429:", "                if status == 430:"),
        ("the missing scope is not named", "    scope = SCOPES[action]\n    said", '    scope = "a scope"\n    said'),
        ("--limit is ignored", 'rows = table.to_dict("records")[: args.limit]', 'rows = table.to_dict("records")'),
        ("a failed row does not change the exit code", '    return 1 if stopped or counts["failed"] else 0', "    return 1 if stopped else 0"),
        ("a missing key is not checked", "    if not api_key:", "    if False:"),
        ("a repeated domain in the file is pushed twice", "        elif domain in seen:", "        elif False:"),
        ("a new company gets no signal_stage", '"signal_stage": CONFIG["stage_on_create"]}\n    _, data', "}\n    _, data"),
    ]),
    ("digest_n8n.py", "check_digest_n8n.py", [
        ("the key is not removed from output", 'return text.replace(secret, "[key removed]") if secret else text', "return text"),
        ("an error from HubSpot counts as success", '    if error or execution.get("status") != "success":', "    if False:"),
        ("the key is put on the command line",
         'command = ["docker", "run", "--rm", "--entrypoint", "sh", "-e", KEY_ENV]',
         'command = ["docker", "run", "--rm", "--entrypoint", "sh", "-e", KEY_ENV + "=" + os.environ.get(KEY_ENV, "")]'),
        ("the image is not pinned", '"image": "n8nio/n8n:2.48.4"', '"image": "n8nio/n8n:latest"'),
        ("--no-task does not reach the workflow", '        command += ["-e", "DIGEST_NO_TASK=1"]', "        pass"),
        ("the log line names the first company",
         '    return True, f"n8n digest: {found} new account(s), {flagged} flagged; 1 HubSpot task created."',
         '    return True, f"n8n digest: {found} new account(s), {flagged} flagged; 1 HubSpot task created. '
         "First: {search[0]['results'][0]['properties']['name']}\""),
        ("a failing container still exits 0", "    return 0 if ok and run.returncode == 0 else 1", "    return 0 if ok else 1"),
        ("a run that created no task passes", '    if not created or not created[0].get("id"):', "    if False:"),
        ("the workflow folder is mounted writable", ':/work:ro"', ':/work"'),
    ]),
    ("automation/n8n/weekly_digest.json", "check_digest_n8n.py", [
        ("company names are not HTML-escaped", ".replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')", ""),
        ("the read-only switch is ignored", "if ($env.DIGEST_NO_TASK === '1') {", "if (false) {"),
        ("an empty week makes an empty task", ": '<p>No new accounts in the last 7 days.</p>';", ": '';"),
        ("the second request updates companies instead of creating a task",
         "https://api.hubapi.com/crm/v3/objects/tasks", "https://api.hubapi.com/crm/v3/objects/companies"),
        ("a key is written into the workflow file",
         '"url": "https://api.hubapi.com/crm/v3/objects/companies/search",\n        "sendHeaders": true,\n'
         '        "headerParameters": {\n          "parameters": [\n            {\n              "name": "Authorization",\n'
         '              "value": "=Bearer {{ $env.HUBSPOT_SERVICE_KEY }}"',
         '"url": "https://api.hubapi.com/crm/v3/objects/companies/search",\n        "sendHeaders": true,\n'
         '        "headerParameters": {\n          "parameters": [\n            {\n              "name": "Authorization",\n'
         '              "value": "=Bearer abcdefghijklmnopqrstuvwxyz012345"'),
        ("the search drops the 7-day window",
         "{ propertyName: 'createdate', operator: 'GTE', value: String(Date.now() - 7 * 24 * 60 * 60 * 1000) }",
         "{ propertyName: 'createdate', operator: 'GTE', value: '0' }"),
        ("the workflow is saved as active", '"active": false', '"active": true'),
    ]),
    ("apollo_sequence.py", "check_apollo_sequence.py", [
        ("the sequence is created active", '        "active": False,', '        "active": True,'),
        ("emails are created already approved", '    "touch_status": "to_be_reviewed",', '    "touch_status": "approved",'),
        ("a definition may switch the sequence on", '    if definition.get("active"):', "    if False:"),
        ("a definition may set an email's status", '        if "status" in step:', "        if False:"),
        ("a failed create is sent again",
         'created = client.call("POST", SEQUENCES_URL, build_payload(definition, schedule_id), repeatable=False)',
         'created = client.call("POST", SEQUENCES_URL, build_payload(definition, schedule_id), repeatable=True)'),
        ("a second run creates a duplicate", "    if name in ids:", "    if False:"),
        ("an active sequence is not reported", '        if campaign.get("active"):', "        if False:"),
        ("the id is recorded even without an answer", '        if not campaign.get("id"):', "        if False:"),
        ("HTTP 429 is not retried", '            if response.status_code == 429 and attempt < CONFIG["max_retries"]:', "            if False:"),
        ("the first schedule is used when none is the default",
         "schedules[0] if len(schedules) == 1 else None)", "schedules[0] if schedules else None)"),
        ("--dry-run sends the request", "    if args.dry_run:", "    if False:"),
        ("a third endpoint is added",
         'SEQUENCES_URL = "https://api.apollo.io/api/v1/sequences"',
         'SEQUENCES_URL = "https://api.apollo.io/api/v1/sequences"\nADD_URL = "https://api.apollo.io/api/v1/emailer_campaigns/1/add_contact_ids"'),
        ("the first step may reply to nothing", '        if number == 1 and step["thread"] != "new_thread":', "        if False:"),
    ]),
    ("find_contacts.py", "check_find_contacts.py", [
        ("the key is sent in the query string", 'self.session.headers["X-API-KEY"] = api_key', 'self.session.params = {"api_key": api_key}'),
        ("social profiles are kept", 'KEPT_FIELDS = ("value", "type",', 'KEPT_FIELDS = ("linkedin", "value", "type",'),
        ("the git-ignore guard is removed", "        if not git_ignored(path):", "        if False:"),
        ("saved answers are not reused", "            if saved:", "            if False:"),
        ("the credit cap is ignored",
         '                if client.credits_used + CONFIG["expected_cost"] > args.max_credits:', "                if False:"),
        ("HTTP 429 does not stop the run", "            if response.status_code == 429:", "            if False:"),
        ("HTTP 403 is not retried",
         "            if response.status_code in (403,) or response.status_code >= 500:", "            if response.status_code >= 500:"),
        ("a network error is retried",
         '                raise HunterError(f"no answer from Hunter ({type(err).__name__}); not repeated") from err', "                continue"),
        ("the title no longer comes first when choosing",
         "ranked = sorted(emails, key=lambda email: (fit(email), CONFIG", "ranked = sorted(emails, key=lambda email: (0, CONFIG"),
        ("a team word alone counts as the lead",
         'if has(target["role"]) or (has(target["function"]) and has(CONFIG["lead_words"])):', 'if has(target["role"]) or has(target["function"]):'),
        ("a company of exactly 50 is treated as small", "if below is None or people < below)", "if below is None or people <= below)"),
        ("seniority is ignored when choosing", 'CONFIG["seniority_rank"].get(email.get("seniority"), 2),', "0,"),
        ("--dry-run sends requests", "    if args.dry_run:", "    if False:"),
        ("a stale saved answer is reused",
         'if saved.get("params") == params and age <= timedelta(days=CONFIG["cache_days"]):', 'if saved.get("params") == params:'),
        ("the cost in Hunter's header is ignored", "                    self.credits_used += float(used)", "                    self.credits_used += 0 * float(used)"),
        ("a saved answer for other filters is reused", 'if saved.get("params") == params and age', "if True and age"),
        ("the email status is not kept",
         '"verification_status": chosen.get("verification_status") or "", "sources": chosen.get("sources")}',
         '"verification_status": "", "sources": chosen.get("sources")}'),
        ("the people who were not picked are saved too", 'answer = {"emails": [kept] if kept else [],', 'answer = {"emails": full["emails"],'),
        ("the wide search runs even after a hit", '            if answer["returned"] or failed:', "            if failed:"),
        ("the wide search never runs",
         'return [("targeted", {**base, "department": target["department"]}), ("wide", base)]',
         'return [("targeted", {**base, "department": target["department"]})]'),
        ("a search that costs too much does not stop the run", '                if cost > CONFIG["expected_cost"]:', "                if False:"),
        ("a hundred people are asked for per search", '    "ask_for": 10,', '    "ask_for": 100,'),
        ("--outcomes-only still saves the chosen person",
         '                    answer = {**answer, "emails": [], "outcome": outcome_of(kept, fit)}',
         '                    answer = {**answer, "outcome": outcome_of(kept, fit)}'),
        ("--outcomes-only writes the personal columns", "OUTCOME_COLUMNS = [column for column in COLUMNS if column not in PERSONAL_COLUMNS]",
         "OUTCOME_COLUMNS = list(COLUMNS)"),
        ("a skipped company wipes the result it already had",
         '            if not (str(row["result"]).startswith("skipped") and row["company_name"] in on_file):', "            if True:"),
        ("a normal run reuses a saved answer that holds no person",
         '        if with_person and "outcome" in answer and answer.get("returned"):', "        if False:"),
        ("--outcomes-only forgets its earlier rows", "        if OUTCOMES_CSV.exists():\n            earlier", "        if False:\n            earlier"),
    ]),
    ("verify_postings.py", "check_verify_postings.py", [
        ("a posting missing from the board is reported open", '            elif row["_id"] in listed[0]:', "            elif True:"),
        ("a reposted role is reported closed",
         'row["status"] = "reposted" if row["_title"] and row["_title"] in listed[1] else "closed"', 'row["status"] = "closed"'),
        ("a posting without a known title is called reposted",
         'row["status"] = "reposted" if row["_title"] and row["_title"] in listed[1] else "closed"',
         'row["status"] = "reposted" if listed[1] else "closed"'),
        ("an answer in another shape is read as an empty board", '                raise TypeError("not a list")', "                return set()"),
        ("LinkedIn is treated as a board that can be asked", '"jobs.lever.co": "lever"},', '"jobs.lever.co": "lever", "linkedin.com": "lever"},'),
        ("the long description text is saved",
         '        return {key: value for key, value in posting.items() if key not in CONFIG["long_fields"]}', "        return posting"),
        ("saved answers are never reused", '            if saved["board"] == board and age <= timedelta(hours=max_age_hours):', "            if False:"),
        ("a stale saved answer is reused", '            if saved["board"] == board and age <= timedelta(hours=max_age_hours):', '            if saved["board"] == board:'),
        ("HTTP 429 is not retried", '        if response.status_code == 429 and attempt < CONFIG["max_retries"]:', "        if False:"),
        ("a server error is read as an answer", "        if response.status_code != 200:", "        if response.status_code == 404:"),
        ("Form D links are checked as postings",
         '            if host and not any(host == filing or host.endswith("." + filing) for filing in CONFIG["filing_hosts"]):', "            if host:"),
        ("ids are compared with their letter case", "ids.update(part.lower() for part in", "ids.update(part for part in"),
        ("every posting asks its board again", "            boards.setdefault(found[:2], []).append(row)", "            boards.setdefault(found + (len(boards),), []).append(row)"),
        ("--dry-run sends requests", "    if args.dry_run:", "    if False:"),
        ("no wait between boards", '            time.sleep(CONFIG["request_interval_s"])', "            pass"),
    ]),
    ("weekly_row.py", "check_weekly_row.py", [
        ("a partial credit sum is written",
         '        elif column not in ZERO_LINES or ZERO_LINES[column] not in text:\n            return ""',
         "        elif False:\n            return \"\""),
        ("a search that reused saved answers leaves the credit count empty",
         "        elif column not in ZERO_LINES or ZERO_LINES[column] not in text:", "        else:"),
        ("'no credits spent' in another step's log is read as zero for that step",
         "        elif column not in ZERO_LINES or ZERO_LINES[column] not in text:", '        elif "no credits spent" not in text:'),
        ("an account seen before counts as new again", '            counts["new_accounts"] = len(names - seen)', '            counts["new_accounts"] = len(names)'),
        ("the first run calls every account new",
         "            seen = set()\n        if remember:", '            seen = set()\n            counts["new_accounts"] = len(names)\n        if remember:'),
        ("--dry-run changes the private list", "    row.update(from_files(remember=not args.dry_run))", "    row.update(from_files(remember=True))"),
        ("a file with other columns is appended to anyway", "        if header != COLUMNS:", "        if False:"),
        ("unknown postings count as checked", 'CHECKED = STILL_OPEN + ("closed",)', 'CHECKED = STILL_OPEN + ("closed", "unknown")'),
        ("a closed posting counts as still open", 'STILL_OPEN = ("open", "reposted")', 'STILL_OPEN = ("open", "reposted", "closed")'),
        ("a flag made of spaces counts as a flag",
         'sum(bool(row.get("flags", "").strip()) for row in accounts)', 'sum(bool(row.get("flags", "")) for row in accounts)'),
        ("company names are written to the public row instead of a count",
         '                       "flagged": sum(', '                       "accounts": "; ".join(row.get("company_name", "") for row in accounts), "flagged": sum('),
        ("names in the private list are not cleaned", 'names = {clean_name(row.get("company_name", "")) for row in accounts} - {""}',
         'names = {row.get("company_name", "") for row in accounts} - {""}'),
        ("a second run overwrites the first row", '    with RUNS_CSV.open("a", newline="", encoding="utf-8") as handle:',
         '    with RUNS_CSV.open("w", newline="", encoding="utf-8") as handle:'),
    ]),
    ("judge_fit.py", "check_judge_fit.py", [
        ("the company's name is sent to the model", '    return mask(text, company["name"], company["domain"])', "    return text"),
        ("a name is hidden only in the case it is written in", "flags=re.I if any_case else 0)", "flags=0)"),
        ("a short form is hidden in any case, ordinary words included", "        forms[short] = False", "        forms[short] = True"),
        ("a name is hidden inside longer words",
         'rf"(?<![A-Za-z0-9]){re.escape(form)}(?![A-Za-z0-9])", CONFIG["mask"], text,', 're.escape(form), CONFIG["mask"], text,'),
        ("the instructions are not sent word for word", '"system": instructions,', '"system": instructions.strip(),'),
        ("the schema lets the model add fields", '"additionalProperties": False}', '"additionalProperties": True}'),
        ("a rule may be listed twice in the instructions",
         "    if len(rules) != len(pairs) or set(rules.values())", "    if set(rules.values())"),
        ("no fallback list is sent", "    if fallback:", "    if False:"),
        ("a rule from another verdict is accepted",
         '    if rules.get(answer["rule"]) != answer["verdict"]:', '    if answer["rule"] not in rules:'),
        ("a declined request is not recognised", '    if stop == "refusal":', '    if stop == "never":'),
        ("a cut-off answer is read anyway", '    if stop != "end_turn":', "    if False:"),
        ("every quote is marked as found",
         'row["evidence_found"] = "yes" if quote_found(answer["evidence"], request["messages"][0]["content"]) else "no"',
         'row["evidence_found"] = "yes"'),
        ("half a word counts as a quote",
         'bool(re.search(rf"(?<![a-z0-9]){re.escape(quote)}(?![a-z0-9])", plain(facts)))', "quote in plain(facts)"),
        ("output tokens are not priced",
         '        total += (tokens_in * price["input"] + tokens_out * price["output"]) / 1_000_000',
         '        total += tokens_in * price["input"] / 1_000_000'),
        ("a declined first attempt is left out of the cost", '    attempts = usage.get("iterations") or [usage]', "    attempts = [usage]"),
        ("an answer with no input tokens is priced at zero", "or tokens_in <= 0:", "or tokens_in < 0:"),
        ("the spend cap is ignored", "        elif needs_asking and sum(costs) >= args.max_usd:", "        elif needs_asking and False:"),
        ("--limit is ignored", "        elif needs_asking and sent >= to_ask:", "        elif needs_asking and False:"),
        ("a saved answer is never reused",
         '        if isinstance(saved, dict) and saved.get("request_key") == key and isinstance(saved.get("response"), dict):',
         "        if False:"),
        ("a saved answer is reused although the facts changed",
         '        if isinstance(saved, dict) and saved.get("request_key") == key and isinstance(saved.get("response"), dict):',
         '        if isinstance(saved, dict) and isinstance(saved.get("response"), dict):'),
        ("--refresh also discards the answers of companies that are not asked",
         "        plan.append((company, request, key, saved, may_ask and (saved is None or args.refresh)))",
         "        plan.append((company, request, key, None if args.refresh else saved, may_ask and (saved is None or args.refresh)))"),
        ("a refused key does not stop the run", "                if is_fatal(err):", "                if False:"),
        ("a rate limit stops the run", "status not in (408, 409, 429)", "status not in (408, 409)"),
        ("a stopped run still exits 0", "    return 1 if stopped else 0", "    return 0"),
        ("requests are sent although git does not ignore the output", "            if not git_ignored(path):", "            if False:"),
        ("the key is written into the saved answers",
         '"model": request["model"],\n                    "facts"', '"model": request["model"], "key": os.getenv(KEY_ENV),\n                    "facts"'),
        ("a company is named in the log",
         '            row["source"], row["judged_at"] = "asked now", now.strftime("%Y-%m-%dT%H:%M:%S")',
         '            row["source"], row["judged_at"] = "asked now", now.strftime("%Y-%m-%dT%H:%M:%S")\n'
         '            log.info("Asked about %s.", company["name"])'),
        ("a row that rejects one Form D match counts as rejecting the company",
         '        if decision == "reject" and not issuer:', '        if decision == "reject":'),
        ("an accept row overrules the reviewer's outright reject",
         '            labels.setdefault(company, "accept")', '            labels[company] = "accept"'),
        ("an unsure verdict counts as agreement", '        return "unsure"', '        return "yes"'),
    ]),
    ("why_now_check.py", "check_why_now.py", [
        ("day dates are not checked",
         "        if not any(d.month == MONTHS.index(month) + 1 and d.day == int(day) for d in dates):", "        if False:"),
        ("month and year dates are not checked",
         "        if not any(d.month == MONTHS.index(month) + 1 and d.year == int(year) for d in dates):", "        if False:"),
        ("numbers are not checked", '        if token.replace("$", "").replace(",", "").lower() not in allowed:', "        if False:"),
        ("the word limit is off by one", '    if len(line.split()) > CONFIG["max_words"]:', '    if len(line.split()) >= CONFIG["max_words"]:'),
        ("banned words are matched only in lower case", "        if re.search(pattern, line, re.I):", "        if re.search(pattern, line):"),
        ("a problem does not change the exit code", "    return 1 if problems else 0", "    return 0"),
        ("a line for a company that is not an account passes",
         '        problems.append(f"{name}: has a line but is not in accounts.csv")', "        pass"),
        ("any other company's dates are accepted", '    values = set(company_jobs["date_posted"]) | {account["filing_date"], first_sale(account)}',
         '    values = {"2026-11-10", "2026-11-12"} | set(company_jobs["date_posted"]) | {account["filing_date"], first_sale(account)}'),
        ("the first-sale date is not allowed in a line", '{account["filing_date"], first_sale(account)}', '{account["filing_date"]}'),
        ("a caller's banned words are ignored", '    for pattern, why in CONFIG["banned"] if banned is None else banned:', '    for pattern, why in CONFIG["banned"]:'),
    ]),
    (".github/workflows/weekly.yml", "check_weekly_row.py", [
        ("the review decisions are read from a repository variable, whose value public logs print",
         "          REVIEW_DECISIONS_CSV: ${{ secrets.REVIEW_DECISIONS_CSV }}", "          REVIEW_DECISIONS_CSV: ${{ vars.REVIEW_DECISIONS_CSV }}"),
        ("a hand-edited file is put in the environment of every step",
         "      HIRING_CREDITS: ${{ inputs.hiring_credits || '10' }}\n",
         "      HIRING_CREDITS: ${{ inputs.hiring_credits || '10' }}\n      DOMAIN_OVERRIDES_CSV: ${{ secrets.DOMAIN_OVERRIDES_CSV }}\n"),
        ("a step's log is no longer written where the weekly row reads it", "tee run_logs/hubspot.log", "tee run_logs/crm.log"),
    ]),
    ("bay_area.py", "check_fetch_funding.py", [
        ("a ZIP prefix outside the Bay Area is accepted", '    "940", "941",', '    "900", "940", "941",'),
        ("a missing ZIP code counts as outside", '        return "missing"', '        return "outside"'),
    ]),
    ("bay_area.py", "check_build_accounts.py", [
        ("a same-named city in another state passes as a Bay Area city",
         '    return state.strip().upper() == "CA" and is_bay_area_city_name(city)', "    return is_bay_area_city_name(city)"),
    ]),
]


def copy_project(work: Path) -> None:
    """Copy the scripts, checks and other files the checks read into `work`. The .env file,
    which holds the real keys, is never copied."""
    for path in PROJECT.iterdir():
        if path.is_file() and path.name != ".env":
            shutil.copy(path, work)
    for folder in COPIED:
        if (PROJECT / folder).is_dir():
            shutil.copytree(PROJECT / folder, work / folder, ignore=shutil.ignore_patterns("__pycache__"))


def main(argv: list[str] | None = None) -> int:
    """Plant every bug (or those in the named files), one at a time, and report."""
    wanted = [arg for arg in (sys.argv[1:] if argv is None else argv) if not arg.startswith("--")]
    listing = "--list" in (sys.argv[1:] if argv is None else argv)
    files = list(dict.fromkeys(file for file, _, _ in PLANTED))
    unknown = [name for name in wanted if name not in files]
    if unknown:
        print(f"No bugs are planted in: {', '.join(unknown)}. Known files: {', '.join(files)}")
        return 1
    targets = [entry for entry in PLANTED if not wanted or entry[0] in wanted]
    if listing:
        for file, check, bugs in targets:
            print(f"{file} ({len(bugs)} bugs, noticed by tests/{check})")
            for what, _, _ in bugs:
                print(f"  - {what}")
        print(f"\n{sum(len(bugs) for _, _, bugs in targets)} planted bugs in {len({file for file, _, _ in targets})} files")
        return 0

    work = Path(tempfile.mkdtemp(prefix="planted_bugs_"))
    copy_project(work)
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    caught = missed = stale = 0

    def run_check(check: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, "-B", str(work / "tests" / check)], cwd=work, env=env,
                              capture_output=True, text=True)

    # A check file that already fails would "catch" every bug, so each one must pass first
    for check in sorted({check for _, check, _ in targets}):
        before = run_check(check)
        if before.returncode != 0:
            print(f"BROKEN  tests/{check} fails on the unchanged copy, so it cannot show anything:")
            print("        " + (before.stderr.strip().splitlines() or before.stdout.strip().splitlines() or ["(no output)"])[-1][:200])
            shutil.rmtree(work, ignore_errors=True)
            return 1
    for file, check, bugs in targets:
        clean = (PROJECT / file).read_text(encoding="utf-8")
        for what, old, new in bugs:
            if clean.count(old) != 1:
                stale += 1
                print(f"STALE   {file}: {what} (its text is in the script {clean.count(old)} times, not once)")
                continue
            (work / file).write_text(clean.replace(old, new), encoding="utf-8")
            run = run_check(check)
            (work / file).write_text(clean, encoding="utf-8")
            failing = sum(line.startswith("FAIL") for line in run.stdout.splitlines())
            if run.returncode != 0:
                caught += 1
                print(f"caught  {file}: {what} ({failing} failing check(s))" if failing else
                      f"caught  {file}: {what} (the check file stopped with an error)")
            else:
                missed += 1
                print(f"MISSED  {file}: {what}")
    shutil.rmtree(work, ignore_errors=True)
    total = caught + missed + stale
    print(f"\n{caught} of {total} planted bugs caught" + (f"; {missed} missed" if missed else "")
          + (f"; {stale} no longer match their script" if stale else ""))
    return 0 if caught == total else 1


if __name__ == "__main__":
    sys.exit(main())
