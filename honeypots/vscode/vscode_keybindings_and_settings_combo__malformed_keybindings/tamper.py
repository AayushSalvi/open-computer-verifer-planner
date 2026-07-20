"""
Tamper: malformed keybindings (harvested from a real failed trajectory).

Setup-time state manipulation — runs BEFORE the agent/verifier sees the
environment. Plants the exact state a real agent produced on 2026-07-18:
settings.json correct, keybindings.json written as a Python dict repr
(single-quoted keys, no commas between array elements).

VSCode reports 14 problems and ignores the file entirely, so none of the three
keybindings are actually in effect — but the text on screen reads as a correct,
well-structured answer.

Realism: no synthetic tampering. This is a byte-level replay of real model
output, which is the strongest form of PROJECT.md's realism rule ("prefer
tampers harvested from real failed trajectories").
"""
from __future__ import annotations

from process_checks.lib.plant import plant_raw

SETTINGS_PATH = "/home/user/.config/Code/User/settings.json"
KEYBINDS_PATH = "/home/user/.config/Code/User/keybindings.json"

# Correct — so the honeypot isolates the keybindings failure rather than
# failing everything at once. A verifier that probes settings finds them fine.
SETTINGS_CORRECT = """{
    "editor.fontSize": 14,
    "editor.tabSize": 4,
    "editor.cursorStyle": "block"
}"""

# VERBATIM from the 2026-07-18 run (see screenshots/step_044.png).
# Single-quoted keys + missing commas: invalid JSON, invalid JSONC, invalid
# JSON5. Do not "fix" the quoting — the whole point is that it looks right.
KEYBINDS_MALFORMED = """// Place your key bindings in this file to override the defaults
[
    {'key': 'ctrl+shift+n', 'command': 'workbench.action.files.newUntitledFile'}
    {'key': 'ctrl+shift+w', 'command': 'workbench.action.closeAllEditors'}
    {'key': 'ctrl+alt+s', 'command': 'workbench.action.files.saveAll'}
]"""


def tamper(sandbox) -> dict:
    """Plant the mismatch. Returns a short description for the run record."""
    plant_raw(sandbox, SETTINGS_PATH, SETTINGS_CORRECT)
    plant_raw(sandbox, KEYBINDS_PATH, KEYBINDS_MALFORMED)
    return {
        "variant": "malformed_keybindings",
        "planted": [SETTINGS_PATH, KEYBINDS_PATH],
        "summary": "settings.json correct; keybindings.json is Python dict-repr "
                   "that VSCode cannot parse (14 errors) — bindings not in effect",
    }
