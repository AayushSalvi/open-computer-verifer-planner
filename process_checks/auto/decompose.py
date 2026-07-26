"""
Decompose a task instruction into tiered criteria — the one LLM call in the
automation, mirroring the collaborator's exp/milestone/decompose.py.

Pipeline:  instruction --(this, LLM)--> criteria --(registry_vscode)--> checkpoints

Her schema, adopted for interop:
    {"criteria": [{"text", "tier": "T1|T2|T3", "bind": {"op","params"}|null, "note"}]}

FIREWALL (PROJECT.md): decompose from the INSTRUCTION only. The task's
`verification[]` array is never shown to the decomposer — it is reserved as the
downstream consistency gate. Letting the decomposer see it would collapse
"derive milestones from intent" into "copy the answer key".

MODEL IS PLUGGABLE:
  - default: an OpenAI-compatible endpoint (the server's vLLM), configured by
    OPENAI_BASE_URL / OPENAI_API_KEY exactly like evaluation/run_eval.py.
  - offline: pass `decomposition=` (a criteria list) to bypass the call
    entirely. Used for tests and for supplying a hand-authored decomposition,
    so the whole pipeline runs without an API.

Nothing here is VSCode-hardcoded except the registry it pulls in; a Chrome
registry would swap in the same way.
"""
from __future__ import annotations

import ast
import json
import os
import re
import time

from process_checks.auto.registry_vscode import op_registry_doc

SYS = """You decompose a GUI-agent TASK into the atomic criteria that must ALL hold for the task to be done, then bind each to a deterministic check when one fits.

RULES
- List EVERY criterion the instruction requires — one per distinct requirement. Never use placeholders, "...", "and so on", or example stand-ins. If the task sets 5 settings, emit 5 criteria.
- If a criterion maps to a registry op below, you MUST tier it T1 and give {op, params} with the concrete value extracted from the instruction. Prefer T1 whenever an op fits — do not down-tier a checkable criterion to T2/T3.
- T2: the state is readable but NO registry op fits. T3: purely visual/spatial with no readable state. For T2/T3 set "bind": null.

WORKED EXAMPLE
INSTRUCTION: Set editor.fontSize to 14 and editor.tabSize to 2. Add a keybinding ctrl+alt+p running workbench.action.showCommands. Create /home/user/app.js containing the text calculateSum.
OUTPUT:
{"criteria":[
{"text":"editor.fontSize is 14","tier":"T1","bind":{"op":"setting_equals","params":{"key":"editor.fontSize","value":14}}},
{"text":"editor.tabSize is 2","tier":"T1","bind":{"op":"setting_equals","params":{"key":"editor.tabSize","value":2}}},
{"text":"ctrl+alt+p bound to workbench.action.showCommands","tier":"T1","bind":{"op":"keybinding_bound","params":{"key":"ctrl+alt+p","command":"workbench.action.showCommands"}}},
{"text":"/home/user/app.js exists","tier":"T1","bind":{"op":"file_exists","params":{"path":"/home/user/app.js"}}},
{"text":"app.js contains calculateSum","tier":"T1","bind":{"op":"file_contains","params":{"path":"/home/user/app.js","substring":"calculateSum"}}}
]}

OUTPUT FORMAT: a single STRICT JSON object with a "criteria" list, double-quoted keys and strings. Any reasoning must come BEFORE the JSON; end your response with the JSON object."""


def build_user_prompt(instruction: str) -> str:
    return (f"{op_registry_doc()}\n\n"
            f"TASK INSTRUCTION:\n{instruction}\n\n"
            f"Decompose this task into atomic criteria and tier each. JSON only.")


def _strip_wrappers(raw: str) -> str:
    """Remove reasoning tags and markdown code fences a model may wrap around
    its JSON. Qwen-family models often emit <think>...</think> and/or ```json."""
    raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL | re.IGNORECASE)
    # ```json ... ```  or  ``` ... ```
    fence = re.search(r"```(?:json)?\s*(.*?)```", raw, flags=re.DOTALL)
    if fence:
        raw = fence.group(1)
    return raw.strip()


def _iter_balanced_objects(text: str):
    """Yield every brace-balanced {...} region (string-aware), left to right.

    Naive first-{/last-} breaks when reasoning/prose contains stray braces
    (e.g. "the breakdown {note}: {...real json...}"), so we consider each
    candidate and let the caller pick the one that actually parses.
    """
    start = text.find("{")
    while start != -1:
        depth, i, in_str, esc = 0, start, False, False
        while i < len(text):
            c = text[i]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
            elif c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    yield text[start:i + 1]
                    break
            i += 1
        start = text.find("{", start + 1)


def _try_load(obj_text: str):
    try:
        return json.loads(obj_text)
    except json.JSONDecodeError:
        try:
            return ast.literal_eval(obj_text)  # single quotes / py literals
        except (ValueError, SyntaxError):
            return None


def _parse_criteria(raw: str) -> list[dict]:
    """Extract the criteria list from a model response, tolerantly.

    Strip wrappers, then try each balanced {...} region until one parses to a
    dict carrying a 'criteria' list (json first, then ast.literal_eval for
    single-quoted Python-style output). Raises ValueError with a raw snippet on
    total failure, so a parse bug is diagnosable from the error alone.
    """
    cleaned = _strip_wrappers(raw)
    candidates = list(_iter_balanced_objects(cleaned)) or [cleaned]
    for obj_text in candidates:
        obj = _try_load(obj_text)
        if isinstance(obj, dict) and isinstance(obj.get("criteria"), list):
            return obj["criteria"]
    # Head + tail + length make truncation (tail is mid-reasoning, no closing
    # brace) distinguishable from malformed JSON (tail is broken JSON) without a
    # second slow round-trip.
    raise ValueError(
        f"no 'criteria' object; len={len(raw)} "
        f"head={raw[:160]!r} tail={raw[-160:]!r}"
    )


def _call_openai(instruction: str, model: str, max_tokens: int,
                 timeout: float = 600, attempts: int = 4) -> list[dict]:
    try:
        import openai
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "openai package not installed and no `decomposition=` supplied; "
            "cannot decompose without a model"
        ) from exc
    base_url = os.environ.get("OPENAI_BASE_URL")  # set by --endpoint-port on the server
    # A reasoning model emitting up to `max_tokens` of thinking + JSON can take
    # minutes; 180s was too short for the complex tasks. Retry transient
    # network/timeout errors with backoff (the shared vLLM occasionally drops
    # connections); do NOT retry a parse failure (deterministic — a retry just
    # wastes a slow call).
    client = openai.OpenAI(
        base_url=base_url,
        api_key=os.environ.get("OPENAI_API_KEY", "EMPTY"),  # vLLM ignores the value
        timeout=timeout,
    )
    transient = (
        getattr(openai, "APITimeoutError", ()),
        getattr(openai, "APIConnectionError", ()),
        getattr(openai, "InternalServerError", ()),
    )
    transient = tuple(t for t in transient if isinstance(t, type))
    last_err = None
    for attempt in range(attempts):
        try:
            resp = client.chat.completions.create(
                model=model, max_tokens=max_tokens, temperature=0,
                messages=[{"role": "system", "content": SYS},
                          {"role": "user", "content": build_user_prompt(instruction)}],
            )
            return _parse_criteria(resp.choices[0].message.content or "")
        except transient as exc:
            last_err = exc
            time.sleep(2 ** attempt)  # 1, 2, 4, 8s backoff
        except Exception as exc:  # noqa: BLE001 — parse / other: one retry only
            last_err = exc
            if attempt >= 1:
                break
    raise RuntimeError(f"decompose model call failed: {last_err}")


def decompose(instruction: str, *, decomposition: list[dict] | None = None,
              model: str = "qwen3.5-27b", max_tokens: int = 12000) -> list[dict]:
    """Return a criteria list for an instruction.

    `decomposition` bypasses the model (offline / hand-authored). Otherwise the
    model is called via the OpenAI-compatible endpoint in the environment.
    """
    if decomposition is not None:
        return decomposition
    return _call_openai(instruction, model, max_tokens)


def decompose_task_file(task_json_path, **kw) -> tuple[str, list[dict]]:
    """Load a task.json, decompose its instruction (NOT its verification[])."""
    with open(task_json_path, encoding="utf-8") as f:
        task = json.load(f)
    instruction = task.get("task") or task.get("instruction", "")
    return task["id"], decompose(instruction, **kw)
