"""Pre-giveaway preflight check. Read-only — does not post anything.

Run this right before Daniel drops a $0 listing to verify:
  - Session cookies are alive
  - CSRF token refreshed successfully (same token POST /comments will use)
  - Daniel's feed is reachable
  - Fresh comment-count endpoint responds correctly
  - State files are sane (no accidental dupes will fire)
  - Matcher would classify the latest Daniel post correctly

If /refresh succeeds and authed GETs return 200, the same session cookie +
CSRF token will be accepted by POST /posts/{id}/comments. So this preflight
proves the full auth chain without leaving any footprint on the platform.

Usage:
    .venv/bin/python src/preflight.py
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from api import AuthExpired, WatchlinkClient, comments_count, extract_text
from matcher import find_giveaway_word, is_free_giveaway_post
from sniper import (
    COOKIES_FILE,
    DANIEL_USER_ID,
    FEED_CATEGORIES,
    STATE_FILE,
    _creds,
    load_dotenv,
    load_state,
)


# ANSI styling
RESET = "\033[0m"
BOLD = "\033[1m"
GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
DIM = "\033[2m"

OK = f"{GREEN}✓{RESET}"
BAD = f"{RED}✗{RESET}"
WARN = f"{YELLOW}!{RESET}"


def ok(msg: str) -> None:
    print(f"  {OK} {msg}")


def bad(msg: str) -> None:
    print(f"  {BAD} {msg}")


def warn(msg: str) -> None:
    print(f"  {WARN} {msg}")


def info(msg: str) -> None:
    print(f"    {DIM}{msg}{RESET}")


def section(title: str) -> None:
    print(f"\n{BOLD}{title}{RESET}")


# Post IDs in production are in the low 4-digit range (~1000-1500 as of this
# writing). A 9-digit value is "impossibly far ahead" — guaranteed to not
# resolve to a real post for the purposes of probing the POST endpoint.
_PROBE_FAKE_POST_ID = 999_999_999


def probe_comment_endpoint(client: WatchlinkClient) -> tuple[bool, str]:
    """POST /comments with a fake post ID. Returns (auth_ok, message).

    Expected outcomes:
      404                    → post not found, auth + CSRF accepted (good)
      422                    → payload rejected after auth (good — auth ok)
      401                    → session dead (bad — re-login needed)
      403                    → CSRF rejected (bad)
      200/201                → unexpected success; investigate
    """
    body = {"comment": {"text": "x", "parent_comment_id": None}}
    headers = {
        "Content-Type": "application/json",
        "X-CSRF-Token": client.csrf_token or "",
    }
    try:
        resp = client._client.post(
            f"/posts/{_PROBE_FAKE_POST_ID}/comments",
            headers=headers,
            json=body,
        )
    except Exception as e:
        return False, f"network error: {e!r}"

    code = resp.status_code
    body_text = (resp.text or "")[:200]
    if code == 404:
        return True, f"HTTP 404 (post not found) — auth + CSRF accepted, no comment created"
    if code == 422:
        return True, f"HTTP 422 (validation) — auth + CSRF accepted, no comment created"
    if code == 401:
        return False, f"HTTP 401 — session dead. run: .venv/bin/python src/sniper.py login"
    if code == 403:
        return False, f"HTTP 403 — CSRF rejected: {body_text}"
    if code in (200, 201):
        return False, f"HTTP {code} — UNEXPECTED success on fake id; check post {_PROBE_FAKE_POST_ID}: {body_text}"
    return False, f"HTTP {code}: {body_text}"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="watchlink sniper preflight (read-only by default)")
    ap.add_argument(
        "--probe-post",
        action="store_true",
        help="actively probe POST /comments using a fake post ID (proves write-path auth without creating a comment). Generates one 404 entry in server logs.",
    )
    args = ap.parse_args(argv)
    print(f"{BOLD}WatchLink sniper preflight{RESET}  {DIM}{time.strftime('%Y-%m-%d %H:%M:%S')}{RESET}")
    failures = 0

    # ---------- env ----------
    section("Environment")
    load_dotenv()
    email, password = _creds()
    if email:
        ok(f"WATCHLINK_EMAIL = {email}")
    else:
        bad("WATCHLINK_EMAIL not set in .env")
        failures += 1
    if password:
        ok(f"WATCHLINK_PASSWORD set ({len(password)} chars)")
    else:
        bad("WATCHLINK_PASSWORD not set in .env")
        failures += 1

    # ---------- state files ----------
    section("State files")
    if COOKIES_FILE.exists():
        ok(f"cookies.json present ({COOKIES_FILE.stat().st_size} bytes)")
    else:
        warn("cookies.json missing — will login fresh from creds")

    if STATE_FILE.exists():
        state = load_state()
        commented = state.get("commented", {})
        matched = state.get("matched", {})
        skipped = state.get("skipped_had_comments", {})
        ok(f"seen.json present")
        info(f"commented={len(commented)}  matched={len(matched)}  skipped_late={len(skipped)}")
    else:
        warn("seen.json missing — first run, will be created")
        state = {"commented": {}}

    # ---------- auth ----------
    section("Auth")
    client = WatchlinkClient(COOKIES_FILE)
    try:
        client.ensure_authed(email, password)
        ok("POST /refresh → 200 (session alive)")
    except AuthExpired as e:
        bad(f"auth failed: {e}")
        info("→ run: .venv/bin/python src/sniper.py login")
        client.close()
        return 1
    except Exception as e:
        bad(f"auth crashed: {e!r}")
        client.close()
        return 1

    if client.csrf_token:
        ok(f"CSRF token present ({len(client.csrf_token)} chars)")
    else:
        bad("CSRF token empty after refresh")
        failures += 1

    # Group cookies by host so we can tell session cookies from analytics ones.
    by_domain: dict[str, list[str]] = {}
    for c in client._client.cookies.jar:
        by_domain.setdefault(c.domain, []).append(c.name)
    api_cookies = sorted({n for d, names in by_domain.items() if "api.watchlink.co" in d for n in names})
    auth_markers = {"jwt_access", "jwt_refresh", "_session_id"}
    auth_cookies = [n for n in api_cookies if n in auth_markers or "session" in n.lower()]
    if "jwt_refresh" in auth_cookies:
        ok(f"auth cookies on api.watchlink.co: {', '.join(auth_cookies)}")
    elif auth_cookies:
        warn(f"partial auth cookies on api.watchlink.co: {auth_cookies}")
    else:
        bad(f"no auth cookies found on api.watchlink.co. api cookies: {api_cookies}")
        failures += 1

    # ---------- Daniel's feed ----------
    section("Daniel's feed")
    try:
        posts = client.user_posts(DANIEL_USER_ID, page=1, per_page=5)
        ok(f"GET /users/{{Daniel}}/posts → {len(posts)} posts")
    except Exception as e:
        bad(f"Daniel's feed failed: {e!r}")
        client.close()
        return 1

    if not posts:
        bad("Daniel's feed returned 0 posts (unexpected)")
        client.close()
        return 1

    latest = posts[0]
    pid = latest.get("id")
    text = extract_text(latest) or ""
    cc = comments_count(latest)
    preview = text.replace("\n", " ")[:100]

    ok(f"latest post id={pid}")
    info(f"preview: {preview!r}")
    info(f"cached comments_count: {cc}")

    word = find_giveaway_word(text)
    free = is_free_giveaway_post(latest)
    if word:
        info(f"matcher: explicit word → {word!r}")
    elif free:
        info(f"matcher: $0 free-deal → would default to 'sold'")
    else:
        info("matcher: no giveaway pattern (regular post)")

    if str(pid) in state.get("commented", {}):
        warn(f"already commented on post {pid} → loop will skip it")

    # ---------- fresh-check endpoint (the last-mile gate) ----------
    section("Fresh-check endpoint")
    try:
        live_count = client.post_comments_count(pid)
        ok(f"GET /posts/{pid}/comments pagination.count = {live_count}")
        if cc is not None and abs(cc - live_count) > 3:
            warn(f"cached ({cc}) vs live ({live_count}) drifted — normal feed lag")
    except Exception as e:
        bad(f"fresh-check failed: {e!r}")
        failures += 1

    # ---------- write-path probe (opt-in) ----------
    if args.probe_post:
        section("Comment-endpoint probe (active)")
        info("POSTing to a fake post id to verify write-path auth without")
        info("creating a real comment. Expects 404 if everything is good.")
        write_ok, msg = probe_comment_endpoint(client)
        if write_ok:
            ok(msg)
        else:
            bad(msg)
            failures += 1

    # ---------- global feed ----------
    section("Global feed")
    for cat in FEED_CATEGORIES:
        try:
            feed = client.feed(cat, page=1, per_page=5)
            ok(f"GET /feed?category={cat} → {len(feed)} posts")
        except Exception as e:
            bad(f"feed '{cat}' failed: {e!r}")
            failures += 1

    # ---------- summary ----------
    section("Summary")
    if failures == 0:
        scope = "read + write paths" if args.probe_post else "read path"
        print(f"  {OK} {GREEN}All systems go.{RESET} {scope} verified.")
        if not args.probe_post:
            print(f"  {DIM}For write-path verification too: re-run with --probe-post{RESET}")
        print()
        print(f"  {DIM}Live (armed):{RESET}  .venv/bin/python src/sniper.py loop --arm --interval 30")
        print(f"  {DIM}Dry-run:     {RESET}  .venv/bin/python src/sniper.py once")
        print()
        print(f"  {YELLOW}Bot-detection notes:{RESET}")
        print(f"    • 30s interval is aggressive but defensible (real users F5 hot deals).")
        print(f"      Consider 45-60s if you want a safer cadence.")
        print(f"    • Don't run multiple sniper processes — duplicate POSTs would be obvious.")
        print(f"    • The fresh-check gate aborts if anyone beat you, so you won't")
        print(f"      double-comment on a post that already has activity.")
        rc = 0
    else:
        print(f"  {BAD} {RED}{failures} check(s) failed.{RESET} Do NOT arm until resolved.")
        rc = 1

    client.close()
    return rc


if __name__ == "__main__":
    sys.exit(main())
