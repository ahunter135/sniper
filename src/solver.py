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
import time

import httpx

log = logging.getLogger("sniper.solver")


# Global minimum interval between Claude calls (any caller — classify_giveaway,
# is_actionable_by_bot, solve_with_llm). The Anthropic free tier is 5 RPM
# (1 call / 12s); default 13s gives a small safety margin. Override via env:
#     SNIPER_LLM_MIN_INTERVAL=2.5   # for build tier (50 RPM)
# When a call is throttled by this gate, _call_claude returns None
# immediately — no retry, no wait — so the scan loop stays responsive.
_LLM_MIN_INTERVAL = float(os.environ.get("SNIPER_LLM_MIN_INTERVAL", "13"))
_last_claude_call_ts: float = 0.0


def _call_claude(prompt: str, *, max_tokens: int = 80,
                 timeout: float = 8.0, max_retries: int = 2) -> str | None:
    """Single Claude call with retry on 429 / 5xx. Returns text or None.

    Returns None when:
      - ANTHROPIC_API_KEY is unset
      - Globally throttled (last call < _LLM_MIN_INTERVAL seconds ago)
      - All retries exhausted on a transient error
      - The response shape is unexpected
    """
    global _last_claude_call_ts
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None
    now = time.monotonic()
    elapsed = now - _last_claude_call_ts
    if elapsed < _LLM_MIN_INTERVAL:
        log.info(
            f"claude throttled ({elapsed:.1f}s since last call, "
            f"min {_LLM_MIN_INTERVAL}s)"
        )
        return None
    _last_claude_call_ts = now
    for attempt in range(max_retries + 1):
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
                    "max_tokens": max_tokens,
                    "messages": [{"role": "user", "content": prompt}],
                },
                timeout=timeout,
            )
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < max_retries:
                retry_after = resp.headers.get("retry-after")
                wait = float(retry_after) if retry_after else (1.5 ** attempt) + 0.5
                log.warning(
                    f"claude {resp.status_code}; backing off {wait:.1f}s "
                    f"(attempt {attempt + 1}/{max_retries})"
                )
                time.sleep(wait)
                continue
            resp.raise_for_status()
            body = resp.json()
            for block in body.get("content", []):
                if block.get("type") == "text":
                    return block["text"].strip()
            return None
        except httpx.HTTPStatusError as e:
            log.warning(
                f"claude HTTP {e.response.status_code}: {e.response.text[:200]}"
            )
            return None
        except Exception as e:
            log.warning(f"claude exception: {type(e).__name__}: {e}")
            return None
    return None

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


def solve_with_llm(text: str) -> str | None:
    """Math/riddle solver via Claude. Returns the bare answer string."""
    prompt = (
        "You are reading a watch-giveaway social media post. The first "
        "commenter with the correct answer wins. Reply with ONLY the answer "
        "— no explanation, no punctuation, no quotes. For a math problem, "
        "give just the number. For a riddle, give just the word or phrase.\n\n"
        f"Post text:\n{text}"
    )
    out = _call_claude(prompt, max_tokens=50)
    if not out:
        return None
    return out.strip().strip('"').strip("'")


def is_actionable_by_bot(text: str, intended_comment: str) -> tuple[bool, str]:
    """LLM safety gate. Returns (safe, reason).

    Asks Claude whether posting `intended_comment` is the ONLY action needed
    to fairly enter the giveaway described in `text`. Blocks posts that
    require off-bot actions: inviting/tagging users, following, reposting,
    creating a new post, clicking external links, etc.

    Fail-open: missing API key, network error, or an unrecognized response
    all return (True, ...) so the bot keeps shooting when the LLM isn't
    available. Only an explicit BLOCK from the LLM returns False.
    """
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return True, "no API key set; rules check disabled"

    prompt = (
        "You are reviewing a watch-giveaway social media post to decide if a "
        "comment-bot can fairly enter. The bot can ONLY post a single comment "
        "containing exactly the text shown below — it cannot follow accounts, "
        "tag or invite friends, send DMs, create new posts, click external "
        "links, like/react, or take any other action.\n\n"
        f'Post text:\n"""\n{text}\n"""\n\n'
        f'The bot will post exactly this comment: "{intended_comment}"\n\n'
        "Does entering this giveaway require ANYTHING BEYOND posting that "
        "single comment ON THIS POST? Common disqualifying requirements:\n"
        "- Tagging or inviting other users\n"
        "- Following an account\n"
        "- Reposting, sharing, or re-uploading content\n"
        "- Creating a new TOP-LEVEL post (not a comment). Phrasing like "
        '"post on the app", "make a post", "create a post", "post about X" '
        "means a brand-new post, not a reply/comment. BLOCK these.\n"
        "- Visiting an external link\n"
        "- Liking or reacting to the post\n"
        "- DMing the poster (before winning)\n"
        "- Multi-step actions across multiple posts\n\n"
        'Post-win costs like "just pay shipping" or "DM me to arrange '
        'pickup after you win" are OK and do NOT disqualify entry.\n\n'
        "Reply with one word on the first line: SAFE or BLOCK\n"
        "Then on a new line, a brief reason (under 20 words)."
    )
    out = _call_claude(prompt, max_tokens=80)
    if not out:
        return True, "LLM unavailable; proceeding"
    lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
    verdict = lines[0].upper() if lines else ""
    reason = " ".join(lines[1:]).strip() or "no reason given"
    if verdict.startswith("BLOCK"):
        return False, reason
    if verdict.startswith("SAFE"):
        return True, reason
    return True, f"unrecognized verdict {verdict!r}; proceeding"


def classify_giveaway(text: str) -> tuple[str | None, str, str]:
    """LLM-driven primary classifier for Daniel's posts.

    Decides three things in a single Claude call:
      1. Is this an active giveaway entered by posting a single comment?
      2. What KIND of prize: watch / merch / other
      3. What should the bot comment?

    Returns (comment, kind, status):
      - status="yes"   → (comment, kind, "yes")   ; kind ∈ {watch, merch, other}
      - status="no"    → (None,    "unknown", "no")
      - status="error" → (None,    "unknown", "error")  ; no key, network, rate-limit, unparseable

    Caller should persist "no" results in state to avoid re-checking the
    same post every tick; "error" results are retried on the next scan.

    `kind` is the watchlist filter's hook: callers should apply the
    `--filter` keywords only when kind == "watch" (or "unknown", as a
    safe fallback when the LLM was unavailable).
    """
    if not text:
        return None, "unknown", "no"
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None, "unknown", "error"

    prompt = (
        "You are watching the WatchLink (watchlink.co) account run by Daniel "
        "Cheek. He posts a mix of: watch giveaways (sometimes free with "
        "shipping, sometimes 'first to comment X'), WatchLink-branded merch "
        "giveaways (hats, t-shirts, stickers, hoodies, mugs), math puzzles, "
        "and regular content (priced sales, opinions, jokes, hype, photos).\n\n"
        "Decide if THIS specific post is an ACTIVE giveaway where the entry "
        "method is to post a single comment.\n\n"
        f'Post text:\n"""\n{text}\n"""\n\n'
        "Reply with exactly ONE line, in one of these two forms:\n"
        "  NO\n"
        "  YES <kind> <comment>\n\n"
        "Where <kind> is exactly one of:\n"
        "  watch — Daniel is giving away a wristwatch (any brand)\n"
        "  merch — Daniel is giving away WatchLink-branded gear (hat, "
        "t-shirt, hoodie, sticker, mug, tote, etc.)\n"
        "  other — any other prize (cash, gift card, mystery, non-watch "
        "items that aren't WL merch)\n\n"
        "And <comment> is the single short text the bot should post "
        "(≤ 25 characters, no surrounding quotes):\n"
        "- For 'first to comment X gets it' → X\n"
        "- For math problems / riddles → the answer\n"
        "- For 'first N comments win' or open-ended invites → me\n\n"
        "Examples:\n"
        "  NO\n"
        "  YES watch sold\n"
        "  YES watch 58\n"
        "  YES watch mine\n"
        "  YES merch me\n"
        "  YES merch tee\n\n"
        "Treat these as NO:\n"
        "- Priced sales (item costs money beyond shipping)\n"
        "- Announcements of an UPCOMING giveaway — even later TODAY — that "
        "isn't live yet. Phrasing like \"going to be today's daily deal\", "
        '"be ready when it drops", "later today", "coming up", "stay tuned", '
        '"about to drop" means the giveaway hasn\'t opened yet. Wait for the '
        "actual drop post.\n"
        "- Posts hinting at a top-level POST entry mechanic, not a comment. "
        'Phrasing like "post on the app today", "make a post", "create a '
        'post", "get in your post first", "post first", "make sure you post" '
        "all mean entry requires a NEW TOP-LEVEL post.\n"
        "- Opinions, jokes, hype, tattoo pics, just-arrived-in-the-mail posts\n"
        "- Posts about giveaways that have already concluded\n"
        "- Giveaways requiring follows, tags, invites, DMs, reposts, or "
        "external clicks"
    )
    out = _call_claude(prompt, max_tokens=40)
    if out is None:
        return None, "unknown", "error"
    line = out.split("\n", 1)[0].strip()
    upper = line.upper()
    if upper.startswith("NO"):
        return None, "unknown", "no"
    if upper.startswith("YES"):
        parts = line.split(maxsplit=2)
        if len(parts) >= 3:
            kind = parts[1].lower()
            if kind not in ("watch", "merch", "other"):
                kind = "other"
            comment = parts[2].strip().strip('"').strip("'")
            if 1 <= len(comment) <= 25:
                return comment, kind, "yes"
    # Couldn't parse — treat as transient so we retry later.
    return None, "unknown", "error"


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
