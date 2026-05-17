"""Battery test for the LLM primary classifier.

`classify_giveaway` returns (comment, kind, status). This battery covers:
  - YES + watch (free Seiko deals, math problems, quoted-word watch giveaways)
  - YES + merch (hats, t-shirts, stickers, hoodies)
  - NO (sales, opinions, future announcements, concluded giveaways)

The `kind` field gates whether `--filter` keywords apply: only `watch`
giveaways get filtered by the user's watchlist; merch passes through.

Run:
    source .venv/bin/activate
    python tests/test_merch_detect.py
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

from solver import classify_giveaway
from sniper import load_dotenv


# Each case: (label, post_text, expected_comment_or_None, expected_kind_or_None,
#             line_must_contain_or_None)
# expected_comment=None means we expect status="no"; expected_kind is the
# expected kind on a "yes" verdict (ignored on "no").
# line_must_contain is a lowercase substring the LLM's `line` field must
# include (case-insensitive). Only checked when expected_kind == "watch".
CASES: list[tuple[str, str, str | None, str | None, str | None]] = [
    # --- WATCH giveaways (filter SHOULD apply here)
    (
        "watch: quoted-word",
        'First person to comment "mine" gets this Tudor BB58. $0 + shipping.',
        "mine", "watch", "black bay",
    ),
    (
        "watch: Free99 daily deal",
        "WatchLink Daily Deal 5/13/26. Look at this beauty. Free99 + shipping "
        "Who's snaggin'? Seiko SSK033.",
        "sold", "watch", "seiko",
    ),
    (
        "watch: math problem",
        "WatchLink Daily Deal. First person to solve gets this Seiko for "
        "free (just pay shipping): (18/3) x (7+5) - 14",
        "58", "watch", "seiko",
    ),
    (
        "watch: $0 panda",
        "WatchLink Daily Deal. Take this Seiko Panda for $0 + shipping. "
        "First to comment 'sold' wins.",
        "sold", "watch", "seiko",
    ),
    (
        # The key reason this whole feature exists: Daniel writes only the
        # reference number, not the line name. Filter `presage` must still
        # match via the LLM's line classification.
        "watch: SRPJ13 ref only (no 'Presage' word)",
        "First person to comment 'sold' gets this SRPJ13 BNIB. $0 + shipping.",
        "sold", "watch", "presage",
    ),
    (
        "watch: SARW035 ref only",
        "Free SARW035 for the first commenter. $0 + shipping.",
        "me", "watch", "presage",
    ),

    # --- MERCH giveaways (filter should NOT apply)
    (
        "merch: WL hat top-N",
        "First 3 comments get a WL hat",
        "me", "merch", None,
    ),
    (
        "merch: tshirt quoted-word",
        'First to comment "tee" gets a WatchLink t-shirt',
        "tee", "merch", None,
    ),
    (
        "merch: hoodie",
        "Free WatchLink hoodie for the first commenter. Just say me below.",
        "me", "merch", None,
    ),
    (
        "merch: sticker drop",
        "I have 5 WL stickers to give out. First 5 to drop a 🐢 win one!",
        "🐢", "merch", None,
    ),

    # --- NOT giveaways
    ("no: just arrived",
     "Just received my WatchLink hat in the mail today. Love it.",
     None, None, None),
    ("no: future announce",
     "Working on some new merch designs. Hats and tees coming next month!",
     None, None, None),
    ("no: priced sale",
     "Selling my Seiko SKX007 for $450 + shipping. DM if interested.",
     None, None, None),
    ("no: tattoo hype",
     "Antonio Guerrera is a wild man for getting a WatchLink tattoo 😭",
     None, None, None),
    ("no: concluded",
     "Congrats to @brian for winning yesterday's hat giveaway!",
     None, None, None),
    ("no: top-level post required",
     "We're dropping this Seiko Panda for free. To get it, just post on "
     "the app today.",
     None, None, None),
]


def main() -> None:
    load_dotenv()
    rows: list[dict] = []
    for i, (label, text, exp_comment, exp_kind, exp_line) in enumerate(CASES):
        if i > 0:
            time.sleep(1.5)
        t0 = time.perf_counter()
        comment, kind, status, line = classify_giveaway(text)
        elapsed = time.perf_counter() - t0

        line_ok = True
        if exp_line is not None and exp_kind == "watch":
            line_ok = bool(line) and exp_line.lower() in line.lower()

        if exp_comment is None:
            ok = status == "no"
            exp_str = "NO"
            got_str = (
                "NO" if status == "no" else
                (f"YES {kind} {comment!r} line={line!r}" if status == "yes" else "ERROR")
            )
        else:
            ok = (
                status == "yes"
                and comment == exp_comment
                and kind == exp_kind
                and line_ok
            )
            exp_str = f"YES {exp_kind} {exp_comment!r}"
            if exp_line:
                exp_str += f" line~{exp_line!r}"
            got_str = (
                f"YES {kind} {comment!r} line={line!r}" if status == "yes" else
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
            f"| {i} | {r['label']} | {r['expected']} | {r['got']} | "
            f"{ok} | {lat} |"
        )

    passed = sum(1 for r in rows if r["ok"])
    print(f"\nTotal: {passed}/{len(rows)} matched expected outcome.")


if __name__ == "__main__":
    main()
