"""A small, honest evaluator for the S3 Vectors filter expressions the access scopes emit.

It interprets a filter the way S3 Vectors behaves: a leaf on a missing key, or comparing
values of different kinds (a string with a number, a bool with a number), does not match;
and **array-valued metadata matches when ANY element matches** (``{"audience": ["renter"]}``
satisfies ``audience = renter``). The live run of ``python -m common.filter_probe`` on the
``dev`` index (2026-11-23, ADR 0002 verification plan) confirmed the operators and the
array behaviour; the missing-key and type-mismatch behaviour was confirmed too. Nothing
here proves anything about a different index or service version.

It supports exactly ``common.access.FILTER_OPERATORS_USED`` and raises on anything else,
so the filter builder cannot start using an unverified operator unnoticed.
"""
from __future__ import annotations

SEEN_OPERATORS: set = set()


def _number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _equal(left, right) -> bool:
    if isinstance(left, list):  # S3 Vectors: an array matches if any element matches
        return any(_equal(item, right) for item in left)
    if _number(left) and _number(right):
        return left == right
    return type(left) is type(right) and left == right


def matches(expr, metadata) -> bool:
    assert isinstance(expr, dict) and len(expr) == 1, expr
    ((key, arg),) = expr.items()
    if key == "$and":
        SEEN_OPERATORS.add(key)
        return all(matches(e, metadata) for e in arg)
    if key == "$or":
        SEEN_OPERATORS.add(key)
        return any(matches(e, metadata) for e in arg)
    assert not key.startswith("$"), f"operator {key} used as a field"
    assert isinstance(arg, dict) and len(arg) == 1, arg
    ((op, operand),) = arg.items()
    SEEN_OPERATORS.add(op)
    assert op in {"$eq", "$in", "$lte", "$gte"}, f"unsupported operator {op}"
    if not isinstance(metadata, dict) or key not in metadata:
        return False
    value = metadata[key]
    if op == "$eq":
        return _equal(value, operand)
    if op == "$in":
        assert isinstance(operand, list) and operand, "$in needs a non-empty list"
        return any(_equal(value, item) for item in operand)
    if not (_number(value) and _number(operand)):
        return False
    return value <= operand if op == "$lte" else value >= operand


def walk(node):
    """Yield every (key, value) in a nested filter expression."""
    if isinstance(node, dict):
        for k, v in node.items():
            yield k, v
            yield from walk(v)
    elif isinstance(node, list):
        for item in node:
            yield from walk(item)
