"""watchlink.co giveaway sniper (API-backed, auto-login).

Commands:
    python src/sniper.py login           # force re-login from .env creds
    python src/sniper.py once            # one scan pass, dry-run
    python src/sniper.py once --arm      # one scan pass, POST comments for real
    python src/sniper.py loop            # poll every --interval seconds (default 300)

Safety invariants when armed:
    1. Post text must match a "first to comment X" pattern (matcher.py).
    2. Post's cached comment_count must be 0 (if the field is present).
    3. A fresh GET /posts/{id}/comments must report count == 0 right before posting.
       Only then do we POST the comment.

Credentials live in .env (not committed):
    WATCHLINK_EMAIL=you@example.com
    WATCHLINK_PASSWORD=...
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path

import httpx

from api import AuthExpired, WatchlinkClient, comments_count, extract_text
from matcher import (
    DAILY_DEAL_HEADER,
    DEFAULT_FREE_DEAL_WORD,
    find_giveaway_word,
    is_free_giveaway_post,
)
from solver import solve_giveaway

BASE = Path(__file__).resolve().parent.parent

DANIEL_USER_ID = "8493630a-4f84-4eab-9e20-b68f255f02ac"
# Daniel's posts already surface via /users/{id}/posts; the buy_sell feed scan
# was duplicate work that doubled per-tick latency. Set to ["buy_sell"] (or any
# category) only if you want broader feed coverage at the cost of speed.
FEED_CATEGORIES: list[str] = []

ENV_FILE = BASE / ".env"
COOKIES_FILE = BASE / "state" / "cookies.json"
STATE_FILE = BASE / "state" / "seen.json"
LOG_FILE = BASE / "logs" / "sniper.log"

MAX_POSTS_PER_SCAN = 20

# Optional watchlist filter. When non-empty, the bot only posts on a matched
# post if ALL of these keywords appear (case-insensitive substring match) in
# the post text or brand metadata. Empty list = no filter (current behavior).
# Set at startup from --filter.
_FILTER_KEYWORDS: list[str] = []

_console = logging.StreamHandler(sys.stdout)
_console.setFormatter(logging.Formatter("%(asctime)s %(message)s", datefmt="%H:%M:%S"))
_file = logging.FileHandler(LOG_FILE)
_file.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
logging.basicConfig(level=logging.INFO, handlers=[_console, _file])

# httpx logs every request at INFO; at 2s polling that's the loudest noise in
# the terminal. Demote third-party HTTP loggers; we still capture failures.
for _noisy in ("httpx", "httpcore"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

log = logging.getLogger("sniper")


# ---------- env + state ----------

def load_dotenv(path: Path = ENV_FILE) -> None:
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip()
        if v and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        os.environ.setdefault(k, v)


def load_state() -> dict:
    defaults = {"commented": {}, "matched": {}, "skipped_had_comments": {}}
    if STATE_FILE.exists():
        loaded = json.loads(STATE_FILE.read_text())
        for k, v in defaults.items():
            loaded.setdefault(k, v)
        return loaded
    return defaults


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True))


def _creds() -> tuple[str | None, str | None]:
    return os.environ.get("WATCHLINK_EMAIL"), os.environ.get("WATCHLINK_PASSWORD")


# ---------- main pipeline ----------

def _post_author_id(post: dict) -> str | None:
    user = post.get("user")
    if isinstance(user, dict):
        uid = user.get("id")
        if uid:
            return str(uid)
    for key in ("user_id", "author_id"):
        v = post.get(key)
        if v:
            return str(v)
    return None


def _is_daniel_post(post: dict, source: str) -> bool:
    if source.startswith(f"profile:{DANIEL_USER_ID}"):
        return True
    return _post_author_id(post) == DANIEL_USER_ID


def _post_matches_filter(post: dict, text: str | None) -> bool:
    """True if all _FILTER_KEYWORDS appear (case-insensitive) somewhere in
    the post text or brand metadata. Always True when the filter is empty."""
    if not _FILTER_KEYWORDS:
        return True
    haystack = (text or "").lower()
    brand = post.get("brand")
    if isinstance(brand, dict):
        haystack += " " + str(brand.get("name") or "").lower()
    elif isinstance(brand, str):
        haystack += " " + brand.lower()
    return all(kw.lower() in haystack for kw in _FILTER_KEYWORDS)


def _consider_post(client: WatchlinkClient, post: dict, source: str,
                   armed: bool, state: dict) -> None:
    post_id = str(post.get("id") or "")
    if not post_id:
        return
    if post_id in state["commented"]:
        return
    # Already-lost races: any post in skipped_had_comments was either >0 on
    # cached check or >0 on fresh check, so we can't win it. Skipping early
    # avoids matcher work + log noise on every cycle.
    if post_id in state.get("skipped_had_comments", {}):
        return

    text = extract_text(post)
    word = find_giveaway_word(text) if text else None

    is_daniel = _is_daniel_post(post, source)
    has_daily_deal_header = bool(text) and bool(DAILY_DEAL_HEADER.search(text))

    # Math-puzzle tier: Daniel's 5/14/26 deal swapped "first to comment X" for
    # "first to solve this math problem." Tries safe local arithmetic first,
    # then an Anthropic Haiku call if ANTHROPIC_API_KEY is set in .env. Runs
    # BEFORE the free-deal tier because math posts often contain the word
    # "free" (e.g. "...gets this Seiko for free") which would otherwise
    # mis-fire as a "sold" comment.
    if not word and is_daniel and has_daily_deal_header:
        answer = solve_giveaway(text)
        if answer:
            word = answer
            log.info(f"MATH-PUZZLE post={post_id} → {word!r}")

    # Fallback: Daniel's "Free Seiko Daily Deal" series (SSK033 GMT, etc.)
    # is listed at $0 without an explicit "first to comment X" phrase. When
    # we see Daniel post a $0 listing, default to commenting "sold" — the
    # community convention for his daily deals.
    if not word and is_daniel and is_free_giveaway_post(post, text):
        word = DEFAULT_FREE_DEAL_WORD
        log.info(f"FREE-DEAL post={post_id} (Daniel, $0 listing) → defaulting to {word!r}")

    # DEBUG-level diagnostic: one line per unmatched Daniel post so we can
    # reconstruct what each drop looked like at scan time. Off by default.
    if is_daniel and not word and log.isEnabledFor(logging.DEBUG):
        brand = post.get("brand")
        brand_name = brand.get("name") if isinstance(brand, dict) else brand
        log.debug(
            f"DANIEL-POST post={post_id} category={post.get('category')!r} "
            f"brand={brand_name!r} price={post.get('price')!r} "
            f"formatted_price={post.get('formatted_price')!r} "
            f"comments={comments_count(post)} "
            f"has_$0_in_text={'$0' in (text or '')} "
            f"preview={(text or '')[:120]!r}"
        )

    if not word:
        return

    cached_count = comments_count(post)
    log.info(
        f"MATCH source={source} post={post_id} word={word!r} "
        f"cached_comments={cached_count} preview={text[:140]!r}"
    )

    # Watchlist gate: if --filter is set, every keyword must appear in the
    # post text or brand. Logged but not persisted to state, so changing the
    # filter at restart re-considers previously-filtered posts.
    if not _post_matches_filter(post, text):
        log.info(
            f"FILTERED post={post_id}: keywords {_FILTER_KEYWORDS!r} "
            f"not all present in text/brand — skip"
        )
        return

    # Fast reject on cached count. The feed payload is authoritative enough
    # for a first-pass filter — if it already shows comments, we're late.
    if cached_count is not None and cached_count > 0:
        state["skipped_had_comments"][post_id] = {
            "word": word,
            "count": cached_count,
            "source": source,
            "ts": time.time(),
        }
        save_state(state)
        log.info(f"SKIP post={post_id}: already has {cached_count} comments (cached)")
        return

    state["matched"][post_id] = {
        "word": word,
        "text": text[:500],
        "source": source,
        "cached_comments": cached_count,
        "ts": time.time(),
    }
    save_state(state)

    if not armed:
        log.info(f"DRY-RUN: would comment {word!r} on post {post_id}")
        return

    # Last-mile check: fresh GET of the comments endpoint. This closes the
    # window between the list payload and our POST. Typically ~100-200ms old.
    try:
        fresh_count = client.post_comments_count(post_id)
    except Exception:
        log.exception(f"fresh comment-count check failed for {post_id}")
        return

    if fresh_count > 0:
        state["skipped_had_comments"][post_id] = {
            "word": word,
            "count": fresh_count,
            "source": source,
            "ts": time.time(),
            "via": "fresh_check",
        }
        save_state(state)
        log.info(f"SKIP post={post_id}: fresh check shows {fresh_count} comments — too late")
        return

    try:
        client.post_comment(post_id, word)
        state["commented"][post_id] = {
            "word": word,
            "source": source,
            "ts": time.time(),
        }
        save_state(state)
        log.info(f"POSTED {word!r} on post {post_id}")
    except httpx.HTTPStatusError as e:
        log.error(f"comment POST failed on {post_id}: {e.response.status_code} {e.response.text[:200]}")
    except Exception:
        log.exception(f"comment POST failed on {post_id}")


def _scan(client: WatchlinkClient, posts: list[dict], source: str,
          armed: bool, state: dict) -> None:
    for post in posts[:MAX_POSTS_PER_SCAN]:
        try:
            _consider_post(client, post, source, armed, state)
        except Exception:
            log.exception(f"_consider_post failed on post={post.get('id')}")


def run_once(armed: bool) -> None:
    load_dotenv()
    email, password = _creds()
    state = load_state()

    with WatchlinkClient(COOKIES_FILE) as client:
        try:
            client.ensure_authed(email, password)
        except AuthExpired as e:
            log.error(f"auth failed: {e}")
            return
        except Exception:
            log.exception("auth step crashed")
            return

        try:
            posts = client.user_posts(DANIEL_USER_ID, page=1, per_page=MAX_POSTS_PER_SCAN)
            log.info(f"Daniel's posts: {len(posts)}")
            _scan(client, posts, f"profile:{DANIEL_USER_ID}", armed, state)
        except Exception:
            log.exception("user_posts scan failed")

        for cat in FEED_CATEGORIES:
            try:
                posts = client.feed(cat, page=1, per_page=MAX_POSTS_PER_SCAN)
                log.info(f"Feed '{cat}': {len(posts)}")
                _scan(client, posts, f"feed:{cat}", armed, state)
            except Exception:
                log.exception(f"feed '{cat}' scan failed")


def _scan_all(client: WatchlinkClient, armed: bool, state: dict) -> None:
    """One scan pass against the configured surfaces. No auth/refresh — the
    caller is responsible for keeping the session warm.

    Deliberately silent on the happy path. The terminal stays quiet unless a
    match, skip, post, or error happens — at 2s polling, a per-tick "scanned
    N posts" line drowns everything else.
    """
    posts = client.user_posts(DANIEL_USER_ID, page=1, per_page=MAX_POSTS_PER_SCAN)
    _scan(client, posts, f"profile:{DANIEL_USER_ID}", armed, state)

    for cat in FEED_CATEGORIES:
        try:
            posts = client.feed(cat, page=1, per_page=MAX_POSTS_PER_SCAN)
            _scan(client, posts, f"feed:{cat}", armed, state)
        except Exception:
            log.exception(f"feed '{cat}' scan failed")


def run_loop(armed: bool, interval: int) -> None:
    """Long-lived poll loop. One WatchlinkClient instance + one auth handshake
    on startup; we only re-auth if the server returns 401 mid-loop. This is
    the hot path — every saved millisecond per tick widens our race window."""
    load_dotenv()
    email, password = _creds()
    state = load_state()

    with WatchlinkClient(COOKIES_FILE) as client:
        try:
            client.ensure_authed(email, password)
        except AuthExpired as e:
            log.error(f"initial auth failed: {e}")
            return
        except Exception:
            log.exception("initial auth crashed")
            return

        log.info(
            f"loop armed={armed} interval={interval}s "
            f"filter={_FILTER_KEYWORDS or 'off'}"
        )
        while True:
            tick_start = time.time()
            try:
                _scan_all(client, armed, state)
            except AuthExpired:
                log.info("session expired mid-loop, re-authenticating")
                try:
                    client.ensure_authed(email, password)
                except Exception:
                    log.exception("re-auth failed; backing off")
                    time.sleep(max(interval, 30))
                    continue
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 401:
                    log.info("401 mid-loop, re-authenticating")
                    try:
                        client.ensure_authed(email, password)
                    except Exception:
                        log.exception("re-auth after 401 failed; backing off")
                        time.sleep(max(interval, 30))
                        continue
                elif e.response.status_code == 429:
                    log.warning(f"429 rate-limited; backing off 60s")
                    time.sleep(60)
                    continue
                else:
                    log.exception(f"scan HTTP error {e.response.status_code}")
            except Exception:
                log.exception("scan cycle crashed")

            elapsed = time.time() - tick_start
            sleep_for = max(0.0, interval - elapsed)
            time.sleep(sleep_for)


def force_login() -> None:
    load_dotenv()
    email, password = _creds()
    if not email or not password:
        log.error("WATCHLINK_EMAIL and WATCHLINK_PASSWORD must be set in .env")
        sys.exit(1)
    with WatchlinkClient(COOKIES_FILE) as client:
        client.login(email, password)
        client.refresh()
    log.info(f"Login ok, cookies written to {COOKIES_FILE}")


def main() -> None:
    ap = argparse.ArgumentParser(description="watchlink giveaway sniper")
    ap.add_argument("command", choices=["login", "once", "loop"])
    ap.add_argument("--arm", action="store_true",
                    help="actually post comments (default: dry-run logs only)")
    ap.add_argument("--interval", type=int, default=300,
                    help="seconds between runs in loop mode (default 300)")
    ap.add_argument("--filter", type=str, default=None,
                    help='Only act on posts whose text/brand contains ALL of '
                         'these space-separated keywords '
                         '(case-insensitive, order-independent). E.g. '
                         '--filter "seiko presage". Omit for no filter.')
    args = ap.parse_args()

    global _FILTER_KEYWORDS
    _FILTER_KEYWORDS = (args.filter or "").split()

    if args.command == "login":
        force_login()
    elif args.command == "once":
        run_once(armed=args.arm)
    elif args.command == "loop":
        run_loop(armed=args.arm, interval=args.interval)


if __name__ == "__main__":
    main()
