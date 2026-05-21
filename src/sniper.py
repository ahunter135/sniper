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
import random
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
from solver import classify_giveaway, solve_giveaway
# is_actionable_by_bot is kept in solver.py for the test suite, but the
# bot's hot path no longer calls it — rule-check is now folded into
# classify_giveaway via the regex prefilter + prompt's NO list.

BASE = Path(__file__).resolve().parent.parent

DANIEL_USER_ID = "8493630a-4f84-4eab-9e20-b68f255f02ac"
# Daniel's posts already surface via /users/{id}/posts; the buy_sell feed scan
# was duplicate work that doubled per-tick latency. Set to ["buy_sell"] (or any
# category) only if you want broader feed coverage at the cost of speed.
FEED_CATEGORIES: list[str] = []

ENV_FILE = BASE / ".env"
COOKIES_FILE = BASE / "state" / "cookies.json"
STATE_FILE = BASE / "state" / "seen.json"
# Log file location. Honored env override lets tests (tests/bench_latency.py)
# redirect to /tmp so test runs don't pollute the live bot's log.
LOG_FILE = Path(os.environ.get("SNIPER_LOG_FILE") or (BASE / "logs" / "sniper.log"))

MAX_POSTS_PER_SCAN = 20

# Optional watchlist filter. When non-empty, the bot only posts on a matched
# post if ALL of these keywords appear (case-insensitive substring match) in
# the post text or brand metadata. Empty list = no filter (current behavior).
# Set at startup from --filter.
_FILTER_KEYWORDS: list[str] = []

# Optional pre-POST delay (seconds). When both > 0, the bot sleeps
# random.uniform(min, max) between the rules-check and the fresh-count gate.
# Two effects:
#   1. Bot looks like a fast human (4-9s response) instead of a 1.5s machine.
#   2. If a real person comments during the delay, fresh-count > 0 triggers
#      the existing "too late" skip — bot quietly bows out instead of
#      stomping on a thread where it lost the race a beat earlier.
# Set at startup from --post-delay-min / --post-delay-max.
_POST_DELAY_MIN: float = 0.0
_POST_DELAY_MAX: float = 0.0

# When True, the user has affirmed they've completed any earlier off-bot
# prerequisites Daniel required (e.g., "post on the app today before the
# drop"). Disables the red-flag regex screen AND instructs the classifier
# to treat past-tense prerequisites as satisfied. Set via --prereqs-done.
# Default False — bot stays conservative; user has to opt into the trust.
_PREREQS_DONE: bool = False

# Allowed watch conditions from the API's `condition` field. Posts whose
# condition is set and not in this set are skipped before the LLM is
# called. Posts with no condition field (or condition=None) are assumed
# to be new and proceed normally. Set via --allow-quality.
_ALLOWED_QUALITIES: set[str] = {"new", "like_new"}


def _normalize_quality(raw: object) -> str | None:
    """Lowercase + space→underscore. None / empty / non-string → None."""
    if not isinstance(raw, str):
        return None
    s = raw.strip().lower().replace(" ", "_").replace("-", "_")
    return s or None


def _post_meets_quality_bar(post: dict) -> tuple[bool, str | None]:
    """Returns (passes, normalized_condition).

    Posts with no `condition` field (or condition=None / empty) are
    assumed to be new and pass. Posts with an explicit condition must
    match _ALLOWED_QUALITIES (case-insensitive after normalization).
    """
    cond = _normalize_quality(post.get("condition"))
    if cond is None:
        return True, None  # assume new
    return cond in _ALLOWED_QUALITIES, cond

# Cap on classify_giveaway() calls per scan tick. The first scan after startup
# wants to LLM-classify every historical Daniel post (~20 of them) — without a
# cap that's an instant 429 cascade that stalls the bot for minutes. With this
# cap the bot amortizes catch-up over multiple ticks: at 5s interval and 1
# call/tick that's 12 classifications/min, well under Anthropic's free-tier
# limit. New posts at the top of the feed get classified first.
_LLM_CALLS_PER_TICK = 1
_llm_calls_this_tick = 0

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
    defaults = {
        "commented": {},
        "matched": {},
        "skipped_had_comments": {},
        "skipped_by_rules": {},
        "llm_no_giveaway": {},
        "skipped_by_filter": {},
        "skipped_by_quality": {},
    }
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


def _post_matches_filter(post: dict, text: str | None,
                         watch_line: str | None = None) -> bool:
    """True if all _FILTER_KEYWORDS appear (case-insensitive) somewhere in
    the post text, brand metadata, OR the LLM-identified watch line.

    `watch_line` lets `--filter presage` match a post whose body only shows
    a reference number ("SRPJ13") — the LLM augments the haystack with
    canonical model names from its watch-catalog knowledge.
    """
    if not _FILTER_KEYWORDS:
        return True
    haystack = (text or "").lower()
    brand = post.get("brand")
    if isinstance(brand, dict):
        haystack += " " + str(brand.get("name") or "").lower()
    elif isinstance(brand, str):
        haystack += " " + brand.lower()
    if watch_line:
        haystack += " " + watch_line.lower()
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
    # Already-blocked by the LLM rules check. Don't burn API calls re-asking.
    if post_id in state.get("skipped_by_rules", {}):
        return
    # LLM has already decided this isn't a giveaway. Skip without calling
    # the LLM again. Remove from seen.json to re-evaluate.
    if post_id in state.get("llm_no_giveaway", {}):
        return
    # Previously rejected by the --filter gate. We only short-circuit if the
    # filter keywords haven't changed since the rejection was persisted; a new
    # filter means the post deserves re-evaluation.
    prior_filter = state.get("skipped_by_filter", {}).get(post_id)
    if prior_filter is not None:
        prior_keywords = sorted(prior_filter.get("filter_at_time") or [])
        if prior_keywords == sorted(_FILTER_KEYWORDS):
            return
    # Previously rejected by quality bar. Same logic — only honour the
    # persisted skip if the allowed-quality set still matches.
    prior_q = state.get("skipped_by_quality", {}).get(post_id)
    if prior_q is not None:
        prior_allowed = set(prior_q.get("allowed") or [])
        if prior_allowed == _ALLOWED_QUALITIES:
            return

    # Quality gate (deterministic, before any LLM call) — skip posts whose
    # condition field is set to something below the user's bar. Assume
    # condition=None means new. Persisted so we don't re-evaluate.
    quality_ok, post_condition = _post_meets_quality_bar(post)
    if not quality_ok:
        state["skipped_by_quality"][post_id] = {
            "condition": post_condition,
            "allowed": sorted(_ALLOWED_QUALITIES),
            "source": source,
            "ts": time.time(),
        }
        save_state(state)
        log.info(
            f"QUALITY-SKIP post={post_id} condition={post_condition!r} "
            f"not in allowed={sorted(_ALLOWED_QUALITIES)} — skip"
        )
        return

    text = extract_text(post)
    is_daniel = _is_daniel_post(post, source)

    # Local fast-path matchers (sub-ms each). The LLM is the primary
    # decision-maker, but these run first as a backup so the bot still
    # functions if the LLM is unavailable (no API key, rate-limited, etc.)
    # and as a high-precision fallback for the quoted-word case.
    local_word: str | None = None
    if text:
        local_word = find_giveaway_word(text)
        has_daily_deal_header = bool(DAILY_DEAL_HEADER.search(text))
        if not local_word and is_daniel and has_daily_deal_header:
            answer = solve_giveaway(text)
            if answer:
                local_word = answer
        if not local_word and is_daniel and is_free_giveaway_post(post, text):
            local_word = DEFAULT_FREE_DEAL_WORD

    # Primary classifier: one Claude call per uncached Daniel post that
    # decides (a) is this a giveaway, (b) watch / merch / other, (c) what
    # comment to post. Persisted skips (commented/llm_no_giveaway/etc.) at
    # the top of this function mean each post pays the LLM cost at most once.
    # Per-tick budget prevents the startup catch-up from triggering a 429
    # cascade — when exhausted, posts fall through to local-only and get a
    # proper LLM verdict on a later tick.
    global _llm_calls_this_tick
    word: str | None = None
    kind = "unknown"
    watch_line: str | None = None
    if is_daniel and text:
        if _llm_calls_this_tick >= _LLM_CALLS_PER_TICK:
            # Defer LLM classification to a future tick. Don't persist —
            # this post will come back through the pipeline next scan.
            word = local_word
            kind = "unknown"
        else:
            _llm_calls_this_tick += 1
            llm_comment, llm_kind, status, llm_line = classify_giveaway(
                text, prereqs_done=_PREREQS_DONE,
            )
            if status == "no":
                state["llm_no_giveaway"][post_id] = {
                    "source": source,
                    "ts": time.time(),
                    "preview": text[:140],
                }
                save_state(state)
                return
            if status == "yes":
                kind = llm_kind
                watch_line = llm_line if kind == "watch" else None
                # Prefer the local matcher's word when it found one — it's
                # exact-text-match precise. LLM word is the fallback.
                word = local_word or llm_comment
                log.info(
                    f"LLM-CLASSIFY post={post_id} kind={kind} "
                    f"comment={(llm_comment or '')!r} local={local_word!r} "
                    f"line={watch_line!r}"
                )
            else:  # status == "error"
                # LLM unavailable. Fall back to local matchers.
                word = local_word
                log.info(
                    f"LLM-ERROR post={post_id} — falling back to local "
                    f"word={local_word!r}"
                )
    else:
        word = local_word

    if not word:
        return

    cached_count = comments_count(post)
    log.info(
        f"MATCH source={source} post={post_id} word={word!r} kind={kind} "
        f"cached_comments={cached_count} preview={text[:140]!r}"
    )

    # Watchlist gate: --filter only applies to watch giveaways. Merch and
    # other kinds are always allowed past the filter (cheap prizes — sniping
    # everything is the point). "unknown" (LLM was unavailable / deferred)
    # is treated like "watch" for safety so a stale LLM doesn't bypass the
    # user's watchlist preference.
    filter_relevant = kind in ("watch", "unknown")
    if (
        _FILTER_KEYWORDS and filter_relevant
        and not _post_matches_filter(post, text, watch_line=watch_line)
    ):
        # Only persist when we had a confirmed kind. With kind="unknown"
        # we'd lock in a stale rejection on a post we never properly
        # classified — better to retry next tick when budget permits.
        if kind != "unknown":
            state["skipped_by_filter"][post_id] = {
                "filter_at_time": sorted(_FILTER_KEYWORDS),
                "kind": kind,
                "word": word,
                "line": watch_line,
                "ts": time.time(),
            }
            save_state(state)
        log.info(
            f"FILTERED post={post_id} kind={kind} line={watch_line!r} "
            f"keywords {_FILTER_KEYWORDS!r} not all present — skip"
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

    # Rules-check is now layered into the classifier itself:
    #   Layer 1: has_off_bot_requirement(text) — deterministic regex,
    #            already ran INSIDE classify_giveaway above and would have
    #            returned status="no" if any red-flag phrase matched.
    #   Layer 2: classify_giveaway prompt — explicit NO list covering
    #            follow / repost / tag / invite / send-proof / before-
    #            commenting / "post on the app" patterns.
    # Dropping the separate is_actionable_by_bot() LLM call here halves
    # the per-match LLM budget (was 2 calls per match, now 1) so we
    # stay under Anthropic's 5 RPM free-tier cap during a real race.

    if not armed:
        log.info(f"DRY-RUN: would comment {word!r} on post {post_id}")
        return

    # Optional pre-POST delay. Makes the bot look like a fast human (4-9s
    # response) rather than a 1.5s machine. Side effect: if a real person
    # comments during the sleep, the fresh-count gate below will skip the
    # POST — we'd rather lose the race quietly than be the obvious bot.
    if _POST_DELAY_MAX > 0 and _POST_DELAY_MIN < _POST_DELAY_MAX:
        delay = random.uniform(_POST_DELAY_MIN, _POST_DELAY_MAX)
        log.info(f"DELAY post={post_id} sleeping {delay:.2f}s before POST")
        time.sleep(delay)
    elif _POST_DELAY_MAX > 0:
        # Min == max → fixed delay (e.g., --post-delay-min=5 --post-delay-max=5)
        log.info(f"DELAY post={post_id} sleeping {_POST_DELAY_MAX:.2f}s before POST")
        time.sleep(_POST_DELAY_MAX)

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
    global _llm_calls_this_tick
    _llm_calls_this_tick = 0
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

        if _POST_DELAY_MAX > 0:
            delay_str = f"{_POST_DELAY_MIN:.1f}-{_POST_DELAY_MAX:.1f}s"
        else:
            delay_str = "off"
        log.info(
            f"loop armed={armed} interval={interval}s "
            f"filter={_FILTER_KEYWORDS or 'off'} "
            f"quality={sorted(_ALLOWED_QUALITIES)} "
            f"post-delay={delay_str} "
            f"rules-safety={'classifier-only (prereqs-done!)' if _PREREQS_DONE else 'regex+classifier'}"
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
    ap.add_argument("--post-delay-min", type=float, default=0.0,
                    help='Lower bound on random pre-POST sleep, in seconds. '
                         'Default 0 (no delay). Combined with --post-delay-max '
                         'to disguise bot speed as a fast-human response time.')
    ap.add_argument("--post-delay-max", type=float, default=0.0,
                    help='Upper bound on random pre-POST sleep, in seconds. '
                         'Default 0 (no delay). If the bot is too obviously '
                         'first, try --post-delay-min 4 --post-delay-max 9.')
    ap.add_argument("--prereqs-done", action="store_true",
                    help='Affirm that you have manually completed any '
                         'off-bot prerequisites Daniel posted earlier '
                         '(e.g., "post on the app today"). Disables the '
                         'red-flag regex screen and tells the LLM to treat '
                         'past-tense conditions as satisfied. USE WITH CARE '
                         'on days you have actually done the prereq.')
    ap.add_argument("--allow-quality", type=str, default="new,like_new",
                    help='Comma-separated list of acceptable values for '
                         'the post `condition` field (case-insensitive). '
                         'Default "new,like_new". Posts with condition '
                         'outside this set are skipped before any LLM '
                         'call. Posts with no condition field are assumed '
                         'new and pass.')
    args = ap.parse_args()

    global _FILTER_KEYWORDS, _POST_DELAY_MIN, _POST_DELAY_MAX, _PREREQS_DONE
    global _ALLOWED_QUALITIES
    _FILTER_KEYWORDS = (args.filter or "").split()
    _POST_DELAY_MIN = max(0.0, args.post_delay_min)
    _POST_DELAY_MAX = max(_POST_DELAY_MIN, args.post_delay_max)
    _PREREQS_DONE = args.prereqs_done
    _ALLOWED_QUALITIES = {
        q for q in (
            _normalize_quality(part) for part in args.allow_quality.split(",")
        )
        if q
    }

    if args.command == "login":
        force_login()
    elif args.command == "once":
        run_once(armed=args.arm)
    elif args.command == "loop":
        run_loop(armed=args.arm, interval=args.interval)


if __name__ == "__main__":
    main()
