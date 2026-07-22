"""
Our checkpoint probes  ->  StructAgent verifier probe specs.

Target schema (WenyiWU0111/StructAgent,
mm_agents/structagent/core/verifier/boundary_verify.py):

    file_grep  {"kind":"file_grep","file_path":"/abs/path",
                "patterns":["regex",...],"all_must_match":true,
                "proximity_lines":N}

`proximity_lines` makes all patterns co-occur inside a sliding window of N
consecutive lines. Her own docstring gives our exact case as the example:
"3 = same keybindings.json entry's key + command". That is what closes the
false-accept hole where a key on one entry and a command on another would
satisfy two independent regexes.

Her `vs_code` DOMAIN_TRUST_MAP entry allows file_grep / a11y_match /
shell_command, and says to trust the on-disk file, so file_grep is the right
transport for every probe we currently emit.

KNOWN LIMITATION -- regex is weaker than our parser
---------------------------------------------------
Our checkpoints read the file and parse it as JSONC. A regex over raw text
cannot reproduce that, and is weaker in three ways:

  1. a value inside a `//` comment still matches   -> false pass
  2. a malformed file VSCode ignores still matches -> false pass
  3. scope is by path only, so nothing distinguishes a workspace file from
     the user file beyond the path we point at

(1) and (2) matter most: a honeypot whose whole point is that the file does
not parse would still satisfy a naive pattern. We mitigate by anchoring
patterns and, where the checkpoint demands a structural relationship, using
proximity_lines. Emitted specs carry `weaker_than_checkpoint` naming the
residual gap, so nothing downstream assumes parity with the deterministic
verdict.

The clean fix is a JSONC-aware probe kind (file + key path + expected value).
That is a change to her verifier's action space, so it is proposed rather
than assumed.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

#: Body of a single JSON object, allowing one level of nesting (a binding may
#: carry an `"args": {...}`). Cannot cross an entry boundary, because the outer
#: alternation excludes bare braces.
_OBJ_BODY = r"(?:[^{}]|\{[^{}]*\})*"


def _json_key_value_pattern(key: str, value) -> str:
    """Regex for `"key": value` in a JSON/JSONC file, whitespace tolerant."""
    k = re.escape(json.dumps(key)[1:-1])          # escaped, without quotes
    if isinstance(value, bool):
        v = "true" if value else "false"
    elif isinstance(value, (int, float)):
        v = re.escape(str(value))
    else:
        v = f'"{re.escape(str(value))}"'
    return rf'"{k}"\s*:\s*{v}'


def settings_probe(path: str, key: str, value) -> dict:
    """file_grep spec for `key == value` in a settings-style JSON object."""
    return {
        "kind": "file_grep",
        "file_path": path,
        "patterns": [_json_key_value_pattern(key, value)],
        "all_must_match": True,
    }


def keybinding_probe(path: str, key_combo: str, command: str) -> dict:
    """file_grep spec for a binding whose key AND command sit on ONE entry.

    Two independent patterns would match a file with the key on one entry and
    the command on another -- the exact false accept our checkpoints close.

    `proximity_lines` is the schema's answer to that, but it is unsafe here: it
    windows by LINE COUNT, so with compact one-line entries a 3-line window
    spans three separate bindings and false-passes anyway (verified in
    test_structagent.py). The window size that is correct depends on the file's
    formatting, which we do not control.

    So we scope structurally instead: ONE pattern requiring key and command
    inside the same `{...}` object, in either order. The object body cannot
    cross an entry boundary, which makes the guarantee independent of layout.
    """
    k = _json_key_value_pattern("key", key_combo)
    c = _json_key_value_pattern("command", command)
    # (?i): the checkpoint lowercases both sides before comparing, so its
    # matching is case-insensitive and the regex must be too. Without this a
    # predicate carrying the lowercased literal ("...newuntitledfile") fails
    # against the real file's camelCase ("...newUntitledFile").
    return {
        "kind": "file_grep",
        "file_path": path,
        "patterns": [
            rf"(?i)\{{{_OBJ_BODY}(?:{k}{_OBJ_BODY}{c}|{c}{_OBJ_BODY}{k}){_OBJ_BODY}\}}"
        ],
        "all_must_match": True,
    }


# --------------------------------------------------------------------------- #
# Deriving probes from a checkpoint's predicate
# --------------------------------------------------------------------------- #

_EQ_RE = re.compile(r"result\.get\(\s*'([^']+)'\s*\)\s*==\s*(.+?)\s*$")
_KB_RE = re.compile(
    r"b\.get\(\s*'key'.*?==\s*'([^']+)'.*?b\.get\(\s*'command'.*?==\s*'([^']+)'",
    re.DOTALL,
)


def _literal(text: str):
    text = text.strip()
    try:
        return json.loads(text.replace("'", '"'))
    except Exception:
        return text.strip("'\"")


def probe_for_checkpoint(check: dict) -> dict | None:
    """Best-effort translation of one checkpoint into a StructAgent probe.

    Returns None when the predicate is not one of the recognised shapes --
    silently emitting a weaker probe would be worse than emitting nothing.
    """
    path = check.get("jsonc_file")
    expr = check.get("eval", "")
    if not path or not expr:
        return None

    m = _KB_RE.search(expr)
    if m:
        spec = keybinding_probe(path, m.group(1), m.group(2))
        spec["weaker_than_checkpoint"] = (
            "regex cannot confirm the file parses; a malformed keybindings.json "
            "that VSCode ignores can still satisfy these patterns"
        )
        return spec

    for part in expr.split(" and "):
        m = _EQ_RE.search(part.strip())
        if m:
            spec = settings_probe(path, m.group(1), _literal(m.group(2)))
            spec["weaker_than_checkpoint"] = (
                "regex matches inside // comments and cannot confirm the file "
                "parses; scope is by path only"
            )
            return spec
    return None


def add_probe_specs(bundle: dict, checkpoints: dict) -> dict:
    """Attach StructAgent probe specs to a training-pair bundle, in place.

    Each unique probe gains `structagent`, or `structagent_unavailable` with a
    reason when the predicate has no faithful regex form.
    """
    by_id = {c["id"]: c for c in checkpoints.get("checkpoints", [])}
    translated = untranslated = 0
    for variant in bundle.get("variants", []):
        for probe in variant.get("probes", []):
            specs = []
            for cid in probe.get("answers", []):
                spec = probe_for_checkpoint(by_id.get(cid, {}))
                if spec is None:
                    untranslated += 1
                    continue
                specs.append({"condition": cid, **spec})
                translated += 1
            if specs:
                probe["structagent"] = specs
            else:
                probe["structagent_unavailable"] = (
                    "no faithful regex form for these predicates"
                )
    bundle["structagent_probe_stats"] = {
        "translated": translated,
        "untranslated": untranslated,
        "schema_source": "WenyiWU0111/StructAgent boundary_verify.py _PROBE_DOC",
        "note": ("file_grep is the right transport but a weaker predicate than the "
                 "JSONC parse behind each checkpoint; see weaker_than_checkpoint. "
                 "A JSONC-aware probe kind would close the gap."),
    }
    return bundle


def load_checkpoints(checkpoints_dir) -> dict:
    p = Path(checkpoints_dir)
    if p.is_dir():
        p = p / "checkpoints.json"
    with open(p, encoding="utf-8") as f:
        return json.load(f)
