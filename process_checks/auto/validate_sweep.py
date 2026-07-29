"""
Breadth validation: run the whole VSCode task pool through the auto-pipeline on
live Docker sandboxes and aggregate the result.

For each task:
  decompose (live) -> checkpoints + golden + honeypots -> validate_server.validate()

The point of the sweep is to turn "the mechanism works on the seed" into "it
generalises", and to harvest every authored-verifier FALSE-ACCEPT the honeypots
expose (the collaborator-facing finding).

HONEST BOOKKEEPING — a task is only counted in the golden-gate rate if it is
actually validatable model-free and fully plantable. We separate:
  - decompose_error      : the untrained model failed to emit usable criteria
                           (truncation / spiral) — a Phase-1 baseline fact, not
                           a validation failure.
  - skipped (blocking)   : the task cannot be validated the way this driver
                           does — has judge:"llm" verification (needs a judge
                           model), or its golden has unplantable/skipped ops
                           (state we cannot fake by writing files, e.g. an
                           installed extension). Flagged, not scored.
  - validated            : ran on a sandbox; golden_gate + honeypot results real.

Resumable: appends one JSON line per task to the out file; re-running skips
tasks already recorded, so a long run survives an interruption.

Usage (server, env opencomputer):
  # live decompose against the vLLM (note: model is on 8112, not 8012):
  nohup python -m process_checks.auto.validate_sweep --endpoint-port 8112 \
      > logs/sweep.log 2>&1 &
  # a subset first, to sanity-check before the full ~30-task run:
  python -m process_checks.auto.validate_sweep --endpoint-port 8112 \
      --tasks vscode_keybindings_and_settings_combo vscode_python_snippet
  # offline (no model), from pre-supplied decompositions:
  python -m process_checks.auto.validate_sweep --decompositions decomps.jsonl
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from evaluation.runtime.run_config import TASKS_DIR

from process_checks.auto.decompose import decompose
from process_checks.auto.golden_vscode import criteria_to_golden
from process_checks.auto.registry_vscode import criteria_to_checkpoints
from process_checks.auto.validate_server import _load_task, validate


def _all_vscode_task_ids() -> list[str]:
    return sorted(p.parent.name for p in TASKS_DIR.glob("vscode_*/task.json"))


def _load_decomps(path: str | None) -> dict:
    out = {}
    if path:
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                r = json.loads(line)
                out[r["task_id"]] = r["criteria"]
    return out


def _preflight(task_id: str, criteria: list) -> tuple[dict, dict, list[str]]:
    """Structural checks before spending a sandbox. Returns (spec, golden, blocking)
    where `blocking` lists reasons this task can't be validated model-free."""
    task = _load_task(task_id)
    blocking: list[str] = []

    if any(c.get("judge") == "llm" for c in task.get("verification", [])):
        blocking.append("llm_checks")

    spec = criteria_to_checkpoints(task_id, criteria)
    if spec["deferred"]:
        blocking.append(f"deferred={len(spec['deferred'])}")  # T2/T3 or unbindable -> golden incomplete
    if spec["malformed"]:
        blocking.append(f"malformed={len(spec['malformed'])}")

    golden = criteria_to_golden(criteria)
    if golden["unplantable"]:
        blocking.append("unplantable=" + ",".join(u["op"] for u in golden["unplantable"]))
    if golden["skipped"]:
        blocking.append(f"skipped={len(golden['skipped'])}")

    return spec, golden, blocking


# stages we never retry on resume; everything else (errors) is retried
TERMINAL_OK = {"validated", "skipped"}


def _ascii(s: str) -> str:
    """Console-safe: task instructions contain arrows/unicode that a Windows
    cp1252 stdout cannot encode. Linux utf-8 is fine, but strip for portability."""
    return str(s).encode("ascii", "replace").decode("ascii")


def _authored_ops(task: dict):
    import collections
    from process_checks.auto.coverage import _CMD_TO_OP
    fams = collections.Counter()
    for check in task.get("verification", []):
        if check.get("judge") == "llm":
            fams["<llm-judge>"] += 1
            continue
        cmd = check.get("command", "")
        if cmd:
            head = cmd.split()[0]
            fams[_CMD_TO_OP.get(head, f"<unknown:{head}>")] += 1
    return fams


def explain(task_id: str, criteria: list) -> None:
    """Diagnose a task: show each decomposed criterion's fate and classify every
    skip as MODEL (bad/incomplete/hallucinated criterion), CODE (we have the op
    but a planter gap / merge error), or BY-DESIGN (genuinely unplantable)."""
    from process_checks.auto.registry_vscode import OP_DOC
    from process_checks.auto.golden_vscode import _PLANTERS, _UNPLANTABLE

    valid_ops = set(OP_DOC)
    task = _load_task(task_id)
    spec = criteria_to_checkpoints(task_id, criteria)
    golden = criteria_to_golden(criteria)

    print(f"\n=== EXPLAIN {task_id} ===")
    print(f"instruction: {_ascii(task.get('task','')[:280])}")
    print(f"\nauthored verification wants (op -> count): {dict(_authored_ops(task))}")

    print(f"\nDECOMPOSED CRITERIA ({len(criteria)}):")
    for i, c in enumerate(criteria, 1):
        if not isinstance(c, dict):
            print(f"  {i}. <non-dict criterion> {repr(c)[:50]}")
            continue
        b = c.get("bind") or {}
        print(f"  {i}. [{c.get('tier')}] {b.get('op')} {b.get('params') or {}}"
              f"   -- {_ascii(c.get('text','')[:48])}")

    print(f"\nBINDER: {len(spec['checkpoints'])} checkpoints, "
          f"{len(spec['deferred'])} deferred, {len(spec['malformed'])} malformed")
    for d in spec["deferred"]:
        print(f"  DEFERRED (no checkpoint): {d.get('criterion','')[:40]!r} — {d.get('reason','')}")

    print(f"\nPLANTER: {len(golden['files'])} files "
          f"({[p.split('/')[-1] for p in golden['files']]}), "
          f"{len(golden['unplantable'])} unplantable, {len(golden['skipped'])} skipped")
    for u in golden["unplantable"]:
        print(f"  UNPLANTABLE: op={u['op']}  -> BY-DESIGN (no file can fake this)")
    for s in golden["skipped"]:
        op, reason = s["op"], s["reason"]
        if op not in valid_ops:
            verdict = "MODEL — op not in the registry (hallucinated/unknown op)"
        elif op in _UNPLANTABLE:
            verdict = "BY-DESIGN — unplantable by file"
        elif op not in _PLANTERS:
            verdict = "CODE — registry has the op but no planter (binder/planter asymmetry)"
        elif reason == "no planter":
            verdict = "CODE — no planter for a registry op"
        elif reason.startswith("'") or "KeyError" in reason:
            verdict = "MODEL — op valid but params incomplete"
        else:
            verdict = "CODE — golden merge/serialize error (GoldenError)"
        print(f"  SKIPPED plant: op={op}  reason={reason!r}  -> {verdict}")

    # family-level: authored kinds we produced nothing for (a decompose miss)
    produced = set()
    for cp in spec["checkpoints"]:
        cmd = cp.get("command", "")
        if cmd:
            from process_checks.auto.coverage import _CMD_TO_OP
            produced.add(_CMD_TO_OP.get(cmd.split()[0]))
        else:
            f = cp.get("jsonc_file", "")
            if "keybindings.json" in f: produced.add("keybinding_bound")
            elif "/snippets/" in f: produced.add("snippet_exists")
            elif "/.vscode/settings.json" in f: produced.add("workspace_setting_equals")
            elif "settings.json" in f: produced.add("setting_equals")
    authored = {op for op in _authored_ops(task) if not op.startswith("<")}
    missed = sorted(authored - produced)
    if missed:
        print(f"\nFAMILY MISS: authored wants {missed} but decompose produced no such op")
        print("  -> MODEL (decompose failed to extract this kind) or COVERAGE "
              "(registry lacks the op). Check if the op is in the registry above.")
    else:
        print("\nFAMILY: every authored op-kind has a produced checkpoint.")


def _emit(fh, rec: dict, latest: dict) -> None:
    fh.write(json.dumps(rec) + "\n")
    fh.flush()
    latest[rec["task"]] = rec
    tag = rec.get("stage", "?")
    extra = ""
    if rec.get("stage") == "validated":
        extra = f" golden_gate={rec['golden_gate']} authored={rec['authored']} hp_ok={rec['honeypots_ok']}"
        if rec.get("false_accepts"):
            extra += f" FALSE_ACCEPTS={len(rec['false_accepts'])}"
    elif rec.get("reason"):
        extra = f" {rec['reason']}"
    elif rec.get("error"):
        extra = f" {rec['error'][:80]}"
    print(f"  [{tag}] {rec['task']}{extra}", flush=True)


def run(task_ids: list[str], *, model: str, supplied: dict, backend: str,
        malformed: bool, out_jsonl: Path) -> list[dict]:
    # latest-wins: a task's most recent line is its state. Only validated/skipped
    # are terminal; error lines are retried (a 404 or a crashed sandbox is
    # transient/fixable, not a permanent verdict).
    latest: dict[str, dict] = {}
    if out_jsonl.exists():
        for line in out_jsonl.read_text(encoding="utf-8").splitlines():
            if line.strip():
                r = json.loads(line)
                latest[r["task"]] = r
    done = {t for t, r in latest.items() if r.get("stage") in TERMINAL_OK}
    fh = out_jsonl.open("a", encoding="utf-8")
    try:
        for tid in task_ids:
            if tid in done:
                print(f"  [skip-done] {tid}", flush=True)
                continue
            rec: dict = {"task": tid}
            try:
                task = _load_task(tid)
            except FileNotFoundError:
                rec["stage"] = "load_error"
                rec["error"] = "task.json not found"
                _emit(fh, rec, latest)
                continue

            criteria = supplied.get(tid)
            if criteria is None:
                try:
                    criteria = decompose(task.get("task", ""), model=model)
                except Exception as exc:  # noqa: BLE001
                    rec["stage"] = "decompose_error"
                    rec["error"] = f"{type(exc).__name__}: {exc}"
                    _emit(fh, rec, latest)
                    continue

            spec, golden, blocking = _preflight(tid, criteria)
            rec["n_checkpoints"] = len(spec["checkpoints"])
            rec["blocking"] = blocking
            if blocking:
                rec["stage"] = "skipped"
                rec["reason"] = ";".join(blocking)
                _emit(fh, rec, latest)
                continue

            try:
                report = validate(tid, criteria, app_name="vscode",
                                  env_backend=backend, malformed=malformed)
                rec["stage"] = "validated"
                rec["golden_gate"] = report["golden"]["GATE"]
                rec["authored"] = (f'{report["golden"]["authored"]["passed"]}/'
                                   f'{report["golden"]["authored"]["total"]}')
                rec["our_fail"] = report["golden"]["our_fail"]
                rec["honeypots_ok"] = report["honeypots_all_ok"]
                rec["false_accepts"] = [
                    {"kind": h["kind"], "target": h["target"], "authored": h["authored_score"]}
                    for h in report["honeypots"] if h["false_accept_in_authored"]]
            except Exception as exc:  # noqa: BLE001
                rec["stage"] = "validate_error"
                rec["error"] = f"{type(exc).__name__}: {exc}"
            _emit(fh, rec, latest)
    finally:
        fh.close()
    return list(latest.values())


def summarise(results: list[dict]) -> dict:
    validated = [r for r in results if r.get("stage") == "validated"]
    golden_pass = [r for r in validated if r.get("golden_gate")]
    false_accepts = [r for r in validated if r.get("false_accepts")]
    return {
        "tasks": len(results),
        "validated": len(validated),
        "golden_gate_pass": len(golden_pass),
        "golden_gate_fail": [r["task"] for r in validated if not r.get("golden_gate")],
        "tasks_with_false_accepts": len(false_accepts),
        "decompose_error": [r["task"] for r in results if r.get("stage") == "decompose_error"],
        "skipped": [{"task": r["task"], "reason": r.get("reason")} for r in results if r.get("stage") == "skipped"],
        "validate_error": [{"task": r["task"], "error": r.get("error")} for r in results if r.get("stage") == "validate_error"],
    }


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--tasks", nargs="*", help="subset of task ids (default: all vscode_*)")
    p.add_argument("--model", default="Qwen/Qwen3.5-27B",
                   help="served model id; the plain 'qwen3.5-27b' alias 404s. "
                        "Confirm with: curl -s localhost:PORT/v1/models")
    p.add_argument("--endpoint-port", type=int, help="vLLM port for live decompose (model is on 8112)")
    p.add_argument("--decompositions", help="offline JSONL {task_id, criteria}; skips the model")
    p.add_argument("--env-backend", default="docker")
    p.add_argument("--malformed", action="store_true")
    p.add_argument("--out", default="process_checks/runs/vscode_sweep.jsonl")
    p.add_argument("--explain", metavar="TASK",
                   help="diagnose one task offline (decompose + classify skips), no sandbox")
    a = p.parse_args()

    if a.endpoint_port:
        os.environ["OPENAI_BASE_URL"] = f"http://localhost:{a.endpoint_port}/v1"

    if a.explain:
        supplied = _load_decomps(a.decompositions)
        criteria = supplied.get(a.explain)
        if criteria is None:
            task = _load_task(a.explain)
            criteria = decompose(task.get("task", ""), model=a.model)
        explain(a.explain, criteria)
        return 0

    known = set(_all_vscode_task_ids())
    task_ids = a.tasks or sorted(known)
    unknown = [t for t in task_ids if t not in known]
    if unknown:
        print(f"WARNING: {len(unknown)} unknown task id(s) will be skipped: {unknown}", flush=True)
        task_ids = [t for t in task_ids if t in known]
    supplied = _load_decomps(a.decompositions)
    out_jsonl = Path(a.out)
    out_jsonl.parent.mkdir(parents=True, exist_ok=True)

    print(f"sweeping {len(task_ids)} VSCode task(s); resumable -> {out_jsonl}\n", flush=True)
    results = run(task_ids, model=a.model, supplied=supplied,
                  backend=a.env_backend, malformed=a.malformed, out_jsonl=out_jsonl)

    s = summarise(results)
    print("\n=== SWEEP SUMMARY ===")
    print(f"tasks seen:            {s['tasks']}")
    print(f"validated on sandbox:  {s['validated']}")
    print(f"  golden gate PASS:    {s['golden_gate_pass']}/{s['validated']}")
    if s["golden_gate_fail"]:
        print(f"  golden gate FAIL:    {s['golden_gate_fail']}")
    print(f"  tasks w/ authored false-accepts caught: {s['tasks_with_false_accepts']}")
    if s["decompose_error"]:
        print(f"decompose failures ({len(s['decompose_error'])}): {s['decompose_error']}")
    if s["skipped"]:
        print(f"skipped (not model-free validatable): {len(s['skipped'])}")
        for r in s["skipped"]:
            print(f"    {r['task']}: {r['reason']}")
    if s["validate_error"]:
        print(f"validate errors: {len(s['validate_error'])}")
        for r in s["validate_error"]:
            print(f"    {r['task']}: {r['error'][:100]}")

    # the collaborator-facing table
    fa = [(r["task"], r["false_accepts"]) for r in results if r.get("false_accepts")]
    if fa:
        print("\n=== AUTHORED-VERIFIER FALSE-ACCEPTS (our checkpoints catch, outcome verifier misses) ===")
        for tid, accepts in fa:
            for h in accepts:
                print(f"  {tid:45s} {h['kind']:18s} authored={h['authored']}")

    summ_path = out_jsonl.with_suffix(".summary.json")
    summ_path.write_text(json.dumps(s, indent=2), encoding="utf-8")
    print(f"\nsummary: {summ_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
