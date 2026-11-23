# Public-Catastropic-Crisis-Briefing-Assistant

 **AAI-530 Final Team Project · Group 6 · University of San Diego · MS in Applied Artificial Intelligence**


## Project overview
We aim to build an assistant that will help executives brief the public on information regarding public catastrophies. This assistant will achieve this by relying on official communications and real time information only to generate briefings for exeutives. The information gathered and sent to our assistant will be form reliable sources to ensure that misinformation is not sent out to the public, with the goal of helping create faster responses during high pressure situations. 


## Team contributions

| Team member | Responsibility | Contributions |
|---|---|---|
| Nikita Saran | Data acquisition and verification | Source integration, retrieval of real time data, validation of source credibility |
| Jasmine Duong | LLM integration, prompt engineering, and briefing generation | Integrate LLM through API, develop system instructions and prompts to create briefings |
| Reema Eid | Crisis analysis, briefing generation and safety | Generate situation reports, compare current updates against previous updates, implement safeguards |
| Glen Salazar | UI, testing and documentation | Build chat interface or dashboard, display crisis information, develop testing framework and evaluate responses | 

## Repository Structure

```
Public-Catastrophic-Crisis-Briefing-Assistant/
├── README.md
├── requirements.txt                      # anthropic SDK (briefing module) + ipykernel (notebooks)
├── .gitignore                            # excludes .env (API key)
├── .github/
│   └── workflows/
│       └── validation.yml                # CI: pipeline validation + briefing unit tests
│
├── src/
│   └── crisis/
│       ├── __init__.py
│       ├── config.py                     # settings and paths
│       ├── source_catalog.py             # USGS / NOAA / FEMA source definitions
│       ├── connectors.py                 # API connections to the live sources
│       ├── live_evidence.py              # live fetch + parsers -> input claims
│       ├── normalization.py              # claims -> common record format
│       ├── verification.py               # scoring, conflicts, confirmed/preliminary/unverified
│       ├── pipeline.py                   # end-to-end data pipeline -> validated output
│       ├── audit.py                      # run manifests and logging
│       ├── briefing.py                   # LLM layer: filtering, grounding checks, briefings, Q&A
│       └── prompts.py                    # system instructions (v1-v5) and question templates
│
├── schemas/
│   └── verified-crisis-batch.schema.json # schema for validated pipeline output
│
├── data/
│   ├── sample-validated-output.json      # small sample for demos and tests
│   ├── live/                             # live pulls: input claims, validated output, raw API responses
│   │   └── raw/<run_id>/                 # raw USGS / NOAA / FEMA responses per run
│   ├── briefings/                        # generated briefings
│   └── prompt_experiments/               # prompt-version comparison results
│
├── notebooks/
│   ├── validation-walkthrough.ipynb      # walks through the data pipeline
│   └── briefing-demo.ipynb               # generates briefings, Q&A, consistency check, prompt comparison
│
├── tests/
│   ├── fixtures/
│   │   └── input-claims.json             # test input data
│   ├── test_briefing.py                  # briefing unit tests (fake LLM, no API key needed)
│   ├── run_validation.py                 # run the pipeline validation tests
│   ├── validate_schema.py                # check output against the JSON schema
│   ├── run_live_evidence.py              # one-command live evidence bundle
│   ├── run_briefing.py                   # generate a briefing from the command line
│   ├── run_prompt_experiment.py          # compare prompt versions
│   ├── prompt_questions.json             # question set for the prompt comparison
│   ├── test-matrix.md                    # test plan
│   └── test-results*.md / *.json         # saved test results and snapshots
│
├── docs/
│   ├── data-schema.md                    # data fields + briefing output format
│   ├── verification-policy.md            # how records are classified
│   ├── decision-logic.md                 # scoring and decision rules
│   ├── assumptions-register.md
│   ├── threat-model.md
│   ├── source-compliance-and-scope.md
│   ├── live-ingestion-readiness.md
│   ├── live-retrieval-evidence.md
│   ├── hardening-coverage-matrix.md
│   ├── runbook.md                        # how to run the system
│   └── submission-index.md
│
├── logs/
│   ├── live-fetch-log.jsonl              # log of live API fetches
│   └── run-manifest.jsonl                # record of pipeline runs
│
├── assets/
│   ├── data-flow.svg                     # data flow diagram
│   ├── checklist.pdf
│   └── data-sources-strategy.pdf
│
└── *.html               

```

---

## Setup

- Python 3.12

Setup:

```bash
# 1. Clone and enter the repo
git clone https://github.com/nik1saran/Public-Catastrophic-Crisis-Briefing-Assistant.git
cd Public-Catastrophic-Crisis-Briefing-Assistant

# 2. Install dependencies
pip install -r requirements.txt

```

### Environment variables

Create `.env` in the project root. Never commit real keys.

```dotenv
ANTHROPIC_API_KEY=your_key
```
Optional: To choose a different model or backend, add `CRISIS_LLM=anthropic:claude-haiku-5-5` to .env file.

## Usage


## Saved data and outputs

| File or folder | Purpose |
|---|---|
| `data/live/raw/<run_id>/` | Raw USGS, NOAA and FEMA API responses for one run. |
| `data/live/input-claims-live_<timestamp>.json` | Claims parsed from the raw responses. |
| `data/live/validated-output-live_<timestamp>.json` | Verified records with status, scores and conflicts. The briefing module reads this file. |
| `data/briefings/briefing-<run_id>.json` | Generated briefing: events, grounded facts with quotes, review flag, disclosure. |
| `data/prompt_experiments/<timestamp>.json` | Prompt-version comparison results (v1–v5). |
| `data/sample-validated-output.json` | Small sample dataset for demos and tests. |
| `tests/live-test-results-<run_id>.json` / `.md` | Results of a live retrieval run. |
| `logs/live-fetch-log.jsonl` | Log of live API fetches. |
| `logs/run-manifest.jsonl` | Record of pipeline runs. |
| `.env` | Local API key and optional model setting. Never committed. |

Actual files are generated when stages execute. The repository may omit source data or results to protect credentials, respect source terms, and keep submission size manageable.



## Reliability, cost, and limitations
