"""Inspect Daniel's recent posts raw — figure out why we missed the SSK033."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from api import WatchlinkClient, comments_count, extract_text
from matcher import find_giveaway_word, is_free_giveaway_post
from sniper import COOKIES_FILE, DANIEL_USER_ID, _creds, load_dotenv


def main() -> int:
    load_dotenv()
    email, password = _creds()
    with WatchlinkClient(COOKIES_FILE) as client:
        client.ensure_authed(email, password)
        posts = client.user_posts(DANIEL_USER_ID, page=1, per_page=10)

    print(f"Got {len(posts)} posts.\n")
    for p in posts:
        pid = p.get("id")
        text = (extract_text(p) or "").replace("\n", " ")[:150]
        cc = comments_count(p)
        full_text = extract_text(p) or ""
        word = find_giveaway_word(full_text)
        free = is_free_giveaway_post(p, full_text)

        # All keys at top level
        keys = sorted(p.keys())
        # Anything that smells like price/money
        money_like = {k: p.get(k) for k in keys if "price" in k.lower() or "amount" in k.lower() or "cost" in k.lower() or "free" in k.lower()}

        print(f"=== post {pid} ===")
        print(f"  preview:     {text!r}")
        print(f"  comments:    {cc}")
        print(f"  matcher:     word={word!r}  free={free}")
        print(f"  top keys:    {keys}")
        print(f"  money-like:  {money_like}")
        print(f"  full json:")
        print(json.dumps(p, indent=4, default=str)[:2000])
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
