"""
JSONC (JSON with Comments) reader — matches VSCode's own tolerance.

WHY THIS EXISTS
---------------
verifiers/vscode/vscode.py reads settings.json / keybindings.json with a plain
json.loads(). But VSCode ships those files as JSONC: its default
keybindings.json literally begins with

    // Place your key bindings in this file to override the defaults

which makes json.loads() fail at char 0. A correctly-configured file can
therefore be reported as unreadable, and a checkpoint that maps "unreadable"
to FAIL then emits a FALSE NEGATIVE — which, per PROJECT.md, flips the sign of
the RL reward on a correct trajectory.

This module lets a checkpoint tell two cases apart that currently look
identical:

  (a) valid JSONC, VSCode loads it fine  -> parse it, evaluate the predicate
  (b) malformed even as JSONC            -> genuinely broken; VSCode ignores
                                            the file, so the setting is NOT in
                                            effect and FAIL is the truthful
                                            verdict (with evidence)

DELIBERATE STRICTNESS
---------------------
We match VSCode's tolerance exactly — no more, no less:

  ACCEPTED (VSCode accepts these):  // line comments, /* block */ comments,
                                    trailing commas, a UTF-8 BOM
  REJECTED (VSCode rejects these):  single-quoted keys/strings, unquoted keys,
                                    missing commas between elements

Being stricter than VSCode causes false negatives. Being MORE PERMISSIVE
causes false positives, which are worse — a lenient parser (e.g. json5, which
accepts single quotes) would have reported the 2026-07-18 malformed-keybindings
run as PASS when VSCode was reporting 14 errors and ignoring the file entirely.
Do not "improve" this by making it more forgiving.

Stdlib only, deterministic, read-only.
"""
from __future__ import annotations

import json


class JsoncError(ValueError):
    """Raised when text is not valid JSONC (i.e. VSCode could not load it either)."""


def _strip_comments(text: str) -> str:
    """Remove // and /* */ comments, without touching comment-like sequences
    inside string literals (e.g. the // in "http://example.com")."""
    out: list[str] = []
    i, n = 0, len(text)
    in_string = False

    while i < n:
        c = text[i]

        if in_string:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(text[i + 1])  # copy the escaped char verbatim
                i += 2
                continue
            if c == '"':
                in_string = False
            i += 1
            continue

        if c == '"':  # only double quotes open a JSON string
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
    """Remove commas that directly precede a closing } or ], ignoring commas
    inside string literals."""
    out: list[str] = []
    i, n = 0, len(text)
    in_string = False

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
                i += 1  # drop this trailing comma
                continue

        out.append(c)
        i += 1

    return "".join(out)


def loads_jsonc(text: str):
    """Parse JSONC text. Raises JsoncError if VSCode could not load it either.

    An empty / whitespace-only file parses to None — VSCode treats that as
    "nothing configured", which is a legitimate readable state, not an error.
    """
    if text is None:
        raise JsoncError("no content")

    text = text.lstrip("﻿")  # UTF-8 BOM
    stripped = _strip_trailing_commas(_strip_comments(text)).strip()

    if not stripped:
        return None

    try:
        return json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise JsoncError(
            f"malformed even as JSONC (VSCode cannot load this file): {exc}"
        ) from exc


def read_jsonc_text(text: str) -> dict:
    """Non-raising wrapper returning a structured verdict, for checkpoint use.

    Returns one of:
        {"ok": True,  "data": <parsed>}
        {"ok": False, "error": "<why>", "raw": "<first 500 chars, for evidence>"}

    The `raw` field matters: the sandbox is destroyed when a run ends, so a
    parse failure is otherwise undiagnosable after the fact.
    """
    try:
        return {"ok": True, "data": loads_jsonc(text)}
    except JsoncError as exc:
        return {"ok": False, "error": str(exc), "raw": (text or "")[:500]}
