"""
VSCode T1 registry — the deterministic core of checkpoint automation.

`catalog.md` turned into code. Given a decomposed criterion whose tier is T1
(maps to a known deterministic op), this emits our checkpoint spec with NO LLM
involved. This is the "template" tier: it is where the "automatic" claim for
VSCode actually lives, because most VSCode criteria are T1.

The decompose step (one LLM call, mirrors the collaborator's decompose.py)
produces criteria of the form:

    {"text": "...", "tier": "T1", "bind": {"op": "<op>", "params": {...}}, "note": "..."}

This module maps each T1 `bind` to a checkpoint. Ops are the vocabulary the
decomposer is allowed to emit (OP_DOC below is fed into its prompt, so it can
only pick ops that exist here). An unknown op raises rather than guessing — a
silently-wrong checkpoint poisons the reward.

Two emit targets per op, because VSCode has two channels:
  - JSONC files (settings.json, keybindings.json, snippets, .vscode/*) -> our
    `jsonc_file` + `eval` lane, which parses JSONC correctly (a regex cannot).
  - non-file state (installed extensions) -> the existing verifiers/vscode.py
    `check-*` command lane, reused, not reinvented.

Design rules kept:
  - reuse existing check-* endpoints before inventing anything (PROJECT.md)
  - emitted predicate is deterministic + read-only (no LLM at runtime)
  - the boolean is DIRECTED evidence, matched to exactly one criterion (her C5)
"""
from __future__ import annotations

import json
import re

USER_DIR = "/home/user/.config/Code/User"
SETTINGS = f"{USER_DIR}/settings.json"
KEYBINDINGS = f"{USER_DIR}/keybindings.json"


class RegistryError(ValueError):
    """Raised when a bind cannot be mapped — never guessed around."""


# --------------------------------------------------------------------------- #
# op vocabulary — this doc string is fed to the decomposer's prompt, so the
# model may only emit ops that appear here.
# --------------------------------------------------------------------------- #
OP_DOC = {
    "setting_equals":
        'setting_equals {key, value}  # a user settings.json key equals a value '
        '(e.g. editor.fontSize = 14)',
    "keybinding_bound":
        'keybinding_bound {key, command}  # a keybindings.json entry binds a key '
        'combo to a command ON THE SAME entry',
    "snippet_exists":
        'snippet_exists {language, prefix}  # a snippet with this prefix exists '
        'for a language',
    "extension_installed":
        'extension_installed {extension_id}  # an extension is installed '
        '(e.g. ms-python.python)',
    "workspace_setting_equals":
        'workspace_setting_equals {workspace, key, value}  # a '
        '<workspace>/.vscode/settings.json key equals a value',
    "workspace_extension_recommended":
        'workspace_extension_recommended {workspace, extension_id}  # extension in '
        '<workspace>/.vscode/extensions.json recommendations',
    "task_defined":
        'task_defined {workspace, label}  # a task with this label exists in '
        '<workspace>/.vscode/tasks.json',
    "launch_config_exists":
        'launch_config_exists {workspace, name}  # a launch config with this name '
        'in <workspace>/.vscode/launch.json',
    "file_exists":
        'file_exists {path}  # a file exists on disk',
    "file_contains":
        'file_contains {path, substring}  # a file contains a literal substring',
}


def op_registry_doc() -> str:
    """The registry block to embed in the decomposer prompt for domain=vscode."""
    return ("DETERMINISTIC CHECK REGISTRY for VSCode (a criterion is T1 ONLY if it "
            "maps to one of these ops; extract concrete params from the "
            "instruction):\n" + "\n".join(f"- {v}" for v in OP_DOC.values()))


# --------------------------------------------------------------------------- #
# helpers that build our checkpoint predicate shapes
# --------------------------------------------------------------------------- #

def _jsonc_settings(cid, note, path, key, value, channel="file"):
    """settings-style: result is a dict, result.get(key) == value."""
    return {
        "id": cid,
        "description": note,
        "channel": channel,
        "jsonc_file": path,
        "eval": (f"isinstance(result, dict) and "
                 f"result.get({key!r}) == {json.dumps(value)}"),
    }


def _cmd(cid, note, command, key, expected, channel="cli"):
    """reuse an existing verifiers/vscode.py check-* command."""
    return {"id": cid, "description": note, "channel": channel,
            "command": command, "key": key, "expected": expected}


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return s[:40] or "c"


# --------------------------------------------------------------------------- #
# per-op emitters
# --------------------------------------------------------------------------- #

def _op_setting_equals(cid, p, note):
    return _jsonc_settings(cid, note, SETTINGS, p["key"], p["value"])


def _op_keybinding_bound(cid, p, note):
    key = str(p["key"]).lower()
    cmd = str(p["command"]).lower()
    return {
        "id": cid, "description": note, "channel": "file",
        "jsonc_file": KEYBINDINGS,
        "eval": (f"isinstance(result, list) and any("
                 f"isinstance(b, dict) and str(b.get('key','')).lower() == {key!r} "
                 f"and str(b.get('command','')).lower() == {cmd!r} for b in result)"),
    }


def _op_snippet_exists(cid, p, note):
    lang = p["language"]
    prefix = str(p["prefix"]).lower()
    path = f"{USER_DIR}/snippets/{lang}.json"
    # snippet file: {name: {prefix: "..."|[...], body: ...}}; prefix may be str or list
    return {
        "id": cid, "description": note, "channel": "file",
        "jsonc_file": path,
        "eval": (f"isinstance(result, dict) and any("
                 f"isinstance(s, dict) and ("
                 f"str(s.get('prefix','')).lower() == {prefix!r} or "
                 f"(isinstance(s.get('prefix'), list) and {prefix!r} in "
                 f"[str(x).lower() for x in s.get('prefix', [])])"
                 f") for s in result.values())"),
    }


def _op_extension_installed(cid, p, note):
    ext = p["extension_id"]
    return _cmd(cid, note, f"check-extension-installed {ext}", "installed", True)


def _op_workspace_setting_equals(cid, p, note):
    path = f"{p['workspace']}/.vscode/settings.json"
    return _jsonc_settings(cid, note, path, p["key"], p["value"])


def _op_workspace_extension_recommended(cid, p, note):
    ws, ext = p["workspace"], p["extension_id"]
    return _cmd(cid, note,
                f"check-workspace-extension-recommended {ws} {ext}",
                "recommended", True)


def _op_task_defined(cid, p, note):
    ws, label = p["workspace"], p["label"]
    return _cmd(cid, note, f"check-task-exists {ws} {label}", "exists", True)


def _op_launch_config_exists(cid, p, note):
    ws, name = p["workspace"], p["name"]
    return _cmd(cid, note, f"check-launch-config-exists {ws} {name}", "exists", True)


def _op_file_exists(cid, p, note):
    return _cmd(cid, note, f"check-file-exists {p['path']}", "exists", True, channel="file")


def _op_file_contains(cid, p, note):
    return _cmd(cid, note, f"check-file-contains {p['path']} {p['substring']}",
                "contains", True, channel="file")


_EMITTERS = {
    "setting_equals": _op_setting_equals,
    "keybinding_bound": _op_keybinding_bound,
    "snippet_exists": _op_snippet_exists,
    "extension_installed": _op_extension_installed,
    "workspace_setting_equals": _op_workspace_setting_equals,
    "workspace_extension_recommended": _op_workspace_extension_recommended,
    "task_defined": _op_task_defined,
    "launch_config_exists": _op_launch_config_exists,
    "file_exists": _op_file_exists,
    "file_contains": _op_file_contains,
}


def bind_to_checkpoint(bind: dict, cid: str, note: str) -> dict:
    """Map one T1 bind {op, params} to a checkpoint. Raises on unknown op or
    missing params — never emits a guessed check."""
    op = bind.get("op")
    if op not in _EMITTERS:
        raise RegistryError(f"unknown op {op!r} (not in the VSCode registry)")
    params = bind.get("params") or {}
    try:
        return _EMITTERS[op](cid, params, note)
    except KeyError as exc:
        raise RegistryError(f"op {op!r} missing required param {exc}") from exc


def criteria_to_checkpoints(task_id: str, criteria: list[dict]) -> dict:
    """Turn a decomposition into a checkpoints.json spec.

    T1 criteria become checkpoints. T2/T3 are recorded but not bound (they need
    a getter+judge or are unverifiable) — carried through so the automation
    rate and the residual are both visible.
    """
    checkpoints, deferred = [], []
    for i, c in enumerate(criteria, 1):
        tier = c.get("tier")
        note = c.get("text", "")
        if tier == "T1" and c.get("bind"):
            cid = f"m{i}_{_slug(note)}"
            checkpoints.append(bind_to_checkpoint(c["bind"], cid, note))
        else:
            deferred.append({"criterion": note, "tier": tier,
                             "reason": c.get("note", "")})
    return {
        "task_id": task_id,
        "app": "vscode",
        "source": "auto",
        "checkpoints": checkpoints,
        "deferred": deferred,
        "tier_counts": {
            "T1": len(checkpoints),
            "T2/T3": len(deferred),
            "total": len(criteria),
        },
    }
