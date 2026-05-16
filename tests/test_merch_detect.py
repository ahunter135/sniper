"""Battery test for the LLM general-giveaway detector.

Covers merch giveaways (hats, t-shirts, stickers) and other novel mechanics
the local matcher doesn't recognize. Also confirms the detector says NO to
non-giveaways (sales, opinions, jokes, photos).

Run:
    source .venv/bin/activate
    python tests/test_merch_detect.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from solver import find_general_giveaway_comment
from sniper import load_dotenv


CASES: list[tuple[str, str, str | None]] = [
    # (label, post_text, expected_comment_or_None)
    # None means: expected status "no" (not a giveaway)
    # A string means: expected status "yes" and that's the expected comment

    # --- Merch giveaways (should detect)
    (
        "first 3 get a WL hat",
        "First 3 comments get a WL hat",
        "me",
    ),
    (
        "first to comment tee gets a tshirt",
        'First to comment "tee" gets a WatchLink t-shirt',
        "tee",
    ),
    (
        "sticker drop",
        "I have 5 WL stickers to give out. First 5 people to drop a 🐢 win one!",
        "🐢",
    ),
    (
        "hoodie giveaway",
        "Free WatchLink hoodie for the first commenter. Just say me below.",
        "me",
    ),

    # --- Watch giveaways (should detect; existing matchers also cover these)
    (
        "quoted-word watch giveaway",
        'First person to comment "mine" gets this Tudor BB58. $0 + shipping.',
        "mine",
    ),
    (
        "math problem (no header)",
        "Solve this and the watch is yours: 9 x 8 - 12",
        "60",
    ),

    # --- NOT giveaways (should say NO)
    (
        "got a hat in the mail",
        "Just received my WatchLink hat in the mail today. Love it.",
        None,
    ),
    (
        "future merch announcement",
        "Working on some new merch designs. Hats and tees coming next month!",
        None,
    ),
    (
        "regular sale",
        "Selling my Seiko SKX007 for $450 + shipping. DM if interested.",
        None,
    ),
    (
        "tattoo hype",
        "Antonio Guerrera is a wild man for getting a WatchLink tattoo 😭",
        None,
    ),
    (
        "concluded giveaway",
        "Congrats to @brian for winning yesterday's hat giveaway!",
        None,
    ),
    (
        "regular content",
        "Haters are saying this is AI generated. It's not.",
        None,
    ),
]


def main() -> None:
    load_dotenv()
    rows: list[dict] = []
    for i, (label, text, expected) in enumerate(CASES):
        if i > 0:
            time.sleep(1.5)
        t0 = time.perf_counter()
        comment, status = find_general_giveaway_comment(text)
        elapsed = time.perf_counter() - t0
        if expected is None:
            ok = status == "no"
            exp_str = "NO"
            got_str = "NO" if status == "no" else (
                f"YES {comment!r}" if status == "yes" else "ERROR"
            )
        else:
            ok = status == "yes" and comment == expected
            exp_str = f"YES {expected!r}"
            got_str = (
                f"YES {comment!r}" if status == "yes" else
                ("NO" if status == "no" else "ERROR")
            )
        rows.append({
            "label": label,
            "expected": exp_str,
            "got": got_str,
            "ok": ok,
            "latency_s": elapsed,
        })

    print()
    print("| # | label | expected | got | OK | latency |")
    print("|---|---|---|---|---|---|")
    for i, r in enumerate(rows, 1):
        ok = "✓" if r["ok"] else "✗"
        lat = f"{r['latency_s']:.2f} s"
        print(
            f"| {i} | {r['label']} | {r['expected']} | {r['got']} | {ok} | {lat} |"
        )

    passed = sum(1 for r in rows if r["ok"])
    print(f"\nTotal: {passed}/{len(rows)} matched expected outcome.")


if __name__ == "__main__":
    main()
