"""
Registry coverage over the VSCode task pool — the offline automation ceiling.

The full automation is  instruction --(LLM)--> criteria --(registry)--> checkpoints.
This measures only the second arrow, and does it WITHOUT the model, by asking:
of every check the 30 pool tasks actually use, how many map to a registry op we
have?

That is a *ceiling*, stated honestly: it says how much the deterministic tier
COULD bind if the decomposer extracts criteria perfectly. It does not measure
whether the LLM decompose step is accurate — that needs a live model run and is
gated against each task's own verification[] (the consistency gate). Reported
separately so the two numbers are never conflated.

We map the pool's `check-*` verification commands to registry ops. Each command
family corresponds to exactly one op (the registry was built from the same
catalog), so an uncovered command means a genuinely missing op, not a naming
mismatch.

Run:  python -m process_checks.auto.coverage
"""
from __future__ import annotations

import collections
import json
from pathlib import Path

from process_checks.auto.registry_vscode import OP_DOC
from evaluation.runtime.run_config import TASKS_DIR

# check-* command family -> registry op. This is the audited correspondence;
# a command not listed here has no op and counts as uncovered.
_CMD_TO_OP = {
    "check-setting": "setting_equals",
    "check-keybinding-exists": "keybinding_bound",
    "check-keybinding-command": "keybinding_bound",
    "check-snippet-exists": "snippet_exists",
    "check-extension-installed": "extension_installed",
    "check-workspace-setting": "workspace_setting_equals",
    "check-workspace-extension-recommended": "workspace_extension_recommended",
    "check-task-exists": "task_defined",
    "check-tasks-defined": "task_defined",
    "check-launch-config-exists": "launch_config_exists",
    "check-file-exists": "file_exists",
    "check-file-contains": "file_contains",
    "check-workspace-has-file": "file_exists",
}


def analyze() -> dict:
    tasks = sorted(TASKS_DIR.glob("vscode_*/task.json"))
    total_checks = covered = 0
    by_op = collections.Counter()
    uncovered_cmds = collections.Counter()
    eval_checks = 0
    per_task = []

    for tp in tasks:
        task = json.loads(tp.read_text(encoding="utf-8"))
        t_total = t_cov = 0
        for check in task.get("verification", []):
            cmd = check.get("command", "")
            if not cmd:
                # an `eval`-based check with no command: not a template op
                eval_checks += 1
                t_total += 1
                total_checks += 1
                continue
            family = cmd.split()[0]
            t_total += 1
            total_checks += 1
            op = _CMD_TO_OP.get(family)
            if op:
                covered += 1
                t_cov += 1
                by_op[op] += 1
            else:
                uncovered_cmds[family] += 1
        per_task.append({"task": task["id"], "checks": t_total, "covered": t_cov,
                         "fully_covered": t_cov == t_total and t_total > 0})

    fully = sum(1 for t in per_task if t["fully_covered"])
    return {
        "tasks": len(tasks),
        "fully_covered_tasks": fully,
        "total_checks": total_checks,
        "covered_checks": covered,
        "coverage_pct": round(100 * covered / total_checks, 1) if total_checks else 0,
        "eval_checks_no_command": eval_checks,
        "registry_ops": len(OP_DOC),
        "checks_by_op": dict(by_op.most_common()),
        "uncovered_commands": dict(uncovered_cmds.most_common()),
        "per_task": per_task,
    }


def main() -> int:
    r = analyze()
    print(f"VSCode task pool: {r['tasks']} tasks, {r['total_checks']} checks total\n")
    print(f"  registry ops available:      {r['registry_ops']}")
    print(f"  checks mappable to an op:    {r['covered_checks']}/{r['total_checks']}"
          f"  ({r['coverage_pct']}%)  <- tier-1 automation ceiling")
    print(f"  tasks fully covered:         {r['fully_covered_tasks']}/{r['tasks']}")
    if r["eval_checks_no_command"]:
        print(f"  eval-only checks (no cmd):   {r['eval_checks_no_command']}")
    if r["uncovered_commands"]:
        print(f"  UNCOVERED command families:  {r['uncovered_commands']}")
    else:
        print("  every check-* command in the pool maps to a registry op")
    print("\n  checks handled per op:")
    for op, n in r["checks_by_op"].items():
        print(f"    {op:34s} {n}")

    out = Path(__file__).resolve().parents[1] / "runs" / "vscode_registry_coverage.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(r, indent=2), encoding="utf-8")
    print(f"\n  report: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
