"""
Prove honeypot auto-generation. A honeypot is correct iff the generated
checkpoints, run on it, flip EXACTLY the targeted checkpoint PASS->FAIL and
leave every other checkpoint PASS. That single-flip property is the whole
self-validation — no hand-labelling.

Four parts:
  1. value-level single-flip (JSONC + settings ops, incl. a boolean setting and
     the split-keybinding trap): run the checkpoints, assert the verdict vector
     equals the generated `expected` — exactly one False, at the target.
  2. file-lane single-flip (missing_file / absent_substring): the checkpoint
     delegates to the authored verifier, so validate the perturbed ARTIFACT
     directly — the trapped property is gone, everything else intact.
  3. malformed reproduces the hand-built honeypot: on the seed task's 3 settings
     + 3 keybindings, a malformed keybindings file yields [T,T,T,F,F,F] with
     status `ok` — the exact vector of the hand-built malformed_keybindings.
  4. bookkeeping: extension_installed is reported unhoneypottable.
"""
from __future__ import annotations

import json
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from computer_env.backends.base import CommandExitException          # noqa: E402
from process_checks.runner.checkpoints import run_checkpoints         # noqa: E402
from process_checks.auto.registry_vscode import criteria_to_checkpoints  # noqa: E402
from process_checks.auto.honeypot_vscode import (                     # noqa: E402
    criteria_to_honeypots, plant_honeypot,
)

WS = "/home/user/project"
fails = []


@dataclass
class R:
    stdout: str; stderr: str = ""; exit_code: int = 0


class FakeCmd:
    def __init__(s, fs): s.fs = fs
    def run(s, command, timeout=None):
        if command.startswith(("mkdir", "rm -rf")):
            return R("")
        if command.startswith("cat "):
            p = command[4:].strip()
            if p not in s.fs:
                raise CommandExitException(command, 1, stderr=f"cat: {p}: No such file or directory")
            return R(s.fs[p])
        raise CommandExitException(command, 127, stderr="not simulated offline")


class FakeFiles:
    def __init__(s, fs): s.fs = fs
    def write(s, path, data): s.fs[path] = data


class FakeSandbox:
    def __init__(s):
        s.fs = {}
        s.commands = FakeCmd(s.fs)
        s.files = FakeFiles(s.fs)


def _run(spec, hp):
    """Plant a honeypot's files, run the checkpoints, return {cid: pass}."""
    sb = FakeSandbox()
    plant_honeypot(sb, hp)
    d = Path(tempfile.mkdtemp())
    (d / "checkpoints.json").write_text(json.dumps(spec), encoding="utf-8")
    return {c["id"]: c for c in run_checkpoints(sb, "vscode", d)}


# =========================================================================== #
# 1. value-level single-flip
# =========================================================================== #
JSONC = [
    {"tier": "T1", "bind": {"op": "setting_equals", "params": {"key": "editor.fontSize", "value": 14}}},
    {"tier": "T1", "bind": {"op": "setting_equals", "params": {"key": "files.autoSave", "value": False}}},  # boolean
    {"tier": "T1", "bind": {"op": "workspace_setting_equals", "params": {"workspace": WS, "key": "python.linting.enabled", "value": True}}},
    {"tier": "T1", "bind": {"op": "keybinding_bound", "params": {"key": "ctrl+alt+s", "command": "workbench.action.files.saveAll"}}},
    {"tier": "T1", "bind": {"op": "snippet_exists", "params": {"language": "python", "prefix": "pp"}}},
]
spec = criteria_to_checkpoints("t", JSONC)
gen = criteria_to_honeypots(JSONC)
print(f"part 1 — value-level honeypots: {len(gen['honeypots'])}\n")
for hp in gen["honeypots"]:
    verdicts = _run(spec, hp)
    got = {cid: v["pass"] for cid, v in verdicts.items()}
    exp = hp["expected"]
    n_fail = sum(1 for p in got.values() if not p)
    ok = got == exp and n_fail == 1 and got.get(hp["target"]) is False
    print(f"  [{'OK' if ok else 'BAD'}] target={hp['target']:<10} kind={hp['kind']:<16} "
          f"flips={n_fail}  {hp['note'][:52]}")
    if not ok:
        fails.append(f"honeypot {hp['target']} ({hp['kind']}): got {got} expected {exp}")


# =========================================================================== #
# 2. file-lane single-flip (validate the artifact, verifier not faked)
# =========================================================================== #
FILE = [
    {"tier": "T1", "bind": {"op": "file_exists", "params": {"path": f"{WS}/main.py"}}},
    {"tier": "T1", "bind": {"op": "file_contains", "params": {"path": f"{WS}/main.py", "substring": "def main"}}},
    {"tier": "T1", "bind": {"op": "file_contains", "params": {"path": f"{WS}/util.py", "substring": "helper"}}},
]
genf = criteria_to_honeypots(FILE)
print(f"\npart 2 — file-lane honeypots (artifact-checked): {len(genf['honeypots'])}\n")
for hp in genf["honeypots"]:
    files = hp["files"]
    if hp["kind"] == "missing_file":
        # main.py gone; util.py still present with its content
        ok = (f"{WS}/main.py" not in files) and ("helper" in files.get(f"{WS}/util.py", ""))
    else:  # absent_substring
        # find the file/substring this honeypot targeted from the note
        ok = True
        # def main removed from main.py, but main.py still exists; util untouched
        if "def main" in hp["note"]:
            ok = ("def main" not in files.get(f"{WS}/main.py", "")) and (f"{WS}/main.py" in files) \
                 and ("helper" in files.get(f"{WS}/util.py", ""))
        elif "helper" in hp["note"]:
            ok = ("helper" not in files.get(f"{WS}/util.py", "")) and (f"{WS}/util.py" in files) \
                 and ("def main" in files.get(f"{WS}/main.py", ""))
    print(f"  [{'OK' if ok else 'BAD'}] target={hp['target']:<10} kind={hp['kind']:<16} {hp['note'][:52]}")
    if not ok:
        fails.append(f"file-lane honeypot {hp['target']} ({hp['kind']}) artifact wrong: {files}")


# =========================================================================== #
# 3. malformed reproduces the hand-built malformed_keybindings vector
# =========================================================================== #
SEED = [
    {"tier": "T1", "bind": {"op": "setting_equals", "params": {"key": "editor.fontSize", "value": 14}}},
    {"tier": "T1", "bind": {"op": "setting_equals", "params": {"key": "editor.tabSize", "value": 4}}},
    {"tier": "T1", "bind": {"op": "setting_equals", "params": {"key": "editor.cursorStyle", "value": "block"}}},
    {"tier": "T1", "bind": {"op": "keybinding_bound", "params": {"key": "ctrl+shift+n", "command": "workbench.action.files.newUntitledFile"}}},
    {"tier": "T1", "bind": {"op": "keybinding_bound", "params": {"key": "ctrl+shift+w", "command": "workbench.action.closeAllEditors"}}},
    {"tier": "T1", "bind": {"op": "keybinding_bound", "params": {"key": "ctrl+alt+s", "command": "workbench.action.files.saveAll"}}},
]
seed_spec = criteria_to_checkpoints("seed", SEED)
seed_cids = [c["id"] for c in seed_spec["checkpoints"]]
gm = criteria_to_honeypots(SEED, malformed=True)
# pick a malformed honeypot that targets a keybinding
kb_hp = next(h for h in gm["honeypots"] if h["op"] == "keybinding_bound")
verdicts = _run(seed_spec, kb_hp)
vec = [verdicts[cid]["pass"] for cid in seed_cids]
statuses = [verdicts[cid]["status"] for cid in seed_cids]
want_vec = [True, True, True, False, False, False]
kb_statuses_ok = all(verdicts[cid]["status"] == "ok" for cid in seed_cids[3:])
print(f"\npart 3 — malformed keybindings reproduces hand-built:")
print(f"  vector:   {vec}")
print(f"  want:     {want_vec}")
print(f"  statuses: {statuses}  (keybinding fails must be 'ok', not 'unreadable')")
if vec != want_vec:
    fails.append(f"malformed vector {vec} != hand-built {want_vec}")
if not kb_statuses_ok:
    fails.append("malformed keybinding fails must have status 'ok' (determinate), not 'unreadable'")


# =========================================================================== #
# 4. bookkeeping
# =========================================================================== #
WITH_EXT = JSONC + [
    {"tier": "T1", "bind": {"op": "extension_installed", "params": {"extension_id": "ms-python.python"}}},
]
ge = criteria_to_honeypots(WITH_EXT)
unh = [u["op"] for u in ge["unhoneypottable"]]
print(f"\npart 4 — bookkeeping:")
print(f"  unhoneypottable: {unh}")
if unh != ["extension_installed"]:
    fails.append(f"extension_installed should be the only unhoneypottable op, got {unh}")

print("\n" + ("HONEYPOT AUTO-GEN OK: every value-level honeypot flips exactly its "
              "target; file-lane artifacts perturbed correctly; malformed reproduces "
              "the hand-built vector; extension_installed reported unhoneypottable."
              if not fails else "FAILURES:\n  " + "\n  ".join(fails)))
sys.exit(1 if fails else 0)
