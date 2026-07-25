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

import json
import os
from pathlib import Path

from process_checks.auto.registry_vscode import op_registry_doc

SYS = """You decompose a GUI-agent TASK into the atomic criteria that must ALL hold for the task to be considered done, then tier each criterion by how its truth can be established. Be complete (cover everything the instruction requires) but not redundant. Extract concrete parameters (key, value, file path, command, prefix) from the instruction into the check when tier T1.

Tiers:
- T1: maps to a deterministic registry op — give op + params (with the extracted expected value/target).
- T2: the state is readable (a file/app value) but NO registry op fits — mark tier T2 (a getter+judge will handle it).
- T3: purely visual/spatial with no readable state — tier T3.

Output STRICT JSON only:
{"criteria": [{"text": "<atomic criterion>", "tier": "T1|T2|T3", "bind": {"op": "...", "params": {...}}, "note": "<why this tier>"}]}
For T2/T3, set "bind": null."""


def build_user_prompt(instruction: str) -> str:
    return (f"{op_registry_doc()}\n\n"
            f"TASK INSTRUCTION:\n{instruction}\n\n"
            f"Decompose this task into atomic criteria and tier each. JSON only.")


def _parse_criteria(raw: str) -> list[dict]:
    """Pull the JSON object out of a model response (first { to last })."""
    s, e = raw.find("{"), raw.rfind("}")
    if s < 0 or e <= s:
        raise ValueError("no JSON object in model output")
    obj = json.loads(raw[s:e + 1])
    crit = obj.get("criteria")
    if not isinstance(crit, list):
        raise ValueError("parsed object has no 'criteria' list")
    return crit


def _call_openai(instruction: str, model: str, max_tokens: int) -> list[dict]:
    try:
        import openai
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "openai package not installed and no `decomposition=` supplied; "
            "cannot decompose without a model"
        ) from exc
    base_url = os.environ.get("OPENAI_BASE_URL")  # set by --endpoint-port on the server
    client = openai.OpenAI(
        base_url=base_url,
        api_key=os.environ.get("OPENAI_API_KEY", "EMPTY"),  # vLLM ignores the value
        timeout=180,
    )
    last_err = None
    for _ in range(2):  # one retry, as hers does
        try:
            resp = client.chat.completions.create(
                model=model, max_tokens=max_tokens, temperature=0,
                messages=[{"role": "system", "content": SYS},
                          {"role": "user", "content": build_user_prompt(instruction)}],
            )
            return _parse_criteria(resp.choices[0].message.content or "")
        except Exception as exc:  # noqa: BLE001
            last_err = exc
    raise RuntimeError(f"decompose model call failed: {last_err}")


def decompose(instruction: str, *, decomposition: list[dict] | None = None,
              model: str = "qwen3.5-27b", max_tokens: int = 1600) -> list[dict]:
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
