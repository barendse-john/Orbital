"""Telegram wiring: onboarding, natural language, and the slash commands.

Plain text is understood by the AI backend; every action it can take also has
a slash command, so the bot still works when the model is unreachable.
"""

from __future__ import annotations

import logging

from telegram import (
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from . import timezones
from .brain import Intent, normalise_time, parse_intent, resolve_timezone, valid_timezone
from .db import Database
from .digest import DigestService
from .formatting import esc
from .news import NewsFetcher
from .scheduler import DigestScheduler

log = logging.getLogger(__name__)

STAGE = "onboarding_stage"
STAGE_TZ = "await_timezone"
STAGE_TIME = "await_time"

HELP_TEXT = """<b>Just talk to me normally:</b>
• <i>"i like Manchester United"</i> - saves a topic
• <i>"search up news about satellites"</i> - one-off search
• <i>"stop sending me tennis"</i> - drops a topic
• <i>"send the digest at 7am"</i> - changes your delivery time

<b>Or use commands:</b>
/topics - what you follow
/add &lt;topic&gt; - follow something
/remove &lt;topic&gt; - stop following it
/search &lt;query&gt; - search right now
/digest - send today's digest immediately
/time &lt;HH:MM&gt; - set your daily digest time
/timezone &lt;city&gt; - set your timezone
/pause and /resume - mute or unmute the daily digest
/status - your settings and today's API usage
/help - this message"""


class BotHandlers:
    def __init__(
        self, cfg, db: Database, ai, fetcher: NewsFetcher,
        digest: DigestService, scheduler: DigestScheduler,
    ):
        self.cfg = cfg
        self.db = db
        self.ai = ai
        self.fetcher = fetcher
        self.digest = digest
        self.scheduler = scheduler

    # --------------------------------------------------------- registration

    def register(self, app: Application) -> None:
        app.add_handler(CommandHandler("start", self.cmd_start))
        app.add_handler(CommandHandler("help", self.cmd_help))
        app.add_handler(CommandHandler("topics", self.cmd_topics))
        app.add_handler(CommandHandler("add", self.cmd_add))
        app.add_handler(CommandHandler("remove", self.cmd_remove))
        app.add_handler(CommandHandler("clear", self.cmd_clear))
        app.add_handler(CommandHandler("search", self.cmd_search))
        app.add_handler(CommandHandler("digest", self.cmd_digest))
        app.add_handler(CommandHandler("time", self.cmd_time))
        app.add_handler(CommandHandler("timezone", self.cmd_timezone))
        app.add_handler(CommandHandler("pause", self.cmd_pause))
        app.add_handler(CommandHandler("resume", self.cmd_resume))
        app.add_handler(CommandHandler("status", self.cmd_status))
        app.add_handler(CommandHandler("allow", self.cmd_allow))
        app.add_handler(CommandHandler("deny", self.cmd_deny))
        app.add_handler(MessageHandler(filters.LOCATION, self.on_location))
        app.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, self.on_text)
        )
        app.add_error_handler(self.on_error)

    # ------------------------------------------------------- access control

    async def _allowed(self, update: Update) -> bool:
        user = update.effective_user
        if user is None:
            return False

        # Nobody has claimed this bot yet (fresh install, no config entries,
        # empty database) - the first person to message it becomes the owner.
        if not self.cfg.telegram.whitelist and not self.cfg.telegram.admins:
            if await self.db.bootstrap_owner(user.id):
                log.info("Bootstrap: %s (id %s) claimed ownership", user.username,
                         user.id)
                await update.effective_message.reply_text(
                    "🔑 Nobody had claimed this bot yet, so you just did - "
                    "you're the owner and first admin. You can add friends "
                    "later with /allow.\n"
                )
                return True

        allowed = set(self.cfg.telegram.whitelist) | set(self.cfg.telegram.admins)
        allowed |= await self.db.allowed_user_ids()
        if user.id in allowed:
            return True
        log.warning("Rejected %s (@%s, id %s)", user.first_name, user.username,
                    user.id)
        await update.effective_message.reply_text(
            "This is a private bot, sorry.\n\n"
            f"Your Telegram ID is {user.id} - send that to the owner if you "
            "should have access."
        )
        return False

    async def _is_admin(self, user_id: int) -> bool:
        if user_id in set(self.cfg.telegram.admins):
            return True
        return user_id in await self.db.admin_user_ids()

    async def _ready_user(self, update: Update, context):
        """Whitelist check + user row + onboarding. None means stop here."""
        if not await self._allowed(update):
            return None
        tg = update.effective_user
        user = await self.db.ensure_user(
            tg.id, tg.username, tg.first_name,
            default_digest_time=self.cfg.digest.default_time,
        )
        if not user.timezone:
            await self._begin_onboarding(update, context)
            return None
        return user

    # ------------------------------------------------------------ onboarding

    async def _begin_onboarding(self, update: Update, context) -> None:
        context.user_data[STAGE] = STAGE_TZ
        keyboard = None
        if timezones.available():
            keyboard = ReplyKeyboardMarkup(
                [[KeyboardButton("📍 Share my location",
                                 request_location=True)]],
                resize_keyboard=True, one_time_keyboard=True,
            )
        await update.effective_message.reply_text(
            "Hi! I send you news on the topics you pick.\n\n"
            "First, when should your daily digest land? I need your timezone "
            "- tap the button below, or just type your city "
            "(\"Amsterdam\", \"Tokyo\").",
            reply_markup=keyboard or ReplyKeyboardRemove(),
        )

    async def on_location(self, update: Update, context) -> None:
        if not await self._allowed(update):
            return
        tg = update.effective_user
        await self.db.ensure_user(
            tg.id, tg.username, tg.first_name,
            default_digest_time=self.cfg.digest.default_time,
        )
        loc = update.effective_message.location
        tz = await timezones.timezone_from_coords(loc.latitude, loc.longitude)
        if not tz:
            await update.effective_message.reply_text(
                "I couldn't work out the timezone from that. What city are "
                "you in?", reply_markup=ReplyKeyboardRemove(),
            )
            context.user_data[STAGE] = STAGE_TZ
            return
        # Only the zone is kept - the coordinates are not stored.
        await self._accept_timezone(update, context, tz)

    async def _accept_timezone(self, update: Update, context, tz: str) -> None:
        user_id = update.effective_user.id
        await self.db.set_timezone(user_id, tz)
        user = await self.db.get_user(user_id)

        if user and user.onboarded:
            self.scheduler.schedule(user)
            await update.effective_message.reply_text(
                f"Timezone set to <b>{esc(tz)}</b>. Your digest stays at "
                f"{esc(user.digest_time or self.cfg.digest.default_time)} "
                "local time.",
                parse_mode=ParseMode.HTML, reply_markup=ReplyKeyboardRemove(),
            )
            return

        context.user_data[STAGE] = STAGE_TIME
        await update.effective_message.reply_text(
            f"Got it - <b>{esc(tz)}</b>.\n\n"
            "What time would you like the daily digest? Say something like "
            f"\"8am\" or \"19:30\" (or \"skip\" for "
            f"{esc(self.cfg.digest.default_time)}).",
            parse_mode=ParseMode.HTML, reply_markup=ReplyKeyboardRemove(),
        )

    async def _handle_onboarding(self, update: Update, context, text: str) -> bool:
        """Returns True if the message was consumed by the setup flow."""
        stage = context.user_data.get(STAGE)
        if stage == STAGE_TZ:
            tz = await resolve_timezone(self.ai, text)
            if not tz:
                await update.effective_message.reply_text(
                    "I don't know that place. Try a bigger city nearby, or an "
                    "exact zone like \"Europe/Amsterdam\"."
                )
                return True
            await self._accept_timezone(update, context, tz)
            return True

        if stage == STAGE_TIME:
            if text.strip().lower() in {"skip", "default", "whatever"}:
                hhmm = self.cfg.digest.default_time
            else:
                hhmm = normalise_time(text)
            if not hhmm:
                await update.effective_message.reply_text(
                    "I need a time like \"8am\", \"19:30\" or \"skip\"."
                )
                return True
            await self.db.set_digest_time(update.effective_user.id, hhmm)
            await self.db.set_onboarded(update.effective_user.id, True)
            context.user_data.pop(STAGE, None)
            user = await self.db.get_user(update.effective_user.id)
            self.scheduler.schedule(user)
            await update.effective_message.reply_text(
                f"All set - digest every day at <b>{esc(hhmm)}</b>.\n\n"
                "Now tell me what to follow: <i>\"i like Manchester United\"</i>, "
                "<i>\"follow satellite launches\"</i>.\n\n" + HELP_TEXT,
                parse_mode=ParseMode.HTML,
            )
            return True

        return False

    # --------------------------------------------------------- text handling

    async def on_text(self, update: Update, context) -> None:
        if not await self._allowed(update):
            return
        tg = update.effective_user
        text = (update.effective_message.text or "").strip()
        if not text:
            return

        user = await self.db.ensure_user(
            tg.id, tg.username, tg.first_name,
            default_digest_time=self.cfg.digest.default_time,
        )

        if context.user_data.get(STAGE) or not user.timezone:
            if not context.user_data.get(STAGE):
                await self._begin_onboarding(update, context)
                return
            if await self._handle_onboarding(update, context, text):
                return

        await context.bot.send_chat_action(tg.id, ChatAction.TYPING)
        topics = await self.db.list_topics(tg.id)
        intent = await parse_intent(self.ai, text, topics=[t.label for t in topics])
        await self._dispatch(update, context, intent)

    async def _dispatch(self, update: Update, context, intent: Intent) -> None:
        user_id = update.effective_user.id
        action = intent.action

        if action == "add_topic" and intent.topic:
            await self._add_topic(update, intent.topic, intent.query or intent.topic,
                                  intent.reply)
        elif action == "remove_topic" and intent.topic:
            await self._remove_topic(update, intent.topic)
        elif action == "list_topics":
            await self._show_topics(update)
        elif action == "clear_topics":
            removed = await self.db.clear_topics(user_id)
            await update.effective_message.reply_text(
                f"Cleared {removed} topic(s)." if removed
                else "You weren't following anything."
            )
        elif action == "search" and intent.query:
            await self._run_search(update, context, intent.query)
        elif action == "set_time" and intent.time:
            await self._set_time(update, intent.time)
        elif action == "set_timezone" and intent.timezone:
            await self._accept_timezone(update, context, intent.timezone)
        elif action == "digest_now":
            await self._send_digest_now(update, context)
        elif action == "pause":
            await self.db.set_digest_enabled(user_id, False)
            self.scheduler.cancel(user_id)
            await update.effective_message.reply_text(
                "Daily digest paused. /resume when you want it back - "
                "searches still work."
            )
        elif action == "resume":
            await self.db.set_digest_enabled(user_id, True)
            user = await self.db.get_user(user_id)
            self.scheduler.schedule(user)
            await update.effective_message.reply_text(
                f"Digest back on at {esc(user.digest_time or '')} "
                f"{esc(user.timezone or '')}.", parse_mode=ParseMode.HTML,
            )
        elif action == "help":
            await update.effective_message.reply_text(
                HELP_TEXT, parse_mode=ParseMode.HTML
            )
        else:
            await update.effective_message.reply_text(
                intent.reply or "Not sure what you meant - try /help."
            )

    # ------------------------------------------------------------- actions

    async def _add_topic(self, update: Update, label: str, query: str,
                         reply: str | None = None) -> None:
        user_id = update.effective_user.id
        created = await self.db.add_topic(user_id, label, query)
        if not created:
            await update.effective_message.reply_text(
                f"Already following <b>{esc(label)}</b>.",
                parse_mode=ParseMode.HTML,
            )
            return
        note = reply or f"Following <b>{esc(label)}</b>."
        count = len(await self.db.list_topics(user_id))
        await update.effective_message.reply_text(
            f"✅ {note}\nThat's {count} topic(s). It'll be in your next digest - "
            "or /digest for it now.",
            parse_mode=ParseMode.HTML,
        )

    async def _remove_topic(self, update: Update, label: str) -> None:
        removed = await self.db.remove_topic(update.effective_user.id, label)
        if removed:
            await update.effective_message.reply_text(
                f"🗑 Dropped <b>{esc(removed)}</b>.", parse_mode=ParseMode.HTML
            )
        else:
            await update.effective_message.reply_text(
                f"You're not following anything like \"{esc(label)}\". "
                "/topics shows the list.", parse_mode=ParseMode.HTML,
            )

    async def _show_topics(self, update: Update) -> None:
        topics = await self.db.list_topics(update.effective_user.id)
        if not topics:
            await update.effective_message.reply_text(
                "No topics yet. Try \"i like Formula 1\" or /add space launches."
            )
            return
        lines = "\n".join(f"• <b>{esc(t.label)}</b>" for t in topics)
        await update.effective_message.reply_text(
            f"You're following:\n{lines}", parse_mode=ParseMode.HTML
        )

    async def _run_search(self, update: Update, context, query: str) -> None:
        await context.bot.send_chat_action(update.effective_user.id,
                                           ChatAction.TYPING)
        body = await self.digest.search_reply(
            update.effective_user.id, query,
            limit=self.cfg.news.max_articles_per_topic,
        )
        await update.effective_message.reply_text(
            body, parse_mode=ParseMode.HTML, disable_web_page_preview=True
        )

    async def _set_time(self, update: Update, hhmm: str) -> None:
        user_id = update.effective_user.id
        await self.db.set_digest_time(user_id, hhmm)
        user = await self.db.get_user(user_id)
        scheduled = self.scheduler.schedule(user)
        suffix = f" {esc(user.timezone)}" if user.timezone else ""
        await update.effective_message.reply_text(
            f"⏰ Digest set for <b>{esc(hhmm)}</b>{suffix}." if scheduled
            else f"Time saved as {esc(hhmm)} (digest is paused - /resume).",
            parse_mode=ParseMode.HTML,
        )

    async def _send_digest_now(self, update: Update, context) -> None:
        user = await self.db.get_user(update.effective_user.id)
        if user is None:
            return
        await context.bot.send_chat_action(user.user_id, ChatAction.TYPING)
        await self.digest.send_digest(context.bot, user, manual=True)

    # ------------------------------------------------------------ commands

    async def cmd_start(self, update: Update, context) -> None:
        if not await self._allowed(update):
            return
        tg = update.effective_user
        user = await self.db.ensure_user(
            tg.id, tg.username, tg.first_name,
            default_digest_time=self.cfg.digest.default_time,
        )
        if not user.timezone:
            await self._begin_onboarding(update, context)
            return
        await update.effective_message.reply_text(
            f"Welcome back, {esc(tg.first_name or 'there')}.\n\n" + HELP_TEXT,
            parse_mode=ParseMode.HTML,
        )

    async def cmd_help(self, update: Update, context) -> None:
        if not await self._allowed(update):
            return
        await update.effective_message.reply_text(
            HELP_TEXT, parse_mode=ParseMode.HTML
        )

    async def cmd_topics(self, update: Update, context) -> None:
        if await self._ready_user(update, context):
            await self._show_topics(update)

    async def cmd_add(self, update: Update, context) -> None:
        if not await self._ready_user(update, context):
            return
        topic = " ".join(context.args).strip()
        if not topic:
            await update.effective_message.reply_text("Usage: /add Formula 1")
            return
        await self._add_topic(update, topic, topic)

    async def cmd_remove(self, update: Update, context) -> None:
        if not await self._ready_user(update, context):
            return
        topic = " ".join(context.args).strip()
        if not topic:
            await update.effective_message.reply_text("Usage: /remove Formula 1")
            return
        await self._remove_topic(update, topic)

    async def cmd_clear(self, update: Update, context) -> None:
        if not await self._ready_user(update, context):
            return
        removed = await self.db.clear_topics(update.effective_user.id)
        await update.effective_message.reply_text(
            f"Cleared {removed} topic(s)." if removed else "Nothing to clear."
        )

    async def cmd_search(self, update: Update, context) -> None:
        if not await self._ready_user(update, context):
            return
        query = " ".join(context.args).strip()
        if not query:
            await update.effective_message.reply_text("Usage: /search mars rover")
            return
        await self._run_search(update, context, query)

    async def cmd_digest(self, update: Update, context) -> None:
        if await self._ready_user(update, context):
            await self._send_digest_now(update, context)

    async def cmd_time(self, update: Update, context) -> None:
        if not await self._ready_user(update, context):
            return
        raw = " ".join(context.args).strip()
        hhmm = normalise_time(raw) if raw else None
        if not hhmm:
            await update.effective_message.reply_text(
                "Usage: /time 08:00 (or /time 7am)"
            )
            return
        await self._set_time(update, hhmm)

    async def cmd_timezone(self, update: Update, context) -> None:
        if not await self._allowed(update):
            return
        place = " ".join(context.args).strip()
        if not place:
            await update.effective_message.reply_text(
                "Usage: /timezone Amsterdam (or /timezone Europe/Amsterdam)"
            )
            return
        tz = place if valid_timezone(place) else await resolve_timezone(self.ai, place)
        if not tz:
            await update.effective_message.reply_text(
                f"I don't know \"{esc(place)}\". Try a bigger city or an exact "
                "zone name.", parse_mode=ParseMode.HTML,
            )
            return
        await self._accept_timezone(update, context, tz)

    async def cmd_pause(self, update: Update, context) -> None:
        if not await self._ready_user(update, context):
            return
        await self._dispatch(update, context, Intent(action="pause"))

    async def cmd_resume(self, update: Update, context) -> None:
        if not await self._ready_user(update, context):
            return
        await self._dispatch(update, context, Intent(action="resume"))

    async def cmd_status(self, update: Update, context) -> None:
        if not await self._allowed(update):
            return
        user_id = update.effective_user.id
        user = await self.db.get_user(user_id)
        stats = await self.db.stats(user_id)
        quota = self.cfg.news.gnews.daily_quota
        source = "GNews + RSS fallback" if self.cfg.news.gnews.api_key else "RSS only"
        lines = [
            f"<b>Your ID</b>: <code>{user_id}</code>",
            f"<b>Timezone</b>: {esc(user.timezone or 'not set')}",
            f"<b>Digest</b>: {esc(user.digest_time or 'not set')}"
            f"{'' if (user and user.digest_enabled) else ' (paused)'}",
            f"<b>Topics</b>: {stats['topics']}",
            f"<b>Articles sent</b>: {stats['articles_sent']}",
            "",
            f"<b>News</b>: {source}",
            f"<b>GNews today</b>: {stats['gnews_used_today']}/{quota}",
            f"<b>AI backend</b>: {esc(self.cfg.ai.backend)}",
        ]
        await update.effective_message.reply_text(
            "\n".join(lines), parse_mode=ParseMode.HTML
        )

    async def cmd_allow(self, update: Update, context) -> None:
        if not await self._allowed(update):
            return
        if not await self._is_admin(update.effective_user.id):
            await update.effective_message.reply_text("Admins only.")
            return
        try:
            new_id = int(context.args[0])
        except (IndexError, ValueError):
            await update.effective_message.reply_text("Usage: /allow 123456789")
            return
        await self.db.allow_user(new_id, update.effective_user.id)
        await update.effective_message.reply_text(
            f"✅ {new_id} can now use the bot. Tell them to send /start."
        )

    async def cmd_deny(self, update: Update, context) -> None:
        if not await self._allowed(update):
            return
        if not await self._is_admin(update.effective_user.id):
            await update.effective_message.reply_text("Admins only.")
            return
        try:
            gone_id = int(context.args[0])
        except (IndexError, ValueError):
            await update.effective_message.reply_text("Usage: /deny 123456789")
            return
        removed = await self.db.deny_user(gone_id)
        self.scheduler.cancel(gone_id)
        await update.effective_message.reply_text(
            f"Removed {gone_id}." if removed
            else f"{gone_id} wasn't in the runtime list "
                 "(config whitelist entries are removed in config.yaml)."
        )

    # --------------------------------------------------------------- errors

    async def on_error(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        log.exception("Handler error", exc_info=context.error)
        if isinstance(update, Update) and update.effective_message:
            try:
                await update.effective_message.reply_text(
                    "Something went wrong on my end - it's logged. Try again?"
                )
            except Exception:  # noqa: BLE001
                pass
