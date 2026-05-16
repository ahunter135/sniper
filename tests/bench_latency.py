"""End-to-end latency benchmark for the sniper pipeline.

For each test post we measure the time from "post payload in hand" to "would-
have-posted decision". The pipeline path varies per post (quoted-word matcher,
free-deal fallback, local arithmetic, or LLM fallback) so the table reveals
how much each tier costs.

`post_comment` and `post_comments_count` are stubbed — nothing is actually
written to watchlink.co. State writes are also patched out.

Run:
    source .venv/bin/activate
    python tests/bench_latency.py
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

# Redirect the sniper file-handler to /tmp BEFORE importing sniper, so the
# bench's POSTED-on-stub entries don't pollute the live bot's log file.
os.environ.setdefault("SNIPER_LOG_FILE", "/tmp/watchlink_bench.log")
# Disable the production rate-limit throttle so back-to-back test calls run.
# The Anthropic per-minute cap will still apply server-side; the retry helper
# handles it.
os.environ.setdefault("SNIPER_LLM_MIN_INTERVAL", "0")

from api import WatchlinkClient, extract_text
from sniper import _consider_post, _creds, COOKIES_FILE, DANIEL_USER_ID, load_dotenv


def make_post(post_id: int, text: str, *, category: str = "buy_sell",
              brand: str | None = "Seiko") -> dict:
    """Build a synthetic post payload that looks like the live API shape."""
    return {
        "id": post_id,
        "category": category,
        "brand": {"name": brand} if brand else None,
        "price": None,
        "user": {"id": DANIEL_USER_ID},
        "text": text,
        "comments": {"comments_count": 0},
    }


def reset_comments(post: dict) -> dict:
    """Pretend the post just appeared with zero comments (so gate 2 passes)."""
    p = dict(post)
    if isinstance(p.get("comments"), dict):
        p["comments"] = {**p["comments"], "comments_count": 0}
    p["comments_count"] = 0
    return p


class StubClient:
    """Records calls without hitting the network."""
    def __init__(self) -> None:
        self.posted: dict | None = None

    def post_comments_count(self, _post_id) -> int:
        return 0

    def post_comment(self, post_id, text: str) -> dict:
        self.posted = {"post_id": post_id, "text": text}
        return {"id": 0}


def run_one(post: dict, label: str, results: list) -> None:
    client = StubClient()
    state = {"commented": {}, "matched": {}, "skipped_had_comments": {}}
    preview = extract_text(post).replace("\n", " ")[:70]
    with patch("sniper.save_state"):
        t0 = time.perf_counter()
        _consider_post(
            client, post, f"profile:{DANIEL_USER_ID}",
            armed=True, state=state,
        )
        elapsed = time.perf_counter() - t0
    results.append({
        "label": label,
        "post_id": post.get("id"),
        "preview": preview,
        "word": client.posted["text"] if client.posted else None,
        "latency_s": elapsed,
    })


def main() -> None:
    load_dotenv()
    results: list = []

    email, password = _creds()
    with WatchlinkClient(COOKIES_FILE) as c:
        c.ensure_authed(email, password)
        real_posts = c.user_posts(DANIEL_USER_ID, page=1, per_page=20)

    by_id = {p.get("id"): p for p in real_posts}

    if 2573 in by_id:
        run_one(reset_comments(by_id[2573]), "real 2573 (math, local arith)", results)
    if 2577 in by_id:
        run_one(reset_comments(by_id[2577]), "real 2577 (top-N, no match)", results)

    synth_cases = [
        ("synth quoted-word", make_post(
            99001,
            'WatchLink Daily Deal\n\nFirst person to comment "me" gets this '
            'Seiko Presage SRPJ13. $0 + shipping.',
        )),
        ("synth Free99 fallback", make_post(
            99002,
            "WatchLink Daily Deal Free99 + shipping. Seiko SSK033. Who wants it?",
        )),
        ("synth math (local arith)", make_post(
            99003,
            "WatchLink Daily Deal\n\nFirst person to solve this math problem "
            "in the comments gets this Seiko for free: (24/4) x (3+9) - 17",
        )),
        ("synth math word -> LLM", make_post(
            99004,
            "WatchLink Daily Deal\n\nMath problem: what is two cubed plus "
            "seven minus three? First correct answer wins.",
        )),
        ("synth riddle -> LLM", make_post(
            99005,
            "WatchLink Daily Deal\n\nRiddle: I have hands but no fingers, "
            "and I run without legs. What am I? First commenter with the "
            "correct answer wins.",
        )),
    ]
    for label, post in synth_cases:
        run_one(post, label, results)

    print()
    print("| # | label | post_id | preview | comment | latency |")
    print("|---|---|---|---|---|---|")
    for i, r in enumerate(results, 1):
        word = f"`{r['word']!r}`" if r["word"] is not None else "_(no action)_"
        if r["latency_s"] < 0.05:
            lat = f"{r['latency_s']*1000:.2f} ms"
        else:
            lat = f"{r['latency_s']:.2f} s"
        preview = r["preview"].replace("|", "\\|")
        print(f"| {i} | {r['label']} | {r['post_id']} | `{preview}` | {word} | {lat} |")


if __name__ == "__main__":
    main()
