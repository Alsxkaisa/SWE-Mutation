#!/usr/bin/env python3
"""
SWE-Mutation inference using opencode + skill.

This script adapts the SWE-Mutation agentic mutation framework to use opencode
as the agent runtime instead of mini-swe-agent. It builds Docker inference
images with opencode + skill, runs mutation tasks inside containers, verifies
mutants with the Judge (F2P tests), and saves results compatible with the
existing evaluation pipeline.

Architecture:
  SWE-bench instance image (swebench/sweb.eval.*)
      ↓ add opencode + uv + skill
  inference image (swt-mut.eval.*)
      ↓ apply golden patches → run opencode with mutation prompt
  candidate mutants extracted from <patch> tags
      ↓ verify with Judge (F2P test execution)
  accepted mutants saved to preds.json

Usage:
    # Generate mutants with skill
    python inference/inference_opencode_unified.py --mode generate_mutants \\
        --patches-file data/curated_mutations.jsonl \\
        --model deepseek/deepseek-v4-flash --max-instances 5

    # With specific run_id for resume
    python inference/inference_opencode_unified.py --mode generate_mutants \\
        --patches-file data/curated_mutations.jsonl \\
        --model deepseek/deepseek-v4-flash --run-id 20260709

    # Evaluate test suites against mutants
    python inference/inference_opencode_unified.py --mode run_eval \\
        --patches-file data/patches.jsonl \\
        --mutants-file results/mutants/preds.json \\
        --test-preds-file results/tests/preds.json

    # Full pipeline: generate mutants then evaluate
    python inference/inference_opencode_unified.py --mode all \\
        --patches-file data/patches.jsonl \\
        --model deepseek/deepseek-v4-flash
"""

import argparse
import base64
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

# Add project root to sys.path so that 'evaluation' and 'framework' are importable
_SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPT_DIR.parent))

import tempfile
import threading
import time
import traceback
import uuid
from pathlib import Path

import docker

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
PROMPTS_DIR = PROJECT_ROOT / "prompts"
# SKILLS_DIR is unused; skill is mounted from host ~/.opencode at runtime
OPCODE_CONFIG_PATH = SCRIPT_DIR / "opencode.json"
PROMPT_SKILL_PATH = PROMPTS_DIR / "opencode_skill.txt"

REPO_CACHE_DEFAULT = PROJECT_ROOT / "repo-cache"
WORKSPACE_DIR_DEFAULT = PROJECT_ROOT / "tmp" / "workspaces"
PREDICTIONS_DIR = PROJECT_ROOT / "predictions"
EVAL_DIR_DEFAULT = PROJECT_ROOT / "eval_results"

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
DEFAULT_MODEL = "deepseek/deepseek-v4-flash"
DEFAULT_TIMEOUT = 600
SKILL_NAME = "dt-generation"
OPCODE_VERSION = "v1.17.9"
GIT_BASE_URL = os.environ.get("GIT_BASE_URL", "https://github.com")

# Default dataset (matching swt-bench-inference reference)
DEFAULT_DATASET = "eth-sri/SWT-bench_Lite_bm25_27k_zsb"

STRATEGY_GROUPS = [
    ("A", "API Specifications & Contracts", ["A1", "A2", "A3", "A4"]),
    ("B", "Boundaries & Conditional Logic", ["B1", "B2", "B3"]),
    ("C", "Type & Data Shape", ["C1", "C2", "C3"]),
    ("D", "Stateful Logic & Sequences", ["D1", "D2", "D3", "D4", "D5", "D6"]),
    ("E", "Test-Expectation Alignment", ["E1", "E2"]),
]


# ---------------------------------------------------------------------------
# Helpers: streaming subprocess
# ---------------------------------------------------------------------------

def _run_streamed(cmd, cwd, timeout, prefix="", err_prefix="ERR", env=None, check=True, quiet_stderr=False):
    proc = subprocess.Popen(
        cmd,
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env or os.environ.copy(),
    )

    stdout_lines = []
    stderr_lines = []

    def _read(pipe, acc, label):
        for line in iter(pipe.readline, ""):
            acc.append(line)
            if label is not False and not quiet_stderr:
                fmt = f"  [{label}] {line.rstrip()}" if label else f"  {line.rstrip()}"
                sys.stdout.write(fmt + "\n")
                sys.stdout.flush()

    t_out = threading.Thread(target=_read, args=(proc.stdout, stdout_lines, prefix))
    t_err = threading.Thread(target=_read, args=(proc.stderr, stderr_lines, err_prefix))
    t_out.start()
    t_err.start()

    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        t_out.join()
        t_err.join()
        raise
    except TypeError:
        proc.wait()

    t_out.join()
    t_err.join()

    result = subprocess.CompletedProcess(
        args=cmd,
        returncode=proc.returncode,
        stdout="".join(stdout_lines),
        stderr="".join(stderr_lines),
    )

    if check and result.returncode != 0:
        raise subprocess.CalledProcessError(
            result.returncode, cmd, output=result.stdout, stderr=result.stderr,
        )

    return result


def _run_simple(cmd, cwd="/tmp", timeout=60, check=False):
    return subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, timeout=timeout, check=check)


# ---------------------------------------------------------------------------
# Helpers: data loading (compatible with mutation.py / evaluate.py)
# ---------------------------------------------------------------------------

def _parse_list_field(v):
    if v is None:
        return []
    if isinstance(v, list):
        return [str(x) for x in v]
    if isinstance(v, str):
        s = v.strip()
        try:
            if s.startswith("[") and s.endswith("]"):
                return [str(x) for x in json.loads(s)]
            if "," in s:
                return [x.strip() for x in s.split(",") if x.strip()]
            if s:
                return [s]
        except Exception:
            return [s]
    return []


def load_patches(patches_file: Path):
    mapping = {}
    for line in patches_file.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        obj = json.loads(line)
        iid = obj.get("instance_id") or obj.get("id") or obj.get("name")
        if not iid:
            continue
        mapping[str(iid)] = {
            "repo": obj.get("repo", ""),
            "version": obj.get("version", ""),
            "base_commit": obj.get("base_commit", ""),
            "patch": obj.get("patch", ""),
            "test_patch": obj.get("test_patch", ""),
            "test_files": _parse_list_field(obj.get("test_files")),
            "files": _parse_list_field(obj.get("files")),
            "FAIL_TO_PASS": _parse_list_field(obj.get("FAIL_TO_PASS")),
            "PASS_TO_PASS": _parse_list_field(obj.get("PASS_TO_PASS")),
            "problem_statement": obj.get("problem_statement", obj.get("repo_description", "")),
            "image_name": obj.get("image_name", ""),
        }
    return mapping


def load_all_instance_ids(patches_file: Path):
    ids = []
    for line in patches_file.read_text().splitlines():
        s = line.strip()
        if not s:
            continue
        obj = json.loads(s)
        iid = obj.get("instance_id") or obj.get("id") or obj.get("name")
        if iid:
            ids.append(str(iid))
    return ids


def get_completed_ids(output_path):
    if not output_path.exists():
        return set()
    completed = set()
    data = json.loads(output_path.read_text())
    for iid in data:
        completed.add(iid)
    return completed


def get_swebench_docker_image_name(instance: dict) -> str:
    image_name = instance.get("image_name")
    if image_name:
        return image_name
    iid = instance["instance_id"]
    id_docker_compatible = iid.replace("__", "_1776_")
    return f"swebench/sweb.eval.x86_64.{id_docker_compatible}:latest".lower()


# ---------------------------------------------------------------------------
# Helpers: repo / workspace management
# ---------------------------------------------------------------------------

def ensure_repo(repo, repo_cache_dir):
    safe_name = repo.replace("/", "__")
    repo_path = repo_cache_dir / safe_name
    if not (repo_path / ".git").exists():
        repo_path.mkdir(parents=True, exist_ok=True)
        clone_url = f"{GIT_BASE_URL}/{repo}.git"
        print(f"  Cloning {clone_url} ...")
        _run_streamed(
            ["git", "clone", "--quiet", clone_url, str(repo_path)],
            cwd=repo_path.parent, timeout=600, prefix="git",
        )
        print(f"  Clone complete: {repo_path}")
    return repo_path


def setup_workspace(repo_path, base_commit, instance_id, workspace_dir):
    safe_repo_name = repo_path.name
    instance_dir = workspace_dir / instance_id
    worktree_path = instance_dir / safe_repo_name

    if instance_dir.exists():
        shutil.rmtree(instance_dir, ignore_errors=True)
    instance_dir.mkdir(parents=True, exist_ok=True)

    print(f"  Copying repo to workspace ...")
    shutil.copytree(str(repo_path), str(worktree_path), symlinks=True)

    _run_streamed(
        ["git", "-C", str(worktree_path), "checkout", "--detach", base_commit],
        cwd=str(instance_dir), timeout=60, prefix="git",
    )
    for branch in ["main", "master", "develop"]:
        _run_streamed(
            ["git", "-C", str(worktree_path), "branch", "-D", branch],
            cwd=str(instance_dir), timeout=10, prefix="git", check=False,
        )
    _run_streamed(
        ["git", "-C", str(worktree_path), "remote", "remove", "origin"],
        cwd=str(instance_dir), timeout=10, prefix="git", check=False,
    )
    _run_streamed(
        ["git", "-C", str(worktree_path), "reflog", "expire", "--expire=now", "--all"],
        cwd=str(instance_dir), timeout=10, prefix="git", check=False,
    )
    _run_streamed(
        ["git", "-C", str(worktree_path), "gc", "--prune=now"],
        cwd=str(instance_dir), timeout=30, prefix="git", check=False,
    )

    result = _run_streamed(
        ["git", "-C", str(worktree_path), "rev-parse", "HEAD"],
        cwd=str(instance_dir), timeout=10, prefix="git",
    )
    actual_commit = result.stdout.strip()
    print(f"  Verified commit: {actual_commit[:8]} (expected: {base_commit[:8]})")
    if not actual_commit.startswith(base_commit):
        raise RuntimeError(f"Commit mismatch! Expected {base_commit}, got {actual_commit}")

    return worktree_path


def cleanup_workspace(worktree_path):
    if worktree_path and worktree_path.exists():
        shutil.rmtree(worktree_path, ignore_errors=True)


def write_issue(worktree_path, issue_text):
    (worktree_path / "ISSUE.md").write_text(issue_text)


def apply_patches_in_workspace(worktree_path, code_patch, test_patch):
    """Apply golden patches to the workspace. Returns True on success."""
    def _apply(patch_text, label):
        if not patch_text or not patch_text.strip():
            return True
        marker = f"SWE_MUTATION_{label.upper()}_PATCH_EOF"
        with tempfile.NamedTemporaryFile(mode='w', suffix='.patch', delete=False, dir=worktree_path) as f:
            f.write(patch_text)
            patch_file = f.name
        try:
            _run_simple(["git", "apply", "-p1", patch_file], cwd=worktree_path, timeout=30, check=True)
            return True
        except subprocess.CalledProcessError as e:
            print(f"  Failed to apply {label} patch: {e.stderr[:200]}")
            return False
        finally:
            Path(patch_file).unlink(missing_ok=True)

    return _apply(code_patch, "code") and _apply(test_patch, "test")


def get_instance_test_cmd(instance):
    try:
        from swebench.harness.constants import MAP_REPO_VERSION_TO_SPECS
        repo = instance.get("repo", "")
        version = instance.get("version", "")
        if repo and version:
            spec = MAP_REPO_VERSION_TO_SPECS.get(repo, {}).get(version, {})
            raw = spec.get("test_cmd", "")
            if isinstance(raw, list):
                return raw[0] if raw else ""
            return raw or ""
    except Exception:
        pass
    return ""


# ---------------------------------------------------------------------------
# Docker image management
# ---------------------------------------------------------------------------

def ensure_src_instance_image(instance: dict) -> str:
    image_key = get_swebench_docker_image_name(instance)
    client = docker.from_env()
    try:
        client.images.get(image_key)
        print(f"  Src image exists: {image_key[:60]}...")
        return image_key
    except docker.errors.ImageNotFound:
        print(f"  Building src instance image: {image_key}")
        from swebench.harness.test_spec.test_spec import make_test_spec
        from swebench.harness.docker_build import build_instance_image as sweb_build_image
        import logging
        spec = make_test_spec(instance)
        sweb_build_image(
            test_spec=spec,
            client=client,
            logger=logging.getLogger(f"build-{instance.get('instance_id', 'unknown')}"),
            nocache=False,
        )
        print(f"  Src instance image built: {image_key}")
        return image_key


def build_inference_image(src_image_key: str) -> str:
    inference_image_key = src_image_key.replace("sweb.eval", "swt-mut.eval")

    client = docker.from_env()
    try:
        client.images.get(inference_image_key)
        print(f"  Inference image exists: {inference_image_key[:60]}...")
        return inference_image_key
    except docker.errors.ImageNotFound:
        pass

    print(f"  Building inference image: {inference_image_key}")
    ocode_arch = "arm64" if "arm64" in src_image_key else "x64"

    dockerfile = f"""FROM {src_image_key}
RUN apt-get update && apt-get install -y ca-certificates && update-ca-certificates && rm -rf /var/lib/apt/lists/*
RUN mkdir -p /home/nonroot/.local/state && chown -R nonroot:nonroot /home/nonroot/.local
RUN mkdir -p /home/nonroot/.local/share/opencode && chown -R nonroot:nonroot /home/nonroot/.local/share/opencode
RUN mkdir -p /home/nonroot/.local/share/uv && chown -R nonroot:nonroot /home/nonroot/.local/share/uv
RUN chown -R nonroot:nonroot /testbed
RUN curl -fsSL "https://github.com/anomalyco/opencode/releases/download/{OPCODE_VERSION}/opencode-linux-{ocode_arch}.tar.gz" \
    | tar xz -C /usr/local/bin opencode
RUN curl -LsSf https://astral.sh/uv/install.sh | sh && \
    cp /root/.local/bin/uv /usr/local/bin/uv && \
    cp /root/.local/bin/uvx /usr/local/bin/uvx && \
    chmod +x /usr/local/bin/uv /usr/local/bin/uvx
"""

    with tempfile.TemporaryDirectory() as tmpdir:
        Path(tmpdir / "Dockerfile").write_text(dockerfile)
        try:
            result = client.images.build(
                path=tmpdir,
                tag=inference_image_key,
                rm=True,
                forcerm=True,
            )
            print(f"  Inference image built: {inference_image_key}")
            return inference_image_key
        except docker.errors.BuildError as e:
            print(f"  Build failed: {e}")
            raise


def _docker_mount_args():
    mounts = []
    auth_json = Path.home() / ".local/share/opencode/auth.json"
    config_dir = Path.home() / ".config/opencode"
    opencode_dir = Path.home() / ".opencode"

    if auth_json.exists():
        mounts.extend(["-v", f"{auth_json}:/tmp/auth.json:ro"])
    if config_dir.exists():
        mounts.extend(["-v", f"{config_dir}:/home/nonroot/.config/opencode"])
    if opencode_dir.exists():
        mounts.extend(["-v", f"{opencode_dir}:/home/nonroot/.opencode"])
    return mounts


# ---------------------------------------------------------------------------
# opencode runner
# ---------------------------------------------------------------------------

def run_opencode(
    inference_image: str,
    prompt: str,
    model: str,
    timeout: int,
    instance_id: str,
    agent: str = None,
    opencode_config: Path = None,
    workspace_mount: str = None,
) -> tuple[subprocess.CompletedProcess, Path | None]:
    prompt_b64 = base64.b64encode(prompt.encode()).decode()
    agent_flag = f"--agent {agent}" if agent else ""
    container_name = f"opencode-swm-{instance_id}-{uuid.uuid4().hex[:8]}"

    setup_script = f"""#!/bin/bash
set -e
export HOME=/home/nonroot
if [ -f /opt/miniconda3/bin/activate ]; then
    source /opt/miniconda3/bin/activate
    conda activate testbed 2>/dev/null || true
fi
if [ -f /home/nonroot/miniconda3/bin/activate ]; then
    source /home/nonroot/miniconda3/bin/activate
    conda activate testbed 2>/dev/null || true
fi
cd /testbed
git config --global --add safe.directory /testbed || true
if [ -f /tmp/auth.json ]; then
    mkdir -p /home/nonroot/.local/share/opencode
    cp /tmp/auth.json /home/nonroot/.local/share/opencode/auth.json
fi
echo "  [opencode] Running opencode..."
OPCODE_PROMPT=$(echo {prompt_b64} | base64 -d)
opencode run "$OPCODE_PROMPT" --model {model} {agent_flag} --title {instance_id} --dir /testbed || true

echo "  [export] Exporting session..."
LATEST_SESSION=$(opencode session list --format json 2>/dev/null | python3 -c "import sys,json; sessions=json.load(sys.stdin); print(sessions[0]['id'] if sessions else '')" 2>/dev/null || true)
if [ -n "$LATEST_SESSION" ]; then
    opencode export "$LATEST_SESSION" > /tmp/opencode_session_export.json 2>/dev/null || true
fi
"""

    with tempfile.NamedTemporaryFile(mode='w', suffix='.sh', delete=False) as f:
        f.write(setup_script)
        script_path = f.name

    export_path = None
    try:
        cmd = ["docker", "run", "--name", container_name, "--user", "nonroot"]
        cmd.extend(["-v", f"{script_path}:/run.sh"])

        if workspace_mount:
            cmd.extend(["-v", f"{workspace_mount}:/testbed"])

        if opencode_config is not None and opencode_config.exists():
            cmd.extend(["-v", f"{opencode_config}:/testbed/opencode.json"])

        cmd.extend(_docker_mount_args())
        cmd.extend(["--workdir", "/testbed", inference_image, "bash", "/run.sh"])

        result = _run_streamed(
            cmd, cwd="/tmp", timeout=timeout,
            prefix="opencode", err_prefix="opencode",
            check=False, quiet_stderr=True,
        )

        try:
            tmp_export = Path(tempfile.gettempdir()) / f"opencode_session_{instance_id}.json"
            cp_result = _run_simple(
                ["docker", "cp", f"{container_name}:/tmp/opencode_session_export.json", str(tmp_export)],
                timeout=30, check=False,
            )
            if cp_result.returncode == 0 and tmp_export.exists() and tmp_export.stat().st_size > 0:
                export_path = tmp_export
        except Exception:
            pass

        return result, export_path
    finally:
        _run_simple(["docker", "rm", "-f", container_name], timeout=10, check=False)
        os.unlink(script_path)


# ---------------------------------------------------------------------------
# Judge: verify mutant kills F2P tests
# ---------------------------------------------------------------------------

def judge_mutant(image_name: str, code_patch: str, test_patch: str, candidate_diff: str,
                 f2p_tests: list[str], test_cmd: str, instance_id: str = "") -> dict:
    """Verify the candidate mutant causes at least one F2P test to fail."""
    if not f2p_tests:
        return {"ok": False, "reason": "no_f2p_tests"}

    container_name = f"judge-{instance_id}-{uuid.uuid4().hex[:8]}"

    def _apply_patch_in_container(container, patch_text, label):
        if not patch_text or not patch_text.strip():
            return True
        marker = f"SWE_MUTATION_{label.upper()}_PATCH_EOF"
        ps = patch_text.replace("'", "'\\''")
        cmd = f"cat > /tmp/{label}.patch << '{marker}'\n{patch_text}\n{marker}"
        _run_simple(["docker", "exec", container, "bash", "-c", cmd], timeout=30, check=True)
        r = _run_simple(["docker", "exec", container, "bash", "-c", f"git apply -p1 /tmp/{label}.patch 2>&1"], timeout=30, check=False)
        return r.returncode == 0

    try:
        _run_simple(
            ["docker", "run", "-d", "--name", container_name, "--user", "nonroot",
             "--workdir", "/testbed", image_name, "tail", "-f", "/dev/null"],
            timeout=30, check=True,
        )
        time.sleep(2)  # wait for container start

        # Reset and apply golden patches
        _run_simple(["docker", "exec", container_name, "bash", "-c", "git reset --hard && git clean -fd && git checkout ."], timeout=30, check=False)
        _run_simple(["docker", "exec", container_name, "bash", "-c", "git config --global user.email 'judge@swe-mutation.dev' && git config --global user.name 'Judge'"], timeout=10, check=False)

        if not _apply_patch_in_container(container_name, code_patch, "code"):
            return {"ok": False, "reason": "code_patch_apply_failed"}
        if not _apply_patch_in_container(container_name, test_patch, "test"):
            return {"ok": False, "reason": "test_patch_apply_failed"}
        _run_simple(["docker", "exec", container_name, "bash", "-c", "git add -A && git commit --allow-empty -m 'chore: apply baseline patches'"], timeout=30, check=False)

        if not _apply_patch_in_container(container_name, candidate_diff, "candidate"):
            return {"ok": False, "reason": "candidate_apply_failed"}

        # Run F2P tests
        if not test_cmd:
            return {"ok": False, "reason": "no_test_cmd"}

        if f2p_tests:
            if "pytest" in test_cmd:
                test_cmd = f"{test_cmd} {' '.join(f2p_tests)}"
            elif "runtests.py" in test_cmd or "django" in instance_id.lower():
                test_cmd = f"export PYTHONIOENCODING=utf-8 && export LC_ALL=C.UTF-8 && {test_cmd} {' '.join(f2p_tests)}"
            elif "mvn" in test_cmd or "gradle" in test_cmd:
                pass
            else:
                test_cmd = f"{test_cmd} {' '.join(f2p_tests)}"

        if "cargo" in test_cmd:
            test_cmd = f"source $HOME/.cargo/env 2>/dev/null || true && {test_cmd}"
        if "runtests.py" in test_cmd or "django" in instance_id.lower():
            test_cmd = f"export PYTHONIOENCODING=utf-8 && export LC_ALL=C.UTF-8 && {test_cmd}"

        r = _run_simple(
            ["docker", "exec", container_name, "bash", "-c", f"{test_cmd} 2>&1 | cat"],
            timeout=120, check=False,
        )
        output = r.stdout + r.stderr

        parsed = _parse_test_output(output)
        f2p_failed = parsed.get("failed", 0) > 0 or parsed.get("errors", 0) > 0

        return {
            "ok": f2p_failed,
            "reason": "f2p_failed" if f2p_failed else "no_f2p_failure",
            "test_results": parsed,
            "output": output[-2000:],
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "reason": "timeout"}
    except Exception as e:
        return {"ok": False, "reason": f"error: {e}"}
    finally:
        _run_simple(["docker", "rm", "-f", container_name], timeout=10, check=False)


def _parse_test_output(output: str) -> dict:
    passed = failed = errors = skipped = 0
    try:
        for line in reversed(output.split("\n")):
            line = line.strip()
            if not line:
                continue
            if line.startswith("Tests:") and "Assertions:" in line:
                m = re.search(r"Tests:\s*(\d+)", line)
                if m:
                    total = int(m.group(1))
                    fm = re.search(r"Failures:\s*(\d+)", line)
                    em = re.search(r"Errors:\s*(\d+)", line)
                    failed = int(fm.group(1)) if fm else 0
                    errors = int(em.group(1)) if em else 0
                    passed = total - failed - errors
                    break
            if "Tests run:" in line:
                m = re.search(r"Tests run:\s*(\d+)", line)
                if m:
                    total = int(m.group(1))
                    fm = re.search(r"Failures:\s*(\d+)", line)
                    em = re.search(r"Errors:\s*(\d+)", line)
                    failed = int(fm.group(1)) if fm else 0
                    errors = int(em.group(1)) if em else 0
                    passed = total - failed - errors
                    break
            if any(k in line for k in ("passed", "failed", "error", "skipped")):
                nums = re.findall(r"(\d+)\s+(passed|failed|errors?|skipped)", line)
                if nums:
                    for count, kw in nums:
                        c = int(count)
                        if kw == "passed":      passed = c
                        elif kw == "failed":    failed = c
                        elif kw in ("error", "errors"): errors = c
                        elif kw == "skipped":   skipped = c
                    break
            if "Ran" in line and "test" in line:
                m = re.search(r"Ran\s+(\d+)\s+test", line)
                if m:
                    total = int(m.group(1))
                    lines = output.split("\n")
                    idx = next((i for i, l in enumerate(lines) if line in l), -1)
                    for nl in lines[idx + 1:idx + 6] if idx >= 0 else []:
                        nl = nl.strip()
                        if re.match(r"^OK", nl):
                            passed = total; break
                        fm2 = re.match(r"^FAILED\s+\((.+)\)$", nl)
                        if fm2:
                            parts = fm2.group(1)
                            f3 = re.search(r"failures?=(\d+)", parts)
                            e3 = re.search(r"errors?=(\d+)", parts)
                            s3 = re.search(r"skipped=(\d+)", parts)
                            failed = int(f3.group(1)) if f3 else 0
                            errors = int(e3.group(1)) if e3 else 0
                            skipped = int(s3.group(1)) if s3 else 0
                            passed = max(0, total - failed - errors - skipped)
                            break
                    break
    except Exception:
        pass
    return {"passed": passed, "failed": failed, "errors": errors, "skipped": skipped,
            "total": passed + failed + errors + skipped}


# ---------------------------------------------------------------------------
# Patch extraction helpers
# ---------------------------------------------------------------------------

def _filesystem_diff(worktree_path):
    _run_simple(["git", "add", "-N", "."], cwd=worktree_path, timeout=10, check=False)
    r = _run_simple(["git", "diff"], cwd=worktree_path, timeout=10, check=False)
    return r.stdout.strip()


def _validate_patch(patch, worktree_path):
    if not patch:
        return False, "empty patch"
    worktree_resolved = worktree_path.resolve()
    for line in patch.split('\n'):
        if line.startswith('--- a/') or line.startswith('+++ b/'):
            file_path = line[6:]
            if file_path == '/dev/null':
                continue
            full_path = (worktree_path / file_path).resolve()
            try:
                full_path.relative_to(worktree_resolved)
            except ValueError:
                return False, f"file outside workspace: {file_path}"
    return True, None


def extract_allowed_files_from_patch(patch_text: str) -> list[str]:
    files = []
    for line in patch_text.splitlines():
        if line.startswith("+++ b/"):
            path = line[6:].strip()
            if path and path not in files and path != "/dev/null":
                files.append(path)
    return files


def extract_patch(stdout, worktree_path):
    m = re.search(
        r"^\s*<patch>\s*\n?(.*?)\n?\s*</patch>\s*$",
        stdout, re.MULTILINE | re.DOTALL,
    )
    if m:
        patch = m.group(1).strip()
        is_valid, reason = _validate_patch(patch, worktree_path)
        if is_valid:
            return patch
        print(f"  WARNING: patch rejected - {reason}")

    m = re.search(r"(diff --git .+)", stdout, re.DOTALL)
    if m:
        patch = m.group(1).strip()
        is_valid, reason = _validate_patch(patch, worktree_path)
        if is_valid:
            return patch
        print(f"  WARNING: raw diff rejected - {reason}")

    patch = _filesystem_diff(worktree_path)
    is_valid, reason = _validate_patch(patch, worktree_path)
    if is_valid:
        return patch
    return ""


def is_valid_patch(stdout, patch):
    if not patch:
        return False, "empty patch"
    if "<patch>" not in stdout:
        return False, "agent did not output <patch> tag"
    m = re.search(r"^\s*<patch>\s*\n?(.*?)\n?\s*</patch>\s*$", stdout, re.MULTILINE | re.DOTALL)
    if m:
        tag_content = m.group(1).strip()
        if not tag_content:
            return False, "empty patch tags"
        if not tag_content.startswith("diff --git"):
            return False, "patch does not contain valid git diff"
    return True, None


# ---------------------------------------------------------------------------
# Main logic: generate mutants
# ---------------------------------------------------------------------------

def generate_mutants(args):
    run_ts = time.strftime("%Y%m%d_%H%M%S")
    model_safe = args.model.replace("/", "_")
    run_tag = args.run_id if args.run_id else run_ts

    model_name = f"opencode__{model_safe}"
    output_path = Path(args.output) if args.output else (
        PREDICTIONS_DIR / f"{model_name}__{run_tag}" / "preds.json"
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)

    repo_cache_dir = Path(args.repo_cache) if args.repo_cache else REPO_CACHE_DEFAULT
    repo_cache_dir.mkdir(parents=True, exist_ok=True)
    workspace_dir = Path(args.workspace_dir) if args.workspace_dir else WORKSPACE_DIR_DEFAULT
    run_workspace = workspace_dir / run_tag
    run_workspace.mkdir(parents=True, exist_ok=True)

    completed_ids = get_completed_ids(output_path)
    is_resume = len(completed_ids) > 0

    # Load instances: HF dataset, or local patches file if --patches-file is given
    if args.patches_file:
        patches_file = Path(args.patches_file)
        if not patches_file.exists():
            print(f"Patches file not found: {patches_file}")
            sys.exit(1)
        instances = load_patches(patches_file)
        all_instances = [{"instance_id": iid, **instances[iid]} for iid in instances]
    else:
        dataset_name = args.dataset or DEFAULT_DATASET
        print(f"Loading dataset: {dataset_name}")
        from datasets import load_dataset as hf_load
        all_instances = list(hf_load(dataset_name, split="test"))

    # Extract allowed_files from patch if not already present
    for inst in all_instances:
        if not inst.get("files") and inst.get("patch"):
            inst["files"] = extract_allowed_files_from_patch(inst["patch"])
        if not inst.get("problem_statement") and inst.get("issue"):
            inst["problem_statement"] = inst["issue"]

    all_ids = [inst["instance_id"] for inst in all_instances]
    id_to_instance = {inst["instance_id"]: inst for inst in all_instances}

    print(f"\nRun ID  : {run_tag}")
    if not args.run_id:
        print(f"  (auto-generated; to resume: --run-id {run_tag})")
    print(f"Dataset : {args.dataset or DEFAULT_DATASET}")
    print(f"Output  : {output_path}")
    print(f"Mode    : SKILL-BASED MUTATION")
    if is_resume:
        print(f"Status  : RESUME ({len(completed_ids)} already completed)")

    if args.instance_ids:
        instance_ids = [iid for iid in args.instance_ids if iid in id_to_instance and iid not in completed_ids]
    else:
        instance_ids = [iid for iid in all_ids if iid not in completed_ids]

    if args.max_instances:
        instance_ids = instance_ids[:args.max_instances]

    print(f"Total instances : {len(all_ids)}")
    print(f"Already done    : {len(completed_ids)}")
    print(f"To process      : {len(instance_ids)}")
    if not instance_ids:
        print("Nothing to do.")
        return output_path

    skill_name = args.skill

    prompt_template = PROMPT_SKILL_PATH.read_text() if PROMPT_SKILL_PATH.exists() else ""
    if not prompt_template:
        print(f"Prompt template not found at {PROMPT_SKILL_PATH}")
        sys.exit(1)

    # Cache inference images across instances that share the same base image
    image_cache = {}

    success_ids = []
    failed_ids = []

    for idx, instance_id in enumerate(instance_ids):
        instance = id_to_instance[instance_id]
        repo = instance["repo"]
        base_commit = instance["base_commit"]
        issue = instance["problem_statement"]
        version = instance.get("version", "")
        code_patch = instance.get("patch", "")
        test_patch = instance.get("test_patch", "")
        test_files = instance.get("test_files", [])
        allowed_files = instance.get("files", [])
        f2p_tests = instance.get("FAIL_TO_PASS", [])

        print(f"\n{'=' * 60}")
        print(f"[{idx + 1}/{len(instance_ids)}] {instance_id}")
        print(f"  Repo: {repo}  Version: {version}  Commit: {base_commit[:8]}")

        if not code_patch:
            print(f"  SKIP: no golden patch")
            failed_ids.append(instance_id)
            continue

        worktree_path = None
        try:
            repo_path = ensure_repo(repo, repo_cache_dir)
            worktree_path = setup_workspace(repo_path, base_commit, instance_id, run_workspace)
            write_issue(worktree_path, issue)

            # Apply golden patches
            print(f"  Applying golden patches ...")
            if not apply_patches_in_workspace(worktree_path, code_patch, test_patch):
                print(f"  FAILED to apply golden patches")
                failed_ids.append(instance_id)
                continue
            _run_simple(["git", "add", "-A", "&&", "git", "commit", "--allow-empty", "-m", "baseline"],
                        cwd=worktree_path, timeout=30, check=False)

            # Get Docker image
            image_name = instance.get("image_name") or get_swebench_docker_image_name(instance)

            # Build inference image (cache keyed by src image)
            if image_name not in image_cache:
                try:
                    ensure_src_instance_image({"instance_id": instance_id, "image_name": image_name})
                    image_cache[image_name] = build_inference_image(image_name)
                except Exception as e:
                    print(f"  FAILED to build inference image: {e}")
                    failed_ids.append(instance_id)
                    continue
            inference_image = image_cache[image_name]

            test_cmd = get_instance_test_cmd(instance)

            # Run per-strategy-group rounds
            instance_mutations = []
            for round_idx, (group_code, group_name, strategies) in enumerate(STRATEGY_GROUPS, 1):
                print(f"\n  --- Round {round_idx}/5: {group_name} ---")

                prompt = prompt_template.format(
                    skill=skill_name,
                    strategy_group=group_code,
                    strategy_group_name=group_name,
                    allowed_strategies=str(strategies),
                    issue=issue,
                    allowed_files=str(allowed_files),
                    test_files=str(test_files),
                )

                retries = args.retry_limit + 1
                accepted = False
                candidate_diff = ""
                judge_result = {}
                attempts = 0

                for attempt in range(1, retries + 1):
                    attempts = attempt
                    print(f"    Attempt {attempt}/{retries} ...")

                    # Run opencode
                    t0 = time.time()
                    result, session_export = run_opencode(
                        inference_image, prompt, args.model, args.timeout or DEFAULT_TIMEOUT,
                        instance_id, args.agent,
                        opencode_config=OPCODE_CONFIG_PATH,
                        workspace_mount=str(worktree_path),
                    )
                    elapsed = time.time() - t0
                    print(f"    opencode finished (exit={result.returncode}, elapsed={elapsed:.0f}s)")

                    # Save session export
                    if session_export and session_export.exists():
                        session_dst = run_workspace / instance_id / f"round{round_idx}_{group_code}_session.json"
                        session_dst.parent.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(session_export), str(session_dst))

                    # Extract candidate patch
                    candidate_diff = extract_patch(result.stdout, worktree_path)

                    if not candidate_diff:
                        print(f"    No valid patch extracted")
                        continue

                    print(f"    Candidate patch: {len(candidate_diff)} chars")

                    # Judge verification
                    print(f"    Running Judge (F2P verification) ...")
                    judge_result = judge_mutant(
                        image_name, code_patch, test_patch, candidate_diff,
                        f2p_tests, test_cmd, instance_id,
                    )

                    if judge_result.get("ok"):
                        accepted = True
                        print(f"    ✓ Mutant accepted (F2P failed)")
                        break
                    else:
                        print(f"    ✗ Judge rejected: {judge_result.get('reason')}")
                        if attempt < retries:
                            print(f"    Retrying with new attempt...")

                instance_mutations.append({
                    "round": round_idx,
                    "strategy_group": group_code,
                    "strategy_group_name": group_name,
                    "allowed_strategies": strategies,
                    "accepted": accepted,
                    "diff": candidate_diff if accepted else "",
                    "judge_result": judge_result,
                    "attempts": attempts,
                })

            # Save instance results
            n_accepted = sum(1 for m in instance_mutations if m.get("accepted"))
            print(f"\n  Instance summary: {n_accepted}/{len(instance_mutations)} mutants accepted")

            if n_accepted > 0:
                pred_entry = {
                    "model_name_or_path": model_name,
                    "instance_id": instance_id,
                    "model_patch": json.dumps({
                        "mutations": [m for m in instance_mutations if m.get("accepted")],
                    }, ensure_ascii=False),
                }

                existing = {}
                if output_path.exists():
                    existing = json.loads(output_path.read_text())
                existing[instance_id] = pred_entry
                output_path.write_text(json.dumps(existing, indent=2, ensure_ascii=False))
                print(f"  ✓ Saved to {output_path}")
                success_ids.append(instance_id)
            else:
                print(f"  ✗ No accepted mutants")
                failed_ids.append(instance_id)

        except subprocess.TimeoutExpired:
            print(f"  ✗ TIMEOUT")
            failed_ids.append(instance_id)
        except Exception:
            print(f"  ✗ Error: {traceback.format_exc()}")
            failed_ids.append(instance_id)
        finally:
            if worktree_path is not None:
                cleanup_workspace(worktree_path)

    total_run = len(success_ids) + len(failed_ids)
    print(f"\n{'=' * 60}")
    print(f"Mutation Summary:")
    print(f"  Total  : {total_run}")
    print(f"  Success: {len(success_ids)}")
    print(f"  Failed : {len(failed_ids)}")
    if failed_ids:
        print(f"  Failed IDs: {', '.join(failed_ids)}")
    print(f"Output → {output_path}")

    return output_path


# ---------------------------------------------------------------------------
# Main logic: evaluate test suites
# ---------------------------------------------------------------------------

def run_eval(args):
    """Evaluate generated test suites against mutants (Pass@1, VRR, RDR)."""
    from evaluation.evaluate import evaluate as eval_main

    output_dir = Path(args.eval_output) if args.eval_output else (
        EVAL_DIR_DEFAULT / f"eval_{args.task}"
    )

    agg = eval_main(
        patches_file=Path(args.patches_file),
        mutants_file=Path(args.mutants_file),
        test_preds_file=Path(args.test_preds_file),
        task=args.task,
        output_dir=output_dir,
        workers=args.workers,
        filter_spec=args.filter_spec,
        timeout=args.timeout or DEFAULT_TIMEOUT,
    )
    return agg


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="SWE-Mutation inference using opencode + skill"
    )
    parser.add_argument("--mode", default="generate_mutants",
                        choices=["generate_mutants", "run_eval", "all"],
                        help="Pipeline mode")
    parser.add_argument("--dataset", default=DEFAULT_DATASET,
                        help=f"HuggingFace dataset (default: {DEFAULT_DATASET})")
    parser.add_argument("--patches-file", default=None,
                        help="JSONL file with instance patch data (overrides --dataset)")
    parser.add_argument("--model", default=DEFAULT_MODEL,
                        help="Model name for opencode")
    parser.add_argument("--max-instances", type=int, default=None,
                        help="Max instances to process")
    parser.add_argument("--output", default=None,
                        help="Output path for preds.json")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT,
                        help="Timeout per instance in seconds")
    parser.add_argument("--repo-cache", default=None,
                        help="Repo cache directory")
    parser.add_argument("--workspace-dir", default=None,
                        help="Workspace directory")
    parser.add_argument("--instance-ids", nargs="+", default=None,
                        help="Run only these instance IDs")
    parser.add_argument("--agent", default=None,
                        help="opencode agent to use")
    parser.add_argument("--run-id", default=None,
                        help="Run identifier for resume")
    parser.add_argument("--skill", default=SKILL_NAME,
                        help=f"Skill name to use (default: {SKILL_NAME})")
    parser.add_argument("--retry-limit", type=int, default=2,
                        help="Retries per round after rejection")

    # Eval-specific args
    parser.add_argument("--mutants-file", default=None,
                        help="preds.json with mutants (for eval mode)")
    parser.add_argument("--test-preds-file", default=None,
                        help="preds.json with test patches (for eval mode)")
    parser.add_argument("--task", default="test_repair",
                        choices=["test_generation", "test_repair"],
                        help="Evaluation task")
    parser.add_argument("--eval-output", default=None,
                        help="Evaluation output directory")
    parser.add_argument("--workers", type=int, default=1,
                        help="Parallel workers for evaluation")
    parser.add_argument("--filter-spec", default="",
                        help="Regex filter for instance IDs (eval)")

    args = parser.parse_args()

    if args.mode in ("generate_mutants", "all"):
        preds_path = generate_mutants(args)
        if args.mode == "all":
            args.mutants_file = str(preds_path)

    if args.mode in ("run_eval", "all"):
        if not args.mutants_file or not args.test_preds_file:
            print("Error: --mutants-file and --test-preds-file required for eval mode")
            sys.exit(1)
        run_eval(args)


if __name__ == "__main__":
    main()
