"""
Tamper: workspace settings shadowing user settings.

Setup-time state manipulation. Plants the task's three settings in a WORKSPACE
settings file (/home/user/project/.vscode/settings.json) while leaving the USER
settings file (~/.config/Code/User/settings.json) — the file the task
explicitly named — holding stale values.

Keybindings are planted correctly so the variant isolates the settings failure.

Realism: this is one of the most plausible failure modes for this task. VSCode
has several settings scopes, "Preferences: Open Settings (JSON)" vs
"Preferences: Open Workspace Settings (JSON)" are adjacent entries in the
command palette, and an agent that picks the wrong one produces exactly this
state. The values are even *in effect* for the open workspace, so the editor
behaves as requested — the only thing wrong is WHICH file holds them.

The planted settings.json is valid JSONC (VSCode's own comment-header style),
which also confirms our checkpoints read the right file rather than
accidentally passing on any settings.json they happen to find.
"""
from __future__ import annotations

from process_checks.lib.plant import plant_jsonc, plant_raw

USER_SETTINGS = "/home/user/.config/Code/User/settings.json"
WORKSPACE_SETTINGS = "/home/user/project/.vscode/settings.json"
KEYBINDS_PATH = "/home/user/.config/Code/User/keybindings.json"

# What the task actually asked for — planted in the WRONG scope.
REQUESTED = {
    "editor.fontSize": 14,
    "editor.tabSize": 4,
    "editor.cursorStyle": "block",
}

# Stale defaults left in the USER file: present and plausible, but not the
# requested values. Deliberately not empty — an empty file is a weaker trap,
# since "nothing here" is easier to notice than "something wrong here".
USER_STALE = {
    "editor.fontSize": 12,
    "editor.tabSize": 2,
    "editor.cursorStyle": "line",
}

KEYBINDS_CORRECT = [
    {"key": "ctrl+shift+n", "command": "workbench.action.files.newUntitledFile"},
    {"key": "ctrl+shift+w", "command": "workbench.action.closeAllEditors"},
    {"key": "ctrl+alt+s", "command": "workbench.action.files.saveAll"},
]


def tamper(sandbox) -> dict:
    """Plant the mismatch. Returns a short description for the run record."""
    # Wrong scope, right values — with VSCode's comment header so it reads as
    # a file VSCode itself produced.
    plant_jsonc(
        sandbox,
        WORKSPACE_SETTINGS,
        REQUESTED,
        header_comment="// Workspace settings",
    )
    # Right scope, wrong values.
    plant_jsonc(sandbox, USER_SETTINGS, USER_STALE)
    # Correct, so only the settings conditions fail.
    #
    # Deliberately STRICT JSON — no comment header. This variant's job is to be
    # a pure *vision* blind spot that the existing outcome verifier still
    # catches, which requires the outcome verifier to be able to read every
    # file it checks. Planting VSCode's usual `// Place your key bindings...`
    # header here trips the oracle's JSONC bug (confirmed on the server
    # 2026-07-20: all five keybinding checks became unreadable, giving 0/8
    # instead of 5/8) and confounds the contrast with the
    # malformed_keybindings variant. The oracle's JSONC false negative is
    # already demonstrated twice elsewhere — the live 2026-07-18 run and
    # synthetic_golden.py's `jsonc` variant — so it does not need a third
    # sighting here at the cost of this variant's clarity.
    plant_jsonc(sandbox, KEYBINDS_PATH, KEYBINDS_CORRECT)
    # A plausible project for the workspace file to belong to.
    plant_raw(sandbox, "/home/user/project/README.md", "# project\n")
    return {
        "variant": "workspace_shadow",
        "planted": [WORKSPACE_SETTINGS, USER_SETTINGS, KEYBINDS_PATH],
        "summary": "requested settings live in the workspace file; the user file "
                   "named by the task still holds stale values",
    }
