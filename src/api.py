"""Minimal httpx client for the watchlink.co API.

Reverse-engineered from the public Vite bundle:
    https://watchlink.co/assets/index-*.js

Auth model:
- Session cookie set on api.watchlink.co (HttpOnly, scoped to that host).
- POST /sessions with {session: {login, password}} → returns {access_token, csrf_token, profile}
  and sets the session cookie.
- POST /refresh (with existing cookie) mints a fresh CSRF token.
- Mutations require `X-CSRF-Token` header + the session cookie.

Cookies are persisted in `state/cookies.json` between runs and rewritten
after every request so rotations survive.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

import httpx

BASE_URL = "https://api.watchlink.co/api/v1"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

log = logging.getLogger("sniper.api")


class AuthExpired(Exception):
    """Raised when auth fails and credentials can't recover it."""


class WatchlinkClient:
    def __init__(self, cookies_path: Path, timeout: float = 15.0) -> None:
        self.cookies_path = cookies_path
        self.csrf_token: str | None = None
        self._client = httpx.Client(
            base_url=BASE_URL,
            timeout=timeout,
            headers={
                "User-Agent": DEFAULT_USER_AGENT,
                "Accept": "application/json",
                "Origin": "https://watchlink.co",
                "Referer": "https://watchlink.co/",
            },
            follow_redirects=True,
        )
        self._load_cookies()

    # ---------- cookie jar persistence ----------

    def _load_cookies(self) -> None:
        if not self.cookies_path.exists():
            return
        data = json.loads(self.cookies_path.read_text())
        for c in data.get("cookies", []):
            self._client.cookies.set(
                name=c["name"],
                value=c["value"],
                domain=c.get("domain") or "",
                path=c.get("path") or "/",
            )
        self.csrf_token = data.get("csrf_token")

    def _save_cookies(self) -> None:
        cookies_out = [
            {"name": c.name, "value": c.value, "domain": c.domain, "path": c.path}
            for c in self._client.cookies.jar
        ]
        self.cookies_path.write_text(json.dumps({
            "cookies": cookies_out,
            "csrf_token": self.csrf_token,
            "saved_at": time.time(),
        }, indent=2, sort_keys=True))

    # ---------- session ----------

    def login(self, email: str, password: str) -> None:
        """POST /sessions with credentials. Sets session cookie + csrf_token."""
        resp = self._client.post(
            "/sessions",
            headers={"Content-Type": "application/json"},
            json={"session": {"login": email, "password": password}},
        )
        if resp.status_code == 401:
            raise AuthExpired("login failed: invalid credentials (401)")
        if resp.status_code == 403:
            raise AuthExpired("login failed: forbidden (403) — terms or account status?")
        resp.raise_for_status()
        body = resp.json()
        self.csrf_token = body.get("csrf_token") or self.csrf_token
        self._save_cookies()
        log.info("login ok")

    def refresh(self) -> None:
        """Call /refresh to mint a fresh CSRF token. Requires valid session cookie."""
        headers = {"Content-Type": "application/json"}
        if self.csrf_token:
            headers["X-CSRF-Token"] = self.csrf_token
        resp = self._client.post("/refresh", headers=headers)
        if resp.status_code == 401:
            raise AuthExpired("session expired")
        resp.raise_for_status()
        body = resp.json()
        self.csrf_token = body.get("csrf_token") or self.csrf_token
        self._save_cookies()
        log.debug("csrf refreshed")

    def ensure_authed(self, email: str | None, password: str | None) -> None:
        """Try /refresh first; if session is dead, log in with env creds; raise if neither works."""
        try:
            self.refresh()
            return
        except AuthExpired:
            if not email or not password:
                raise
            log.info("session expired, re-logging in")
        self.login(email, password)
        # After login the server sets session cookie; refresh to confirm CSRF.
        self.refresh()

    # ---------- reads ----------

    def user_posts(self, user_id: str, page: int = 1, per_page: int = 20) -> list[dict]:
        resp = self._client.get(
            f"/users/{user_id}/posts",
            params={"status": "published", "per_page": per_page, "page": page},
        )
        resp.raise_for_status()
        self._save_cookies()
        return resp.json().get("data", [])

    def feed(self, category: str, page: int = 1, per_page: int = 20) -> list[dict]:
        resp = self._client.get(
            "/feed",
            params={"category": category, "per_page": per_page, "page": page},
        )
        resp.raise_for_status()
        self._save_cookies()
        return resp.json().get("data", [])

    def post_comments_count(self, post_id: int | str) -> int:
        """Freshest possible count of comments on a post. Used as the last-mile
        'still zero?' gate before we post."""
        resp = self._client.get(
            f"/posts/{post_id}/comments",
            params={"per_page": 1, "page": 1},
        )
        resp.raise_for_status()
        body = resp.json()
        pag = body.get("pagination") or {}
        count = pag.get("count")
        if isinstance(count, int):
            return count
        # Fallback: use data length if pagination.count is missing
        return len(body.get("data") or [])

    # ---------- writes ----------

    def post_comment(self, post_id: int | str, text: str,
                     parent_comment_id: int | None = None) -> dict[str, Any]:
        if not self.csrf_token:
            raise RuntimeError("no CSRF token — call refresh()/login() first")
        headers = {
            "Content-Type": "application/json",
            "X-CSRF-Token": self.csrf_token,
        }
        body = {"comment": {"text": text, "parent_comment_id": parent_comment_id}}
        resp = self._client.post(
            f"/posts/{post_id}/comments", headers=headers, json=body,
        )
        if resp.status_code == 401:
            raise AuthExpired("session expired mid-run")
        resp.raise_for_status()
        self._save_cookies()
        return resp.json()

    # ---------- lifecycle ----------

    def close(self) -> None:
        self._save_cookies()
        self._client.close()

    def __enter__(self) -> "WatchlinkClient":
        return self

    def __exit__(self, *_):
        self.close()


# ---------- post-text helpers ----------

def extract_text(post: dict) -> str:
    """Pull plain text out of a post dict. Permissive — API can return TipTap
    JSON, HTML, or a plain string across different payload shapes."""
    for key in ("text", "body", "caption", "description"):
        v = post.get(key)
        if isinstance(v, str) and v.strip():
            return v
    v = post.get("content")
    if isinstance(v, str) and v.strip():
        return v
    if isinstance(v, dict):
        return _flatten_tiptap(v)
    return ""


def _flatten_tiptap(node: Any) -> str:
    if isinstance(node, dict):
        if node.get("type") == "text" and isinstance(node.get("text"), str):
            return node["text"]
        return " ".join(_flatten_tiptap(c) for c in node.get("content", []) or [])
    if isinstance(node, list):
        return " ".join(_flatten_tiptap(c) for c in node)
    return ""


def comments_count(post: dict) -> int | None:
    """Read the comment count off a post dict if present.

    The API nests it under post["comments"]["comments_count"] in current
    payloads; older shapes had it at top level. Check both.
    """
    keys = ("comments_count", "root_comments_count", "comment_count")
    for key in keys:
        v = post.get(key)
        if isinstance(v, int) and not isinstance(v, bool):
            return v
    nested = post.get("comments")
    if isinstance(nested, dict):
        for key in keys:
            v = nested.get(key)
            if isinstance(v, int) and not isinstance(v, bool):
                return v
    return None
