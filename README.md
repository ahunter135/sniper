# watchlink-sniper

Polls watchlink.co every 5 minutes. Scans two surfaces each run:

1. Daniel Cheek's profile posts (`GET /users/{uuid}/posts`)
2. The global Buy/Sell feed (`GET /feed?category=buy_sell`)

When a post looks like a "first to comment X gets it" giveaway *and has zero
comments*, it (optionally) drops the requested word as a comment under your
account. See `API.md` for the reverse-engineered endpoint reference.

Direct API calls to `api.watchlink.co` — no headless browser per run.

## Safety gates (in order, all must pass to post)

1. **Text match** — `matcher.py` finds a "first to comment/say/reply/… '<word>'" pattern.
2. **Cached comment count is 0** — if the feed payload shows any comments, skip.
3. **Fresh comment count is 0** — a second `GET /posts/{id}/comments?per_page=1`
   immediately before posting, so we don't race into a non-empty thread.
4. **Not already in `state/seen.json` under `commented`** — never double-post.

## One-time setup

```bash
cd /Users/austinhunter/dev/watchlink-sniper
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
chmod 600 .env
# fill in WATCHLINK_EMAIL / WATCHLINK_PASSWORD

python src/sniper.py login     # validates creds, writes state/cookies.json
```

`state/cookies.json` is the session jar; `.env` is never logged.

## Manual test

```bash
python src/sniper.py once            # dry-run: log matches, don't post
python src/sniper.py once --arm      # post for real
```

Matches and skip reasons are persisted in `state/seen.json` so runs are
auditable and idempotent.

## Run on a 5-minute schedule (launchd)

```bash
cp com.austin.watchlink-sniper.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.austin.watchlink-sniper.plist

# watch it work
tail -f logs/sniper.log

# stop
launchctl unload ~/Library/LaunchAgents/com.austin.watchlink-sniper.plist
```

Edit `run.sh` → `ARM=1` to enable real posting, then reload the plist.

## How matching works

`src/matcher.py` finds any short quoted word (`"me"`, `'sold'`, smart quotes
included) whose ±90-char context mentions "first" plus an action verb like
*comment / say / reply / type / write / post / drop / leave / dm*. Supported
phrasings are covered in `src/test_matcher.py`:

```bash
cd src && python test_matcher.py
```

## Notes

- Session cookies rotate; the client re-saves the jar after every request.
- If `/refresh` returns 401, the client auto-logs in again using `.env` creds —
  so the scheduled job is self-healing.
- To adjust scan surfaces, edit `DANIEL_USER_ID` / `FEED_CATEGORIES` in
  `src/sniper.py`.
