"""
Server-side independent validation of the auto-generated artifacts.

Everything else in process_checks/auto/ is validated OFFLINE against a fake
sandbox — which proves internal consistency but nothing about the real
environment or the human-authored verifier. This driver closes that gap on a
live Docker sandbox. It needs NO model (we plant the golden directly instead of
running an agent), only Docker.

Per task:

  GOLDEN  — the non-circular gate.
    plant the generated golden, then run the task's OWN authored verify_task()
    (its verification[]). If the golden is genuinely correct, the authored
    verifier passes ALL checks. Also run our generated checkpoints: they must
    all pass too (consistency). The authored verifier is the independent oracle
    — it never saw our spec, so its pass is real evidence, not our own tool
    agreeing with itself.

  HONEYPOTS — the discrimination gate.
    for each generated honeypot, set the sandbox to that honeypot's files, then:
      * run OUR checkpoints: the targeted checkpoint must flip to FAIL and match
        the generated `expected` vector (this is the core self-validation).
      * run the AUTHORED verify_task too, for information: if it still passes
        fully on a honeypot our checkpoint catches, that is a false-accept in
        the authored verifier that our process-check closes — exactly the
        value-add. Reported, not gated.

Reuses setup_sandbox_session / verify_task / run_checkpoints unchanged — pure
composition, no core edit.

Usage (on the server, env opencomputer):
  python -m process_checks.auto.validate_server \
      --task vscode_keybindings_and_settings_combo \
      --decompositions process_checks/auto/decomps_seed.jsonl \
      --env-backend docker
  # or let it decompose live (needs the vLLM up):
  python -m process_checks.auto.validate_server --task <id> --endpoint-port 8012
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import tempfile
from pathlib import Path

from evaluation.runtime.run_config import TASKS_DIR
from evaluation.runtime.sandbox_session import setup_sandbox_session
from evaluation.runtime.verification import verify_task

from process_checks.auto.decompose import decompose
from process_checks.auto.golden_vscode import criteria_to_golden, plant_golden_state
from process_checks.auto.honeypot_vscode import criteria_to_honeypots, plant_honeypot
from process_checks.auto.registry_vscode import criteria_to_checkpoints
from process_checks.runner.checkpoints import run_checkpoints


def _load_task(task_id: str) -> dict:
    p = TASKS_DIR / task_id / "task.json"
    return json.loads(p.read_text(encoding="utf-8"))


def _load_decomp(path: str | None, task_id: str) -> list | None:
    if not path:
        return None
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and json.loads(line).get("task_id") == task_id:
            return json.loads(line)["criteria"]
    return None


def _write_spec(spec: dict) -> Path:
    d = Path(tempfile.mkdtemp())
    (d / "checkpoints.json").write_text(json.dumps(spec), encoding="utf-8")
    return d


def _rm(sandbox, path: str) -> None:
    sandbox.commands.run(f"rm -f {shlex.quote(path)}", timeout=15)


def _set_state(sandbox, files: dict, golden_paths: set[str]) -> None:
    """Make the sandbox hold exactly `files` for the golden path set: plant each
    file, and remove any golden path this variant drops (missing_file case)."""
    for path, text in files.items():
        from process_checks.lib.plant import plant_raw
        plant_raw(sandbox, path, text)
    for path in golden_paths - set(files):
        _rm(sandbox, path)


def _authored(sandbox, app_name: str, task: dict) -> dict:
    checks = task.get("verification", [])
    llm = sum(1 for c in checks if c.get("judge") == "llm")
    passed, total, results = verify_task(sandbox, app_name, checks)
    return {"passed": passed, "total": total, "full_pass": passed == total,
            "llm_checks": llm,
            "fail_desc": [r.get("description") or r.get("command")
                          for r in results if r and not r["passed"]]}


def validate(task_id: str, criteria: list, *, app_name: str, env_backend: str,
             malformed: bool) -> dict:
    spec = criteria_to_checkpoints(task_id, criteria)
    golden = criteria_to_golden(criteria)
    golden_paths = set(golden["files"])
    hp_gen = criteria_to_honeypots(criteria, malformed=malformed)
    spec_dir = _write_spec(spec)

    task = _load_task(task_id)
    report: dict = {"task": task_id, "env_backend": env_backend,
                    "n_checkpoints": len(spec["checkpoints"]),
                    "n_golden_files": len(golden_paths),
                    "unplantable": golden["unplantable"],
                    "n_honeypots": len(hp_gen["honeypots"])}

    session = setup_sandbox_session(app_name, task=task, env_backend=env_backend)
    sandbox = session.sandbox
    try:
        # ---- GOLDEN -----------------------------------------------------
        plant_golden_state(sandbox, golden)
        authored = _authored(sandbox, app_name, task)
        ours = run_checkpoints(sandbox, app_name, spec_dir)
        ours_all_pass = all(c["pass"] for c in ours)
        report["golden"] = {
            "authored": authored,
            "our_checkpoints_pass": ours_all_pass,
            "our_fail": [c["id"] for c in ours if not c["pass"]],
            # the gate: authored oracle fully passes AND our checks agree
            "GATE": authored["full_pass"] and ours_all_pass,
        }

        # ---- HONEYPOTS --------------------------------------------------
        hp_reports = []
        for hp in hp_gen["honeypots"]:
            _set_state(sandbox, hp["files"], golden_paths)
            ours = {c["id"]: c for c in run_checkpoints(sandbox, app_name, spec_dir)}
            got = {cid: v["pass"] for cid, v in ours.items()}
            exp = {cid: p for cid, p in hp["expected"].items() if p is not None}
            # our checkpoints must match the generated expectation exactly
            our_ok = all(got.get(cid) == exp[cid] for cid in exp) \
                and got.get(hp["target"]) is False
            authored_hp = _authored(sandbox, app_name, task)
            hp_reports.append({
                "target": hp["target"], "op": hp["op"], "kind": hp["kind"],
                "note": hp["note"],
                "our_checkpoints_ok": our_ok,
                "our_verdicts": got,
                "authored_full_pass": authored_hp["full_pass"],
                "authored_score": f"{authored_hp['passed']}/{authored_hp['total']}",
                # the interesting case: authored verifier MISSES what we catch
                "false_accept_in_authored": authored_hp["full_pass"],
            })
        report["honeypots"] = hp_reports
        report["honeypots_all_ok"] = all(h["our_checkpoints_ok"] for h in hp_reports)
    finally:
        try:
            sandbox.kill()
        except Exception as exc:  # noqa: BLE001
            print(f"WARNING: sandbox kill failed: {exc}")

    report["PASS"] = bool(report["golden"]["GATE"] and report.get("honeypots_all_ok", True))
    return report


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--task", required=True)
    p.add_argument("--app", default="vscode")
    p.add_argument("--decompositions", help="JSONL {task_id, criteria}; offline, no model")
    p.add_argument("--model", default="qwen3.5-27b")
    p.add_argument("--endpoint-port", type=int)
    p.add_argument("--env-backend", default="docker")
    p.add_argument("--malformed", action="store_true",
                   help="use the inert-file honeypot family instead of value-level")
    p.add_argument("--out")
    a = p.parse_args()

    if a.endpoint_port:
        os.environ["OPENAI_BASE_URL"] = f"http://localhost:{a.endpoint_port}/v1"

    criteria = _load_decomp(a.decompositions, a.task)
    if criteria is None:
        task = _load_task(a.task)
        criteria = decompose(task.get("task", ""), model=a.model)

    report = validate(a.task, criteria, app_name=a.app,
                      env_backend=a.env_backend, malformed=a.malformed)

    g = report["golden"]
    print(f"\n=== {a.task} — server validation ===")
    print(f"checkpoints: {report['n_checkpoints']}  golden files: {report['n_golden_files']}"
          f"  honeypots: {report['n_honeypots']}")
    print(f"\nGOLDEN gate: {'PASS' if g['GATE'] else 'FAIL'}")
    print(f"  authored verify_task: {g['authored']['passed']}/{g['authored']['total']}"
          f"  (full_pass={g['authored']['full_pass']}, llm_checks={g['authored']['llm_checks']})")
    if g["authored"]["fail_desc"]:
        print(f"    authored fails: {g['authored']['fail_desc']}")
    print(f"  our checkpoints all pass: {g['our_checkpoints_pass']}"
          + (f"  (fails: {g['our_fail']})" if g["our_fail"] else ""))
    print(f"\nHONEYPOTS: {sum(h['our_checkpoints_ok'] for h in report['honeypots'])}"
          f"/{len(report['honeypots'])} caught by our checkpoints")
    for h in report["honeypots"]:
        fa = "  <-- authored MISSES (false-accept our check catches)" if h["false_accept_in_authored"] else ""
        print(f"  [{'OK' if h['our_checkpoints_ok'] else 'BAD'}] {h['target']:<12} {h['kind']:<16}"
              f" authored={h['authored_score']}{fa}")
    print(f"\nOVERALL: {'PASS' if report['PASS'] else 'FAIL'}")

    out = Path(a.out) if a.out else Path(__file__).resolve().parents[1] / "runs" / f"validate_{a.task}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"report: {out}")
    return 0 if report["PASS"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
