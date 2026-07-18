"""
process_checks/runner/checkpoints.py

Fires a task's checkpoints.json against a *live* sandbox, using the exact same
run_verifier() channel evaluation/runtime/verification.py::verify_task() uses.
Never edits evaluation/runtime/* — pure reuse.

Resolution mirrors verify_task()'s own two lanes (key/expected, eval) so a
checkpoint that reuses a check-* endpoint behaves identically to how the
outcome verifier reads that endpoint — only the granularity (per-condition
vs. one scalar) and the moment it's invoked differ.

Errors resolve to FAIL, never a crash: an unreachable sandbox or an
unparseable check-* response yields {"error": ...} from run_verifier(), and
for both resolution styles below that naturally falls through to pass=False
(an eval expression guards on `isinstance(result, list)`; a key/expected
lookup on an error dict returns None, which never equals a truthy `expected`).
"""
from __future__ import annotations

import json
from pathlib import Path

from evaluation.runtime.verification import run_verifier


def _load_checkpoints(checkpoints_dir) -> dict:
    path = Path(checkpoints_dir)
    if path.is_dir():
        path = path / "checkpoints.json"
    with open(path) as f:
        return json.load(f)


def _resolve(sandbox, app_name: str, check: dict) -> dict:
    """Resolve one checkpoint against the live sandbox into
    {id, pass, evidence, channel} — the PROJECT.md condition record shape."""
    command = check["command"]
    data = run_verifier(sandbox, app_name, command)
    channel = check.get("channel", "file")
    checkpoint_id = check["id"]
    verifier_errored = isinstance(data, dict) and "error" in data

    if "eval" in check:
        try:
            result = data
            passed = bool(eval(check["eval"]))  # noqa: S307 — same pattern verify_task uses
        except Exception as exc:
            return {
                "id": checkpoint_id,
                "pass": False,
                "evidence": f"eval error: {exc}",
                "channel": channel,
            }
        if verifier_errored:
            evidence = f"verifier error: {str(data['error'])[:120]}"
        else:
            desc = check.get("description", checkpoint_id)
            evidence = f"{desc} => {'confirmed' if passed else 'not found'}"
        return {"id": checkpoint_id, "pass": passed, "evidence": evidence, "channel": channel}

    key = check["key"]
    expected = check["expected"]
    actual = data.get(key) if isinstance(data, dict) else None
    passed = actual == expected
    if verifier_errored:
        evidence = f"verifier error: {str(data['error'])[:120]}"
    else:
        evidence = f"{key}={actual!r}" + ("" if passed else f" (expected {expected!r})")
    return {"id": checkpoint_id, "pass": passed, "evidence": evidence, "channel": channel}


def run_checkpoints(sandbox, app_name: str, checkpoints_dir) -> list[dict]:
    """Fire every checkpoint for a task against the given live sandbox.

    Returns the `conditions` array from the PROJECT.md interface contract:
    a list of {id, pass, evidence, channel}. Does NOT compute
    `outcome_consistent` — that needs the outcome verifier's own result too,
    which is the caller's job (see run_task_with_checkpoints.py). Keeping
    this function single-purpose lets it also be reused directly by the
    gate-validation steps (golden/failing trajectory checks, mutation
    testing) without dragging in the full driver.
    """
    spec = _load_checkpoints(checkpoints_dir)
    return [_resolve(sandbox, app_name, check) for check in spec["checkpoints"]]
