"""Telegram wiring: onboarding, natural language, and the slash commands.

Plain text is understood by the AI backend; every action it can take also has
a slash command, so the bot still works when the model is unreachable.
"""

from __future__ import annotations

import logging
import re

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from telegram.constants import ChatAction, ParseMode
import httpx
from telegram.error import BadRequest, NetworkError, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from . import timezones
from .brain import (MAX_TOPIC_QUESTIONS, Intent, TopicPlan, link_citations,
                    normalise_time, parse_intent, plain_query,
                    plan_chat_turn, plan_topic, refine_query,
                    resolve_timezone, summarise_articles, valid_timezone,
                    write_chat_answer)
from .db import Database
from .digest import DigestService
from .formatting import article_line, esc
from .news import NewsFetcher
from .scheduler import DigestScheduler

log = logging.getLogger(__name__)

STAGE = "onboarding_stage"
STAGE_TZ = "await_timezone"
STAGE_TIME = "await_time"

HELP_TEXT = """<b>Your digest arrives once a day</b> - the best of the last 24 hours on
your topics, ranked by how widely a story was reported.

<b>The rest of the time, just talk to me:</b>
• <i>"what's the market saying about Nvidia?"</i> - I'll go and read
• <i>"i like Manchester United"</i> - saves a topic
• <i>"stop sending me tennis"</i> - drops a topic
• <i>"send the digest at 7am"</i> - changes your delivery time

<b>Or use commands:</b>
/topics - what you follow
/add &lt;topic&gt; - follow something
/remove &lt;topic&gt; - stop following it
/retune &lt;topic&gt; - narrow what a topic searches for
/search &lt;query&gt; - search right now
/time &lt;HH:MM&gt; - set your daily digest time
/timezone &lt;city&gt; - set your timezone
/pause and /resume - mute or unmute the daily digest
/requests - (admins) who has asked to join
/users - (admins) who can use the bot
/status - your settings and today's API usage
/help - this message"""


_SKIP_ANSWERS = {
    "skip", "any", "anything", "all", "everything", "whatever", "dunno",
    "idk", "no preference", "doesn't matter", "does not matter", "don't mind",
    "no idea", "just save it", "leave it", "as is",
}


def _is_skip(text: str) -> bool:
    return text.strip().lower().rstrip("!.?") in _SKIP_ANSWERS


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
        app.add_handler(CommandHandler("retune", self.cmd_retune))
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
        app.add_handler(CommandHandler("requests", self.cmd_requests))
        app.add_handler(CommandHandler("users", self.cmd_users))
        app.add_handler(
            CallbackQueryHandler(self.on_access_decision, pattern=r"^access:")
        )
        app.add_handler(MessageHandler(filters.LOCATION, self.on_location))
        app.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, self.on_text)
        )
        app.add_error_handler(self.on_error)

    # ---------------------------------------------------------------- typing

    @staticmethod
    async def _typing(context, chat_id: int) -> None:
        """Show "typing...", and never let it matter.

        A bare `await send_chat_action(...)` is a network call, and on a
        flaky link it raises TimedOut - which aborted the handler before it
        answered, so the bot looked frozen over a cosmetic detail.
        """
        try:
            await context.bot.send_chat_action(chat_id, ChatAction.TYPING)
        except TelegramError as exc:
            log.debug("Could not show typing to %s: %s", chat_id, exc)

    # ------------------------------------------------------- access control

    async def _allowed(self, update: Update, context=None) -> bool:
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
        await self._request_access(update, context)
        return False

    # ------------------------------------------------------ access requests

    async def _request_access(self, update: Update, context) -> None:
        """Turn a stranger away, but ask the owner on their behalf."""
        user = update.effective_user
        message = update.effective_message
        existing = await self.db.get_access_request(user.id)

        if existing is not None and existing["status"] == "denied":
            # No second bite, and the owner is not asked again.
            await message.reply_text("This is a private bot.")
            return
        if existing is not None:
            await message.reply_text(
                "Still waiting on the owner - you'll hear back here."
            )
            return

        note = (message.text or "").strip()
        await self.db.raise_access_request(
            user.id, user.username, user.first_name, note
        )
        log.info("Access requested by %s (@%s, id %s)", user.first_name,
                 user.username, user.id)
        await message.reply_text(
            "This is a private bot, but I've asked the owner. "
            "You'll hear back here."
        )
        await self._notify_admins(context, user, note)

    async def _admin_ids(self) -> set[int]:
        return set(self.cfg.telegram.admins) | await self.db.admin_user_ids()

    async def _notify_admins(self, context, user, note: str) -> None:
        handle = f"@{user.username}" if user.username else "no username"
        body = (
            f"🙋 <b>{esc(user.first_name or 'Someone')}</b> ({esc(handle)}, "
            f"id <code>{user.id}</code>) wants access."
        )
        if note:
            body += f"\n\nThey said: <i>{esc(note[:300])}</i>"
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Approve",
                                 callback_data=f"access:approve:{user.id}"),
            InlineKeyboardButton("🚫 Deny",
                                 callback_data=f"access:deny:{user.id}"),
        ]])
        for admin_id in await self._admin_ids():
            try:
                await context.bot.send_message(
                    chat_id=admin_id, text=body, parse_mode=ParseMode.HTML,
                    reply_markup=keyboard,
                )
            except TelegramError as exc:
                log.warning("Could not reach admin %s: %s", admin_id, exc)

    async def on_access_decision(self, update: Update, context) -> None:
        """The Approve / Deny buttons. Only admins, and only once."""
        query = update.callback_query
        if not await self._is_admin(query.from_user.id):
            await query.answer("Admins only.", show_alert=True)
            return

        try:
            _, action, raw_id = query.data.split(":")
            target = int(raw_id)
        except ValueError:
            await query.answer("That button is stale.")
            return

        status = "approved" if action == "approve" else "denied"
        if not await self.db.decide_access_request(target, status,
                                                   query.from_user.id):
            # Another admin already pressed a button on their own copy.
            existing = await self.db.get_access_request(target)
            settled = existing["status"] if existing else "gone"
            await query.answer(f"Already {settled}.")
            await query.edit_message_reply_markup(reply_markup=None)
            return

        if status == "approved":
            await self.db.allow_user(target, query.from_user.id)
        await query.answer("Approved." if status == "approved" else "Denied.")

        by = query.from_user.first_name or query.from_user.id
        mark = "✅ Approved" if status == "approved" else "🚫 Denied"
        await query.edit_message_text(
            f"{query.message.text_html}\n\n{mark} by {esc(str(by))}",
            parse_mode=ParseMode.HTML, reply_markup=None,
        )

        await self._tell_decision(context, target, status == "approved")

    async def _tell_decision(self, context, user_id: int,
                             approved: bool) -> bool:
        """Let someone know either way. False if they couldn't be reached."""
        text = (
            "You're in. Say anything and I'll get you set up." if approved
            else "The owner didn't approve access, sorry."
        )
        try:
            await context.bot.send_message(chat_id=user_id, text=text)
            return True
        except TelegramError as exc:
            # Telegram won't let a bot open a conversation, so this is
            # normal for an id added by hand that never messaged first.
            log.warning("Could not tell %s the decision: %s", user_id, exc)
            return False

    async def _is_admin(self, user_id: int) -> bool:
        if user_id in set(self.cfg.telegram.admins):
            return True
        return user_id in await self.db.admin_user_ids()

    async def _ready_user(self, update: Update, context):
        """Whitelist check + user row + onboarding. None means stop here."""
        if not await self._allowed(update, context):
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
        if not await self._allowed(update, context):
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
                "Now tell me what you care about, however you'd say it out "
                "loud: <i>\"i like Manchester United\"</i>, <i>\"follow "
                "satellite launches\"</i>, <i>\"keep me posted on the "
                "ECB\"</i>.\n\n<i>/help if you ever want the full list.</i>",
                parse_mode=ParseMode.HTML,
            )
            return True

        return False

    # --------------------------------------------------------- text handling

    async def on_text(self, update: Update, context) -> None:
        if not await self._allowed(update, context):
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

        # A narrowing question is outstanding: this message is the answer,
        # unless they typed a command, which means they moved on.
        pending = await self.db.get_pending_topic(tg.id)
        if pending:
            await self.db.clear_pending_topic(tg.id)
            if not text.startswith("/"):
                await self._answer_pending_topic(update, context, pending, text)
                return

        # Mid-conversation: keep answering until they wind it up, unless
        # they typed a command, which is them changing the subject.
        chat = await self.db.get_chat(tg.id)
        if chat is not None:
            if not text.startswith("/"):
                await self._continue_chat(update, context, chat, text)
                return
            await self.db.end_chat(tg.id)

        await self._typing(context, tg.id)
        topics = await self.db.list_topics(tg.id)
        intent = await parse_intent(self.ai, text, topics=[t.label for t in topics])
        await self._dispatch(update, context, intent)

    async def _dispatch(self, update: Update, context, intent: Intent) -> None:
        user_id = update.effective_user.id
        action = intent.action

        if action == "add_topic" and intent.topic:
            await self._start_add_topic(update, context, intent.topic,
                                        intent.reply)
        elif action == "retune_topic" and intent.topic:
            await self._retune(update, context, intent.topic)
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
            await self._start_chat(update, context)
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

    async def _start_add_topic(self, update: Update, context, label: str,
                               reply: str | None = None) -> None:
        """Plan the query first; ask one narrowing question if it's too broad."""
        user_id = update.effective_user.id
        existing = await self.db.list_topics(user_id)
        if any(t.label.lower() == label.lower() for t in existing):
            await update.effective_message.reply_text(
                f"Already following <b>{esc(label)}</b>. "
                f"/retune {esc(label)} to narrow it.",
                parse_mode=ParseMode.HTML,
            )
            return

        await self._typing(context, user_id)
        plan = await plan_topic(self.ai, label)
        if plan.question:
            await self._ask_about_topic(update, label, plan.question, [])
            return
        await self._save_topic(update, plan.label, plan.query or label, reply)

    async def _ask_about_topic(self, update: Update, label: str, question: str,
                               transcript: list[dict]) -> None:
        await self.db.set_pending_topic(update.effective_user.id, label,
                                        question, transcript=transcript)
        # The way out is only worth spelling out the first time.
        hint = ("\n\n<i>Or say \"skip\" and I'll take it as it is.</i>"
                if not transcript else "")
        await update.effective_message.reply_text(
            f"{esc(question)}{hint}", parse_mode=ParseMode.HTML,
        )

    async def _answer_pending_topic(self, update: Update, context,
                                    pending: tuple, text: str) -> None:
        label, question, transcript = pending
        user_id = update.effective_user.id
        await self._typing(context, user_id)

        if _is_skip(text):
            answers = " ".join(str(t.get("a", "")) for t in transcript)
            plan = TopicPlan(label=label,
                             query=plain_query(label, answers or None))
        else:
            transcript = transcript + [{"q": question, "a": text}]
            plan = await plan_topic(self.ai, label, transcript=transcript)
            if plan.question and len(transcript) < MAX_TOPIC_QUESTIONS:
                await self._ask_about_topic(update, label, plan.question,
                                            transcript)
                return

        # A topic that already exists is being retuned, and keeps its own
        # label - renaming what someone follows out from under them is rude.
        existing = await self.db.list_topics(user_id)
        if any(t.label.lower() == label.lower() for t in existing):
            await self._save_topic(update, label, plan.query or label)
        else:
            await self._save_topic(update, plan.label, plan.query or label)

    async def _save_topic(self, update: Update, label: str, query: str,
                          reply: str | None = None) -> None:
        user_id = update.effective_user.id
        created = await self.db.add_topic(user_id, label, query)
        shown = f"\n<i>searching: {esc(query)}</i>"
        if not created:
            await self.db.update_topic_query(user_id, label, query)
            await update.effective_message.reply_text(
                f"\U0001f3af Retuned <b>{esc(label)}</b>.{shown}",
                parse_mode=ParseMode.HTML,
            )
            return
        note = reply or f"Following <b>{esc(label)}</b>."
        count = len(await self.db.list_topics(user_id))
        hint = ("\nYour digest lands each morning; ask me anything before "
                "then.") if count == 1 else ""
        await update.effective_message.reply_text(
            f"\u2705 {note} ({count} topic{'s' if count != 1 else ''})"
            f"{shown}{hint}",
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
            f"You're following:\n{lines}\n\n"
            "<i>Tell me if one is pulling in the wrong news.</i>",
            parse_mode=ParseMode.HTML,
        )

    async def _run_search(self, update: Update, context, query: str) -> None:
        await self._typing(context, update.effective_user.id)
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

    async def _start_chat(self, update: Update, context) -> None:
        """A digest on demand would be thin, so offer the real thing instead."""
        await self.db.start_chat(update.effective_user.id)
        await update.effective_message.reply_text(
            "A digest on the spot would be thin - the morning one is built "
            "out of a whole day of news. What do you want to know about?"
        )

    async def _continue_chat(self, update: Update, context,
                             history: list[dict], text: str) -> None:
        user_id = update.effective_user.id
        await self._typing(context, user_id)

        topics = await self.db.list_topics(user_id)
        turn = await plan_chat_turn(self.ai, history, text,
                                    topics=[t.label for t in topics])
        if turn.end:
            await self.db.end_chat(user_id)
            await update.effective_message.reply_text("Alright.")
            return

        await self.db.append_chat(user_id, "user", text)
        if turn.search:
            body, plain = await self._answer_from_news(history, text, turn.search)
        else:
            body = plain = turn.reply or "What about it?"

        await self.db.append_chat(user_id, "bot", plain)
        if notes := await self._learn_from_chat(user_id, turn):
            body += "\n\n" + "\n".join(f"<i>{note}</i>" for note in notes)
        await update.effective_message.reply_text(
            body, parse_mode=ParseMode.HTML, disable_web_page_preview=True
        )

    async def _learn_from_chat(self, user_id: int, turn) -> list[str]:
        """Let the conversation improve the topics. Returns lines to append.

        An explicit steer is acted on at once. A passing interest is only
        recorded: it has to keep coming up over more than a week before it
        changes anything, so one busy week for a company doesn't rewrite a
        query that then outlives the story.
        """
        topics = {t.label.lower(): t for t in await self.db.list_topics(user_id)}
        notes: list[str] = []

        if turn.steer:
            wanted = str(turn.steer.get("label") or "").strip().lower()
            change = str(turn.steer.get("change") or "").strip()
            topic = topics.get(wanted)
            if topic is not None and change:
                if note := await self._apply_refinement(user_id, topic, change):
                    notes.append(note)

        for interest in turn.interests:
            topic = topics.get(interest["label"].strip().lower())
            if topic is not None:
                await self.db.record_signal(user_id, topic.label,
                                            interest["phrase"])

        for label, phrase in await self.db.ripe_signals(user_id):
            await self.db.mark_signals_applied(user_id, label, phrase)
            topic = topics.get(label.lower())
            if topic is None:
                continue
            if note := await self._apply_refinement(user_id, topic,
                                                    f"add {phrase}"):
                notes.append(note)

        return notes

    async def _apply_refinement(self, user_id: int, topic, change: str):
        refined = await refine_query(self.ai, topic.label, topic.query, change)
        if refined is None:
            return None
        query, note = refined
        await self.db.update_topic_query(user_id, topic.label, query)
        log.info("Tightened %r for %s: %s", topic.label, user_id, query)
        return f"Tightened <b>{esc(topic.label)}</b> - {esc(note)}."

    async def _answer_from_news(self, history: list[dict], text: str,
                                query: str) -> tuple[str, str]:
        """(message to send, plain version for the transcript)."""
        articles, _source, window = await self.fetcher.widening_search(
            query, limit=4)
        if not articles:
            # No reporting is not the same as nothing to say.
            background = await background_answer(self.ai, text)
            if background:
                shown = (f"No recent news on <b>{esc(query)}</b>. Background, "
                         f"which may be out of date:\n\n{esc(background)}")
                return shown, background
            miss = (f"Nothing in the last month on <b>{esc(query)}</b>, and I "
                    "don't have much on it myself.")
            return miss, f"nothing found on {query}"

        summaries = await summarise_articles(self.ai, articles, attempts=1)
        for article, summary in zip(articles, summaries):
            article.summary = summary

        # Deliberately not marked as sent: a story worth discussing now is
        # still worth putting in tomorrow's digest.
        written = await write_chat_answer(self.ai, history, text, articles)
        if written:
            return link_citations(written, articles, escape=esc), written
        # No model: the articles themselves are still an answer.
        listed = "\n".join(article_line(a) for a in articles)
        return listed, listed

    async def _send_digest_now(self, update: Update, context) -> None:
        user = await self.db.get_user(update.effective_user.id)
        if user is None:
            return
        await self._typing(context, user.user_id)
        await self.digest.send_digest(context.bot, user, manual=True)

    # ------------------------------------------------------------ commands

    async def cmd_start(self, update: Update, context) -> None:
        if not await self._allowed(update, context):
            return
        tg = update.effective_user
        user = await self.db.ensure_user(
            tg.id, tg.username, tg.first_name,
            default_digest_time=self.cfg.digest.default_time,
        )
        if not user.timezone:
            await self._begin_onboarding(update, context)
            return
        topics = await self.db.list_topics(tg.id)
        following = ", ".join(esc(t.label) for t in topics)
        await update.effective_message.reply_text(
            f"Welcome back, {esc(tg.first_name or 'there')}."
            + (f"\n\nStill following: {following}." if following else "")
            + "\n\n<i>/help for what I can do.</i>",
            parse_mode=ParseMode.HTML,
        )

    async def cmd_help(self, update: Update, context) -> None:
        if not await self._allowed(update, context):
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
        await self._start_add_topic(update, context, topic)

    async def cmd_retune(self, update: Update, context) -> None:
        if not await self._ready_user(update, context):
            return
        await self._retune(update, context, " ".join(context.args).strip())

    async def _retune(self, update: Update, context, wanted: str) -> None:
        user_id = update.effective_user.id
        wanted = wanted.strip()
        topics = await self.db.list_topics(user_id)
        if not wanted or not topics:
            await update.effective_message.reply_text(
                "Usage: /retune finance - /topics lists what you follow."
            )
            return
        needle = wanted.lower()
        words = {w for w in re.findall(r"[a-z0-9]{4,}", needle)}
        match = (
            next((t for t in topics if t.label.lower() == needle), None)
            or next((t for t in topics if needle in t.label.lower()), None)
            # "the space topic" should still find "Satellites and Space".
            or next((t for t in topics
                     if words & set(re.findall(r"[a-z0-9]{4,}", t.label.lower()))),
                    None)
        )
        if match is None:
            await update.effective_message.reply_text(
                f"You're not following anything like \"{esc(wanted)}\".",
                parse_mode=ParseMode.HTML,
            )
            return

        await self._typing(context, user_id)
        plan = await plan_topic(self.ai, match.label)
        if plan.question:
            await self._ask_about_topic(update, match.label, plan.question, [])
            return
        await self._save_topic(update, match.label, plan.query or match.query)

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
        """Admin-only: the same ranked digest the morning run sends."""
        if not await self._ready_user(update, context):
            return
        if not await self._is_admin(update.effective_user.id):
            await update.effective_message.reply_text(
                "Your digest lands each morning. Ask me about anything in "
                "the meantime."
            )
            return
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
        if not await self._allowed(update, context):
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
        if not await self._allowed(update, context):
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

    async def cmd_users(self, update: Update, context) -> None:
        """Everyone who can use the bot, and where their access came from."""
        if not await self._allowed(update, context):
            return
        if not await self._is_admin(update.effective_user.id):
            await update.effective_message.reply_text("Admins only.")
            return

        config_ids = set(self.cfg.telegram.whitelist)
        admin_ids = await self._admin_ids()
        added = await self.db.allowed_with_dates()
        known = await self.db.known_users()

        lines = []
        for user_id in sorted(config_ids | admin_ids | set(added)):
            row = known.get(user_id)
            name = (row["first_name"] if row else None) or "Unknown"
            handle = f" (@{row['username']})" if row and row["username"] else ""
            tags = []
            if user_id in admin_ids:
                tags.append("admin")
            if user_id in config_ids:
                tags.append("in config.yaml")
            if user_id in added:
                tags.append(f"added {added[user_id][:10]}")
            if row is None:
                # On the list but has never messaged - usually a typo'd id.
                tags.append("never messaged")
            elif not row["digest_enabled"]:
                tags.append("digest paused")
            lines.append(
                f"• <b>{esc(name)}</b>{esc(handle)} - <code>{user_id}</code>"
                f"\n   <i>{esc(', '.join(tags))}</i>"
            )

        waiting = len(await self.db.list_access_requests(status="pending"))
        footer = (f"\n\n⏳ {waiting} waiting - /requests"
                  if waiting else "\n\n<i>/deny &lt;id&gt; removes someone.</i>")
        await update.effective_message.reply_text(
            f"👥 <b>{len(lines)} with access</b>\n\n" + "\n".join(lines) + footer,
            parse_mode=ParseMode.HTML,
        )

    async def cmd_requests(self, update: Update, context) -> None:
        """Who has asked for access, and what was decided."""
        if not await self._allowed(update, context):
            return
        if not await self._is_admin(update.effective_user.id):
            await update.effective_message.reply_text("Admins only.")
            return

        rows = await self.db.list_access_requests(limit=20)
        if not rows:
            await update.effective_message.reply_text("Nobody has asked yet.")
            return

        marks = {"pending": "⏳", "approved": "✅", "denied": "🚫"}
        lines = []
        for row in rows:
            handle = f"@{row['username']}" if row["username"] else row["user_id"]
            line = (f"{marks.get(row['status'], '?')} <b>"
                    f"{esc(row['first_name'] or 'Someone')}</b> ({esc(str(handle))})")
            if row["status"] == "pending":
                line += f"\n   /allow {row['user_id']}  ·  /deny {row['user_id']}"
            lines.append(line)

        pending = sum(1 for r in rows if r["status"] == "pending")
        header = f"{pending} waiting" if pending else "Nothing waiting"
        await update.effective_message.reply_text(
            f"<b>{header}</b>\n\n" + "\n".join(lines),
            parse_mode=ParseMode.HTML,
        )

    async def cmd_allow(self, update: Update, context) -> None:
        if not await self._allowed(update, context):
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
        await self.db.decide_access_request(new_id, "approved",
                                            update.effective_user.id)
        told = await self._tell_decision(context, new_id, approved=True)
        await update.effective_message.reply_text(
            f"✅ {new_id} can now use the bot, and I've told them."
            if told else
            f"✅ {new_id} can now use the bot - but I couldn't message them. "
            "Telegram only lets me reply, so ask them to message me first."
        )

    async def cmd_deny(self, update: Update, context) -> None:
        if not await self._allowed(update, context):
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
        decided = await self.db.decide_access_request(
            gone_id, "denied", update.effective_user.id
        )
        self.scheduler.cancel(gone_id)
        # Someone still waiting on an answer gets one; someone being removed
        # after months of use does not need a notification about it.
        if decided and not removed:
            await self._tell_decision(context, gone_id, approved=False)
        await update.effective_message.reply_text(
            f"Removed {gone_id}." if removed
            else f"{gone_id} wasn't in the runtime list "
                 "(config whitelist entries are removed in config.yaml)."
        )

    # --------------------------------------------------------------- errors

    async def on_error(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        error = context.error
        log.exception("Handler error", exc_info=error)
        if not (isinstance(update, Update) and update.effective_message):
            return
        # A dropped connection is worth naming: "something went wrong" sends
        # someone to the logs for what is usually a blip.
        # BadRequest subclasses NetworkError in PTB, and is a real bug -
        # blaming the connection for it would send us looking in the wrong
        # place.
        network = (isinstance(error, (NetworkError, httpx.HTTPError))
                   and not isinstance(error, BadRequest))
        text = ("I lost my connection for a moment - say that again?"
                if network
                else "Something went wrong on my end - it's logged. Try again?")
        try:
            await update.effective_message.reply_text(text)
        except Exception:  # noqa: BLE001
            pass
