"""
Tests for the OSWorld -> checkpoint converter.

The load-bearing test is `test_or_is_never_decomposed`. Splitting an `or`
evaluator into separate conditions would report "condition B failed" when the
agent legitimately satisfied A — a manufactured false negative, which is the
single worst failure mode for a reward signal. That property is asserted
directly here rather than inferred from aggregate counts.

Run:  python -m process_checks.convert.test_osworld
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from process_checks.convert.osworld import convert  # noqa: E402

FAILURES: list[str] = []


def check(cond: bool, label: str) -> None:
    print(f"  {'OK ' if cond else 'XX '} {label}")
    if not cond:
        FAILURES.append(label)


def task(evaluator, **kw):
    return {"id": kw.get("id", "t1"), "instruction": kw.get("instruction", "do a thing"),
            "evaluator": evaluator}


print("=== conj='and' decomposes into one condition per metric ===")
s = convert(task({
    "func": ["is_expected_url_pattern_match", "check_direct_json_object"],
    "conj": "and",
    "result": [{"type": "active_url_from_accessTree"}, {"type": "vm_file"}],
    "expected": [{"type": "rule"}, {"type": "rule"}],
}))
check(s["decomposable"] is True, "marked decomposable")
check(len(s["checkpoints"]) == 2, "2 conditions emitted")
check(s["checkpoints"][0]["channel"] == "cdp", "condition 1 channel = cdp")
check(s["checkpoints"][1]["channel"] == "file", "condition 2 channel = file")
check(s["checkpoints"][0]["osworld"]["func"] == "is_expected_url_pattern_match",
      "condition 1 keeps its own func")
check(s["checkpoints"][0]["osworld"]["result"] == {"type": "active_url_from_accessTree"},
      "condition 1 keeps its own getter (not the whole list)")

print("\n=== absent conj defaults to 'and' (desktop_env.py:403) ===")
s = convert(task({
    "func": ["a", "b"],
    "result": [{"type": "vm_file"}, {"type": "vm_file"}],
    "expected": [None, None],
}))
check(s["decomposable"] is True, "no-conj task is decomposed (default 'and')")
check(len(s["checkpoints"]) == 2, "2 conditions emitted")

print("\n=== conj='or' is NEVER decomposed  [SAFETY] ===")
s = convert(task({
    "func": ["save_as_pdf", "save_as_docx"],
    "conj": "or",
    "result": [{"type": "vm_file"}, {"type": "vm_file"}],
    "expected": [{"type": "rule"}, {"type": "rule"}],
}))
check(s["decomposable"] is False, "NOT marked decomposable")
check(len(s["checkpoints"]) == 1, "collapsed to exactly 1 opaque condition")
check(any("ALTERNATIVE" in w for w in s["warnings"]), "warns that these are alternatives")
check(s["checkpoints"][0]["osworld"]["func"] == ["save_as_pdf", "save_as_docx"],
      "opaque condition retains the full disjunction")

print("\n=== single-func task ===")
s = convert(task({"func": "exact_match", "result": {"type": "vm_file"},
                  "expected": {"type": "rule"}}))
check(s["decomposable"] is False, "single func is not 'decomposable'")
check(len(s["checkpoints"]) == 1, "1 condition emitted")
check(s["checkpoints"][0]["channel"] == "file", "channel inferred")

print("\n=== infeasible / missing evaluator ===")
for fn in ("infeasible", None):
    s = convert(task({"func": fn}))
    check(s["checkpoints"] == [], f"func={fn!r}: no conditions emitted")
    check(any("no usable evaluator" in w for w in s["warnings"]), f"func={fn!r}: warned")

print("\n=== postconfig is preserved AND flagged (stale-state risk) ===")
s = convert(task({
    "func": "exact_match", "result": {"type": "vm_file"}, "expected": {"type": "rule"},
    "postconfig": [{"type": "launch", "parameters": {"command": ["pkill", "chrome"]}}],
}))
check("postconfig" in s, "postconfig carried through")
check(any("flushed" in w for w in s["warnings"]), "warns state must be flushed before reading")

print("\n=== unmapped getter is surfaced, never guessed ===")
s = convert(task({"func": "f", "result": {"type": "totally_made_up_getter"},
                  "expected": None}))
check(s["checkpoints"][0]["channel"] == "unknown", "channel = unknown")
check(any("unmapped getter" in w for w in s["warnings"]), "warns about the unmapped getter")

print("\n=== channels outside PROJECT.md's enum are flagged ===")
s = convert(task({"func": "f", "result": {"type": "accessibility_tree"}, "expected": None}))
check(s["checkpoints"][0]["channel"] == "a11y", "a11y mapped")
check(any("not in PROJECT.md" in w for w in s["warnings"]), "flagged as outside the enum")

print("\n=== verified channel mappings (read from getter sources) ===")
for gtype, want in [("history", "sqlite"), ("cookie_data", "sqlite"),
                    ("bookmarks", "file"), ("enable_do_not_track", "file"),
                    ("vscode_config", "cli"), ("vm_terminal_output", "cli"),
                    ("vlc_playing_info", "http")]:
    s = convert(task({"func": "f", "result": {"type": gtype}, "expected": None}))
    check(s["checkpoints"][0]["channel"] == want, f"{gtype} -> {want}")

print("\n" + ("ALL CONVERTER TESTS PASSED" if not FAILURES
              else f"{len(FAILURES)} FAILURE(S): " + "; ".join(FAILURES)))
sys.exit(1 if FAILURES else 0)
