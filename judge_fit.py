"""Ask a language model whether each company with a relevant posting is a target account
(optional review-time step; the weekly job never runs it).

Step 3 decides the account list with rules. Rules cannot read a description: they did not
recognise a competitor of the client or a clinic, which the reviewer had to block by hand.
This script asks the Claude API for a second opinion on every company in data/jobs.csv and
compares the answers with the reviewer's decisions in data/manual/review_decisions.csv.
The answers change nothing in data/accounts.csv.

What is sent, one request per company: the instructions in prompts/fit_rules.md, word for
word, and the company's facts as TheirStack returned them: two descriptions, industry,
headcount, 30-day posting count and the titles of its relevant roles. The company's name
and website are replaced by "[the company]" first, so the answer rests on the description
and not on what the name brings to mind. Nothing from data/contacts/ is read or sent.

What comes back, as JSON that the API holds to a schema: a verdict (target, not_target or
unsure), the rule that decided it, a quote from the facts and one sentence for the
reviewer. The script checks what code can check: the rule must belong to the verdict, and
the quote must be found in the facts that were sent. An answer that fails the first check
is not used; a quote that is not found is marked in the output for the reviewer.

Cost: every answer is priced from the token counts the API returns, at the list prices in
CONFIG. --max-usd stops the run once that much is spent; it is checked before each request,
so a run can pass it by one answer. Every answer is saved in data/raw/ with its time, and a
saved answer for the same model, instructions and facts is reused, so a second run sends
nothing. If the model declines a request, the API may answer with the fallback model named
in CONFIG; the output says which model answered. The SDK retries HTTP 429, server errors
and lost connections with a growing wait; a company that still gets no answer is marked
and the run goes on. Any other HTTP 4xx means the key, the balance or the request itself
is wrong, so the run stops.

Writes data/fit_check.csv, one row per company, and logs counts only.

Usage:
    python judge_fit.py --dry-run     # count what would be asked; send nothing
    python judge_fit.py --limit 5     # send at most 5 requests
    python judge_fit.py               # ask about the rest; saved answers are reused
    python judge_fit.py --limit 0     # send nothing; rebuild the table from the saved answers
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import statistics
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import anthropic
import pandas as pd
from dotenv import load_dotenv

from build_accounts import distinct_titles, read_table, review_decisions
from names import clean_name, short_name, strip_legal_suffix

ROOT = Path(__file__).resolve().parent
JOBS_CSV = ROOT / "data" / "jobs.csv"
ACCOUNTS_CSV = ROOT / "data" / "accounts.csv"
DECISIONS_CSV = ROOT / "data" / "manual" / "review_decisions.csv"
RULES_MD = ROOT / "prompts" / "fit_rules.md"
FIT_CSV = ROOT / "data" / "fit_check.csv"
RAW_DIR = ROOT / "data" / "raw"
KEY_ENV = "ANTHROPIC_API_KEY"

CONFIG: dict[str, Any] = {
    "default_model": "claude-fable-5-2",
    # List prices in USD per million tokens, read from Anthropic's model pages on 2026-11-18.
    # "fallback" is the model the API may answer with when this one declines a request.
    "models": {
        "claude-fable-5-2": {"input": 8.00, "output": 40.00, "fallback": "claude-opus-5"},
        "claude-opus-5-5": {"input": 4.00, "output": 20.00, "fallback": "claude-opus-5"},
        "claude-opus-5": {"input": 5.00, "output": 25.00},
        "claude-sonnet-5-5": {"input": 2.00, "output": 10.00},
        "claude-haiku-5-5": {"input": 0.10, "output": 0.50},
    },
    "fallback_beta": "server-side-fallback-2026-07-01",   # the beta header a fallback list needs
    "effort": "high",
    "max_tokens": 8000,          # thinking and answer together; an answer cut off here is not used
    "default_max_usd": 3.0,
    "max_retries": 4,            # by the SDK, for HTTP 429, server errors and lost connections
    "timeout_s": 600,            # the SDK's own default; a shorter wait would retry, and pay, twice
    "verdicts": ("target", "not_target", "unsure"),
    "expected": {"accept": "target", "reject": "not_target"},   # the verdict that agrees with the reviewer
    "mask": "[the company]",
    "min_label_letters": 4,      # a shorter website label ("sim") is too likely to be an ordinary word
}
FIELDS = ("verdict", "rule", "evidence", "explanation")
COLUMNS = ["company_name", "listed", "reviewer_decision", "verdict", "agrees", "rule", "evidence", "evidence_found",
           "explanation", "status", "answered_by", "source", "input_tokens", "output_tokens", "cost_usd", "judged_at"]
JOB_COLUMNS = ["company_name", "company_domain", "company_description", "company_long_description",
               "company_industry", "employee_count", "theirstack_jobs_last_30_days", "job_title"]
QUOTE_MARKS = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"', "–": "-", "—": "-"})

log = logging.getLogger("judge_fit")


class AnswerError(ValueError):
    """One company's answer cannot be used; the run goes on."""


class FatalError(RuntimeError):
    """The run cannot go on: the key, the balance or the request itself is wrong."""


def shown(path: Path) -> Path:
    """A path relative to the project, for log lines."""
    return path.relative_to(ROOT) if path.is_relative_to(ROOT) else path


def git_ignored(path: Path) -> bool:
    """True only if git says it ignores this path. False if it does not, or if git cannot say."""
    try:
        return subprocess.run(["git", "-C", str(ROOT), "check-ignore", "-q", str(path)],
                              capture_output=True).returncode == 0
    except OSError:
        return False


def read_rules(path: Path | None = None) -> tuple[str, dict[str, str]]:
    """The instructions sent to the model, and the verdict that each rule in their table
    belongs to. The table is the only place the rules are defined."""
    path = path or RULES_MD
    text = path.read_text(encoding="utf-8")
    pairs = re.findall(r"^\| `(\w+)` \| `(\w+)` \|", text, re.M)
    rules = {rule: verdict for verdict, rule in pairs}
    if len(rules) != len(pairs) or set(rules.values()) != set(CONFIG["verdicts"]):
        raise ValueError(f"{path.name} needs a table of verdicts and rules: each rule once, and at least one rule "
                         f"for each of {', '.join(CONFIG['verdicts'])}")
    return text, rules


def answer_schema(rules: dict[str, str]) -> dict[str, Any]:
    """The JSON schema the API holds each answer to."""
    return {"type": "object",
            "properties": {"verdict": {"type": "string", "enum": list(CONFIG["verdicts"])},
                           "rule": {"type": "string", "enum": sorted(rules)},
                           "evidence": {"type": "string"},
                           "explanation": {"type": "string"}},
            "required": list(FIELDS),
            "additionalProperties": False}


def first_text(values: pd.Series) -> str:
    """The first cell that is not empty, on one line."""
    return next((" ".join(str(value).split()) for value in values if str(value).strip()), "")


def largest(values: pd.Series) -> str:
    """The largest number in the cells as a whole number, or "" if there is none."""
    number = pd.to_numeric(values, errors="coerce").max()
    return "" if pd.isna(number) else str(int(number))


def company_facts(jobs: pd.DataFrame) -> list[dict[str, Any]]:
    """What data/jobs.csv says about each company, in name order."""
    missing = [column for column in JOB_COLUMNS if column not in jobs.columns]
    if missing:
        raise ValueError(f"{JOBS_CSV.name} lacks the columns {', '.join(missing)}")
    companies = []
    for key, rows in jobs.groupby(jobs["company_name"].map(clean_name), sort=True):
        if key:
            companies.append({"key": key, "name": first_text(rows["company_name"]),
                              "domain": first_text(rows["company_domain"]).lower(),
                              "description": first_text(rows["company_description"]),
                              "long_description": first_text(rows["company_long_description"]),
                              "industry": first_text(rows["company_industry"]),
                              "headcount": largest(rows["employee_count"]),
                              "recent_jobs": largest(rows["theirstack_jobs_last_30_days"]),
                              "titles": distinct_titles(rows["job_title"])})
    return companies


def hidden_forms(name: str, domain: str) -> list[tuple[str, bool]]:
    """The spellings of a company to hide, longest first, each with whether case is ignored.
    A short form ("Example" from "Example AI") is hidden only as written, because in lower
    case it is often an ordinary word."""
    full = strip_legal_suffix(name)
    forms: dict[str, bool] = {form: True for form in (name.strip(), full, re.sub(r"\s+", "", full)) if form}
    short = short_name(full)
    if short and short.lower() not in {form.lower() for form in forms}:
        forms[short] = False
    if domain:
        forms.setdefault(domain, True)
        label = domain.rsplit(".", 1)[0]
        if len(label) >= CONFIG["min_label_letters"] and label.lower() not in {form.lower() for form in forms}:
            forms[label] = True
    return sorted(forms.items(), key=lambda item: -len(item[0]))


def mask(text: str, name: str, domain: str) -> str:
    """`text` with the company's name and website replaced by "[the company]"."""
    for form, any_case in hidden_forms(name, domain):
        text = re.sub(rf"(?<![A-Za-z0-9]){re.escape(form)}(?![A-Za-z0-9])", CONFIG["mask"], text,
                      flags=re.I if any_case else 0)
    return text


def facts_text(company: dict[str, Any]) -> str:
    """The facts of one company as the model reads them, with its name and website hidden."""
    lines = [("Short description", company["description"]), ("Long description", company["long_description"]),
             ("Industry", company["industry"]), ("Employees", company["headcount"]),
             ("Jobs posted in the last 30 days", company["recent_jobs"]),
             ("Open roles that matched the search", "; ".join(company["titles"]))]
    text = "\n".join(f"{label}: {value or 'not given'}" for label, value in lines)
    return mask(text, company["name"], company["domain"])


def request_for(model: str, instructions: str, rules: dict[str, str], facts: str) -> dict[str, Any]:
    """Everything that shapes the answer for one company."""
    return {"model": model, "max_tokens": CONFIG["max_tokens"], "system": instructions,
            "messages": [{"role": "user", "content": facts}],
            "output_config": {"effort": CONFIG["effort"],
                              "format": {"type": "json_schema", "schema": answer_schema(rules)}}}


def request_key(request: dict[str, Any]) -> str:
    """A fingerprint of a request. A saved answer is reused only for the same fingerprint."""
    fallback = CONFIG["models"][request["model"]].get("fallback", "")
    return hashlib.sha256(json.dumps([request, fallback], sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def make_client(key: str) -> Any:
    """The official SDK client. It waits and retries HTTP 429, server errors and lost connections itself."""
    return anthropic.Anthropic(api_key=key, max_retries=CONFIG["max_retries"], timeout=CONFIG["timeout_s"])


def ask(client: Any, request: dict[str, Any]) -> dict[str, Any]:
    """Send one request and return the API's answer as plain data. A model with a fallback
    is asked through the beta endpoint, which takes the fallback list."""
    fallback = CONFIG["models"][request["model"]].get("fallback")
    if fallback:
        message = client.beta.messages.create(**request, fallbacks=[{"model": fallback}],
                                              betas=[CONFIG["fallback_beta"]])
    else:
        message = client.messages.create(**request)
    return message.to_dict(mode="json")


def is_fatal(error: Exception) -> bool:
    """True for an HTTP 4xx answer other than 408, 409 and 429: the key, the balance or the
    request itself is wrong, and every other request would fail the same way."""
    status = getattr(error, "status_code", None)
    return isinstance(status, int) and 400 <= status < 500 and status not in (408, 409, 429)


def by_fallback(message: dict[str, Any]) -> bool:
    """True if a fallback model produced this answer: the API marks that attempt in its usage record."""
    attempts = (message.get("usage") or {}).get("iterations") or []
    return any(isinstance(attempt, dict) and attempt.get("type") == "fallback_message" for attempt in attempts)


def read_answer(message: dict[str, Any], rules: dict[str, str]) -> dict[str, str]:
    """The four fields of a usable answer. Raises AnswerError when the model declined, was
    cut off, or named a rule that does not belong to its verdict."""
    stop = message.get("stop_reason")
    if stop == "refusal":
        raise AnswerError(f"the model declined ({(message.get('stop_details') or {}).get('category') or 'no category'})")
    if stop != "end_turn":
        raise AnswerError(f"the answer was cut off ({stop})")
    text = next((block.get("text", "") for block in message.get("content") or []
                 if isinstance(block, dict) and block.get("type") == "text"), "")
    try:
        answer = json.loads(text)
    except ValueError as err:
        raise AnswerError("the answer is not JSON") from err
    if not isinstance(answer, dict) or set(answer) != set(FIELDS) or not all(isinstance(answer[f], str) for f in FIELDS):
        raise AnswerError("the answer does not hold the four text fields")
    if rules.get(answer["rule"]) != answer["verdict"]:
        raise AnswerError(f"rule {answer['rule']!r} does not belong to verdict {answer['verdict']!r}")
    return {field: " ".join(answer[field].split()) for field in FIELDS}


def plain(text: str) -> str:
    """Text for comparing a quote with the facts: lower case, straight quotes, single spaces."""
    return " ".join(text.lower().translate(QUOTE_MARKS).split())


def quote_found(evidence: str, facts: str) -> bool:
    """True if the model's quote is in the facts it was given, word for word and as whole words."""
    quote = plain(evidence).strip(" .,;:\"'")
    return bool(quote) and bool(re.search(rf"(?<![a-z0-9]){re.escape(quote)}(?![a-z0-9])", plain(facts)))


def cost_usd(message: dict[str, Any]) -> float:
    """The list-price cost of one answer, from the token counts the API returned. When a
    fallback model answered, every attempt is counted at its own model's price, the declined
    one included, so the figure is an upper bound. Raises FatalError when a count or a price
    is missing, because the spend cap cannot be kept without them."""
    usage = message.get("usage") or {}
    attempts = usage.get("iterations") or [usage]
    total = 0.0
    for attempt in attempts:
        model = attempt.get("model") or message.get("model")
        price = CONFIG["models"].get(model)
        tokens_in, tokens_out = attempt.get("input_tokens"), attempt.get("output_tokens")
        if price is None or not isinstance(tokens_in, int) or not isinstance(tokens_out, int) or tokens_in <= 0:
            raise FatalError(f"an answer came back without usable token counts or from a model with no price here "
                             f"({model}), so its cost is unknown")
        # No request asks for prompt caching, so cached tokens are not expected; if any appear
        # they are counted at the dearest rate (a one-hour cache write costs twice the input price)
        tokens_in += 2 * (attempt.get("cache_creation_input_tokens") or 0) + (attempt.get("cache_read_input_tokens") or 0)
        total += (tokens_in * price["input"] + tokens_out * price["output"]) / 1_000_000
    return total


def slug(name: str) -> str:
    """A company name as part of a file name."""
    return re.sub(r"[^a-z0-9]+", "-", clean_name(name)).strip("-") or "company"


def saved_answer(name: str, key: str) -> dict[str, Any] | None:
    """The newest saved API answer for this company and request fingerprint, or None."""
    for path in sorted(RAW_DIR.glob(f"fit_{slug(name)}_*.json"), reverse=True):
        try:
            saved = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(saved, dict) and saved.get("request_key") == key and isinstance(saved.get("response"), dict):
            return saved
    return None


def save_answer(name: str, key: str, request: dict[str, Any], message: dict[str, Any], now: datetime) -> None:
    """Save one raw API answer in data/raw/ with its time and the facts that were sent."""
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    (RAW_DIR / f"fit_{slug(name)}_{now.strftime('%Y-%m-%dT%H%M%S%fZ')}.json").write_text(
        json.dumps({"saved_at": now.isoformat(), "company_name": name, "request_key": key, "model": request["model"],
                    "facts": request["messages"][0]["content"], "response": message}, indent=1, ensure_ascii=False),
        encoding="utf-8")


def reviewer_labels(path: Path | None = None) -> dict[str, str]:
    """accept or reject by cleaned company name, from the reviewer's decisions file. A row
    that rejects one Form D match says nothing about the company itself and is left out; a
    company the reviewer rejected outright stays rejected whatever its other rows say."""
    labels: dict[str, str] = {}
    for (company, issuer), (decision, _) in review_decisions(path or DECISIONS_CSV).items():
        if decision == "reject" and not issuer:
            labels[company] = "reject"
        elif decision == "accept":
            labels.setdefault(company, "accept")
    return labels


def agreement(decision: str, verdict: str) -> str:
    """yes, no or unsure: whether a verdict agrees with the reviewer's decision. "" without both."""
    if not decision or not verdict:
        return ""
    if verdict == "unsure":
        return "unsure"
    return "yes" if CONFIG["expected"][decision] == verdict else "no"


def summarize(rows: list[dict[str, Any]]) -> dict[str, int]:
    """Counts for the log: verdicts, quotes found, and the verdicts against the reviewer's decisions."""
    judged = [row for row in rows if row["verdict"]]
    decided = [row for row in rows if row["reviewer_decision"]]
    counts = {"companies": len(rows), "judged": len(judged),
              "not_asked": sum(row["status"].startswith("not asked") for row in rows),
              "not_judged": sum(row["status"].startswith("not judged") for row in rows),
              "quotes_found": sum(row["evidence_found"] == "yes" for row in judged),
              "by_fallback": sum(row["status"] == "ok: answered by the fallback model" for row in judged),
              "decisions": len(decided), "decisions_judged": sum(bool(row["verdict"]) for row in decided),
              "doubted_listed": sum(row["listed"] == "yes" and row["verdict"] in ("not_target", "unsure") for row in rows)}
    for verdict in CONFIG["verdicts"]:
        counts[verdict] = sum(row["verdict"] == verdict for row in judged)
        for decision in CONFIG["expected"]:
            counts[f"{decision}_{verdict}"] = sum(row["reviewer_decision"] == decision and row["verdict"] == verdict
                                                  for row in decided)
    for answer in ("yes", "no", "unsure"):
        counts[f"agrees_{answer}"] = sum(row["agrees"] == answer for row in decided)
    return counts


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(description="Ask a model whether each company is a target account (review-time).")
    parser.add_argument("--dry-run", action="store_true", help="count what would be asked; send nothing")
    parser.add_argument("--limit", type=int, default=None, help="send at most this many requests in this run")
    parser.add_argument("--companies", default="", help="comma-separated company names: ask only about these")
    parser.add_argument("--model", default=CONFIG["default_model"], choices=sorted(CONFIG["models"]))
    parser.add_argument("--max-usd", type=float, default=CONFIG["default_max_usd"],
                        help="stop asking once this much is spent in this run, at list price (default %(default)s)")
    parser.add_argument("--refresh", action="store_true", help="ask again even where an answer is saved")
    args = parser.parse_args(argv)
    if (args.limit is not None and args.limit < 0) or args.max_usd <= 0:
        parser.error("--limit cannot be negative and --max-usd must be above 0")
    return args


def main(argv: list[str] | None = None) -> int:
    """Ask about the companies in data/jobs.csv and write data/fit_check.csv."""
    args = parse_args(argv)
    # Only this script logs at INFO; the SDK would otherwise add a line for every request it sends
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    log.setLevel(logging.INFO)
    if not JOBS_CSV.exists():
        log.error("%s is missing; run fetch_jobs.py first.", shown(JOBS_CSV))
        return 1
    try:
        instructions, rules = read_rules()
        companies = company_facts(read_table(JOBS_CSV))
        labels = reviewer_labels()
    except (OSError, ValueError) as err:
        log.error("%s", err)
        return 1
    wanted = {clean_name(name) for name in args.companies.split(",") if name.strip()}
    unknown = wanted - {company["key"] for company in companies}
    if unknown:
        log.error("Not in %s: %s", shown(JOBS_CSV), ", ".join(sorted(unknown)))
        return 1
    listed = ({clean_name(name) for name in read_table(ACCOUNTS_CSV)["company_name"]} if ACCOUNTS_CSV.exists() else set())

    # For each company: its request, the newest saved answer for that exact request, and
    # whether this run should ask (no saved answer yet, or --refresh; only among --companies)
    plan = []
    for company in companies:
        request = request_for(args.model, instructions, rules, facts_text(company))
        key = request_key(request)
        saved = saved_answer(company["name"], key)
        may_ask = not wanted or company["key"] in wanted
        plan.append((company, request, key, saved, may_ask and (saved is None or args.refresh)))
    needed = sum(needs_asking for *_, needs_asking in plan)
    to_ask = needed if args.limit is None else min(args.limit, needed)
    if args.dry_run:
        letters = [len(instructions) + len(request["messages"][0]["content"])
                   for _, request, _, _, needs_asking in plan if needs_asking][:to_ask]
        log.info("Dry run: nothing sent. %d companies in %s; a saved answer exists for %d; %d request(s) would be "
                 "sent to %s, about %d characters of instructions and facts in all. The cap is $%.2f.",
                 len(plan), shown(JOBS_CSV), sum(saved is not None for _, _, _, saved, _ in plan), to_ask, args.model,
                 sum(letters), args.max_usd)
        return 0

    client = None
    if to_ask:
        for path in (FIT_CSV, RAW_DIR / "fit_check.json"):
            if not git_ignored(path):
                log.error("git does not ignore %s, so the answers could be committed. Nothing was sent.", shown(path))
                return 1
        load_dotenv(ROOT / ".env")
        key_value = os.getenv(KEY_ENV, "").strip()
        if not key_value:
            log.error("%s is not set in .env; nothing was sent.", KEY_ENV)
            return 1
        client = make_client(key_value)

    rows, costs, sizes, sent, stopped, capped = [], [], [], 0, "", False
    for company, request, key, saved, needs_asking in plan:
        row = {**dict.fromkeys(COLUMNS, ""), "company_name": company["name"],
               "listed": "yes" if company["key"] in listed else "no",
               "reviewer_decision": labels.get(company["key"], "")}
        rows.append(row)
        message, why_not = None, "it is not among --companies"
        if needs_asking and stopped:
            why_not = stopped
        elif needs_asking and sent >= to_ask:
            why_not = "--limit was reached"
        elif needs_asking and sum(costs) >= args.max_usd:
            why_not, capped = "the spend cap was reached", True
        elif needs_asking:
            now = datetime.now(timezone.utc)
            sent += 1
            try:
                message = ask(client, request)
            except anthropic.APIError as err:
                if is_fatal(err):
                    stopped = "the run stopped"
                    log.error("The API refused a request (HTTP %s, %s: %s). Stopped; nothing more is sent.",
                              getattr(err, "status_code", "?"), type(err).__name__, str(err)[:200])
                row["status"] = f"not judged: {type(err).__name__}"
                continue
            save_answer(company["name"], key, request, message, now)
            row["source"], row["judged_at"] = "asked now", now.strftime("%Y-%m-%dT%H:%M:%S")
        if message is None:
            # Not asked in this run: fall back on the saved answer, if there is one
            if saved is None:
                row["status"] = f"not asked: {why_not}"
                continue
            message, row["source"], row["judged_at"] = saved["response"], "saved answer", str(saved.get("saved_at", ""))[:19]
        try:
            row["cost_usd"] = round(cost_usd(message), 6)
        except FatalError as err:
            log.error("Stopped: %s.", err)
            stopped, row["status"] = "the run stopped", "not judged: the answer has no usable token counts"
            continue
        usage = message.get("usage") or {}
        row["input_tokens"], row["output_tokens"] = usage.get("input_tokens", ""), usage.get("output_tokens", "")
        row["answered_by"] = message.get("model", "")
        if row["source"] == "asked now":
            costs.append(row["cost_usd"])
            sizes.append((usage.get("input_tokens", 0), usage.get("output_tokens", 0)))
        try:
            answer = read_answer(message, rules)
        except AnswerError as err:
            row["status"] = f"not judged: {err}"
            continue
        row.update(answer)
        row["evidence_found"] = "yes" if quote_found(answer["evidence"], request["messages"][0]["content"]) else "no"
        row["agrees"] = agreement(row["reviewer_decision"], answer["verdict"])
        row["status"] = "ok: answered by the fallback model" if by_fallback(message) else "ok"
    if capped:
        log.warning("The spend cap of $%.2f was reached after %d request(s); the companies left were not asked.",
                    args.max_usd, sent)

    FIT_CSV.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows, columns=COLUMNS).to_csv(FIT_CSV, index=False)
    count = summarize(rows)
    # Counts only, as in every other step: no company is named in a log
    log.info("Fit check with %s: %d companies; asked now %d, read from saved answers %d, not asked %d, asked but not "
             "judged %d.", args.model, count["companies"], sent, sum(row["source"] == "saved answer" for row in rows),
             count["not_asked"], count["not_judged"])
    log.info("Verdicts: %d target, %d not_target, %d unsure. Quotes found in the facts: %d of %d. Answered by the "
             "fallback model: %d. Listed accounts the model doubts: %d (see %s).", count["target"], count["not_target"],
             count["unsure"], count["quotes_found"], count["judged"], count["by_fallback"], count["doubted_listed"],
             shown(FIT_CSV))
    log.info("Against the reviewer's %d decision(s), %d judged: agree %d, disagree %d, unsure %d. Kept by the reviewer: "
             "%d target, %d not_target, %d unsure. Rejected by the reviewer: %d target, %d not_target, %d unsure.",
             count["decisions"], count["decisions_judged"], count["agrees_yes"], count["agrees_no"],
             count["agrees_unsure"], count["accept_target"], count["accept_not_target"], count["accept_unsure"],
             count["reject_target"], count["reject_not_target"], count["reject_unsure"])
    if costs:
        log.info("Spent in this run: $%.4f at list price for %d answer(s); median %d tokens in and %d out, dearest "
                 "answer $%.4f.", sum(costs), len(costs), statistics.median(size[0] for size in sizes),
                 statistics.median(size[1] for size in sizes), max(costs))
        if count["not_asked"]:
            log.info("At this rate the %d companies not asked would cost about $%.2f.", count["not_asked"],
                     count["not_asked"] * sum(costs) / len(costs))
    else:
        log.info("Spent in this run: $0, no request was answered.")
    return 1 if stopped else 0


if __name__ == "__main__":
    sys.exit(main())
