"""
Prove the automation reproduces the hand-built seed task.

Feed a decomposition of keybindings_and_settings_combo (the shape her
decompose.py would emit) through the registry, then confirm the AUTO-generated
checkpoints behave identically to the HAND-BUILT ones on the golden and both
honeypot states — same verdict vector, every state.
"""
import importlib.util, json, sys
from dataclasses import dataclass
from pathlib import Path

REPO = Path("C:/Data_science_projects/research_task/OS-world2/orchestrator/OpenComputer")
sys.path.insert(0, str(REPO))

from computer_env.backends.base import CommandExitException           # noqa: E402
from process_checks.runner.checkpoints import run_checkpoints          # noqa: E402
from process_checks.auto.registry_vscode import criteria_to_checkpoints  # noqa: E402

TASK = "vscode_keybindings_and_settings_combo"
CPDIR = REPO / "process_checks" / "vscode" / TASK

# The decomposition a decomposer LLM would emit from the instruction
# "set editor.fontSize 14, editor.tabSize 4, editor.cursorStyle block; bind
#  ctrl+shift+n->newUntitledFile, ctrl+shift+w->closeAllEditors,
#  ctrl+alt+s->saveAll". All six criteria are T1.
DECOMP = [
    {"text": "editor.fontSize is 14", "tier": "T1",
     "bind": {"op": "setting_equals", "params": {"key": "editor.fontSize", "value": 14}}},
    {"text": "editor.tabSize is 4", "tier": "T1",
     "bind": {"op": "setting_equals", "params": {"key": "editor.tabSize", "value": 4}}},
    {"text": "editor.cursorStyle is block", "tier": "T1",
     "bind": {"op": "setting_equals", "params": {"key": "editor.cursorStyle", "value": "block"}}},
    {"text": "ctrl+shift+n bound to newUntitledFile", "tier": "T1",
     "bind": {"op": "keybinding_bound", "params": {
         "key": "ctrl+shift+n", "command": "workbench.action.files.newUntitledFile"}}},
    {"text": "ctrl+shift+w bound to closeAllEditors", "tier": "T1",
     "bind": {"op": "keybinding_bound", "params": {
         "key": "ctrl+shift+w", "command": "workbench.action.closeAllEditors"}}},
    {"text": "ctrl+alt+s bound to saveAll", "tier": "T1",
     "bind": {"op": "keybinding_bound", "params": {
         "key": "ctrl+alt+s", "command": "workbench.action.files.saveAll"}}},
]

auto = criteria_to_checkpoints(TASK, DECOMP)
print(f"decomposition: {auto['tier_counts']}  (all T1 = fully automatable)")


# ---- fake sandbox that serves planted files (same as our other tests) -------
@dataclass
class R:
    stdout: str; stderr: str = ""; exit_code: int = 0


class FakeCmd:
    def __init__(s, fs): s.fs = fs
    def run(s, command, timeout=None):
        if command.startswith(("mkdir", "rm -rf")): return R("")
        if command.startswith("cat "):
            p = command[4:].strip()
            if p not in s.fs:
                raise CommandExitException(command, 1, stderr=f"cat: {p}: No such file or directory")
            return R(s.fs[p])
        raise AssertionError(command)


class FakeFiles:
    def __init__(s, fs): s.fs = fs
    def write(s, path, data): s.fs[path] = data

class FakeSandbox:
    def __init__(s, fs): s.fs = fs; s.commands = FakeCmd(fs); s.files = FakeFiles(fs)


def load(path, name):
    spec = importlib.util.spec_from_file_location(f"m_{name}", path)
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return getattr(m, name)


# build the three states from the real golden.py + tamper.py
plant_golden = load(CPDIR / "golden.py", "plant_golden")
hp = REPO / "honeypots" / "vscode"

def state_from(planter):
    fs = {}
    planter(FakeSandbox(fs))
    return fs

states = {
    "golden": state_from(lambda sb: plant_golden(sb, "jsonc")),
    "malformed_keybindings": state_from(load(hp / f"{TASK}__malformed_keybindings" / "tamper.py", "tamper")),
    "workspace_shadow": state_from(load(hp / f"{TASK}__workspace_shadow" / "tamper.py", "tamper")),
}

# write the auto spec to a temp dir so run_checkpoints can load it
import tempfile, os
autodir = Path(tempfile.mkdtemp())
(autodir / "checkpoints.json").write_text(json.dumps(auto), encoding="utf-8")

print("\nstate                    hand-built vector            auto vector                  match")
print("-" * 92)
fails = 0
for name, fs in states.items():
    hand = {c["id"]: c["pass"] for c in run_checkpoints(FakeSandbox(dict(fs)), "vscode", CPDIR)}
    auto_v = {c["id"]: c["pass"] for c in run_checkpoints(FakeSandbox(dict(fs)), "vscode", autodir)}
    hand_vec = [hand[k] for k in sorted(hand)]
    auto_vec = [auto_v[k] for k in sorted(auto_v)]
    ok = hand_vec == auto_vec
    fails += (not ok)
    print(f"{name:24s} {str(hand_vec):28s} {str(auto_vec):28s} {'OK' if ok else 'XX MISMATCH'}")

print("\n" + ("AUTO-GENERATED CHECKPOINTS MATCH HAND-BUILT ON EVERY STATE"
              if not fails else f"{fails} STATE(S) DIVERGED"))
sys.exit(1 if fails else 0)
