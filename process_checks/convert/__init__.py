"""process_checks/convert — authoring-time converters.

Turn existing task/evaluator formats into our per-condition checkpoint specs.
Conversion produces CANDIDATES only; every emitted condition must still clear
the validation gates in PROJECT.md before it is trusted.
"""

from .osworld import convert, convert_file, convert_tree

__all__ = ["convert", "convert_file", "convert_tree"]
