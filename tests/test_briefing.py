"""Offline tests for the briefing module (fake LLM, no API key needed).

Run: python -m unittest tests.test_briefing
"""
import unittest
from datetime import datetime, timezone
from unittest import mock

from src.crisis.briefing import (DISCLOSURE, answer_question, build_briefing, check_consistency, get_llm,
                                 load_env, select_input_records, ungrounded_places)
from src.crisis.prompts import PROMPT_VERSIONS, TEMPLATES
from tests.run_prompt_experiment import evaluate


NOW = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def rec(rid, event="e1", status="preliminary", **kw):
    base = dict(record_id=rid, event_id=event, event_type="storm", status=status, is_active=True,
                is_superseded=False, source_name="NOAA", source_url="u", retrieved_at_utc=NOW,
                published_at_utc=NOW, location_name="Bay County, FL", conflict_flags=[],
                claim_text="NOAA alert 'Flood Warning' for Bay County, FL.",
                source_metadata={"noaa_instruction": "Move to higher ground immediately.",
                                 "noaa_description": "Flooding expected. 2,500 residents may be affected.",
                                 "noaa_severity": "Severe"})
    base.update(kw)
    return base


GOOD = {
    "casualties": [],
    "affected_population": [{"record_id": "a", "value": 2500, "quote": "2,500 residents may be affected"}],
    "locations": [{"record_id": "a", "name": "Bay County, FL"}],
    "emergency_actions": [{"record_id": "a", "action": "Move to higher ground", "quote": "Move to higher ground immediately."}],
    "summary": "Officials report flooding in Bay County, FL, affecting up to 2,500 residents.",
    "cited_record_ids": ["a"],
}


def example_backend():
    """Shape of a custom backend factory: returns fn(system, user, json_schema) -> dict."""
    return lambda system, user, schema: GOOD


NO_ANSWER = {**GOOD, "casualties": [], "affected_population": [], "locations": [], "emergency_actions": [],
             "summary": "The verified sources provided do not report this.", "answerable": False,
             "information_gaps": ["number of casualties"], "cited_record_ids": []}


class RealRunFixTests(unittest.TestCase):
    """Problems found in the first real run with live data."""

    def test_max_events_keeps_important_events(self):
        quake = rec("q", event="quake", source_name="USGS", location_name="Salamatof, Alaska",
                    claim_text="USGS reports earthquake magnitude 1 near Salamatof, Alaska.",
                    source_metadata={"usgs_mag": 1})
        alert = rec("a", event="alert")
        events = build_briefing({"records": [quake, alert]}, lambda s, u, sc: GOOD, max_events=1)["events"]
        self.assertEqual([e["event_id"] for e in events], ["alert"])

    def test_number_reworded_as_decimal_is_accepted(self):
        quake = rec("a", source_name="USGS", location_name="Salamatof, Alaska",
                    claim_text="USGS reports earthquake magnitude 1 near Salamatof, Alaska.", source_metadata={})
        reply = {**GOOD, "summary": "USGS reports a preliminary magnitude 1.0 earthquake near Salamatof, Alaska."}
        res = answer_question({"records": [quake]}, "What happened?", lambda s, u, sc: reply)
        self.assertEqual(res["answer_source"], "llm")

    def test_failure_reason_is_reported(self):
        bad = {**GOOD, "summary": "Flooding killed 40 people in Bay County."}
        res = answer_question({"records": [rec("a")]}, "What happened?", lambda s, u, sc: bad)
        self.assertIn("numbers not in sources", res["llm_error"])
        bad = {**GOOD, "summary": "Officials report flooding in Miami."}
        res = answer_question({"records": [rec("a")]}, "What happened?", lambda s, u, sc: bad)
        self.assertIn("Miami", res["llm_error"])

    def test_failed_question_is_not_answered_with_unrelated_claims(self):
        bad = {**GOOD, "summary": "Flooding killed 40 people in Bay County."}
        res = answer_question({"records": [rec("a")]}, "What happened?", lambda s, u, sc: bad)
        self.assertNotIn("Flood Warning", res["answer"])
        self.assertIn("could not be generated", res["answer"])


class PlaceCheckFalseRejectionTests(unittest.TestCase):
    """Wordings the real model used that the first version of the place check wrongly rejected."""

    def setUp(self):
        self.records = [rec("a", claim_text="NOAA alert 'Small Craft Advisory' for Pittsburgh area, Bay County, FL.",
                            location_name="Bay County, FL")]

    def test_plural_hyphenated_pronoun_and_state_name_are_accepted(self):
        for text in ("Officials note Advisories apply in Bay County.",     # plural of Advisory
                     "Officials note the Pittsburgh-area alert.",          # hyphenated word
                     "Officials say that I should note Bay County.",       # pronoun
                     "Officials note flooding in Florida.",                # FL -> Florida
                     "Officials note data published 2026-11-17T21:35:30Z.",    # timestamp letters T and Z
                     "Officials note the alert was issued at 21:35 UTC."):     # model writes UTC for Z
            self.assertEqual(ungrounded_places(text, self.records), [], text)

    def test_invented_names_are_still_caught(self):
        self.assertEqual(ungrounded_places("Officials note flooding in Texas.", self.records), ["Texas"])
        self.assertEqual(ungrounded_places("Officials note the Denver-area alert.", self.records), ["Denver"])
        self.assertEqual(ungrounded_places("Officials note Mayor Smith's order.", self.records), ["Mayor", "Smith"])


class ConflictAndTestAlertTests(unittest.TestCase):
    def test_conflicts_are_shown_and_flagged_for_review(self):
        a = rec("a", conflict_flags=["conflict_casualties"],
                conflict_details=[{"field": "casualties_reported", "values_seen": [2, 5]}])
        item = build_briefing({"records": [a]}, lambda s, u, sc: GOOD)["events"][0]
        self.assertTrue(item["conflicts"]["has_conflict"])
        self.assertEqual(item["conflicts"]["flags"], ["conflict_casualties"])
        self.assertIn("sources conflict: conflict_casualties", item["review_notes"])
        clean = build_briefing({"records": [rec("a")]}, lambda s, u, sc: GOOD)["events"][0]
        self.assertFalse(clean["conflicts"]["has_conflict"])

    def test_answers_report_conflicts_of_cited_records(self):
        a = rec("a", conflict_flags=["conflict_location"], conflict_details=[])
        res = answer_question({"records": [a]}, "Where?", lambda s, u, sc: GOOD)
        self.assertEqual(res["conflicts"]["flags"], ["conflict_location"])

    def test_noaa_test_and_exercise_alerts_are_excluded(self):
        test = rec("t", source_metadata={"noaa_status": "Test", "noaa_event": "Test Message"})
        drill = rec("d", source_metadata={"noaa_status": "Exercise"})
        real = rec("r", source_metadata={"noaa_status": "Actual"})
        old_file = rec("o")  # older data without noaa_status is still accepted
        ids = [r["record_id"] for r in select_input_records({"records": [test, drill, real, old_file]})]
        self.assertEqual(ids, ["r", "o"])

    def test_parser_drops_non_actual_noaa_alerts(self):
        from src.crisis.live_evidence import LIVE_SOURCES, _parse_noaa
        feats = [{"id": f"id{i}", "properties": {"status": st, "event": ev, "areaDesc": "X", "sent": NOW}}
                 for i, (st, ev) in enumerate([("Test", "Test Message"), ("Actual", "Flood Warning"),
                                               ("Exercise", "Drill"), ("Draft", "Draft")])]
        noaa = next(s for s in LIVE_SOURCES if s.source_id == "noaa_alerts")
        out = _parse_noaa("run", noaa, NOW, {"features": feats})
        self.assertEqual([r["source_metadata"]["noaa_event"] for r in out], ["Flood Warning"])
        self.assertEqual(out[0]["source_metadata"]["noaa_status"], "Actual")


class SafetyFeatureTests(unittest.TestCase):
    def test_invented_place_in_answer_falls_back(self):
        bad = {**GOOD, "summary": "Officials report flooding in Miami and Bay County, FL."}
        res = answer_question({"records": [rec("a")]}, "Where?", lambda s, u, sc: bad)
        self.assertEqual(res["answer_source"], "fallback")
        self.assertTrue(res["requires_human_review"])
        self.assertIn("model output failed grounding checks; showing source text", res["review_notes"])

    def test_place_check_ignores_sentence_start_and_finds_unknown_names(self):
        records = [rec("a")]
        self.assertEqual(ungrounded_places("Flooding hit Bay County, FL.", records), [])
        self.assertEqual(ungrounded_places("Flooding hit Miami and Bay County.", records), ["Miami"])

    def test_every_output_carries_review_flag_and_disclosure(self):
        batch = {"records": [rec("a")]}
        briefing = build_briefing(batch, lambda s, u, sc: GOOD)
        self.assertTrue(briefing["requires_human_review"])
        self.assertEqual(briefing["disclosure"], DISCLOSURE)
        event = briefing["events"][0]
        self.assertTrue(event["requires_human_review"])
        self.assertIn("contains preliminary information", event["review_notes"])
        empty = answer_question({"records": []}, "Anything?", lambda s, u, sc: GOOD)
        self.assertTrue(empty["requires_human_review"])
        self.assertEqual(empty["disclosure"], DISCLOSURE)

    def test_consistency_detects_stable_and_flapping_models(self):
        batch = {"records": [rec("a")]}
        stable = check_consistency(batch, "Who is affected?", lambda s, u, sc: GOOD, runs=3)
        self.assertTrue(stable["consistent"])
        self.assertEqual(stable["text_similarity"], 1.0)
        replies = iter([GOOD, NO_ANSWER, GOOD])
        flapping = check_consistency(batch, "Who is affected?", lambda s, u, sc: next(replies), runs=3)
        self.assertFalse(flapping["consistent"])
        self.assertFalse(flapping["answerable_agree"])


class EnvTests(unittest.TestCase):
    def test_load_env_sets_missing_and_keeps_existing(self):
        import os, tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / ".env"
            f.write_text('# c\nCRISIS_TEST_NEW="abc"\nCRISIS_TEST_KEPT=from_file\nCRISIS_TEST_EMPTY=\n', encoding="utf-8")
            with mock.patch.dict(os.environ, {"CRISIS_TEST_KEPT": "from_env"}):
                load_env(f)
                self.assertEqual(os.environ["CRISIS_TEST_NEW"], "abc")
                self.assertEqual(os.environ["CRISIS_TEST_KEPT"], "from_env")
                self.assertNotIn("CRISIS_TEST_EMPTY", os.environ)
                os.environ.pop("CRISIS_TEST_NEW")


class QuestionTests(unittest.TestCase):
    def test_unanswerable_question_reports_gap(self):
        res = answer_question({"records": [rec("a")]}, "", lambda s, u, sc: NO_ANSWER, template="casualties")
        self.assertFalse(res["answerable"])
        self.assertEqual(res["information_gaps"], ["number of casualties"])
        self.assertEqual(res["question"], TEMPLATES["casualties"])

    def test_no_safe_records_means_no_llm_call(self):
        def must_not_run(*a):
            raise AssertionError("LLM called with no verified records")
        res = answer_question({"records": [rec("a", status="unverified")]}, "Anything?", must_not_run)
        self.assertFalse(res["answerable"])
        self.assertEqual(res["answer_source"], "none")

    def test_answer_is_grounded_and_cites_sources(self):
        res = answer_question({"records": [rec("a")]}, "Who is affected?", lambda s, u, sc: GOOD)
        self.assertTrue(res["answerable"])
        self.assertEqual([s["record_id"] for s in res["sources"]], ["a"])

    def test_area_template_and_version_reach_prompt(self):
        seen = {}
        def spy(system, user, schema):
            seen.update(system=system, user=user)
            return GOOD
        answer_question({"records": [rec("a")]}, "", spy, template="area_actions", version="v1", area="Bay County")
        self.assertIn("Bay County", seen["user"])
        self.assertEqual(seen["system"], PROMPT_VERSIONS["v1"])
        self.assertIn("<record id=\"a\"", seen["user"])

    def test_experiment_metrics(self):
        qs = [{"question": "Who is affected?", "expect_answerable": True},
              {"template": "casualties", "expect_answerable": False}]
        llm = lambda s, u, sc: NO_ANSWER if "casualties" in u else GOOD
        out = evaluate({"records": [rec("a")]}, qs, llm, ["v1", "v2"])
        self.assertEqual(out["v2"]["answerable_match"], 1.0)
        self.assertEqual(out["v2"]["fallback_rate"], 0.0)


class BriefingTests(unittest.TestCase):
    def test_custom_backend_is_loaded_by_spec(self):
        llm = get_llm("tests.test_briefing:example_backend")
        item = build_briefing({"records": [rec("a")]}, llm)["events"][0]
        self.assertEqual(item["summary_source"], "llm")

    def test_anthropic_spec_sets_model(self):
        with mock.patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}):
            self.assertEqual(get_llm("anthropic:claude-haiku-5-5").model, "claude-haiku-5-5")

    def test_only_safe_records_reach_llm(self):
        batch = {"records": [rec("ok"), rec("old", is_active=False), rec("unv", status="unverified"),
                             rec("sup", is_superseded=True)]}
        self.assertEqual([r["record_id"] for r in select_input_records(batch)], ["ok"])

    def test_old_or_expired_records_are_dropped_even_if_flagged_active(self):
        batch = {"records": [rec("ok"),
                             rec("old", published_at_utc="2026-01-01T00:00:00Z"),
                             rec("exp", expires_at_utc="2026-01-01T00:00:00Z")]}
        self.assertEqual([r["record_id"] for r in select_input_records(batch)], ["ok"])

    def test_grounded_output_is_kept(self):
        item = build_briefing({"records": [rec("a")]}, lambda s, u, sc: GOOD)["events"][0]
        self.assertEqual(item["summary_source"], "llm")
        self.assertEqual(item["facts"]["affected_population"][0]["value"], 2500)
        self.assertEqual(item["facts"]["emergency_actions"][0]["action"], "Move to higher ground")
        self.assertEqual(item["not_reported"], ["casualties"])
        self.assertTrue(item["important"]["is_important"])
        self.assertIsNone(item["llm_error"])

    def test_invented_facts_are_rejected(self):
        bad = {**GOOD,
               "casualties": [{"record_id": "a", "value": 12, "quote": "12 people died"}],
               "emergency_actions": [{"record_id": "a", "action": "Evacuate", "quote": "Evacuate the city now"}]}
        item = build_briefing({"records": [rec("a")]}, lambda s, u, sc: bad)["events"][0]
        self.assertEqual(item["facts"]["casualties"], [])
        self.assertEqual(item["facts"]["emergency_actions"], [])
        self.assertEqual(len(item["rejected_facts"]), 2)

    def test_summary_with_invented_number_falls_back(self):
        bad = {**GOOD, "summary": "Flooding killed 40 people in Bay County."}
        item = build_briefing({"records": [rec("a")]}, lambda s, u, sc: bad)["events"][0]
        self.assertEqual(item["summary_source"], "fallback")
        self.assertIn("Flood Warning", item["summary"])

    def test_llm_failure_does_not_block_briefing(self):
        def boom(*a):
            raise RuntimeError("api down")
        item = build_briefing({"records": [rec("a")]}, boom)["events"][0]
        self.assertEqual(item["summary_source"], "fallback")
        self.assertIn("api down", item["llm_error"])

    def test_structured_casualties_used_when_present(self):
        item = build_briefing({"records": [rec("a", casualties_reported=3)]}, lambda s, u, sc: GOOD)["events"][0]
        self.assertEqual(item["facts"]["casualties"][0]["value"], 3)
        self.assertNotIn("casualties", item["not_reported"])


if __name__ == "__main__":
    unittest.main()
