#!/usr/bin/env python3
"""
Convert the released ``data/curated_mutations.jsonl`` into the mutant
``preds.json`` format expected by ``evaluation/evaluate.py`` (``_load_mutants``).

Input  (one JSON object per line):
    {"instance_id": "...", "mutation": {"strategy_group": "A", "strategy_code":
     "A1", "diff": "diff --git ...", "explanation": "..."}}

Output (a single JSON object keyed by instance_id):
    {"<instance_id>": {"model_name_or_path": "swe-mutation/curated",
                       "instance_id": "<instance_id>",
                       "model_patch": "<json string: {\"mutations\": [{\"diff\": ...}, ...]}>"}}

The evaluator re-parses ``model_patch`` as JSON, reads its ``mutations`` list,
and assigns ids ``mutant_1``, ``mutant_2``, ... in order. Records without a
``diff`` are skipped (they cannot be applied/executed).

Example
-------
python scripts/convert_curated_to_mutants.py \
    --curated data/curated_mutations.jsonl \
    -o results/mutants/preds.json
"""

from __future__ import annotations

import argparse
import collections
import json
from pathlib import Path


def convert(curated_path: Path) -> dict[str, dict]:
    by_iid: dict[str, list[dict]] = collections.defaultdict(list)
    n_lines = n_kept = n_no_diff = 0

    for line in curated_path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        n_lines += 1
        row = json.loads(line)
        iid = row.get("instance_id")
        mut = row.get("mutation", {}) or {}
        if not iid:
            continue
        if not mut.get("diff"):
            n_no_diff += 1
            continue
        by_iid[iid].append({
            "diff": mut["diff"],
            "strategy_group": mut.get("strategy_group"),
            "strategy_code": mut.get("strategy_code"),
            "explanation": mut.get("explanation", ""),
        })
        n_kept += 1

    preds = {
        iid: {
            "model_name_or_path": "swe-mutation/curated",
            "instance_id": iid,
            "model_patch": json.dumps({"mutations": muts}),
        }
        for iid, muts in by_iid.items()
    }

    print(f"[convert] read {n_lines} records; kept {n_kept} mutants "
          f"across {len(preds)} instances; skipped {n_no_diff} without a diff")
    return preds


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--curated", type=Path, default=Path("data/curated_mutations.jsonl"),
                    help="Path to curated_mutations.jsonl (default: data/curated_mutations.jsonl)")
    ap.add_argument("-o", "--output", type=Path, default=Path("results/mutants/preds.json"),
                    help="Output mutant preds.json (default: results/mutants/preds.json)")
    args = ap.parse_args()

    preds = convert(args.curated)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(preds, indent=2))
    print(f"[convert] wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
