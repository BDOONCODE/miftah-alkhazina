"""معادلات المؤشرات المخصصة — محلل آمن بقائمة سماح، بدون eval.

المسموح: أرقام، أسماء مؤشرات معروفة (بالعربي مثل الفائض أو مخصص_الضريبة)،
العمليات + − × ÷ والأقواس، والدوال min و max و abs.
مثال: الفائض / الوارد * 100
"""
from __future__ import annotations

import ast
import operator
from collections.abc import Collection, Mapping

_BINARY = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv}
_UNARY = {ast.UAdd: operator.pos, ast.USub: operator.neg}
_FUNCS = {"min": min, "max": max, "abs": abs}
MAX_LENGTH = 300

_NORMALIZE = str.maketrans({"×": "*", "÷": "/", "−": "-", "٫": ".", "،": ",", **{d: str(i) for i, d in enumerate("٠١٢٣٤٥٦٧٨٩")}})


class FormulaError(ValueError):
    pass


def _parse(expr: str) -> ast.Expression:
    text = expr.translate(_NORMALIZE).strip()
    if not text:
        raise FormulaError("المعادلة فاضية")
    if len(text) > MAX_LENGTH:
        raise FormulaError("المعادلة طويلة جدًا")
    try:
        return ast.parse(text, mode="eval")
    except SyntaxError as exc:
        raise FormulaError("صيغة المعادلة غير صحيحة") from exc


def _check(node: ast.AST, allowed: Collection[str]) -> None:
    if isinstance(node, ast.Expression):
        _check(node.body, allowed)
    elif isinstance(node, ast.BinOp) and type(node.op) in _BINARY:
        _check(node.left, allowed)
        _check(node.right, allowed)
    elif isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY:
        _check(node.operand, allowed)
    elif isinstance(node, ast.Constant) and type(node.value) in (int, float):
        pass
    elif isinstance(node, ast.Name):
        if node.id not in allowed:
            raise FormulaError(f"مؤشر غير معروف: {node.id}")
    elif (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in _FUNCS
        and not node.keywords
        and node.args
    ):
        for arg in node.args:
            _check(arg, allowed)
    else:
        raise FormulaError("المعادلة فيها عنصر غير مسموح (المسموح: أرقام، مؤشرات، + - * / ( ) min max abs)")


def validate(expr: str, allowed: Collection[str]) -> None:
    _check(_parse(expr), allowed)


def _eval(node: ast.AST, variables: Mapping[str, float]) -> float:
    if isinstance(node, ast.Expression):
        return _eval(node.body, variables)
    if isinstance(node, ast.BinOp):
        return _BINARY[type(node.op)](_eval(node.left, variables), _eval(node.right, variables))
    if isinstance(node, ast.UnaryOp):
        return _UNARY[type(node.op)](_eval(node.operand, variables))
    if isinstance(node, ast.Constant):
        return float(node.value)
    if isinstance(node, ast.Name):
        return float(variables[node.id])
    if isinstance(node, ast.Call):
        return float(_FUNCS[node.func.id](*(_eval(a, variables) for a in node.args)))
    raise FormulaError("عنصر غير مسموح")


def evaluate(expr: str, variables: Mapping[str, float]) -> float | None:
    """يرجّع None لو فيه قسمة على صفر."""
    tree = _parse(expr)
    _check(tree, variables.keys())
    try:
        return _eval(tree, variables)
    except ZeroDivisionError:
        return None
