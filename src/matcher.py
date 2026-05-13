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
    """True iff the post payload represents a $0 listing.

    Used as a fallback for Daniel's free-watch giveaway series (e.g., the 8x
    Free Seiko Daily Deals starting with the SSK033 GMT) where the listing is
    posted at $0 with no explicit "first to comment X" word — the convention
    is that whoever comments first wins.

    Detection is layered because the API is inconsistent:
      1. price == 0 (any numeric form) — direct case.
      2. price == null + body text contains "$0" + post has structured
         listing metadata (brand set, category=buy_sell). This was the actual
         SSK033 drop on 2026-05-04: post 1548 had price=null and "$0 +
         shipping" only in the body. The structural guard (brand non-null)
         distinguishes a real listing from an announcement post that just
         mentions $0 in passing — the announcement (post 1448) had brand=null.
    """
    if _is_zero_numeric_price(_extract_price(post)):
        return True

    if text is None:
        return False
    if "$0" not in text:
        return False
    # Structural check: real $0 listings carry brand + buy_sell category;
    # announcement posts that mention "$0" in passing do not.
    if not post.get("brand"):
        return False
    if post.get("category") != "buy_sell":
        return False
    return True


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
