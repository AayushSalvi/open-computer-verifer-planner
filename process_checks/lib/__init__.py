"""process_checks/lib — shared checkpoint primitives.

Deterministic, read-only inspection helpers used by checkpoints when no
existing verifiers/<app>/<app>.py endpoint covers a check (tier-3 in
PROJECT.md's mapping procedure). Every primitive here should be logged in
process_checks/catalog.md.
"""

from .jsonc import JsoncError, loads_jsonc, read_jsonc_text

__all__ = ["loads_jsonc", "read_jsonc_text", "JsoncError"]
