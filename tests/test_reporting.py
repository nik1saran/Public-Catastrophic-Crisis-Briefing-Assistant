"""Offline tests for Person 3: analysis, briefing generation, safety and human approval (no API key needed).

Run: python -m unittest tests.test_reporting
"""
import json
import tempfile
import unittest
from pathlib import Path

from src.crisis.briefing import build_briefing
from src.crisis.reporting import (ReleaseBlocked, approve, build_situation_report, check_release_safety,
                                  compare_briefings, describe_changes, draft_public_statement, edit_draft, information_status,
                                  reject, release, render_executive_briefing)
from tests.test_briefing import GOOD, rec

FAKE = lambda s, u, sc: GOOD  # noqa: E731  grounded reply for the Bay County flood record "a"


def quake(rid="q", **kw):
    return rec(rid, event="quake", event_type="earthquake", source_name="USGS", location_name="Salamatof, Alaska",
               claim_text="USGS reports earthquake magnitude 6.1 near Salamatof, Alaska.",
               source_metadata={"usgs_mag": 6.1}, **kw)


def briefing(*records):
    return {**build_briefing({"records": list(records), "ingestion_run_id": "run_x"}, FAKE), "input_run_id": "run_x"}


PREVIOUS = briefing(rec("a"))
CURRENT = briefing(rec("a", status="confirmed"), quake())


class ComparisonTests(unittest.TestCase):
    def test_first_briefing_has_no_previous(self):
        ch = compare_briefings(None, PREVIOUS)
        self.assertEqual(ch["new_events"], ["e1"])
        self.assertEqual(ch["trends"], [])

    def test_detects_new_events_status_changes_and_trends(self):
        ch = compare_briefings(PREVIOUS, CURRENT)
        self.assertEqual(ch["new_events"], ["quake"])
        self.assertEqual(ch["status_changes"], [{"event_id": "e1", "from": "preliminary", "to": "confirmed"}])
        quake_trend = next(t for t in ch["trends"] if t["event_type"] == "earthquake")
        self.assertEqual(quake_trend["direction"], "increasing")
        self.assertTrue(ch["has_changes"])

    def test_ended_events_and_new_conflicts(self):
        conflicted = briefing(rec("a", conflict_flags=["casualty_mismatch"], conflict_details=["NOAA vs FEMA"]))
        ch = compare_briefings(CURRENT, conflicted)
        self.assertEqual(ch["ended_events"], ["quake"])
        self.assertEqual(ch["new_conflicts"][0]["flags"], ["casualty_mismatch"])
        self.assertEqual(ch["status_changes"][0]["to"], "conflicting")

    def test_figure_changes_use_verified_numbers_only(self):
        later = briefing(rec("a", casualties_reported=3))
        ch = compare_briefings(PREVIOUS, later)
        self.assertEqual(ch["figure_changes"], [{"event_id": "e1", "field": "casualties", "before": [], "after": [3]}])

    def test_long_change_lists_are_shortened(self):
        many = briefing(*[rec(f"r{i}", event=f"ev{i}") for i in range(8)])
        lines = describe_changes(compare_briefings(PREVIOUS, many), max_listed=3)
        self.assertTrue(any(line.startswith("8 new events, including:") for line in lines))

    def test_no_changes(self):
        self.assertFalse(compare_briefings(PREVIOUS, PREVIOUS)["has_changes"])


class SituationReportTests(unittest.TestCase):
    def setUp(self):
        self.report = build_situation_report(CURRENT, PREVIOUS)

    def test_groups_by_certainty(self):
        self.assertEqual([e["event_id"] for e in self.report["confirmed"]], ["e1"])
        self.assertEqual([e["event_id"] for e in self.report["preliminary"]], ["quake"])
        self.assertEqual(self.report["counts"]["events"], 2)

    def test_conflict_overrides_confirmed(self):
        event = briefing(rec("a", status="confirmed", conflict_flags=["x"]))["events"][0]
        self.assertEqual(information_status(event), "conflicting")

    def test_gaps_and_priorities(self):
        gaps = {g["event_id"]: g["missing"] for g in self.report["information_gaps"]}
        self.assertIn("casualties", gaps["e1"])
        self.assertIn("significant earthquake", self.report["priority_events"][0]["priority_reasons"]
                      + self.report["priority_events"][1]["priority_reasons"])

    def test_always_requires_review(self):
        self.assertTrue(self.report["requires_human_review"])

    def test_executive_markdown_sections(self):
        md = render_executive_briefing(self.report)
        for heading in ("## Bottom line", "## Changes since last briefing", "## Confirmed information",
                        "## Preliminary information", "## Conflicting reports", "## Information not available",
                        "requires human review"):
            self.assertIn(heading, md)
        self.assertIn("weather alert | Bay County, FL: status changed from preliminary to confirmed", md)
        self.assertIn("casualties: not reported", md)

    def test_empty_briefing(self):
        report = build_situation_report(briefing())
        self.assertEqual(report["counts"]["events"], 0)
        self.assertIn("None.", render_executive_briefing(report))


class PublicStatementTests(unittest.TestCase):
    def setUp(self):
        self.report = build_situation_report(CURRENT, PREVIOUS)

    def test_statement_is_hedged_and_safe(self):
        d = draft_public_statement(self.report)
        self.assertIn("NOAA confirm", d["text"])
        self.assertIn("Preliminary information from USGS indicates", d["text"])
        self.assertIn("No casualty figures have been confirmed.", d["text"])
        self.assertTrue(d["safety"]["passed"], d["safety"])
        self.assertEqual(d["state"], "pending_review")

    def test_area_filter(self):
        d = draft_public_statement(self.report, area="Bay County")
        self.assertNotIn("Salamatof", d["text"])
        d = draft_public_statement(self.report, area="Nowhere")
        self.assertIn("no verified information", d["text"])

    def test_overflow_count_passes_safety(self):
        many = build_situation_report(briefing(*[rec(f"r{i}", event=f"ev{i}") for i in range(12)]))
        d = draft_public_statement(many, max_events=2)
        self.assertIn("monitoring 10 other", d["text"])
        self.assertTrue(d["safety"]["passed"], d["safety"])

    def test_conflicting_events_not_stated_as_fact(self):
        report = build_situation_report(briefing(rec("a", status="confirmed", conflict_flags=["x"])))
        text = draft_public_statement(report)["text"]
        self.assertIn("differ between sources", text)
        self.assertNotIn("confirm a", text)

    def test_verified_figures_are_included(self):
        report = build_situation_report(briefing(rec("a", casualties_reported=3)))
        d = draft_public_statement(report)
        self.assertIn("Verified reports state 3 casualties.", d["text"])
        self.assertTrue(d["safety"]["passed"], d["safety"])


class SafetyTests(unittest.TestCase):
    def setUp(self):
        self.report = build_situation_report(CURRENT)

    def test_blocks_invented_casualties(self):
        res = check_release_safety("Flooding has killed 12 people in Bay County.", self.report)
        self.assertFalse(res["passed"])
        self.assertIn("'12'", res["blocking"][0])

    def test_blocks_vague_estimates(self):
        self.assertFalse(check_release_safety("Dozens of people are injured.", self.report)["passed"])

    def test_blocks_other_unsourced_numbers(self):
        self.assertFalse(check_release_safety("Shelters open at 9 locations.", self.report)["passed"])

    def test_allows_verified_figure(self):
        report = build_situation_report(briefing(rec("a", casualties_reported=3)))
        self.assertTrue(check_release_safety("Officials report 3 casualties.", report)["passed"])

    def test_warns_on_preliminary(self):
        self.assertTrue(any("preliminary" in w for w in check_release_safety("Stay safe.", self.report)["warnings"]))


class ApprovalTests(unittest.TestCase):
    def setUp(self):
        self.draft = draft_public_statement(build_situation_report(CURRENT))

    def test_cannot_release_without_approval(self):
        with self.assertRaises(ReleaseBlocked):
            release(self.draft, "ops")

    def test_approve_then_release(self):
        approve(self.draft, "J. Reviewer", "checked against sources")
        out = release(self.draft, "ops")
        self.assertEqual(out["state"], "released")
        self.assertEqual([h["action"] for h in out["history"]], ["created", "approved", "released"])
        self.assertNotIn("_report", out)

    def test_edit_after_approval_resets(self):
        approve(self.draft, "J. Reviewer")
        edit_draft(self.draft, self.draft["text"] + " Stay indoors.", "editor")
        self.assertEqual(self.draft["state"], "pending_review")
        with self.assertRaises(ReleaseBlocked):
            release(self.draft, "ops")

    def test_tampering_after_approval_is_caught(self):
        approve(self.draft, "J. Reviewer")
        self.draft["text"] += " Changed."
        with self.assertRaises(ReleaseBlocked):
            release(self.draft, "ops")

    def test_unsafe_draft_cannot_be_approved(self):
        edit_draft(self.draft, "Flooding has killed 12 people.", "editor")
        with self.assertRaises(ReleaseBlocked):
            approve(self.draft, "J. Reviewer")

    def test_reviewer_name_and_reject_reason_required(self):
        with self.assertRaises(ValueError):
            approve(self.draft, " ")
        with self.assertRaises(ValueError):
            reject(self.draft, "J. Reviewer", "")
        reject(self.draft, "J. Reviewer", "tone too alarming")
        with self.assertRaises(ReleaseBlocked):
            release(self.draft, "ops")

    def test_audit_log_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "approvals.jsonl"
            approve(self.draft, "J. Reviewer", log_path=log)
            release(self.draft, "ops", log_path=log)
            actions = [json.loads(line)["action"] for line in log.read_text().splitlines()]
            self.assertEqual(actions, ["approved", "released"])


if __name__ == "__main__":
    unittest.main()
