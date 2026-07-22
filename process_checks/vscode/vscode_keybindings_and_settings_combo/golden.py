"""
Golden state for vscode_keybindings_and_settings_combo.

The counterpart to a honeypot's tamper.py: that plants a lie, this plants the
truth. Same task, same instruction, and the ONLY difference is whether the
screen can be trusted.

That contrast is the point. A cold-start set made only of honeypots teaches
"always probe", which is the mirror image of the pathology being fixed --
ritual probing earns no validity credit and wastes the budget. The verifier
needs examples where a glance genuinely suffices in order to learn *when*
looking underneath is necessary.

Two styles, both correct and both loaded fine by VSCode:
  "plain" -- strict JSON
  "jsonc" -- VSCode's own comment header plus a trailing comma

The jsonc style is what VSCode actually writes. It is included because the
existing outcome verifier fails on it (a confirmed false negative), so it also
serves as the oracle-bug demonstration in synthetic_golden.py.
"""
from __future__ import annotations

from process_checks.lib.plant import plant_raw

SETTINGS_PATH = "/home/user/.config/Code/User/settings.json"
KEYBINDS_PATH = "/home/user/.config/Code/User/keybindings.json"

SETTINGS_BODY = """{
    "editor.fontSize": 14,
    "editor.tabSize": 4,
    "editor.cursorStyle": "block"
}"""

KEYBINDS_BODY = """[
    { "key": "ctrl+shift+n", "command": "workbench.action.files.newUntitledFile" },
    { "key": "ctrl+shift+w", "command": "workbench.action.closeAllEditors" },
    { "key": "ctrl+alt+s",   "command": "workbench.action.files.saveAll" }
]"""

KEYBINDS_HEADER = "// Place your key bindings in this file to override the defaults"


def keybindings_text(style: str = "plain") -> str:
    if style == "jsonc":
        return f"{KEYBINDS_HEADER}\n{KEYBINDS_BODY}"
    return KEYBINDS_BODY


def settings_text(style: str = "plain") -> str:
    if style == "jsonc":
        # trailing comma + comment: valid JSONC, rejected by a strict parser
        body = SETTINGS_BODY.rstrip()[:-1].rstrip() + ",\n}"
        return f"// editor preferences\n{body}"
    return SETTINGS_BODY


def plant_golden(sandbox, style: str = "plain") -> dict:
    """Plant a state in which the task is genuinely, fully complete."""
    plant_raw(sandbox, SETTINGS_PATH, settings_text(style))
    plant_raw(sandbox, KEYBINDS_PATH, keybindings_text(style))
    return {
        "variant": f"golden_{style}",
        "planted": [SETTINGS_PATH, KEYBINDS_PATH],
        "summary": f"task genuinely complete; config written as {style}",
        "screen_is_truthful": True,
    }
