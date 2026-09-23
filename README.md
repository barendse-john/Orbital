# News Bot

A private Telegram bot that follows the topics you care about and sends you a
daily digest. Talk to it normally - *"i like Manchester United"*, *"search up
news about satellites"* - or use slash commands when you'd rather be terse.

Built for a Raspberry Pi: Python 3.10+, SQLite, no external services beyond the
news source and the model you choose.

---

## What it does

| | |
|---|---|
| **Understands plain English** | *"i like Manchester United"* saves a topic. *"any updates on Starship?"* runs a one-off search. *"send it at 7am"* moves your digest. |
| **Asks before it guesses** | Say *"finance"* and it asks what you actually mean - up to four short questions, stopping the moment it could write a good query - then searches for `"financial markets" OR "central bank"` instead of matching the word anywhere. |
| **Learns from talking to you** | Say *"more on launch startups"* mid-conversation and the topic's query changes there and then. Something you just keep asking about has to keep coming up for over a week before it counts, so one busy news week doesn't rewrite what you follow. Every change is announced in one line. |
| **Daily digest** | One short message at a time you pick, in your own timezone. The best six stories of the last 24 hours, ranked by how many outlets ran them, how well they match your topic, the source's track record and how late in the day they broke. Each story is a sentence with the outlet's name carrying the link, and the lead story's picture on top. |
| **A news chat, not a digest on demand** | Ask for a digest mid-afternoon and it says so, then asks what you actually want to know - and answers from real reporting, with the links in the words. It follows up until you say thanks or go quiet. |
| **Per-person setup** | Whitelisted friends each get their own topics, their own digest time, their own timezone. |
| **Friends ask, you tap** | A stranger who messages the bot gets turned away politely, and you get their name, username and first message with Approve / Deny buttons. One person, one request, however many times they message. `/requests` shows the history. |
| **Never repeats itself** | An article you've already been sent won't come back, even if a different source or a second topic turns it up. |
| **Two news sources** | GNews API while the free 100/day allowance lasts, then Google News RSS - free, unlimited, no key. |
| **Two AI backends** | Anthropic (cheap, fast) or a local Ollama model (free, slower). One line in `config.yaml`. |

---

## Setup

### 1. Get your tokens

**Telegram bot token** - message [@BotFather](https://t.me/BotFather), send
`/newbot`, follow the prompts, copy the token.

**Anthropic API key** (if using the `anthropic` backend) -
[console.anthropic.com](https://console.anthropic.com) → API keys.

**GNews API key** (optional) - [gnews.io](https://gnews.io) → free tier, 100
requests/day. Leave it blank to run on RSS alone.

### 2. Install

```bash
git clone <your-repo> ~/newsbot   # or just copy the folder to the Pi
cd ~/newsbot
./scripts/setup.sh
```

That creates a virtualenv, installs everything, and copies `.env.example` and
`config.example.yaml` into place.

### 3. Configure

`.env` holds the secrets:

```ini
TELEGRAM_BOT_TOKEN=123456789:AA...
ANTHROPIC_API_KEY=sk-ant-...
GNEWS_API_KEY=
```

`config.yaml` holds everything else - you don't need to edit the whitelist
by hand. Leave it empty, start the bot, and message it: the first person to
do so automatically becomes its owner (whitelisted and admin), no restart or
YAML editing required.

```yaml
telegram:
  whitelist: []
  admins: []
```

Only fill these in yourself if you want to hand-pick the owner ahead of time,
or you're restoring a bot's `config.yaml` without its database and want to
skip the claim step.

### 4. Check and run

```bash
./.venv/bin/python -m newsbot --check   # verifies token, key, and a live news fetch
./.venv/bin/python -m newsbot
```

Message the bot on Telegram and it walks you through setup: it asks for your
timezone (tap **📍 Share my location**, or type a city like *"Amsterdam"*),
then what time you want your digest. Only the timezone is stored - the
coordinates are used once and thrown away.

### 5. Keep it running

```bash
sudo cp scripts/newsbot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now newsbot
journalctl -u newsbot -f          # watch the logs
```

Edit the `User=` and paths in the unit file if the project isn't at
`/home/pi/newsbot`.

---

## Using it

Plain English works for everything:

| You say | It does |
|---|---|
| *"i like Manchester United"* | Saves a topic |
| *"keep me posted on the ECB"* | Saves a topic |
| *"search up news about satellites"* | One-off search, right now |
| *"what's happening in Sudan"* | One-off search |
| *"stop sending me tennis"* | Drops a topic |
| *"send the digest at 7am"* | Moves your delivery time |
| *"i'm in Tokyo now"* | Changes your timezone |

And the commands, which keep working even if the model is unreachable:

```
/topics            what you follow
/add <topic>       follow something
/remove <topic>    stop following it
/retune <topic>    narrow what a topic searches for (or just say so)
/search <query>    search now
/digest            (admins) send the ranked digest immediately
/time 08:00        set your digest time
/timezone Tokyo    set your timezone
/pause /resume     mute or unmute the daily digest
/requests          (admins) who has asked to join, and what you decided
/status            your settings and today's API usage
/allow <id>        (admins) let a friend in
/deny <id>         (admins) remove them
```

---

## Backups

Git covers the code. It deliberately doesn't cover the three things that
would actually hurt to lose, because they're secret or machine-specific:
`.env` (your API keys), `config.yaml`, and `data/newsbot.db` (topics,
timezone, digest time, sent-article history).

```bash
./scripts/backup.sh
```

Writes a timestamped `.tar.gz` to `~/newsbot-backups/`, keeping the last 14.
The database is snapshotted through sqlite3's backup API rather than copied,
so a backup taken while the bot is mid-write is still consistent. The archive
holds your keys in plaintext and is written `0600` accordingly.

**Get it off the Pi.** An SD card that dies takes its own backups with it:

```bash
scp john@raspberrypi.local:~/newsbot-backups/newsbot-*.tar.gz .
```

**To restore onto a fresh card**, clone the repo as usual, then unpack over
the top:

```bash
cd ~/newsbot
tar -xzf newsbot-20260903-220546.tar.gz
mkdir -p data && mv newsbot.db data/
sudo systemctl restart newsbot
```

Your topics, schedule and read history come back exactly as they were.

**To run it weekly**, add a cron entry (`crontab -e`):

```
0 4 * * 0 /home/john/newsbot/scripts/backup.sh
```

## Keeping the Pi up to date

The Pi's checkout is a git clone of this repo, so pushing a change to GitHub
doesn't reach it by itself - something on the Pi has to `git pull`.
`newsbot-update.timer` does that automatically: it checks for new commits
every 15 minutes and, if there are any, pulls, reinstalls dependencies if
`requirements.txt` changed, and restarts the service.

**One-time setup on the Pi:**

```bash
# Let deploy.sh restart the service without asking for a password
echo "john ALL=(root) NOPASSWD: /usr/bin/systemctl restart newsbot" | \
  sudo tee /etc/sudoers.d/newsbot-deploy
sudo chmod 440 /etc/sudoers.d/newsbot-deploy

sudo cp scripts/newsbot-update.service scripts/newsbot-update.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now newsbot-update.timer
```

Check it's scheduled and see when it last ran:

```bash
systemctl list-timers newsbot-update.timer
journalctl -t newsbot-deploy -f
```

**Deploying by hand** (skip the wait, or check what a pull would do) works the
same way whether or not the timer is installed:

```bash
./scripts/deploy.sh
```

It's quiet when there's nothing new, and only pulls when the Pi is behind
`origin/main` - `.env`, `config.yaml`, and `data/` are gitignored, so nothing
you've configured locally is ever touched or overwritten by a pull.

If a deploy ever fails because of local changes on the Pi (`git pull was not
a fast-forward`), that means something was edited directly on the Pi instead
of pushed through git - `git status` there to see what, then either commit
and push it properly or `git stash` it before pulling again.

## Adding friends

1. They message the bot; it replies with their Telegram ID.
2. You send `/allow <their id>`.
3. They send `/start` and do their own setup.

They get their own topics, their own digest time, and their own timezone.
Nothing is shared. `/deny <id>` removes them again.

---

## Choosing an AI backend

```yaml
ai:
  backend: anthropic     # or: ollama
```

**Anthropic** - roughly $0.30-0.50/month at five topics and five articles a
day on `claude-haiku-4-5`. Fast enough that chat feels instant.

**Ollama** - free and private, but a Pi with no GPU takes 30+ seconds per
summary. Sensible if you're running this on a desktop:

```bash
ollama pull llama3.2:3b
```

```yaml
ai:
  backend: ollama
  ollama:
    base_url: http://localhost:11434   # or your desktop's IP from the Pi
    model: llama3.2:3b
```

Switching is a config edit and a restart - no data migration, no code change.

**If the model is down entirely**, the bot keeps working: keyword rules handle
`add`/`remove`/`search`, and digests fall back to each article's own blurb
instead of a written summary.

---

## How the news sources fit together

Each topic in a digest costs one search. With the default cap of five topics
that's five requests a day, well inside the GNews free tier - so on-demand
searches have plenty of headroom too.

When GNews returns a quota error, the bot marks the allowance spent for the day
and everything falls through to Google News RSS, which has no key and no limit.
Transient GNews errors (a 503, a timeout) hand the request back to the counter
so a bad minute doesn't cost you a slot. `/status` shows the day's usage.

Articles are identified by their normalised headline rather than their URL,
because GNews and RSS hand back different links for the same story. That's what
stops the same piece arriving twice from two sources, or twice under two
topics.

---

## When a question finds nothing

News APIs match words that literally appear in headlines, and they AND the
terms together. That works for named things - "Manchester United", "Starship"
- and fails for themes, because no headline contains "top businesses invest".
Since the intent parser compresses whatever you typed into a short phrase,
thematic questions used to come back empty and fall straight through to the
model's own knowledge.

A question that finds nothing now widens along two axes:

1. **Time** - the last 24h, then the week, then the month. Right for a real
   subject with sparse coverage.
2. **The query itself** - the model rewrites it into terms that actually
   appear in coverage: *"where are corporations investing"* becomes
   `Nvidia data center spending`, `capital expenditure earnings`. Each
   candidate is tried against the month.

The rewrite is only requested once the original query has already come up
empty, so it costs nothing on the common path. Without a model (Ollama down,
no API key) it falls back to dropping filler words and ORing what is left.

When a rewrite is what found the articles, the reply says so - otherwise
results that look tangential read as a bug rather than a broadened search.
Only after all of that does it answer from background knowledge.

## Project layout

```
newsbot/
├── __main__.py      entry point, wiring, --check
├── config.py        config.yaml + .env, with ${VAR} interpolation
├── db.py            SQLite: users, topics, sent articles, quota
├── brain.py         all prompts: intent, summaries, timezone lookup
├── handlers.py      Telegram handlers, onboarding, commands
├── digest.py        building and sending digests
├── scheduler.py     one daily job per user, in their timezone
├── formatting.py    Telegram HTML, message splitting
├── timezones.py     coordinates -> IANA zone, offline
├── ai/              anthropic | ollama, behind one interface
└── news/            gnews | rss, behind one fetcher
```

Run the tests (no keys or network needed):

```bash
./.venv/bin/python -m unittest discover -s tests -t .
```

---

## Troubleshooting

**"This is a private bot"** - your ID isn't whitelisted. The message includes
your ID; add it to `config.yaml` and restart.

**No digest arrived** - check `/status` for your time and timezone, confirm the
digest isn't paused, and look at `journalctl -u newsbot -e`. Jobs are
rescheduled on every start, so a restart fixes a stuck schedule.

**"Nothing new today"** - every article found was already sent to you. Verify
with `/search <topic>`, which ignores the dedupe filter.

**The location button doesn't appear** - Telegram Desktop doesn't support it.
Type a city name instead, or use `/timezone Europe/Amsterdam`.

**`timezonefinder` won't install** - skip it. The bot detects its absence and
asks for a city name instead; nothing else changes.

**Summaries look like raw blurbs** - the model call failed and the bot fell
back. Check the logs for the reason (bad key, rate limit, Ollama not running).

---

## Rocket launches and the globe

The bot also tracks rocket launches (The Space Devs' Launch Library 2) and
satellites (CelesTrak), both free and keyless.

- `/launches` - the next five launches, in your timezone, with livestream links.
- `/launchalerts` - toggle reminders a day and 30 minutes before liftoff
  (launches marked Go or TBC; a launch that slips is reminded again).

**The globe** is served by the bot itself at `http://<pi-hostname>.local:8080`
on your home network - no extra service. It opens in a see-through amber "hologram" view (Real view button switches
to satellite imagery). It is a CesiumJS globe on the real
WGS84 ellipsoid (flattened at the poles), with day/night lighting and
controls: scroll wheel zooms, middle-drag pans (the ground follows the
cursor), right-drag rotates the view around the point you're looking at
(sideways turns, up/down tilts). On a phone, one finger pans and pinch
zooms. The globe is near-solid so the far side doesn't distract; the X-ray
button makes it see-through. Left-click inspects: a country or ocean
shows its latest news, a satellite shows its orbit and news about it, a
launch pad shows the launch and the rocket's specs and news. The layer
buttons (Sats, Launches, Grid, Rings, Borders) hide things you don't want.
Globe news comes from Google News RSS via `/api/news`, cached 15 minutes -
it never spends the GNews allowance the digests use. Launches in the next 72 hours get a
card pinned above their pad with a live countdown and link (pulsing red in the
last hour). Satellites fly on their actual orbits, propagated with SGP4 in the
browser; tap one for altitude, speed and its full orbit, or Follow it. The
1x/60x/600x buttons speed time up.

On a phone, open the page and use "Add to Home Screen" to get it as an app.
The page loads its 3D engine from cdn.jsdelivr.net, so the viewing device
needs internet (the Pi only serves the page and data).

Settings live under `space:` in `config.yaml` (see `config.example.yaml`);
without that section the defaults apply.

## The phone app (Orbital)

`/app` on the Pi is an installable app: upcoming launches with countdowns,
Watch / launch-page / calendar buttons, your news briefing and topics, and
notifications for launch reminders and the daily briefing.

**Make it reachable over https** (Android only installs apps and allows
notifications over https). With Tailscale on the Pi and the phone:

```bash
sudo tailscale serve --bg 8080          # https://<pi-name>.<tailnet>.ts.net
```

MagicDNS and HTTPS certificates must be on in the Tailscale admin console
(DNS page). Then put that address in `config.yaml` so Telegram links use it:

```yaml
space:
  public_url: https://jb.your-tailnet.ts.net
```

**Pair your phone:** send `/app` to the bot, open the link on the phone, then
Chrome ⋮ → Install app, and switch on notifications in its Settings tab.
`/app unpair` signs every phone out.

**Calendar:** every launch has "+ Google Calendar" and "+ .ics" buttons. The
whole schedule is a feed at `/calendar.ics`. Google Calendar can only
subscribe to a public link; to allow that for the feed alone:

```bash
sudo tailscale funnel --bg --https=8443 --set-path=/calendar.ics http://127.0.0.1:8080/calendar.ics
```

then add `https://<pi-name>.<tailnet>.ts.net:8443/calendar.ics` in Google
Calendar → Other calendars → From URL. Google refreshes it every few hours.
