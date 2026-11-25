"""Person 3: crisis analysis, briefing generation and safety.

Takes briefings produced by briefing.build_briefing() and turns them into material an executive can act on:

  compare_briefings()        what changed since the previous briefing, plus simple trends
  build_situation_report()   one structured report: priorities, confirmed / preliminary / conflicting /
                             unavailable information, changes, and open information gaps
  render_executive_briefing() the report as a Markdown executive briefing (fixed template)
  draft_public_statement()   a plain-language public statement built only from what the sources support
  check_release_safety()     blocks casualty figures and other numbers that no verified source states
  approve() / reject() / edit_draft() / release()
                             human approval: nothing can be released until a named person approves the
                             exact text, and any edit after approval sends it back for review

Everything here is deterministic. No LLM call is made in this module, so the same briefing always gives
the same report, and every number in a draft can be traced back to a verified record.
"""
from __future__ import annotations

import hashlib
import re
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .audit import append_manifest
from .briefing import DISCLOSURE, _canon, _numbers

FIGURE_FIELDS = ("casualties", "affected_population")
STATUS_ORDER = ("confirmed", "preliminary", "conflicting")

# Words that make a number in a sentence a claim about harm to people.
_HARM = re.compile(
    r"\b(kill(?:ed|s)?|dead|deaths?|died|dying|fatalit(?:y|ies)|casualt(?:y|ies)|injur(?:ed|ies|y)|hurt|"
    r"wounded|missing|displaced|evacuat(?:ed|ions?)|homeless|hospitali[sz]ed|victims?|lives|people|residents)\b",
    re.I)
# Vague quantities that read like figures but come from no source.
_VAGUE = re.compile(r"\b(dozens|hundreds|thousands|scores|many|numerous|countless)\s+(?:of\s+)?"
                    r"(people|residents|deaths|dead|victims|injur\w*|lives|casualties)\b", re.I)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------- per-event helpers

def information_status(event: dict) -> str:
    """confirmed / preliminary / conflicting. Conflicting wins: a confirmed record that another
    source contradicts must not be presented as settled fact."""
    return "conflicting" if (event.get("conflicts") or {}).get("has_conflict") else event.get("status", "preliminary")


def verified_figures(event: dict) -> dict[str, list[int]]:
    """Casualty and population numbers that the briefing step verified (structured field or exact quote)."""
    facts = event.get("facts") or {}
    return {f: sorted({int(i["value"]) for i in facts.get(f, []) if i.get("value") is not None}) for f in FIGURE_FIELDS}


def _location(event: dict, record_locations: dict[str, str] | None = None) -> str:
    """Locations the model extracted (and the code verified); otherwise the records' own location field."""
    locs = [i.get("name") for i in (event.get("facts") or {}).get("locations", []) if i.get("name")]
    if not locs and record_locations:
        locs = [record_locations[s["record_id"]] for s in event.get("sources", []) if record_locations.get(s["record_id"])]
    locs = list(dict.fromkeys(p.strip() for loc in locs for p in loc.split(";") if p.strip()))  # NOAA joins zones with ";"
    if len(locs) > 3:
        return ", ".join(locs[:3]) + f" and {len(locs) - 3} more"
    return ", ".join(locs) or "location not stated"


_EVENT_NOUNS = {"other": "declared emergency", "storm": "weather alert", "earthquake": "earthquake",
                "flood": "flood", "wildfire": "wildfire", "hurricane": "hurricane"}


def event_label(event_type: str | None) -> str:
    return _EVENT_NOUNS.get(event_type or "", (event_type or "emergency").replace("_", " "))


def _a(noun: str) -> str:
    return ("an " if noun[:1].lower() in "aeiou" else "a ") + noun


def _actions(event: dict) -> list[str]:
    return list(dict.fromkeys(i["action"] for i in (event.get("facts") or {}).get("emergency_actions", [])
                              if i.get("action")))


def _sources(event: dict) -> list[str]:
    return sorted({s["source_name"] for s in event.get("sources", []) if s.get("source_name")})


def _latest(event: dict) -> str | None:
    times = [s.get("published_at_utc") or s.get("retrieved_at_utc") for s in event.get("sources", [])]
    return max((t for t in times if t), default=None)


# ---------------------------------------------------------------- comparison and trends

def compare_briefings(previous: dict | None, current: dict) -> dict:
    """What changed between two briefings. Events are matched on event_id.
    With no previous briefing, every event is reported as new."""
    prev = {e["event_id"]: e for e in (previous or {}).get("events", [])}
    cur = {e["event_id"]: e for e in current.get("events", [])}
    changes: dict[str, Any] = {
        "previous_run_id": (previous or {}).get("input_run_id"),
        "current_run_id": current.get("input_run_id"),
        "new_events": sorted(cur.keys() - prev.keys()),
        "ended_events": sorted(prev.keys() - cur.keys()),
        "status_changes": [], "new_conflicts": [], "resolved_conflicts": [],
        "figure_changes": [], "newly_important": [],
    }
    for eid in sorted(cur.keys() & prev.keys()):
        before, after = prev[eid], cur[eid]
        if information_status(before) != information_status(after):
            changes["status_changes"].append({"event_id": eid, "from": information_status(before),
                                              "to": information_status(after)})
        had, has = before["conflicts"]["has_conflict"], after["conflicts"]["has_conflict"]
        if has and not had:
            changes["new_conflicts"].append({"event_id": eid, "flags": after["conflicts"]["flags"]})
        if had and not has:
            changes["resolved_conflicts"].append(eid)
        fb, fa = verified_figures(before), verified_figures(after)
        for field in FIGURE_FIELDS:
            if fb[field] != fa[field]:
                changes["figure_changes"].append({"event_id": eid, "field": field,
                                                  "before": fb[field], "after": fa[field]})
        if after["important"]["is_important"] and not before["important"]["is_important"]:
            changes["newly_important"].append({"event_id": eid, "reasons": after["important"]["reasons"]})
    changes["trends"] = _trends(list(prev.values()), list(cur.values())) if previous else []
    changes["has_changes"] = any(changes[k] for k in ("new_events", "ended_events", "status_changes",
                                                      "new_conflicts", "resolved_conflicts",
                                                      "figure_changes", "newly_important"))
    return changes


def _trends(prev: list[dict], cur: list[dict]) -> list[dict]:
    """Active-event counts per event type, before and after."""
    before = Counter(e.get("event_type") or "unknown" for e in prev)
    after = Counter(e.get("event_type") or "unknown" for e in cur)
    out = []
    for etype in sorted(before.keys() | after.keys()):
        b, a = before[etype], after[etype]
        out.append({"event_type": etype, "before": b, "after": a,
                    "direction": "increasing" if a > b else "decreasing" if a < b else "steady"})
    return out


def describe_changes(changes: dict, names: dict[str, str] | None = None, max_listed: int = 5) -> list[str]:
    """One plain sentence per change, for the briefing text. `names` maps event_id to a readable
    name ("weather alert | Bay County, FL"); long lists of new or ended events are shortened."""
    if changes["previous_run_id"] is None:
        return ["First briefing in this series: no earlier report to compare against."]
    n = lambda eid: (names or {}).get(eid, eid)  # noqa: E731

    def listed(ids: list[str], label: str) -> list[str]:
        if len(ids) <= max_listed:
            return [f"{label}: {n(e)}" for e in ids]
        shown = "; ".join(n(e) for e in ids[:max_listed])
        return [f"{len(ids)} {label.lower()}s, including: {shown}"]

    lines = listed(changes["new_events"], "New event") + listed(changes["ended_events"], "Ended event")
    lines += [f"{n(c['event_id'])}: status changed from {c['from']} to {c['to']}" for c in changes["status_changes"]]
    lines += [f"{n(c['event_id'])}: sources now conflict ({', '.join(c['flags'])})" for c in changes["new_conflicts"]]
    lines += [f"{n(e)}: earlier source conflict is resolved" for e in changes["resolved_conflicts"]]
    lines += [f"{n(c['event_id'])}: verified {c['field'].replace('_', ' ')} changed from "
              f"{c['before'] or 'not reported'} to {c['after'] or 'not reported'}" for c in changes["figure_changes"]]
    lines += [f"{n(c['event_id'])}: now a priority ({'; '.join(c['reasons'])})" for c in changes["newly_important"]]
    lines += [f"{event_label(t['event_type'])}: {t['before']} -> {t['after']} active ({t['direction']})"
              for t in changes["trends"] if t["direction"] != "steady"]
    return lines or ["No significant changes since the previous briefing."]


# ---------------------------------------------------------------- situation report

def _event_entry(event: dict, record_locations: dict[str, str] | None = None) -> dict:
    figures = verified_figures(event)
    return {
        "event_id": event["event_id"],
        "event_type": event.get("event_type"),
        "label": event_label(event.get("event_type")),
        "information_status": information_status(event),
        "location": _location(event, record_locations),
        "summary": event.get("summary", ""),
        "summary_source": event.get("summary_source"),
        "verified_figures": figures,
        "not_reported": event.get("not_reported", []),
        "emergency_actions": _actions(event),
        "priority_reasons": event["important"]["reasons"],
        "conflict_details": event["conflicts"].get("details", []),
        "sources": _sources(event),
        "latest_source_time_utc": _latest(event),
        "review_notes": event.get("review_notes", []),
    }


def build_situation_report(current: dict, previous: dict | None = None, batch: dict | None = None,
                           previous_batch: dict | None = None) -> dict:
    """Organize a briefing into a situation report. Events with no confirmed or preliminary records never
    reach this step (briefing.select_input_records drops them), so 'unavailable' lists what the sources
    do not state about the events that are reported.
    Pass the verified `batch` the briefing came from so locations can be shown even when the model
    did not extract any (for example when the LLM call failed and the fallback summary was used);
    `previous_batch` does the same for events that ended since the previous briefing."""
    locs = {r["record_id"]: r.get("location_name") for b in (previous_batch, batch) for r in (b or {}).get("records", [])}
    entries = [_event_entry(e, locs) for e in current.get("events", [])]
    names = {e["event_id"]: f"{event_label(e.get('event_type'))} | {_location(e, locs)}"
             for e in (previous or {}).get("events", [])}
    names.update({e["event_id"]: f"{e['label']} | {e['location']}" for e in entries})
    by_status = {s: [e for e in entries if e["information_status"] == s] for s in STATUS_ORDER}
    gaps = [{"event_id": e["event_id"], "name": names[e["event_id"]], "missing": e["not_reported"]}
            for e in entries if e["not_reported"]]
    changes = compare_briefings(previous, current)
    review = [f"{names[e['event_id']]}: {n}" for e in entries for n in e["review_notes"]]
    return {
        "report_id": f"sitrep_{uuid.uuid4().hex[:10]}",
        "generated_at_utc": _now(),
        "input_run_id": current.get("input_run_id"),
        "briefing_generated_at_utc": current.get("generated_at_utc"),
        "model": current.get("model"),
        "counts": {"events": len(entries), "priority": sum(bool(e["priority_reasons"]) for e in entries),
                   **{s: len(v) for s, v in by_status.items()}},
        "priority_events": [e for e in entries if e["priority_reasons"]],
        "confirmed": by_status["confirmed"],
        "preliminary": by_status["preliminary"],
        "conflicting": by_status["conflicting"],
        "information_gaps": gaps,
        "changes": changes,
        "change_summary": describe_changes(changes, names),
        "review_items": review,
        "requires_human_review": True,
        "disclosure": DISCLOSURE,
    }


# ---------------------------------------------------------------- executive briefing (Markdown template)

def _figures_line(e: dict) -> str:
    parts = []
    for field in FIGURE_FIELDS:
        vals = e["verified_figures"][field]
        label = field.replace("_", " ")
        parts.append(f"{label}: {', '.join(map(str, vals))} (verified)" if vals else f"{label}: not reported")
    return "; ".join(parts)


def _event_md(e: dict, hedge: str = "") -> list[str]:
    lines = [f"- **{e['label']} | {e['location']}**{hedge}",
             f"  - {e['summary']}",
             f"  - Figures: {_figures_line(e)}"]
    if e["emergency_actions"]:
        lines.append(f"  - Public actions in effect: {'; '.join(e['emergency_actions'])}")
    if e["conflict_details"]:
        lines.append(f"  - Conflict: {'; '.join(map(str, e['conflict_details']))}")
    lines.append(f"  - Sources: {', '.join(e['sources']) or 'none listed'} (latest {e['latest_source_time_utc'] or 'n/a'})")
    return lines


def render_executive_briefing(report: dict) -> str:
    """Fixed executive template: bottom line first, then information grouped by how certain it is."""
    c = report["counts"]
    out = [f"# Executive Crisis Briefing",
           f"*Report {report['report_id']} | generated {report['generated_at_utc']} | data run {report['input_run_id']}*",
           "", "> **DRAFT: requires human review before any public use.** " + report["disclosure"], "",
           "## Bottom line",
           f"{c['events']} active event(s): {c['priority']} priority, {c['confirmed']} confirmed, "
           f"{c['preliminary']} preliminary, {c['conflicting']} with conflicting sources.", ""]
    if report["priority_events"]:
        out.append("## Priority events")
        out += [f"- {e['label']} | {e['location']}: {'; '.join(e['priority_reasons'])}"
                for e in report["priority_events"]]
        out.append("")
    out.append("## Changes since last briefing")
    out += [f"- {line}" for line in report["change_summary"]] + [""]
    sections = [("Confirmed information", "confirmed", ""),
                ("Preliminary information (attribute to source; may change)", "preliminary", " _(preliminary)_"),
                ("Conflicting reports (do not state as fact)", "conflicting", " _(sources disagree)_")]
    for title, key, hedge in sections:
        out.append(f"## {title}")
        out += [line for e in report[key] for line in _event_md(e, hedge)] or ["- None."]
        out.append("")
    out.append("## Information not available")
    out += [f"- {g['name']}: {', '.join(m.replace('_', ' ') for m in g['missing'])} not reported by any source"
            for g in report["information_gaps"]] or ["- No gaps recorded."]
    out += ["", "## For the reviewer"]
    out += [f"- {r}" for r in report["review_items"]] or ["- No automatic review flags."]
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------- public statement draft

def draft_public_statement(report: dict, area: str | None = None, include_preliminary: bool = True,
                           max_events: int = 5) -> dict:
    """A plain-language statement for the public, built from a template, not free text.
    Rules: confirmed items are stated with attribution; preliminary items are hedged; conflicting items are
    never stated as fact; casualty and population numbers appear only if verified; otherwise the statement
    says they have not been confirmed. Filter to one area with `area` (matches the location text)."""
    def wanted(e: dict) -> bool:
        return area is None or area.lower() in e["location"].lower()

    # Confirmed before preliminary, priority events first within each; at most max_events are described.
    candidates = [x for x in report["confirmed"] if wanted(x)]
    if include_preliminary:
        candidates += [x for x in report["preliminary"] if wanted(x)]
    candidates.sort(key=lambda e: (e["information_status"] != "confirmed", not e["priority_reasons"]))
    paragraphs = []
    for e in candidates[:max_events]:
        lead = (f"{' and '.join(e['sources'])} confirm" if e["information_status"] == "confirmed"
                else f"Preliminary information from {' and '.join(e['sources'])} indicates")
        paragraphs.append(_statement_lines(e, lead))
    if len(candidates) > max_events:
        paragraphs.append(f"Officials are also monitoring {len(candidates) - max_events} other lower-priority "
                          "alerts and reports.")
    conflicting = [x for x in report["conflicting"] if wanted(x)]
    if conflicting:
        places = ", ".join(dict.fromkeys(e["location"] for e in conflicting))
        paragraphs.append(f"Reports about {places} differ between sources. Officials are verifying the "
                          "details and will share an update when they are confirmed.")
    if not paragraphs:
        paragraphs.append("There is no verified information to share at this time" +
                          (f" for {area}." if area else "."))
    text = "\n\n".join(paragraphs + ["We will provide updates as verified information becomes available."])
    # The "N other alerts" count is computed here, so it is the one number allowed beyond the sources.
    return new_draft("public_statement", text, report, extra_allowed={str(max(0, len(candidates) - max_events))})


def _statement_lines(e: dict, lead: str) -> str:
    where = "near" if e["location"][:1].isdigit() else "affecting"  # USGS style: "3 km ESE of Berkeley, CA"
    lines = [f"{lead} {_a(e['label'])} {where} {e['location']}."]
    for field, label in (("casualties", "casualties"), ("affected_population", "people affected")):
        vals = e["verified_figures"][field]
        if vals:
            lines.append(f"Verified reports state {max(vals)} {label}.")
    if not e["verified_figures"]["casualties"]:
        lines.append("No casualty figures have been confirmed.")
    if e["emergency_actions"]:
        lines.append("Official guidance for the area: " + "; ".join(e["emergency_actions"]) + ".")
    return " ".join(lines)


# ---------------------------------------------------------------- safety checks

def _allowed_numbers(report: dict) -> set[str]:
    """Numbers a draft may contain: anything stated in the report's own verified material."""
    entries = report["confirmed"] + report["preliminary"] + report["conflicting"]
    text = " ".join(f"{e['summary']} {e['location']} {' '.join(e['emergency_actions'])}" for e in entries)
    allowed = _numbers(text) | {str(v) for e in entries for vals in e["verified_figures"].values() for v in vals}
    return allowed | {str(n) for n in report["counts"].values()} | _numbers(str(report.get("input_run_id") or ""))


def check_release_safety(text: str, report: dict, extra_allowed: set[str] | None = None) -> dict:
    """Blocking problems stop release; warnings are shown to the reviewer.
    blocking: a number next to a harm word (killed, injured, missing, displaced, people...) that is not a
              verified casualty/population figure; vague harm estimates ("dozens of people"); any other
              number that does not appear in the verified material.
    warnings: preliminary or conflicting information present; no verified casualty data."""
    verified = {str(v) for e in report["confirmed"] + report["preliminary"] + report["conflicting"]
                for vals in e["verified_figures"].values() for v in vals}
    allowed = _allowed_numbers(report) | set(extra_allowed or ())
    blocking, warnings = [], []
    for sentence in re.split(r"(?<=[.!?])\s+|\n+", text):
        nums = {_canon(n) for n in re.findall(r"\d[\d,]*\.?\d*", sentence)}
        if _HARM.search(sentence):
            for n in sorted(nums - verified):
                blocking.append(f"unverified casualty/population figure {n!r} in: {sentence.strip()!r}")
            nums &= verified
        for n in sorted(nums - allowed):
            blocking.append(f"number {n!r} is not in the verified sources: {sentence.strip()!r}")
        for m in _VAGUE.finditer(sentence):
            blocking.append(f"vague estimate {m.group(0)!r} is not a verified figure")
    if report["counts"]["preliminary"]:
        warnings.append("contains preliminary information; check that it is clearly attributed and hedged")
    if report["counts"]["conflicting"]:
        warnings.append("sources conflict on at least one event; make sure it is not stated as fact")
    if not verified:
        warnings.append("no verified casualty or population figures exist for these events")
    return {"passed": not blocking, "blocking": blocking, "warnings": warnings}


# ---------------------------------------------------------------- human approval workflow

class ReleaseBlocked(Exception):
    """Raised when something tries to release a draft that is not approved, was changed, or is unsafe."""


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def new_draft(kind: str, text: str, report: dict, extra_allowed: set[str] | None = None) -> dict:
    return {
        "draft_id": f"draft_{uuid.uuid4().hex[:10]}",
        "kind": kind,
        "report_id": report["report_id"],
        "input_run_id": report["input_run_id"],
        "created_at_utc": _now(),
        "text": text,
        "content_sha256": _sha(text),
        "safety": check_release_safety(text, report, extra_allowed),
        "state": "pending_review",
        "approved_sha256": None,
        "history": [{"at_utc": _now(), "action": "created", "by": "system", "note": ""}],
        "_report": report,  # kept so edits can be re-checked against the same verified material
        "_extra_allowed": set(extra_allowed or ()),
    }


def _log(draft: dict, action: str, by: str, note: str, log_path: Path | None) -> None:
    entry = {"at_utc": _now(), "action": action, "by": by, "note": note}
    draft["history"].append(entry)
    if log_path:
        append_manifest(Path(log_path), {"draft_id": draft["draft_id"], "kind": draft["kind"],
                                         "content_sha256": draft["content_sha256"], **entry})


def edit_draft(draft: dict, new_text: str, editor: str, log_path: Path | None = None) -> dict:
    """Any edit re-runs the safety checks and cancels an earlier approval."""
    draft.update(text=new_text, content_sha256=_sha(new_text), state="pending_review", approved_sha256=None,
                 safety=check_release_safety(new_text, draft["_report"], draft["_extra_allowed"]))
    _log(draft, "edited", editor, "approval reset", log_path)
    return draft


def approve(draft: dict, reviewer: str, note: str = "", log_path: Path | None = None) -> dict:
    """A named reviewer approves this exact text. Drafts with blocking safety problems cannot be approved."""
    if not reviewer or not reviewer.strip():
        raise ValueError("approval needs the reviewer's name")
    if not draft["safety"]["passed"]:
        raise ReleaseBlocked("fix blocking safety problems first: " + "; ".join(draft["safety"]["blocking"]))
    draft.update(state="approved", approved_sha256=draft["content_sha256"])
    _log(draft, "approved", reviewer.strip(), note, log_path)
    return draft


def reject(draft: dict, reviewer: str, reason: str, log_path: Path | None = None) -> dict:
    if not reason.strip():
        raise ValueError("a rejection needs a reason")
    draft.update(state="rejected", approved_sha256=None)
    _log(draft, "rejected", reviewer, reason, log_path)
    return draft


def release(draft: dict, released_by: str, log_path: Path | None = None) -> dict:
    """The only way out of the system. Re-checks everything at the moment of release."""
    if draft["state"] != "approved":
        raise ReleaseBlocked(f"draft is {draft['state']}, not approved")
    if _sha(draft["text"]) != draft["approved_sha256"]:
        raise ReleaseBlocked("text changed after approval; it must be reviewed again")
    if not check_release_safety(draft["text"], draft["_report"], draft["_extra_allowed"])["passed"]:
        raise ReleaseBlocked("safety checks fail on the approved text")
    draft["state"] = "released"
    _log(draft, "released", released_by, "", log_path)
    return {k: v for k, v in draft.items() if not k.startswith("_")}
