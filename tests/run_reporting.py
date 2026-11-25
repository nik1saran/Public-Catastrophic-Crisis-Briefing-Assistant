"""Turn briefings into a situation report, an executive briefing and a public-statement draft.

Usage:
  python tests/run_reporting.py [current-briefing.json] [--previous previous-briefing.json]
                                [--batch validated-output.json] [--area "Bay County"]
Defaults: the two newest files in data/briefings (newest = current). No API key is needed:
this step makes no LLM calls. Outputs go to data/reports/. Drafts are saved as pending_review;
nothing is released from this script.
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.crisis.reporting import build_situation_report, draft_public_statement, render_executive_briefing


def _load(path):
    return json.loads(Path(path).read_text(encoding="utf-8")) if path else None


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("current", nargs="?")
    p.add_argument("--previous")
    p.add_argument("--batch", help="validated output the current briefing was built from (adds locations)")
    p.add_argument("--area")
    args = p.parse_args()

    found = sorted((ROOT / "data" / "briefings").glob("briefing-*.json"), key=lambda f: f.stat().st_mtime)
    current = args.current or (found[-1] if found else None)
    if current is None:
        print("No briefing found. Run tests/run_briefing.py first.")
        return 1
    previous = args.previous or (str(found[-2]) if not args.current and len(found) > 1 else None)

    report = build_situation_report(_load(current), _load(previous), _load(args.batch))
    draft = draft_public_statement(report, area=args.area)
    out = ROOT / "data" / "reports"
    out.mkdir(parents=True, exist_ok=True)
    stem = report["report_id"]
    (out / f"{stem}.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    (out / f"{stem}-executive.md").write_text(render_executive_briefing(report), encoding="utf-8")
    (out / f"{stem}-public-draft.json").write_text(
        json.dumps({k: v for k, v in draft.items() if not k.startswith("_")}, indent=2), encoding="utf-8")

    print("\n".join(report["change_summary"]))
    print(f"\nPublic draft safety: {'PASS' if draft['safety']['passed'] else 'BLOCKED'}")
    for line in draft["safety"]["blocking"] + draft["safety"]["warnings"]:
        print(f"  - {line}")
    print(f"Wrote {out}/{stem}*.  The draft is pending human review.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
