"""Math-puzzle / answer extraction for Daniel's daily deal giveaways.

Daniel started escalating beyond "first to comment X" giveaways:
    5/14/26 → "First person to solve this math problem... (18/3) x (7+5) - 14"

Two tiers, in order:
  1. Local arithmetic via Python's `ast` module — BODMAS, paren nesting,
     `x`/`×`/`÷` aliases. Sub-millisecond, no network, no API key.
  2. Optional LLM fallback (Anthropic Haiku) — only fires if ANTHROPIC_API_KEY
     is in env and the local tier couldn't produce a number. Covers riddles,
     word problems, lateral-thinking puzzles.
"""
from __future__ import annotations

import ast
import logging
import operator as op
import os
import re

import httpx

log = logging.getLogger("sniper.solver")

# Keywords that signal "this post wants an answer in the comments." If none
# match, we don't bother solving — Daniel's normal "first to comment X" path
# handles those.
MATH_TRIGGER = re.compile(
    r"\bsolve\b|math\s+problem|math\s+question|\bequation\b|\bpuzzle\b|"
    r"\briddle\b|answer\s+this|\bguess\b|what(?:'s|\s+is|\s+do\s+you\s+get)",
    re.IGNORECASE,
)

_OPS = {
    ast.Add: op.add,
    ast.Sub: op.sub,
    ast.Mult: op.mul,
    ast.Div: op.truediv,
    ast.FloorDiv: op.floordiv,
    ast.Mod: op.mod,
    ast.Pow: op.pow,
    ast.USub: op.neg,
    ast.UAdd: op.pos,
}


def _eval_node(node: ast.AST) -> int | float:
    if isinstance(node, ast.Expression):
        return _eval_node(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_eval_node(node.left), _eval_node(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_eval_node(node.operand))
    raise ValueError(f"unsafe ast node: {ast.dump(node)}")


def _format_number(x: int | float) -> str:
    if isinstance(x, float) and x.is_integer():
        return str(int(x))
    if isinstance(x, float):
        return f"{x:.4f}".rstrip("0").rstrip(".")
    return str(x)


def solve_arithmetic(expr: str) -> str | None:
    """Safely evaluate an arithmetic expression. Returns the answer as a
    string, or None if the expression can't be parsed."""
    if not expr:
        return None
    # 'x' / 'X' / '×' between numeric boundaries → multiplication
    cleaned = re.sub(
        r"([\d\)])\s*[xX×]\s*([\d\(])", r"\1 * \2", expr,
    )
    cleaned = cleaned.replace("÷", "/")
    cleaned = cleaned.strip().strip("?!.;,:")
    if not cleaned:
        return None
    try:
        tree = ast.parse(cleaned, mode="eval")
        result = _eval_node(tree)
    except (SyntaxError, ValueError, KeyError, ZeroDivisionError, TypeError):
        return None
    if isinstance(result, complex):
        return None
    return _format_number(result)


# Chars that can appear inside an arithmetic expression. NB: `=` is excluded
# so "2 + 3 = ?" naturally splits the run at "= ?".
_MATH_CHARS = re.compile(r"[\d\s\+\-\*\/xX×÷\(\)\.]{4,}")
_HAS_OPERATOR = re.compile(r"[\+\-\*xX×÷]")


def extract_math_expression(text: str) -> str | None:
    """Pull a candidate arithmetic expression out of post body text.

    Anchors on a math-trigger phrase if one is present (avoids matching
    things like the post's date "5/14/26"), then picks the longest run of
    math characters that contains at least one operator.
    """
    if not text:
        return None
    anchor = MATH_TRIGGER.search(text)
    search_text = text[anchor.start():] if anchor else text
    candidates = [
        c.strip() for c in _MATH_CHARS.findall(search_text)
    ]
    scored = [c for c in candidates if _HAS_OPERATOR.search(c)]
    if not scored:
        return None
    return max(scored, key=len)


def solve_with_llm(text: str, timeout: float = 8.0) -> str | None:
    """Last-resort LLM call. Silently returns None unless ANTHROPIC_API_KEY
    is set in env. Asks Claude Haiku for just the bare answer."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None
    prompt = (
        "You are reading a watch-giveaway social media post. The first "
        "commenter with the correct answer wins. Reply with ONLY the answer "
        "— no explanation, no punctuation, no quotes. For a math problem, "
        "give just the number. For a riddle, give just the word or phrase.\n\n"
        f"Post text:\n{text}"
    )
    try:
        resp = httpx.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 50,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=timeout,
        )
        resp.raise_for_status()
        body = resp.json()
        for block in body.get("content", []):
            if block.get("type") == "text":
                return block["text"].strip().strip('"').strip("'")
    except Exception:
        log.exception("LLM fallback failed")
    return None


def solve_giveaway(text: str) -> str | None:
    """Top-level: if the post looks like a puzzle/math giveaway, return the
    answer to comment. Returns None if no puzzle is detected or no tier
    succeeded."""
    if not text or not MATH_TRIGGER.search(text):
        return None
    expr = extract_math_expression(text)
    if expr:
        answer = solve_arithmetic(expr)
        if answer is not None:
            log.info(f"math-local: {expr!r} → {answer}")
            return answer
        log.info(f"math-local: could not evaluate {expr!r}, trying LLM")
    answer = solve_with_llm(text)
    if answer:
        log.info(f"math-llm: {answer!r}")
    elif os.environ.get("ANTHROPIC_API_KEY") is None:
        log.info("math-llm: skipped (ANTHROPIC_API_KEY not set)")
    return answer
