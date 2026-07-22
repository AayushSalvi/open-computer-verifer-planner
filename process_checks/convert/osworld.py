"""
OSWorld task.json  ->  our per-condition checkpoint spec.

WHY
---
OSWorld evaluators already carry the getter/metric split we use, and a subset
carry MULTIPLE metrics conjoined into one score. Their scorer collapses those
to a single float — and worse, short-circuits:

    # desktop_env.py:559
    if self.metric_conj == 'and' and float(metric) == 0.0:
        return 0        # remaining conditions are NEVER evaluated

So on a failed run, every condition after the first failure is not merely
unreported — it was never computed. Converting to per-condition records and
evaluating each independently recovers signal that does not exist upstream.

THE `or` TRAP
-------------
`conj` defaults to "and" (desktop_env.py:403), but 25 tasks declare `conj:
"or"`. There, the metrics are ALTERNATIVE ACCEPTABLE SOLUTIONS, not milestones.
Splitting them would report "condition B failed" when the agent legitimately
satisfied A — manufacturing exactly the false negatives this project exists to
prevent. We therefore refuse to decompose `or` tasks and emit a single opaque
condition instead.

WHAT THIS DOES NOT DO
---------------------
Convert is authoring-time only. It produces candidate conditions; it does NOT
validate them. Every emitted condition still has to clear the gates in
PROJECT.md before it is trusted — these are inherited from evaluators we have
already proven can be wrong (see the JSONC defect in OSWorld's own
metrics/vscode.py).
"""
from __future__ import annotations

import json
from pathlib import Path

# Getter type -> our interface-record channel.
# PROJECT.md defines: file | git | sqlite | cdp | cli
# Every mapping below was verified by reading the getter's implementation in
# osworld_eval/desktop_env/evaluators/getters/. Getters left out are reported as
# "unknown" rather than guessed — a mis-tagged channel is a silent lie about
# where evidence came from.
_CHANNEL_BY_GETTER = {
    # --- on-disk files -----------------------------------------------------
    "vm_file": "file",
    "cache_file": "file",
    "gimp_config_file": "file",
    "vlc_config": "file",
    "googledrive_file": "file",
    "info_from_json": "file",
    "vm_wallpaper": "file",
    "audio_in_slide": "file",
    "find_unpacked_extension_path": "file",
    "shortcuts_on_desktop": "file",
    # Chrome Bookmarks + Preferences are JSON on disk (verified: chrome.py)
    "bookmarks": "file",
    "enable_do_not_track": "file",
    "new_startup_page": "file",
    "profile_name": "file",
    # --- SQLite ------------------------------------------------------------
    # Chrome History / Cookies are SQLite DBs (verified: chrome.py get_history,
    # get_cookie_data). Matches PROJECT.md's inspection-channel cheat sheet.
    "history": "sqlite",
    "cookie_data": "sqlite",
    # --- shell -------------------------------------------------------------
    "vm_command_line": "cli",
    "vm_command_error": "cli",
    "vm_terminal_output": "cli",
    # runs `code --list-extensions`-style commands (verified: vscode.py)
    "vscode_config": "cli",
    # --- browser / live app introspection ----------------------------------
    "active_url_from_accessTree": "cdp",
    "active_tab_html_parse": "cdp",
    "active_tab_url_parse": "cdp",
    "active_tab_info": "cdp",
    "open_tabs_info": "cdp",
    "page_info": "cdp",
    "url_dashPart": "cdp",
    "url_path_parse": "cdp",
    # --- channels NOT in PROJECT.md's enum (flagged on use) ----------------
    "accessibility_tree": "a11y",
    "vlc_playing_info": "http",   # VLC HTTP interface (verified: vlc.py)
    "rule": "rule",
    "rule_relativeTime": "rule",
}

# Channels PROJECT.md's interface contract currently allows.
KNOWN_CHANNELS = {"file", "git", "sqlite", "cdp", "cli"}


def _as_list(value, n: int) -> list:
    """Normalize a field that may be a scalar, a list, or absent."""
    if value is None:
        return [None] * n
    if isinstance(value, list):
        return value + [None] * (n - len(value))
    return [value] + [None] * (n - 1)


def _channel_for(getter) -> tuple[str, str | None]:
    """Return (channel, warning). Unknown getters are surfaced, never guessed."""
    if not isinstance(getter, dict):
        return "unknown", "getter is not a dict"
    gtype = getter.get("type", "")
    ch = _CHANNEL_BY_GETTER.get(gtype)
    if ch is None:
        return "unknown", f"unmapped getter type {gtype!r}"
    if ch not in KNOWN_CHANNELS:
        return ch, f"channel {ch!r} (from getter {gtype!r}) is not in PROJECT.md's channel enum"
    return ch, None


def convert(task: dict) -> dict:
    """Convert one OSWorld task dict into our checkpoint spec.

    Always returns a spec. `decomposable` says whether the conditions are
    genuine independent milestones; `warnings` carries anything a human should
    look at before trusting the output.
    """
    task_id = task.get("id", "?")
    ev = task.get("evaluator") or {}
    func = ev.get("func")
    warnings: list[str] = []

    spec = {
        "task_id": task_id,
        "source": "osworld",
        "instruction": task.get("instruction", ""),
        "decomposable": False,
        "checkpoints": [],
        "warnings": warnings,
    }

    if func in (None, "infeasible"):
        warnings.append(f"no usable evaluator (func={func!r}) — nothing to convert")
        return spec

    # OSWorld default when `conj` is absent is "and" (desktop_env.py:403).
    conj = ev.get("conj", "and")

    if ev.get("postconfig"):
        # These steps (restart the app, sleep, …) exist because many apps buffer
        # state in memory and only flush on exit. Ignoring them silently is a
        # false-negative source; see PROJECT.md on flush-before-read.
        spec["postconfig"] = ev["postconfig"]
        warnings.append(
            f"evaluator declares {len(ev['postconfig'])} postconfig step(s): state must be "
            "flushed before reading, or checks may see stale state"
        )

    funcs = func if isinstance(func, list) else [func]
    n = len(funcs)
    results = _as_list(ev.get("result"), n)
    expecteds = _as_list(ev.get("expected"), n)
    options = _as_list(ev.get("options"), n)

    # --- the `or` trap: alternatives, not milestones -------------------------
    if n > 1 and conj != "and":
        warnings.append(
            f"conj={conj!r}: these {n} metrics are ALTERNATIVE acceptable solutions, "
            "not milestones — decomposing them would produce false negatives. "
            "Emitted as one opaque condition."
        )
        spec["checkpoints"] = [{
            "id": f"c1_{conj}_of_{n}",
            "channel": "unknown",
            "description": f"{conj} over {n} alternative checks: {', '.join(map(str, funcs))}",
            "osworld": {"func": funcs, "conj": conj, "result": ev.get("result"),
                        "expected": ev.get("expected"), "options": ev.get("options")},
        }]
        return spec

    # --- the decomposable case ----------------------------------------------
    spec["decomposable"] = n > 1
    for i, fname in enumerate(funcs):
        channel, warn = _channel_for(results[i])
        if warn:
            warnings.append(f"condition {i+1} ({fname}): {warn}")
        cp = {
            "id": f"c{i+1}_{fname}",
            "channel": channel,
            "description": f"{fname} over {(results[i] or {}).get('type', '?')}"
                           if isinstance(results[i], dict) else str(fname),
            "osworld": {"func": fname, "result": results[i], "expected": expecteds[i]},
        }
        if options[i] is not None:
            cp["osworld"]["options"] = options[i]
        spec["checkpoints"].append(cp)

    return spec


def convert_file(path) -> dict:
    with open(path, encoding="utf-8") as f:
        return convert(json.load(f))


def convert_tree(root) -> list[dict]:
    """Convert every task JSON under `root`. Returns specs in path order."""
    out = []
    for p in sorted(Path(root).rglob("*.json")):
        try:
            with open(p, encoding="utf-8") as f:
                task = json.load(f)
        except Exception:
            continue
        if not isinstance(task, dict) or "evaluator" not in task:
            continue
        spec = convert(task)
        spec["source_path"] = str(p)
        spec["domain"] = Path(p).parent.name
        out.append(spec)
    return out


# ---------------------------------------------------------------------------
# CLI — emit the converted specs to disk
# ---------------------------------------------------------------------------

def _write_specs(specs: list[dict], out_dir: Path, only_decomposable: bool) -> tuple[int, int]:
    """Write one checkpoints.json per task, mirroring our repo layout:
        <out_dir>/<domain>/<task_id>/checkpoints.json
    """
    written = skipped = 0
    for spec in specs:
        if only_decomposable and not spec["decomposable"]:
            skipped += 1
            continue
        if not spec["checkpoints"]:
            skipped += 1
            continue
        d = out_dir / spec.get("domain", "unknown") / spec["task_id"]
        d.mkdir(parents=True, exist_ok=True)
        with open(d / "checkpoints.json", "w", encoding="utf-8") as f:
            json.dump(spec, f, indent=2, ensure_ascii=False)
        written += 1
    return written, skipped


def main() -> int:
    import argparse
    import collections

    p = argparse.ArgumentParser(
        description="Convert an OSWorld task corpus into per-condition checkpoint specs."
    )
    p.add_argument("corpus", help="root dir of OSWorld task JSONs")
    p.add_argument("-o", "--out-dir", required=True, help="where to write specs")
    p.add_argument("--all", action="store_true",
                   help="also write single/opaque tasks (default: decomposable only)")
    args = p.parse_args()

    specs = convert_tree(args.corpus)
    out_dir = Path(args.out_dir)
    written, skipped = _write_specs(specs, out_dir, only_decomposable=not args.all)

    decomp = [s for s in specs if s["decomposable"]]
    or_trapped = [s for s in specs if any("ALTERNATIVE" in w for w in s["warnings"])]
    channels = collections.Counter(c["channel"] for s in specs for c in s["checkpoints"])

    index = {
        "corpus": str(args.corpus),
        "tasks_seen": len(specs),
        "decomposable_tasks": len(decomp),
        "conditions_from_decomposable": sum(len(s["checkpoints"]) for s in decomp),
        "or_tasks_left_opaque": len(or_trapped),
        "tasks_without_evaluator": sum(1 for s in specs if not s["checkpoints"]),
        "channels": dict(channels),
        "written": written,
        "skipped": skipped,
        "unvalidated": True,
        "note": ("These are CANDIDATES converted from OSWorld evaluators. None have "
                 "passed PROJECT.md's gates yet, and the upstream evaluators are known "
                 "to contain defects (see the JSONC bug in metrics/vscode.py)."),
        "tasks": [
            {"task_id": s["task_id"], "domain": s.get("domain"),
             "conditions": len(s["checkpoints"]),
             "instruction": s["instruction"][:120],
             "warnings": s["warnings"]}
            for s in decomp
        ],
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    with open(out_dir / "index.json", "w", encoding="utf-8") as f:
        json.dump(index, f, indent=2, ensure_ascii=False)

    print(f"tasks seen:            {len(specs)}")
    print(f"  decomposable:        {len(decomp)}  -> {index['conditions_from_decomposable']} conditions")
    print(f"  'or' left opaque:    {len(or_trapped)}")
    print(f"  no evaluator:        {index['tasks_without_evaluator']}")
    print(f"channels: {dict(channels)}")
    print(f"\nwrote {written} spec(s), skipped {skipped}")
    print(f"index: {out_dir / 'index.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
