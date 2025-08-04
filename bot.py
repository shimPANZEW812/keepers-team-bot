#!/usr/bin/env python3
"""
Telegram bot for Keepers Team application processing.

This script implements a Telegram bot that automates the intake and moderation of
applications from users wishing to join the Keepers Team. It does not rely on
the python-telegram-bot library; instead, it communicates with the Telegram
Bot API directly using HTTP requests. The bot supports long polling and
handles the full application flow, including user questionnaire, preview of
answers, confirmation, forwarding to a moderator chat, and moderator actions
to accept or reject the application with a reason.

Required environment variables:

  BOT_TOKEN:            Telegram bot token provided by BotFather.
  MODERATOR_CHAT_ID:    Numeric ID (with sign) of the moderator chat where
                        applications are sent. For private groups or supergroups
                        this typically starts with a '-' (e.g. "-1001234567890").
  CHANNEL_INVITE_LINK:  Invite link to the private channel users are granted
                        access to upon acceptance.

The bot stores user state in memory only; restarting the script will reset
active conversations. The bot uses HTML parse_mode for formatting messages.
"""

import os
import time
import html
from dataclasses import dataclass, field
from typing import Dict, List, Optional
import requests


# Fetch required configuration from environment variables.
BOT_TOKEN = os.environ.get("BOT_TOKEN")
MODERATOR_CHAT_ID = os.environ.get("MODERATOR_CHAT_ID")
CHANNEL_INVITE_LINK = os.environ.get("CHANNEL_INVITE_LINK")

# Validate configuration.
if not BOT_TOKEN or not MODERATOR_CHAT_ID or not CHANNEL_INVITE_LINK:
    raise RuntimeError(
        "Environment variables BOT_TOKEN, MODERATOR_CHAT_ID, and CHANNEL_INVITE_LINK must be set"
    )

API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"


def telegram_request(method: str, params: Optional[dict] = None) -> dict:
    """Send a request to the Telegram Bot API and return the JSON response.

    Args:
        method: The API method (e.g. "sendMessage").
        params: A dictionary of parameters to include in the request.

    Returns:
        The parsed JSON response.
    """
    url = f"{API_URL}/{method}"
    try:
        resp = requests.post(url, data=params, timeout=60)
        resp.raise_for_status()
        data = resp.json()
        if not data.get("ok"):
            # Print to stderr but continue raising an exception
            print(f"Telegram API returned an error: {data}")
        return data
    except Exception as exc:
        # Log the error but do not crash
        print(f"Error communicating with Telegram API: {exc}")
        return {"ok": False, "error": str(exc)}


@dataclass
class UserState:
    """Tracks the current state of a user's application process."""

    step: int = 0  # Which question is being asked (1-based index). 0 means not started.
    answers: List[str] = field(default_factory=list)  # Collected answers from the user.
    submitted: bool = False  # Whether the application has been sent for moderation.
    awaiting_user_confirmation: bool = False  # Waiting for user to confirm the summary.
    # The message_id of the summary message sent to the user (for editing buttons, optional)
    summary_message_id: Optional[int] = None


@dataclass
class PendingApplication:
    """Represents an application awaiting moderation."""

    user_id: int  # Telegram user ID of the applicant
    username: str  # Telegram username of the applicant (may be empty)
    answers: List[str]  # Collected answers from the applicant
    # message_id of the application message in the moderator chat
    moderator_message_id: Optional[int] = None
    # Whether the moderator has clicked accept or decline (awaiting reason)
    awaiting_reason: bool = False
    # ID of the moderator user who clicked decline (to match for reason input)
    declined_by: Optional[int] = None


class KeepersBot:
    """Core bot class encapsulating the long-polling loop and handlers."""

    def __init__(self):
        self.user_states: Dict[int, UserState] = {}
        self.pending_apps: Dict[int, PendingApplication] = {}
        self.last_update_id: Optional[int] = None

    def send_message(
        self,
        chat_id: int | str,
        text: str,
        parse_mode: str = "HTML",
        reply_markup: Optional[dict] = None,
        disable_notification: bool = False,
    ) -> dict:
        """Helper to send a message."""
        params = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": True,
            "disable_notification": disable_notification,
        }
        if reply_markup:
            params["reply_markup"] = reply_markup
        return telegram_request("sendMessage", params)

    def edit_message_reply_markup(
        self,
        chat_id: int | str,
        message_id: int,
        reply_markup: Optional[dict],
    ) -> dict:
        """Helper to edit the reply markup of a message."""
        params = {
            "chat_id": chat_id,
            "message_id": message_id,
            "reply_markup": reply_markup,
        }
        return telegram_request("editMessageReplyMarkup", params)

    def answer_callback_query(self, callback_query_id: str, text: str = "") -> None:
        """Answer callback queries to acknowledge button presses."""
        telegram_request(
            "answerCallbackQuery",
            {"callback_query_id": callback_query_id, "text": text, "show_alert": False},
        )

    def build_inline_keyboard(self, buttons: List[List[dict]]) -> dict:
        """Construct an inline keyboard markup for Telegram API."""
        return {"inline_keyboard": buttons}

    def get_updates(self) -> List[dict]:
        """Retrieve new updates from Telegram since the last processed update_id."""
        params = {
            "timeout": 60,  # long polling timeout
            "allowed_updates": ["message", "callback_query"],
        }
        if self.last_update_id is not None:
            params["offset"] = self.last_update_id + 1
        resp = telegram_request("getUpdates", params)
        if resp.get("ok"):
            return resp.get("result", [])
        return []

    def start_questionnaire(self, user_id: int) -> None:
        """Initiate questionnaire for a user."""
        state = self.user_states.setdefault(user_id, UserState())
        state.step = 1
        state.answers.clear()
        state.submitted = False
        state.awaiting_user_confirmation = False
        state.summary_message_id = None
        # Send greeting
        greeting = (
            "Приветствуем тебя! С тобой бот Keepers Team.\n\n"
            "Мы открыли набор в нашу команду, работающую в сфере NFT-подарков через Telegram.\n"
            "Уже сейчас ты можешь начать зарабатывать на одном из самых перспективных направлений.\n\n"
            "🔺 Мы предлагаем одни из лучших условий на рынке:\n\n"
            "— 60% от оценки скупа — твоя чистая прибыль.\n"
            "Для ТОП-воркеров предусмотрен индивидуальный процент и бонусные условия.\n\n"
            "— Пошаговые мануалы, основанные на реальном опыте.\n"
            "Также доступны обучающие методички.\n\n"
            "— Постоянная поддержка от ТОПОВ.\n\n"
            "📈 Благодаря нашей системе распределения процентов ты сможешь выстроить пассивный доход без ограничений — всё зависит только от твоего желания и активности.\n\n"
            "👥 Уже создавал или планируешь собрать собственную команду?\n"
            "Для филиалов и опытных воркеров — особые условия сотрудничества и поддержка на старте."
        )
        # Ask first question after greeting
        self.send_message(user_id, greeting)
        time.sleep(0.2)  # slight delay to ensure ordering
        self.ask_next_question(user_id)

    def ask_next_question(self, user_id: int) -> None:
        """Send the next questionnaire question based on the user's current step."""
        state = self.user_states[user_id]
        questions = [
            "Сколько вам лет?",
            (
                "Уже работал в этой сфере?\n"
                "Если да — где и с каким капиталом?\n"
                "Если нет — расскажи, в каких сферах у тебя был опыт"
            ),
            "Готовы ли вы вложить 10–35 $ на оплату расходников?",
            "Ссылка на форум или источник, откуда вы о нас узнали",
        ]
        if 1 <= state.step <= len(questions):
            self.send_message(user_id, questions[state.step - 1])
        else:
            # Out of range; ignore
            pass

    def present_summary(self, user_id: int) -> None:
        """Present the filled questionnaire to the user for confirmation."""
        state = self.user_states[user_id]
        # Build summary text with answers enumerated starting from 1
        lines = []
        for idx, ans in enumerate(state.answers, start=1):
            # Escape HTML characters to prevent injection
            escaped = html.escape(ans)
            lines.append(f"{idx}. {escaped}")
        summary_text = "\n".join(lines) if lines else "(пусто)"
        # Buttons: Accept to submit, Decline to restart
        buttons = [
            [
                {"text": "✅ Принять", "callback_data": f"user_accept:{user_id}"},
                {"text": "❌ Отклонить", "callback_data": f"user_decline:{user_id}"},
            ]
        ]
        reply_markup = self.build_inline_keyboard(buttons)
        resp = self.send_message(
            user_id,
            summary_text + "\n\nПожалуйста, убедитесь, что заявка заполнена корректно.",
            reply_markup=reply_markup,
        )
        if resp.get("ok"):
            state.awaiting_user_confirmation = True
            state.summary_message_id = resp["result"]["message_id"]

    def handle_user_message(self, update: dict) -> None:
        """Handle a standard message from a user or moderator."""
        message = update.get("message") or update.get("edited_message")
        if not message:
            return
        chat_id = message["chat"]["id"]
        user = message.get("from", {})
        user_id = user.get("id")
        text = message.get("text", "").strip()

        # If message is from moderator chat and awaiting a reason for a user rejection
        if str(chat_id) == MODERATOR_CHAT_ID:
            # Look for any pending application awaiting reason
            for app in list(self.pending_apps.values()):
                if app.awaiting_reason and app.declined_by == user_id:
                    reason = text if text else "Без объяснения причины"
                    # Send rejection to user
                    rejection_text = (
                        "🚫 <b>Ваша заявка была отклонена. Причина:</b>\n"
                        f"{html.escape(reason)}\n\n"
                        "Если хотите подать заявку повторно, удалите переписку с ботом и начните сначала.\n"
                        "(Если бот не отвечает — убедитесь, что он не в чёрном списке.)"
                    )
                    self.send_message(app.user_id, rejection_text)
                    # Mark as not pending
                    self.pending_apps.pop(app.user_id, None)
                    # Edit moderator message to remove buttons
                    if app.moderator_message_id:
                        self.edit_message_reply_markup(
                            MODERATOR_CHAT_ID,
                            app.moderator_message_id,
                            reply_markup={"inline_keyboard": []},
                        )
                    # Notify moderator
                    self.send_message(
                        MODERATOR_CHAT_ID,
                        f"Заявка пользователя @{app.username or 'user'+str(app.user_id)} отклонена.",
                    )
                    return

            # If no application awaiting reason, ignore moderator chat messages
            return

        # Non-moderator chat message: treat as applicant
        # Initialize state if not exists
        state = self.user_states.get(user_id)
        if not state:
            # If message is /start
            if text.lower() == "/start":
                self.start_questionnaire(user_id)
            else:
                # Prompt to start
                self.send_message(
                    chat_id,
                    "Для подачи заявки отправьте команду /start.",
                )
            return

        # If user already submitted and not awaiting new application
        if state.submitted:
            # Always respond with on hold message
            self.send_message(
                chat_id,
                "Ваша заявка отправлена на рассмотрение. "
                "Ментор ответит вам, как только примет решение.\n"
                "Повторная подача заявки разрешена только после очистки истории диалога с ботом.",
            )
            return

        # If waiting for user confirmation and user sends something other than buttons
        if state.awaiting_user_confirmation:
            # Instruct to use buttons
            self.send_message(
                chat_id,
                "Пожалуйста, используйте кнопки ниже, чтобы подтвердить или отменить заявку.",
            )
            return

        # If step is in range of questions
        if 1 <= state.step <= 4:
            # Save the answer
            state.answers.append(text)
            state.step += 1
            if state.step <= 4:
                # Ask the next question in the sequence
                self.ask_next_question(user_id)
            else:
                # Completed all questions: show summary and stop further processing
                self.present_summary(user_id)
                # Once the summary is presented we don't want to fall through and
                # accidentally prompt the user to restart. Return early to
                # ensure no additional messages are sent in this handler call.
                return
        else:
            # If step is 0 or >4 and message not recognized
            # Do not reset the user state automatically here. Just prompt
            # them to start the questionnaire if they haven't already.
            self.send_message(
                chat_id,
                "Отправьте /start для начала анкеты."
            )

    def handle_callback_query(self, update: dict) -> None:
        """Process callback queries from inline keyboards."""
        callback_query = update.get("callback_query")
        if not callback_query:
            return
        query_id = callback_query["id"]
        data = callback_query.get("data", "")
        from_user = callback_query.get("from", {})
        user_id = from_user.get("id")

        # Acknowledge callback to remove the loading state
        self.answer_callback_query(query_id)

        # Parse callback data
        if data.startswith("user_accept:"):
            try:
                applicant_id = int(data.split(":", 1)[1])
            except ValueError:
                return
            # Only handle if this is the same applicant
            if user_id != applicant_id:
                # Not expected: ignore silently
                return
            state = self.user_states.get(applicant_id)
            if not state or state.submitted:
                return
            # Mark as submitted
            state.submitted = True
            state.awaiting_user_confirmation = False
            # Remove buttons from user summary message
            if state.summary_message_id:
                self.edit_message_reply_markup(
                    applicant_id,
                    state.summary_message_id,
                    reply_markup={"inline_keyboard": []},
                )
            # Send application to moderator chat
            username = from_user.get("username", "")
            lines = []
            for idx, ans in enumerate(state.answers, start=1):
                lines.append(f"{idx}. {html.escape(ans)}")
            app_text = "\n".join(lines) if lines else "(пусто)"
            message_text = (
                f"📌 Новая заявка от @{username or 'user'+str(applicant_id)}:\n\n"
                f"{app_text}"
            )
            buttons = [
                [
                    {"text": "✅ Принять", "callback_data": f"mod_accept:{applicant_id}"},
                    {"text": "❌ Отклонить", "callback_data": f"mod_decline:{applicant_id}"},
                ]
            ]
            reply_markup = self.build_inline_keyboard(buttons)
            resp = self.send_message(MODERATOR_CHAT_ID, message_text, reply_markup=reply_markup)
            mod_msg_id = resp.get("result", {}).get("message_id") if resp.get("ok") else None
            # Track pending application
            self.pending_apps[applicant_id] = PendingApplication(
                user_id=applicant_id,
                username=username or "",
                answers=state.answers.copy(),
                moderator_message_id=mod_msg_id,
                awaiting_reason=False,
            )
            # Inform applicant that submission is sent
            self.send_message(
                applicant_id,
                "Ваша заявка отправлена на рассмотрение. "
                "Ментор ответит вам, как только примет решение."
            )
            return

        if data.startswith("user_decline:"):
            # Applicant declined their own summary; reset state
            try:
                applicant_id = int(data.split(":", 1)[1])
            except ValueError:
                return
            if user_id != applicant_id:
                return
            state = self.user_states.get(applicant_id)
            if not state or state.submitted:
                return
            # Edit reply markup to remove buttons
            if state.summary_message_id:
                self.edit_message_reply_markup(
                    applicant_id,
                    state.summary_message_id,
                    reply_markup={"inline_keyboard": []},
                )
            # Reset state to start over
            self.user_states[applicant_id] = UserState()
            self.send_message(
                applicant_id,
                "Анкета отменена. Если хотите подать заявку заново, отправьте команду /start."
            )
            return

        if data.startswith("mod_accept:"):
            try:
                applicant_id = int(data.split(":", 1)[1])
            except ValueError:
                return
            # Only proceed if there is a pending application
            app = self.pending_apps.get(applicant_id)
            if not app:
                return
            # Remove pending to avoid duplicate decisions
            self.pending_apps.pop(applicant_id, None)
            # Edit moderator message to remove buttons
            if app.moderator_message_id:
                self.edit_message_reply_markup(
                    MODERATOR_CHAT_ID,
                    app.moderator_message_id,
                    reply_markup={"inline_keyboard": []},
                )
            # Send acceptance message to user
            acceptance_text = (
                "🎉 <b>Удачной игры!</b>\n#blood_play 🩸🎮\n\n"
                f"{html.escape(CHANNEL_INVITE_LINK)}"
            )
            self.send_message(applicant_id, acceptance_text)
            # Notify moderator chat
            self.send_message(
                MODERATOR_CHAT_ID,
                f"Заявка пользователя @{app.username or 'user'+str(applicant_id)} принята."
            )
            return

        if data.startswith("mod_decline:"):
            try:
                applicant_id = int(data.split(":", 1)[1])
            except ValueError:
                return
            app = self.pending_apps.get(applicant_id)
            if not app:
                return
            # Mark awaiting reason
            app.awaiting_reason = True
            app.declined_by = user_id
            # Ask moderator to provide reason via next message
            self.send_message(
                MODERATOR_CHAT_ID,
                "Пожалуйста, введите причину отказа:"
            )
            return

    def run(self) -> None:
        """Main loop: continuously poll for updates and dispatch them."""
        print("Keepers Bot started. Waiting for updates...")
        while True:
            updates = self.get_updates()
            for update in updates:
                # Track last update id to avoid re-processing
                self.last_update_id = update["update_id"]
                if "message" in update or "edited_message" in update:
                    self.handle_user_message(update)
                elif "callback_query" in update:
                    self.handle_callback_query(update)
            # Throttle to avoid rapid looping in case of no updates
            time.sleep(0.5)


if __name__ == "__main__":
    bot = KeepersBot()
    bot.run()
