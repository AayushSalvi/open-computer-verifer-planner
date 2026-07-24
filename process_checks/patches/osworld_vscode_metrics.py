"""
Drop-in replacement for OSWorld's two VSCode JSON metrics.

TARGET
------
`desktop_env/evaluators/metrics/vscode.py` — `check_json_settings` and
`check_json_keybindings`.

WHAT IS WRONG WITH THE ORIGINALS
--------------------------------
Both parse VSCode config files with a strict `json.loads`. VSCode writes those
files as **JSONC**: `//` and `/* */` comments and trailing commas are legal, and
VSCode's own default `keybindings.json` opens with

    // Place your key bindings in this file to override the defaults

so a strict parser fails at character 0 and the metric returns 0.0 for a
perfectly correct configuration.

`check_json_keybindings` half-mitigates this with a `skip_first_line_load_json`
fallback — literally skipping line 1. That covers exactly the one-line comment
case and breaks on two-line headers or block comments.

`check_json_keybindings` has a second, independent defect: it tests
`expected in data`, an **exact dict match**. Any binding carrying an extra field
fails, even when the key and command are both correct. `"when"` clauses are
extremely common in real keybindings.

Measured on synthetic inputs (all four are CORRECT configurations scored 0.0):

    check_json_settings    JSONC comment header       0.0   should be 1.0
    check_json_settings    trailing comma             0.0   should be 1.0
    check_json_keybindings 2-line comment header      0.0   should be 1.0
    check_json_keybindings binding with a when clause 0.0   should be 1.0

SCOPE / URGENCY
---------------
These metrics are used by **`vs_code` tasks only** (9 in the OSWorld corpus).
A rollout corpus without a `vs_code` domain is unaffected. This patch is
therefore **preventive**, not a fix for damage already done — worth applying
before the domain is added, not an emergency.

SELF-CONTAINED ON PURPOSE
-------------------------
The JSONC reader is inlined rather than imported from `process_checks/lib/`, so
this file can be dropped into another repo with no extra dependency. It
duplicates `lib/jsonc.py`; if both live in one tree, prefer the library.

DELIBERATE STRICTNESS
---------------------
We match VSCode's tolerance exactly — comments, trailing commas and a BOM are
accepted; single-quoted keys and missing commas are **rejected**, because VSCode
rejects them too. Do not "improve" this by making it more permissive: a lenient
parser (json5 accepts single quotes) would score a genuinely broken file, one
VSCode refuses to load, as passing.
"""
from __future__ import annotations

import json
from typing import Any


# --------------------------------------------------------------------------- #
# Minimal JSONC reader (inlined; see module docstring)
# --------------------------------------------------------------------------- #

def _strip_comments(text: str) -> str:
    """Remove // and /* */ comments, ignoring comment-like text inside strings."""
    out: list[str] = []
    i, n, in_string = 0, len(text), False
    while i < n:
        c = text[i]
        if in_string:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if c == '"':
                in_string = False
            i += 1
            continue
        if c == '"':
            in_string = True
            out.append(c)
            i += 1
            continue
        if c == "/" and i + 1 < n:
            if text[i + 1] == "/":
                while i < n and text[i] not in "\r\n":
                    i += 1
                continue
            if text[i + 1] == "*":
                i += 2
                while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                    i += 1
                i += 2
                continue
        out.append(c)
        i += 1
    return "".join(out)


def _strip_trailing_commas(text: str) -> str:
    """Remove commas directly preceding a closing } or ], ignoring strings."""
    out: list[str] = []
    i, n, in_string = 0, len(text), False
    while i < n:
        c = text[i]
        if in_string:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if c == '"':
                in_string = False
            i += 1
            continue
        if c == '"':
            in_string = True
            out.append(c)
            i += 1
            continue
        if c == ",":
            j = i + 1
            while j < n and text[j] in " \t\r\n":
                j += 1
            if j < n and text[j] in "}]":
                i += 1
                continue
        out.append(c)
        i += 1
    return "".join(out)


def _load_jsonc(path: str) -> Any:
    """Parse a JSONC file. Returns None if unreadable or malformed.

    None means "VSCode could not load this either", which for a config file is
    a determinate failure: the settings are genuinely not in effect.
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
    except (OSError, UnicodeDecodeError):
        return None
    text = text.lstrip("﻿")  # BOM
    stripped = _strip_trailing_commas(_strip_comments(text)).strip()
    if not stripped:
        return None
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return None


# --------------------------------------------------------------------------- #
# The patched metrics
# --------------------------------------------------------------------------- #

def check_json_settings(actual: str, expected: str, **options) -> float:
    """Check that every expected key/value appears in a JSONC settings file.

    Same contract as the original: `expected` is a dict carrying "expected".
    Only the parser changes.
    """
    if not actual:
        return 0.0
    data = _load_jsonc(actual)
    if not isinstance(data, dict):
        return 0.0
    expect = expected["expected"]
    for key, value in expect.items():
        if key not in data or data[key] != value:
            return 0.0
    return 1.0


def check_json_keybindings(actual: str, expected: str, **options) -> float:
    """Check that a keybinding matching the expected fields exists.

    Two changes from the original:

    1. JSONC parsing (replaces strict json.loads + the skip-first-line hack).
    2. **Subset match instead of exact dict match.** A binding matches if it
       contains every expected key/value; extra fields are tolerated. This is
       what fixes the `when`-clause false negative.

    Judgement call worth flagging: a `when`-guarded binding is *conditional*, so
    it is not strictly identical to an unconditional one. Subset matching treats
    them as equivalent. That is the right default here because the alternative
    rejects correct work, but a task that specifically requires an
    *unconditional* binding would need `"when": None` expressed explicitly.
    """
    if not actual:
        return 0.0
    data = _load_jsonc(actual)
    if not isinstance(data, list):
        return 0.0
    expect = expected["expected"]
    if not isinstance(expect, dict):
        return 1.0 if expect in data else 0.0
    for binding in data:
        if isinstance(binding, dict) and all(
            binding.get(k) == v for k, v in expect.items()
        ):
            return 1.0
    return 0.0
