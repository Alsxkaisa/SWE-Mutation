#!/usr/bin/env python3
"""
Build the ``instances.jsonl`` metadata file required by the SWE-Mutation
generator (``framework/mutation.py``) and evaluator (``evaluation/evaluate.py``).

The released ``data/curated_mutations.jsonl`` only stores the mutation payloads
(``instance_id`` + ``mutation``). The pipeline additionally needs the upstream
repository metadata for each instance:

    instance_id, repo, version, base_commit, patch, test_patch,
    problem_statement, FAIL_TO_PASS, PASS_TO_PASS, test_files, files

Those fields come straight from the datasets SWE-Mutation is built on:
  * SWE-bench Verified          -> the 500 Python instances
  * SWE-bench-Multilingual      -> the 300 multilingual instances

This script pulls one (or more) HuggingFace datasets, keeps only the
instance_ids that appear in a curated mutations file (if given), derives
``files`` / ``test_files`` from the diff headers of ``patch`` / ``test_patch``,
and writes the result as JSONL.

Examples
--------
# Python core (SWE-bench Verified), filtered to the curated instances:
python scripts/build_instances.py \
    --dataset princeton-nlp/SWE-bench_Verified \
    --curated data/curated_mutations.jsonl \
    -o data/swe_mutation/instances.jsonl

# Multilingual subset (append into the same file):
python scripts/build_instances.py \
    --dataset swe-bench/SWE-bench_Multilingual \
    --curated data/curated_mutations.jsonl \
    -o data/swe_mutation/instances.jsonl --append
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# Fields we copy verbatim from the upstream dataset when present.
_PASSTHROUGH = [
    "repo",
    "version",
    "base_commit",
    "patch",
    "test_patch",
    "problem_statement",
    "FAIL_TO_PASS",
    "PASS_TO_PASS",
    "environment_setup_commit",
    "image_name",
]

# ``diff --git a/<path> b/<path>`` captures the modified file path robustly,
# including for created/deleted files where +++/--- may be /dev/null.
_DIFF_GIT_RE = re.compile(r"^diff --git a/(?P<a>.+?) b/(?P<b>.+?)\s*$", re.MULTILINE)


def files_from_diff(diff_text: str) -> list[str]:
    """Extract the set of file paths touched by a unified git diff (ordered)."""
    if not diff_text:
        return []
    seen: list[str] = []
    for m in _DIFF_GIT_RE.finditer(diff_text):
        path = m.group("b") or m.group("a")
        if path and path != "/dev/null" and path not in seen:
            seen.append(path)
    return seen


def load_curated_ids(curated_path: Path | None) -> set[str] | None:
    """Return the set of instance_ids present in a curated mutations JSONL."""
    if curated_path is None:
        return None
    ids: set[str] = set()
    for line in curated_path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        obj = json.loads(line)
        iid = obj.get("instance_id")
        if iid:
            ids.add(str(iid))
    return ids


def build_row(row: dict) -> dict:
    """Convert an upstream dataset row into the SWE-Mutation instance schema."""
    out: dict = {"instance_id": row["instance_id"]}
    for key in _PASSTHROUGH:
        if key in row and row[key] is not None:
            out[key] = row[key]
    # Derived scope fields (used by _load_patches / _load_test_patches).
    out["files"] = files_from_diff(row.get("patch", ""))
    out["test_files"] = files_from_diff(row.get("test_patch", ""))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dataset", required=True,
                    help="HuggingFace dataset id, e.g. princeton-nlp/SWE-bench_Verified")
    ap.add_argument("--split", default="test", help="Dataset split (default: test)")
    ap.add_argument("--curated", type=Path, default=None,
                    help="Optional curated_mutations.jsonl to filter instance_ids by")
    ap.add_argument("-o", "--output", type=Path, required=True,
                    help="Output instances.jsonl path")
    ap.add_argument("--append", action="store_true",
                    help="Append to the output file instead of overwriting "
                         "(useful when combining Verified + Multilingual)")
    args = ap.parse_args()

    try:
        from datasets import load_dataset
    except ImportError:
        print("ERROR: `datasets` is not installed. Run: pip install datasets", file=sys.stderr)
        return 1

    keep_ids = load_curated_ids(args.curated)

    print(f"[build_instances] loading {args.dataset} (split={args.split}) ...", file=sys.stderr)
    ds = load_dataset(args.dataset, split=args.split)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    mode = "a" if args.append else "w"

    n_written = 0
    n_no_files = 0
    written_ids: set[str] = set()
    with args.output.open(mode) as f:
        for row in ds:
            iid = row.get("instance_id")
            if not iid:
                continue
            if keep_ids is not None and iid not in keep_ids:
                continue
            out = build_row(row)
            if not out["files"]:
                n_no_files += 1
            f.write(json.dumps(out, ensure_ascii=False) + "\n")
            written_ids.add(iid)
            n_written += 1

    print(f"[build_instances] wrote {n_written} instances -> {args.output} "
          f"(mode={'append' if args.append else 'write'})", file=sys.stderr)
    if n_no_files:
        print(f"[build_instances] WARNING: {n_no_files} instances had no parsable "
              f"files in `patch` (empty golden patch?)", file=sys.stderr)
    if keep_ids is not None:
        missing = keep_ids - written_ids
        print(f"[build_instances] curated ids: {len(keep_ids)}, "
              f"matched in this dataset: {len(written_ids & keep_ids)}, "
              f"still missing: {len(missing)}", file=sys.stderr)
        if missing and len(missing) <= 20:
            print(f"[build_instances] missing ids: {sorted(missing)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
