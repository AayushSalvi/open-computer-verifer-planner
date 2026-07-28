"""
Prove golden auto-generation: for the same {op, params} spec, the generated
golden state satisfies what the checkpoint checks.

Three lanes, validated the honest way for each:

  A. JSONC-lane ops (settings / workspace settings / keybindings / snippets):
     our OWN runner parses these files. Plant the golden, run the generated
     checkpoints, assert PASS. Includes a BOOLEAN-valued setting — the case
     that surfaced the json.dumps(True)->"true" eval bug now fixed in the
     registry.

  B. file/raw-lane ops (file_exists / file_contains): the checkpoint delegates
     to the authored verifiers/vscode.py, so faking that verifier would prove
     nothing about the real one. Instead assert the planted ARTIFACT directly
     satisfies the op's concrete property (the file is present; its text
     contains the substring) — exactly what the real verifier will read on the
     server. Not circular against our checkpoint: it checks the file, not our
     predicate.

  C. bookkeeping: extension_installed is the only unplantable op (no golden
     file can fake an installed extension); nothing silently skipped; multiple
     ops on one file MERGE into a single file (two settings -> one
     settings.json; exists+contains on one path -> one file).

The remaining server-side half — planting the golden and confirming the task's
AUTHORED verify_task passes on it — is the independent oracle and lives in the
server driver; this is the offline half.
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
from process_checks.auto.registry_vscode import criteria_to_checkpoints, SETTINGS  # noqa: E402
from process_checks.auto.golden_vscode import criteria_to_golden, plant_golden_state  # noqa: E402

WS = "/home/user/project"

JSONC_LANE = [
    {"tier": "T1", "bind": {"op": "setting_equals", "params": {"key": "editor.fontSize", "value": 14}}},
    {"tier": "T1", "bind": {"op": "setting_equals", "params": {"key": "editor.tabSize", "value": 4}}},
    # boolean value: the exact case that broke json.dumps(True) -> "true"
    {"tier": "T1", "bind": {"op": "setting_equals", "params": {"key": "files.autoSave", "value": False}}},
    {"tier": "T1", "bind": {"op": "keybinding_bound", "params": {"key": "ctrl+alt+s", "command": "workbench.action.files.saveAll"}}},
    {"tier": "T1", "bind": {"op": "snippet_exists", "params": {"language": "python", "prefix": "pp"}}},
    {"tier": "T1", "bind": {"op": "workspace_setting_equals", "params": {"workspace": WS, "key": "python.linting.enabled", "value": True}}},
]

FILE_LANE = [
    {"tier": "T1", "bind": {"op": "file_exists", "params": {"path": f"{WS}/main.py"}}},
    {"tier": "T1", "bind": {"op": "file_contains", "params": {"path": f"{WS}/main.py", "substring": "def main"}}},
    {"tier": "T1", "bind": {"op": "file_contains", "params": {"path": f"{WS}/main.py", "substring": "return 0"}}},
]

CMD_WS_LANE = [  # bookkeeping only offline; validated by the real verifier on the server
    {"tier": "T1", "bind": {"op": "workspace_extension_recommended", "params": {"workspace": WS, "extension_id": "ms-python.python"}}},
    {"tier": "T1", "bind": {"op": "task_defined", "params": {"workspace": WS, "label": "build"}}},
    {"tier": "T1", "bind": {"op": "launch_config_exists", "params": {"workspace": WS, "name": "Debug"}}},
]

UNPLANTABLE = [
    {"tier": "T1", "bind": {"op": "extension_installed", "params": {"extension_id": "ms-python.python"}}},
]

CRITERIA = JSONC_LANE + FILE_LANE + CMD_WS_LANE + UNPLANTABLE


@dataclass
class R:
    stdout: str; stderr: str = ""; exit_code: int = 0


class FakeCmd:
    """Only `cat` and mkdir/rm — enough for the JSONC lane. Command-lane
    verifier calls are NOT simulated; those ops are validated in lane B/C."""
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


fails = []
golden = criteria_to_golden(CRITERIA)

print(f"golden files planted:  {len(golden['files'])}")
print(f"unplantable ops:       {[u['op'] for u in golden['unplantable']]}")
print(f"skipped:               {golden['skipped']}\n")

# ---- lane A: JSONC-lane checkpoints must PASS on the generated golden -------
sb = FakeSandbox()
plant_golden_state(sb, golden)
autodir = Path(tempfile.mkdtemp())
jsonc_spec = criteria_to_checkpoints("t", JSONC_LANE)
(autodir / "checkpoints.json").write_text(json.dumps(jsonc_spec), encoding="utf-8")
conds = run_checkpoints(sb, "vscode", autodir)
print("lane A — JSONC-lane checkpoint verdicts on the generated golden:")
for c in conds:
    print(f"  {'PASS' if c['pass'] else 'FAIL'} [{c['status']}] {c['id']}"
          + ("" if c["pass"] else "  <-- FAIL"))
if not all(c["pass"] for c in conds):
    fails.append("a JSONC-lane checkpoint did not pass on the generated golden")

# ---- lane B: the planted file artifact satisfies the file-op property ------
print("\nlane B — planted file artifacts satisfy the file ops directly:")
for c in FILE_LANE:
    p = c["bind"]["params"]
    path = p["path"]
    text = golden["files"].get(path)
    if text is None:
        fails.append(f"file op golden missing for {path}"); continue
    if "substring" in p:
        ok = p["substring"] in text
        print(f"  {'OK' if ok else 'MISS'}  contains {p['substring']!r} in {path}")
        if not ok:
            fails.append(f"golden for {path} lacks substring {p['substring']!r}")
    else:
        print(f"  OK    exists {path}")

# ---- lane C: bookkeeping ----------------------------------------------------
print("\nlane C — bookkeeping:")
# merge: two settings + one boolean setting -> ONE settings.json with 3 keys
sdata = json.loads(golden["files"][SETTINGS])
merged_ok = sdata == {"editor.fontSize": 14, "editor.tabSize": 4, "files.autoSave": False}
print(f"  {'OK' if merged_ok else 'MISS'}  settings.json merged: {sdata}")
if not merged_ok:
    fails.append(f"settings.json did not merge to 3 keys: {sdata}")
# merge: file_exists + 2x file_contains on one path -> ONE file
mainpy = [p for p in golden["files"] if p.endswith("main.py")]
print(f"  {'OK' if len(mainpy) == 1 else 'MISS'}  main.py is a single merged file: {mainpy}")
if len(mainpy) != 1:
    fails.append(f"main.py should be one merged file, got {mainpy}")
# unplantable reporting
if [u["op"] for u in golden["unplantable"]] != ["extension_installed"]:
    fails.append("extension_installed should be the only unplantable op")
else:
    print("  OK    extension_installed reported unplantable (not silently dropped)")
if golden["skipped"]:
    fails.append(f"unexpected skips: {golden['skipped']}")
else:
    print("  OK    nothing silently skipped")

print("\n" + ("GOLDEN AUTO-GEN OK: JSONC-lane checkpoints pass on the generated "
              "golden (incl. a boolean setting); file-lane artifacts satisfy their "
              "ops; merges and unplantable reporting correct."
              if not fails else "FAILURES:\n  " + "\n  ".join(fails)))
sys.exit(1 if fails else 0)
