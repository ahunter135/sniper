# watchlink-sniper — Windows setup

A bot that watches watchlink.co and races to be the first comment on Daniel
Cheek's giveaway posts. It polls every 2 seconds, and the moment Daniel posts
something like *"first to comment 'sold' gets it"*, it drops the word under your
account before anyone else can.

This guide assumes you have **never opened a terminal before**. Follow it top to
bottom. Every command goes into the same window. Do not skip steps.

---

## What you'll end up with

- A black window (PowerShell) sitting open on your desktop, quietly polling
  Watchlink every 2 seconds under your account.
- Logs scrolling so you can see it working.
- If a giveaway hits, the bot comments before you could blink.

You keep the window open while you're "playing." Close it to stop.

---

## 1. Install Python

1. Go to <https://www.python.org/downloads/windows/>.
2. Click the big yellow **"Download Python 3.12.x"** button (any 3.11 / 3.12 / 3.13 is fine).
3. Run the installer.
4. **CRITICAL — on the very first screen, tick the box that says
   "Add python.exe to PATH"** before clicking Install. If you miss this, nothing
   else in this guide will work. Uninstall and reinstall if you forget.
5. Click **Install Now** and let it finish.

Verify it worked: press the **Windows key**, type `powershell`, hit Enter. In
the blue window that opens, type:

```powershell
python --version
```

You should see something like `Python 3.12.5`. If you see "python is not
recognized," you forgot the PATH checkbox — uninstall Python and redo step 4.

---

## 2. Install Git

1. Go to <https://git-scm.com/download/win>.
2. The download starts automatically. Run the installer.
3. Click **Next** on every screen — the defaults are fine.

Verify: in PowerShell, type:

```powershell
git --version
```

You should see something like `git version 2.45.x`.

---

## 3. Clone the project

In PowerShell, run these three commands one at a time (paste, Enter, wait):

```powershell
cd $HOME\Documents
git clone https://github.com/<austin's-github-username>/watchlink-sniper.git
cd watchlink-sniper
```

Replace `<austin's-github-username>` with whatever I sent you in the repo link.

You are now "inside" the project folder. The prompt should end with
`watchlink-sniper>`. Every command from here on assumes you're in this folder.

---

## 4. Create the Python environment

Still in PowerShell, in the `watchlink-sniper` folder:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
```

If the second command errors with something about **"execution policy"**, run
this once, then re-try the `Activate.ps1` line:

```powershell
Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned
```

Say **Y** when it asks.

Once active, your prompt will have `(.venv)` at the front. Now install the one
dependency:

```powershell
pip install -r requirements.txt
```

---

## 5. Add your Watchlink credentials

You need a Watchlink account (the same email + password you use to log into the
app). The bot will post comments **as you**, so use your real account.

In the `watchlink-sniper` folder, there's a file called `.env.example`. Copy it
to `.env`:

```powershell
copy .env.example .env
notepad .env
```

Notepad opens. You'll see:

```
WATCHLINK_EMAIL=you@example.com
WATCHLINK_PASSWORD=your-password-here
```

Replace those two values with your actual Watchlink email and password. Save
(Ctrl+S) and close Notepad.

> Your password sits on your own machine in `.env`. It is in `.gitignore`, so it
> never gets pushed back to GitHub. Don't share screenshots of this file.

---

## 6. Log in once

This proves your credentials work and saves a session cookie so future runs
don't need to log in every time.

```powershell
python src\sniper.py login
```

You should see a line ending in `login OK`. If you see an auth error, your
email/password in `.env` is wrong — re-open `notepad .env` and fix it.

---

## 7. Dry-run test (no comments posted)

Before letting it loose, do one safe pass that will **log matches but not
actually post**:

```powershell
python src\sniper.py once
```

It scans Daniel's recent posts, prints what it would have done, and exits. No
comments get posted. If you see lines like `scan complete` with no errors,
you're good.

---

## 8. Go live

This is the real thing. It will poll every 2 seconds and comment for real the
moment it finds a giveaway with zero comments.

```powershell
python src\sniper.py loop --arm --interval 2
```

You'll see a line every couple of seconds. Leave this window open. Minimize it
if you want — just don't close it.

**To stop:** click the PowerShell window and press **Ctrl+C**, or just close the
window.

**To start again later:** open PowerShell, then:

```powershell
cd $HOME\Documents\watchlink-sniper
.venv\Scripts\Activate.ps1
python src\sniper.py loop --arm --interval 2
```

---

## Safety gates (why it won't embarrass you)

Before it ever posts a comment, **all four** of these must be true:

1. The post text matches a "first to comment / say / reply '<word>'" pattern.
2. The post's comment count in the feed is 0.
3. A fresh check immediately before posting still shows 0 comments.
4. The bot has never posted on this post before (tracked in
   `state\seen.json`).

If any one fails, it skips and moves on. It will never double-post and it will
never jump into a thread that already has comments.

There's also a default for Daniel's "Free Seiko Daily Deal" series: when he
posts a $0 listing without explicit instructions, the bot comments `"sold"` —
the community convention for his daily deals.

---

## Troubleshooting

**"python is not recognized"** — You skipped the "Add to PATH" checkbox in
step 1. Uninstall Python, reinstall, and tick the box.

**"cannot be loaded because running scripts is disabled"** when activating the
venv — run the `Set-ExecutionPolicy` command from step 4.

**Login errors** — your `.env` has the wrong email or password. Open it with
`notepad .env`, fix it, then re-run `python src\sniper.py login`.

**Bot is running but never posts** — that's expected most of the time. Daniel
only drops giveaways occasionally. Check `logs\sniper.log` to confirm it's
scanning:

```powershell
Get-Content logs\sniper.log -Wait -Tail 20
```

That'll stream the log live (Ctrl+C to stop watching).

**Computer goes to sleep** — the bot stops while your PC sleeps. Either set
your power plan to "never sleep" while you're playing, or accept that you'll
miss anything during sleep.

---

## Files at a glance

- `.env` — your credentials. **Never share or commit this.**
- `state\cookies.json` — saved login session.
- `state\seen.json` — what the bot has seen and acted on. Safe to delete; it
  will rebuild.
- `logs\sniper.log` — running log of everything the bot has done.

Good luck. Don't comment "sold" on anything yourself while the bot is running —
you'll race against your own bot and one of you will lose.
