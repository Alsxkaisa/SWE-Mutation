# SWE-Mutation Inference with opencode + Skill

## Overview

This directory contains the opencode-based inference pipeline for SWE-Mutation. Instead of using Mini-SWE-Agent as the agent runtime, `inference_opencode_unified.py` uses **opencode** to drive the agent and integrates task definitions via an **opencode skill** (default: `dt-generation`).


### Architecture

```
SWE-bench instance image (swebench/sweb.eval.*)
  │
  ├── add opencode v1.17.9 + uv
  ├── add SWE-Mutation skill (SKILL.md)
  │
  ▼
inference image (swt-mut.eval.*)
  │
  ├── ① start container, mount workspace + opencode config
  ├── ② apply golden patches (code_patch + test_patch)
  ├── ③ run opencode with prompt referencing /{skill} skill
  ├── ④ extract candidate diff from <patch> tags
  ├── ⑤ Judge: verify F2P tests fail against mutated code
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

### Dependencies

#### External
- **Docker**: containers for SWE-bench images
- **opencode CLI**: downloaded into images at build time (`v1.17.9`)
- **uv**: Python package manager, downloaded into images at build time

#### Python
- `docker` (PyPI: `docker`)
- `swebench` (for test command lookup via `MAP_REPO_VERSION_TO_SPECS`)
- Python ≥ 3.10

Install with:
```bash
pip install docker swebench
```

---

## Usage

### 1. Prepare Patches File

The `--patches-file` accepts a JSONL file with one instance per line. Each line must contain:

```json
{
  "instance_id": "django__django-12345",
  "repo": "django/django",
  "version": "4.0",
  "base_commit": "abc123def",
  "patch": "diff --git a/...",
  "test_patch": "diff --git b/...",
  "test_files": ["tests/test_foo.py"],
  "files": ["django/foo/bar.py"],
  "FAIL_TO_PASS": ["test_case_1", "test_case_2"],
  "PASS_TO_PASS": ["test_case_3"],
  "problem_statement": "Bug description..."
}
```

The benchmark instance metadata from [SWE-bench](https://github.com/princeton-nlp/SWE-bench) works directly. The project's own `data/curated_mutations.jsonl` is in the same format but records post-hoc mutations; for generating new mutants, use the original SWE-bench instance file.

### 2. Generate Mutants

Default skill is `dt-generation`. Use `--skill` to override:

```bash
python inference/inference_opencode_unified.py \
    --mode generate_mutants \
    --patches-file /path/to/instances.jsonl \
    --model deepseek/deepseek-v4-flash \
    --max-instances 5
```

Resume a previous run:
```bash
python inference/inference_opencode_unified.py \
    --mode generate_mutants \
    --patches-file /path/to/instances.jsonl \
    --model deepseek/deepseek-v4-flash \
    --run-id 20260709
```

### 3. Evaluate Test Suites

```bash
python inference/inference_opencode_unified.py \
    --mode run_eval \
    --patches-file /path/to/instances.jsonl \
    --mutants-file results/mutants/preds.json \
    --test-preds-file results/tests/preds.json
```

### 4. Full Pipeline

```bash
python inference/inference_opencode_unified.py \
    --mode all \
    --patches-file /path/to/instances.jsonl \
    --model deepseek/deepseek-v4-flash \
    --test-preds-file results/tests/preds.json
```

---

## Options

### Mutation Generation

| Argument | Default | Description |
|----------|---------|-------------|
| `--mode` | `generate_mutants` | Pipeline mode: `generate_mutants`, `run_eval`, `all` |
| `--patches-file` | (required) | JSONL with instance patch data |
| `--model` | `deepseek/deepseek-v4-flash` | Model for opencode |
| `--max-instances` | unlimited | Limit number of instances |
| `--output` | auto | Output path for `preds.json` |
| `--timeout` | `600` | Per-instance timeout (seconds) |
| `--instance-ids` | all | Space-separated list of specific instance IDs |
| `--agent` | default | opencode agent name (e.g., `build`) |
| `--skill` | `dt-generation` | Skill name to load from `.opencode/skills/<name>/SKILL.md` |
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

### Inference Image Caching

Inference images are built once per SWE-bench base image and cached in Docker. The image adds opencode, uv, and the configured skill on top of the existing SWE-bench evaluation image, then reused across instances sharing the same base.

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
| Strategy injection | Jinja2 template in YAML config | `/{skill}` skill via `SKILL.md` |
| Image management | `swebench.harness.test_spec` | Direct Docker SDK (`docker build`) |
| Judge verification | `DockerEnvironment` (mini-swe-agent) | Raw `docker exec` (no agent dependency) |
| Output | `preds.json` (JSON object per instance) | Same format (compatible) |
| Resume support | `--skip-existing` | `--run-id` (timestamp-based skip) |
| Dependency | `mini-swe-agent`, `swebench` | `docker`, `swebench` (lighter) |
