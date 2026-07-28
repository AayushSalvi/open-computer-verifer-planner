"""
Honeypot auto-generation for VSCode — Phase 2, the THIRD artifact from one spec.

The registry `{op, params}` already drives two things:
  - the BINDER (registry_vscode) -> a checkpoint predicate,
  - the PLANTER (golden_vscode) -> the golden state that satisfies it.

This adds the PERTURBER: given the same spec, plant a *screen-vs-latent
mismatch* — a state that a screen-only reading would accept as done, but whose
latent (checkable) state fails the checkpoint. Anchoring the reward to that
latent state is exactly what forces a verifier to probe instead of trusting the
screenshot (PROJECT.md §honeypots; the collaborator's judge design).

DESIGN INVARIANT — single flip.
A honeypot perturbs the golden at exactly ONE criterion. Its checkpoint flips
PASS->FAIL; every other checkpoint stays PASS. Two payoffs:
  - self-validating: run the generated checkpoints on the honeypot, exactly one
    must fail, and it must be the targeted one. No hand-labelling.
  - it is the "lying" half of a contrastive SFT pair (truthful golden vs. this)
    that differs in one isolated place — the cleanest possible training signal.

Two perturbation families:
  - value-level (default): the config is present and well-formed but WRONG in
    one spot (near-miss value, split keybinding, decoy prefix/label/id, missing
    file, absent substring). Single flip by construction.
  - `malformed` (opt-in, JSONC files only): the target file carries the correct
    TEXT but is written so VSCode cannot load it (single-quoted dict-repr), so
    the state is inert. This is the strongest mismatch — text present, effect
    absent — and is a byte-level match to the hand-built `malformed_keybindings`
    honeypot. It flips every checkpoint that reads that file.

`extension_installed` has no file to perturb, so it is reported
`unhoneypottable`, not silently skipped — same honesty rule as the planter.
"""
from __future__ import annotations

import json

from process_checks.auto.golden_vscode import (
    _snippets_path,
    _ws,
    criteria_to_golden,
)
from process_checks.auto.registry_vscode import (
    KEYBINDINGS,
    SETTINGS,
    criteria_to_checkpoints,
)

# ops with no file-only golden -> nothing to perturb by writing a file
_UNPLANTABLE = {"extension_installed"}

# JSONC-file ops whose file can be made inert by a malformed-but-plausible write
_MALFORM_OK = {
    "setting_equals", "workspace_setting_equals", "keybinding_bound",
    "snippet_exists", "workspace_extension_recommended", "task_defined",
    "launch_config_exists",
}


class HoneypotError(ValueError):
    pass


def _wrong_value(v):
    """A plausible-but-wrong sibling of a config value. bool BEFORE int: bool is
    an int subclass, and `not True` is the intended near-miss, not `True + 1`."""
    if isinstance(v, bool):
        return not v
    if isinstance(v, (int, float)):
        return v + 1
    if isinstance(v, str):
        return (v + "_x") if v else "__wrong__"
    return "__wrong__"


def _file_of(op: str, p: dict) -> str | None:
    """The single file an op's golden lives in (to locate/perturb it)."""
    return {
        "setting_equals": SETTINGS,
        "workspace_setting_equals": _ws(p.get("workspace", ""), "settings.json"),
        "keybinding_bound": KEYBINDINGS,
        "snippet_exists": _snippets_path(p.get("language", "")),
        "workspace_extension_recommended": _ws(p.get("workspace", ""), "extensions.json"),
        "task_defined": _ws(p.get("workspace", ""), "tasks.json"),
        "launch_config_exists": _ws(p.get("workspace", ""), "launch.json"),
        "file_exists": p.get("path"),
        "file_contains": p.get("path"),
    }.get(op)


# --------------------------------------------------------------------------- #
# value-level perturbers: (golden_files, params) -> (new_files, note)
# each mutates a COPY, touching only the target's contribution
# --------------------------------------------------------------------------- #

def _perturb_setting(files, p, path):
    f = dict(files)
    d = json.loads(f[path])
    d[p["key"]] = _wrong_value(p["value"])
    f[path] = json.dumps(d, indent=4)
    return f, f"{p['key']} present but = {d[p['key']]!r}, not {p['value']!r}"


def _perturb_user_setting(files, p):
    return _perturb_setting(files, p, SETTINGS)


def _perturb_ws_setting(files, p):
    return _perturb_setting(files, p, _ws(p["workspace"], "settings.json"))


def _perturb_keybinding(files, p):
    """Split the binding across two entries: right-key/wrong-command and
    wrong-key/right-command. Each entry satisfies a naive single-field check;
    no entry satisfies the joint one — the exact false-accept our checkpoint
    closes."""
    f = dict(files)
    arr = json.loads(f[KEYBINDINGS])
    key, cmd = str(p["key"]), str(p["command"])
    arr = [b for b in arr if not (
        isinstance(b, dict)
        and str(b.get("key", "")).lower() == key.lower()
        and str(b.get("command", "")).lower() == cmd.lower())]
    arr.append({"key": key, "command": cmd + ".decoy"})   # right key, wrong command
    arr.append({"key": "ctrl+f12", "command": cmd})        # wrong key, right command
    f[KEYBINDINGS] = json.dumps(arr, indent=4)
    return f, (f"{key} and {cmd} both present but on DIFFERENT entries "
               "(single-field checks pass; joint fails)")


def _perturb_snippet(files, p):
    f = dict(files)
    path = _snippets_path(p["language"])
    d = json.loads(f[path])
    tgt = str(p["prefix"]).lower()
    for s in d.values():
        if isinstance(s, dict) and str(s.get("prefix", "")).lower() == tgt:
            s["prefix"] = str(p["prefix"]) + "x"
    f[path] = json.dumps(d, indent=4)
    return f, f"a snippet exists but under prefix {p['prefix']}x, not {p['prefix']}"


def _perturb_ws_ext(files, p):
    f = dict(files)
    path = _ws(p["workspace"], "extensions.json")
    d = json.loads(f[path])
    ext = p["extension_id"]
    d["recommendations"] = [r for r in d.get("recommendations", []) if r != ext] + [ext + "-decoy"]
    f[path] = json.dumps(d, indent=4)
    return f, f"a recommendation exists but for {ext}-decoy, not {ext}"


def _perturb_task(files, p):
    f = dict(files)
    path = _ws(p["workspace"], "tasks.json")
    d = json.loads(f[path])
    for t in d.get("tasks", []):
        if t.get("label") == p["label"]:
            t["label"] = p["label"] + "-decoy"
    f[path] = json.dumps(d, indent=4)
    return f, f"a task exists but labelled {p['label']}-decoy, not {p['label']}"


def _perturb_launch(files, p):
    f = dict(files)
    path = _ws(p["workspace"], "launch.json")
    d = json.loads(f[path])
    for cfg in d.get("configurations", []):
        if cfg.get("name") == p["name"]:
            cfg["name"] = p["name"] + "-decoy"
    f[path] = json.dumps(d, indent=4)
    return f, f"a launch config exists but named {p['name']}-decoy, not {p['name']}"


def _perturb_file_exists(files, p):
    f = dict(files)
    f.pop(p["path"], None)
    return f, f"{p['path']} was not created"


def _perturb_file_contains(files, p):
    f = dict(files)
    path, sub = p["path"], p["substring"]
    txt = f.get(path, "")
    f[path] = "\n".join(ln for ln in txt.split("\n") if ln != sub)
    return f, f"{path} present but missing {sub!r}"


_DEFAULT = {
    "setting_equals": _perturb_user_setting,
    "workspace_setting_equals": _perturb_ws_setting,
    "keybinding_bound": _perturb_keybinding,
    "snippet_exists": _perturb_snippet,
    "workspace_extension_recommended": _perturb_ws_ext,
    "task_defined": _perturb_task,
    "launch_config_exists": _perturb_launch,
    "file_exists": _perturb_file_exists,
    "file_contains": _perturb_file_contains,
}

_KIND = {
    "setting_equals": "near_miss_value",
    "workspace_setting_equals": "near_miss_value",
    "keybinding_bound": "split_keybinding",
    "snippet_exists": "decoy_prefix",
    "workspace_extension_recommended": "decoy_recommendation",
    "task_defined": "decoy_label",
    "launch_config_exists": "decoy_name",
    "file_exists": "missing_file",
    "file_contains": "absent_substring",
}


def _malform(files, path):
    """Rewrite a JSON file as a Python dict-repr (single-quoted keys) so VSCode
    cannot load it: text correct, effect absent. Matches the hand-built
    malformed_keybindings honeypot byte-for-byte in style."""
    f = dict(files)
    f[path] = repr(json.loads(f[path]))
    return f, (f"{path} written as python-repr (single quotes) -> VSCode ignores "
               "it; the state it describes is not in effect")


# --------------------------------------------------------------------------- #
# generator
# --------------------------------------------------------------------------- #

def _t1_binds(criteria):
    return [c for c in criteria
            if isinstance(c, dict) and c.get("tier") == "T1"
            and isinstance(c.get("bind"), dict)]


def criteria_to_honeypots(criteria: list[dict], *, malformed: bool = False) -> dict:
    """One honeypot per trappable criterion (single-flip).

    Returns {"honeypots": [...], "unhoneypottable": [...]}. Each honeypot is
    {target, op, kind, note, files, expected} where `expected` maps every
    checkpoint id -> the pass verdict the honeypot should produce (False at the
    trapped checkpoint(s), True elsewhere, None for unplantable-op checkpoints
    whose verdict cannot be determined from files alone).

    `malformed=True` uses the inert-file family for JSONC ops instead of the
    value-level default; that flips every checkpoint reading the target file.
    """
    t1 = _t1_binds(criteria)
    golden = criteria_to_golden(criteria)["files"]
    spec = criteria_to_checkpoints("hp", criteria)
    cids = [cp["id"] for cp in spec["checkpoints"]]
    if len(cids) != len(t1):
        raise HoneypotError(
            f"checkpoint/criterion misalignment ({len(cids)} vs {len(t1)}) — "
            "cannot guarantee single-flip semantics")

    ops = [c["bind"]["op"] for c in t1]

    honeypots, unhoneypottable = [], []
    for i, c in enumerate(t1):
        op = ops[i]
        params = c["bind"].get("params") or {}
        if op in _UNPLANTABLE:
            unhoneypottable.append({"target": cids[i], "op": op,
                                    "reason": "unplantable -> no file to perturb"})
            continue

        use_malform = malformed and op in _MALFORM_OK
        if use_malform:
            path = _file_of(op, params)
            files, note = _malform(golden, path)
            # every checkpoint reading this file is now inert
            affected = {cids[j] for j in range(len(t1))
                        if _file_of(ops[j], t1[j]["bind"].get("params") or {}) == path
                        and ops[j] not in _UNPLANTABLE}
            kind = "malformed"
        else:
            files, note = _DEFAULT[op](golden, params)
            affected = {cids[i]}
            kind = _KIND[op]

        expected = {}
        for j, cid in enumerate(cids):
            expected[cid] = None if ops[j] in _UNPLANTABLE else (cid not in affected)

        honeypots.append({
            "target": cids[i], "op": op, "kind": kind,
            "note": note, "files": files, "expected": expected,
        })

    return {"honeypots": honeypots, "unhoneypottable": unhoneypottable}


def plant_honeypot(sandbox, honeypot: dict) -> list[str]:
    """Write a generated honeypot's files into a sandbox (setup-time tamper).
    Returns the paths. Never call from inside a checkpoint."""
    from process_checks.lib.plant import plant_raw
    for path, text in honeypot["files"].items():
        plant_raw(sandbox, path, text)
    return list(honeypot["files"])
