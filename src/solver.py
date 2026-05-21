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
_LLM_MIN_INTERVAL = float(os.environ.get("SNIPER_LLM_MIN_INTERVAL", "15"))
_last_claude_call_ts: float = 0.0

# Model selection. Default Haiku 4.5 (fast, cheap, mostly accurate). Override
# via SNIPER_LLM_MODEL for harder questions / trivia accuracy:
#     SNIPER_LLM_MODEL=claude-sonnet-4-6              # ~2-3× slower, better recall
#     SNIPER_LLM_MODEL=claude-opus-4-7                # ~5-10× slower, highest accuracy
# Anthropic's Messages API expects model IDs in canonical form; see
# https://docs.anthropic.com/en/docs/about-claude/models.
_LLM_MODEL = os.environ.get("SNIPER_LLM_MODEL", "claude-haiku-4-5-20251001")


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
                    "model": _LLM_MODEL,
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


# Deterministic safety net: phrases that almost always mean the post requires
# off-bot action ("tell somebody about us, then comment 'done' with proof").
# Run BEFORE the LLM rules check so we still fail-closed when the LLM is
# rate-limited / offline. Conservative wording — biased toward false-positives
# (skip a real giveaway) over false-negatives (post on a rule-violating one).
_OFF_BOT_RED_FLAGS = re.compile(
    r"\b(?:"
    # Off-platform sharing / spreading the word about us
    r"tell\s+(?:somebody|someone|somebodies|anybody|anyone|"
    r"a\s+friend|your\s+friends?|some\s+friends?|"
    r"\d+\s+friends?|two\s+friends?|three\s+friends?|"
    r"people|everyone)"
    r"|spread\s+the\s+word"
    # Proof / receipts requirements
    r"|(?:send|dm|message|provide|show)\s+(?:me|us|daniel|him|the\s+host)?\s*"
    r"(?:proof|receipts?|screenshots?|evidence)"
    r"|prove\s+(?:that\s+)?you"
    # Tag / invite N friends
    r"|tag\s+(?:\d+|two|three|four|five|some|your|a)\s+(?:friends?|people|users?)"
    r"|invite\s+(?:\d+|two|three|four|five|some|your|a)\s+(?:friends?|people|users?)"
    # Pre-comment requirements (any action gated by "before commenting")
    r"|before\s+(?:you\s+)?comment(?:ing)?"
    # Follow / repost / external platforms
    r"|follow\s+(?:us|our|@|me\s+on)"
    r"|repost\s+(?:our|this|the|to|on|on\s+your)"
    r"|share\s+(?:this\s+)?(?:to|on)\s+(?:your\s+)?(?:story|feed|page|wall)"
    # Signup / subscribe / external clicks
    r"|sign\s+up\s+(?:on|for|at|first)"
    r"|subscribe\s+to"
    r"|link\s+in\s+(?:bio|comments?)"
    # Top-level post mechanic (different from commenting)
    r"|post\s+on\s+the\s+app"
    r"|(?:make|create)\s+(?:a|your)\s+(?:own\s+)?post"
    r")\b",
    re.IGNORECASE,
)


def has_off_bot_requirement(text: str) -> tuple[bool, str]:
    """True if `text` contains a phrase that means entry requires more than a
    single comment. Returns (matched, matching_phrase). LLM-free, works
    when rate-limited."""
    if not text:
        return False, ""
    m = _OFF_BOT_RED_FLAGS.search(text)
    return (True, m.group(0)) if m else (False, "")


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
    # Deterministic pre-screen — fail-closed on red-flag phrasing even when
    # the LLM is unavailable. This is what should have caught post 3093
    # ("tell somebody about WatchLink … message me receipts … before
    # commenting 'done'") when Haiku was throttled and the LLM rules check
    # fell back to fail-open.
    matched, phrase = has_off_bot_requirement(text)
    if matched:
        return False, f"off-bot action required (red-flag phrase {phrase!r})"

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


def classify_giveaway(text: str) -> tuple[str | None, str, str, str | None]:
    """LLM-driven primary classifier for Daniel's posts.

    Decides four things in a single Claude call:
      1. Is this an active giveaway entered by posting a single comment?
      2. What KIND of prize: watch / merch / other
      3. What should the bot comment?
      4. For watches: what's the line/family/model? (e.g. "Seiko Presage
         SRPJ13", inferred even when the post only shows a reference number)

    Returns (comment, kind, status, line):
      - status="yes"   → (comment, kind, "yes", line_or_None)
      - status="no"    → (None,    "unknown", "no",    None)
      - status="error" → (None,    "unknown", "error", None)

    `line` is the LLM's best guess at the specific watch family — used by
    the --filter gate so e.g. `--filter presage` matches "SRPJ13" posts
    even when the literal word "Presage" never appears in Daniel's text.
    None for merch/other/unknown.
    """
    if not text:
        return None, "unknown", "no", None
    # Hard veto: if the post text contains an off-bot-action red flag, the
    # bot cannot legitimately enter regardless of how the rest of the post
    # reads. Persisting as "no" stops the bot from posting AND from re-
    # querying the LLM on every tick.
    matched, phrase = has_off_bot_requirement(text)
    if matched:
        log.info(f"classify_giveaway: red-flag phrase {phrase!r} → no")
        return None, "unknown", "no", None
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None, "unknown", "error", None

    prompt = (
        "You are watching the WatchLink (watchlink.co) account run by Daniel "
        "Cheek. He posts a mix of: watch giveaways (sometimes free with "
        "shipping, sometimes 'first to comment X'), WatchLink-branded merch "
        "giveaways (hats, t-shirts, stickers, hoodies, mugs), math puzzles, "
        "and regular content (priced sales, opinions, jokes, hype, photos).\n\n"
        "Decide if THIS specific post is an ACTIVE giveaway where the entry "
        "method is to post a single comment.\n\n"
        f'Post text:\n"""\n{text}\n"""\n\n'
        "Reply with EXACTLY one of these formats. NO extra prose.\n\n"
        "  Form 1 (not a giveaway):\n"
        "    NO\n\n"
        "  Form 2 (giveaway, non-watch):\n"
        "    YES <kind> <comment>\n\n"
        "  Form 3 (giveaway, watch — two lines, no blank between):\n"
        "    YES watch <comment>\n"
        "    LINE <watch_line>\n\n"
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
        "For watch giveaways ONLY, the second LINE identifies the watch "
        "family/model using your watch-catalog knowledge. Recognise the "
        "model even when the post only shows a brand + reference number "
        "(e.g. 'SRPJ13' → 'Seiko Presage SRPJ13'; 'SARW035' → 'Seiko "
        "Presage SARW035'; 'BB58' → 'Tudor Black Bay 58'; '126610LN' → "
        "'Rolex Submariner 126610LN'). When the watch is genuinely "
        "ambiguous (e.g. just 'Free Seiko' with no model details), "
        "write: <brand> (line unknown).\n\n"
        "Seiko reference-prefix disambiguation (prefixes overlap across "
        "Seiko's catalog — apply these rules unless the post text clearly "
        "contradicts):\n"
        "- SARW, SARX, SARY, SARZ → Seiko Presage\n"
        "- SARB → Seiko classic dress / Alpinist (NOT a sport diver)\n"
        "- SRPJ, SRPB, SRPC → Seiko Presage\n"
        "- SRLP (e.g. SRLP75) → Seiko Presage (limited editions / "
        "collabs like the Riki Watanabe series)\n"
        "- SSA (e.g. SSA427, SSA395) → Seiko Presage Cocktail Time auto\n"
        "- SPB refs (apply these rules in order; first match wins):\n"
        "    Rule 1 (Prospex, ALWAYS): SPB143, SPB145, SPB147, SPB149, "
        "SPB151, SPB153, SPB185, SPB187, SPB239, SPB315, SPB317 — these "
        "are Prospex diver reissues (Willard, 62MAS, Captain Willard, "
        "etc.). Output 'Seiko Prospex' for these even if the post is "
        "ambiguous.\n"
        "    Rule 2 (Presage, ALWAYS): SPB117, SPB161-SPB169, "
        "SPB221-SPB223, SPB259-SPB263 — these are Presage (Sharp Edged, "
        "Star Bar, 60th Anniversary). Output 'Seiko Presage' for these.\n"
        "    Rule 3 (default): for any other SPB ref, output Presage "
        "UNLESS the post mentions diver / 200m / 300m / dive bezel.\n"
        "- SKX, SBDC, SBDX, SLA → Seiko Prospex (diver)\n"
        "- SRPK, SRPD7x, SRPE5x, SRPE7x → Seiko 5 Sports\n\n"
        "Examples:\n"
        "  NO\n\n"
        "  YES watch sold\n"
        "  LINE Seiko Presage SRPJ13\n\n"
        "  YES watch 58\n"
        "  LINE Seiko (line unknown)\n\n"
        "  YES watch mine\n"
        "  LINE Tudor Black Bay 58\n\n"
        "  YES merch me\n\n"
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
        "- Posts that require ANY off-bot action before commenting. Phrasing "
        'like "tell somebody about us / about WatchLink", "send me receipts", '
        '"message me proof", "DM screenshots", "prove that you", "screenshot '
        'your", "before commenting" all mean the post requires more than '
        "just a comment.\n"
        "- Opinions, jokes, hype, tattoo pics, just-arrived-in-the-mail posts\n"
        "- Posts about giveaways that have already concluded\n"
        "- Giveaways requiring follows, tags, invites, DMs, reposts, or "
        "external clicks"
    )
    out = _call_claude(prompt, max_tokens=80)
    if out is None:
        return None, "unknown", "error", None
    response_lines = [ln.strip() for ln in out.split("\n") if ln.strip()]
    first = response_lines[0] if response_lines else ""
    upper = first.upper()
    if upper.startswith("NO"):
        return None, "unknown", "no", None
    if upper.startswith("YES"):
        parts = first.split(maxsplit=2)
        if len(parts) >= 3:
            kind = parts[1].lower()
            if kind not in ("watch", "merch", "other"):
                kind = "other"
            comment = parts[2].strip().strip('"').strip("'")
            if 1 <= len(comment) <= 25:
                # Optional LINE on subsequent line — only meaningful for watches.
                watch_line: str | None = None
                if kind == "watch":
                    for subsequent in response_lines[1:]:
                        if subsequent.upper().startswith("LINE"):
                            watch_line = subsequent[4:].strip().strip(":").strip()
                            watch_line = watch_line or None
                            break
                return comment, kind, "yes", watch_line
    # Couldn't parse — treat as transient so we retry later.
    return None, "unknown", "error", None


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
