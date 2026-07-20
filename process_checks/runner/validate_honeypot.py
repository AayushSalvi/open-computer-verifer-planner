#!/usr/bin/env python3
"""
process_checks/runner/validate_honeypot.py

Self-verification for honeypot variants. For each variant: plant its tamper in
a fresh sandbox, run the BASE TASK's checkpoints against the planted state, and
assert every verdict matches the variant's declared `expected_conditions` (and
`expected_status` where given).

Why this matters: a honeypot is only useful if the checkpoint set actually
catches the mismatch it plants. An uncaught honeypot silently teaches the
verifier that rubber-stamping is fine — the exact pathology it was built to
fix. This makes that failure loud instead of silent.

The base checkpoints are NOT duplicated into the honeypot directory; each
variant declares expectations against the base set, so the two cannot drift.

Usage (server-side, env `opencomputer`, repo root):
    python -m process_checks.runner.validate_honeypot --app vscode
    python -m process_checks.runner.validate_honeypot --app vscode \\
        --variant vscode_keybindings_and_settings_combo__workspace_shadow

Exit 0 iff every validated variant matches. Records land in
process_checks/runs/honeypot_validation_<ts>/.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
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
from evaluation.runtime.run_config import DEFAULT_SANDBOX_TIMEOUT, TASKS_DIR  # noqa: E402
from evaluation.runtime.sandbox_session import setup_sandbox_session  # noqa: E402
from evaluation.runtime.verification import verify_task  # noqa: E402

from process_checks.runner.checkpoints import run_checkpoints  # noqa: E402

HONEYPOTS_DIR = REPO_ROOT / "honeypots"


def _load_tamper(variant_dir: Path):
    """Import the variant's tamper.py and return its tamper() callable."""
    spec = importlib.util.spec_from_file_location(
        f"tamper_{variant_dir.name}", variant_dir / "tamper.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.tamper


def _reset_config(sandbox) -> None:
    """Clear VSCode config + planted workspace between variants so one
    variant's state cannot leak into the next one's verdicts."""
    sandbox.commands.run(
        "rm -rf /home/user/.config/Code/User/settings.json "
        "/home/user/.config/Code/User/keybindings.json "
        "/home/user/project",
        timeout=20,
    )


def validate_variant(sandbox, variant_dir: Path, out_dir: Path) -> bool:
    spec = json.loads((variant_dir / "honeypot.json").read_text())
    base_task, app = spec["base_task"], spec["app"]
    checkpoints_dir = REPO_ROOT / "process_checks" / app / base_task

    print(f"\n--- variant: {spec['variant']} ---")
    print(f"  mismatch: {spec['mismatch']}")

    _reset_config(sandbox)
    planted = _load_tamper(variant_dir)(sandbox)
    print(f"  planted: {planted['summary']}")

    conditions = run_checkpoints(sandbox, app, checkpoints_dir)
    expected = spec["expected_conditions"]
    expected_status = spec.get("expected_status", {})

    mismatches = []
    for c in conditions:
        want = expected.get(c["id"])
        if want is not None and c["pass"] != want:
            mismatches.append(
                f"    {c['id']}: pass={c['pass']}, expected {want}  [{c['evidence'][:70]}]"
            )
        want_status = expected_status.get(c["id"])
        if want_status is not None and c["status"] != want_status:
            mismatches.append(
                f"    {c['id']}: status={c['status']}, expected {want_status} "
                f"(a determinate failure must not be reported as unreadable)"
            )
        print(f"    {'PASS' if c['pass'] else 'FAIL'} [{c['status']}] {c['id']:16s} {c['evidence'][:80]}")

    # The catching conditions are the whole point: they MUST fail on a
    # successfully-planted mismatch, or the honeypot teaches nothing.
    catching = spec.get("catching_conditions", [])
    by_id = {c["id"]: c for c in conditions}
    uncaught = [cid for cid in catching if by_id.get(cid, {}).get("pass") is not False]
    if uncaught:
        mismatches.append(f"    HONEYPOT NOT CAUGHT by: {', '.join(uncaught)}")

    # Cross-check the outcome verifier too, where the variant declares it.
    outcome_note = None
    if "expected_outcome_verifier" in spec:
        with open(TASKS_DIR / base_task / "task.json") as f:
            task = json.load(f)
        passed, total, _ = verify_task(sandbox, app, task["verification"])
        want = spec["expected_outcome_verifier"]
        outcome_note = {"checks_passed": passed, "checks_total": total}
        print(f"  outcome verifier: {passed}/{total} "
              f"(variant declares {want['checks_passed']}/{want['checks_total']})")
        if passed != want["checks_passed"]:
            mismatches.append(
                f"    outcome verifier: {passed}/{total}, variant declares "
                f"{want['checks_passed']}/{want['checks_total']}"
            )

    record = {
        "variant": spec["variant"],
        "base_task": base_task,
        "planted": planted,
        "conditions": conditions,
        "outcome": outcome_note,
        "validated": not mismatches,
    }
    (out_dir / f"record_{spec['variant']}.json").write_text(
        json.dumps(record, indent=2, default=str)
    )

    if mismatches:
        print("  MISMATCH:")
        for m in mismatches:
            print(m)
    else:
        print(f"  OK — honeypot caught by {', '.join(catching)}")
    return not mismatches


def run(app, variant, **backend) -> int:
    app_dir = HONEYPOTS_DIR / app
    if not app_dir.is_dir():
        print(f"No honeypots for app '{app}' at {app_dir}")
        return 1

    variant_dirs = sorted(d for d in app_dir.iterdir()
                          if d.is_dir() and (d / "honeypot.json").exists())
    if variant:
        variant_dirs = [d for d in variant_dirs if d.name == variant]
        if not variant_dirs:
            print(f"Variant '{variant}' not found under {app_dir}")
            return 1

    # All variants share one sandbox; _reset_config isolates them.
    first_spec = json.loads((variant_dirs[0] / "honeypot.json").read_text())
    with open(TASKS_DIR / first_spec["base_task"] / "task.json") as f:
        base_task_json = json.load(f)

    out_dir = REPO_ROOT / "process_checks" / "runs" / f"honeypot_validation_{datetime.now():%Y%m%d_%H%M%S}"
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"{'=' * 70}\n  Honeypot validation: {app} ({len(variant_dirs)} variant(s))\n{'=' * 70}")
    session = setup_sandbox_session(
        app, base_task_json, backend.pop("sandbox_timeout"),
        run_id=f"honeypot_validation_{datetime.now():%H%M%S}", **backend
    )
    sandbox = session.sandbox
    failures = 0
    try:
        for d in variant_dirs:
            if not validate_variant(sandbox, d, out_dir):
                failures += 1
    finally:
        try:
            sandbox.kill()
        except Exception as exc:
            print(f"  WARNING: failed to kill sandbox: {exc}")

    print(f"\n{'ALL HONEYPOTS VALIDATED' if not failures else f'{failures} VARIANT(S) MISMATCHED'}")
    print(f"records: {out_dir}")
    return 0 if not failures else 1


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--app", default="vscode")
    p.add_argument("--variant", help="validate one variant directory by name")
    p.add_argument("--env-backend", choices=["e2b", "docker", "remote_docker"], default=DEFAULT_ENV_BACKEND)
    p.add_argument("--docker-image", default=DEFAULT_DOCKER_IMAGE)
    p.add_argument("--docker-platform", default=DEFAULT_DOCKER_PLATFORM)
    p.add_argument("--docker-shm-size", default=DEFAULT_DOCKER_SHM_SIZE)
    p.add_argument("--docker-memory", default=DEFAULT_DOCKER_MEMORY)
    p.add_argument("--docker-cpus", default=DEFAULT_DOCKER_CPUS)
    p.add_argument("--docker-ready-timeout", type=int, default=DEFAULT_DOCKER_READY_TIMEOUT)
    p.add_argument("--sandbox-timeout", type=int, default=DEFAULT_SANDBOX_TIMEOUT)
    a = p.parse_args()
    sys.exit(run(a.app, a.variant,
                 env_backend=a.env_backend, docker_image=a.docker_image,
                 docker_platform=a.docker_platform, docker_shm_size=a.docker_shm_size,
                 docker_memory=a.docker_memory, docker_cpus=a.docker_cpus,
                 docker_ready_timeout=a.docker_ready_timeout,
                 sandbox_timeout=a.sandbox_timeout))


if __name__ == "__main__":
    main()
