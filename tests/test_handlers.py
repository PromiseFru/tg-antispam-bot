from __future__ import annotations

import datetime as dt
import time

import pytest
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
)
from aiogram.methods import (
    AnswerCallbackQuery,
    BanChatMember,
    DeleteMessage,
    DeleteMessages,
    EditEphemeralMessageText,
    EditMessageText,
    GetChat,
    GetChatAdministrators,
    GetChatMember,
    RestrictChatMember,
    SendMessage,
    SendRichMessage,
    UnbanChatMember,
)
from aiogram.types import (
    CallbackQuery,
    Chat,
    ChatMemberLeft,
    ChatMemberMember,
    ChatMemberUpdated,
    ForumTopicCreated,
    Message,
    MessageEntity,
    User,
)

from conftest import (
    ADMIN_ID,
    BOT_ID,
    CHAT_ID,
    DHM_SPAM_TEXT,
    MEMBER_ID,
    OTHER_ALLOWED_CHAT_ID,
    OWNER_ID,
    SPAMMER_ID,
    administrator,
    bot_member_event,
    card_text,
    full_chat,
    log_action,
    make_chat,
    make_message,
    make_user,
    tables,
    verbs,
)
from tg_antispam_bot.db import ACTION_BAN, ACTION_DELETE, ACTION_RESTRICT
from tg_antispam_bot.render import (
    SURFACE_DM,
    SURFACE_EPHEMERAL,
    ActionCb,
    CardCb,
    DigestListCb,
    DismissCb,
    LogPageCb,
    LogViewCb,
    PrefCb,
    SubCb,
)

NOW = int(time.time())
ALL_COMMANDS = [
    "/antispam off",
    "/status",
    "/log",
    "/threshold ban 1",
    "/addrule text_pattern x",
    "/delrule mention_pattern ^dhm_",
    "/trust 5",
    "/untrust 5",
    "/raidmode on",
    "/setupalerts",
]
PRIVATE_CHAT_ID = ADMIN_ID


def now_utc():
    return dt.datetime.now(tz=dt.UTC)


def command_message(text, user_id=ADMIN_ID, message_id=700, **kwargs):
    return make_message(
        text,
        user=make_user(user_id, f"User{user_id}"),
        message_id=message_id,
        **kwargs,
    )


def private_message(text, user_id=ADMIN_ID):
    chat = Chat(id=user_id, type="private")
    return command_message(text, user_id=user_id, chat=chat)


def sends(session):
    return session.calls(SendMessage)


def rich_sends(session):
    return session.calls(SendRichMessage)


def view_links(method):
    found = []

    def walk(value):
        if isinstance(value, dict):
            data = value.get("callback_data")
            if isinstance(data, str) and data.startswith("lgv:"):
                found.append(data)
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(method.rich_message.model_dump())
    return found


def replies(session):
    return [*sends(session), *rich_sends(session)]


def callback(data, user_id=ADMIN_ID, message=None):
    return CallbackQuery(
        id="cb1",
        from_user=make_user(user_id, f"User{user_id}"),
        chat_instance="x",
        data=data,
        message=message,
    )


class TestAuthorization:
    @pytest.mark.parametrize("text", ALL_COMMANDS)
    async def test_non_admin_gets_no_reply_and_no_change(
        self, feed, session, db, text
    ):
        await feed(message=command_message(text, user_id=MEMBER_ID))
        assert replies(session) == []
        assert session.calls(DeleteMessage) == []
        assert await db.get_settings(CHAT_ID) == {}
        assert not await db.is_trusted(CHAT_ID, 5)
        chat = await db.get_chat(CHAT_ID)
        assert chat.raid_until == 0

    async def test_non_admin_command_is_still_scanned_as_spam(
        self, feed, session
    ):
        text = "/status " + DHM_SPAM_TEXT
        message = make_message(
            text,
            user=make_user(SPAMMER_ID),
            entities=[
                MessageEntity(type="bot_command", offset=0, length=7),
                *(make_message(text).entities or []),
            ],
        )
        await feed(message=message)
        assert sends(session) == []
        assert session.calls(DeleteMessages)[0].message_ids == [1000]
        assert session.calls(BanChatMember)[0].user_id == SPAMMER_ID

    @pytest.mark.parametrize("text", ALL_COMMANDS)
    async def test_admin_reply_targets_only_the_invoker(
        self, feed, session, text
    ):
        await feed(message=command_message(text))
        sent = replies(session)
        assert len(sent) == 1
        parameters = sent[0].ephemeral_message_parameters
        assert parameters is not None
        assert parameters.receiver_user_id == ADMIN_ID
        assert sent[0].chat_id == CHAT_ID

    async def test_outage_during_admin_check_gives_no_reply(
        self, feed, session
    ):
        session.on(
            GetChatAdministrators, lambda m: TelegramNetworkError(m, "down")
        )
        await feed(message=command_message("/status"))
        assert replies(session) == []

    async def test_command_addressed_to_another_bot_is_ignored(
        self, feed, session
    ):
        await feed(message=command_message("/status@other_bot"))
        assert replies(session) == []


class TestCommandDelivery:
    async def test_normal_command_is_deleted_after_processing(
        self, feed, session
    ):
        await feed(message=command_message("/status", message_id=701))
        assert [call.message_id for call in session.calls(DeleteMessage)] == [
            701
        ]
        kinds = [type(request).__name__ for request in session.requests]
        assert kinds.index("SendRichMessage") < kinds.index("DeleteMessage")

    async def test_command_is_deleted_when_reply_fails(self, feed, session):
        session.on(SendRichMessage, lambda m: TelegramForbiddenError(m, "no"))
        await feed(message=command_message("/log", message_id=702))
        assert [call.message_id for call in session.calls(DeleteMessage)] == [
            702
        ]

    async def test_ephemeral_command_replies_to_ephemeral_id_and_is_not_deleted(
        self, feed, session
    ):
        await feed(
            message=command_message(
                "/status", message_id=0, ephemeral_message_id=33
            )
        )
        reply = rich_sends(session)[0]
        assert reply.reply_parameters.ephemeral_message_id == 33
        assert reply.ephemeral_message_parameters.receiver_user_id == ADMIN_ID
        assert session.calls(DeleteMessage) == []

    async def test_normal_command_has_no_reply_parameters(self, feed, session):
        await feed(message=command_message("/status"))
        assert rich_sends(session)[0].reply_parameters is None

    async def test_anonymous_admin_command_is_deleted_without_effect(
        self, feed, session, db
    ):
        anon = User(id=1087968824, is_bot=True, first_name="Group")
        entities = [MessageEntity(type="bot_command", offset=0, length=9)]
        message = make_message(
            "/raidmode on",
            user=anon,
            sender_chat=make_chat(),
            entities=entities,
            message_id=703,
        )
        await feed(message=message)
        assert sends(session) == []
        assert [call.message_id for call in session.calls(DeleteMessage)] == [
            703
        ]
        assert (await db.get_chat(CHAT_ID)).raid_until == 0

    async def test_ephemeral_command_from_non_admin_is_ignored(
        self, feed, session
    ):
        await feed(
            message=command_message(
                "/status",
                user_id=MEMBER_ID,
                message_id=0,
                ephemeral_message_id=34,
            )
        )
        assert replies(session) == []
        assert session.calls(DeleteMessages) == []


class TestAdminCommands:
    async def test_antispam_off_stops_scanning(self, feed, session):
        await feed(message=command_message("/antispam off"))
        session.clear()
        await feed(
            message=make_message(DHM_SPAM_TEXT, user=make_user(SPAMMER_ID))
        )
        assert session.calls(DeleteMessages) == []

    async def test_antispam_alerts_toggle(self, feed, services):
        await feed(message=command_message("/antispam alerts on"))
        rules = (await services.rules.get(CHAT_ID)).rules
        assert rules.notifications.live_ephemeral_alerts is True

    async def test_threshold_updates_tier(self, feed, services, session):
        await feed(message=command_message("/threshold ban 90"))
        assert (
            await services.rules.get(CHAT_ID)
        ).rules.tiers.delete_and_ban == 90
        assert "set to 90" in sends(session)[0].text

    async def test_threshold_rejects_inverted_order(
        self, feed, services, session
    ):
        await feed(message=command_message("/threshold delete 500"))
        assert (await services.rules.get(CHAT_ID)).rules.tiers.delete == 50
        assert "must satisfy" in sends(session)[0].text

    async def test_threshold_rejects_garbage(self, feed, session):
        await feed(message=command_message("/threshold nope"))
        assert "Usage" in sends(session)[0].text

    async def test_addrule_takes_effect(self, feed, services):
        await feed(message=command_message("/addrule mention_pattern ^spm_"))
        patterns = (
            await services.rules.get(CHAT_ID)
        ).rules.detectors.mention_pattern.patterns
        assert "^spm_" in patterns

    async def test_addrule_rejects_invalid_regex(self, feed, services, session):
        await feed(message=command_message("/addrule text_pattern (a"))
        patterns = (
            await services.rules.get(CHAT_ID)
        ).rules.detectors.text_pattern.patterns
        assert "(a" not in patterns
        assert "invalid regex" in sends(session)[0].text

    async def test_addrule_rejects_non_pattern_detector(self, feed, session):
        await feed(message=command_message("/addrule raid x"))
        assert "must be one of" in sends(session)[0].text

    async def test_addrule_reply_escapes_pattern(self, feed, session):
        await feed(message=command_message("/addrule text_pattern <b>"))
        assert "&lt;b&gt;" in sends(session)[0].text

    async def test_delrule_removes_default_and_added(self, feed, services):
        await feed(message=command_message("/delrule mention_pattern ^dhm_"))
        await feed(message=command_message("/addrule mention_pattern ^spm_"))
        await feed(message=command_message("/delrule mention_pattern ^spm_"))
        patterns = (
            await services.rules.get(CHAT_ID)
        ).rules.detectors.mention_pattern.patterns
        assert patterns == []

    async def test_delrule_then_addrule_restores_default(self, feed, services):
        await feed(message=command_message("/delrule mention_pattern ^dhm_"))
        await feed(message=command_message("/addrule mention_pattern ^dhm_"))
        patterns = (
            await services.rules.get(CHAT_ID)
        ).rules.detectors.mention_pattern.patterns
        assert patterns == ["^dhm_"]

    async def test_delrule_without_args_lists_patterns(self, feed, session):
        await feed(message=command_message("/delrule"))
        assert "^dhm_" in sends(session)[0].text

    async def test_trust_by_id_and_untrust(self, feed, db):
        await feed(message=command_message("/trust 555"))
        assert await db.is_trusted(CHAT_ID, 555)
        await feed(message=command_message("/untrust 555"))
        assert not await db.is_trusted(CHAT_ID, 555)

    async def test_trust_by_reply(self, feed, db):
        replied = make_message("hi", user=make_user(556), message_id=9)
        await feed(message=command_message("/trust", reply_to_message=replied))
        assert await db.is_trusted(CHAT_ID, 556)

    async def test_trust_without_target_shows_usage(self, feed, session):
        await feed(message=command_message("/trust"))
        assert "Reply to a message" in sends(session)[0].text

    async def test_raidmode_on_then_off(self, feed, services):
        await feed(message=command_message("/raidmode on"))
        assert await services.engine.raid_active(CHAT_ID)
        await feed(message=command_message("/raidmode off"))
        assert not await services.engine.raid_active(CHAT_ID)

    async def test_setupalerts_has_deep_link(self, feed, session):
        await feed(message=command_message("/setupalerts"))
        reply = sends(session)[0]
        expected = f"https://t.me/dekuspam_bot?start=sub_{CHAT_ID}"
        assert expected in reply.text
        assert reply.reply_markup.inline_keyboard[0][0].url == expected

    async def test_status_reports_everything(self, feed, session, db, services):
        session.on(
            GetChatMember,
            lambda m: administrator(
                User(id=BOT_ID, is_bot=True, first_name="B")
            ),
        )
        session.set_admins(CHAT_ID, OWNER_ID, ADMIN_ID, 102, 103)
        await db.set_subscription(CHAT_ID, ADMIN_ID, True, NOW)
        await db.mark_started(102, NOW)
        await db.set_subscription(CHAT_ID, 103, True, NOW)
        await db.mark_user_blocked(103, NOW)
        await db.set_subscription(CHAT_ID, ADMIN_ID, True, NOW)
        await feed(message=command_message("/status"))
        card = rich_sends(session)[0]
        settings, thresholds, admins = tables(card.rich_message)
        rights = {row[0].text.text: row[1].text for row in settings.cells}
        assert "missing" not in rights["Bot rights"]
        assert [row[1].text for row in thresholds.cells[1:]] == [
            "50 pts",
            "70 pts",
            "100 pts",
        ]
        states = {row[0].text.user.id: row[1].text for row in admins.cells[1:]}
        assert "Not subscribed" in states[102]
        assert "Subscribed" in states[ADMIN_ID]
        assert "Not subscribed" not in states[ADMIN_ID]
        assert "Blocked" in states[103]
        assert "Not started" in states[OWNER_ID]
        assert BOT_ID not in states


class TestLog:
    async def test_log_rows_open_their_action(self, feed, session, db):
        action = await log_action(db)
        await feed(message=command_message("/log"))
        reply = rich_sends(session)[0]
        assert view_links(reply) == [f"lgv:{CHAT_ID}:{action.id}:0:5"]
        assert reply.reply_markup is None

    async def test_log_limits_entries(self, feed, session, db):
        for _ in range(4):
            await log_action(db)
        await feed(message=command_message("/log 2"))
        reply = rich_sends(session)[0]
        assert len(view_links(reply)) == 2
        [[older]] = reply.reply_markup.inline_keyboard
        assert older.callback_data == f"lgp:{CHAT_ID}:1:2"

    async def test_log_older_page_edits_in_place(self, feed, session, db):
        actions = [await log_action(db) for _ in range(3)]
        data = LogPageCb(chat_id=CHAT_ID, page=1, size=2).pack()
        message = Message(
            message_id=0,
            date=now_utc(),
            chat=make_chat(),
            ephemeral_message_id=44,
        )
        await feed(callback_query=callback(data, message=message))
        edit = session.calls(EditEphemeralMessageText)[0]
        assert view_links(edit) == [f"lgv:{CHAT_ID}:{actions[0].id}:1:2"]

    async def test_log_page_needs_an_admin(self, feed, session, db):
        await log_action(db)
        data = LogPageCb(chat_id=CHAT_ID, page=0, size=2).pack()
        await feed(callback_query=callback(data, user_id=MEMBER_ID))
        assert session.calls(EditEphemeralMessageText) == []
        assert rich_sends(session) == []

    async def test_log_only_shows_this_chat(self, feed, session, db):
        await log_action(
            db, chat_id=-555, user_id=1, user_name="Other", action=ACTION_DELETE
        )
        await feed(message=command_message("/log"))
        assert "No actions recorded" in card_text(rich_sends(session)[0])

    def ephemeral_callback(
        self, action, verb="unban", user_id=ADMIN_ID, ephemeral_id=44
    ):
        message = make_message(
            "log",
            user=User(id=BOT_ID, is_bot=True, first_name="B"),
            message_id=0,
            ephemeral_message_id=ephemeral_id,
        )
        data = ActionCb(
            action_id=action.id, verb=verb, surface=SURFACE_EPHEMERAL
        ).pack()
        return callback(data, user_id=user_id, message=message)

    async def test_press_edits_same_ephemeral_message(self, feed, session, db):
        action = await log_action(db)
        await feed(callback_query=self.ephemeral_callback(action))
        edit = session.calls(EditEphemeralMessageText)[0]
        assert edit.ephemeral_message_id == 44
        assert edit.receiver_user_id == ADMIN_ID
        assert edit.chat_id == CHAT_ID
        assert "Unbanned" in card_text(edit)
        assert "User101" in card_text(edit)
        [[back]] = edit.reply_markup.inline_keyboard
        assert back.callback_data == f"lgp:{CHAT_ID}:0:5"
        assert session.calls(UnbanChatMember)[0].user_id == SPAMMER_ID
        assert replies(session) == []

    def view_callback(self, action_id, user_id=ADMIN_ID, chat_id=CHAT_ID):
        message = make_message(
            "log",
            user=User(id=BOT_ID, is_bot=True, first_name="B"),
            message_id=0,
            ephemeral_message_id=44,
        )
        data = LogViewCb(
            chat_id=chat_id, action_id=action_id, page=2, size=3
        ).pack()
        return callback(data, user_id=user_id, message=message)

    async def test_view_replaces_log_with_actions_and_back(
        self, feed, session, db
    ):
        action = await log_action(db)
        await feed(callback_query=self.view_callback(action.id))
        edit = session.calls(EditEphemeralMessageText)[0]
        assert edit.ephemeral_message_id == 44
        rows = edit.reply_markup.inline_keyboard
        verbs = [
            button.callback_data.split(":")[2]
            for row in rows[:-1]
            for button in row
        ]
        assert verbs == ["unban", "trust", "fp", "confirm"]
        assert rows[-1][0].callback_data == f"lgp:{CHAT_ID}:2:3"

    async def test_view_needs_an_admin(self, feed, session, db):
        action = await log_action(db)
        await feed(callback_query=self.view_callback(action.id, MEMBER_ID))
        assert session.calls(EditEphemeralMessageText) == []

    async def test_view_refuses_an_action_from_another_chat(
        self, feed, session, db
    ):
        other = await log_action(
            db,
            chat_id=OTHER_ALLOWED_CHAT_ID,
            user_id=1,
            user_name="Other",
            message_text="secret",
            action=ACTION_DELETE,
        )
        await feed(callback_query=self.view_callback(other.id))
        assert session.calls(EditEphemeralMessageText) == []
        assert replies(session) == []

    async def test_press_without_ephemeral_id_falls_back_to_new_ephemeral(
        self, feed, session, db
    ):
        action = await log_action(db)
        data = ActionCb(
            action_id=action.id, verb="unban", surface=SURFACE_EPHEMERAL
        ).pack()
        await feed(callback_query=callback(data))
        assert session.calls(EditEphemeralMessageText) == []
        sent = rich_sends(session)[0]
        assert sent.ephemeral_message_parameters.callback_query_id == "cb1"
        assert sent.ephemeral_message_parameters.receiver_user_id == ADMIN_ID


class TestResolution:
    def dm_callback(
        self, action, verb="unban", user_id=ADMIN_ID, message_id=8000
    ):
        message = make_message(
            "card",
            chat=Chat(id=user_id, type="private"),
            user=User(id=BOT_ID, is_bot=True, first_name="B"),
            message_id=message_id,
        )
        data = ActionCb(
            action_id=action.id, verb=verb, surface=SURFACE_DM
        ).pack()
        return callback(data, user_id=user_id, message=message)

    async def test_confirm_keeps_ban_and_closes_incident(
        self, feed, session, db
    ):
        earlier = await log_action(db, action=ACTION_DELETE)
        action = await log_action(db)
        await feed(callback_query=self.dm_callback(action, verb="confirm"))
        assert session.calls(UnbanChatMember) == []
        assert session.calls(RestrictChatMember) == []
        for record in (earlier, action):
            stored = await db.get_action(record.id)
            assert stored.resolution == "confirm"
        assert not await db.is_trusted(CHAT_ID, SPAMMER_ID)
        edit = session.calls(EditMessageText)[0]
        assert "Confirmed" in card_text(edit)
        [[dismiss]] = edit.reply_markup.inline_keyboard
        assert dismiss.callback_data.startswith("dis:")

    async def test_unban_lifts_ban_and_shows_resolver(self, feed, session, db):
        action = await log_action(db)
        await feed(callback_query=self.dm_callback(action))
        unban = session.calls(UnbanChatMember)[0]
        assert unban.user_id == SPAMMER_ID
        assert unban.only_if_banned is True
        edit = session.calls(EditMessageText)[0]
        assert edit.text is None
        assert "User101" in card_text(edit)
        assert "Unbanned" in card_text(edit)
        [[dismiss]] = edit.reply_markup.inline_keyboard
        assert dismiss.callback_data == f"dis:{action.id}"

    async def test_non_admin_presser_is_rejected(self, feed, session, db):
        action = await log_action(db)
        await feed(callback_query=self.dm_callback(action, user_id=MEMBER_ID))
        assert session.calls(UnbanChatMember) == []
        assert session.calls(EditMessageText) == []
        assert session.calls(AnswerCallbackQuery)[0].show_alert is True
        assert not (await db.get_action(action.id)).is_resolved

    async def test_demoted_admin_is_rejected_despite_cache(
        self, feed, session, db, services
    ):
        action = await log_action(db)
        await services.admins.get_admins(CHAT_ID)
        session.set_admins(CHAT_ID, OWNER_ID)
        await feed(callback_query=self.dm_callback(action))
        assert session.calls(UnbanChatMember) == []

    async def test_trust_adds_trusted_and_lifts(self, feed, session, db):
        action = await log_action(db)
        await feed(callback_query=self.dm_callback(action, verb="trust"))
        assert await db.is_trusted(CHAT_ID, SPAMMER_ID)
        assert len(session.calls(UnbanChatMember)) == 1

    async def test_false_positive_clears_history(self, feed, session, db):
        await db.add_join(CHAT_ID, SPAMMER_ID, NOW)
        await db.bump_message_count(CHAT_ID, SPAMMER_ID, NOW)
        action = await log_action(db)
        await feed(callback_query=self.dm_callback(action, verb="fp"))
        assert await db.last_join(CHAT_ID, SPAMMER_ID) is None
        assert await db.message_count(CHAT_ID, SPAMMER_ID) == 1
        assert await db.buffered_messages(CHAT_ID, SPAMMER_ID) == []
        assert (await db.get_action(action.id)).resolution == "fp"

    async def test_restricted_user_gets_chat_defaults_back(
        self, feed, session, db
    ):
        session.on(GetChat, lambda m: full_chat())
        action = await log_action(db, action="delete_and_restrict")
        await feed(callback_query=self.dm_callback(action))
        call = session.calls(RestrictChatMember)[0]
        assert call.permissions.can_send_messages is True
        assert call.until_date is None

    async def test_delete_only_action_lifts_nothing(self, feed, session, db):
        action = await log_action(db, action=ACTION_DELETE)
        await feed(callback_query=self.dm_callback(action))
        assert session.calls(UnbanChatMember) == []
        assert session.calls(RestrictChatMember) == []
        assert (await db.get_action(action.id)).is_resolved

    async def test_second_press_does_not_act_twice(self, feed, session, db):
        action = await log_action(db)
        await feed(callback_query=self.dm_callback(action))
        await feed(
            callback_query=self.dm_callback(
                action, verb="trust", user_id=OWNER_ID
            )
        )
        assert len(session.calls(UnbanChatMember)) == 1
        assert not await db.is_trusted(CHAT_ID, SPAMMER_ID)
        assert (await db.get_action(action.id)).resolved_by == ADMIN_ID

    async def test_other_admins_dms_are_updated(self, feed, session, db):
        action = await log_action(db)
        await db.add_action_dm(action.id, ADMIN_ID, 8000)
        await db.add_action_dm(action.id, OWNER_ID, 8001)
        await feed(callback_query=self.dm_callback(action, message_id=8000))
        edits = {
            (call.chat_id, call.message_id)
            for call in session.calls(EditMessageText)
        }
        assert edits == {(ADMIN_ID, 8000), (OWNER_ID, 8001)}

    async def test_unban_clears_in_flight_sanction(
        self, feed, session, db, services
    ):
        spammer = make_user(SPAMMER_ID)
        await feed(
            message=make_message(DHM_SPAM_TEXT, user=spammer, message_id=1)
        )
        action = (await db.recent_actions(CHAT_ID, 1))[0]
        await feed(callback_query=self.dm_callback(action))
        session.clear()
        await feed(
            message=make_message("hello again", user=spammer, message_id=2)
        )
        assert session.calls(DeleteMessages) == []


class TestResolveWhenLiftFails:
    async def press(self, feed, db, verb, action=ACTION_RESTRICT):
        record = await log_action(db, action=action)
        data = ActionCb(
            action_id=record.id, verb=verb, surface=SURFACE_DM
        ).pack()
        await feed(
            callback_query=CallbackQuery(
                id="1",
                from_user=make_user(ADMIN_ID),
                chat_instance="x",
                data=data,
            )
        )
        return record.id

    @pytest.fixture(autouse=True)
    def lift_fails(self, session):

        session.on(GetChat, lambda m: full_chat())
        session.on(
            RestrictChatMember,
            lambda m: TelegramBadRequest(m, "method is for supergroups"),
        )
        session.on(
            UnbanChatMember,
            lambda m: TelegramBadRequest(m, "not enough rights"),
        )

    async def test_trust_still_applies_and_warns(self, feed, db, session):
        action_id = await self.press(feed, db, "trust")
        assert await db.is_trusted(CHAT_ID, SPAMMER_ID)
        assert (await db.get_action(action_id)).is_resolved
        answer = session.calls(AnswerCallbackQuery)[0]
        assert answer.show_alert is True
        assert "saved" in answer.text

    async def test_false_positive_still_applies_and_warns(
        self, feed, db, session
    ):
        await db.add_join(CHAT_ID, SPAMMER_ID, NOW)
        action_id = await self.press(feed, db, "fp")
        assert await db.last_join(CHAT_ID, SPAMMER_ID) is None
        assert (await db.get_action(action_id)).is_resolved
        assert session.calls(AnswerCallbackQuery)[0].show_alert is True

    async def test_unban_stays_open_when_lift_fails(self, feed, db, session):
        action_id = await self.press(feed, db, "unban", ACTION_BAN)
        assert not (await db.get_action(action_id)).is_resolved
        assert "rejected" in session.calls(AnswerCallbackQuery)[0].text


class TestResolvingAnIncident:
    async def action(self, db, kind, until_ts=None):
        return await log_action(db, action=kind, until_ts=until_ts)

    def press(self, action_id, verb):
        data = ActionCb(action_id=action_id, verb=verb, surface=SURFACE_DM)
        return CallbackQuery(
            id="1",
            from_user=make_user(ADMIN_ID),
            chat_instance="x",
            data=data.pack(),
        )

    async def test_trust_on_old_card_lifts_the_later_ban(
        self, feed, db, session
    ):
        first = await self.action(db, "delete")
        second = await self.action(db, "delete_and_restrict", NOW + 600)
        third = await self.action(db, "delete_and_ban")
        await feed(callback_query=self.press(first.id, "trust"))
        assert session.calls(UnbanChatMember)[0].user_id == SPAMMER_ID
        assert await db.is_trusted(CHAT_ID, SPAMMER_ID)
        for item in (first, second, third):
            assert (await db.get_action(item.id)).resolution == "trust"

    async def test_later_restriction_is_lifted_from_old_card(
        self, feed, db, session
    ):

        session.on(GetChat, lambda m: full_chat())
        first = await self.action(db, "delete")
        await self.action(db, "delete_and_restrict", int(time.time()) + 600)
        await feed(callback_query=self.press(first.id, "fp"))
        assert session.calls(RestrictChatMember)[
            0
        ].permissions.can_send_messages

    async def test_expired_restriction_is_not_touched(self, feed, db, session):

        session.on(GetChat, lambda m: full_chat())
        first = await self.action(db, "delete")
        await self.action(db, "delete_and_restrict", int(time.time()) - 10)
        await feed(callback_query=self.press(first.id, "fp"))
        assert session.calls(RestrictChatMember) == []

    async def test_other_users_and_chats_are_untouched(self, feed, db, session):
        first = await self.action(db, "delete")
        other = await log_action(db, user_id=1)
        elsewhere = await log_action(db, chat_id=-5)
        await feed(callback_query=self.press(first.id, "unban"))
        assert session.calls(UnbanChatMember) == []
        assert not (await db.get_action(other.id)).is_resolved
        assert not (await db.get_action(elsewhere.id)).is_resolved

    async def test_failed_unban_reopens_the_whole_incident(
        self, feed, db, session
    ):
        session.on(
            UnbanChatMember,
            lambda m: TelegramBadRequest(m, "not enough rights"),
        )
        first = await self.action(db, "delete")
        second = await self.action(db, "delete_and_ban")
        await feed(callback_query=self.press(first.id, "unban"))
        assert not (await db.get_action(first.id)).is_resolved
        assert not (await db.get_action(second.id)).is_resolved

    async def test_every_admins_cards_are_updated(self, feed, db, session):
        first = await self.action(db, "delete")
        second = await self.action(db, "delete_and_ban")
        await db.add_action_dm(first.id, ADMIN_ID, 8000)
        await db.add_action_dm(second.id, ADMIN_ID, 8001)
        await feed(callback_query=self.press(first.id, "unban"))
        edits = {call.message_id for call in session.calls(EditMessageText)}
        assert edits == {8000, 8001}

    async def test_false_positive_stops_re_escalation(
        self, feed, engine, db, sink
    ):
        await db.set_setting(CHAT_ID, "pattern_add.link_pattern", [r"t\.me/\+"])
        await db.set_setting(CHAT_ID, "tiers.delete", 20)
        engine.rules.invalidate(CHAT_ID)
        spammer = make_user(SPAMMER_ID)
        for message_id in (1, 2, 3):
            engine.clear_sanction(CHAT_ID, SPAMMER_ID)
            await engine.handle_message(
                make_message(LINK, user=spammer, message_id=message_id)
            )
        assert sink.actions[2].action == "delete_and_ban"
        await db.claim_resolution(sink.actions[2].id, 1, "A", "fp", NOW)
        for other in await db.open_actions(CHAT_ID, SPAMMER_ID):
            await db.claim_resolution(other.id, 1, "A", "fp", NOW)
        engine.clear_sanction(CHAT_ID, SPAMMER_ID)
        await engine.handle_message(
            make_message(LINK, user=spammer, message_id=4)
        )
        assert sink.actions[3].action == "delete"


class TestDigestList:
    def list_callback(self, page=0, new=1, user_id=ADMIN_ID):
        message = make_message(
            "digest",
            chat=Chat(id=user_id, type="private"),
            user=User(id=BOT_ID, is_bot=True, first_name="B"),
            message_id=9000,
        )
        data = DigestListCb(chat_id=CHAT_ID, page=page, new=new).pack()
        return callback(data, user_id=user_id, message=message)

    async def test_full_list_pages_all_actions(self, feed, session, db):
        for _ in range(12):
            await log_action(db)
        await feed(callback_query=self.list_callback())
        sent = rich_sends(session)[0]
        assert "12 actions" in card_text(sent)
        older = sent.reply_markup.inline_keyboard[-1][0]
        assert older.callback_data == f"dgl:{CHAT_ID}:1:0"
        await feed(callback_query=self.list_callback(page=1, new=0))
        edit = session.calls(EditMessageText)[0]
        assert "Page 2 of 2" in card_text(edit)

    async def test_non_admin_cannot_view_list(self, feed, session, db):
        await log_action(db)
        await feed(callback_query=self.list_callback(user_id=MEMBER_ID))
        assert replies(session) == []

    async def test_review_button_sends_actionable_card(self, feed, session, db):
        action = await log_action(db)
        message = make_message(
            "list",
            chat=Chat(id=ADMIN_ID, type="private"),
            user=User(id=BOT_ID, is_bot=True, first_name="B"),
            message_id=9001,
        )
        await feed(
            callback_query=callback(
                CardCb(action_id=action.id).pack(), message=message
            )
        )
        sent = session.calls(SendRichMessage)[0]
        assert "unban" in verbs(sent.reply_markup)
        assert (await db.action_dms(action.id))[0][0] == ADMIN_ID


class TestPrivateChat:
    async def test_start_lists_only_groups_where_user_is_admin(
        self, feed, session, db
    ):
        session.set_admins(-200, OWNER_ID)
        await db.upsert_chat(-200, "Other Group", "supergroup", NOW)
        await feed(message=private_message("/start"))
        card = rich_sends(session)[0]
        [table] = tables(card.rich_message)
        [row] = table.cells[1:]
        assert "Test Group" in row[0].text
        assert row[1].text.button.callback_data == f"sub:{CHAT_ID}:1"
        assert await db.has_started(ADMIN_ID)

    async def test_start_excludes_inactive_groups(self, feed, session, db):
        await db.set_chat_active(CHAT_ID, False, NOW)
        await feed(message=private_message("/start"))
        assert sends(session)[0].reply_markup is None

    async def test_subscribe_button_toggles(self, feed, session, db):
        message = private_message("/groups")
        data = SubCb(chat_id=CHAT_ID, on=1).pack()
        await feed(callback_query=callback(data, message=message))
        assert await db.is_subscribed(CHAT_ID, ADMIN_ID)
        edited = session.calls(EditMessageText)[0]
        [table] = tables(edited.rich_message)
        toggle = table.cells[1][1].text.button
        assert toggle.callback_data == f"sub:{CHAT_ID}:0"
        data = SubCb(chat_id=CHAT_ID, on=0).pack()
        await feed(callback_query=callback(data, message=message))
        assert not await db.is_subscribed(CHAT_ID, ADMIN_ID)

    async def test_subscribe_button_requires_admin(self, feed, session, db):
        data = SubCb(chat_id=CHAT_ID, on=1).pack()
        await feed(callback_query=callback(data, user_id=MEMBER_ID))
        assert not await db.is_subscribed(CHAT_ID, MEMBER_ID)

    async def test_deep_link_subscribes_admin(self, feed, session, db):
        await feed(message=private_message(f"/start sub_{CHAT_ID}"))
        assert await db.is_subscribed(CHAT_ID, ADMIN_ID)
        assert "Subscribed" in card_text(rich_sends(session)[0])

    async def test_deep_link_refuses_non_admin(self, feed, session, db):
        await feed(
            message=private_message(f"/start sub_{CHAT_ID}", user_id=MEMBER_ID)
        )
        assert not await db.is_subscribed(CHAT_ID, MEMBER_ID)
        assert "not an admin" in sends(session)[0].text

    async def test_deep_link_rejects_garbage(self, feed, session, db):
        await feed(message=private_message("/start sub_abc"))
        assert "not valid" in card_text(rich_sends(session)[0])

    async def test_start_reactivates_blocked_subscription(self, feed, db):
        await db.set_subscription(CHAT_ID, ADMIN_ID, True, NOW)
        await db.mark_user_blocked(ADMIN_ID, NOW)
        await feed(message=private_message("/start"))
        assert await db.is_subscribed(CHAT_ID, ADMIN_ID)

    async def test_explicit_unsubscribe_survives_start(self, feed, db):
        await db.set_subscription(CHAT_ID, ADMIN_ID, False, NOW)
        await feed(message=private_message("/start"))
        assert not await db.is_subscribed(CHAT_ID, ADMIN_ID)

    async def test_prefs_flow(self, feed, session, db):
        await feed(message=private_message("/prefs"))
        assert "All actions" in sends(session)[0].text
        message = private_message("/prefs")
        await feed(
            callback_query=callback(
                PrefCb(mode="bans_only").pack(), message=message
            )
        )
        assert await db.get_pref(ADMIN_ID) == "bans_only"

    async def test_prefs_command_refused_for_non_admin(self, feed, session, db):
        await feed(message=private_message("/prefs", user_id=MEMBER_ID))
        reply = sends(session)[0]
        assert "not an admin of any group" in reply.text
        assert reply.reply_markup is None
        assert not await db.has_started(MEMBER_ID)

    async def test_prefs_button_refused_for_non_admin(self, feed, session, db):
        message = private_message("/prefs", user_id=MEMBER_ID)
        await feed(
            callback_query=callback(
                PrefCb(mode="bans_only").pack(),
                user_id=MEMBER_ID,
                message=message,
            )
        )
        assert await db.get_pref(MEMBER_ID) == "all"
        assert session.calls(EditMessageText) == []

    async def test_prefs_rejects_unknown_mode(self, feed, db):
        await feed(callback_query=callback(PrefCb(mode="nonsense").pack()))
        assert await db.get_pref(ADMIN_ID) == "all"


class TestBotAdded:
    bot_user = User(id=BOT_ID, is_bot=True, first_name="Bot")

    async def test_dm_to_adder_reports_missing_rights(self, feed, session, db):
        await feed(
            my_chat_member=bot_member_event(
                ChatMemberMember(user=self.bot_user)
            )
        )
        sent = sends(session)[0]
        assert sent.chat_id == ADMIN_ID
        assert "admin status" in sent.text
        assert sent.ephemeral_message_parameters is None
        assert (await db.get_chat(CHAT_ID)).active

    async def test_dm_confirms_full_rights(self, feed, session):
        await feed(
            my_chat_member=bot_member_event(administrator(self.bot_user))
        )
        assert "rights I need" in sends(session)[0].text

    async def test_partial_rights_are_named(self, feed, session):
        member = administrator(self.bot_user).model_copy(
            update={"can_restrict_members": False}
        )
        await feed(my_chat_member=bot_member_event(member))
        assert "ban users" in sends(session)[0].text
        assert "delete messages" not in sends(session)[0].text

    async def test_falls_back_to_ephemeral_when_adder_has_not_started(
        self, feed, session
    ):
        def forbid_private(method):
            if method.ephemeral_message_parameters is None:
                return TelegramForbiddenError(
                    method, "bot can't initiate conversation"
                )
            return None

        session.on(SendMessage, forbid_private)
        await feed(
            my_chat_member=bot_member_event(administrator(self.bot_user))
        )
        first, second = sends(session)
        assert first.chat_id == ADMIN_ID
        assert second.chat_id == CHAT_ID
        assert second.ephemeral_message_parameters.receiver_user_id == ADMIN_ID

    async def test_removal_marks_chat_inactive(self, feed, session, db):
        event = bot_member_event(
            ChatMemberLeft(user=self.bot_user),
            old_member=ChatMemberMember(user=self.bot_user),
        )
        await feed(my_chat_member=event)
        assert not (await db.get_chat(CHAT_ID)).active
        assert sends(session) == []

    async def test_user_blocking_bot_in_private_marks_inactive(self, feed, db):
        from aiogram.types import ChatMemberBanned

        await db.set_subscription(CHAT_ID, ADMIN_ID, True, NOW)
        event = bot_member_event(
            ChatMemberBanned(user=self.bot_user, until_date=now_utc()),
            old_member=ChatMemberMember(user=self.bot_user),
            chat=Chat(id=ADMIN_ID, type="private"),
        )
        await feed(my_chat_member=event)
        assert not await db.is_subscribed(CHAT_ID, ADMIN_ID)


class TestPipelineRouting:
    async def test_spam_message_reaches_engine(self, feed, session):
        await feed(
            message=make_message(DHM_SPAM_TEXT, user=make_user(SPAMMER_ID))
        )
        assert len(session.calls(BanChatMember)) == 1

    async def test_edited_message_reaches_engine(self, feed, session):
        await feed(
            edited_message=make_message(
                DHM_SPAM_TEXT, user=make_user(SPAMMER_ID)
            )
        )
        assert len(session.calls(BanChatMember)) == 1

    async def test_private_message_is_not_scanned(self, feed, session):
        chat = Chat(id=SPAMMER_ID, type="private")
        await feed(
            message=make_message(
                DHM_SPAM_TEXT, user=make_user(SPAMMER_ID), chat=chat
            )
        )
        assert session.calls(BanChatMember) == []

    async def test_chat_member_join_is_tracked(self, feed, db):
        user = make_user(MEMBER_ID)
        event = ChatMemberUpdated(
            chat=make_chat(),
            from_user=user,
            date=now_utc(),
            old_chat_member=ChatMemberLeft(user=user),
            new_chat_member=ChatMemberMember(user=user),
        )
        await feed(chat_member=event)
        assert await db.last_join(CHAT_ID, MEMBER_ID) is not None

    async def test_admin_demotion_invalidates_cache(
        self, feed, session, services
    ):
        await services.admins.get_admins(CHAT_ID)
        session.set_admins(CHAT_ID, OWNER_ID)
        user = make_user(ADMIN_ID)
        event = ChatMemberUpdated(
            chat=make_chat(),
            from_user=make_user(OWNER_ID),
            date=now_utc(),
            old_chat_member=administrator(user),
            new_chat_member=ChatMemberMember(user=user),
        )
        await feed(chat_member=event)
        assert not await services.admins.is_admin(CHAT_ID, ADMIN_ID)

    async def test_chat_migration_moves_state(self, feed, db, services):
        await db.set_subscription(CHAT_ID, ADMIN_ID, True, NOW)
        new_id = -1009999
        message = make_message(None, migrate_to_chat_id=new_id)
        await feed(message=message)
        assert await db.is_subscribed(new_id, ADMIN_ID)
        assert await db.get_chat(CHAT_ID) is None
        assert (await db.get_chat(new_id)).type == "supergroup"

    async def test_end_to_end_dm_after_spam(self, feed, session, db):
        await db.set_subscription(CHAT_ID, ADMIN_ID, True, NOW)
        await feed(
            message=make_message(DHM_SPAM_TEXT, user=make_user(SPAMMER_ID))
        )
        sent = session.calls(SendRichMessage)
        assert [call.chat_id for call in sent] == [ADMIN_ID]
        assert sent[0].ephemeral_message_parameters is None


class TestLiveAlerts:
    async def enable(self, db, rules):
        await db.set_setting(
            CHAT_ID, "notifications.live_ephemeral_alerts", True
        )
        rules.invalidate(CHAT_ID)

    def alerts(self, session):
        return [
            call
            for call in sends(session)
            if call.ephemeral_message_parameters is not None
        ]

    async def test_off_by_default(self, feed, session):
        await feed(
            message=make_message(DHM_SPAM_TEXT, user=make_user(SPAMMER_ID))
        )
        assert self.alerts(session) == []

    async def test_each_human_admin_gets_an_ephemeral(
        self, feed, session, db, rules
    ):
        await self.enable(db, rules)
        await feed(
            message=make_message(DHM_SPAM_TEXT, user=make_user(SPAMMER_ID))
        )
        receivers = sorted(
            call.ephemeral_message_parameters.receiver_user_id
            for call in self.alerts(session)
        )
        assert receivers == [OWNER_ID, ADMIN_ID]
        assert all(call.chat_id == CHAT_ID for call in self.alerts(session))

    async def test_delivery_failure_is_ignored_and_dm_still_sent(
        self, feed, session, db, rules
    ):
        await self.enable(db, rules)
        await db.set_subscription(CHAT_ID, ADMIN_ID, True, NOW)

        def fail_ephemeral(method):
            if method.ephemeral_message_parameters is not None:
                return TelegramForbiddenError(method, "no")
            return None

        session.on(SendMessage, fail_ephemeral)
        await feed(
            message=make_message(DHM_SPAM_TEXT, user=make_user(SPAMMER_ID))
        )
        cards = session.calls(SendRichMessage)
        assert [call.chat_id for call in cards] == [ADMIN_ID]
        assert len(await db.recent_actions(CHAT_ID, 5)) == 1

    async def test_flood_limit_mutes_alerts_for_chat(
        self, feed, session, db, rules
    ):
        from aiogram.exceptions import TelegramRetryAfter

        await self.enable(db, rules)

        def flood(method):
            return TelegramRetryAfter(method, "flood", retry_after=30)

        session.on(SendMessage, flood)
        await feed(
            message=make_message(
                DHM_SPAM_TEXT, user=make_user(SPAMMER_ID), message_id=1
            )
        )
        first_round = len(self.alerts(session))
        await feed(
            message=make_message(
                DHM_SPAM_TEXT, user=make_user(SPAMMER_ID + 1), message_id=2
            )
        )
        assert len(self.alerts(session)) == first_round


class TestStorage:
    async def test_wal_mode(self, db):
        assert (await db.journal_mode()).lower() == "wal"

    async def test_cleanup_prunes_old_rows_only(self, db):
        await db.add_join(CHAT_ID, 1, NOW - 1000)
        await db.add_join(CHAT_ID, 2, NOW)
        await db.buffer_message(CHAT_ID, 1, 1, NOW - 1000)
        await db.buffer_message(CHAT_ID, 2, 2, NOW)
        action = await log_action(db)
        await db.digest_add(CHAT_ID, ADMIN_ID, action, NOW - 1000, 10)
        await db.digest_add(CHAT_ID, OWNER_ID, action, NOW, 500)
        await db.cleanup(NOW, 500, 500, 500, 500, 500)
        assert await db.last_join(CHAT_ID, 1) is None
        assert await db.last_join(CHAT_ID, 2) == NOW
        assert await db.buffered_messages(CHAT_ID, 1) == []
        assert await db.buffered_messages(CHAT_ID, 2) == [2]
        assert await db.get_digest(CHAT_ID, ADMIN_ID) is None
        assert await db.get_digest(CHAT_ID, OWNER_ID) is not None
        assert await db.get_action(action.id) is not None


class TestTrustTarget:
    async def test_out_of_range_id_gets_usage_reply(self, feed, session, db):
        await feed(message=command_message("/trust 99999999999999999999999"))
        assert "Reply to a message" in sends(session)[0].text

    def trust(self, reply):
        return make_message(
            "/trust",
            user=make_user(ADMIN_ID),
            reply_to_message=reply,
        )

    async def test_topic_link_is_not_a_target(self, feed, db, session):
        created = make_message(
            None,
            user=make_user(777),
            message_id=5,
            forum_topic_created=ForumTopicCreated(name="t", icon_color=7322096),
        )
        await feed(message=self.trust(created))
        assert not await db.is_trusted(CHAT_ID, 777)
        assert "Reply to a message" in session.calls(SendMessage)[0].text


class TestDismiss:
    def card_message(self, chat):
        return Message(
            message_id=9001,
            date=now_utc(),
            chat=chat,
            from_user=User(id=BOT_ID, is_bot=True, first_name="B"),
        )

    async def press(self, feed, action, chat, user_id=ADMIN_ID):
        data = DismissCb(action_id=action.id).pack()
        await feed(
            callback_query=callback(
                data, user_id=user_id, message=self.card_message(chat)
            )
        )

    async def test_private_card_is_deleted(self, feed, session, db):
        action = await log_action(db)
        chat = Chat(id=ADMIN_ID, type="private")
        await self.press(feed, action, chat)
        deleted = session.calls(DeleteMessage)[0]
        assert (deleted.chat_id, deleted.message_id) == (ADMIN_ID, 9001)

    async def test_former_admin_can_clear_their_own_dm(self, feed, session, db):
        action = await log_action(db)
        chat = Chat(id=MEMBER_ID, type="private")
        await self.press(feed, action, chat, user_id=MEMBER_ID)
        assert len(session.calls(DeleteMessage)) == 1

    async def test_group_card_needs_an_admin(self, feed, session, db):
        action = await log_action(db)
        chat = Chat(id=CHAT_ID, type="supergroup")
        await self.press(feed, action, chat, user_id=MEMBER_ID)
        assert session.calls(DeleteMessage) == []
        await self.press(feed, action, chat)
        assert len(session.calls(DeleteMessage)) == 1

    async def test_delete_failure_tells_the_user(self, feed, session, db):
        session.on(DeleteMessage, lambda m: TelegramBadRequest(m, "too old"))
        action = await log_action(db)
        chat = Chat(id=ADMIN_ID, type="private")
        await self.press(feed, action, chat)
        answer = session.calls(AnswerCallbackQuery)[0]
        assert answer.show_alert is True
        assert "delete it yourself" in answer.text


class TestEphemeralReplyFallback:
    async def test_stale_command_still_gets_a_reply(self, feed, session):
        def reject_reply(method):
            if method.reply_parameters is not None:
                return TelegramBadRequest(method, "REPLY_TO_INVALID")
            return None

        session.on(SendRichMessage, reject_reply)
        await feed(
            message=command_message(
                "/status", message_id=0, ephemeral_message_id=33
            )
        )
        calls = rich_sends(session)
        assert len(calls) == 2
        assert calls[1].reply_parameters is None
        assert (
            calls[1].ephemeral_message_parameters.receiver_user_id == ADMIN_ID
        )

    async def test_other_errors_are_not_retried(self, feed, session):
        session.on(
            SendRichMessage, lambda m: TelegramBadRequest(m, "something else")
        )
        await feed(
            message=command_message(
                "/status", message_id=0, ephemeral_message_id=33
            )
        )
        assert len(rich_sends(session)) == 1


LINK = "join us t.me/+abcdefgh"


class TestCommandArguments:
    def command(self, text):
        return make_message(text, user=make_user(ADMIN_ID))

    async def test_log_ignores_superscript_digit(self, feed, session):
        await feed(message=self.command("/log ²"))
        assert "recent actions" in card_text(session.calls(SendRichMessage)[0])

    async def test_threshold_superscript_shows_usage(self, feed, session):
        await feed(message=self.command("/threshold ban ²"))
        assert "Usage" in session.calls(SendMessage)[0].text


class TestCommandsCreateTheChatRow:
    async def test_raidmode_works_on_a_fresh_database(
        self, bot, feed, services, session, db
    ):
        session.set_admins(OTHER_ALLOWED_CHAT_ID, ADMIN_ID)
        chat = Chat(id=OTHER_ALLOWED_CHAT_ID, type="supergroup", title="New")
        assert await db.get_chat(OTHER_ALLOWED_CHAT_ID) is None
        await feed(
            message=make_message(
                "/raidmode on",
                user=make_user(ADMIN_ID),
                chat=chat,
            )
        )
        assert await services.engine.raid_active(OTHER_ALLOWED_CHAT_ID)
