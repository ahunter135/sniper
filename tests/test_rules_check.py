"""Battery test for the LLM rules-check gate.

Each test gives the LLM (1) a post body and (2) the comment the bot would
post, then asks "can the bot fairly enter this with one comment?"

Run:
    source .venv/bin/activate
    python tests/test_rules_check.py
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

os.environ.setdefault("SNIPER_LOG_FILE", "/tmp/watchlink_bench.log")
os.environ.setdefault("SNIPER_LLM_MIN_INTERVAL", "0")

from solver import is_actionable_by_bot
from sniper import load_dotenv


CASES: list[tuple[str, str, str, bool]] = [
    # (label, post_text, intended_comment, expected_safe)
    (
        "quoted-word (clean)",
        'First person to comment "me" gets this Seiko. $0 + shipping.',
        "me",
        True,
    ),
    (
        "math problem (clean)",
        "WatchLink Daily Deal 5/14/26. First to solve gets this Seiko for "
        "free (just pay shipping): (18/3) x (7+5) - 14",
        "58",
        True,
    ),
    (
        "Free99 daily deal (clean)",
        "WatchLink Daily Deal 5/13/26. Look at this beauty. Free99 + shipping "
        "Who's snaggin'?",
        "sold",
        True,
    ),
    (
        "invite 3 friends (block)",
        "Daily Deal 5/16/26. To win this Seiko, invite 3 friends to "
        "WatchLink and post their names in the comments.",
        "Tom, Dick, Harry",
        False,
    ),
    (
        "tag 2 friends (block)",
        "First commenter to tag 2 friends below wins this Rolex Submariner.",
        "me",
        False,
    ),
    (
        "follow + comment (block)",
        "To enter: follow @watchlink and comment your favorite watch.",
        "me",
        False,
    ),
    (
        "repost required (block)",
        "Repost this to your story and comment 'done' to enter.",
        "done",
        False,
    ),
    (
        "create a new post (block)",
        "We're dropping this Seiko Panda for free. To get it, just post on "
        "the app today.",
        "sold",
        False,
    ),
    (
        "external link required (block)",
        "Click the link in our bio, sign up, and comment your username here "
        "to enter the giveaway.",
        "username",
        False,
    ),
    (
        "post-win shipping OK (clean)",
        'First to comment "claim" wins this Seiko. Winner pays shipping '
        "via PayPal after I DM them.",
        "claim",
        True,
    ),
    (
        "post-win DM OK (clean)",
        "First commenter wins. DM me your address after I announce the "
        "winner so I can ship.",
        "me",
        True,
    ),
    (
        "top-N comments (clean, no extra action)",
        "First 3 comments get a WL hat.",
        "me",
        True,
    ),
]


def main() -> None:
    load_dotenv()
    rows: list[dict] = []
    for i, (label, text, comment, expected) in enumerate(CASES):
        # Light throttle. The shared _call_claude helper already retries on
        # 429, but pacing the test reduces wall-clock retry time.
        if i > 0:
            time.sleep(1.5)
        t0 = time.perf_counter()
        safe, reason = is_actionable_by_bot(text, comment)
        elapsed = time.perf_counter() - t0
        rows.append({
            "label": label,
            "comment": comment,
            "expected": "SAFE" if expected else "BLOCK",
            "verdict": "SAFE" if safe else "BLOCK",
            "ok": (safe == expected),
            "reason": reason[:80],
            "latency_s": elapsed,
        })

    print()
    print("| # | label | comment | expected | verdict | OK | latency | reason |")
    print("|---|---|---|---|---|---|---|---|")
    for i, r in enumerate(rows, 1):
        ok = "✓" if r["ok"] else "✗"
        comment = f"`{r['comment']!r}`".replace("|", "\\|")
        reason = r["reason"].replace("|", "\\|")
        lat = f"{r['latency_s']:.2f} s"
        print(
            f"| {i} | {r['label']} | {comment} | {r['expected']} | "
            f"{r['verdict']} | {ok} | {lat} | {reason} |"
        )

    passed = sum(1 for r in rows if r["ok"])
    print(f"\nTotal: {passed}/{len(rows)} matched expected verdict.")


if __name__ == "__main__":
    main()
