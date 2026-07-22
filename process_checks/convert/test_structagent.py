"""
Tests for the StructAgent probe emitter.

The emitted patterns are executed with the SAME matching semantics as
StructAgent's `_check_patterns` (verifiers.py), reimplemented here, so these
assert real behaviour rather than pattern shape.

The load-bearing test is `split key/command must NOT match`: without
proximity_lines a file with the key on one entry and the command on another
satisfies two independent regexes, which is the false accept our checkpoints
exist to close.

Run:  python -m process_checks.convert.test_structagent
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from process_checks.convert.structagent import (  # noqa: E402
    keybinding_probe, probe_for_checkpoint, settings_probe,
)

FAILURES: list[str] = []


def check(cond: bool, label: str) -> None:
    print(f"  {'OK ' if cond else 'XX '} {label}")
    if not cond:
        FAILURES.append(label)


def eval_spec(spec: dict, text: str) -> bool:
    """Mirror of StructAgent's _check_patterns (verifiers.py:392)."""
    compiled = [re.compile(p, re.MULTILINE) for p in spec["patterns"]]
    prox = spec.get("proximity_lines")
    if prox is None:
        return (all(c.search(text) for c in compiled) if spec.get("all_must_match")
                else any(c.search(text) for c in compiled))
    lines = text.split("\n")
    win = max(1, prox)
    return any(all(c.search("\n".join(lines[i:i + win])) for c in compiled)
               for i in range(len(lines)))


SETTINGS_OK = """{
    "editor.fontSize": 14,
    "editor.tabSize": 4,
    "editor.cursorStyle": "block"
}"""
SETTINGS_WRONG = """{
    "editor.fontSize": 12,
    "editor.tabSize": 2
}"""

KB_OK = """// Place your key bindings in this file to override the defaults
[
    { "key": "ctrl+shift+n", "command": "workbench.action.files.newUntitledFile" },
    { "key": "ctrl+shift+w", "command": "workbench.action.closeAllEditors" }
]"""
KB_SPLIT = """[
    { "key": "ctrl+shift+n", "command": "WRONG_COMMAND" },
    { "key": "WRONG_KEY",    "command": "workbench.action.files.newUntitledFile" }
]"""
KB_WITH_WHEN = """[
    { "key": "ctrl+shift+n",
      "command": "workbench.action.files.newUntitledFile",
      "when": "editorTextFocus" }
]"""

print("=== settings: numeric value ===")
s = settings_probe("/p/settings.json", "editor.fontSize", 14)
check(eval_spec(s, SETTINGS_OK) is True, "matches when the value is right")
check(eval_spec(s, SETTINGS_WRONG) is False, "does not match a different value")
check(s["file_path"] == "/p/settings.json", "file_path carried through")
check(s["kind"] == "file_grep", "kind is file_grep")

print("\n=== settings: string value ===")
s = settings_probe("/p/settings.json", "editor.cursorStyle", "block")
check(eval_spec(s, SETTINGS_OK) is True, "matches quoted string value")
check(eval_spec(s, '{"editor.cursorStyle": "line"}') is False, "rejects a different string")

print("\n=== settings: the dot in a key is escaped, not a wildcard ===")
s = settings_probe("/p/settings.json", "editor.fontSize", 14)
check(eval_spec(s, '{"editorXfontSize": 14}') is False,
      "'.' does not match an arbitrary character")

print("\n=== keybindings: key + command on ONE entry ===")
k = keybinding_probe("/p/keybindings.json", "ctrl+shift+n",
                     "workbench.action.files.newUntitledFile")
check(len(k["patterns"]) == 1, "one brace-scoped pattern, not two independent ones")
check("proximity_lines" not in k, "does NOT rely on proximity_lines (see below)")
check(eval_spec(k, KB_OK) is True, "matches a correct single-entry binding")

print("\n=== keybindings: split key/command must NOT match  [SAFETY] ===")
check(eval_spec(k, KB_SPLIT) is False,
      "key on one entry + command on another is REJECTED")

# Why brace scoping rather than the schema's proximity_lines: proximity windows
# by LINE COUNT, so with compact one-line entries a 3-line window spans three
# separate bindings and false-passes. The safe window depends on the file's
# formatting, which we do not control.
prox_version = {
    "kind": "file_grep", "file_path": "/p/keybindings.json",
    "patterns": ['"key"\\s*:\\s*"ctrl\\+shift\\+n"',
                 '"command"\\s*:\\s*"workbench\\.action\\.files\\.newUntitledFile"'],
    "all_must_match": True, "proximity_lines": 3,
}
check(eval_spec(prox_version, KB_SPLIT) is True,
      "proximity_lines=3 WOULD false-pass on compact entries (why we scope by brace)")

print("\n=== keybindings: layout variations ===")
check(eval_spec(k, KB_WITH_WHEN) is True, "multi-line entry with a when clause")
check(eval_spec(k, '[{ "command": "workbench.action.files.newUntitledFile", '
                   '"key": "ctrl+shift+n" }]') is True, "reversed field order")
check(eval_spec(k, '[{ "key": "ctrl+shift+n", "command": '
                   '"workbench.action.files.newUntitledFile", "args": {"x": 1} }]') is True,
      "entry carrying a nested args object")
check(eval_spec(k, '[\n{ "key": "ctrl+shift+n", "command": "X" },\n\n\n\n'
                   '{ "key": "Y", "command": "workbench.action.files.newUntitledFile" }\n]') is False,
      "split with many blank lines between is still rejected")

print("\n=== derivation from real checkpoint predicates ===")
cp_setting = {
    "jsonc_file": "/home/user/.config/Code/User/settings.json",
    "eval": "isinstance(result, dict) and result.get('editor.fontSize') == 14",
}
p = probe_for_checkpoint(cp_setting)
check(p is not None and p["kind"] == "file_grep", "settings checkpoint -> file_grep")
check(p is not None and eval_spec(p, SETTINGS_OK) is True, "derived probe matches golden")
check(p is not None and "weaker_than_checkpoint" in p, "residual gap is declared")

cp_kb = {
    "jsonc_file": "/home/user/.config/Code/User/keybindings.json",
    "eval": ("isinstance(result, list) and any(isinstance(b, dict) and "
             "str(b.get('key', '')).lower() == 'ctrl+shift+n' and "
             "str(b.get('command', '')).lower() == "
             "'workbench.action.files.newuntitledfile' for b in result)"),
}
p = probe_for_checkpoint(cp_kb)
check(p is not None and len(p["patterns"]) == 1, "keybinding checkpoint -> one scoped pattern")
check(p is not None and eval_spec(p, KB_OK) is True, "derived probe matches the golden file")
check(p is not None and eval_spec(p, KB_SPLIT) is False, "derived probe rejects the split file")

print("\n=== unrecognised predicates emit nothing rather than something weak ===")
check(probe_for_checkpoint({"jsonc_file": "/p/x.json", "eval": "len(result) > 3"}) is None,
      "unknown predicate shape -> None")
check(probe_for_checkpoint({"eval": "result.get('a') == 1"}) is None, "no file path -> None")

print("\n=== KNOWN WEAKNESS, asserted so it is not mistaken for parity ===")
commented = '// "editor.fontSize": 14\n{ "editor.tabSize": 4 }'
s = settings_probe("/p/settings.json", "editor.fontSize", 14)
check(eval_spec(s, commented) is True,
      "regex DOES match inside a // comment (documented; our parser does not)")
malformed = "[\n    {'key': 'ctrl+shift+n', 'command': 'workbench.action.files.newUntitledFile'}\n]"
loose_kb = keybinding_probe("/p/k.json", "ctrl+shift+n", "workbench.action.files.newUntitledFile")
check(eval_spec(loose_kb, malformed) is False,
      "single-quoted malformed file does NOT match (double-quote anchoring helps here)")

print("\n" + ("ALL STRUCTAGENT PROBE TESTS PASSED" if not FAILURES
              else f"{len(FAILURES)} FAILURE(S): " + "; ".join(FAILURES)))
sys.exit(1 if FAILURES else 0)
