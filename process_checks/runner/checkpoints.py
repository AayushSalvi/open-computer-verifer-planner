"""
process_checks/runner/checkpoints.py

Fires a task's checkpoints.json against a *live* sandbox. Never edits
evaluation/runtime/* — pure reuse.

Three resolution lanes per checkpoint:

  1. "command" + "key"/"expected"  — run a verifiers/<app>.py check-* endpoint
     via the same run_verifier() path verify_task() uses; assert data[key].
  2. "command" + "eval"            — same channel, arbitrary predicate over the
     parsed JSON (bound to `result`).
  3. "jsonc_file" + "eval"         — read the raw file from the sandbox and
     parse it with process_checks.lib.jsonc (VSCode-tolerance JSONC), then
     evaluate the predicate over the parsed value (bound to `result`).

Lane 3 exists because VSCode's settings.json / keybindings.json are JSONC
(comment headers, trailing commas) and the verifier's strict json.loads
reports a perfectly VSCode-valid file as unreadable — a false negative that
flips the RL reward sign. Confirmed live 2026-07-18.

Verdict semantics (the `status` field — see PROJECT.md interface contract):

  status "ok"          — the read was determinate; `pass` is a real verdict.
                         Includes: file missing (nothing configured -> FAIL)
                         and file malformed-even-as-JSONC (VSCode ignores the
                         file, so the milestone is genuinely unmet -> FAIL,
                         with the raw content preserved as evidence).
  status "unreadable"  — we could not observe the state (sandbox/command
                         error). `pass` is False but it is NOT a verdict; the
                         RL consumer must not treat it as a confirmed FAIL.
"""
from __future__ import annotations

import json
from pathlib import Path

from computer_env.backends.base import CommandExitException
from evaluation.runtime.verification import run_verifier

from process_checks.lib.jsonc import read_jsonc_text


def _load_checkpoints(checkpoints_dir) -> dict:
    path = Path(checkpoints_dir)
    if path.is_dir():
        path = path / "checkpoints.json"
    with open(path) as f:
        return json.load(f)


def _eval_predicate(expr: str, result) -> tuple[bool, str | None]:
    """Evaluate a checkpoint predicate. Returns (verdict, error)."""
    try:
        return bool(eval(expr)), None  # noqa: S307 — same pattern verify_task uses
    except Exception as exc:
        return False, f"eval error: {exc}"


#: Cap on captured probe output. Config files are small; this only guards
#: against a checkpoint pointed at something unexpectedly large.
PROBE_CAPTURE_LIMIT = 4000


def _resolve_jsonc_file(sandbox, check: dict, capture_probes: bool = False) -> dict:
    checkpoint_id = check["id"]
    channel = check.get("channel", "file")
    path = check["jsonc_file"]

    try:
        cmd_result = sandbox.commands.run(f"cat {path}", timeout=15)
        raw_text = cmd_result.stdout
    except CommandExitException as exc:
        stderr = (exc.stderr or "").strip()
        if "No such file" in stderr:
            # Determinate: the file does not exist, so nothing is configured.
            return {
                "id": checkpoint_id,
                "pass": False,
                "status": "ok",
                "evidence": f"{path} not present (nothing configured)",
                "channel": channel,
            }
        return {
            "id": checkpoint_id,
            "pass": False,
            "status": "unreadable",
            "evidence": f"could not read {path}: {stderr[:120]}",
            "channel": channel,
        }
    except Exception as exc:
        return {
            "id": checkpoint_id,
            "pass": False,
            "status": "unreadable",
            "evidence": f"could not read {path}: {exc}",
            "channel": channel,
        }

    parsed = read_jsonc_text(raw_text)
    if not parsed["ok"]:
        # Determinate FAIL: VSCode cannot load this file either, so the
        # configured state is genuinely not in effect. Keep the raw head as
        # evidence — the sandbox is destroyed at run end, so this is the only
        # surviving diagnostic.
        rec = {
            "id": checkpoint_id,
            "pass": False,
            "status": "ok",
            "evidence": f"{parsed['error'][:100]} | raw: {parsed['raw'][:150]!r}",
            "channel": channel,
        }
        if capture_probes:
            # The most valuable trace of all: the file the screen showed as
            # fine, and the parse failure that proves it is not.
            rec["probe"] = {"kind": "read_file", "path": path, "parse": "jsonc"}
            rec["probe_result"] = (raw_text or "")[:PROBE_CAPTURE_LIMIT]
            rec["probe_result_truncated"] = len(raw_text or "") > PROBE_CAPTURE_LIMIT
        return rec

    result = parsed["data"]
    verdict, err = _eval_predicate(check["eval"], result)
    if err:
        return {
            "id": checkpoint_id,
            "pass": False,
            "status": "unreadable",
            "evidence": err,
            "channel": channel,
        }
    desc = check.get("description", checkpoint_id)
    record = {
        "id": checkpoint_id,
        "pass": verdict,
        "status": "ok",
        "evidence": f"{desc} => {'confirmed' if verdict else 'not found'}",
        "channel": channel,
    }
    if capture_probes:
        # The gold probe trace: what a correct verifier should look at, and
        # what it would find. This is the demonstration half of an SFT
        # example -- the verdict alone does not teach the behaviour.
        raw = raw_text or ""
        record["probe"] = {"kind": "read_file", "path": path, "parse": "jsonc"}
        record["probe_result"] = raw[:PROBE_CAPTURE_LIMIT]
        record["probe_result_truncated"] = len(raw) > PROBE_CAPTURE_LIMIT
    return record


def _resolve_command(sandbox, app_name: str, check: dict) -> dict:
    checkpoint_id = check["id"]
    channel = check.get("channel", "file")
    data = run_verifier(sandbox, app_name, check["command"])
    verifier_errored = isinstance(data, dict) and "error" in data

    if "eval" in check:
        result = data
        verdict, err = _eval_predicate(check["eval"], result)
        if err:
            return {"id": checkpoint_id, "pass": False, "status": "unreadable",
                    "evidence": err, "channel": channel}
        if verifier_errored:
            # The endpoint could not read the state; the predicate's False is
            # not a real verdict.
            return {"id": checkpoint_id, "pass": False, "status": "unreadable",
                    "evidence": f"verifier error: {str(data['error'])[:120]}",
                    "channel": channel}
        desc = check.get("description", checkpoint_id)
        return {"id": checkpoint_id, "pass": verdict, "status": "ok",
                "evidence": f"{desc} => {'confirmed' if verdict else 'not found'}",
                "channel": channel}

    key = check["key"]
    expected = check["expected"]
    actual = data.get(key) if isinstance(data, dict) else None
    passed = actual == expected
    if verifier_errored:
        return {"id": checkpoint_id, "pass": False, "status": "unreadable",
                "evidence": f"verifier error: {str(data['error'])[:120]}",
                "channel": channel}
    evidence = f"{key}={actual!r}" + ("" if passed else f" (expected {expected!r})")
    return {"id": checkpoint_id, "pass": passed, "status": "ok",
            "evidence": evidence, "channel": channel}


def _resolve(sandbox, app_name: str, check: dict, capture_probes: bool = False) -> dict:
    if "jsonc_file" in check:
        return _resolve_jsonc_file(sandbox, check, capture_probes=capture_probes)
    return _resolve_command(sandbox, app_name, check)


def run_checkpoints(sandbox, app_name: str, checkpoints_dir,
                    capture_probes: bool = False) -> list[dict]:
    """Fire every checkpoint for a task against the given live sandbox.

    Returns the `conditions` array from the PROJECT.md interface contract:
    a list of {id, pass, status, evidence, channel}. `outcome_consistent`
    is the caller's job (see run_task_with_checkpoints.py).

    `capture_probes=True` additionally records, per condition, the probe that
    produced the verdict and what it returned. That is the demonstration half
    of an SFT example -- a verdict on its own shows the answer but not the
    behaviour that reaches it. Off by default: the RL interface record should
    stay small, and probe output can carry file contents.
    """
    spec = _load_checkpoints(checkpoints_dir)
    return [_resolve(sandbox, app_name, check, capture_probes=capture_probes)
            for check in spec["checkpoints"]]
