# Orbital

A self-hosted news and space tracker for a Raspberry Pi. It follows the
topics you care about, builds a ranked morning briefing, pushes major stories
as they break, and tracks rocket launches and satellites on a 3D globe. You
read all of it in **Orbital**, an installable phone app served by the Pi.

- **Python 3.10+**, SQLite, no database server, no cloud hosting
- **Runs on a Pi 4/5** (or any Linux box) as a systemd service
- **Free to run** apart from the AI: about $0.30–0.50 a month on Claude Haiku,
  or nothing with a local Ollama model
- **Just for you**: one owner, one install, signed in by pairing your phone

---

## Contents

- [What it does](#what-it-does)
- [Quick start](#quick-start)
- [Orbital, the phone app](#orbital-the-phone-app)
- [The launch and satellite globe](#the-launch-and-satellite-globe)
- [How news is chosen](#how-news-is-chosen)
- [Choosing an AI backend](#choosing-an-ai-backend)
- [Optional: reports from Google Drive](#optional-reports-from-google-drive)
- [Running it long-term](#running-it-long-term)
- [Coming from the Telegram version](#coming-from-the-telegram-version)
- [Security notes](#security-notes)
- [Project layout and tests](#project-layout-and-tests)
- [Troubleshooting](#troubleshooting)
- [License](#license)

---

## What it does

**News**

| | |
|---|---|
| **Asks before it guesses** | Add *"finance"* as a topic and the app asks what you actually mean (up to four short questions), then searches for `"financial markets" OR "central bank"` instead of matching the word anywhere. |
| **Ranked morning briefing** | An hourly engine collects stories from Google News and the site feeds you choose, has the AI score each one for relevance and impact, and builds your briefing from the best. It arrives as a notification at the time you pick, in your timezone. |
| **Breaking news** | Stories scored as major arrive as a notification straight away (at most 3 a day, and you can turn it off). |
| **Learns your taste** | 👍/👎 on any story are shown to the AI as examples of what you do and don't want. |
| **Never repeats itself** | Stories are deduplicated by headline and merged across outlets, so one event never arrives twice. |

**Space**

| | |
|---|---|
| **Launch reminders** | A notification a day and 30 minutes before liftoff for launches marked Go or TBC, and again if a launch slips. |
| **3D globe** | CesiumJS globe on the real WGS84 ellipsoid with satellites flying on their actual orbits (SGP4 in the browser), countdown cards above launch pads, and news for whatever you click. |
| **Calendar** | Every launch can go into Google Calendar or any `.ics` calendar, or subscribe to the whole schedule. |

---

## Quick start

### 1. Get your keys

| Key | Where | Needed? |
|---|---|---|
| Anthropic API key | [console.anthropic.com](https://console.anthropic.com) → API keys | Only for the `anthropic` backend |
| GNews API key | [gnews.io](https://gnews.io), free tier, 100 requests/day | No, Google News RSS works without one |

Launch data ([The Space Devs](https://thespacedevs.com/llapi)) and satellite
orbits ([CelesTrak](https://celestrak.org)) need no keys.

### 2. Install

```bash
git clone https://github.com/barendse-john/Orbital.git
cd Orbital
./scripts/setup.sh
```

`setup.sh` creates a virtualenv in `.venv/`, installs the requirements, and
copies `.env.example` → `.env` and `config.example.yaml` → `config.yaml`.

### 3. Configure

Put your keys in `.env`:

```ini
ANTHROPIC_API_KEY=sk-ant-...
GNEWS_API_KEY=
```

`config.yaml` holds everything else and works as copied. Every option is
commented in [`config.example.yaml`](config.example.yaml). The one you may
need is `space.public_url`: the address your phone uses to reach the Pi (see
[Orbital](#orbital-the-phone-app)). Left empty, it's
`http://<pi-hostname>.local:8080`.

### 4. Check it

```bash
./.venv/bin/python -m newsbot check   # the AI, a live news fetch, launches and orbits
```

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

(`./.venv/bin/python -m newsbot` runs it in the foreground instead.)

### 6. Pair your phone

```bash
./.venv/bin/python -m newsbot pair --name "Your name"
```

This prints a link and a QR code. Open the link on your phone and Orbital
signs in, picks up your phone's timezone, and is ready for topics. Add a few
in the **News** tab and switch on notifications in **Settings**. Your first
briefing arrives at 08:00 unless you choose another time.

`python -m newsbot unpair` signs every paired phone out. Run `pair` again to
add or re-add a phone.

---

## Orbital, the phone app

The app lives at `/app` on port 8080 and has four tabs:

- **Launches**: upcoming launches with countdowns, plus Watch, launch-page and
  calendar buttons.
- **News**: your briefings and a **Top now** view of the best of the last 24
  hours. Add and remove topics here, and vote 👍/👎 on stories.
- **Reports**: Markdown reports from a Google Drive folder, if you set that up.
- **Settings**: briefing time and timezone, launch reminders and notifications.

**Installing and notifications need HTTPS.** Over plain `http://` the app
works, but Android only offers *Create shortcut* and won't allow
notifications. The easiest way to get HTTPS is
[Tailscale](https://tailscale.com) on both the Pi and the phone:

```bash
sudo tailscale serve --bg 8080      # → https://<pi-name>.<tailnet>.ts.net
```

Turn on MagicDNS and HTTPS certificates in the Tailscale admin console (DNS
page), then set that address in `config.yaml` so pairing links use it:

```yaml
space:
  public_url: https://<pi-name>.<tailnet>.ts.net
```

Run `pair` again, open the new link, choose Chrome ⋮ → **Install app**, and
switch on notifications in the app's Settings tab.

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
   relevance 0–10 and impact 0–10, and a few words on why. Your 👍/👎 votes
   are given to the AI as examples of your taste.
3. **Ranks** by relevance, impact, how many outlets carry the story, source
   credibility, freshness and your votes for that outlet. Duplicate coverage
   of one event is merged.

The morning briefing is the best unsent stories (8 by default, at most 3 per
topic). Stories scored as major are pushed as they break, at most 3 a day.

**Sources.** GNews is used while its free 100 requests/day last; after that,
everything falls through to Google News RSS, which needs no key and has no
limit. A transient GNews error (a 503 or a timeout) doesn't count against the
quota.

---

## Choosing an AI backend

```yaml
ai:
  backend: anthropic     # or: ollama
```

| | Anthropic | Ollama |
|---|---|---|
| Cost | ~$0.30–0.50/month on `claude-haiku-4-5` | Free |
| Speed | Fast | 30+ s per summary on a Pi without a GPU |
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
model is down entirely**, Orbital keeps working: topics get a plain keyword
query, stories are ranked by keywords, and the briefing uses each article's
own summary.

---

## Optional: reports from Google Drive

If something else writes Markdown reports into a Google Drive folder (a
scheduled AI routine, a script, you), Orbital checks that folder every 15
minutes and shows new reports in the **Reports** tab, with a notification
that opens straight to the report. Copies are kept in `data/reports/`.

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

**2. Point Orbital at the folder.** In `.env`, set the folder ID (the long
string at the end of the folder's URL):

```ini
GDRIVE_REPORTS_FOLDER_ID=1AbC...
```

**3. Check and restart:**

```bash
./.venv/bin/python -m newsbot check     # the "reports" line should say OK
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
`.env`, `config.yaml`, `data/newsbot.db` (your topics, settings, history and
paired phones) and the Drive service account key.

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

Paired phones keep working after a restore. To back up weekly, add
`0 4 * * 0 /path/to/Orbital/scripts/backup.sh` to `crontab -e`.

---

## Coming from the Telegram version

Orbital started as a Telegram news bot. Version 2 drops Telegram entirely:

- **Your data carries over.** On first start, whoever claimed the old bot
  (or, failing that, its oldest user) becomes Orbital's owner, with their
  topics, settings, history and already-paired phone. `python -m newsbot
  check` names the owner.
- **Friends' access ends.** Orbital serves one person; phones paired by
  other users are no longer accepted. Their old rows stay in the database,
  unused.
- **Clean-up is optional.** `TELEGRAM_BOT_TOKEN` in `.env` and a
  `telegram:` section in `config.yaml` are ignored, so you can delete them
  whenever you like. To retire the bot itself, send `/deletebot` to
  [@BotFather](https://t.me/BotFather).

---

## Security notes

- **Secrets stay local.** API keys live in `.env`; the database, the Drive
  key and the push-notification key live in `data/`. All are gitignored.
- **The web server is plain HTTP on `0.0.0.0:8080`.** It is meant for your
  home network or a Tailscale tailnet. Don't port-forward it to the internet.
- **A pairing link is a password.** Anyone who opens it can read your news
  and change your settings, so don't share it. `python -m newsbot unpair`
  revokes every paired phone at once.
- **Public routes are read-only.** Without a paired token, the server only
  hands out the globe and its news lookups, launch and satellite data, and
  the launch calendar. Your topics, briefings and reports need the token.

---

## Project layout and tests

```
newsbot/
├── __main__.py      entry point: run, pair, unpair, check
├── config.py        config.yaml + .env, with ${VAR} interpolation
├── db.py            SQLite: owner, topics, sent articles, votes, quota
├── brain.py         the prompts: topic interviews and summaries
├── engine.py        the hourly collect → score → rank news engine
├── ranking.py       story scoring and merging duplicate coverage
├── digest.py        building the daily briefing
├── scheduler.py     every timed job (APScheduler)
├── space.py         launches (Launch Library 2) and satellites (CelesTrak)
├── webapp.py        HTTP server for the globe, the app and its API
├── appservice.py    what the Orbital app reads and writes
├── push.py          Web Push notifications (VAPID)
├── reports.py       Google Drive folder → Reports tab
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

**The app says "Pair this phone"**: it has no valid sign-in. Run
`python -m newsbot pair` on the Pi and open the new link. This also happens
after `unpair`.

**The pairing link won't open on the phone**: the link uses
`space.public_url`, or `http://<pi-hostname>.local:8080` when that's empty,
and not every phone resolves `.local` names. Set `public_url` to an address
the phone can reach (your Tailscale name, or the Pi's IP) and run `pair`
again, or pass it once with `pair --url http://<address>:8080`.

**The app won't install or notify**: it has to be opened over HTTPS (see
[Orbital](#orbital-the-phone-app)). If `pywebpush` failed to install, the app
still works, just without notifications.

**No briefing arrived**: in Settings, check the time, the timezone and that
the briefing is on, and that you follow at least one topic. Then look at
`journalctl -u newsbot -e`. A restart reschedules everything.

**Summaries look like raw blurbs**: the AI call failed and Orbital fell
back. The logs give the reason (bad key, rate limit, Ollama not running).

**No report arrived**: run `python -m newsbot check`; the `reports` line
names the problem. A 404 from Drive almost always means the folder isn't
shared with the service account's email.

---

## License

[MIT](LICENSE)
