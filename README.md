# Nutcracker ticket watcher 🎟

A Telegram bot that watches **The Nutcracker** at the Royal Opera House
(3 Dec 2026 – 13 Jan 2027) and messages you the moment returned seats appear in
**Orchestra Stalls rows A–G** or **Stalls Circle row A**.

It runs for free on GitHub, 24/7 — your phone and laptop can be off.

---

## 1. Create your Telegram bot (3 minutes)

1. In Telegram, open **@BotFather** → send `/newbot`.
2. Give it any name (e.g. *Nutcracker Watcher*) and a username ending in `bot`
   (e.g. `alina_nutcracker_bot`).
3. BotFather replies with a **token** like `7123456789:AAH...`. Copy it somewhere safe.
4. Tap the link to your new bot and press **Start** (otherwise it isn't allowed to message you).
5. Open **@userinfobot** → **Start**. It replies with your **Id** (a number). Copy it.

## 2. Put the bot on GitHub (10 minutes)

1. Sign up at [github.com](https://github.com) (free).
2. **New repository** → name it `nutcracker-watcher` → choose **Public** → **Create**.
   *Public is what makes it free to run all day. Your Telegram token is stored
   separately as a secret and is never visible.*
3. Upload the files:
   - **Add file → Upload files** → add `monitor.py`, `requirements.txt`, `README.md`, `.gitignore` → **Commit**.
   - **Add file → Create new file** → type the name `.github/workflows/monitor.yml`
     (the slashes create the folders) → paste the contents of that file → **Commit**.
4. **Settings → Secrets and variables → Actions → New repository secret**, twice:
   - `TELEGRAM_BOT_TOKEN` = the token from BotFather
   - `TELEGRAM_CHAT_ID` = your Id from @userinfobot
5. **Actions** tab → enable workflows if asked → **Nutcracker ticket watcher** →
   **Run workflow**.

Within about 3 minutes your bot sends **👋 Nutcracker watcher is running** with
the number of performances it found. That's it — it now runs by itself until
you turn it off (Actions → the workflow → ⋯ → Disable workflow).

## What the messages mean

| Message | Meaning |
|---|---|
| 🎟🎟 **Seats in your rows** | Seats in your target rows are free right now — open the link and book. |
| 🎟 **Tickets just appeared** | A sold-out date is showing tickets, but the seat map couldn't be read — check fast. |
| *(silent)* Returns appeared… none in your rows | Something came back, just not where you want to sit. |
| ⏳ / ⚠️ *(silent)* | The site has a queue up, or the bot can't read the page. It keeps trying. |

In the bot chat: **/status** shows every date, **/check** checks right now, **/help**.

## Optional settings

**Settings → Secrets and variables → Actions → Variables tab → New variable**

- `ONLY_DATES` — e.g. `19 December, 23 December, 2 January`
  (full month names). Strongly recommended: with fewer dates, each seat map
  is checked every few minutes instead of roughly every half hour.
- `TARGET_SEATS` — default `Orchestra Stalls: A-G; Stalls Circle: A`.
  Example with Grand Tier too: `Orchestra Stalls: A-G; Stalls Circle: A; Grand Tier: A-B`

## Good to know

- It checks the Nutcracker page every ~3 minutes and opens seat maps of any
  date whose status changes straight away, plus a few others in rotation.
- It only reads public pages. It never books, logs in or skips a queue —
  you buy the tickets yourself. Stay logged in to your RBO account on your
  phone so checkout is quick.
- **Friday Rush:** every Friday at 1pm the RBO releases tickets for the
  following week's performances, including sold-out ones (max 2 per person):
  https://www.rbo.org.uk/visit/tickets/friday-rush
- If the RBO changes its website, the bot tells you it can't read the page.
  Page snapshots are kept in each run (Actions → a run → *Artifacts*) for fixing it.

## Running it on your own computer instead (optional)

Needs Python 3.10+. The computer has to stay awake.

```bash
pip install -r requirements.txt
python -m playwright install chromium
cp .env.example .env        # then put your token and Id in .env
python monitor.py --test-telegram   # should send you a test message
python monitor.py
```
