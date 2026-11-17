# Data Schema Specification

## Project
**Public Catastrophic Crisis Briefing Assistant**

## Purpose
This schema defines the canonical output format for Data Acquisition & Verification. It standardizes ingested and verified crisis records for downstream consumption by AI summarization, crisis analysis, and interface components.

## Design Principles
- Deterministic structure across all records
- Full source attribution and temporal traceability
- Explicit uncertainty and conflict representation
- No inferred critical values without evidence

## Record Entity: `verified_crisis_record`

Each row/object represents one normalized claim snapshot tied to a specific event context.

## Field Definitions

| Field | Type | Required | Allowed Values / Format | Description |
|---|---|---:|---|---|
| `record_id` | string | Yes | UUID or deterministic hash | Unique record identifier |
| `event_id` | string | Yes | Stable event key | Groups records tied to same incident |
| `event_type` | string | Yes | Controlled vocabulary | Hazard category (earthquake, flood, wildfire, cyclone, epidemic, etc.) |
| `source_name` | string | Yes | Free text | Human-readable source identity |
| `source_url` | string | Yes | Valid URL | Link to originating source |
| `source_tier` | string | Yes | `tier_a`, `tier_b`, `tier_c` | Source hierarchy class |
| `source_event_id` | string | No | Source-native event/alert identifier | Enables revision tracking within a source |
| `canonical_event_id` | string | No | Deterministic cross-source grouping key | Used to correlate likely same event across sources |
| `published_at_utc` | string | Yes* | ISO 8601 UTC | Source publication/update timestamp; use `null` only if unavailable |
| `retrieved_at_utc` | string | Yes | ISO 8601 UTC | Time system ingested this record |
| `expires_at_utc` | string | No | ISO 8601 UTC | Expiration/end-of-validity timestamp when provided by source |
| `last_updated_at_utc` | string | No | ISO 8601 UTC | Source revision/update timestamp |
| `location_name` | string | Yes | Free text | Canonical location label |
| `location_geo` | object | No | `{ "lat": number, "lon": number }` | Optional normalized coordinates |
| `country_code` | string | No | ISO-3166-1 alpha-2 | Country code for analytics/filtering |
| `casualties_reported` | integer | No | `>= 0` or `null` | Reported fatalities count |
| `injuries_reported` | integer | No | `>= 0` or `null` | Reported injuries count |
| `missing_reported` | integer | No | `>= 0` or `null` | Reported missing persons |
| `displaced_reported` | integer | No | `>= 0` or `null` | Reported displaced population |
| `affected_population_reported` | integer | No | `>= 0` or `null` | Estimated affected population |
| `infrastructure_impact` | string | No | Free text / controlled tags | Key impact notes |
| `emergency_actions` | array[string] | No | Action strings | Evacuation, shelter activation, alerts, closures, etc. |
| `claim_text` | string | Yes | Free text | Normalized factual claim snippet |
| `status` | string | Yes | `confirmed`, `preliminary`, `unverified` | Verification status |
| `confidence_score` | number | Yes | `0.00` to `1.00` | Deterministic confidence output |
| `conflict_flags` | array[string] | Yes | Flag codes | Contradictions detected for this claim |
| `conflict_details` | array[object] | No | Structured conflict notes | Field-level discrepancy evidence |
| `supersedes_record_id` | string | No | Existing `record_id` | Links new update replacing stale claim |
| `superseded_by_record_id` | string | No | Existing `record_id` | Links stale claim to newer replacement |
| `is_active` | boolean | Yes | `true` / `false` | Whether record should be considered for downstream briefing model input |
| `active_state_reason` | string | No | `active`, `expired`, `superseded`, `stale` | Why record is inactive when excluded |
| `is_superseded` | boolean | Yes | `true` / `false` | Indicates whether this record is outdated |
| `evidence_notes` | string | Yes | Free text | Why this status and score were assigned |
| `source_metadata` | object | No | Source-specific structured fields | Preserves NOAA/FEMA/USGS details used by brief generation |
| `fields_not_reported` | array[string] | No | Names of `*_reported` impact fields | Impact fields the source did not report (value is `null`). Downstream steps must say "not reported" for these, never estimate. |
| `ingestion_run_id` | string | Yes | UUID/hash | Batch/run traceability key |

### `source_metadata` keys (consistent for every record from a given source)

| Source | Keys |
|---|---|
| USGS | `usgs_alert`, `usgs_tsunami`, `usgs_felt`, `usgs_sig`, `usgs_mag` |
| NOAA | `noaa_event`, `noaa_headline`, `noaa_description`, `noaa_instruction`, `noaa_severity`, `noaa_urgency`, `noaa_certainty`, `noaa_response`, `noaa_message_type`, `noaa_ends`, `noaa_status`, `noaa_sender` |
| FEMA | `fema_disaster_number`, `fema_incident_type`, `fema_declaration_title`, `fema_county`, `fema_state` |

Only NOAA alerts with `status = Actual` are ingested; Test, Exercise, Draft and System messages are dropped by the parser. Values may be `null` when the source omits them. Downstream briefing generation should read `active_records` and use `source_metadata` for details such as NOAA `noaa_instruction`.

## Controlled Vocabularies

### `status`
- `confirmed`
- `preliminary`
- `unverified`

### `source_tier`
- `tier_a` — authoritative
- `tier_b` — institutional
- `tier_c` — signal-only

### `event_type` (minimum set)
- `earthquake`
- `flood`
- `wildfire`
- `storm`
- `hurricane`
- `landslide`
- `epidemic`
- `industrial_accident`
- `conflict_related_humanitarian`
- `other`

### `conflict_flags` (recommended codes)
- `conflict_casualties`
- `conflict_injuries`
- `conflict_displaced`
- `conflict_location`
- `conflict_timing`
- `conflict_severity`
- `conflict_action_status`

## Validation Rules

1. `source_name`, `source_url`, `retrieved_at_utc`, `status`, and `evidence_notes` are mandatory.
2. `status=confirmed` is invalid when:
   - `source_tier=tier_c`, or
   - `conflict_flags` contains unresolved high-impact conflicts.
3. Numeric impact fields must be non-negative integers or `null` (no negative, no strings).
4. Timestamps must be ISO 8601 UTC.
5. Unknown critical values must remain explicit (`null`), never guessed.
6. `is_superseded=true` requires either `supersedes_record_id` linkage in newer record lineage or explicit archival reason.

## JSON Shape (Reference)

```json
{
  "record_id": "b2df2c9f-85f2-4d41-94ef-f3d963bfc86d",
  "event_id": "event_2026_11_earthquake_001",
  "event_type": "earthquake",
  "source_name": "USGS",
  "source_url": "https://example.org/event/123",
  "source_tier": "tier_a",
  "published_at_utc": "2026-11-06T21:58:00Z",
  "retrieved_at_utc": "2026-11-06T22:00:15Z",
  "location_name": "Offshore Northern California",
  "location_geo": { "lat": 40.12, "lon": -124.35 },
  "country_code": "US",
  "casualties_reported": null,
  "injuries_reported": null,
  "missing_reported": null,
  "displaced_reported": null,
  "affected_population_reported": null,
  "infrastructure_impact": "No confirmed structural damage at retrieval time.",
  "emergency_actions": ["tsunami_assessment_in_progress"],
  "claim_text": "Magnitude 6.8 earthquake detected offshore Northern California.",
  "status": "preliminary",
  "confidence_score": 0.82,
  "conflict_flags": [],
  "conflict_details": [],
  "supersedes_record_id": null,
  "is_superseded": false,
  "evidence_notes": "Tier A source with fresh timestamp; impact fields pending corroboration.",
  "ingestion_run_id": "run_20261106_2200z"
}
```

## Output Containers

### Batch Envelope: `verified_crisis_batch`

```json
{
  "ingestion_run_id": "run_20261106_2200z",
  "generated_at_utc": "2026-11-06T22:00:30Z",
  "records": [],
  "active_records": []
}
```

Required batch metadata:
- `ingestion_run_id`
- `generated_at_utc`
- `records` (array of `verified_crisis_record`)
- `active_records` (subset of `records` filtered for downstream briefing generation)

## Compatibility Contract
Downstream components can assume:
- Stable required fields remain present in every record
- Status semantics are fixed (`confirmed`, `preliminary`, `unverified`)
- Conflicts are explicit and not silently resolved
- Evidence and provenance are available per claim

## Machine-Enforced Schema Artifact
Canonical schema file:
- `schemas/verified-crisis-batch.schema.json`

Validation entrypoint:
- `python .\tests\validate_schema.py`

## Briefing Output (LLM layer)

The briefing module (`src/crisis/briefing.py`) reads a `verified_crisis_batch` and produces the structures
below. It only uses records that are active, current, not superseded, `confirmed` or `preliminary`, and
(for NOAA) have `status = Actual`. Every output requires human review before public release.

### Briefing: `build_briefing(batch, llm)`
| Field | Type | Description |
|---|---|---|
| `generated_at_utc` | string | Time the briefing was generated |
| `input_run_id` | string | `ingestion_run_id` of the batch used |
| `model` | string | Model that produced the text |
| `requires_human_review` | boolean | Always `true` |
| `disclosure` | string | Fixed line: AI-generated from verified sources; a person must review before release |
| `events` | array | One item per event (below), important events first |

### Event item
| Field | Type | Description |
|---|---|---|
| `event_id`, `event_type` | string | From the verified records |
| `status` | string | `preliminary` if any record is preliminary, otherwise `confirmed` |
| `summary` | string | At most 3 sentences, grounded in the records |
| `summary_source` | string | `llm`, or `fallback` if the model's text failed the checks and source text is shown |
| `cited_record_ids` | array[string] | Records the summary relies on |
| `facts` | object | `casualties`, `affected_population`, `locations`, `emergency_actions`. Each item has `record_id`, `quote` (verbatim from the source) and `value` / `name` / `action` |
| `not_reported` | array[string] | `casualties` and/or `affected_population` when no source states them |
| `rejected_facts` | array | Facts the model returned that were not found in the source text |
| `important` | object | `is_important` (boolean) and `reasons` (array[string]) from fixed rules |
| `conflicts` | object | `has_conflict`, `flags`, `details`, taken from the verification step |
| `sources` | array | `record_id`, `source_name`, `source_url`, `published_at_utc`, `retrieved_at_utc` |
| `llm_error` | string/null | Why the model output was not used, or an API error |
| `requires_human_review` | boolean | Always `true` |
| `disclosure` | string | As above |
| `review_notes` | array[string] | What needs a closer look: fallback used, rejected facts, preliminary information, conflicting sources |

### Answer: `answer_question(batch, question, llm, template=None)`
| Field | Type | Description |
|---|---|---|
| `question`, `prompt_version` | string | The question asked and the instruction version used |
| `answer` | string | The grounded answer, or a message that a verified answer could not be generated |
| `answerable` | boolean | `false` when the verified records do not contain the answer |
| `answer_source` | string | `llm`, `fallback`, or `none` (no usable records, model not called) |
| `information_gaps` | array[string] | What the question asks about that the sources do not state |
| `cited_record_ids`, `facts`, `rejected_facts`, `llm_error` | | As in the event item |
| `sources` | array | As in the event item, plus `status`. Absent when `answer_source` is `none` |
| `conflicts`, `requires_human_review`, `disclosure`, `review_notes` | | As in the event item |

### Guarantees
- Numbers and place names in `summary` / `answer` appear in the source records provided to the model; otherwise the text is replaced. Each extracted fact's `quote` appears in the record it cites.
- Casualty and population figures come only from structured source fields or verified quotes. If none exist, they appear in `not_reported`, never as an estimate.
