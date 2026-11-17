"""Generate a briefing from the newest validated live output.

Usage: python tests/run_briefing.py [validated-output.json] [--llm SPEC] [--max-events N]
SPEC: 'anthropic' (default, needs ANTHROPIC_API_KEY), 'anthropic:<model-id>',
      or 'package.module:factory' for your own backend (see get_llm in src/crisis/briefing.py).
The CRISIS_LLM environment variable sets the default SPEC.
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.crisis.briefing import build_briefing, get_llm


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("input", nargs="?", help="validated output JSON (default: newest in data/live)")
    p.add_argument("--llm", help="backend spec (default: $CRISIS_LLM or 'anthropic')")
    p.add_argument("--max-events", type=int)
    args = p.parse_args()

    path = Path(args.input) if args.input else sorted((ROOT / "data" / "live").glob("validated-output-*.json"))[-1]
    briefing = build_briefing(json.loads(path.read_text(encoding="utf-8")), get_llm(args.llm), args.max_events)
    out = ROOT / "data" / "briefings" / f"briefing-{briefing['input_run_id']}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(briefing, indent=2), encoding="utf-8")
    for e in briefing["events"]:
        print(f"[{'!' if e['important']['is_important'] else ' '}] {e['status']:<11} {e['summary'][:110]}")
    print(f"Wrote: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
