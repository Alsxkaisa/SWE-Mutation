# SWE-Mutation Inference with opencode + Skill

## Overview

This directory contains the opencode-based inference pipeline for SWE-Mutation. Instead of using Mini-SWE-Agent as the agent runtime, `inference_opencode_unified.py` uses **opencode** to drive the agent and integrates task definitions via an **opencode skill** (default: `dt-generation`).


### Architecture

```
SWE-bench instance image (swebench/sweb.eval.*)
  │
  ├── add opencode v1.17.9 + uv
  │
  ▼
inference image (swt-mut.eval.*)
  │
  ├── ① start container, mount:
  │      ├── workspace (repo at base commit + golden patches applied)
  │      ├── opencode config (opencode.json)
  │      ├── host ~/.opencode → /home/nonroot/.opencode (skill + auth)
  │      └── host ~/.config/opencode → /home/nonroot/.config/opencode
  │
  ├── ② run opencode with prompt referencing /{skill} skill
  ├── ③ extract candidate diff from <patch> tags
  ├── ④ Judge: separate container verifies F2P tests fail
  │
  ▼
accepted mutants → preds.json (compatible with evaluation pipeline)
```

### Files

| File | Purpose |
|------|---------|
| `inference_opencode_unified.py` | Main inference + evaluation script |
| `opencode.json` | opencode permission configuration (mounted into container) |
| `README.md` | This file |

### Prerequisites

1. **Docker** — SWE-bench instance images must exist locally (e.g., built via `swebench` harness)
2. **opencode CLI** on host — used by the script (downloaded into inference images automatically)
3. **Skill file** — place your skill at `~/.opencode/skills/<skill_name>/SKILL.md` (default: `~/.opencode/skills/dt-generation/SKILL.md`). The host `~/.opencode` is mounted into containers at runtime.
4. **opencode auth** (optional) — `~/.local/share/opencode/auth.json` is mounted for API access

#### Python
- `docker` (PyPI: `docker`)
- `swebench` (for test command lookup via `MAP_REPO_VERSION_TO_SPECS`)
- `datasets` (for HuggingFace dataset loading)
- Python ≥ 3.10

```bash
pip install docker swebench datasets
```

---

## Usage

### Data Source

Instances are loaded from HuggingFace by default (`eth-sri/SWT-bench_Lite_bm25_27k_zsb`). Use `--dataset` to specify a different dataset, or `--patches-file` to load from a local JSONL.

### 1. Generate Mutants

```bash
# From HF dataset (default), first 5 instances
python inference/inference_opencode_unified.py \
    --mode generate_mutants \
    --model deepseek/deepseek-v4-flash \
    --max-instances 5

# From a local patches file
python inference/inference_opencode_unified.py \
    --mode generate_mutants \
    --patches-file /path/to/instances.jsonl \
    --model deepseek/deepseek-v4-flash \
    --max-instances 5

# Specific instances only
python inference/inference_opencode_unified.py \
    --mode generate_mutants \
    --model deepseek/deepseek-v4-flash \
    --instance-ids django__django-12345 sympy__sympy-67890

# Resume a previous run
python inference/inference_opencode_unified.py \
    --mode generate_mutants \
    --model deepseek/deepseek-v4-flash \
    --run-id 20260709
```

### 2. Evaluate Test Suites

```bash
python inference/inference_opencode_unified.py \
    --mode run_eval \
    --mutants-file results/mutants/preds.json \
    --test-preds-file results/tests/preds.json

# With a different dataset (must match the one used for generation)
python inference/inference_opencode_unified.py \
    --mode run_eval \
    --dataset princeton-nlp/SWE-bench_Lite \
    --mutants-file results/mutants/preds.json \
    --test-preds-file results/tests/preds.json
```

### 3. Full Pipeline

```bash
# Generate mutants then evaluate
python inference/inference_opencode_unified.py \
    --mode all \
    --model deepseek/deepseek-v4-flash \
    --max-instances 5 \
    --test-preds-file results/tests/preds.json
```

---

## Options

### Mutation Generation

| Argument | Default | Description |
|----------|---------|-------------|
| `--mode` | `generate_mutants` | Pipeline mode: `generate_mutants`, `run_eval`, `all` |
| `--dataset` | `eth-sri/SWT-bench_Lite_bm25_27k_zsb` | HuggingFace dataset (ignored if `--patches-file` given) |
| `--patches-file` | `None` | Local JSONL with instance data (overrides `--dataset`) |
| `--model` | `deepseek/deepseek-v4-flash` | Model for opencode |
| `--max-instances` | unlimited | Limit number of instances to process |
| `--instance-ids` | all | Space-separated list of specific instance IDs |
| `--output` | auto | Output path for `preds.json` |
| `--timeout` | `600` | Per-instance timeout (seconds) |
| `--agent` | default | opencode agent name (e.g., `build`) |
| `--skill` | `dt-generation` | Skill name; loaded from `~/.opencode/skills/<name>/SKILL.md` |
| `--run-id` | auto (timestamp) | Run identifier for resume |
| `--retry-limit` | `2` | Judge retries per strategy round |
| `--repo-cache` | `./repo-cache` | Git clone cache directory |
| `--workspace-dir` | `./tmp/workspaces` | Temporary workspace root |

### Evaluation

| Argument | Default | Description |
|----------|---------|-------------|
| `--mode` | `generate_mutants` | Use `run_eval` or `all` |
| `--mutants-file` | (required for eval) | `preds.json` with mutants |
| `--test-preds-file` | (required for eval) | `preds.json` with test patches |
| `--task` | `test_repair` | `test_generation` or `test_repair` |
| `--eval-output` | `./eval_results` | Evaluation output directory |
| `--workers` | `1` | Parallel Docker containers |
| `--filter-spec` | `""` | Regex filter for instance IDs |

---

## How It Works

### Prompt + Skill Integration

The prompt template (`prompts/opencode_skill.txt`) starts with `/{skill}` which tells opencode to load the skill from `.opencode/skills/{skill_name}/SKILL.md`. The skill (default: `dt-generation`) defines the task instructions, strategies, and output format.

When the prompt is formatted at runtime, `{skill}` is replaced with the value of `--skill` (default `dt-generation`) and instance-specific variables (`{issue}`, `{strategy_group}`, `{allowed_files}`, etc.) are filled in.

### Per-Round Strategy Execution

For each instance, five rounds are executed — one per strategy group. In each round:

1. opencode runs the agent with a prompt scoped to that round's strategies
2. The agent autonomously explores the codebase and injects a bug
3. The candidate diff is extracted from `<patch>` tags in opencode's stdout
4. **Judge**: a separate Docker container applies golden patches + candidate patch and runs F2P tests; acceptance requires at least one F2P test to fail
5. If rejected, the round retries (up to `--retry-limit` times)

### Skill Mounting (not built into image)

Skills are **not** baked into the inference image. Instead, the host's `~/.opencode` directory is mounted into the container at runtime (`/home/nonroot/.opencode`), which includes:
- `~/.opencode/skills/<skill_name>/SKILL.md` — the skill definition
- `~/.opencode/auth.json` (via `~/.local/share/opencode/auth.json`) — API authentication

This means you can update skills without rebuilding Docker images.

### Inference Image Caching

Inference images are built once per SWE-bench base image and cached in Docker. The image adds opencode and uv on top of the existing SWE-bench evaluation image, then reused across instances sharing the same base.

### Output Format

Results are written to `preds.json` in a format compatible with `evaluation/evaluate.py`:

```json
{
  "django__django-12345": {
    "model_name_or_path": "opencode__deepseek_deepseek-v4-flash",
    "instance_id": "django__django-12345",
    "model_patch": "{\"mutations\": [{\"round\": 1, \"strategy_group\": \"A\", ...}]}"
  }
}
```

---

## Comparison with Mini-SWE-Agent Pipeline

| Aspect | Mini-SWE-Agent (`mutation.py`) | opencode (`inference_opencode_unified.py`) |
|--------|-------------------------------|-------------------------------------------|
| Agent framework | `mini-swe-agent` / `DefaultAgent` | `opencode` CLI |
| Agent interaction | Python-in-process, step-by-step bash | Autonomous agent in Docker container |
| Strategy injection | Jinja2 template in YAML config | `/{skill}` skill via `SKILL.md` (mounted from host) |
| Image management | `swebench.harness.test_spec` | Direct Docker SDK (`docker build`) |
| Data source | Local patches file only | HuggingFace dataset (`--dataset`) or local file (`--patches-file`) |
| Judge verification | `DockerEnvironment` (mini-swe-agent) | Raw `docker exec` (no agent dependency) |
| Output | `preds.json` (JSON object per instance) | Same format (compatible) |
| Resume support | `--skip-existing` | `--run-id` (timestamp-based skip) |
| Dependency | `mini-swe-agent`, `swebench` | `docker`, `swebench` (lighter) |
