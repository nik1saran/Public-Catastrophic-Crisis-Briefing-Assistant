# Crisis Analysis, Briefing Generation and Safety (Person 3)

Module: `src/crisis/reporting.py` · Tests: `tests/test_reporting.py` · Runner: `tests/run_reporting.py` ·
Demo: `notebooks/briefing-analysis-demo.ipynb`

Covers assignment requirements **#1, applied LLM use case and professional workflow**, and **#4, safety
and responsible-use features**.

## Where it sits in the pipeline

```
Person 1: verified batch ──► Person 2: build_briefing() ──► Person 3: reporting.py ──► Person 4: UI
                                                              │
                                     compare with previous ───┤
                                     situation report ────────┤
                                     executive briefing (MD) ─┤
                                     public statement draft ──┤──► safety check ──► human approval ──► release
```

This step makes **no LLM calls**. It only reorganizes and checks what Person 2's grounded briefing already
contains, so the same input always gives the same report, and every number can be traced back to a source.

## Functions for Person 4 (UI)

| Function | Input | Output |
|---|---|---|
| `build_situation_report(current, previous=None, batch=None, previous_batch=None)` | briefings from `build_briefing()`; optionally the verified batches they came from (adds locations when the model extracted none) | situation report dict (below) |
| `render_executive_briefing(report)` | situation report | Markdown executive briefing |
| `draft_public_statement(report, area=None, include_preliminary=True, max_events=5)` | situation report | draft dict, state `pending_review` |
| `check_release_safety(text, report)` | any text plus the report it came from | `{passed, blocking, warnings}` |
| `edit_draft(draft, new_text, editor)` | draft | re-checked draft; any approval is cancelled |
| `approve(draft, reviewer, note="")` / `reject(draft, reviewer, reason)` | draft | updated draft |
| `release(draft, released_by)` | approved draft | released record, or `ReleaseBlocked` |

All approval functions take an optional `log_path` that appends each action to a JSONL audit log.

## Situation report fields

| Field | Description |
|---|---|
| `report_id`, `generated_at_utc`, `input_run_id`, `briefing_generated_at_utc`, `model` | Identification |
| `counts` | `events`, `priority`, `confirmed`, `preliminary`, `conflicting` |
| `priority_events` | Events with rule-based priority reasons (from Person 2's `important_developments`) |
| `confirmed` / `preliminary` / `conflicting` | Event entries grouped by certainty. **Conflicting overrides confirmed**: a confirmed record that another source contradicts is never shown as settled fact |
| `information_gaps` | Per event: what no source states (`casualties`, `affected_population`) |
| `changes` | Output of `compare_briefings()` (below) |
| `change_summary` | One readable line per change |
| `review_items` | Every review note from the briefing step, with the event name |
| `requires_human_review`, `disclosure` | Always `true`, plus the fixed AI disclosure line |

Each **event entry** has `event_id`, `event_type`, `label`, `information_status`, `location`, `summary`,
`summary_source`, `verified_figures`, `not_reported`, `emergency_actions`, `priority_reasons`,
`conflict_details`, `sources`, `latest_source_time_utc` and `review_notes`.

## Comparing updates (`compare_briefings`)

Events are matched on `event_id`. Reported changes: new events, ended events, status changes
(preliminary → confirmed, or → conflicting), new and resolved source conflicts, changes in **verified**
casualty or population figures, events that became a priority, and per-type trends (active-event counts
before and after, `increasing` / `decreasing` / `steady`).

## Executive briefing template

Bottom line → Priority events → Changes since last briefing → Confirmed → Preliminary (attribute, may
change) → Conflicting (do not state as fact) → Information not available → For the reviewer. The draft
banner and disclosure are always at the top.

## Public statement rules

1. Confirmed events are attributed ("NOAA confirm…"); preliminary events are hedged ("Preliminary
   information from USGS indicates…").
2. Conflicting events are **never** described as fact: "Reports about X differ between sources. Officials
   are verifying the details."
3. Casualty and population numbers appear only if Person 2 verified them (structured field or exact source
   quote). Otherwise the statement says "No casualty figures have been confirmed."
4. Confirmed events come first, then priority events; at most `max_events` are described and the rest are
   counted ("Officials are also monitoring N other lower-priority alerts").
5. The statement is built from a fixed template, not free LLM text, so it cannot introduce new claims.

## Safety checks (`check_release_safety`)

**Blocking (the draft cannot be approved or released):**
- a number in a sentence about harm to people (killed, dead, injured, missing, displaced, evacuated,
  people, residents…) that is not a verified casualty or population figure;
- vague harm estimates such as "dozens of people" or "hundreds of victims";
- any other number that does not appear in the verified material (for example "shelters at 9 locations").

**Warnings (shown to the reviewer):** preliminary information present; sources conflict; no verified
casualty or population figures.

These checks run on the generated draft, again after **every human edit**, and again at release.

## Human approval procedure

```
created ──► pending_review ──approve(reviewer)──► approved ──release()──► released
                 ▲    │                              │
                 │    └──reject(reviewer, reason)──► rejected
                 └──────── edit_draft() ◄────────────┘   (any edit cancels approval)
```

- Approval needs a **named reviewer**; a rejection needs a **reason**.
- Approval records a SHA-256 hash of the exact approved text. `release()` refuses if the text changed after
  approval, if the draft is not approved, or if the safety checks fail at that moment.
- Every action is kept in `draft["history"]` and, with `log_path`, appended to a JSONL audit log.

## Known limitations

- Numbers written as words ("twelve people") are not caught by the casualty check.
- Event matching uses `event_id`. NOAA issues a new alert ID when it updates an alert, so an update can show
  as one ended event plus one new event (seen with the "Montgomery" alert in the Nov 11 snapshots). Matching
  on Person 1's `canonical_event_id` would fix this once it groups NOAA updates.
- The NOAA feed includes a `KEEPALIVE` heartbeat message that currently passes through as an event. It
  should be filtered in Person 1's step.
- Trends are counts of active events per type. With snapshots minutes apart they mostly reflect alert
  turnover, not real-world escalation.
