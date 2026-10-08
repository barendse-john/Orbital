# News Bot

A self-hosted news assistant for a Raspberry Pi. It follows the topics you
care about, sends you a ranked morning briefing, and tracks rocket launches and
satellites on a 3D globe. You talk to it on Telegram in plain English, or read
everything in **Orbital**, its installable phone app.

- **Python 3.10+**, SQLite, no database server, no cloud hosting
- **Runs on a Pi 4/5** (or any Linux box) as a systemd service
- **Free to run** apart from the AI: about $0.30–0.50 a month on Claude Haiku,
  or nothing with a local Ollama model

---

## Contents

- [What it does](#what-it-does)
- [Quick start](#quick-start)
- [Using it](#using-it)
- [Orbital, the phone app](#orbital-the-phone-app)
- [The launch and satellite globe](#the-launch-and-satellite-globe)
- [How news is chosen](#how-news-is-chosen)
- [Choosing an AI backend](#choosing-an-ai-backend)
- [Optional: reports from Google Drive](#optional-reports-from-google-drive)
- [Running it long-term](#running-it-long-term)
- [Security notes](#security-notes)
- [Project layout and tests](#project-layout-and-tests)
- [Troubleshooting](#troubleshooting)
- [License](#license)

---

## What it does

**News**

| | |
|---|---|
| **Understands plain English** | *"i like Manchester United"* saves a topic. *"any updates on Starship?"* runs a one-off search. *"send it at 7am"* moves your briefing. |
| **Asks before it guesses** | Say *"finance"* and it asks what you actually mean (up to four short questions) and then searches for `"financial markets" OR "central bank"` instead of matching the word anywhere. |
| **Learns from conversation** | *"more on launch startups"* retunes a topic on the spot. Something you keep asking about has to keep coming up for over a week before it counts, so one busy news week doesn't rewrite what you follow. Every change is announced in one line. |
| **Ranked morning briefing** | An hourly engine collects stories from Google News and the site feeds you choose, has the AI score each one for relevance and impact, and builds your briefing from the best. Major stories can arrive as they break (capped per day). |
| **A news chat** | Ask a question and it answers from real reporting, with links in the words. When nothing matches, it widens the time window and rewrites the query before falling back on background knowledge. |
| **Never repeats itself** | Articles are deduplicated by normalised headline, so the same story doesn't arrive twice from two sources or under two topics. |

**Space**

| | |
|---|---|
| **Launch reminders** | `/launches` lists what's next; `/launchalerts` reminds you a day and 30 minutes before liftoff (and again if a launch slips). |
| **3D globe** | CesiumJS globe on the real WGS84 ellipsoid with satellites flying on their actual orbits (SGP4 in the browser), countdown cards above launch pads, and news for whatever you click. |

**People and access**

| | |
|---|---|
| **Per-person setup** | Everyone you let in gets their own topics, briefing time and timezone. Nothing is shared. |
| **Friends ask, you tap** | A stranger who messages the bot is told you've been asked; you get their name, username and first message with **Approve / Deny** buttons. One request per person however many times they message, and a denial is final. |
| **First message claims the bot** | No whitelist editing: the first person to message a fresh bot becomes its owner and admin. |

---

## Quick start

### 1. Get your keys

| Key | Where | Needed? |
|---|---|---|
| Telegram bot token | Message [@BotFather](https://t.me/BotFather), send `/newbot` | Yes |
| Anthropic API key | [console.anthropic.com](https://console.anthropic.com) → API keys | Only for the `anthropic` backend |
| GNews API key | [gnews.io](https://gnews.io), free tier, 100 requests/day | No, Google News RSS works without one |

Launch data ([The Space Devs](https://thespacedevs.com/llapi)) and satellite
orbits ([CelesTrak](https://celestrak.org)) need no keys.

### 2. Install

```bash
git clone https://github.com/barendse-john/news_bot.git
cd news_bot
./scripts/setup.sh
```

`setup.sh` creates a virtualenv in `.venv/`, installs the requirements, and
copies `.env.example` → `.env` and `config.example.yaml` → `config.yaml`.

### 3. Configure

Put your secrets in `.env`:

```ini
TELEGRAM_BOT_TOKEN=123456789:AA...
ANTHROPIC_API_KEY=sk-ant-...
GNEWS_API_KEY=
```

`config.yaml` holds everything else and works as copied. Every option is
commented in [`config.example.yaml`](config.example.yaml). You don't need to
fill in `telegram.whitelist` or `telegram.admins`: leave them empty and the
first person to message the bot becomes its owner.

### 4. Check and run

```bash
./.venv/bin/python -m newsbot --check   # verifies the token, the AI key and a live news fetch
./.venv/bin/python -m newsbot
```

Message your bot on Telegram. It asks for your timezone (tap
**📍 Share my location**, or type a city such as *"Amsterdam"*) and then what
time you want your briefing. Only the timezone is stored; the coordinates are
used once and discarded.

### 5. Run it as a service

The unit files in `scripts/` contain example paths. This installs them with
your username and checkout path filled in, without editing the files in the
repo:

```bash
sed "s|/home/john/Documents/news_bot|$PWD|g; s|^User=john|User=$USER|" \
  scripts/newsbot.service | sudo tee /etc/systemd/system/newsbot.service >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable --now newsbot
journalctl -u newsbot -f      # watch the logs
```

---

## Using it

Plain English works for almost everything:

| You say | It does |
|---|---|
| *"i like Manchester United"* | Follows a topic |
| *"keep me posted on the ECB"* | Follows a topic |
| *"search up news about satellites"* | Searches right now |
| *"what's the market saying about Nvidia?"* | Reads the coverage and answers |
| *"stop sending me tennis"* | Drops a topic |
| *"send the briefing at 7am"* | Moves your delivery time |
| *"i'm in Tokyo now"* | Changes your timezone |

The commands keep working even when the AI is unreachable:

```
/topics            what you follow
/add <topic>       follow something
/remove <topic>    stop following it
/clear             stop following everything
/retune <topic>    narrow what a topic searches for (or just say so)
/search <query>    search now
/time 08:00        set your briefing time
/timezone Tokyo    set your timezone
/pause  /resume    mute or unmute the daily briefing
/status            your settings and today's API usage
/launches          the next five launches, in your timezone
/launchalerts      toggle launch reminders
/app               pair your phone with the Orbital app
/help              the list in Telegram

Admins only:
/digest            send the ranked briefing now
/requests          who has asked to join, and what you decided
/users             who can use the bot
/allow <id>        let someone in
/deny <id>         remove them
```

---

## Orbital, the phone app

The bot serves an installable web app at `/app` on port 8080. It shows
upcoming launches with countdowns (Watch, launch page and calendar buttons),
your briefing and topics with 👍/👎 voting, a **Top now** view of the last 24
hours, an optional Reports tab, and push notifications for launch reminders,
breaking stories and the morning briefing.

**Pairing:** send `/app` to the bot and open the link on your phone.
`/app unpair` signs every paired phone out.

**Installing and notifications need HTTPS.** Over plain `http://` the page
works, but Android only offers *Create shortcut* and won't allow
notifications. The easiest way to get HTTPS is
[Tailscale](https://tailscale.com) on both the Pi and the phone:

```bash
sudo tailscale serve --bg 8080      # → https://<pi-name>.<tailnet>.ts.net
```

Turn on MagicDNS and HTTPS certificates in the Tailscale admin console (DNS
page), then point the bot's links at that address in `config.yaml`:

```yaml
space:
  public_url: https://<pi-name>.<tailnet>.ts.net
```

Now open the `/app` link, choose Chrome ⋮ → **Install app**, and switch on
notifications in the app's Settings tab.

**Calendar:** every launch has **+ Google Calendar** and **+ .ics** buttons,
and the whole schedule is a feed at `/calendar.ics`. Google Calendar can only
subscribe to a public URL. To expose that one path, and nothing else:

```bash
sudo tailscale funnel --bg --https=8443 --set-path=/calendar.ics http://127.0.0.1:8080/calendar.ics
```

Then add `https://<pi-name>.<tailnet>.ts.net:8443/calendar.ics` under Google
Calendar → Other calendars → From URL.

---

## The launch and satellite globe

The globe is served at the root of the same web server
(`http://<pi-hostname>.local:8080`). It opens in an amber "hologram" view;
**Real view** switches to satellite imagery.

- **Click** a country or ocean for its latest news, a satellite for its orbit
  and news about it, or a launch pad for the launch, the rocket's specs and
  news.
- **Launches** in the next 72 hours get a card pinned above their pad with a
  live countdown, which pulses red in the final hour.
- **Satellites** are propagated with SGP4 in the browser. Tap one for its
  altitude, speed and full orbit, or **Follow** it. The 1×/60×/600× buttons
  speed up time.
- **Layers** (Sats, Launches, Grid, Rings, Borders) toggle on and off;
  **X-ray** makes the globe see-through.

**Mouse controls (default):** scroll to zoom, middle-drag or Shift+left-drag
to pan, right-drag to spin the globe like a desk globe, Ctrl+right-drag to
tilt, and **⌖ Center** (or `C`) to reset. The **Mouse:** button switches to
Google Earth's layout and remembers your choice. On a phone, one finger pans
and two fingers pinch to zoom.

Globe news comes from Google News RSS (cached for 15 minutes), so it never
uses up your GNews quota. The page loads CesiumJS from cdn.jsdelivr.net, so
the viewing device needs internet access; the Pi only serves the page and the
data. Settings are under `space:` in `config.yaml`.

---

## How news is chosen

Every hour the news engine (`newsbot/engine.py`):

1. **Collects** a pool of stories: Google News RSS for each topic over the
   last 48 hours, plus the site feeds under `news.engine.feeds` (BBC, Ars
   Technica, SpaceNews, The Verge and the New York Times by default; add
   your own).
2. **Scores** each new story with the AI: which of your topics it belongs to,
   relevance 0–10 and impact 0–10, and a few words on why. Your 👍/👎 votes in
   the app are given to the AI as examples of your taste.
3. **Ranks** by relevance, impact, how many outlets carry the story, source
   credibility, freshness and your votes for that outlet. Duplicate coverage
   of one event is merged.

The morning briefing is the best unsent stories (8 by default, at most 3 per
topic). Stories scored as major are pushed as they break, at most 3 a day.

**Sources.** GNews is used while its free 100 requests/day last; after that,
everything falls through to Google News RSS, which needs no key and has no
limit. A transient GNews error (a 503 or a timeout) doesn't count against the
quota. `/status` shows the day's usage.

**When a question finds nothing.** News search matches words that literally
appear in headlines, which works for names (*Starship*) but not for themes
(*"where are corporations investing"*). An empty search therefore widens in
two steps: first the time window (24 hours → a week → a month), then the query
itself, which the AI rewrites into terms that actually appear in coverage
(`Nvidia data center spending`, `capital expenditure earnings`). When a
rewrite found the results, the reply says so. Only after all of that does it
answer from background knowledge.

---

## Choosing an AI backend

```yaml
ai:
  backend: anthropic     # or: ollama
```

| | Anthropic | Ollama |
|---|---|---|
| Cost | ~$0.30–0.50/month on `claude-haiku-4-5` | Free |
| Speed | Chat feels instant | 30+ s per summary on a Pi without a GPU |
| Best for | Running on the Pi itself | Pointing the Pi at a desktop with a GPU |

```bash
ollama pull llama3.2:3b
```

```yaml
ai:
  backend: ollama
  ollama:
    base_url: http://localhost:11434   # or your desktop's IP
    model: llama3.2:3b
```

Switching takes a config edit and a restart, with no migration. **If the
model is down entirely**, the bot keeps working: keyword rules handle
add/remove/search, and briefings fall back to each article's own summary.

---

## Optional: reports from Google Drive

If something else writes Markdown reports into a Google Drive folder (a
scheduled AI routine, a script, you), the bot checks that folder every 15
minutes and shows new reports in Orbital's **Reports** tab, with a
notification that opens straight to the report. Copies are kept in
`data/reports/`. By default only the bot's owner sees the tab; list other
Telegram user IDs under `reports.chat_ids` to share it.

**1. Give the Pi read access to Drive** (one time). The Pi uses a *service
account*, a Google identity with no browser login, which suits a headless
machine.

1. At <https://console.cloud.google.com/>, create a project.
2. **APIs & Services → Library** → enable **Google Drive API**.
3. **IAM & Admin → Service Accounts → Create**. Give it any name and skip the
   roles.
4. Open it → **Keys → Add key → JSON**. Save the file as
   `data/google-service-account.json` in the checkout and `chmod 600` it.
5. Copy the service account's email (`…@….iam.gserviceaccount.com`), then in
   Drive share the reports folder with it as **Viewer**. It can see that
   folder and nothing else.

**2. Point the bot at the folder.** In `.env`, set the folder ID (the long
string at the end of the folder's URL):

```ini
GDRIVE_REPORTS_FOLDER_ID=1AbC...
```

**3. Check and restart:**

```bash
./.venv/bin/python -m newsbot --check     # the "reports" line should say OK
sudo systemctl restart newsbot
```

The first run fills the tab with the last two weeks of reports. Only reports
less than 36 hours old trigger a notification (`reports.max_age_hours`).

---

## Running it long-term

### Automatic updates

`deploy.sh` pulls new commits from `origin/main`, reinstalls dependencies, and
restarts the service. It is fast-forward only and stays quiet when there's
nothing new. A systemd timer can run it every 15 minutes:

```bash
# Let deploy.sh restart the service without a password
echo "$USER ALL=(root) NOPASSWD: /usr/bin/systemctl restart newsbot" | \
  sudo tee /etc/sudoers.d/newsbot-deploy
sudo chmod 440 /etc/sudoers.d/newsbot-deploy

sed "s|/home/john/Documents/news_bot|$PWD|g; s|^User=john|User=$USER|" \
  scripts/newsbot-update.service | sudo tee /etc/systemd/system/newsbot-update.service >/dev/null
sudo cp scripts/newsbot-update.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now newsbot-update.timer

systemctl list-timers newsbot-update.timer   # when it last ran / runs next
journalctl -t newsbot-deploy -f              # what it did
```

Run `./scripts/deploy.sh` by hand to deploy immediately. `.env`,
`config.yaml` and `data/` are gitignored, so a pull never touches your setup.
If a deploy reports *"git pull was not a fast-forward"*, something was edited
directly on the Pi: run `git status` there, then commit it properly or
`git stash` it.

### Backups

Git covers the code but deliberately not the things that would hurt to lose:
`.env`, `config.yaml`, `data/newsbot.db` (users, topics, history) and the
Drive service account key.

```bash
./scripts/backup.sh               # → ~/newsbot-backups/, keeps the last 14
./scripts/backup.sh /mnt/usb      # or somewhere else
```

The database is snapshotted through SQLite's backup API, so a backup taken
mid-write is still consistent. **The archive contains your keys in
plaintext** and is written `0600`. Copy it off the Pi, because an SD card
that dies takes its own backups with it:

```bash
scp <user>@<pi-hostname>.local:~/newsbot-backups/newsbot-*.tar.gz .
```

To restore onto a fresh install, clone and run `setup.sh`, then unpack over
the top:

```bash
tar -xzf newsbot-YYYYMMDD-HHMMSS.tar.gz
mkdir -p data && mv newsbot.db data/
[ -f google-service-account.json ] && mv google-service-account.json data/
sudo systemctl restart newsbot
```

To back up weekly, add `0 4 * * 0 /path/to/news_bot/scripts/backup.sh` to
`crontab -e`.

---

## Security notes

- **Secrets stay local.** API keys live in `.env`; the database, the Drive
  key and the push-notification key live in `data/`. All are gitignored.
- **The web server is plain HTTP on `0.0.0.0:8080`.** It is meant for your
  home network or a Tailscale tailnet. Don't port-forward it to the internet.
  If you want a public page, put it behind a tunnel with real authentication.
- **Phone access is by pairing.** Personal endpoints (`/api/me/…`) need a
  device paired through `/app`; `/app unpair` revokes every device.
- **The bot is private by default.** Strangers can only send an access
  request, which an admin approves or denies.

---

## Project layout and tests

```
newsbot/
├── __main__.py      entry point, wiring, --check
├── config.py        config.yaml + .env, with ${VAR} interpolation
├── db.py            SQLite: users, topics, sent articles, votes, quota
├── brain.py         the prompts: intent, summaries, query rewrites, timezones
├── handlers.py      Telegram handlers, onboarding, commands, access requests
├── engine.py        the hourly collect → score → rank news engine
├── ranking.py       story scoring and merging duplicate coverage
├── digest.py        building and sending briefings
├── scheduler.py     one daily job per user, in their timezone
├── formatting.py    Telegram HTML and message splitting
├── space.py         launches (Launch Library 2) and satellites (CelesTrak)
├── webapp.py        HTTP server for the globe, the app and its API
├── appservice.py    what the Orbital app reads and writes
├── push.py          Web Push notifications (VAPID)
├── reports.py       Google Drive folder → Reports tab
├── timezones.py     coordinates → IANA timezone, offline
├── ai/              anthropic | ollama, behind one interface
├── news/            gnews | rss, behind one fetcher
└── web/             the globe (index.html) and Orbital (app/)
scripts/             setup, deploy, backup, systemd units
tests/               unit tests, no keys or network needed
```

```bash
./.venv/bin/python -m unittest discover -s tests -t .
```

---

## Troubleshooting

**"This is a private bot"**: you haven't been let in yet. The owner has been
sent your request; once they tap Approve, send `/start`.

**No briefing arrived**: check `/status` for your time and timezone, make sure
it isn't paused, and look at `journalctl -u newsbot -e`. Jobs are rescheduled
on every start, so a restart fixes a stuck schedule.

**"Nothing new today"**: every article found had already been sent to you.
`/search <topic>` ignores the dedupe filter, so use it to check.

**Summaries look like raw blurbs**: the AI call failed and the bot fell back.
The logs give the reason (bad key, rate limit, Ollama not running).

**The location button doesn't appear**: Telegram Desktop doesn't support it.
Type a city, or use `/timezone Europe/Amsterdam`.

**`timezonefinder` won't install**: skip it. The bot notices it's missing and
asks for a city name instead.

**The app won't install or notify**: it has to be opened over HTTPS (see
[Orbital](#orbital-the-phone-app)). If `pywebpush` failed to install, the app
still works, just without notifications.

**No report arrived**: run `python -m newsbot --check`; the `reports` line
names the problem. A 404 from Drive almost always means the folder isn't
shared with the service account's email.

---

## License

[MIT](LICENSE)
