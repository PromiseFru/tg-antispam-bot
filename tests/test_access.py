from __future__ import annotations

import datetime as dt
import time

import pytest
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramMigrateToChat,
    TelegramNetworkError,
)
from aiogram.methods import (
    BanChatMember,
    DeleteMessages,
    GetChatAdministrators,
    LeaveChat,
    SendMessage,
    SendRichMessage,
)
from aiogram.types import (
    CallbackQuery,
    Chat,
    ChatMemberLeft,
    ChatMemberMember,
    ChatMemberUpdated,
    Update,
    User,
)

from conftest import (
    ADMIN_ID,
    ALLOWED_CHATS,
    BOT_ID,
    CHAT_ID,
    DHM_SPAM_TEXT,
    OTHER_ALLOWED_CHAT_ID,
    SPAMMER_ID,
    administrator,
    card_text,
    log_action,
    make_message,
    make_user,
    settle,
    shared_dispatcher,
)
from tg_antispam_bot.access import AccessControl
from tg_antispam_bot.config import (
    parse_rules_document,
)
from tg_antispam_bot.notify import NotifyService
from tg_antispam_bot.render import SURFACE_DM, ActionCb, SubCb
from tg_antispam_bot.rules_store import RulesStore
from tg_antispam_bot.services import build_services

STRANGER_ID = -100555
FALLBACK_ID = -100999
NEW_ID = -100321
NOW = int(time.time())


def stranger_chat(chat_type="supergroup", chat_id=STRANGER_ID):
    return Chat(id=chat_id, type=chat_type, title="Stranger Group")


def member_event(new_member, chat, old_member=None):
    bot_user = User(id=BOT_ID, is_bot=True, first_name="Bot")
    return ChatMemberUpdated(
        chat=chat,
        from_user=make_user(ADMIN_ID),
        date=dt.datetime.now(tz=dt.UTC),
        old_chat_member=old_member or ChatMemberLeft(user=bot_user),
        new_chat_member=new_member,
    )


def bot_member():
    return administrator(User(id=BOT_ID, is_bot=True, first_name="Bot"))


@pytest.fixture
def make_feed(bot):
    counter = [0]

    def factory(services):
        async def run(**fields):
            counter[0] += 1
            update = Update(update_id=counter[0], **fields)
            await shared_dispatcher(services).feed_update(bot, update)
            await settle(services)

        return run

    return factory


@pytest.fixture
def feed(make_feed, services):
    return make_feed(services)


@pytest.fixture
async def fallback_services(bot, db, rules):
    built = build_services(
        bot,
        db,
        rules,
        ALLOWED_CHATS,
        FALLBACK_ID,
        per_admin_interval=0.001,
        global_per_second=10000,
        digest_edit_interval=0.0,
        verify_retry_seconds=0.001,
    )
    yield built
    await built.stop()


class TestStrangerGroups:
    async def test_message_makes_bot_leave_without_scanning_or_storing(
        self, feed, session, db
    ):
        message = make_message(
            DHM_SPAM_TEXT, user=make_user(SPAMMER_ID), chat=stranger_chat()
        )
        await feed(message=message)
        assert [call.chat_id for call in session.calls(LeaveChat)] == [
            STRANGER_ID
        ]
        assert session.calls(DeleteMessages) == []
        assert session.calls(BanChatMember) == []
        assert session.calls(SendMessage) == []
        assert await db.get_chat(STRANGER_ID) is None
        assert await db.message_count(STRANGER_ID, SPAMMER_ID) == 0
        assert await db.recent_actions(STRANGER_ID, 5) == []

    async def test_burst_leaves_only_once(self, feed, session):
        for message_id in (1, 2, 3):
            await feed(
                message=make_message(
                    "hi", chat=stranger_chat(), message_id=message_id
                )
            )
        assert len(session.calls(LeaveChat)) == 1

    async def test_being_added_leaves_without_contacting_adder(
        self, feed, session, db
    ):
        await feed(my_chat_member=member_event(bot_member(), stranger_chat()))
        assert [call.chat_id for call in session.calls(LeaveChat)] == [
            STRANGER_ID
        ]
        assert session.calls(SendMessage) == []
        assert await db.get_chat(STRANGER_ID) is None

    async def test_being_added_to_channel_leaves(self, feed, session):
        await feed(
            my_chat_member=member_event(bot_member(), stranger_chat("channel"))
        )
        assert len(session.calls(LeaveChat)) == 1

    async def test_being_removed_does_not_try_to_leave(self, feed, session):
        bot_user = User(id=BOT_ID, is_bot=True, first_name="Bot")
        event = member_event(
            ChatMemberLeft(user=bot_user),
            stranger_chat(),
            old_member=ChatMemberMember(user=bot_user),
        )
        await feed(my_chat_member=event)
        assert session.calls(LeaveChat) == []

    async def test_admin_command_gets_no_reply(self, feed, session, db):
        message = make_message(
            "/raidmode on",
            user=make_user(ADMIN_ID),
            chat=stranger_chat(),
        )
        await feed(message=message)
        assert session.calls(SendMessage) == []
        assert session.calls(GetChatAdministrators) == []

    async def test_join_is_not_recorded(self, feed, db):
        user = make_user(SPAMMER_ID)
        event = ChatMemberUpdated(
            chat=stranger_chat(),
            from_user=user,
            date=dt.datetime.now(tz=dt.UTC),
            old_chat_member=ChatMemberLeft(user=user),
            new_chat_member=ChatMemberMember(user=user),
        )
        await feed(chat_member=event)
        assert await db.last_join(STRANGER_ID, SPAMMER_ID) is None

    async def test_button_in_stranger_group_does_nothing(
        self, feed, session, db
    ):
        action = await log_action(db)
        callback = CallbackQuery(
            id="1",
            from_user=make_user(ADMIN_ID),
            chat_instance="x",
            data=ActionCb(
                action_id=action.id, verb="unban", surface=SURFACE_DM
            ).pack(),
            message=make_message("x", chat=stranger_chat()),
        )
        await feed(callback_query=callback)
        assert not (await db.get_action(action.id)).is_resolved


class TestAllowedGroups:
    @pytest.mark.parametrize("chat_id", sorted(ALLOWED_CHATS))
    async def test_listed_group_is_moderated(self, feed, session, chat_id):
        session.set_admins(chat_id, ADMIN_ID)
        chat = Chat(id=chat_id, type="supergroup", title="Mine")
        await feed(
            message=make_message(
                DHM_SPAM_TEXT, user=make_user(SPAMMER_ID), chat=chat
            )
        )
        assert session.calls(LeaveChat) == []
        assert session.calls(BanChatMember)[0].chat_id == chat_id

    async def test_private_chat_always_passes(self, feed, session):
        chat = Chat(id=SPAMMER_ID, type="private")
        await feed(
            message=make_message(
                "/start",
                user=make_user(SPAMMER_ID),
                chat=chat,
            )
        )
        assert session.calls(LeaveChat) == []
        assert len(session.calls(SendMessage)) == 1


class TestSubscriptionsRespectAllowlist:
    async def test_start_hides_unlisted_group(self, feed, session, db):
        session.set_admins(STRANGER_ID, ADMIN_ID)
        await db.upsert_chat(STRANGER_ID, "Stale", "supergroup", NOW)
        chat = Chat(id=ADMIN_ID, type="private")
        await feed(
            message=make_message("/start", user=make_user(ADMIN_ID), chat=chat)
        )
        card = session.calls(SendRichMessage)[0]
        text = card_text(card)
        assert "Test Group" in text
        assert "Stale" not in text

    async def test_deep_link_refused_even_for_admin(self, feed, session, db):
        session.set_admins(STRANGER_ID, ADMIN_ID)
        text = f"/start sub_{STRANGER_ID}"
        chat = Chat(id=ADMIN_ID, type="private")
        await feed(
            message=make_message(text, user=make_user(ADMIN_ID), chat=chat)
        )
        assert not await db.is_subscribed(STRANGER_ID, ADMIN_ID)
        assert "not available" in card_text(session.calls(SendRichMessage)[0])

    async def test_subscribe_button_refused_even_for_admin(
        self, feed, session, db
    ):
        session.set_admins(STRANGER_ID, ADMIN_ID)
        callback = CallbackQuery(
            id="1",
            from_user=make_user(ADMIN_ID),
            chat_instance="x",
            data=SubCb(chat_id=STRANGER_ID, on=1).pack(),
        )
        await feed(callback_query=callback)
        assert not await db.is_subscribed(STRANGER_ID, ADMIN_ID)


class TestFallbackChat:
    async def test_bot_stays_but_does_not_moderate(
        self, make_feed, fallback_services, session
    ):
        feed = make_feed(fallback_services)
        chat = Chat(id=FALLBACK_ID, type="supergroup", title="Logs")
        await feed(
            message=make_message(
                DHM_SPAM_TEXT, user=make_user(SPAMMER_ID), chat=chat
            )
        )
        assert session.calls(LeaveChat) == []
        assert session.calls(DeleteMessages) == []

    async def test_buttons_work(self, make_feed, fallback_services, db):
        feed = make_feed(fallback_services)
        action = await log_action(db)
        chat = Chat(id=FALLBACK_ID, type="supergroup", title="Logs")
        callback = CallbackQuery(
            id="1",
            from_user=make_user(ADMIN_ID),
            chat_instance="x",
            data=ActionCb(
                action_id=action.id, verb="unban", surface=SURFACE_DM
            ).pack(),
            message=make_message("card", chat=chat),
        )
        await feed(callback_query=callback)
        assert (await db.get_action(action.id)).is_resolved

    async def test_stranger_spam_never_reaches_fallback(
        self, make_feed, fallback_services, session
    ):
        feed = make_feed(fallback_services)
        await feed(
            message=make_message(
                DHM_SPAM_TEXT, user=make_user(SPAMMER_ID), chat=stranger_chat()
            )
        )
        assert session.calls(SendMessage) == []
        assert session.calls(SendRichMessage) == []

    async def test_sweep_spares_fallback(self, fallback_services, db, session):
        await db.upsert_chat(FALLBACK_ID, "Logs", "supergroup", NOW)
        await fallback_services.access.sweep()
        assert session.calls(LeaveChat) == []


class TestMigration:
    async def test_upgrade_of_allowed_group_is_followed(
        self, feed, session, db, services
    ):
        await feed(message=make_message(None, migrate_to_chat_id=NEW_ID))
        assert services.access.is_allowed(NEW_ID)
        session.set_admins(NEW_ID, ADMIN_ID)
        chat = Chat(id=NEW_ID, type="supergroup", title="Upgraded")
        await feed(
            message=make_message(
                DHM_SPAM_TEXT, user=make_user(SPAMMER_ID), chat=chat
            )
        )
        assert session.calls(LeaveChat) == []
        assert session.calls(BanChatMember)[0].chat_id == NEW_ID

    async def test_migrate_from_message_arriving_first_is_followed(
        self, feed, session, services
    ):
        chat = Chat(id=NEW_ID, type="supergroup", title="Upgraded")
        await feed(
            message=make_message(None, chat=chat, migrate_from_chat_id=CHAT_ID)
        )
        assert services.access.is_allowed(NEW_ID)
        assert session.calls(LeaveChat) == []

    async def test_upgrade_survives_restart(self, feed, bot, db):
        await feed(message=make_message(None, migrate_to_chat_id=NEW_ID))
        restarted = AccessControl(bot, db, ALLOWED_CHATS)
        assert not restarted.is_allowed(NEW_ID)
        await restarted.load()
        assert restarted.is_allowed(NEW_ID)

    async def test_removing_old_id_also_revokes_new_id(self, feed, bot, db):
        await feed(message=make_message(None, migrate_to_chat_id=NEW_ID))
        restarted = AccessControl(bot, db, {OTHER_ALLOWED_CHAT_ID})
        await restarted.load()
        assert not restarted.is_allowed(NEW_ID)

    async def test_stranger_upgrade_grants_nothing(
        self, feed, session, services
    ):
        await feed(
            message=make_message(
                None, chat=stranger_chat(), migrate_to_chat_id=NEW_ID
            )
        )
        assert not services.access.is_allowed(NEW_ID)
        assert [call.chat_id for call in session.calls(LeaveChat)] == [
            STRANGER_ID
        ]

    async def test_stranger_claiming_unlisted_origin_grants_nothing(
        self, feed, services
    ):
        chat = Chat(id=NEW_ID, type="supergroup", title="Fake")
        await feed(
            message=make_message(
                None, chat=chat, migrate_from_chat_id=STRANGER_ID
            )
        )
        assert not services.access.is_allowed(NEW_ID)

    async def test_buffered_messages_do_not_move_to_new_chat(self, db):
        await db.upsert_chat(CHAT_ID, "G", "group", NOW)
        await db.buffer_message(CHAT_ID, 1, 41, NOW)
        await db.migrate_chat(CHAT_ID, UPGRADED_ID)
        assert await db.buffered_messages(UPGRADED_ID, 1) == []
        assert await db.buffered_messages(CHAT_ID, 1) == []


class TestSweepAndLeaving:
    async def test_sweep_leaves_only_unlisted_active_chats(
        self, services, db, session
    ):
        await db.upsert_chat(STRANGER_ID, "Stale", "supergroup", NOW)
        await db.upsert_chat(
            -100556, "Inactive", "supergroup", NOW, active=False
        )
        await services.access.sweep()
        assert [call.chat_id for call in session.calls(LeaveChat)] == [
            STRANGER_ID
        ]
        assert not (await db.get_chat(STRANGER_ID)).active
        assert (await db.get_chat(CHAT_ID)).active

    async def test_temporary_failure_is_retried_on_next_update(
        self, feed, session
    ):
        attempts = []

        def flaky(method):
            attempts.append(1)
            return (
                TelegramNetworkError(method, "down")
                if len(attempts) == 1
                else True
            )

        session.on(LeaveChat, flaky)
        await feed(
            message=make_message("one", chat=stranger_chat(), message_id=1)
        )
        await feed(
            message=make_message("two", chat=stranger_chat(), message_id=2)
        )
        assert len(attempts) == 2

    async def test_already_gone_is_not_retried(self, feed, session, db):
        await db.upsert_chat(STRANGER_ID, "Stale", "supergroup", NOW)
        session.on(LeaveChat, lambda m: TelegramBadRequest(m, "chat not found"))
        await feed(
            message=make_message("one", chat=stranger_chat(), message_id=1)
        )
        await feed(
            message=make_message("two", chat=stranger_chat(), message_id=2)
        )
        assert len(session.calls(LeaveChat)) == 1
        assert not (await db.get_chat(STRANGER_ID)).active

    async def test_leaves_again_after_retry_window(self, bot, db, session):
        moment = [0.0]
        access = AccessControl(bot, db, ALLOWED_CHATS, clock=lambda: moment[0])
        await access.leave(STRANGER_ID, "x")
        await access.leave(STRANGER_ID, "x")
        moment[0] = 61.0
        await access.leave(STRANGER_ID, "x")
        assert len(session.calls(LeaveChat)) == 2


UPGRADED_ID = -100123


class TestUpgradedGroupRules:
    FILE = {
        "chats": {
            str(FALLBACK_ID): {"enabled": False, "tiers": {"delete": 11}},
        }
    }

    async def test_overrides_follow_the_upgrade(self, db):
        await db.add_chat_migration(FALLBACK_ID, UPGRADED_ID)
        store = RulesStore(db, parse_rules_document(self.FILE))
        rules = (await store.get(UPGRADED_ID)).rules
        assert rules.enabled is False
        assert rules.tiers.delete == 11

    async def test_new_id_entry_wins_over_old_one(self, db):
        await db.add_chat_migration(FALLBACK_ID, UPGRADED_ID)
        document = {
            "chats": {
                **self.FILE["chats"],
                str(UPGRADED_ID): {"tiers": {"delete": 22}},
            }
        }
        store = RulesStore(db, parse_rules_document(document))
        rules = (await store.get(UPGRADED_ID)).rules
        assert rules.enabled is True
        assert rules.tiers.delete == 22

    async def test_group_settings_still_layer_on_top(self, db):
        await db.add_chat_migration(FALLBACK_ID, UPGRADED_ID)
        await db.set_setting(UPGRADED_ID, "tiers.delete", 33)
        store = RulesStore(db, parse_rules_document(self.FILE))
        assert (await store.get(UPGRADED_ID)).rules.tiers.delete == 33

    async def test_upgrade_through_the_bot_keeps_rules(
        self, bot, db, session, feed, services
    ):
        store = RulesStore(
            db,
            parse_rules_document(
                {"chats": {str(CHAT_ID): {"tiers": {"delete": 11}}}}
            ),
        )
        services.rules = store
        await feed(message=make_message(None, migrate_to_chat_id=UPGRADED_ID))
        assert (await store.get(UPGRADED_ID)).rules.tiers.delete == 11

    async def test_overrides_still_follow(self, bot, db, feed, services):
        file = parse_rules_document(
            {"chats": {str(CHAT_ID): {"tiers": {"delete": 11}}}}
        )
        access = AccessControl(bot, db, {CHAT_ID, UPGRADED_ID})
        services.access = access
        services.rules = RulesStore(db, file)
        await feed(message=make_message(None, migrate_to_chat_id=UPGRADED_ID))
        assert await db.migrated_from(UPGRADED_ID) == CHAT_ID
        rules = (await services.rules.get(UPGRADED_ID)).rules
        assert rules.tiers.delete == 11

    async def test_migrate_from_message_refreshes_cached_rules(
        self, bot, db, feed, services
    ):
        file = parse_rules_document(
            {"chats": {str(CHAT_ID): {"tiers": {"delete": 11}}}}
        )
        services.access = AccessControl(bot, db, {CHAT_ID, UPGRADED_ID})
        services.rules = RulesStore(db, file)
        assert (await services.rules.get(UPGRADED_ID)).rules.tiers.delete == 50
        chat = Chat(id=UPGRADED_ID, type="supergroup", title="U")
        await feed(
            message=make_message(None, chat=chat, migrate_from_chat_id=CHAT_ID)
        )
        assert (await services.rules.get(UPGRADED_ID)).rules.tiers.delete == 11


class TestFallbackUpgrade:
    @pytest.fixture
    async def fallback_services(self, bot, db, rules):
        built = build_services(
            bot,
            db,
            rules,
            ALLOWED_CHATS,
            FALLBACK_ID,
            per_admin_interval=0.001,
            global_per_second=10000,
            digest_edit_interval=0.0,
            verify_retry_seconds=0.001,
            group_interval=0.001,
        )
        yield built
        await built.stop()

    async def run(self, bot, services, **fields):
        update = Update(update_id=1, **fields)
        await shared_dispatcher(services).feed_update(bot, update)
        await settle(services)

    async def assert_followed(self, services, session):
        assert services.access.fallback_chat_id == UPGRADED_ID
        assert services.notify.fallback_chat_id == UPGRADED_ID
        assert session.calls(LeaveChat) == []

    async def test_migrate_to_message_is_followed(
        self, bot, fallback_services, session
    ):
        chat = Chat(id=FALLBACK_ID, type="group", title="Logs")
        await self.run(
            bot,
            fallback_services,
            message=make_message(
                None, chat=chat, migrate_to_chat_id=UPGRADED_ID
            ),
        )
        await self.assert_followed(fallback_services, session)

    async def test_migrate_from_message_is_followed(
        self, bot, fallback_services, session
    ):
        chat = Chat(id=UPGRADED_ID, type="supergroup", title="Logs")
        await self.run(
            bot,
            fallback_services,
            message=make_message(
                None, chat=chat, migrate_from_chat_id=FALLBACK_ID
            ),
        )
        await self.assert_followed(fallback_services, session)

    async def test_bot_stays_in_upgraded_chat(
        self, bot, fallback_services, session
    ):
        await fallback_services.access.follow_fallback(UPGRADED_ID)
        chat = Chat(id=UPGRADED_ID, type="supergroup", title="Logs")
        await self.run(
            bot, fallback_services, message=make_message("hi", chat=chat)
        )
        assert session.calls(LeaveChat) == []

    async def test_restart_remembers_new_fallback(self, bot, db, session):
        await db.add_chat_migration(FALLBACK_ID, UPGRADED_ID)
        access = AccessControl(bot, db, ALLOWED_CHATS, FALLBACK_ID)
        await access.load()
        assert access.fallback_chat_id == UPGRADED_ID

    async def test_stranger_upgrade_does_not_move_fallback(
        self, bot, fallback_services, session
    ):
        chat = Chat(id=-5, type="group", title="Stranger")
        await self.run(
            bot,
            fallback_services,
            message=make_message(None, chat=chat, migrate_to_chat_id=-6),
        )
        assert fallback_services.access.fallback_chat_id == FALLBACK_ID

    async def test_send_error_moves_fallback_and_redelivers(
        self, fallback_services, db, session
    ):
        def migrate_once(method):
            if method.chat_id == FALLBACK_ID:
                return TelegramMigrateToChat(
                    method, "upgraded", migrate_to_chat_id=UPGRADED_ID
                )
            return None

        session.on(SendRichMessage, migrate_once)
        action = await log_action(db, action="delete")
        fallback_services.notify.submit(action)
        await settle(fallback_services)
        assert [call.chat_id for call in session.calls(SendRichMessage)] == [
            FALLBACK_ID,
            UPGRADED_ID,
        ]
        assert fallback_services.access.fallback_chat_id == UPGRADED_ID
        assert await db.migrated_from(UPGRADED_ID) == FALLBACK_ID

    async def test_migrate_error_for_admin_recipient_is_contained(
        self, bot, db, rules, admins, session
    ):
        notify = NotifyService(
            bot,
            db,
            rules,
            admins,
            per_admin_interval=0.001,
            global_per_second=10000,
        )
        await db.upsert_chat(CHAT_ID, "G", "supergroup", NOW)
        await db.set_subscription(CHAT_ID, ADMIN_ID, True, NOW)
        session.on(
            SendRichMessage,
            lambda m: TelegramMigrateToChat(m, "x", migrate_to_chat_id=-1),
        )
        action = await log_action(db, user_id=1, action="delete")
        notify.submit(action)
        await notify.join()
        assert len(session.calls(SendRichMessage)) == 1
