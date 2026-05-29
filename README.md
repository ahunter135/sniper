# watchlink-sniper

Polls watchlink.co in a tight loop and races to be the first comment on Daniel
Cheek's giveaway posts. The default production cadence is **2 seconds** (set in
`run.sh`); pushing slower than that loses to push-notification users.

Each tick scans Daniel's profile posts (`GET /users/{uuid}/posts`). The global
buy/sell feed scan is disabled by default — it doubled per-tick latency for
zero added coverage, since Daniel's giveaways show up on his profile first.
Re-enable by setting `FEED_CATEGORIES` in `src/sniper.py`.

When a post looks like a giveaway the bot can win (right pattern, zero
comments, no off-bot prerequisites), it posts the required word as a comment.
See `API.md` for the reverse-engineered endpoint reference.

Direct API calls to `api.watchlink.co` — no headless browser.

**Windows user?** Follow `README-WINDOWS.md` instead — it's a step-by-step
zero-terminal-experience guide.

## Safety gates (in order, all must pass to post)

1. **Quality bar** — post's `condition` field must be `new` or `like_new`
   (or absent, treated as new). Configurable via `--allow-quality`.
2. **Off-bot red-flag screen** — deterministic regex that rejects posts
   requiring tag-friends, follow, repost, "post on the app", send-proof,
   "before commenting", etc. Runs without the LLM so it still fires when
   the LLM is rate-limited.
3. **LLM classifier** — one Claude call per uncached Daniel post decides
   (a) is this a giveaway, (b) watch / merch / other, (c) what comment to
   post. Folds in the rules check as a NO list. Skipped without a key set.
4. **Watchlist filter** (optional) — when `--filter "seiko presage"` is
   set, only `watch` kind giveaways whose body or LLM-resolved model name
   contains every keyword get past this gate.
5. **Cached comment count is 0** — if the feed payload shows any comments,
   skip.
6. **Fresh comment count is 0** — a second `GET /posts/{id}/comments?per_page=1`
   immediately before posting, so we don't race into a non-empty thread.
7. **Not already in `state/seen.json` under `commented`** — never double-post.

Local fast-path matchers (regex + quoted-word extractor + arithmetic solver in
`matcher.py` / `solver.py`) run alongside the LLM so the bot still functions
without an API key — they cover the classic "first to comment 'X'" pattern
and Daniel's `$0` daily-deal listings (default word `sold`).

## One-time setup

```bash
cd /Users/austinhunter/dev/watchlink-sniper
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
chmod 600 .env
# fill in WATCHLINK_EMAIL / WATCHLINK_PASSWORD
# optional: ANTHROPIC_API_KEY for the LLM classifier + riddle solver

python src/sniper.py login     # validates creds, writes state/cookies.json
```

`state/cookies.json` is the session jar; `.env` is never logged.

### Optional environment overrides

- `ANTHROPIC_API_KEY` — enables the LLM classifier (rules check + watch /
  merch / other kind + watch-line resolution) and the riddle fallback.
  Without it the bot falls back to local regex matching only.
- `SNIPER_LLM_MODEL` — defaults to `claude-haiku-4-5-20251001`. Override
  with `claude-sonnet-4-6` or `claude-opus-4-7` for harder questions at
  the cost of latency.
- `SNIPER_LLM_MIN_INTERVAL` — seconds between Claude calls (default 15s,
  sized for the Anthropic free tier's 5 RPM).
- `SNIPER_LOG_FILE` — redirect the log file (used by `tests/bench_latency.py`).

## Manual test

```bash
python src/sniper.py once            # dry-run: log matches, don't post
python src/sniper.py once --arm      # post for real
```

Matches and skip reasons are persisted in `state/seen.json` so runs are
auditable and idempotent. State buckets: `commented`, `matched`,
`skipped_had_comments`, `skipped_by_rules`, `skipped_by_filter`,
`skipped_by_quality`, `llm_no_giveaway`.

## Run as a long-lived daemon (launchd)

```bash
cp com.austin.watchlink-sniper.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.austin.watchlink-sniper.plist

# watch it work
tail -f logs/sniper.log

# stop
launchctl unload ~/Library/LaunchAgents/com.austin.watchlink-sniper.plist
```

The plist uses `KeepAlive=true` and `RunAtLoad=true`. It launches `run.sh`,
which exec's `python src/sniper.py loop --arm --interval 2`. If the loop
exits for any reason, launchd restarts it (throttled to once per 10s).
Edit `run.sh` → `ARM=0` for a continuous dry-run, then reload the plist.

## CLI flags (sniper.py loop / once)

- `--arm` — actually POST comments (default: dry-run, log only).
- `--interval N` — seconds between scan ticks in `loop` mode (default 300,
  but `run.sh` uses 2 for production).
- `--filter "seiko presage"` — only post on `watch`-kind giveaways whose
  body, brand metadata, or LLM-resolved watch line contains **all** of these
  space-separated keywords (case-insensitive, order-independent). Merch /
  other giveaways bypass the filter.
- `--post-delay-min S` / `--post-delay-max S` — sleep
  `random.uniform(min, max)` seconds between the rules check and the
  fresh-count gate. Disguises the bot as a fast human and quietly bows out
  if a real person comments during the delay. Defaults: 0 (off). Try
  `--post-delay-min 4 --post-delay-max 9` if you're being too obviously first.
- `--prereqs-done` — affirm that you have manually completed any off-bot
  prerequisites Daniel posted earlier (e.g., "post on the app today before
  the drop"). Disables the red-flag regex screen for past-tense conditions
  and tells the LLM to treat them as satisfied. Use carefully.
- `--allow-quality "new,like_new"` — comma-separated acceptable values for
  the post `condition` field (case-insensitive, hyphens/spaces normalized to
  underscore). Posts outside this set are skipped pre-LLM. Posts with no
  condition field are assumed new.

## How matching works

The LLM classifier is the primary decision-maker, but the local matchers in
`src/matcher.py` and `src/solver.py` cover the deterministic cases and act
as a fallback when the LLM is unavailable:

- `matcher.find_giveaway_word` — any short quoted word (`"me"`, `'sold'`,
  smart quotes) whose ±90-char context mentions "first" plus an action verb
  (*comment / say / reply / type / write / post / drop / leave / dm*).
- `matcher.is_free_giveaway_post` — Daniel's `$0` daily-deal listings get
  the default word `sold` (community convention).
- `solver.solve_arithmetic` — Python-`ast`-evaluated BODMAS arithmetic with
  `x` / `×` / `÷` aliases.
- `solver.solve_with_llm` — Claude fallback for riddles and word problems.

Supported phrasings live in `src/test_matcher.py`:

```bash
cd src && python test_matcher.py
```

## Notes

- Session cookies rotate; the client re-saves the jar after every request.
- If `/refresh` returns 401, the client auto-logs in again using `.env`
  creds — so the long-lived loop is self-healing.
- The bot caps LLM classifications at 1 per tick. After a fresh start it
  amortizes catch-up over multiple ticks rather than burning the rate
  budget in one cascade. New posts at the top of the feed get classified
  first.
- To adjust scan surfaces, edit `DANIEL_USER_ID` / `FEED_CATEGORIES` in
  `src/sniper.py`.
