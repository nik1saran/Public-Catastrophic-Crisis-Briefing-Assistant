"""Compare prompt versions on a fixed question set.

Usage: python tests/run_prompt_experiment.py [validated-output.json] [--llm SPEC] [--versions v1 v2]
Each question is asked under every version; the table shows how often the model
answered/declined correctly and how much of what it said survived the grounding checks.
Costs one LLM call per question per version.
"""
import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.crisis.briefing import answer_question, get_llm
from src.crisis.prompts import PROMPT_VERSIONS


def evaluate(batch: dict, questions: list[dict], llm, versions: list[str]) -> dict:
    results = {}
    for v in versions:
        rows = [answer_question(batch, q.get("question", ""), llm, template=q.get("template"), version=v)
                for q in questions]
        kept = sum(len(i) for r in rows for i in r["facts"].values())
        rejected = sum(len(r["rejected_facts"]) for r in rows)
        results[v] = {
            "answerable_match": sum(r["answerable"] == q["expect_answerable"] for r, q in zip(rows, questions)) / len(rows),
            "fact_grounding_rate": kept / (kept + rejected) if kept + rejected else 1.0,
            "fallback_rate": sum(r["answer_source"] == "fallback" for r in rows) / len(rows),
            "answers": [{"question": r["question"], "answerable": r["answerable"], "expected": q["expect_answerable"],
                         "answer": r["answer"],
                         "answer_source": r["answer_source"], "rejected": len(r["rejected_facts"]),
                         "error": r["llm_error"]} for r, q in zip(rows, questions)],
        }
    return results


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("input", nargs="?")
    p.add_argument("--llm")
    p.add_argument("--versions", nargs="+", default=list(PROMPT_VERSIONS))
    args = p.parse_args()
    path = Path(args.input) if args.input else sorted((ROOT / "data" / "live").glob("validated-output-*.json"))[-1]
    questions = json.loads((ROOT / "tests" / "prompt_questions.json").read_text(encoding="utf-8"))
    results = evaluate(json.loads(path.read_text(encoding="utf-8")), questions, get_llm(args.llm), args.versions)

    print(f"{'version':<8}{'answerable_match':>18}{'fact_grounding':>16}{'fallback_rate':>15}")
    for v, m in results.items():
        print(f"{v:<8}{m['answerable_match']:>18.0%}{m['fact_grounding_rate']:>16.0%}{m['fallback_rate']:>15.0%}")
    out = ROOT / "data" / "prompt_experiments" / f"{datetime.now(timezone.utc):%Y%m%d_%H%M%SZ}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"Wrote: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
