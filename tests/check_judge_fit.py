"""Offline checks for judge_fit.py. Every company, description, decision and model answer
here is SIMULATED, and the SDK client is replaced by a stand-in kept in memory, so no
request is possible and no key is needed. The project's data/ is never read or written.

Run: python tests/check_judge_fit.py
"""
import hashlib
import json
import logging
import os
import re
import shutil
import sys
import tempfile
import types
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
import judge_fit as jf  # noqa: E402
import anthropic  # noqa: E402
import httpx2  # noqa: E402
import pandas as pd  # noqa: E402

tmp = Path(tempfile.mkdtemp(prefix="sim_fit_"))
jf.JOBS_CSV, jf.ACCOUNTS_CSV, jf.DECISIONS_CSV = tmp / "jobs.csv", tmp / "accounts.csv", tmp / "review_decisions.csv"
jf.FIT_CSV, jf.RAW_DIR = tmp / "fit_check.csv", tmp / "raw"
REAL_MAKE_CLIENT = jf.make_client
IGNORED = {"value": True}
jf.git_ignored = lambda path: IGNORED["value"]
jf.load_dotenv = lambda path: None
SIM_KEY = "sk-ant-SIMULATED-not-a-real-key"
os.environ[jf.KEY_ENV] = SIM_KEY
results = []


def check(name, cond):
    results.append(bool(cond))
    print("PASS" if cond else "FAIL", "-", name)


class Collect(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines = []

    def emit(self, record):
        self.lines.append(f"{record.levelname} {record.getMessage()}")


H = Collect()
jf.log.addHandler(H)
jf.log.setLevel(logging.INFO)


def logged(text):
    return any(text in line for line in H.lines)


def raises(error, call):
    try:
        call()
    except error as err:
        return str(err) or True
    return False


# --- SIMULATED model answers ---------------------------------------------------------------
def message(verdict="target", rule="builds_technology", evidence="SIMULATED", explanation="SIMULATED explanation.",
            model="claude-fable-5-2", tokens=(1000, 500), stop="end_turn", text=None, usage=None, before=()):
    """One SIMULATED API answer in the documented shape."""
    body = json.dumps({"verdict": verdict, "rule": rule, "evidence": evidence, "explanation": explanation})
    return {"id": "msg_SIMULATED", "type": "message", "role": "assistant", "model": model,
            "content": [{"type": "thinking", "thinking": "", "signature": "SIMULATED"}, *before,
                        {"type": "text", "text": body if text is None else text}],
            "stop_reason": stop, "stop_details": None,
            "usage": {"input_tokens": tokens[0], "output_tokens": tokens[1]} if usage is None else usage}


REFUSAL = {"id": "msg_SIMULATED", "type": "message", "role": "assistant", "model": "claude-fable-5-2", "content": [],
           "stop_reason": "refusal", "stop_details": {"type": "refusal", "category": "bio", "explanation": "SIMULATED"},
           "usage": {"input_tokens": 900, "output_tokens": 0}}
FALLBACK_USAGE = {"input_tokens": 412, "output_tokens": 264, "iterations": [
    {"type": "message", "model": "claude-fable-5-2", "input_tokens": 535, "output_tokens": 0},
    {"type": "fallback_message", "model": "claude-opus-5", "input_tokens": 412, "output_tokens": 264}]}
FALLBACK_BLOCK = {"type": "fallback", "from": {"model": "claude-fable-5-2"}, "to": {"model": "claude-opus-5"},
                  "trigger": {"type": "refusal", "category": "cyber"}}
REQUEST = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")


def api_error(kind, status):
    return kind("SIMULATED error", response=httpx2.Response(status, request=REQUEST), body=None)


# What the stand-in answers, chosen by a word that only one SIMULATED description holds
ANSWERS = {
    "warehouse robots": message(evidence="builds warehouse robots"),
    "headhunter": message("not_target", "talent_competitor", "an AI headhunter that places machine learning engineers"),
    "consulting projects": message("target", "services_company", "they run projects for clients, more or less"),
    "dental practice": message("not_target", "not_a_technology_company", "A family dental practice"),
    "Short description: not given": message("unsure", "not_enough_information", "not given"),
    "developer tools": message(evidence="builds developer tools"),
    "protein models": REFUSAL,
    "data pipelines": message(stop="max_tokens", text='{"verdict": "tar'),
    "storage systems": api_error(anthropic.InternalServerError, 500),
    "threat detection": message(evidence="threat detection software", model="claude-opus-5", usage=FALLBACK_USAGE,
                                before=(FALLBACK_BLOCK,)),
    "kilogram scales": message(evidence="sells kilogram scales"),
    "labeling tools": message("target", "talent_competitor", "labeling tools"),
    "scheduling app": message("not_target", "not_a_technology_company", "People like mike stands"),
}


class Messages:
    def __init__(self, client, beta):
        self.client, self.beta = client, beta

    def create(self, **params):
        facts = params["messages"][0]["content"]
        self.client.calls.append({"beta": self.beta, **params})
        answer = next((value for word, value in self.client.answers.items() if word in facts), None)
        if answer is None:
            raise AssertionError("no SIMULATED answer for these facts")
        if isinstance(answer, Exception):
            raise answer
        return types.SimpleNamespace(to_dict=lambda mode="python": json.loads(json.dumps(answer)))


class Client:
    """Stand-in for the SDK client: records every request and answers from `answers`."""

    def __init__(self, answers):
        self.answers, self.calls, self.keys = dict(answers), [], []
        self.messages = Messages(self, False)
        self.beta = types.SimpleNamespace(messages=Messages(self, True))


CLIENT = Client(ANSWERS)


def make_client(key):
    CLIENT.keys.append(key)
    return CLIENT


jf.make_client = make_client


def run(*args):
    """Run the command; return (exit code, requests sent, the table it wrote or None)."""
    before = len(CLIENT.calls)
    H.lines.clear()
    code = jf.main(list(args))
    table = pd.read_csv(jf.FIT_CSV, dtype=str, keep_default_na=False).set_index("company_name") if jf.FIT_CSV.exists() else None
    return code, len(CLIENT.calls) - before, table


# --- SIMULATED companies -------------------------------------------------------------------
def job(name, domain, short, long_text="", industry="", employees="", recent="", title="AI Engineer"):
    return {"company_name": name, "company_domain": domain, "company_description": short,
            "company_long_description": long_text, "company_industry": industry, "employee_count": employees,
            "theirstack_jobs_last_30_days": recent, "job_title": title}


JOBS = [
    job("Alpha Robotics, Inc.", "alpharobotics.ai", "", "", "", "12", "3", "Forward Deployed Engineer"),
    job("Alpha Robotics", "alpharobotics.ai", "Alpha Robotics builds warehouse robots. SIMULATED",
        "ALPHA ROBOTICS ships arms; see alpharobotics.ai or AlphaRobotics on the web.", "Robotics", "40", "5",
        "Robotics Engineer"),
    job("Alpha Robotics", "alpharobotics.ai", "second description, never used", "", "", "", "", "robotics  engineer"),
    job("Bravo AI", "bravo.ai", "Bravo AI is an AI headhunter that places machine learning engineers at startups. SIMULATED"),
    job("Charlie Consulting", "charlieconsulting.com", "Charlie Consulting delivers AI consulting projects. SIMULATED"),
    job("Delta Dental Care", "deltadental.example", "A family dental practice with three offices. SIMULATED",
        industry="Medical Practices"),
    job("Echo Labs", "echolabs.io", "", title="Applied AI Engineer"),
    job("Foxtrot", "foxtrot.dev", "Foxtrot builds developer tools. SIMULATED"),
    job("Golf AI", "glf.io", "Golf AI trains protein models; glf.io. Its glf format is open. SIMULATED"),
    job("Hotel Data", "hoteldata.com", "Hotel Data runs data pipelines. SIMULATED"),
    job("India Systems", "indiasystems.com", "India Systems sells storage systems. SIMULATED"),
    job("Juliet AI", "juliet.ai", "Juliet AI makes threat detection software. SIMULATED"),
    job("Kilo", "kilo.example", "Kilo sells kilogram scales. SIMULATED"),
    job("Lima AI", "lima.ai", "Lima AI makes labeling tools. SIMULATED"),
    job("Mike AI", "mike.ai", "Mike AI is a scheduling app. Mike is fast. People like mike stands. MikeAI rocks; mike.ai. SIMULATED",
        title="Applied AI Engineer at Mike AI"),
]
DECISIONS = [("Bravo AI", "", "reject"), ("Charlie Consulting", "", "accept"), ("Delta Dental Care", "", "reject"),
             ("Foxtrot", "Foxtrot Systems Inc.", "accept"), ("Mike AI", "", "accept"), ("Echo Labs", "", "accept"),
             ("Kilo", "Kilo Holdings LLC", "reject")]


def write_inputs(jobs=JOBS, decisions=DECISIONS):
    pd.DataFrame(jobs).to_csv(jf.JOBS_CSV, index=False)
    pd.DataFrame([{"company_name": name} for name in ("Alpha Robotics", "Charlie Consulting", "Echo Labs", "Mike AI")]
                 ).to_csv(jf.ACCOUNTS_CSV, index=False)
    pd.DataFrame([{"company_name": name, "form_d_issuer": issuer, "decision": decision, "reason": "SIMULATED"}
                  for name, issuer, decision in decisions]).to_csv(jf.DECISIONS_CSV, index=False)


def fingerprint(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


# 1. the instructions and the schema
text, rules = jf.read_rules()
check("the instructions define five rules, each with its verdict",
      rules == {"builds_technology": "target", "services_company": "target", "talent_competitor": "not_target",
                "not_a_technology_company": "not_target", "not_enough_information": "unsure"})
schema = jf.answer_schema(rules)
check("the schema allows only the three verdicts, the rules of the table and the four fields",
      schema["properties"]["verdict"]["enum"] == ["target", "not_target", "unsure"]
      and schema["properties"]["rule"]["enum"] == sorted(rules) and schema["required"] == list(jf.FIELDS)
      and schema["additionalProperties"] is False and set(schema["properties"]) == set(jf.FIELDS))
check("no field asks the model to write out its reasoning, which the API can decline",
      not {"reasoning", "thinking", "trace"} & set(jf.FIELDS))
check("the instructions say that the name is hidden and name no client",
      jf.CONFIG["mask"] in text and "brix" not in text.lower())
bad_rules = tmp / "bad_rules.md"
bad_rules.write_text("| `target` | `a` | x |\n| `not_target` | `b` | y |\n", encoding="utf-8")
check("instructions without a rule for every verdict are refused", raises(ValueError, lambda: jf.read_rules(bad_rules)))
bad_rules.write_text("| `target` | `a` | x |\n| `not_target` | `b` | y |\n| `unsure` | `c` | z |\n"
                     "| `not_target` | `d` | y |\n| `target` | `d` | x |\n", encoding="utf-8")
check("instructions that list one rule twice are refused", raises(ValueError, lambda: jf.read_rules(bad_rules)))

# 2. the facts, and hiding the name
check("without jobs.csv the command exits 1 and sends nothing", run()[:2] == (1, 0) and logged("is missing"))
write_inputs()
companies = jf.company_facts(jf.read_table(jf.JOBS_CSV))
by_name = {company["name"]: company for company in companies}
alpha = by_name.get("Alpha Robotics, Inc.", {})
check("rows of one company are joined whatever the legal suffix: 13 companies", len(companies) == 13 and alpha)
check("the first description that is not empty is used, and the largest headcount and posting count",
      alpha.get("description", "").startswith("Alpha Robotics builds") and alpha.get("headcount") == "40"
      and alpha.get("recent_jobs") == "5")
check("job titles are listed once each", alpha.get("titles") == ["Forward Deployed Engineer", "Robotics Engineer"])
facts = {company["name"]: jf.facts_text(company) for company in companies}
check("an empty fact is written as 'not given'",
      "Short description: not given" in facts["Echo Labs"] and "Employees: not given" in facts["Echo Labs"])
def names(form, text):
    """True if `text` holds `form` as whole words, in any case."""
    return bool(re.search(rf"(?<![a-z0-9]){re.escape(form.lower())}(?![a-z0-9])", text.lower()))


check("no company's name or website is left in its facts, in any case",
      not any(names(name, facts[name]) or names(jf.strip_legal_suffix(name), facts[name]) or names(company["domain"], facts[name])
              for name, company in by_name.items()))
check("the name is hidden in upper case, joined into one word and inside a job title",
      "ALPHA" not in facts["Alpha Robotics, Inc."] and "AlphaRobotics" not in facts["Alpha Robotics, Inc."]
      and "MikeAI" not in facts["Mike AI"] and "Applied AI Engineer at [the company]" in facts["Mike AI"])
check("a short form is hidden as written, and left alone in lower case, where it is an ordinary word",
      "[the company] is fast" in facts["Mike AI"] and "People like mike stands" in facts["Mike AI"])
check("a name inside a longer word is left alone", "kilogram scales" in facts["Kilo"] and "[the company] sells" in facts["Kilo"])
check("a website label shorter than four letters is hidden only as part of the address",
      "Its glf format" in facts["Golf AI"] and "glf.io" not in facts["Golf AI"])
check("the facts hold only company-level fields",
      [line.split(":")[0] for line in facts["Bravo AI"].splitlines()]
      == ["Short description", "Long description", "Industry", "Employees", "Jobs posted in the last 30 days",
          "Open roles that matched the search"])

# 3. the request
request = jf.request_for("claude-fable-5-2", text, rules, facts["Bravo AI"])
check("the request sends the instructions word for word and the hidden facts as the only message",
      request["system"] == text and request["messages"] == [{"role": "user", "content": facts["Bravo AI"]}])
check("the request names the model, the token limit, the effort and the schema",
      request["model"] == "claude-fable-5-2" and request["max_tokens"] == jf.CONFIG["max_tokens"]
      and request["output_config"] == {"effort": "high", "format": {"type": "json_schema", "schema": schema}})
probe = Client({"headhunter": message()})
jf.ask(probe, request)
jf.ask(probe, jf.request_for("claude-haiku-5-5", text, rules, facts["Bravo AI"]))
check("a model with a fallback is asked through the beta endpoint with the fallback list and its header",
      probe.calls[0]["beta"] and probe.calls[0]["fallbacks"] == [{"model": "claude-opus-5"}]
      and probe.calls[0]["betas"] == ["server-side-fallback-2026-07-01"])
check("a model without a fallback is asked through the plain endpoint, with no fallback list",
      not probe.calls[1]["beta"] and "fallbacks" not in probe.calls[1] and "betas" not in probe.calls[1])
key = jf.request_key(request)
check("the fingerprint of a request is stable", key == jf.request_key(jf.request_for("claude-fable-5-2", text, rules, facts["Bravo AI"])))
check("the fingerprint changes with the facts, the instructions and the model",
      len({key, jf.request_key(jf.request_for("claude-fable-5-2", text, rules, facts["Kilo"])),
           jf.request_key(jf.request_for("claude-fable-5-2", text + " ", rules, facts["Bravo AI"])),
           jf.request_key(jf.request_for("claude-opus-5-5", text, rules, facts["Bravo AI"]))}) == 4)
real = REAL_MAKE_CLIENT(SIM_KEY)
check("the real client is the official SDK's, set to retry and to wait",
      isinstance(real, anthropic.Anthropic) and real.max_retries == jf.CONFIG["max_retries"] == 4)

# 4. reading an answer
good = jf.read_answer(message("not_target", "talent_competitor", "an  AI\nheadhunter", before=(FALLBACK_BLOCK,)), rules)
check("a usable answer gives the four fields on one line each, whatever blocks come before the text",
      good == {"verdict": "not_target", "rule": "talent_competitor", "evidence": "an AI headhunter",
               "explanation": "SIMULATED explanation."})
check("a declined request is not an answer", "declined (bio)" in str(raises(jf.AnswerError, lambda: jf.read_answer(REFUSAL, rules))))
check("an answer cut off by the token limit is not used",
      "cut off (max_tokens)" in str(raises(jf.AnswerError, lambda: jf.read_answer(ANSWERS["data pipelines"], rules))))
check("text that is not JSON is not used", raises(jf.AnswerError, lambda: jf.read_answer(message(text="target"), rules)))
check("an answer with a field missing, added or not text is not used",
      raises(jf.AnswerError, lambda: jf.read_answer(message(text='{"verdict": "target", "rule": "builds_technology"}'), rules))
      and raises(jf.AnswerError, lambda: jf.read_answer(message(text=json.dumps(
          {"verdict": "target", "rule": "builds_technology", "evidence": "x", "explanation": "y", "score": "9"})), rules))
      and raises(jf.AnswerError, lambda: jf.read_answer(message(text=json.dumps(
          {"verdict": "target", "rule": "builds_technology", "evidence": 5, "explanation": "y"})), rules)))
check("a rule that does not belong to the verdict is not used",
      "does not belong" in str(raises(jf.AnswerError, lambda: jf.read_answer(ANSWERS["labeling tools"], rules)))
      and raises(jf.AnswerError, lambda: jf.read_answer(message("target", "made_up_rule"), rules)))

# 5. the quote
check("a quote copied from the facts is found, whatever its case, spacing, quote marks or last full stop",
      jf.quote_found("an AI  Headhunter", facts["Bravo AI"]) and jf.quote_found('"[the company] is an AI headhunter".', facts["Bravo AI"])
      and jf.quote_found("it’s here", "Short description: It's here"))
check("a paraphrase, a quote from elsewhere and an empty quote are not found",
      not jf.quote_found("a headhunter for AI", facts["Bravo AI"]) and not jf.quote_found("dental practice", facts["Bravo AI"])
      and not jf.quote_found("", facts["Bravo AI"]) and not jf.quote_found(" . ", facts["Bravo AI"]))
check("a quote has to match whole words, so a stray letter or half a word proves nothing",
      not jf.quote_found("a", "Short description: data pipelines") and not jf.quote_found("ental practice", facts["Delta Dental Care"])
      and jf.quote_found("dental practice", facts["Delta Dental Care"]))

# 6. the cost
check("an answer is priced from its token counts at the model's list price",
      abs(jf.cost_usd(message(tokens=(1000, 500))) - 0.028) < 1e-9
      and abs(jf.cost_usd(message(tokens=(1000, 500), model="claude-haiku-5-5")) - 0.00035) < 1e-9)
check("with a fallback, every attempt is priced at its own model's price",
      abs(jf.cost_usd(ANSWERS["threat detection"]) - (535 * 8 + 412 * 5 + 264 * 25) / 1e6) < 1e-9)
check("cached tokens, if any appear, are counted and never free",
      jf.cost_usd(message(usage={"input_tokens": 1000, "output_tokens": 500, "cache_read_input_tokens": 100,
                                 "cache_creation_input_tokens": 50})) > 0.028 + 150 * 8 / 1e6 - 1e-9)
check("an answer without token counts, with no input tokens or from a model with no price stops the run",
      raises(jf.FatalError, lambda: jf.cost_usd(message(usage={})))
      and raises(jf.FatalError, lambda: jf.cost_usd(message(tokens=(0, 500))))
      and raises(jf.FatalError, lambda: jf.cost_usd(message(tokens=("1000", 500))))
      and raises(jf.FatalError, lambda: jf.cost_usd(message(model="some-other-model"))))
check("the fallback is recognised from the API's usage record",
      jf.by_fallback(ANSWERS["threat detection"]) and not jf.by_fallback(message()) and not jf.by_fallback(REFUSAL))

# 7. agreement with the reviewer
check("a verdict agrees, disagrees or is unsure against a decision, and says nothing without one",
      [jf.agreement(d, v) for d, v in (("accept", "target"), ("accept", "not_target"), ("reject", "not_target"),
                                       ("reject", "target"), ("accept", "unsure"), ("reject", "unsure"), ("", "target"),
                                       ("accept", ""))] == ["yes", "no", "yes", "no", "unsure", "unsure", "", ""])
labels = jf.reviewer_labels()
check("the reviewer's decisions become labels; a confirmed Form D match counts as kept",
      labels == {"bravo ai": "reject", "charlie consulting": "accept", "delta dental care": "reject", "foxtrot": "accept",
                 "mike ai": "accept", "echo labs": "accept"})
check("a row that rejects one Form D match is not a label for the company", "kilo" not in labels)
write_inputs(decisions=[("Bravo AI", "Bravo Holdings Inc.", "accept"), ("Bravo AI", "", "reject"),
                        ("Kilo", "", "reject"), ("Kilo", "Kilo Holdings LLC", "accept")])
check("a company rejected outright stays rejected whatever its other rows say, in either order",
      jf.reviewer_labels() == {"bravo ai": "reject", "kilo": "reject"})
write_inputs()

# 8. runs
accounts_before, decisions_before, jobs_before = (fingerprint(p) for p in (jf.ACCOUNTS_CSV, jf.DECISIONS_CSV, jf.JOBS_CSV))
code, sent, table = run("--dry-run")
check("a dry run sends nothing, writes nothing and counts 13 requests",
      (code, sent, table) == (0, 0, None) and not jf.RAW_DIR.exists() and logged("13 request(s) would be sent")
      and CLIENT.keys == [])
IGNORED["value"] = False
code, sent, table = run()
check("when git does not ignore the output, nothing is sent", (code, sent, table) == (1, 0, None) and logged("git does not ignore"))
IGNORED["value"] = True
del os.environ[jf.KEY_ENV]
code, sent, table = run()
check("without a key, nothing is sent", (code, sent, table) == (1, 0, None) and logged("is not set in .env"))
os.environ[jf.KEY_ENV] = SIM_KEY
check("a company that is not in jobs.csv is refused", run("--companies", "Zulu Corp")[:2] == (1, 0) and logged("Not in"))
parser_refused = False
try:
    jf.main(["--limit", "-1"])
except SystemExit as stop:
    parser_refused = stop.code == 2
check("a negative --limit is refused", parser_refused)

code, sent, table = run("--limit", "2")
check("--limit 2 sends two requests, in name order, and the table still has every company",
      (code, sent) == (0, 2) and len(table) == 13 and table["verdict"].ne("").sum() == 2
      and table.loc["Alpha Robotics, Inc.", "verdict"] == "target" and table.loc["Bravo AI", "verdict"] == "not_target")
check("the companies beyond the limit are marked as not asked",
      table.loc["Charlie Consulting", "status"] == "not asked: --limit was reached" and table["status"].str.startswith("not asked").sum() == 11)
check("the log says what the companies left would cost at the measured rate",
      logged("Spent in this run: $0.0560 at list price for 2 answer(s)")
      and logged("At this rate the 11 companies not asked would cost about $0.31."))
check("each answer is saved with its time, its facts and the request fingerprint",
      len(list(jf.RAW_DIR.glob("fit_*.json"))) == 2
      and all({"saved_at", "company_name", "request_key", "model", "facts", "response"} <= set(json.loads(p.read_text(encoding="utf-8")))
              for p in jf.RAW_DIR.glob("fit_*.json")))
check("the key reaches the SDK client and nothing else: no saved answer, table or log line holds it",
      CLIENT.keys == [SIM_KEY] and not any(SIM_KEY in p.read_text(encoding="utf-8") for p in tmp.rglob("*") if p.is_file())
      and not any(SIM_KEY in line for line in H.lines))
check("the log holds counts only: no company is named",
      not any(name.split(",")[0].lower() in line.lower() for name in by_name for line in H.lines))

code, sent, table = run()
check("the next run asks only about the companies without a saved answer", (code, sent) == (0, 11))
check("verdicts, rules and explanations are written to the table",
      list(table.loc["Delta Dental Care", ["verdict", "rule", "explanation", "status"]])
      == ["not_target", "not_a_technology_company", "SIMULATED explanation.", "ok"])
check("the table says whether the company is on the account list",
      table.loc["Alpha Robotics, Inc.", "listed"] == "yes" and table.loc["Bravo AI", "listed"] == "no")
check("a quote that is in the facts is marked found, and a paraphrase is marked not found",
      table.loc["Bravo AI", "evidence_found"] == "yes" and table.loc["Charlie Consulting", "evidence_found"] == "no")
check("a quote of words the script left in the facts is found there", table.loc["Mike AI", "evidence_found"] == "yes")
check("agreement with the reviewer is written per company",
      list(table.loc[["Bravo AI", "Charlie Consulting", "Delta Dental Care", "Foxtrot", "Mike AI", "Echo Labs", "Kilo", "Alpha Robotics, Inc."], "agrees"])
      == ["yes", "yes", "yes", "yes", "no", "unsure", "", ""])
check("a declined request, a cut-off answer and a wrong rule are marked, not turned into verdicts",
      table.loc["Golf AI", "status"] == "not judged: the model declined (bio)" and table.loc["Golf AI", "verdict"] == ""
      and table.loc["Hotel Data", "status"] == "not judged: the answer was cut off (max_tokens)"
      and "does not belong" in table.loc["Lima AI", "status"] and table.loc["Lima AI", "verdict"] == "")
check("a server error costs one company and the run goes on",
      table.loc["India Systems", "status"] == "not judged: InternalServerError" and table.loc["Juliet AI", "verdict"] == "target"
      and code == 0)
check("an answer from the fallback model is marked, with the model that answered",
      table.loc["Juliet AI", "status"] == "ok: answered by the fallback model" and table.loc["Juliet AI", "answered_by"] == "claude-opus-5"
      and table.loc["Bravo AI", "answered_by"] == "claude-fable-5-2")
check("each row carries the token counts and the cost of its answer",
      table.loc["Bravo AI", "input_tokens"] == "1000" and table.loc["Bravo AI", "output_tokens"] == "500"
      and float(table.loc["Bravo AI", "cost_usd"]) == 0.028 and float(table.loc["Golf AI", "cost_usd"]) == 0.0072)
check("the log counts the verdicts and the quotes",
      logged("Verdicts: 5 target, 3 not_target, 1 unsure. Quotes found in the facts: 8 of 9. Answered by the fallback model: 1.")
      and logged("Listed accounts the model doubts: 2"))
check("the log sets the verdicts against the reviewer's decisions",
      logged("Against the reviewer's 6 decision(s), 6 judged: agree 4, disagree 1, unsure 1. Kept by the reviewer: 2 target, "
             "1 not_target, 1 unsure. Rejected by the reviewer: 0 target, 2 not_target, 0 unsure."))
check("the log reports what was asked, read and spent",
      logged("asked now 11, read from saved answers 2, not asked 0, asked but not judged 4")
      and logged("Spent in this run: $0.2441 at list price for 10 answer(s)"))
check("the run changes neither the account list, the reviewer's decisions nor the jobs",
      (accounts_before, decisions_before, jobs_before) == tuple(fingerprint(p) for p in (jf.ACCOUNTS_CSV, jf.DECISIONS_CSV, jf.JOBS_CSV)))

CLIENT.answers["storage systems"] = message(evidence="sells storage systems")
code, sent, table = run()
check("a third run asks only about the company that got no answer; declined and unusable answers are not paid for twice",
      (code, sent) == (0, 1) and table.loc["India Systems", "verdict"] == "target"
      and table.loc["Golf AI", "source"] == "saved answer" and table.loc["Lima AI", "source"] == "saved answer")
first_table = table.drop(columns=["source", "judged_at"])
code, sent, table = run()
check("a run with every answer saved sends nothing, needs no key and writes the same table",
      (code, sent) == (0, 0) and table.drop(columns=["source", "judged_at"]).equals(first_table)
      and logged("Spent in this run: $0") and CLIENT.keys == [SIM_KEY] * 3)
IGNORED["value"] = False
check("with nothing to ask, the table can be rebuilt even where git cannot be asked", run("--limit", "0")[:2] == (0, 0))
IGNORED["value"] = True

changed = [dict(row, company_description=row["company_description"].replace("warehouse robots", "warehouse robots and arms"))
           for row in JOBS]
write_inputs(jobs=changed)
code, sent, table = run()
check("changed facts are asked about again, and only those", (code, sent) == (0, 1) and "and arms" in CLIENT.calls[-1]["messages"][0]["content"])
write_inputs()
code, sent, table = run("--refresh", "--companies", "Bravo AI, Kilo")
check("--refresh with --companies asks again about those companies only, and keeps the saved answers of the others",
      (code, sent) == (0, 2) and table["verdict"].ne("").sum() == 10 and table.loc["Bravo AI", "source"] == "asked now"
      and table.loc["Delta Dental Care", "source"] == "saved answer")
code, sent, table = run("--model", "claude-haiku-5-5", "--limit", "1")
check("another model has its own saved answers, and its requests carry no fallback list",
      (code, sent) == (0, 1) and CLIENT.calls[-1]["model"] == "claude-haiku-5-5" and not CLIENT.calls[-1]["beta"]
      and table["verdict"].ne("").sum() == 1)

shutil.rmtree(jf.RAW_DIR)
code, sent, table = run("--max-usd", "0.03")
check("the spend cap stops the requests: two answers at $0.028 pass a $0.03 cap, the third is not sent",
      (code, sent) == (0, 2) and table["status"].eq("not asked: the spend cap was reached").sum() == 11
      and logged("The spend cap of $0.03 was reached after 2 request(s)"))

shutil.rmtree(jf.RAW_DIR)
CLIENT.answers["consulting projects"] = api_error(anthropic.AuthenticationError, 401)
code, sent, table = run()
check("a refused key stops the run at once: exit 1, and no later company is asked",
      (code, sent) == (1, 3) and table.loc["Charlie Consulting", "status"] == "not judged: AuthenticationError"
      and table.loc["Delta Dental Care", "status"] == "not asked: the run stopped" and table["verdict"].ne("").sum() == 2
      and logged("Stopped; nothing more is sent"))
check("which errors stop a run: any HTTP 4xx but 408, 409 and 429; a busy or failing server does not",
      [jf.is_fatal(api_error(kind, status)) for kind, status in (
          (anthropic.BadRequestError, 400), (anthropic.AuthenticationError, 401), (anthropic.PaymentRequiredError, 402),
          (anthropic.PermissionDeniedError, 403), (anthropic.NotFoundError, 404), (anthropic.RateLimitError, 429),
          (anthropic.InternalServerError, 500), (anthropic.InternalServerError, 529))]
      == [True, True, True, True, True, False, False, False]
      and not jf.is_fatal(anthropic.APIConnectionError(request=REQUEST)))
shutil.rmtree(jf.RAW_DIR)
CLIENT.answers["consulting projects"] = message("target", "services_company", "consulting projects", usage={})
code, sent, table = run()
check("an answer without token counts stops the run, because the cap could no longer be kept",
      (code, sent) == (1, 3) and logged("Stopped:") and table.loc["Delta Dental Care", "status"] == "not asked: the run stopped")

# 9. the grader itself: answers that copy the reviewer score full marks, a constant answer does not
shutil.rmtree(jf.RAW_DIR)
CLIENT.answers = {word: (message("unsure", "not_enough_information", "x") if not isinstance(value, dict) else value)
                  for word, value in ANSWERS.items()}
CLIENT.answers.update({"consulting projects": message("target", "services_company", "consulting projects"),
                       "scheduling app": message(evidence="a scheduling app"),
                       "Short description: not given": message(evidence="not given")})
code, sent, table = run()
check("answers that match every decision score 6 of 6", logged("6 judged: agree 6, disagree 0, unsure 0"))
shutil.rmtree(jf.RAW_DIR)
CLIENT.answers = {word: message(evidence="words that are in no description") for word in ANSWERS}
code, sent, table = run()
check("answering 'target' for everything agrees on the kept companies only: 4 of 6, and both rejected ones are missed",
      logged("6 judged: agree 4, disagree 2, unsure 0") and logged("Rejected by the reviewer: 2 target, 0 not_target, 0 unsure")
      and logged("Quotes found in the facts: 0 of 13"))

# 10. what the script is able to do at all
source = (PROJECT / "judge_fit.py").read_text(encoding="utf-8")
code_only = source.split('"""', 2)[2]
check("every request goes through the official SDK: no address and no other HTTP library in the code",
      not re.search(r"https?://|import requests|requests\.|urllib|httpx", code_only) and "import anthropic" in code_only)
check("the script only reads the account list, the decisions and the jobs",
      not re.search(r"(ACCOUNTS_CSV|DECISIONS_CSV|JOBS_CSV)\.(write|open|unlink)|to_csv\((ACCOUNTS_CSV|DECISIONS_CSV|JOBS_CSV)", code_only)
      and code_only.count("to_csv(") == 1)
workflow = (PROJECT / ".github" / "workflows" / "weekly.yml").read_text(encoding="utf-8")
check("the weekly job never runs this step and never sees its key",
      "judge_fit" not in workflow and "ANTHROPIC" not in workflow.upper())
example = (PROJECT / ".env.example").read_text(encoding="utf-8")
check(".env.example names the key with a placeholder, not a key",
      re.search(r"^ANTHROPIC_API_KEY=your-", example, re.M) and "sk-ant-" not in example)
check("requirements.txt pins the SDK to one version, so an update cannot fail the weekly run's checks",
      re.search(r"^anthropic==\d+\.\d+\.\d+$", (PROJECT / "requirements.txt").read_text(encoding="utf-8"), re.M))
check("every model that can be chosen has a price, and every fallback is a model with a price",
      all(price["input"] > 0 and price["output"] > 0 and price.get("fallback", name) in jf.CONFIG["models"]
          for name, price in jf.CONFIG["models"].items()) and jf.CONFIG["default_model"] in jf.CONFIG["models"])

print(f"\n{sum(results)}/{len(results)} checks passed")
if all(results):
    shutil.rmtree(tmp, ignore_errors=True)
sys.exit(0 if all(results) else 1)
