"""
plant_* — write-direction siblings of the read primitives in this package.

⚠️  HONEYPOT SETUP ONLY.

PROJECT.md's symmetry rule: every read primitive in process_checks/lib/ may
gain a sibling plant_* function on the same inspection channel, write
direction. These are for honeypot `tamper.py` scripts, which run at SETUP
time, before the agent starts.

NEVER import this module from a checkpoint. Checkpoints are strictly
read-only — a checkpoint that writes to the environment can change the very
state it is meant to observe, and would corrupt the ground truth the RL
reward is anchored to. The runner never imports this module; only
honeypots/*/tamper.py does.

Read siblings live in process_checks/lib/jsonc.py.
"""
from __future__ import annotations

import json
import shlex


def plant_raw(sandbox, path: str, text: str) -> None:
    """Write literal text to a file in the sandbox, creating parent dirs.

    Used when the planted content is deliberately malformed and must survive
    verbatim (e.g. replaying a real agent's broken output byte-for-byte).
    """
    parent = path.rsplit("/", 1)[0]
    sandbox.commands.run(f"mkdir -p {shlex.quote(parent)}", timeout=15)
    sandbox.files.write(path, text)


def plant_jsonc(
    sandbox,
    path: str,
    obj,
    header_comment: str | None = None,
    trailing_comma: bool = False,
) -> None:
    """Write `obj` as JSONC — optionally with VSCode's style of leading comment
    header and/or a trailing comma.

    Write-direction sibling of jsonc.loads_jsonc. Both forms are valid JSONC
    that VSCode loads without complaint, so this plants *correct* state; the
    mismatch in a honeypot comes from WHERE it is planted (wrong file/scope),
    not from the content being broken. For deliberately broken content use
    plant_raw.
    """
    body = json.dumps(obj, indent=4)
    if trailing_comma:
        # `...}\n]` -> `...},\n]` / `..."\n}` -> `...",\n}`
        idx = max(body.rfind("}"), body.rfind("]"))
        if idx > 0:
            body = body[:idx].rstrip() + ",\n" + body[idx:]
    text = f"{header_comment}\n{body}" if header_comment else body
    plant_raw(sandbox, path, text)
