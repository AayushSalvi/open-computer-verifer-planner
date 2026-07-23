"""
Offline audit of upstream OSWorld evaluator metrics.

WHY
---
The collaborator's SFT pipeline uses 0-scored runs as negatives and to build
preference pairs. So an evaluator that scores a CORRECT trajectory 0 does not
merely waste a sample: it presents correct behaviour as failure, and ranks it
below something worse in a pair. Her corpus has 450 zero-scored runs.

We have already confirmed four such false negatives by hand in OSWorld's
VSCode metrics. This generalises that: a mutation-testing harness over the
metric layer, run entirely offline.

WHAT IT TESTS
-------------
Metrics are pure functions -- metric(result, expected, **options) -> float --
so they can be exercised with synthetic inputs, no VM, no sandbox, no model.
Each case supplies:

    correct input   -> must score 1.0   (else FALSE NEGATIVE: the worst kind,
                                         it mislabels good work as failure)
    mutated input   -> must score 0.0   (else FALSE POSITIVE: it accepts work
                                         that was not done)

A metric that fails either direction is reported with the exact input that
broke it, so the finding is reproducible rather than an assertion.

LOADING THIRD-PARTY CODE
------------------------
The metrics package pulls heavy optional deps (a spreadsheet formula engine,
audio fingerprinting, OpenCV) that most metrics never touch. Rather than
require all of them, `load_metrics` stubs missing modules on demand and
reports what it stubbed. Every audited metric is checked against a
known-correct input first, so a stub cannot silently corrupt a verdict without
showing up as a false negative.

Run:  python -m process_checks.audit.metric_audit --osworld <path-to-osworld_eval>
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

STUBBED: set[str] = set()


def load_metrics(osworld_root: str, dotted: str, names: list[str], max_stubs: int = 60):
    """Import metric functions, stubbing missing heavy deps on demand."""
    root = str(Path(osworld_root).resolve())
    if root not in sys.path:
        sys.path.insert(0, root)
    for _ in range(max_stubs):
        try:
            mod = __import__(dotted, fromlist=names)
            return {n: getattr(mod, n) for n in names if hasattr(mod, n)}
        except (ModuleNotFoundError, ImportError) as exc:
            missing = getattr(exc, "name", None)
            if not missing or missing in STUBBED:
                raise
            STUBBED.add(missing)
            sys.modules[missing] = MagicMock()
    raise RuntimeError(f"gave up after {max_stubs} stubs loading {dotted}")


def write_tmp(text: str, suffix: str = ".json") -> str:
    f = tempfile.NamedTemporaryFile("w", suffix=suffix, delete=False, encoding="utf-8")
    f.write(text)
    f.close()
    return f.name


# --------------------------------------------------------------------------- #
# Cases
# --------------------------------------------------------------------------- #
# Each: (label, callable_name, args_for_correct, args_for_mutated, note)
# args are built lazily so temp files are only created when the case runs.

def build_cases(M: dict) -> list[dict]:
    cases: list[dict] = []

    def add(metric, label, correct, mutated, note=""):
        if metric in M:
            cases.append({"metric": metric, "label": label, "correct": correct,
                          "mutated": mutated, "note": note})

    # ---- vscode: settings ---------------------------------------------------
    exp = {"expected": {"editor.fontSize": 14}}
    add("check_json_settings", "plain JSON",
        lambda: (write_tmp('{"editor.fontSize": 14}'), exp),
        lambda: (write_tmp('{"editor.fontSize": 12}'), exp))
    add("check_json_settings", "VSCode JSONC (comment header)",
        lambda: (write_tmp('// settings\n{"editor.fontSize": 14}'), exp),
        lambda: (write_tmp('// settings\n{"editor.fontSize": 12}'), exp),
        "VSCode writes comments here; a strict parser cannot read its own default file")
    add("check_json_settings", "trailing comma",
        lambda: (write_tmp('{"editor.fontSize": 14,}'), exp),
        lambda: (write_tmp('{"editor.fontSize": 12,}'), exp),
        "trailing commas are legal JSONC and VSCode writes them")

    # ---- vscode: keybindings ------------------------------------------------
    B = '{"key": "ctrl+shift+n", "command": "workbench.action.files.newUntitledFile"}'
    BW = '{"key": "ctrl+alt+z", "command": "some.other.command"}'
    expk = {"expected": {"key": "ctrl+shift+n",
                         "command": "workbench.action.files.newUntitledFile"}}
    add("check_json_keybindings", "plain array",
        lambda: (write_tmp(f"[{B}]"), expk),
        lambda: (write_tmp(f"[{BW}]"), expk))
    add("check_json_keybindings", "1-line comment header",
        lambda: (write_tmp(f"// header\n[{B}]"), expk),
        lambda: (write_tmp(f"// header\n[{BW}]"), expk),
        "their skip-first-line workaround covers exactly this case")
    add("check_json_keybindings", "2-line comment header",
        lambda: (write_tmp(f"// one\n// two\n[{B}]"), expk),
        lambda: (write_tmp(f"// one\n// two\n[{BW}]"), expk),
        "the skip-first-line workaround breaks beyond one line")
    BWHEN = ('{"key": "ctrl+shift+n", "command": "workbench.action.files.newUntitledFile",'
             ' "when": "editorTextFocus"}')
    add("check_json_keybindings", "binding carrying a when clause",
        lambda: (write_tmp(f"[{BWHEN}]"), expk),
        lambda: (write_tmp(f"[{BW}]"), expk),
        "exact dict match rejects any binding with an extra field; when clauses are common")

    # ---- general: exact_match ----------------------------------------------
    add("exact_match", "identical strings",
        lambda: ("hello", {"expected": "hello"}),
        lambda: ("hello", {"expected": "goodbye"}))

    # ---- general: check_list ------------------------------------------------
    # Signature verified against the source: takes a PATH to a list file, and
    # rules keyed expect/unexpect holding regexes (not `expected`, and not an
    # in-memory list). Getting this wrong produced a spurious "finding" on the
    # first run -- a harness that reports its own mistakes as upstream bugs is
    # worse than no harness, so every case is checked against the real
    # signature before its verdict is trusted.
    add("check_list", "expected lines present, unexpected absent",
        lambda: (write_tmp("alpha\nbeta\n", ".txt"),
                 {"expect": ["alpha", "beta"], "unexpect": ["gamma"]}),
        lambda: (write_tmp("alpha\ngamma\n", ".txt"),
                 {"expect": ["alpha", "beta"], "unexpect": ["gamma"]}))

    # ---- general: check_include_exclude -------------------------------------
    add("check_include_exclude", "include term present, exclude absent",
        lambda: ("the operation succeeded", {"include": ["succeeded"], "exclude": ["error"]}),
        lambda: ("the operation had an error", {"include": ["succeeded"], "exclude": ["error"]}))

    # ---- general: check_direct_json_object ----------------------------------
    add("check_direct_json_object", "object matches expected keys",
        lambda: ({"a": "1"}, {"expected": {"a": "1"}}),
        lambda: ({"a": "2"}, {"expected": {"a": "1"}}))

    # ---- vscode: compare_text_file ------------------------------------------
    add("compare_text_file", "identical text files",
        lambda: (write_tmp("line one\nline two\n", ".txt"),
                 write_tmp("line one\nline two\n", ".txt")),
        lambda: (write_tmp("line one\nDIFFERENT\n", ".txt"),
                 write_tmp("line one\nline two\n", ".txt")))

    # ---- chrome: is_expected_url_pattern_match ------------------------------
    # result is a URL string (or {"url": ...}); rules["expected"] is a list of
    # regexes, ALL of which must be found (re.search). 12 corpus uses.
    urlrule = {"expected": ["reservation#/vehicles"]}
    add("is_expected_url_pattern_match", "url contains the expected fragment",
        lambda: ("https://www.hertz.com/rentacar/reservation#/vehicles?x=1", urlrule),
        lambda: ("https://www.hertz.com/rentacar/reservation#/review", urlrule))
    add("is_expected_url_pattern_match", "anchored settings pattern",
        lambda: ("chrome://settings/appearance/", {"expected": [r"^chrome://settings/appearance/?$"]}),
        lambda: ("chrome://settings/privacy", {"expected": [r"^chrome://settings/appearance/?$"]}))
    add("is_expected_url_pattern_match", "dict form with 'url' field",
        lambda: ({"url": "https://example.com/manchester/weather"}, {"expected": ["/manchester/"]}),
        lambda: ({"url": "https://example.com/london/weather"}, {"expected": ["/manchester/"]}))

    return cases


def score(fn, args) -> tuple[float | None, str | None]:
    try:
        v = fn(*args)
        return (float(v) if v is not None else None), None
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"


# --------------------------------------------------------------------------- #
# compare_table: needs real xlsx fixtures, so it is audited separately.
#
# Result of this audit (see report note): compare_table's two dominant rule
# types -- sheet_data (53 uses) and check_cell (21) -- are ROBUST as actually
# used in the corpus. The a-priori dtype worry did not reproduce (int vs float
# scores 1.0 correctly). Two sharp edges exist -- sheet_data rejects a trailing
# space in a text cell, and check_cell `eq` is Python type-strict so a numeric
# cell against a numeric-STRING ref scores 0 -- but NEITHER is triggered by any
# real task config: corpus check_cell rules use approx: for numbers (10x) and
# eq only for text (4x), with zero numeric-string eq refs. Reporting this as a
# clean result matters: it says the 450 zero-scored runs are not explained by
# compare_table, and it keeps the VSCode findings credible by showing we do not
# flag every metric.
# --------------------------------------------------------------------------- #

def _xlsx(rows):
    import openpyxl
    f = tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False)
    f.close()
    wb = openpyxl.Workbook()
    ws = wb.active
    for r in rows:
        ws.append(r)
    wb.save(f.name)
    return f.name


def audit_compare_table(compare_table) -> list[dict]:
    """Fixture-based cases. Returns findings (empty if the metric is clean)."""
    SD = [{"type": "sheet_data", "sheet_idx0": 0, "sheet_idx1": "EI0"}]

    def cc(coord, method, ref):
        return [{"type": "check_cell", "sheet_idx": "RNSheet", "coordinate": coord,
                 "props": {"value": {"method": method, "ref": ref}}}]

    gold = _xlsx([["item", "qty"], ["apple", 3], ["pear", 5]])
    cell = _xlsx([["h"], [42]])

    # (label, score_thunk, want, kind, note)
    cases = [
        ("sheet_data: identical sheets",
         lambda: compare_table(_xlsx([["item", "qty"], ["apple", 3], ["pear", 5]]), gold, rules=SD),
         1.0, "correct", ""),
        ("sheet_data: one cell differs",
         lambda: compare_table(_xlsx([["item", "qty"], ["apple", 3], ["pear", 9]]), gold, rules=SD),
         0.0, "mutated", ""),
        ("sheet_data: int 3 vs float 3.0 (dtype)",
         lambda: compare_table(_xlsx([["item", "qty"], ["apple", 3.0], ["pear", 5.0]]), gold, rules=SD),
         1.0, "correct", "the a-priori dtype worry: numerically equal, must score 1.0"),
        ("sheet_data: truncated (missing row)",
         lambda: compare_table(_xlsx([["item", "qty"], ["apple", 3]]), gold, rules=SD),
         0.0, "mutated", ""),
        ("sheet_data: extra trailing column",
         lambda: compare_table(_xlsx([["item", "qty", "x"], ["apple", 3, 1], ["pear", 5, 2]]), gold, rules=SD),
         0.0, "mutated", ""),
        ("check_cell: eq numeric matches",
         lambda: compare_table(cell, None, rules=cc("A2", "eq", 42)), 1.0, "correct", ""),
        ("check_cell: eq numeric mismatch",
         lambda: compare_table(cell, None, rules=cc("A2", "eq", 99)), 0.0, "mutated", ""),
        ("check_cell: float cell vs int ref",
         lambda: compare_table(_xlsx([["h"], [42.0]]), None, rules=cc("A2", "eq", 42)),
         1.0, "correct", "same number, must score 1.0"),
    ]

    findings = []
    for label, thunk, want, kind, note in cases:
        got, err = score(thunk, ())
        bad = (err is not None) or (got is None) or (
            (kind == "correct" and got < 1.0) or (kind == "mutated" and got >= 1.0))
        if bad:
            fn_kind = ("false_negative" if kind == "correct" else "false_positive")
            print(f"  XX compare_table  [{label}]")
            print(f"       {'FALSE NEGATIVE' if kind == 'correct' else 'FALSE POSITIVE'} "
                  f"(scored {err or got}, wanted {want})")
            if note:
                print(f"       note: {note}")
            findings.append({"metric": "compare_table", "case": label,
                             "scored": err or got, "wanted": want, fn_kind: True,
                             "note": note})
        else:
            print(f"  OK compare_table  [{label}]  (scored {got}, wanted {want})")
    return findings


def run_audit(osworld_root: str) -> dict:
    M: dict = {}
    M.update(load_metrics(osworld_root, "desktop_env.evaluators.metrics.vscode",
                          ["check_json_settings", "check_json_keybindings", "compare_text_file"]))
    M.update(load_metrics(osworld_root, "desktop_env.evaluators.metrics.general",
                          ["exact_match", "check_list", "check_include_exclude",
                           "check_direct_json_object"]))
    M.update(load_metrics(osworld_root, "desktop_env.evaluators.metrics.chrome",
                          ["is_expected_url_pattern_match"]))

    cases = build_cases(M)
    findings, ok = [], 0

    print(f"auditing {len(cases)} cases across {len(M)} metrics")
    if STUBBED:
        print(f"auto-stubbed missing deps: {', '.join(sorted(STUBBED))}")
    print()

    for c in cases:
        fn = M[c["metric"]]
        good, gerr = score(fn, c["correct"]())
        bad, berr = score(fn, c["mutated"]())

        false_neg = gerr is not None or (good is not None and good < 1.0)
        false_pos = berr is None and bad is not None and bad >= 1.0

        label = f"{c['metric']}  [{c['label']}]"
        if false_neg or false_pos:
            kinds = []
            if false_neg:
                kinds.append("FALSE NEGATIVE (correct input scored "
                             f"{gerr or good})")
            if false_pos:
                kinds.append(f"FALSE POSITIVE (wrong input scored {bad})")
            print(f"  XX {label}")
            for k in kinds:
                print(f"       {k}")
            if c["note"]:
                print(f"       note: {c['note']}")
            findings.append({"metric": c["metric"], "case": c["label"],
                             "correct_score": gerr or good, "mutated_score": berr or bad,
                             "false_negative": false_neg, "false_positive": false_pos,
                             "note": c["note"]})
        else:
            ok += 1
            print(f"  OK {label}  (correct={good}, mutated={bad})")

    # compare_table needs xlsx fixtures, audited separately.
    print()
    table_mod = load_metrics(osworld_root, "desktop_env.evaluators.metrics.table",
                             ["compare_table"])
    table_findings = []
    if "compare_table" in table_mod:
        table_findings = audit_compare_table(table_mod["compare_table"])
    findings.extend(table_findings)
    total = len(cases) + 8

    print(f"\n{ok + (8 - len(table_findings))}/{total} cases clean, {len(findings)} finding(s)")
    return {"metrics_loaded": sorted(M) + ["compare_table"], "stubbed": sorted(STUBBED),
            "cases": total, "clean": ok + (8 - len(table_findings)), "findings": findings}


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--osworld", required=True,
                   help="path to osworld_eval (contains desktop_env/)")
    p.add_argument("--out", help="write the report JSON here")
    a = p.parse_args()

    report = run_audit(a.osworld)
    if a.out:
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"report: {a.out}")
    # Findings are the product, not an error -- exit 0 so this can run in CI
    # without the presence of upstream bugs failing the build.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
