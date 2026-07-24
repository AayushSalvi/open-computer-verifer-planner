"""
Before/after proof for the OSWorld VSCode metric patch.

Runs the ORIGINAL metrics and the PATCHED ones over identical inputs and shows
both scores side by side. Two things must hold, and the second matters as much
as the first:

  1. every case the original got WRONG is now right
  2. every case the original got RIGHT is UNCHANGED — the patch must not buy
     correct-file handling by loosening the metric into accepting wrong files

Run:  python -m process_checks.patches.test_osworld_vscode_metrics --osworld <path>
"""
from __future__ import annotations

import argparse
import importlib.util
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from process_checks.patches import osworld_vscode_metrics as patched  # noqa: E402


def load_original(osworld_root: str):
    spec = importlib.util.spec_from_file_location(
        "osw_vscode_orig",
        str(Path(osworld_root) / "desktop_env/evaluators/metrics/vscode.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def w(text: str) -> str:
    f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
    f.write(text)
    f.close()
    return f.name


BIND = '{"key": "ctrl+shift+n", "command": "workbench.action.files.newUntitledFile"}'
WRONG = '{"key": "ctrl+alt+z", "command": "some.other.command"}'
WHEN = ('{"key": "ctrl+shift+n", "command": "workbench.action.files.newUntitledFile",'
        ' "when": "editorTextFocus"}')
EXP_S = {"expected": {"editor.fontSize": 14}}
EXP_K = {"expected": {"key": "ctrl+shift+n",
                      "command": "workbench.action.files.newUntitledFile"}}


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--osworld", required=True)
    a = p.parse_args()
    orig = load_original(a.osworld)

    # (metric, label, file text, rules, want)
    cases = [
        ("settings", "plain JSON",                '{"editor.fontSize": 14}',           EXP_S, 1.0),
        ("settings", "JSONC comment header",      '// s\n{"editor.fontSize": 14}',     EXP_S, 1.0),
        ("settings", "block comment",             '/* s */\n{"editor.fontSize": 14}',  EXP_S, 1.0),
        ("settings", "trailing comma",            '{"editor.fontSize": 14,}',          EXP_S, 1.0),
        ("settings", "WRONG value",               '{"editor.fontSize": 12}',           EXP_S, 0.0),
        ("settings", "malformed (single quotes)", "{'editor.fontSize': 14}",           EXP_S, 0.0),
        ("keys",     "plain array",               f"[{BIND}]",                         EXP_K, 1.0),
        ("keys",     "1-line comment header",     f"// h\n[{BIND}]",                   EXP_K, 1.0),
        ("keys",     "2-line comment header",     f"// a\n// b\n[{BIND}]",             EXP_K, 1.0),
        ("keys",     "binding with when clause",  f"[{WHEN}]",                         EXP_K, 1.0),
        ("keys",     "WRONG binding",             f"[{WRONG}]",                        EXP_K, 0.0),
        ("keys",     "malformed (single quotes)",
         "[{'key': 'ctrl+shift+n', 'command': 'workbench.action.files.newUntitledFile'}]",
         EXP_K, 0.0),
    ]

    fixed = unchanged = regressed = still_wrong = 0
    print(f"{'metric':9s} {'case':28s} {'orig':>6s} {'patch':>6s} {'want':>5s}   status")
    print("-" * 74)
    for metric, label, text, rules, want in cases:
        path = w(text)
        of = orig.check_json_settings if metric == "settings" else orig.check_json_keybindings
        pf = patched.check_json_settings if metric == "settings" else patched.check_json_keybindings
        try:
            o = float(of(path, rules))
        except Exception:
            o = None
        try:
            n = float(pf(path, rules))
        except Exception:
            n = None

        o_ok, n_ok = (o == want), (n == want)
        if not o_ok and n_ok:
            status, fixed = "FIXED", fixed + 1
        elif o_ok and n_ok:
            status, unchanged = "ok (unchanged)", unchanged + 1
        elif o_ok and not n_ok:
            status, regressed = "*** REGRESSION ***", regressed + 1
        else:
            status, still_wrong = "still wrong", still_wrong + 1
        print(f"{metric:9s} {label:28s} {str(o):>6s} {str(n):>6s} {want:>5.1f}   {status}")

    print("-" * 74)
    print(f"fixed: {fixed}   unchanged: {unchanged}   regressions: {regressed}   still wrong: {still_wrong}")
    if regressed:
        print("\nREGRESSION — the patch broke a case the original handled. Do not ship.")
        return 1
    print("\nNo regressions. The patch fixes the false negatives without accepting "
          "anything the original correctly rejected.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
