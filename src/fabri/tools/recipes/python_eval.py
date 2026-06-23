"""Recipe: a math-expression evaluator. Uses ast.parse to gate the expression
to a tight subset (numbers, math, arithmetic) so the agent can do quick
calculations without bringing in a heavy tool."""
import ast
import json
import math
import operator
import sys

_BIN_OPS = {
    ast.Add: operator.add, ast.Sub: operator.sub,
    ast.Mult: operator.mul, ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNARY_OPS = {ast.USub: operator.neg, ast.UAdd: operator.pos}
_NAMES = {k: getattr(math, k) for k in ("pi", "e", "tau", "inf", "nan")}
_NAMES.update({
    "sqrt": math.sqrt, "log": math.log, "log2": math.log2, "log10": math.log10,
    "exp": math.exp, "sin": math.sin, "cos": math.cos, "tan": math.tan,
    "floor": math.floor, "ceil": math.ceil, "abs": abs, "min": min, "max": max,
})


def _eval(node):
    if isinstance(node, ast.Expression):
        return _eval(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _BIN_OPS:
        return _BIN_OPS[type(node.op)](_eval(node.left), _eval(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY_OPS:
        return _UNARY_OPS[type(node.op)](_eval(node.operand))
    if isinstance(node, ast.Name) and node.id in _NAMES:
        return _NAMES[node.id]
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _NAMES:
        f = _NAMES[node.func.id]
        return f(*[_eval(a) for a in node.args])
    raise ValueError(f"refused: AST node {type(node).__name__} not in safe subset")


def main() -> int:
    args = json.loads(sys.stdin.read())
    expr = args["expr"]
    try:
        tree = ast.parse(expr, mode="eval")
        value = _eval(tree)
    except Exception as e:
        print(json.dumps({"error": str(e)}))
        return 1
    print(json.dumps({"expr": expr, "value": value}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
