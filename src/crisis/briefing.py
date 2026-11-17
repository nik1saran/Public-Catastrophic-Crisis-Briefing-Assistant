"""LLM summarization and fact extraction on top of the verified batch.

Flow: verified batch -> select safe records -> group by event -> flag important
developments (rules) -> LLM extracts facts + writes a summary -> code checks every
fact and number against the source text -> briefing items for downstream steps.

The LLM never decides what is trusted or important, and anything it returns that is
not found in the cited source text is rejected.
"""
from __future__ import annotations

import difflib
import importlib
import itertools
import json
import os
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .prompts import DEFAULT_VERSION, PROMPT_VERSIONS, TEMPLATES, USER_TEMPLATE
from .verification import _freshness_bucket


def load_env(path: Path | None = None) -> None:
    """Load KEY=value lines from the repo's .env into os.environ (real environment variables win)."""
    path = path or Path(__file__).resolve().parents[2] / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        key, sep, value = line.strip().partition("=")
        if sep and key and not key.startswith("#") and value.strip():
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


load_env()  # lets .env hold ANTHROPIC_API_KEY and, optionally, CRISIS_LLM
MODEL = "claude-opus-5-5"  # default Claude model; override with CRISIS_LLM=anthropic:<model-id>
SAFE_STATUSES = {"confirmed", "preliminary"}

# An LLM backend is any callable (system, user, json_schema) -> dict.
LLMFn = Callable[[str, str, dict], dict]


class AnthropicJSON:
    """Claude backend that returns schema-constrained JSON."""

    def __init__(self, model: str = MODEL, client: Any = None):
        if client is None:
            import anthropic  # imported lazily so the rest of the module works without it

            client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from the environment
        self.client, self.model = client, model

    def __call__(self, system: str, user: str, schema: dict) -> dict:
        resp = self.client.messages.create(
            model=self.model,
            max_tokens=16000,
            system=system,
            messages=[{"role": "user", "content": user}],
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": schema}},
        )
        if resp.stop_reason != "end_turn":
            raise RuntimeError(f"LLM stopped early: {resp.stop_reason}")
        return json.loads(next(b.text for b in resp.content if b.type == "text"))


def get_llm(spec: str | None = None) -> LLMFn:
    """Pick a backend without editing code. spec (or env CRISIS_LLM):
      'anthropic'                  -> Claude with the default model (MODEL above)
      'anthropic:<model-id>'       -> Claude with that model
      'package.module:factory'     -> your own backend: factory() returns a function
                                      (system: str, user: str, json_schema: dict) -> dict
    A custom backend must return JSON that follows json_schema; the grounding checks
    below apply to every backend, so unreliable output is rejected, not trusted."""
    spec = spec or os.environ.get("CRISIS_LLM", "anthropic")
    if spec == "anthropic" or spec.startswith("anthropic:"):
        return AnthropicJSON(spec.partition(":")[2] or MODEL)
    module, _, factory = spec.partition(":")
    return getattr(importlib.import_module(module), factory)()


# ---------------------------------------------------------------- input selection

def _still_current(r: dict) -> bool:
    """Re-check expiry and freshness NOW: the batch's is_active flag was computed when the
    pipeline ran, which may be hours or days ago."""
    expires = r.get("expires_at_utc")
    if expires and datetime.now(timezone.utc) > datetime.fromisoformat(expires.replace("Z", "+00:00")):
        return False
    basis = r.get("published_at_utc") or r.get("retrieved_at_utc")
    return _freshness_bucket(basis, r.get("source_name")) != "stale"


def select_input_records(batch: dict) -> list[dict]:
    """Only active, current, non-superseded, confirmed/preliminary records may reach the LLM.
    NOAA Test/Exercise messages are excluded here too, in case older data files contain them."""
    records = batch.get("active_records") or batch.get("records", [])
    return [
        r for r in records
        if r.get("is_active", True) and not r.get("is_superseded") and r.get("status") in SAFE_STATUSES
        and _still_current(r) and (r.get("source_metadata") or {}).get("noaa_status", "Actual") == "Actual"
    ]


def group_events(records: list[dict]) -> dict[str, list[dict]]:
    events: dict[str, list[dict]] = defaultdict(list)
    for r in records:
        events[r["event_id"]].append(r)
    return events


# ---------------------------------------------------------------- important developments

def important_developments(records: list[dict]) -> list[str]:
    """Rule-based reasons an event deserves attention (empty list = routine)."""
    reasons: list[str] = []
    for r in records:
        m = r.get("source_metadata") or {}
        if m.get("noaa_severity") in ("Severe", "Extreme") or m.get("noaa_urgency") == "Immediate":
            reasons.append(f"high-severity NOAA alert ({m.get('noaa_event')})")
        if m.get("noaa_message_type") in ("Update", "Cancel"):
            reasons.append(f"NOAA alert {m['noaa_message_type'].lower()}")
        if m.get("usgs_tsunami") == 1 or m.get("usgs_alert") in ("orange", "red") or (m.get("usgs_mag") or 0) >= 6:
            reasons.append("significant earthquake")
        if "fema_disaster_number" in m:
            reasons.append("federal disaster declaration")
        if r.get("conflict_flags"):
            reasons.append("sources conflict")
        if r.get("status") == "confirmed":
            reasons.append("confirmed by multiple sources")
    return sorted(set(reasons))


# ---------------------------------------------------------------- grounding helpers

def _norm(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text)).strip().lower()


def record_text(r: dict) -> str:
    """Everything the source actually said about this record - the grounding corpus."""
    parts = [r.get("claim_text"), r.get("location_name"), r.get("source_name"),
             r.get("published_at_utc"), r.get("expires_at_utc"), r.get("last_updated_at_utc")]
    parts += [v for v in (r.get("source_metadata") or {}).values() if v not in (None, "")]
    return " \n".join(str(p) for p in parts if p not in (None, ""))


def _canon(token: str) -> str:
    token = token.replace(",", "").rstrip(".")
    try:
        value = float(token)
    except ValueError:
        return token
    return str(int(value)) if value == int(value) else str(value)  # "1.0" and "1" are the same number


def _numbers(text: str) -> set[str]:
    return {_canon(t) for t in re.findall(r"\d[\d,]*\.?\d*", text)}


FACT_ITEM = {"type": "object", "additionalProperties": False, "required": ["record_id", "quote"],
             "properties": {"record_id": {"type": "string"}, "quote": {"type": "string"}}}


def _with(extra: dict) -> dict:
    return {**FACT_ITEM, "required": FACT_ITEM["required"] + list(extra),
            "properties": {**FACT_ITEM["properties"], **extra}}


SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["casualties", "affected_population", "locations", "emergency_actions", "summary",
                 "answerable", "information_gaps", "cited_record_ids"],
    "properties": {
        "answerable": {"type": "boolean"},
        "information_gaps": {"type": "array", "items": {"type": "string"}},
        "casualties": {"type": "array", "items": _with({"value": {"type": "integer"}})},
        "affected_population": {"type": "array", "items": _with({"value": {"type": "integer"}})},
        "locations": {"type": "array", "items": _with({"name": {"type": "string"}})},
        "emergency_actions": {"type": "array", "items": _with({"action": {"type": "string"}})},
        "summary": {"type": "string"},
        "cited_record_ids": {"type": "array", "items": {"type": "string"}},
    },
}

def _prompt(records: list[dict], question: str) -> str:
    blocks = [f'<record id="{r["record_id"]}" status="{r["status"]}" source="{r["source_name"]}">\n'
              f"{record_text(r)}\n</record>" for r in records]
    return USER_TEMPLATE.format(context="\n".join(blocks), question=question)


def ground_facts(facts: dict, records: list[dict]) -> tuple[dict, list[dict]]:
    """Keep only facts whose quote really appears in the cited record (and contains the value)."""
    corpus = {r["record_id"]: _norm(record_text(r)) for r in records}
    kept: dict[str, list[dict]] = {}
    rejected: list[dict] = []
    for field in ("casualties", "affected_population", "locations", "emergency_actions"):
        kept[field] = []
        for item in facts.get(field, []):
            text = corpus.get(item.get("record_id"))
            ok = text is not None and _norm(item.get("quote", "")) in text
            if ok and "value" in item:
                ok = str(item["value"]) in _numbers(item["quote"])
            if ok and "name" in item:
                ok = _norm(item["name"]) in text
            (kept[field] if ok else rejected).append(item if ok else {"field": field, **item})
    return kept, rejected


def _structured_facts(records: list[dict]) -> dict[str, list[dict]]:
    """Impact numbers a parser already extracted into structured fields."""
    out: dict[str, list[dict]] = {"casualties": [], "affected_population": []}
    for r in records:
        for field, key in (("casualties", "casualties_reported"), ("affected_population", "affected_population_reported")):
            if r.get(key) is not None:
                out[field].append({"value": r[key], "record_id": r["record_id"], "quote": f"{key} field"})
    return out


def fallback_summary(records: list[dict]) -> str:
    claims = list(dict.fromkeys(r["claim_text"] for r in records))
    text = " ".join(claims[:5])
    return text + (f" (and {len(claims) - 5} more related reports)" if len(claims) > 5 else "")


_NOT_PLACES = {"I", "UTC", "Preliminary", "Confirmed", "Officials", "The", "A", "An", "According", "Sources", "Records"}
# One-word US state names, so "California" is accepted when the sources say "CA".
_STATE_WORDS = ("AL Alabama AK Alaska AZ Arizona AR Arkansas CA California CO Colorado CT Connecticut DE Delaware "
                "FL Florida GA Georgia HI Hawaii ID Idaho IL Illinois IN Indiana IA Iowa KS Kansas KY Kentucky "
                "LA Louisiana ME Maine MD Maryland MA Massachusetts MI Michigan MN Minnesota MS Mississippi "
                "MO Missouri MT Montana NE Nebraska NV Nevada OH Ohio OK Oklahoma OR Oregon PA Pennsylvania "
                "TN Tennessee TX Texas UT Utah VT Vermont VA Virginia WA Washington WI Wisconsin WY Wyoming").split()
_STATES = {name.lower(): abbr.lower() for abbr, name in zip(_STATE_WORDS[::2], _STATE_WORDS[1::2])}


def _in_sources(word: str, known: set[str]) -> bool:
    """True if `word` (or its singular form, or a state whose abbreviation appears) is in the sources."""
    w = word.lower()
    forms = {w, w.removesuffix("s"), w.removesuffix("es"), w.removesuffix("ies") + "y"}
    return bool(forms & known) or _STATES.get(w) in known


def ungrounded_places(text: str, records: list[dict]) -> list[str]:
    """Capitalized words in `text` that appear nowhere in the source records (likely invented places or names).
    Plurals, possessives, hyphenated words and state names whose abbreviation is in the sources are accepted.
    The first word of each sentence is skipped because it is capitalized anyway, so a place name that
    only ever appears at the start of a sentence is not caught."""
    known = {t.strip("'’-") for t in re.findall(r"[a-z0-9'’-]+", _norm(" ".join(record_text(r) for r in records)))}
    known |= {k for w in list(known) for k in w.split("-")}
    found: set[str] = set()
    for sentence in re.split(r"(?<=[.!?])\s+", text):
        for token in re.findall(r"[A-Za-z][A-Za-z'’-]*", sentence)[1:]:
            for part in token.split("-"):
                part = re.sub(r"['’]s$", "", part)
                # one-letter "words" are fragments such as the T and Z in 2026-11-17T21:35:30Z, not names
                if len(part) > 1 and part[0].isupper() and part not in _NOT_PLACES and not _in_sources(part, known):
                    found.add(part)
    return sorted(found)


def _summary_problem(summary: str, cited: list[str], records: list[dict]) -> str | None:
    """Why an answer fails the grounding checks, or None if it passes."""
    ids = {r["record_id"] for r in records}
    if not summary.strip() or not cited:
        return "empty or uncited answer"
    if not set(cited) <= ids:
        return "cites record ids that were not provided"
    source_numbers = _numbers(" ".join(record_text(r) for r in records)) | {str(len(records))}
    if extra := sorted(_numbers(summary) - source_numbers):
        return f"numbers not in sources: {extra}"
    if places := ungrounded_places(summary, records):
        return f"names not in sources: {places}"
    return None


# ---------------------------------------------------------------- main entry points

DISCLOSURE = "AI-generated from verified sources. A person must review this before public release."


def _conflicts(records: list[dict]) -> dict:
    """Conflicts the verification step found between sources for these records."""
    flags = sorted({f for r in records for f in r.get("conflict_flags", [])})
    details = [d for r in records for d in r.get("conflict_details", [])]
    return {"has_conflict": bool(flags), "flags": flags, "details": details}


def _review(source: str, rejected: list, status: str | None = None, conflicts: dict | None = None) -> dict:
    """Every output requires human sign-off; review_notes say what deserves a closer look."""
    notes = []
    if conflicts and conflicts["has_conflict"]:
        notes.append("sources conflict: " + ", ".join(conflicts["flags"]))
    if source == "fallback":
        notes.append("model output failed grounding checks; showing source text")
    if rejected:
        notes.append(f"{len(rejected)} model fact(s) rejected as unsupported by the sources")
    if status == "preliminary":
        notes.append("contains preliminary information")
    return {"requires_human_review": True, "disclosure": DISCLOSURE, "review_notes": notes}


def _ask(records: list[dict], question: str, llm: LLMFn, version: str) -> dict:
    """One grounded LLM call. Never raises: failures fall back to the sources' own text."""
    out = {"answer": fallback_summary(records), "answer_source": "fallback", "answerable": True,
           "information_gaps": [], "cited_record_ids": [r["record_id"] for r in records],
           "facts": {k: [] for k in ("casualties", "affected_population", "locations", "emergency_actions")},
           "rejected_facts": [], "llm_error": None}
    try:
        raw = llm(PROMPT_VERSIONS[version], _prompt(records, question), SCHEMA)
        out["facts"], out["rejected_facts"] = ground_facts(raw, records)
        if raw.get("answerable") is False:  # model says the records don't contain the answer
            out.update(answer=raw.get("summary", "").strip() or "The verified sources provided do not report this.",
                       answer_source="llm", answerable=False, cited_record_ids=[],
                       information_gaps=[str(g) for g in raw.get("information_gaps", [])])
        elif (problem := _summary_problem(raw.get("summary", ""), raw.get("cited_record_ids", []), records)) is None:
            out.update(answer=raw["summary"].strip(), answer_source="llm", cited_record_ids=raw["cited_record_ids"],
                       information_gaps=[str(g) for g in raw.get("information_gaps", [])])
        else:
            out["llm_error"] = f"answer failed grounding check ({problem}); used source claim text"
    except Exception as exc:  # network, refusal, bad JSON: never block the briefing
        out["llm_error"] = f"{type(exc).__name__}: {exc}"
    return out


def answer_question(batch: dict, question: str, llm: LLMFn, template: str | None = None,
                    version: str = DEFAULT_VERSION, **fmt: str) -> dict:
    """Answer a crisis question from verified records only.
    `template` is a key of TEMPLATES (formatted with **fmt, e.g. area="Bay County"); otherwise
    `question` is used as written."""
    if template:
        question = TEMPLATES[template].format(**fmt)
    records = select_input_records(batch)
    if not records:  # nothing safe to answer from: don't call the model at all
        return {"question": question, "answer": "No active verified information is available.",
                "answerable": False, "answer_source": "none", "information_gaps": ["no verified records"],
                "cited_record_ids": [], "facts": {}, "rejected_facts": [], "llm_error": None,
                "prompt_version": version, "conflicts": _conflicts([]), **_review("none", [])}
    res = _ask(records, question, llm, version)
    ids = set(res["cited_record_ids"])
    res["sources"] = [{"record_id": r["record_id"], "source_name": r["source_name"], "source_url": r["source_url"],
                       "status": r["status"], "published_at_utc": r.get("published_at_utc"),
                       "retrieved_at_utc": r["retrieved_at_utc"]} for r in records if r["record_id"] in ids]
    if res["answer_source"] == "fallback":  # source claims would not answer the question; say so instead
        res["answer"] = "A verified answer could not be generated for this question. Review the source records directly."
    status = "preliminary" if any(s["status"] == "preliminary" for s in res["sources"]) else None
    conflicts = _conflicts([r for r in records if r["record_id"] in ids])
    return {"question": question, "prompt_version": version, **res, "conflicts": conflicts,
            **_review(res["answer_source"], res["rejected_facts"], status, conflicts)}


def check_consistency(batch: dict, question: str, llm: LLMFn, runs: int = 3, template: str | None = None,
                      version: str = DEFAULT_VERSION, **fmt: str) -> dict:
    """Ask the same question `runs` times and report how much the answers agree.
    Agreement is judged on things that matter in a briefing: whether the model answered, the numbers in
    the answer, and the locations it extracted. `text_similarity` (0-1) is wording overlap, for reference."""
    results = [answer_question(batch, question, llm, template, version, **fmt) for _ in range(max(2, runs))]
    answers = [r["answer"] for r in results]
    pairs = list(itertools.combinations(answers, 2))
    out = {
        "question": results[0]["question"], "runs": len(results),
        "answerable_agree": len({r["answerable"] for r in results}) == 1,
        "numbers_agree": len({frozenset(_numbers(a)) for a in answers}) == 1,
        "locations_agree": len({frozenset(_norm(i["name"]) for i in r["facts"].get("locations", []))
                                for r in results}) == 1,
        "text_similarity": round(sum(difflib.SequenceMatcher(None, a, b).ratio() for a, b in pairs) / len(pairs), 2),
        "answers": answers,
    }
    out["consistent"] = out["answerable_agree"] and out["numbers_agree"] and out["locations_agree"]
    return out


def build_event_briefing(event_id: str, records: list[dict], llm: LLMFn, version: str = DEFAULT_VERSION) -> dict:
    res = _ask(records, TEMPLATES["summary"], llm, version)
    summary, cited, source, error = res["answer"], res["cited_record_ids"], res["answer_source"], res["llm_error"]
    facts, rejected = res["facts"], res["rejected_facts"]
    for field, items in _structured_facts(records).items():
        facts[field] = items + facts[field]
    reasons = important_developments(records)
    conflicts = _conflicts(records)
    return {
        "event_id": event_id,
        "event_type": records[0].get("event_type"),
        "status": "preliminary" if any(r["status"] == "preliminary" for r in records) else "confirmed",
        "summary": summary,
        "summary_source": source,
        "cited_record_ids": cited,
        "facts": facts,
        "not_reported": [f for f in ("casualties", "affected_population") if not facts[f]],
        "rejected_facts": rejected,
        "important": {"is_important": bool(reasons), "reasons": reasons},
        "sources": [{"record_id": r["record_id"], "source_name": r["source_name"], "source_url": r["source_url"],
                     "published_at_utc": r.get("published_at_utc"), "retrieved_at_utc": r["retrieved_at_utc"]}
                    for r in records],
        "llm_error": error,
        "conflicts": conflicts,
        **_review(source, rejected, "preliminary" if any(r["status"] == "preliminary" for r in records) else None,
                  conflicts),
    }


def build_briefing(batch: dict, llm: LLMFn, max_events: int | None = None, version: str = DEFAULT_VERSION) -> dict:
    events = list(group_events(select_input_records(batch)).items())
    events.sort(key=lambda e: not important_developments(e[1]))  # important first, so max_events never drops them
    items = [build_event_briefing(eid, recs, llm, version) for eid, recs in events[:max_events]]
    return {
        "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "input_run_id": batch.get("ingestion_run_id"),
        "model": getattr(llm, "model", "custom"),
        "requires_human_review": True,
        "disclosure": DISCLOSURE,
        "events": items,
    }
