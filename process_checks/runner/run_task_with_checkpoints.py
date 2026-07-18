#!/usr/bin/env python3
"""
process_checks/runner/run_task_with_checkpoints.py

Thin driver that composes OpenComputer's existing runtime functions — exactly
the same composition evaluation/runtime/task_runner.py::run_single_task() uses
— plus one extra call, process_checks.runner.checkpoints.run_checkpoints(),
inserted between the agent finishing and the sandbox dying.

Does NOT edit evaluation/runtime/*.py. Imports and calls its public functions
in sequence, the same way task_runner.py itself does — pure composition, per
PROJECT.md's isolation rule.

Usage (server-side, env `opencomputer`, run from the repo root):
    python -m process_checks.runner.run_task_with_checkpoints \\
        --app vscode --task vscode_keybindings_and_settings_combo \\
        --model qwen3.5-27b --endpoint-port 8012 \\
        --env-backend docker

Writes <out-dir>/<task_id>/record.json containing the PROJECT.md interface
record — {"task_id", "conditions"[], "outcome_consistent"} — alongside the
unchanged outcome summary (passed/total from verify_task) for side-by-side
comparison. verify_task() itself is untouched and stays the consistency
oracle; this script does not replace it, only runs alongside it.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

try:
    import dotenv
except ModuleNotFoundError:
    dotenv = None

if dotenv is not None:
    dotenv.load_dotenv()

from computer_env import (  # noqa: E402
    DEFAULT_DOCKER_CPUS,
    DEFAULT_DOCKER_IMAGE,
    DEFAULT_DOCKER_MEMORY,
    DEFAULT_DOCKER_PLATFORM,
    DEFAULT_DOCKER_READY_TIMEOUT,
    DEFAULT_DOCKER_SHM_SIZE,
    DEFAULT_ENV_BACKEND,
)
from evaluation.runtime.agent_runner import run_agent_on_task  # noqa: E402
from evaluation.runtime.run_config import (  # noqa: E402
    DEFAULT_MAX_ITERATIONS,
    DEFAULT_MODEL,
    DEFAULT_SANDBOX_TIMEOUT,
    TASKS_DIR,
)
from evaluation.runtime.sandbox_session import setup_sandbox_session  # noqa: E402
from evaluation.runtime.verification import verify_task  # noqa: E402

from process_checks.runner.checkpoints import run_checkpoints  # noqa: E402


def _load_task(task_id: str) -> dict:
    task_path = TASKS_DIR / task_id / "task.json"
    with open(task_path) as f:
        return json.load(f)


def run(
    app_name: str,
    task_id: str,
    model_name: str,
    out_dir: Path,
    max_iterations: int = DEFAULT_MAX_ITERATIONS,
    sandbox_timeout: int = DEFAULT_SANDBOX_TIMEOUT,
    env_backend: str = DEFAULT_ENV_BACKEND,
    docker_image: str = DEFAULT_DOCKER_IMAGE,
    docker_platform: str = DEFAULT_DOCKER_PLATFORM,
    docker_shm_size: str = DEFAULT_DOCKER_SHM_SIZE,
    docker_memory: str | None = DEFAULT_DOCKER_MEMORY,
    docker_cpus: str | None = DEFAULT_DOCKER_CPUS,
    docker_ready_timeout: int = DEFAULT_DOCKER_READY_TIMEOUT,
) -> dict:
    task = _load_task(task_id)
    task_text = task["task"]
    run_id = f"process_checks_{datetime.now():%Y%m%d_%H%M%S}"

    traj_dir = out_dir / task_id
    traj_dir.mkdir(parents=True, exist_ok=True)

    checkpoints_dir = Path(__file__).resolve().parents[1] / app_name / task_id
    if not (checkpoints_dir / "checkpoints.json").exists():
        raise FileNotFoundError(
            f"No checkpoints.json for {app_name}/{task_id} at {checkpoints_dir}"
        )

    print(f"{'=' * 70}\n  Task:  {task_id}\n  App:   {app_name}\n  Model: {model_name}\n{'=' * 70}")

    session = setup_sandbox_session(
        app_name,
        task,
        sandbox_timeout,
        run_id=run_id,
        env_backend=env_backend,
        docker_image=docker_image,
        docker_platform=docker_platform,
        docker_shm_size=docker_shm_size,
        docker_memory=docker_memory,
        docker_cpus=docker_cpus,
        docker_ready_timeout=docker_ready_timeout,
    )
    sandbox = session.sandbox
    print(f"  Desktop: {session.stream_url}")

    try:
        agent_done, steps, trajectory = run_agent_on_task(
            sandbox, task_text, model_name, max_iterations, traj_dir
        )
        print(f"  Agent finished in {steps} steps (done={agent_done})")

        # Unchanged outcome path — stays the consistency oracle. Not modified,
        # not skipped: this is exactly what task_runner.run_single_task calls.
        print("  Running outcome verifier (unchanged)...")
        passed, total, details = verify_task(
            sandbox, app_name, task["verification"], trajectory=trajectory, traj_dir=traj_dir
        )
        outcome_reward = passed / total if total else 0.0
        print(f"  Outcome: {passed}/{total} checks (reward={outcome_reward:.2f})")

        # Ours — fires against the same still-alive sandbox, before teardown.
        print("  Running process checkpoints...")
        conditions = run_checkpoints(sandbox, app_name, checkpoints_dir)
        all_pass = all(c["pass"] for c in conditions)
        for c in conditions:
            status = "PASS" if c["pass"] else "FAIL"
            print(f"    {status}  {c['id']:20s} [{c['channel']}]  {c['evidence']}")

        # Consistency rule (PROJECT.md): all-pass => outcome verifier also
        # passes. One direction only — we may be legitimately stricter (see
        # checkpoints.md), so a checkpoint FAIL while the outcome verifier
        # PASSes does not violate this rule.
        outcome_consistent = (not all_pass) or (passed == total)
        if all_pass and not outcome_consistent:
            print(
                "  WARNING: all our checkpoints pass but the outcome verifier "
                "does not — the milestone decomposition may be wrong."
            )

        record = {
            "task_id": task_id,
            "conditions": conditions,
            "outcome_consistent": outcome_consistent,
        }
        outcome_summary = {
            "agent_done": agent_done,
            "agent_steps": steps,
            "checks_passed": passed,
            "checks_total": total,
            "reward": outcome_reward,
            "timestamp": datetime.now().isoformat(),
        }
        full_record = {**record, "outcome": outcome_summary}

        with open(traj_dir / "record.json", "w") as f:
            json.dump(full_record, f, indent=2, default=str)

        return full_record

    finally:
        try:
            sandbox.kill()
        except Exception as exc:
            print(f"  WARNING: failed to kill sandbox for {task_id}: {exc}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--app", required=True)
    parser.add_argument("--task", required=True)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--out-dir", default=str(REPO_ROOT / "process_checks" / "runs"))
    parser.add_argument("--max-iterations", type=int, default=DEFAULT_MAX_ITERATIONS)
    parser.add_argument("--sandbox-timeout", type=int, default=DEFAULT_SANDBOX_TIMEOUT)
    parser.add_argument("--env-backend", choices=["e2b", "docker", "remote_docker"], default=DEFAULT_ENV_BACKEND)
    parser.add_argument("--docker-image", default=DEFAULT_DOCKER_IMAGE)
    parser.add_argument("--docker-platform", default=DEFAULT_DOCKER_PLATFORM)
    parser.add_argument("--docker-shm-size", default=DEFAULT_DOCKER_SHM_SIZE)
    parser.add_argument("--docker-memory", default=DEFAULT_DOCKER_MEMORY)
    parser.add_argument("--docker-cpus", default=DEFAULT_DOCKER_CPUS)
    parser.add_argument("--docker-ready-timeout", type=int, default=DEFAULT_DOCKER_READY_TIMEOUT)
    parser.add_argument("--endpoint-port", type=int, metavar="PORT",
                         help="Local OpenAI-compatible endpoint port (sets OPENAI_BASE_URL), "
                              "e.g. --endpoint-port 8012 for the local vLLM server")
    parser.add_argument("--endpoint-url", type=str, metavar="URL",
                         help="Full OpenAI-compatible endpoint URL (overrides --endpoint-port)")
    args = parser.parse_args()

    if args.endpoint_url:
        os.environ["OPENAI_BASE_URL"] = args.endpoint_url
        print(f"Using endpoint: {args.endpoint_url}")
    elif args.endpoint_port:
        endpoint = f"http://localhost:{args.endpoint_port}/v1"
        os.environ["OPENAI_BASE_URL"] = endpoint
        print(f"Using endpoint: {endpoint}")

    result = run(
        args.app,
        args.task,
        args.model,
        Path(args.out_dir),
        max_iterations=args.max_iterations,
        sandbox_timeout=args.sandbox_timeout,
        env_backend=args.env_backend,
        docker_image=args.docker_image,
        docker_platform=args.docker_platform,
        docker_shm_size=args.docker_shm_size,
        docker_memory=args.docker_memory,
        docker_cpus=args.docker_cpus,
        docker_ready_timeout=args.docker_ready_timeout,
    )
    print("\n" + json.dumps({k: v for k, v in result.items() if k != "conditions"}, indent=2, default=str))


if __name__ == "__main__":
    main()
