"""One-off: post a test comment on the logged-in user's most recent post.

Bypasses matcher/giveaway gates entirely — purpose is just to confirm that
authentication + POST /posts/{id}/comments works end-to-end against the user's
own account. Safe to delete after running.
"""
from __future__ import annotations

import sys

from api import AuthExpired, WatchlinkClient
from sniper import COOKIES_FILE, _creds, load_dotenv

# Discovered from a prior /sessions response: profile.user_id of austin@elcodev.com.
SELF_USER_ID = "2385f90a-1551-4124-a7dd-da4ae9f2ccde"
TEST_TEXT = "test — ignore"


def main() -> int:
    load_dotenv()
    email, password = _creds()

    with WatchlinkClient(COOKIES_FILE) as c:
        # Re-use existing session cookie; only fall back to /sessions if dead.
        try:
            c.refresh()
        except AuthExpired:
            if not email or not password:
                print("session dead and no creds in env", file=sys.stderr)
                return 1
            c.login(email, password)
            c.refresh()

        posts = c.user_posts(SELF_USER_ID, page=1, per_page=5)
        if not posts:
            print("user has no published posts to comment on", file=sys.stderr)
            return 3

        target = posts[0]
        post_id = target.get("id")
        title = target.get("title") or (target.get("text") or "")[:80]
        print(f"target post: id={post_id} preview={title!r}")

        # DISABLED — test confirmed the pipeline works on 2026-05-04. Re-enable
        # only if you need to re-verify; otherwise this script is inert.
        # result = c.post_comment(post_id, TEST_TEXT)
        # comment_id = (result.get("data") or result).get("id") if isinstance(result, dict) else None
        # print(f"OK — posted comment id={comment_id} text={TEST_TEXT!r} on post {post_id}")
        print(f"DRY-RUN: would have posted {TEST_TEXT!r} on post {post_id} (post_comment disabled)")
        return 0


if __name__ == "__main__":
    sys.exit(main())
