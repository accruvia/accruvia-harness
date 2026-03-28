from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from accruvia_harness.evidence.collector import LocalEvidenceCollector


def main() -> int:
    parser = argparse.ArgumentParser(description="Collect local evidence for an objective.")
    parser.add_argument("--objective", required=True, help="Objective identifier")
    parser.add_argument("--type", required=True, help="Evidence artifact type")
    args = parser.parse_args()

    result = LocalEvidenceCollector().collect(args.objective, args.type)
    print(json.dumps(result.__dict__, sort_keys=True))
    return 0 if result.success else 1


if __name__ == "__main__":
    raise SystemExit(main())
