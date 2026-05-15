"""Detect 'first to comment X' giveaway patterns and extract X."""
from __future__ import annotations

import re
from typing import Any

_QUOTE_CLASS = r"[\"'‘’“”«»]"
_QUOTED = re.compile(
    rf"{_QUOTE_CLASS}([^\"'‘’“”«»\n]{{1,30}}){_QUOTE_CLASS}"
)
_VALID_WORD = re.compile(r"^[\w\s!?.,\-#$@]{1,30}$", re.UNICODE)

_TRIGGER_ACTIONS = (
    "comment", "say", "reply", "type", "write", "post", "drop", "leave", "dm",
)
_CONTEXT_WINDOW = 90

DEFAULT_FREE_DEAL_WORD = "sold"

# Daniel headlines every giveaway with some variant of "WatchLink Daily Deal".
DAILY_DEAL_HEADER = re.compile(r"watch\s*link\s+daily\s+deal", re.IGNORECASE)
# Free-price tokens we've seen Daniel use in the body. Required in addition to
# the header — together they fence out paid listings that happen to mention
# "free shipping" or "$0 down".
_FREE_TOKENS = re.compile(
    r"\$0\b|\bfree\s*9+\b|\bfree\.\d+\b|\bfree\b",
    re.IGNORECASE,
)


def find_giveaway_word(text: str) -> str | None:
    """Return the word to comment, or None if no giveaway pattern matches.

    Heuristic: any short quoted substring whose surrounding context (+/- ~90 chars)
    mentions 'first' and one of the action verbs (comment/say/reply/...). This
    covers phrasings like:
        First to comment "me"
        First person to comment "sold" gets it.
        Drop "mine" first and it's yours.
    """
    if not text:
        return None
    lower = text.lower()
    for m in _QUOTED.finditer(text):
        quoted = m.group(1).strip()
        if not quoted or not _VALID_WORD.match(quoted):
            continue
        start = max(0, m.start() - _CONTEXT_WINDOW)
        end = min(len(text), m.end() + _CONTEXT_WINDOW)
        window = lower[start:end]
        if "first" not in window:
            continue
        if not any(a in window for a in _TRIGGER_ACTIONS):
            continue
        return quoted
    return None


def is_free_giveaway_post(post: dict, text: str | None = None) -> bool:
    """True iff the post payload represents a giveaway listing.

    Daniel's giveaway series is inconsistent in how price is expressed, so
    detection is layered:

      1. price == 0 (any numeric form) — the listing form was filled with $0
         (e.g., the SRPJ13 drop on 2026-05-12).
      2. "WatchLink Daily Deal" header + any free token in a buy_sell post —
         covers slang wording like "Free99 + shipping" (5/13/26) where price
         is null and the body never contains the literal "$0".
      3. price == null + body contains "$0" + brand set + buy_sell category —
         the announcement-style SSK033 drop on 2026-05-04 (post 1548); the
         brand guard distinguishes a real listing from an announcement that
         mentions $0 in passing (post 1448 had brand=null).
    """
    if _is_zero_numeric_price(_extract_price(post)):
        return True

    if text is None:
        return False

    # Daily Deal header + free token → giveaway regardless of category.
    # Daniel posted the 5/14/26 deal under "discussion", not "buy_sell".
    if DAILY_DEAL_HEADER.search(text) and _FREE_TOKENS.search(text):
        return True

    # Legacy path for posts without the header: $0 in body + brand set +
    # buy_sell. The brand+category guards filter announcement posts.
    if (
        "$0" in text
        and post.get("brand")
        and post.get("category") == "buy_sell"
    ):
        return True

    return False


def _is_zero_numeric_price(price: Any) -> bool:
    if isinstance(price, bool):
        return False
    if isinstance(price, (int, float)):
        return price == 0
    if isinstance(price, str):
        s = price.strip().lstrip("$").replace(",", "").strip()
        if not s:
            return False
        try:
            return float(s) == 0
        except ValueError:
            return False
    return False


def _extract_price(post: dict) -> Any:
    for key in ("price", "asking_price", "amount"):
        v = post.get(key)
        if v is not None:
            return v
    cents = post.get("price_cents")
    if isinstance(cents, (int, float)) and not isinstance(cents, bool):
        return cents / 100.0
    return None
