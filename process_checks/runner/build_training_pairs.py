#!/usr/bin/env python3
"""
process_checks/runner/build_training_pairs.py

Emit contrastive training pairs for verifier cold-start SFT.

WHY PAIRS
---------
A cold-start set made only of honeypots teaches "always probe" -- the mirror
image of the `done_probe_backed = 0.0` pathology it is meant to fix. Ritual
probing earns no validity credit, so RL then has to un-teach it, wasting the
cold start.

The target behaviour is not "always probe", it is "probe when the screen
cannot settle it". Teaching that needs both halves:

    golden   -- screen tells the truth   -> a glance is genuinely sufficient
    honeypot -- screen lies              -> probing is required

Same task, same instruction, same checkpoints. The ONLY variable is whether
vision is trustworthy, which is exactly the discrimination the verifier has to
learn.

WHAT EACH RECORD CARRIES
------------------------
Per variant, per condition: the verdict, AND the probe that produced it plus
what that probe returned. The verdict alone shows the answer without the
behaviour that reaches it; the probe trace is the demonstration half.

Usage (server-side, env `opencomputer`, repo root):
    python -m process_checks.runner.build_training_pairs \\
        --app vscode --task vscode_keybindings_and_settings_combo \\
        --env-backend docker
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
    DEFAULT_DOCKER_CPUS, DEFAULT_DOCKER_IMAGE, DEFAULT_DOCKER_MEMORY,
    DEFAULT_DOCKER_PLATFORM, DEFAULT_DOCKER_READY_TIMEOUT,
    DEFAULT_DOCKER_SHM_SIZE, DEFAULT_ENV_BACKEND,
)
from evaluation.runtime.run_config import DEFAULT_SANDBOX_TIMEOUT, TASKS_DIR  # noqa: E402
from evaluation.runtime.sandbox_session import setup_sandbox_session  # noqa: E402

from process_checks.runner.checkpoints import run_checkpoints  # noqa: E402

HONEYPOTS_DIR = REPO_ROOT / "honeypots"


def _load_callable(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(f"_mod_{path.parent.name}_{name}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return getattr(module, name)


def _reset(sandbox) -> None:
    """Clear planted state so one variant cannot leak into the next."""
    sandbox.commands.run(
        "rm -rf /home/user/.config/Code/User/settings.json "
        "/home/user/.config/Code/User/keybindings.json /home/user/project",
        timeout=20,
    )


def _dedupe_probes(conditions: list[dict]) -> tuple[list[dict], list[dict]]:
    """Group conditions by the probe that answered them.

    Six conditions over two files is TWO reads, not six. Emitting one probe
    record per condition would duplicate each file's contents three times and,
    worse, demonstrate "issue a separate probe per condition" -- teaching
    exactly the inefficiency the validity credit penalises. A competent
    verifier reads settings.json once and settles all three settings from it,
    so the demonstration has to show that.

    Returns (probes, conditions) where each condition references a probe by id
    and no longer carries its own copy of the result.
    """
    probes: list[dict] = []
    by_key: dict[tuple, dict] = {}
    slim: list[dict] = []

    for c in conditions:
        cond = {k: v for k, v in c.items()
                if k not in ("probe", "probe_result", "probe_result_truncated")}
        p = c.get("probe")
        if p is None:
            slim.append(cond)
            continue
        key = (p.get("kind"), p.get("path"), p.get("parse"))
        entry = by_key.get(key)
        if entry is None:
            entry = {
                "id": f"p{len(probes) + 1}",
                **p,
                "result": c.get("probe_result", ""),
                "result_truncated": c.get("probe_result_truncated", False),
                "answers": [],
            }
            by_key[key] = entry
            probes.append(entry)
        entry["answers"].append(c["id"])
        cond["probe_id"] = entry["id"]
        slim.append(cond)

    return probes, slim


def _variant_record(sandbox, app: str, checkpoints_dir: Path, planted: dict,
                    screen_truthful: bool) -> dict:
    conditions = run_checkpoints(sandbox, app, checkpoints_dir, capture_probes=True)
    unmet = [c["id"] for c in conditions if not c["pass"]]
    all_pass = not unmet
    probes, conditions = _dedupe_probes(conditions)
    return {
        "variant": planted["variant"],
        "screen_is_truthful": screen_truthful,
        # What a correct verifier should conclude about the task.
        "correct_verdict": "done" if all_pass else "not_done",
        # The teaching signal: on a truthful screen a glance suffices, so a
        # probe is optional. On a lying screen the verdict is unreachable
        # without probing.
        "probing_required": not screen_truthful,
        "unmet_conditions": unmet,
        "planted_summary": planted["summary"],
        # The efficient demonstration: N probes settle M conditions, N < M.
        "probe_count": len(probes),
        "probes": probes,
        "conditions": conditions,
    }


def run(app: str, task_id: str, out_dir: Path, golden_styles: list[str], **backend) -> int:
    task_path = TASKS_DIR / task_id / "task.json"
    with open(task_path) as f:
        task = json.load(f)

    checkpoints_dir = REPO_ROOT / "process_checks" / app / task_id
    golden_py = checkpoints_dir / "golden.py"
    if not golden_py.exists():
        print(f"No golden.py for {app}/{task_id} -- cannot build a contrastive pair.")
        print("A honeypot alone teaches 'always probe'; the truthful half is required.")
        return 1
    plant_golden = _load_callable(golden_py, "plant_golden")

    variants = sorted(
        d for d in (HONEYPOTS_DIR / app).iterdir()
        if d.is_dir() and (d / "honeypot.json").exists()
        and json.loads((d / "honeypot.json").read_text()).get("base_task") == task_id
    ) if (HONEYPOTS_DIR / app).is_dir() else []

    if not variants:
        print(f"No honeypot variants found for {task_id}.")
        return 1

    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"{'=' * 70}\n  Training pairs: {task_id}\n"
          f"  golden styles: {golden_styles}   honeypots: {len(variants)}\n{'=' * 70}")

    session = setup_sandbox_session(
        app, task, backend.pop("sandbox_timeout"),
        run_id=f"training_pairs_{datetime.now():%H%M%S}", **backend
    )
    sandbox = session.sandbox
    records = []
    try:
        # --- truthful half -------------------------------------------------
        for style in golden_styles:
            _reset(sandbox)
            planted = plant_golden(sandbox, style)
            rec = _variant_record(sandbox, app, checkpoints_dir, planted, screen_truthful=True)
            records.append(rec)
            print(f"  golden/{style:5s}  verdict={rec['correct_verdict']:8s} "
                  f"probes={rec['probe_count']}->{len(rec['conditions'])}cond  "
                  f"unmet={rec['unmet_conditions'] or 'none'}")
            if rec["correct_verdict"] != "done":
                print("    WARNING: golden state did not fully pass -- the truthful "
                      "half of the pair is broken, not the honeypot.")

        # --- lying half ----------------------------------------------------
        for vdir in variants:
            _reset(sandbox)
            planted = _load_callable(vdir / "tamper.py", "tamper")(sandbox)
            planted.setdefault("variant", vdir.name)
            rec = _variant_record(sandbox, app, checkpoints_dir, planted, screen_truthful=False)
            records.append(rec)
            print(f"  honeypot/{vdir.name[-28:]:28s} verdict={rec['correct_verdict']:8s} "
                  f"probes={rec['probe_count']}->{len(rec['conditions'])}cond  "
                  f"unmet={rec['unmet_conditions']}")
            if rec["correct_verdict"] == "done":
                print("    WARNING: honeypot did not produce a failing verdict -- "
                      "this pair would teach nothing.")
    finally:
        try:
            sandbox.kill()
        except Exception as exc:
            print(f"  WARNING: failed to kill sandbox: {exc}")

    truthful = [r for r in records if r["screen_is_truthful"]]
    lying = [r for r in records if not r["screen_is_truthful"]]
    bundle = {
        "task_id": task_id,
        "app": app,
        "instruction": task["task"],
        "built_at": datetime.now().isoformat(),
        "purpose": "verifier cold-start SFT: contrastive screen-truthful vs screen-lying states",
        "pair_counts": {"truthful": len(truthful), "lying": len(lying)},
        "note": ("Probe traces are the demonstration half: a verdict shows the answer without "
                 "the behaviour that reaches it. Probes are deduplicated -- each variant lists "
                 "the unique probes issued and which conditions each one answers, so the "
                 "demonstration shows N probes settling M conditions rather than one probe per "
                 "condition. Probe encoding is generic (kind/path/parse); map onto the "
                 "verifier's own action schema."),
        "variants": records,
    }
    out = out_dir / f"{task_id}_pairs.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump(bundle, f, indent=2, default=str)

    print(f"\n  truthful states: {len(truthful)}   lying states: {len(lying)}")
    print(f"  wrote {out}")
    return 0 if truthful and lying else 1


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--app", default="vscode")
    p.add_argument("--task", required=True)
    p.add_argument("--out-dir", default=str(REPO_ROOT / "process_checks" / "runs" / "training_pairs"))
    p.add_argument("--golden-styles", default="plain,jsonc",
                   help="comma-separated golden styles to plant (default: plain,jsonc)")
    p.add_argument("--env-backend", choices=["e2b", "docker", "remote_docker"], default=DEFAULT_ENV_BACKEND)
    p.add_argument("--docker-image", default=DEFAULT_DOCKER_IMAGE)
    p.add_argument("--docker-platform", default=DEFAULT_DOCKER_PLATFORM)
    p.add_argument("--docker-shm-size", default=DEFAULT_DOCKER_SHM_SIZE)
    p.add_argument("--docker-memory", default=DEFAULT_DOCKER_MEMORY)
    p.add_argument("--docker-cpus", default=DEFAULT_DOCKER_CPUS)
    p.add_argument("--docker-ready-timeout", type=int, default=DEFAULT_DOCKER_READY_TIMEOUT)
    p.add_argument("--sandbox-timeout", type=int, default=DEFAULT_SANDBOX_TIMEOUT)
    a = p.parse_args()
    sys.exit(run(a.app, a.task, Path(a.out_dir),
                 [s.strip() for s in a.golden_styles.split(",") if s.strip()],
                 env_backend=a.env_backend, docker_image=a.docker_image,
                 docker_platform=a.docker_platform, docker_shm_size=a.docker_shm_size,
                 docker_memory=a.docker_memory, docker_cpus=a.docker_cpus,
                 docker_ready_timeout=a.docker_ready_timeout,
                 sandbox_timeout=a.sandbox_timeout))


if __name__ == "__main__":
    main()
