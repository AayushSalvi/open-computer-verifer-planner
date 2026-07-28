"""
Golden-state auto-generation for VSCode — Phase 2, the inverse of Phase 1.

Phase 1's registry BINDS an {op, params} to a checkpoint predicate ("this file
has this key = this value"). This module does the mirror: it PLANTS the state
that satisfies the predicate. Because both come from the same {op, params}, the
generated golden satisfies the generated checkpoint by construction.

That "by construction" is also the trap: our checkpoint passing on our golden
proves nothing (circular). The real validation is against an INDEPENDENT oracle
— the task's authored verification[], which never touched our spec. Plant the
golden, run the authored verify_task; if it passes, the golden is genuinely
correct. That step needs a sandbox and lives in the server-side driver; here we
only do the offline necessary-condition check (our own checkpoints all pass).

Multiple ops targeting one file MERGE (three setting_equals -> one settings.json,
not three overwrites) — the same grouping the probe dedup used.

Ops that cannot be faked by writing a file (extension_installed needs a real
extension dir) are reported as `unplantable`, not silently skipped.
"""
from __future__ import annotations

import json

from process_checks.auto.registry_vscode import KEYBINDINGS, SETTINGS, USER_DIR

# accumulator entry kinds
_DICT, _LIST, _RAW = "dict", "list", "raw"


def _snippets_path(language: str) -> str:
    return f"{USER_DIR}/snippets/{language}.json"


def _ws(workspace: str, name: str) -> str:
    return f"{workspace}/.vscode/{name}"


class GoldenError(ValueError):
    pass


# --------------------------------------------------------------------------- #
# per-op planters: each contributes to a per-file accumulator
# acc[path] = [kind, data]
# --------------------------------------------------------------------------- #

def _ensure(acc, path, kind):
    if path not in acc:
        acc[path] = [kind, {} if kind == _DICT else ([] if kind == _LIST else set())]
    if acc[path][0] != kind:
        raise GoldenError(f"file {path} wanted as {acc[path][0]} and {kind}")
    return acc[path][1]


def _plant_setting(acc, p):
    _ensure(acc, SETTINGS, _DICT)[p["key"]] = p["value"]


def _plant_workspace_setting(acc, p):
    _ensure(acc, _ws(p["workspace"], "settings.json"), _DICT)[p["key"]] = p["value"]


def _plant_keybinding(acc, p):
    _ensure(acc, KEYBINDINGS, _LIST).append({"key": p["key"], "command": p["command"]})


def _plant_snippet(acc, p):
    d = _ensure(acc, _snippets_path(p["language"]), _DICT)
    name = f"snippet_{p['prefix']}"
    d[name] = {"prefix": p["prefix"], "body": ["$0"], "description": name}


def _plant_ws_ext(acc, p):
    path = _ws(p["workspace"], "extensions.json")
    d = _ensure(acc, path, _DICT)
    d.setdefault("recommendations", [])
    if p["extension_id"] not in d["recommendations"]:
        d["recommendations"].append(p["extension_id"])


def _plant_task(acc, p):
    path = _ws(p["workspace"], "tasks.json")
    d = _ensure(acc, path, _DICT)
    d.setdefault("version", "2.0.0")
    d.setdefault("tasks", [])
    d["tasks"].append({"label": p["label"], "type": "shell", "command": "true"})


def _plant_launch(acc, p):
    path = _ws(p["workspace"], "launch.json")
    d = _ensure(acc, path, _DICT)
    d.setdefault("version", "0.2.0")
    d.setdefault("configurations", [])
    d["configurations"].append({"name": p["name"], "type": "python", "request": "launch"})


def _plant_file_exists(acc, p):
    _ensure(acc, p["path"], _RAW)  # empty content is enough to exist


def _plant_file_contains(acc, p):
    _ensure(acc, p["path"], _RAW).add(p["substring"])


_PLANTERS = {
    "setting_equals": _plant_setting,
    "workspace_setting_equals": _plant_workspace_setting,
    "keybinding_bound": _plant_keybinding,
    "snippet_exists": _plant_snippet,
    "workspace_extension_recommended": _plant_ws_ext,
    "task_defined": _plant_task,
    "launch_config_exists": _plant_launch,
    "file_exists": _plant_file_exists,
    "file_contains": _plant_file_contains,
}

# ops with no file-only golden — they need the real app/extension dir
_UNPLANTABLE = {"extension_installed"}


def _serialize(acc) -> dict:
    """Turn the accumulator into {path: file_text}."""
    out = {}
    for path, (kind, data) in acc.items():
        if kind == _DICT:
            out[path] = json.dumps(data, indent=4)
        elif kind == _LIST:
            out[path] = json.dumps(data, indent=4)
        else:  # raw: a file that must contain each required substring
            out[path] = "\n".join(sorted(data)) + ("\n" if data else "")
    return out


def criteria_to_golden(criteria: list[dict]) -> dict:
    """Build the golden state from a decomposition's T1 criteria.

    Returns {"files": {path: text}, "unplantable": [...], "skipped": [...]}.
    """
    acc: dict = {}
    unplantable, skipped = [], []
    for c in criteria:
        if not isinstance(c, dict) or c.get("tier") != "T1":
            continue
        bind = c.get("bind") or {}
        op = bind.get("op")
        params = bind.get("params") or {}
        if op in _UNPLANTABLE:
            unplantable.append({"op": op, "params": params})
            continue
        planter = _PLANTERS.get(op)
        if planter is None:
            skipped.append({"op": op, "reason": "no planter"})
            continue
        try:
            planter(acc, params)
        except (KeyError, GoldenError) as exc:
            skipped.append({"op": op, "reason": str(exc)})
    return {"files": _serialize(acc), "unplantable": unplantable, "skipped": skipped}


def plant_golden_state(sandbox, golden: dict) -> list[str]:
    """Write a generated golden's files into a sandbox. Returns the paths."""
    from process_checks.lib.plant import plant_raw
    for path, text in golden["files"].items():
        plant_raw(sandbox, path, text)
    return list(golden["files"])
