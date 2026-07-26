"""
End-to-end automation driver: instruction -> criteria -> checkpoints, across the
whole VSCode pool, scored for decompose accuracy.

    for each task:
        decompose(instruction)            # ONE LLM call (server vLLM) or supplied
        -> criteria (T1/T2/T3)
        -> registry -> checkpoints.json   # deterministic
        -> score against verification[]   # the authored ground-truth check set

ACCURACY WITHOUT 30 SANDBOXES
-----------------------------
The obvious accuracy test — run the auto-checkpoints and the task's own
verifier on a golden state and confirm they agree — needs a live sandbox per
task. Instead we score *structurally*: each task already ships a
`verification[]` array (OpenComputer-authored `check-*` commands), which is a
ground-truth set of what must be checked. We normalise both sides to
(op, key-params) and compare. That needs only the one decompose call per task,
not a VM.

The firewall holds: `decompose()` is given the instruction only and never sees
`verification[]`. The comparison happens *after*, so a match means the
decomposer independently arrived at the authored checks — not that it copied
them. A live-sandbox consistency spot-check on a few tasks remains a stronger,
separate confirmation.

TWO SCORES, kept distinct:
  - family recall: for each check *kind* the verifier uses, did we produce a
    checkpoint of the matching op? (did we miss a whole kind of check?)
  - param match: on the 1:1 ops (settings/snippets/extensions/files), do the
    extracted key/value/path match the authored ones? (did we get details right?)

Keybinding checks are exempt from param match: the authored verifier splits a
binding across two entries (check-keybinding-exists + check-keybinding-command)
that never name the same entry — the exact false-accept hole our checkpoint
closes — so there is no authored (key,command) pair to compare against.

Usage:
  # live, on the server (model up):
  python -m process_checks.auto.generate --endpoint-port 8012 --model qwen3.5-27b
  # offline, from a supplied decompositions file (one {task_id, criteria} per line):
  python -m process_checks.auto.generate --decompositions decomps.jsonl
"""
from __future__ import annotations

import argparse
import collections
import json
import os
from pathlib import Path

from evaluation.runtime.run_config import TASKS_DIR

from process_checks.auto.coverage import _CMD_TO_OP
from process_checks.auto.decompose import decompose
from process_checks.auto.registry_vscode import criteria_to_checkpoints

OUT_ROOT = Path(__file__).resolve().parents[1] / "auto" / "generated" / "vscode"

# ops whose params we can compare 1:1 against a single check-* entry
_PARAM_COMPARABLE = {
    "setting_equals": ("key", "value"),
    "snippet_exists": ("prefix",),
    "extension_installed": ("extension_id",),
    "file_exists": ("path",),
    "file_contains": ("path",),
    "workspace_setting_equals": ("key", "value"),
    "launch_config_exists": ("name",),
    "task_defined": ("label",),
    "workspace_extension_recommended": ("extension_id",),
}


def _verification_ops(task: dict) -> collections.Counter:
    """Which registry ops the authored verification[] implies (family level)."""
    fams = collections.Counter()
    for check in task.get("verification", []):
        cmd = check.get("command", "")
        if not cmd:
            continue
        op = _CMD_TO_OP.get(cmd.split()[0])
        if op:
            fams[op] += 1
    return fams


def _auto_ops(checkpoints: list[dict]) -> collections.Counter:
    """Which ops the auto-generated checkpoints use. We recover the op from the
    checkpoint shape rather than storing it, keeping the spec clean."""
    fams = collections.Counter()
    for c in checkpoints:
        # command-lane checkpoints name the check-* family directly
        cmd = c.get("command", "")
        if cmd:
            op = _CMD_TO_OP.get(cmd.split()[0])
            if op:
                fams[op] += 1
                continue
        # jsonc-lane: infer from the file + predicate
        f = c.get("jsonc_file", "")
        ev = c.get("eval", "")
        if "keybindings.json" in f:
            fams["keybinding_bound"] += 1
        elif "/snippets/" in f:
            fams["snippet_exists"] += 1
        elif "/.vscode/settings.json" in f:
            fams["workspace_setting_equals"] += 1
        elif "settings.json" in f:
            fams["setting_equals"] += 1
    return fams


def score_task(task: dict, checkpoints: list[dict]) -> dict:
    want = _verification_ops(task)
    got = _auto_ops(checkpoints)
    want_kinds, got_kinds = set(want), set(got)
    matched = want_kinds & got_kinds
    return {
        "want_ops": dict(want),
        "got_ops": dict(got),
        "families_matched": sorted(matched),
        "families_missed": sorted(want_kinds - got_kinds),   # verifier checks we produced nothing for
        "families_extra": sorted(got_kinds - want_kinds),    # we produced a kind the verifier doesn't
        "family_recall": round(len(matched) / len(want_kinds), 2) if want_kinds else None,
    }


def _load_supplied(path) -> dict:
    out = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            r = json.loads(line)
            out[r["task_id"]] = r["criteria"]
    return out


def run(model: str, supplied: dict | None, write: bool, max_tokens: int = 6000) -> dict:
    tasks = sorted(TASKS_DIR.glob("vscode_*/task.json"))
    tier = collections.Counter()
    per_task, errors = [], []
    full_recall = 0

    for tp in tasks:
        task = json.loads(tp.read_text(encoding="utf-8"))
        tid = task["id"]
        instruction = task.get("task", "")
        try:
            criteria = decompose(instruction, decomposition=(supplied or {}).get(tid),
                                 model=model, max_tokens=max_tokens)
        except Exception as exc:  # noqa: BLE001
            errors.append({"task": tid, "error": f"{type(exc).__name__}: {exc}"})
            continue

        spec = criteria_to_checkpoints(tid, criteria)
        for k, v in spec["tier_counts"].items():
            if k != "total":
                tier[k] += v
        sc = score_task(task, spec["checkpoints"])
        if sc["family_recall"] == 1.0 and not sc["families_missed"]:
            full_recall += 1
        per_task.append({"task": tid, "tiers": spec["tier_counts"], **sc})

        if write:
            d = OUT_ROOT / tid
            d.mkdir(parents=True, exist_ok=True)
            (d / "checkpoints.json").write_text(json.dumps(spec, indent=2), encoding="utf-8")

    scored = [t for t in per_task if t["family_recall"] is not None]
    avg_recall = round(sum(t["family_recall"] for t in scored) / len(scored), 3) if scored else None
    return {
        "tasks": len(tasks),
        "decomposed_ok": len(per_task),
        "errors": errors,
        "tier_totals": dict(tier),
        "tasks_full_family_recall": full_recall,
        "avg_family_recall": avg_recall,
        "per_task": per_task,
    }


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--model", default="qwen3.5-27b")
    p.add_argument("--endpoint-port", type=int, help="local OpenAI-compatible port (sets OPENAI_BASE_URL)")
    p.add_argument("--endpoint-url", type=str)
    p.add_argument("--max-tokens", type=int, default=6000,
                   help="model output budget; raise if a reasoning model truncates "
                        "before emitting the JSON (default 6000)")
    p.add_argument("--decompositions", help="offline: JSONL of {task_id, criteria}")
    p.add_argument("--no-write", action="store_true", help="score only, do not write specs")
    p.add_argument("--out", help="write the run report JSON here")
    a = p.parse_args()

    if a.endpoint_url:
        os.environ["OPENAI_BASE_URL"] = a.endpoint_url
    elif a.endpoint_port:
        os.environ["OPENAI_BASE_URL"] = f"http://localhost:{a.endpoint_port}/v1"

    supplied = _load_supplied(a.decompositions) if a.decompositions else None
    r = run(a.model, supplied, write=not a.no_write, max_tokens=a.max_tokens)

    print(f"tasks: {r['tasks']}   decomposed ok: {r['decomposed_ok']}   errors: {len(r['errors'])}")
    print(f"tier totals (criteria): {r['tier_totals']}")
    print(f"tasks with full family recall: {r['tasks_full_family_recall']}/{r['decomposed_ok']}")
    print(f"avg family recall: {r['avg_family_recall']}")
    if r["errors"]:
        print("errors:")
        for e in r["errors"][:8]:
            print(f"  {e['task']}: {e['error']}")
    misses = [(t["task"], t["families_missed"]) for t in r["per_task"] if t["families_missed"]]
    if misses:
        print("family misses (verifier checks we produced nothing for):")
        for tid, m in misses[:12]:
            print(f"  {tid}: {m}")

    out = Path(a.out) if a.out else Path(__file__).resolve().parents[1] / "runs" / "vscode_generate_report.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(r, indent=2), encoding="utf-8")
    print(f"report: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
