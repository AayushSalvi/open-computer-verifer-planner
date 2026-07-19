#!/usr/bin/env python3
"""
process_checks/runner/synthetic_golden.py

Deterministic golden-state validation for vscode_keybindings_and_settings_combo
— no agent involved. Writes known-good config files into a fresh sandbox, then
runs BOTH the unchanged outcome verifier (verify_task) and our checkpoints, and
asserts the expected verdicts. This decouples "are the checkpoints correct?"
from "did the agent happen to succeed?" (a dice roll costing 10-20 min/attempt).

Two variants, one sandbox:

  plain  — settings.json and keybindings.json as strict JSON.
           Expected: our 6/6 PASS, outcome 8/8, outcome_consistent True.
           This validates PROJECT.md gate 4 (consistency) NON-vacuously.

  jsonc  — same content, but keybindings.json carries VSCode's own default
           comment header ("// Place your key bindings...") — the file VSCode
           itself creates when you open Keyboard Shortcuts (JSON).
           Expected: our 6/6 PASS, outcome only 3/8 — the outcome verifier's
           strict json.loads rejects a file VSCode loads fine. This documents
           the oracle's false negative: it scores a PERFECT config the same
           3/8 as the genuinely-broken 2026-07-18 agent run. When this variant
           reports outcome_consistent False, the defect is in the oracle, not
           in our decomposition (PROJECT.md: consistency is a sanity check,
           not a correctness proof).

Usage (server-side, env `opencomputer`, repo root):
    python -m process_checks.runner.synthetic_golden --env-backend docker

Exit code 0 iff every expectation holds. Records land in
process_checks/runs/synthetic_golden_<ts>/record_<variant>.json.
"""
from __future__ import annotations

import argparse
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

TASK_ID = "vscode_keybindings_and_settings_combo"
APP = "vscode"
CONFIG_DIR = "/home/user/.config/Code/User"
SETTINGS_PATH = f"{CONFIG_DIR}/settings.json"
KEYBINDS_PATH = f"{CONFIG_DIR}/keybindings.json"

SETTINGS_PLAIN = """{
    "editor.fontSize": 14,
    "editor.tabSize": 4,
    "editor.cursorStyle": "block"
}"""

KEYBINDS_BODY = """[
    { "key": "ctrl+shift+n", "command": "workbench.action.files.newUntitledFile" },
    { "key": "ctrl+shift+w", "command": "workbench.action.closeAllEditors" },
    { "key": "ctrl+alt+s",   "command": "workbench.action.files.saveAll" }
]"""

VARIANTS = {
    "plain": {
        "settings": SETTINGS_PLAIN,
        "keybindings": KEYBINDS_BODY,
        # Consistency gate, non-vacuous: everything must agree.
        "expect_ours_all_pass": True,
        "expect_outcome_all_pass": True,
    },
    "jsonc": {
        "settings": SETTINGS_PLAIN,
        "keybindings": (
            "// Place your key bindings in this file to override the defaults\n"
            + KEYBINDS_BODY
        ),
        # VSCode loads this file fine; the outcome verifier's strict parser
        # does not. Ours must pass; the outcome verifier is EXPECTED to drop
        # its 5 keybinding checks (-> 3/8). That mismatch is the documented
        # oracle false negative, not a checkpoint bug.
        "expect_ours_all_pass": True,
        "expect_outcome_all_pass": False,
    },
}


def _write_file(sandbox, path: str, content: str) -> None:
    sandbox.commands.run(f"mkdir -p {CONFIG_DIR}", timeout=15)
    sandbox.files.write(path, content)


def run(env_backend, docker_image, docker_platform, docker_shm_size,
        docker_memory, docker_cpus, docker_ready_timeout, sandbox_timeout) -> int:
    with open(TASKS_DIR / TASK_ID / "task.json") as f:
        task = json.load(f)

    checkpoints_dir = REPO_ROOT / "process_checks" / APP / TASK_ID
    out_dir = REPO_ROOT / "process_checks" / "runs" / f"synthetic_golden_{datetime.now():%Y%m%d_%H%M%S}"
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"{'=' * 70}\n  Synthetic golden validation: {TASK_ID}\n{'=' * 70}")
    session = setup_sandbox_session(
        APP, task, sandbox_timeout,
        run_id=f"synthetic_golden_{datetime.now():%H%M%S}",
        env_backend=env_backend, docker_image=docker_image,
        docker_platform=docker_platform, docker_shm_size=docker_shm_size,
        docker_memory=docker_memory, docker_cpus=docker_cpus,
        docker_ready_timeout=docker_ready_timeout,
    )
    sandbox = session.sandbox
    failures = 0

    try:
        for name, spec in VARIANTS.items():
            print(f"\n--- variant: {name} ---")
            _write_file(sandbox, SETTINGS_PATH, spec["settings"])
            _write_file(sandbox, KEYBINDS_PATH, spec["keybindings"])

            passed, total, _ = verify_task(sandbox, APP, task["verification"])
            outcome_all = passed == total
            print(f"  outcome verifier: {passed}/{total}")

            conditions = run_checkpoints(sandbox, APP, checkpoints_dir)
            ours_all = all(c["pass"] for c in conditions)
            for c in conditions:
                print(f"    {'PASS' if c['pass'] else 'FAIL'} [{c['status']}] {c['id']:16s} {c['evidence'][:90]}")

            record = {
                "task_id": TASK_ID,
                "variant": name,
                "conditions": conditions,
                "outcome_consistent": (not ours_all) or outcome_all,
                "outcome": {"checks_passed": passed, "checks_total": total},
            }
            with open(out_dir / f"record_{name}.json", "w") as f:
                json.dump(record, f, indent=2, default=str)

            ok = (ours_all == spec["expect_ours_all_pass"]
                  and outcome_all == spec["expect_outcome_all_pass"])
            if not ok:
                failures += 1
            print(f"  ours all-pass={ours_all} (want {spec['expect_ours_all_pass']}), "
                  f"outcome all-pass={outcome_all} (want {spec['expect_outcome_all_pass']}) "
                  f"-> {'OK' if ok else 'MISMATCH'}")
            if name == "jsonc" and ours_all and not outcome_all:
                print("  NOTE: outcome verifier rejected a VSCode-valid JSONC file "
                      "-- documented oracle false negative, not a checkpoint bug.")
    finally:
        try:
            sandbox.kill()
        except Exception as exc:
            print(f"  WARNING: failed to kill sandbox: {exc}")

    print(f"\n{'ALL EXPECTATIONS HOLD' if failures == 0 else f'{failures} VARIANT(S) MISMATCHED'}")
    print(f"records: {out_dir}")
    return 0 if failures == 0 else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--env-backend", choices=["e2b", "docker", "remote_docker"], default=DEFAULT_ENV_BACKEND)
    parser.add_argument("--docker-image", default=DEFAULT_DOCKER_IMAGE)
    parser.add_argument("--docker-platform", default=DEFAULT_DOCKER_PLATFORM)
    parser.add_argument("--docker-shm-size", default=DEFAULT_DOCKER_SHM_SIZE)
    parser.add_argument("--docker-memory", default=DEFAULT_DOCKER_MEMORY)
    parser.add_argument("--docker-cpus", default=DEFAULT_DOCKER_CPUS)
    parser.add_argument("--docker-ready-timeout", type=int, default=DEFAULT_DOCKER_READY_TIMEOUT)
    parser.add_argument("--sandbox-timeout", type=int, default=DEFAULT_SANDBOX_TIMEOUT)
    args = parser.parse_args()
    sys.exit(run(args.env_backend, args.docker_image, args.docker_platform,
                 args.docker_shm_size, args.docker_memory, args.docker_cpus,
                 args.docker_ready_timeout, args.sandbox_timeout))


if __name__ == "__main__":
    main()
