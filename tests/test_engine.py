from __future__ import annotations

import asyncio
import datetime as dt
import time
from typing import Any

import pytest
from aiogram.enums import ContentType
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramNetworkError,
    TelegramRetryAfter,
)
from aiogram.methods import (
    BanChatMember,
    DeleteMessages,
    GetChat,
    GetChatAdministrators,
    RestrictChatMember,
    UnbanChatMember,
)
from aiogram.types import (
    Chat,
    ChatMemberLeft,
    ChatMemberMember,
    ChatMemberRestricted,
    ChatMemberUpdated,
    Contact,
    Game,
    Invoice,
    Location,
    PhotoSize,
    Poll,
    PollOption,
    User,
    Venue,
)

from conftest import (
    ADMIN_ID,
    CHAT_ID,
    DHM_SPAM_TEXT,
    FRIENDS_TEXT,
    MEMBER_ID,
    OWNER_ID,
    SHIPPED_RULES,
    SPAMMER_ID,
    TEST_RULES,
    USDT_BUYER_TEXT,
    bot_member_event,
    full_chat,
    join_event,
    log_action,
    make_chat,
    make_message,
    make_user,
)
from tg_antispam_bot.config import (
    TIER_BAN,
    TIER_DELETE,
    TIER_RESTRICT,
    ConfigError,
    DetectorRule,
    Tiers,
    load_rules_file,
    parse_chat_rules,
    parse_rules_document,
)
from tg_antispam_bot.detectors import build_detectors
from tg_antispam_bot.detectors.base import (
    DetectionResult,
    Detector,
    MessageContext,
)
from tg_antispam_bot.engine import SERVICE_CONTENT, evaluate, is_scannable

NOW = int(time.time())
TIERS = Tiers(delete=50, delete_and_restrict=70, delete_and_ban=100)


async def raid_on() -> bool:
    return True


def fixed(name, score, kind="signal"):
    class Fixed(Detector):
        async def check(self, ctx):
            return DetectionResult(score, f"{name} fired")

    Fixed.name = name
    Fixed.kind = kind
    return Fixed(DetectorRule(weight=score))


def default_context(message, **kwargs):
    defaults: dict[str, Any] = dict(
        message=message, user_id=SPAMMER_ID, display_name="Spam", now=NOW
    )
    defaults.update(kwargs)
    return MessageContext(**defaults)


class TestScoring:
    async def test_scores_sum_and_map_to_tiers(self):
        ctx = default_context(make_message())
        result = await evaluate(ctx, [fixed("a", 30), fixed("b", 30)], TIERS)
        assert result.score == 60
        assert result.tier == TIER_DELETE

    @pytest.mark.parametrize(
        "score,tier",
        [
            (49, None),
            (50, TIER_DELETE),
            (69, TIER_DELETE),
            (70, TIER_RESTRICT),
            (99, TIER_RESTRICT),
            (100, TIER_BAN),
            (500, TIER_BAN),
        ],
    )
    async def test_tier_boundaries(self, score, tier):
        result = await evaluate(
            default_context(make_message()), [fixed("a", score)], TIERS
        )
        assert result.tier == tier

    async def test_context_alone_never_acts(self):
        detectors = [
            fixed("new_member", 80, "context"),
            fixed("raid", 80, "context"),
        ]
        result = await evaluate(
            default_context(make_message()), detectors, TIERS
        )
        assert result.score == 160
        assert result.tier is None

    async def test_failing_detector_does_not_break_evaluation(self):
        class Broken(Detector):
            name = "broken"

            async def check(self, ctx):
                raise RuntimeError("boom")

        detectors = [Broken(DetectorRule(weight=1)), fixed("a", 60)]
        result = await evaluate(
            default_context(make_message()), detectors, TIERS
        )
        assert result.score == 60

    async def test_reasons_are_labelled(self):
        result = await evaluate(
            default_context(make_message()), [fixed("a", 60)], TIERS
        )
        assert result.reasons == ["a: a fired (+60 pts)"]


class TestDefaultRules:
    def rules(self):
        return parse_chat_rules(parse_rules_document(TEST_RULES).defaults)

    async def test_dhm_mass_mention_from_new_member_is_banned(self):
        rules = self.rules()
        ctx = default_context(
            make_message(DHM_SPAM_TEXT), joined_at=NOW - 30, message_count=1
        )
        result = await evaluate(ctx, build_detectors(rules), rules.tiers)
        assert result.tier == TIER_BAN
        assert {hit.detector for hit in result.hits} >= {
            "mention_count",
            "mention_pattern",
        }

    async def test_dhm_mass_mention_without_join_record_is_banned(self):
        rules = self.rules()
        ctx = default_context(make_message(DHM_SPAM_TEXT), message_count=500)
        result = await evaluate(ctx, build_detectors(rules), rules.tiers)
        assert result.tier == TIER_BAN

    async def test_new_member_tagging_two_friends_is_not_actioned(self):
        rules = self.rules()
        ctx = default_context(
            make_message(FRIENDS_TEXT), joined_at=NOW - 30, message_count=1
        )
        result = await evaluate(ctx, build_detectors(rules), rules.tiers)
        assert result.tier is None

    async def test_new_member_greeting_during_raid_is_not_actioned(self):
        rules = self.rules()
        ctx = default_context(
            make_message("hello everyone"),
            joined_at=NOW - 30,
            message_count=1,
            raid_active=raid_on,
        )
        result = await evaluate(ctx, build_detectors(rules), rules.tiers)
        assert result.tier is None


class TestEngineFlow:
    async def test_dhm_spam_is_deleted_banned_and_logged(
        self, engine, session, sink, db
    ):
        spammer = make_user(SPAMMER_ID, "Spam")
        await engine.handle_join(join_event(SPAMMER_ID, spammer))
        await engine.handle_message(
            make_message(DHM_SPAM_TEXT, user=spammer, message_id=77)
        )

        assert session.calls(DeleteMessages)[0].message_ids == [77]
        assert session.calls(BanChatMember)[0].user_id == SPAMMER_ID
        assert len(sink.actions) == 1
        action = sink.actions[0]
        assert action.action == TIER_BAN
        assert any("mention_pattern" in reason for reason in action.reasons)
        stored = await db.recent_actions(CHAT_ID, 5)
        assert stored[0].id == action.id

    async def test_friends_message_untouched(self, engine, session, sink):
        await engine.handle_message(make_message(FRIENDS_TEXT))
        assert session.calls(DeleteMessages) == []
        assert session.calls(BanChatMember) == []
        assert sink.actions == []

    async def test_admin_is_skipped(self, engine, session):
        await engine.handle_message(
            make_message(DHM_SPAM_TEXT, user=make_user(ADMIN_ID))
        )
        assert session.calls(DeleteMessages) == []

    async def test_anonymous_admin_is_skipped(self, engine, session):
        chat = make_chat()
        anon = User(id=1087968824, is_bot=True, first_name="Group")
        message = make_message(DHM_SPAM_TEXT, user=anon, sender_chat=chat)
        await engine.handle_message(message)
        assert session.calls(DeleteMessages) == []

    async def test_trusted_user_is_skipped(self, engine, session, db):
        await db.add_trusted(CHAT_ID, SPAMMER_ID)
        await engine.handle_message(
            make_message(DHM_SPAM_TEXT, user=make_user(SPAMMER_ID))
        )
        assert session.calls(DeleteMessages) == []

    async def test_disabled_chat_is_skipped(self, engine, session, db, rules):
        await db.set_setting(CHAT_ID, "enabled", False)
        rules.invalidate(CHAT_ID)
        await engine.handle_message(
            make_message(DHM_SPAM_TEXT, user=make_user(SPAMMER_ID))
        )
        assert session.calls(DeleteMessages) == []

    async def test_ephemeral_message_is_never_scanned(self, engine, session):
        message = make_message(
            DHM_SPAM_TEXT,
            user=make_user(SPAMMER_ID),
            message_id=0,
            ephemeral_message_id=9,
        )
        assert await engine.handle_message(message) is None
        assert session.calls(DeleteMessages) == []

    async def test_buffered_messages_are_bulk_deleted(self, engine, session):
        spammer = make_user(SPAMMER_ID, "Spam")
        await engine.handle_join(join_event(SPAMMER_ID, spammer))
        for message_id in (10, 11):
            await engine.handle_message(
                make_message("filler", user=spammer, message_id=message_id)
            )
        await engine.handle_message(
            make_message(DHM_SPAM_TEXT, user=spammer, message_id=12)
        )
        assert session.calls(DeleteMessages)[0].message_ids == [10, 11, 12]

    async def test_bulk_delete_chunks_at_100(self, engine, session, db):
        for message_id in range(1, 251):
            await db.buffer_message(CHAT_ID, SPAMMER_ID, message_id, NOW)
        await engine.handle_message(
            make_message(
                DHM_SPAM_TEXT, user=make_user(SPAMMER_ID), message_id=251
            )
        )
        sizes = [
            len(call.message_ids) for call in session.calls(DeleteMessages)
        ]
        assert sizes == [100, 100, 51]

    async def test_restrict_tier_mutes_until_configured_time(
        self, engine, session
    ):
        message = make_message(
            "free crypto, join t.me/+abc", user=make_user(SPAMMER_ID)
        )
        await engine.handle_join(join_event(SPAMMER_ID))
        await engine.db.set_setting(CHAT_ID, "tiers.delete_and_restrict", 60)
        engine.rules.invalidate(CHAT_ID)
        await engine.db.set_setting(
            CHAT_ID, "pattern_add.text_pattern", ["free crypto"]
        )
        engine.rules.invalidate(CHAT_ID)
        await engine.handle_message(message)
        call = session.calls(RestrictChatMember)[0]
        assert call.permissions.can_send_messages is False
        assert call.until_date > NOW

    async def test_burst_from_one_user_produces_one_action(
        self, engine, sink, session
    ):
        spammer = make_user(SPAMMER_ID, "Spam")
        for message_id in (1, 2, 3):
            await engine.handle_message(
                make_message(DHM_SPAM_TEXT, user=spammer, message_id=message_id)
            )
        assert len(sink.actions) == 1
        assert len(session.calls(BanChatMember)) == 1
        assert sorted(
            i for c in session.calls(DeleteMessages) for i in c.message_ids
        ) == [1, 2, 3]

    async def test_filler_after_ban_is_deleted_without_new_action(
        self, engine, sink, session
    ):
        spammer = make_user(SPAMMER_ID, "Spam")
        await engine.handle_message(
            make_message(DHM_SPAM_TEXT, user=spammer, message_id=1)
        )
        await engine.handle_message(
            make_message("harmless filler", user=spammer, message_id=2)
        )
        assert len(sink.actions) == 1
        assert sorted(
            i for c in session.calls(DeleteMessages) for i in c.message_ids
        ) == [1, 2]

    async def test_edited_message_is_scanned(self, engine, session, sink):
        spammer = make_user(SPAMMER_ID, "Spam")
        await engine.handle_message(
            make_message("innocent", user=spammer, message_id=5)
        )
        assert session.calls(DeleteMessages) == []
        await engine.handle_message(
            make_message(DHM_SPAM_TEXT, user=spammer, message_id=5),
            is_edit=True,
        )
        assert session.calls(DeleteMessages)[0].message_ids == [5]
        assert any(
            "edited_spam" in reason for reason in sink.actions[0].reasons
        )

    async def test_edit_does_not_count_as_new_message(self, engine, db):
        spammer = make_user(SPAMMER_ID, "Spam")
        await engine.handle_message(
            make_message("one", user=spammer, message_id=5)
        )
        await engine.handle_message(
            make_message("two", user=spammer, message_id=5), is_edit=True
        )
        assert await db.message_count(CHAT_ID, SPAMMER_ID) == 1

    async def test_foreign_channel_sender_is_banned_as_chat(
        self, engine, session
    ):
        from aiogram.methods import BanChatSenderChat, GetChat

        session.on(GetChat, lambda method: full_chat())
        channel = Chat(id=-100777, type="channel", title="Spam Channel")
        bot_user = User(id=136817688, is_bot=True, first_name="Channel")
        await engine.handle_message(
            make_message(DHM_SPAM_TEXT, user=bot_user, sender_chat=channel)
        )
        assert session.calls(BanChatSenderChat)[0].sender_chat_id == -100777

    async def test_automatic_forward_from_linked_channel_is_skipped(
        self, engine, session
    ):
        channel = Chat(id=-100777, type="channel", title="Linked")
        bot_user = User(id=777000, is_bot=False, first_name="Telegram")
        message = make_message(
            DHM_SPAM_TEXT,
            user=bot_user,
            sender_chat=channel,
            is_automatic_forward=True,
        )
        await engine.handle_message(message)
        assert session.calls(DeleteMessages) == []

    async def test_admin_lookup_failure_fails_open(self, engine, session):
        from aiogram.exceptions import TelegramNetworkError
        from aiogram.methods import GetChatAdministrators

        session.on(
            GetChatAdministrators, lambda m: TelegramNetworkError(m, "down")
        )
        await engine.handle_message(
            make_message(DHM_SPAM_TEXT, user=make_user(SPAMMER_ID))
        )
        assert session.calls(DeleteMessages) == []

    async def test_ban_failure_is_recorded(self, engine, session, sink):

        session.on(
            BanChatMember, lambda m: TelegramBadRequest(m, "not enough rights")
        )
        await engine.handle_message(
            make_message(DHM_SPAM_TEXT, user=make_user(SPAMMER_ID))
        )
        assert "ban failed" in sink.actions[0].errors[0]

    async def test_retry_after_is_honoured(self, engine, session, sink):
        attempts = []

        def flaky(method):
            attempts.append(1)
            if len(attempts) == 1:
                return TelegramRetryAfter(method, "slow down", retry_after=0)
            return True

        session.on(DeleteMessages, flaky)
        await engine.handle_message(
            make_message(DHM_SPAM_TEXT, user=make_user(SPAMMER_ID))
        )
        assert len(attempts) == 2
        assert sink.actions[0].errors == []

    async def test_lookup_failure_skips_instead_of_banning(
        self, engine, session
    ):
        from aiogram.exceptions import TelegramNetworkError
        from aiogram.methods import BanChatSenderChat, GetChat

        session.on(GetChat, lambda m: TelegramNetworkError(m, "down"))
        channel = Chat(id=-100777, type="channel", title="Linked")
        sender = make_user(136817688, "Channel")
        await engine.handle_message(
            make_message(DHM_SPAM_TEXT, user=sender, sender_chat=channel)
        )
        assert session.calls(BanChatSenderChat) == []
        assert session.calls(DeleteMessages) == []


class TestRaid:
    async def configure(self, engine, joins=3):
        await engine.db.set_setting(CHAT_ID, "detectors.raid.joins", joins)
        engine.rules.invalidate(CHAT_ID)

    async def test_auto_trigger_restricts_every_joiner_after_threshold(
        self, engine, session, sink
    ):
        await self.configure(engine)
        for user_id in range(1, 7):
            await engine.handle_join(join_event(user_id))
        restricted = [
            call.user_id for call in session.calls(RestrictChatMember)
        ]
        assert restricted == [4, 5, 6]
        assert all(action.action == "raid_restrict" for action in sink.actions)
        assert await engine.raid_active(CHAT_ID)

    async def test_raid_mode_expires(self, engine, session):
        await self.configure(engine)
        await engine.set_raid_mode(CHAT_ID, True)
        assert await engine.raid_active(CHAT_ID)
        await engine.db.set_raid(CHAT_ID, engine.now() - 1)
        assert not await engine.raid_active(CHAT_ID)
        await engine.handle_join(join_event(1))
        assert session.calls(RestrictChatMember) == []

    async def test_manual_off_suppresses_auto_trigger(self, engine, session):
        await self.configure(engine, joins=1)
        await engine.set_raid_mode(CHAT_ID, False)
        for user_id in range(1, 5):
            await engine.handle_join(join_event(user_id))
        assert session.calls(RestrictChatMember) == []
        assert not await engine.raid_active(CHAT_ID)

    async def test_manual_on_restricts_first_joiner(self, engine, session):
        await engine.set_raid_mode(CHAT_ID, True)
        await engine.handle_join(join_event(1))
        assert session.calls(RestrictChatMember)[0].user_id == 1

    async def test_bots_are_tracked_but_not_restricted(
        self, engine, session, db, rules
    ):
        await db.set_setting(CHAT_ID, "remove_bots_added_by_members", False)
        rules.invalidate(CHAT_ID)
        await engine.set_raid_mode(CHAT_ID, True)
        other_bot = User(id=55, is_bot=True, first_name="Helper")
        await engine.handle_join(join_event(55, other_bot))
        assert session.calls(RestrictChatMember) == []
        assert await db.last_join(CHAT_ID, 55) is not None

    async def test_trusted_joiner_is_not_restricted(self, engine, session, db):
        await db.add_trusted(CHAT_ID, 1)
        await engine.set_raid_mode(CHAT_ID, True)
        await engine.handle_join(join_event(1))
        assert session.calls(RestrictChatMember) == []

    async def test_join_time_comes_from_the_event(self, engine, db):
        await engine.handle_join(join_event(1, age_seconds=7200))
        recorded = await db.last_join(CHAT_ID, 1)
        assert recorded == pytest.approx(engine.now() - 7200, abs=2)

    async def test_future_event_date_is_clamped_to_now(self, engine, db):
        await engine.handle_join(join_event(1, age_seconds=-3600))
        assert await db.last_join(CHAT_ID, 1) <= engine.now()

    async def test_backlog_after_downtime_does_not_start_a_raid(
        self, engine, session, sink
    ):
        await self.configure(engine)
        for user_id in range(1, 21):
            age = 7200 - user_id * 300
            await engine.handle_join(join_event(user_id, age_seconds=age))
        assert not await engine.raid_active(CHAT_ID)
        assert session.calls(RestrictChatMember) == []
        assert sink.actions == []

    async def test_old_backlog_join_does_not_count_towards_recent_burst(
        self, engine, session
    ):
        await self.configure(engine)
        for user_id in range(1, 4):
            await engine.handle_join(join_event(user_id, age_seconds=7200))
        await engine.handle_join(join_event(4))
        assert not await engine.raid_active(CHAT_ID)
        assert session.calls(RestrictChatMember) == []

    async def test_recent_joins_in_backlog_still_start_a_raid(self, engine):
        await self.configure(engine)
        for user_id in range(1, 5):
            await engine.handle_join(join_event(user_id, age_seconds=30))
        assert await engine.raid_active(CHAT_ID)

    async def test_member_who_joined_long_ago_is_not_new_after_backlog(
        self, engine
    ):
        spammer = make_user(SPAMMER_ID, "Spam")
        event = join_event(SPAMMER_ID, spammer, age_seconds=2 * 86400)
        await engine.handle_join(event)
        evaluation = await engine.handle_message(
            make_message("hello", user=spammer, message_id=5)
        )
        assert evaluation is not None
        names = {hit.detector for hit in evaluation.hits}
        assert "new_member" not in names

    async def test_active_raid_spares_joiner_older_than_the_window(
        self, engine, session, sink, db
    ):
        await engine.set_raid_mode(CHAT_ID, True)
        await engine.handle_join(join_event(1, age_seconds=120))
        assert session.calls(RestrictChatMember) == []
        assert sink.actions == []
        assert await db.last_join(CHAT_ID, 1) is not None

    async def test_active_raid_restricts_joiner_inside_the_window(
        self, engine, session
    ):
        await engine.set_raid_mode(CHAT_ID, True)
        await engine.handle_join(join_event(1, age_seconds=30))
        assert session.calls(RestrictChatMember)[0].user_id == 1

    async def test_raid_cutoff_follows_the_configured_window(
        self, engine, session
    ):
        await engine.db.set_setting(CHAT_ID, "detectors.raid.window", "10m")
        engine.rules.invalidate(CHAT_ID)
        await engine.set_raid_mode(CHAT_ID, True)
        await engine.handle_join(join_event(1, age_seconds=120))
        assert session.calls(RestrictChatMember)[0].user_id == 1

    async def test_one_account_rejoining_does_not_start_a_raid(self, engine):
        await engine.db.set_setting(CHAT_ID, "detectors.raid.joins", 3)
        engine.rules.invalidate(CHAT_ID)
        for _ in range(10):
            await engine.handle_join(join_event(7))
        assert not await engine.raid_active(CHAT_ID)


class TestMessageTiming:
    async def _window(self, rules) -> int:
        return (await rules.get(CHAT_ID)).rules.detectors.new_member.window

    async def _evaluate(self, engine, db, rules, joined_ago, **fields):
        window = await self._window(rules)
        joined_at = int(time.time()) - window - joined_ago
        await db.add_join(CHAT_ID, SPAMMER_ID, joined_at)
        date = dt.datetime.fromtimestamp(joined_at + 10, tz=dt.UTC)
        is_edit = fields.pop("is_edit", False)
        edit_offset = fields.pop("edit_offset", None)
        if edit_offset is not None:
            fields["edit_date"] = joined_at + edit_offset
        message = make_message(
            "hello", user=make_user(SPAMMER_ID), date=date, **fields
        )
        return await engine.handle_message(message, is_edit=is_edit)

    async def test_delayed_message_is_timed_when_sent(self, engine, db, rules):
        evaluation = await self._evaluate(engine, db, rules, joined_ago=100)
        assert any(
            r.startswith("new_member: joined 10s ago")
            for r in evaluation.reasons
        )

    async def test_edit_is_timed_when_edited(self, engine, db, rules):
        evaluation = await self._evaluate(
            engine, db, rules, joined_ago=100, is_edit=True, edit_offset=40
        )
        assert any(
            r.startswith("new_member: joined 40s ago")
            for r in evaluation.reasons
        )

    async def test_future_date_is_clamped_to_clock(self, engine, db):
        await db.add_join(CHAT_ID, SPAMMER_ID, int(time.time()) - 10)
        date = dt.datetime.now(tz=dt.UTC) + dt.timedelta(days=1)
        message = make_message("hello", user=make_user(SPAMMER_ID), date=date)
        evaluation = await engine.handle_message(message)
        assert any("new_member" in r for r in evaluation.reasons)


SPAM = "free crypto, join t.me/+abc"


def poll_message(question=SPAM):
    poll = Poll(
        id="1",
        question=question,
        options=[PollOption(persistent_id="1", text="yes", voter_count=0)],
        total_voter_count=0,
        is_closed=False,
        is_anonymous=True,
        type="regular",
        allows_multiple_answers=False,
        allows_revoting=False,
        members_only=False,
    )
    return make_message(None, user=make_user(SPAMMER_ID), poll=poll)


async def enable_text_rules(db, rules):
    await db.set_setting(CHAT_ID, "pattern_add.text_pattern", ["free crypto"])
    await db.set_setting(CHAT_ID, "pattern_add.link_pattern", [r"t\.me/\+"])
    rules.invalidate(CHAT_ID)


LINK = "join us t.me/+abcdefgh"


class TestScanningCoverage:
    USER_CONTENT = {
        ContentType.UNKNOWN,
        ContentType.ANY,
        ContentType.TEXT,
        ContentType.ANIMATION,
        ContentType.AUDIO,
        ContentType.DOCUMENT,
        ContentType.LIVE_PHOTO,
        ContentType.PAID_MEDIA,
        ContentType.PHOTO,
        ContentType.STICKER,
        ContentType.STORY,
        ContentType.VIDEO,
        ContentType.VIDEO_NOTE,
        ContentType.VOICE,
        ContentType.CHECKLIST,
        ContentType.CONTACT,
        ContentType.DICE,
        ContentType.GAME,
        ContentType.POLL,
        ContentType.VENUE,
        ContentType.LOCATION,
        ContentType.INVOICE,
        ContentType.GIVEAWAY,
        ContentType.RICH_MESSAGE,
    }

    def test_every_content_type_is_classified(self):
        assert self.USER_CONTENT.isdisjoint(SERVICE_CONTENT)
        assert self.USER_CONTENT | SERVICE_CONTENT == set(ContentType)

    def test_new_member_notice_is_not_scanned(self):
        message = make_message(
            None, new_chat_members=[make_user(SPAMMER_ID, "Spam")]
        )
        assert not is_scannable(message)

    def test_unrecognised_content_is_scanned(self):
        assert is_scannable(make_message(None))

    @pytest.mark.parametrize(
        "fields",
        [
            {"poll": True},
            {
                "contact": Contact(
                    phone_number="1", first_name="free crypto t.me/+abc"
                )
            },
            {
                "venue": Venue(
                    location=Location(latitude=1, longitude=1),
                    title="free crypto",
                    address="t.me/+abc",
                )
            },
            {
                "game": Game(
                    title="free crypto",
                    description="t.me/+abc",
                    photo=[
                        PhotoSize(
                            file_id="1", file_unique_id="1", width=1, height=1
                        )
                    ],
                )
            },
            {
                "invoice": Invoice(
                    title="free crypto",
                    description="t.me/+abc",
                    start_parameter="x",
                    currency="USD",
                    total_amount=1,
                )
            },
        ],
    )
    async def test_non_text_spam_is_actioned(
        self, engine, db, rules, session, sink, fields
    ):
        await enable_text_rules(db, rules)
        if fields.get("poll"):
            message = poll_message()
        else:
            message = make_message(None, user=make_user(SPAMMER_ID), **fields)
        evaluation = await engine.handle_message(message)
        assert evaluation is not None
        names = {hit.detector for hit in evaluation.hits}
        assert {"text_pattern", "link_pattern"} <= names
        assert evaluation.tier is not None
        assert session.calls(DeleteMessages)
        assert "free crypto" in sink.actions[0].message_text

    async def test_paid_media_caption_is_scanned(self, engine, db, rules):
        await enable_text_rules(db, rules)
        message = make_message(
            None,
            user=make_user(SPAMMER_ID),
            caption=SPAM,
            paid_media={"star_count": 1, "paid_media": []},
        )
        assert message.content_type == ContentType.PAID_MEDIA
        evaluation = await engine.handle_message(message)
        assert evaluation is not None and evaluation.tier is not None

    async def test_harmless_poll_is_left_alone(
        self, engine, session, db, rules
    ):
        await enable_text_rules(db, rules)
        await engine.handle_message(poll_message("Pizza tonight?"))
        assert session.calls(DeleteMessages) == []


class TestSanctionGate:
    async def ban(self, engine):
        spammer = make_user(SPAMMER_ID, "Spam")
        await engine.handle_message(
            make_message(DHM_SPAM_TEXT, user=spammer, message_id=1)
        )
        return spammer

    def member_change(self, new_member):
        return ChatMemberUpdated(
            chat=make_chat(),
            from_user=make_user(ADMIN_ID),
            date=dt.datetime.now(tz=dt.UTC),
            old_chat_member=ChatMemberMember(user=new_member.user),
            new_chat_member=new_member,
        )

    def restricted(self, user, can_send):
        return ChatMemberRestricted(
            user=user,
            is_member=True,
            can_send_messages=can_send,
            can_send_audios=can_send,
            can_send_documents=can_send,
            can_send_photos=can_send,
            can_send_videos=can_send,
            can_send_video_notes=can_send,
            can_send_voice_notes=can_send,
            can_send_polls=can_send,
            can_send_other_messages=can_send,
            can_add_web_page_previews=can_send,
            can_edit_tag=False,
            can_react_to_messages=can_send,
            can_change_info=False,
            can_invite_users=False,
            can_pin_messages=False,
            can_manage_topics=False,
            until_date=dt.datetime.now(tz=dt.UTC),
        )

    async def test_admin_lifting_in_telegram_stops_silent_deletes(
        self, feed, services, session
    ):
        spammer = await self.ban(services.engine)
        await feed(
            chat_member=self.member_change(ChatMemberMember(user=spammer))
        )
        session.clear()
        await feed(
            message=make_message("hello again", user=spammer, message_id=2)
        )
        assert session.calls(DeleteMessages) == []

    async def test_restriction_with_send_rights_clears_gate(
        self, feed, services, session
    ):
        spammer = await self.ban(services.engine)
        await feed(
            chat_member=self.member_change(self.restricted(spammer, True))
        )
        session.clear()
        await feed(message=make_message("hi", user=spammer, message_id=2))
        assert session.calls(DeleteMessages) == []

    async def test_bots_own_restriction_keeps_gate(
        self, feed, services, session
    ):
        spammer = await self.ban(services.engine)
        await feed(
            chat_member=self.member_change(self.restricted(spammer, False))
        )
        session.clear()
        await feed(message=make_message("hi", user=spammer, message_id=2))
        assert session.calls(DeleteMessages)[0].message_ids == [2]

    async def test_expired_short_restriction_stops_silent_deletes(
        self, engine, db, rules, session, sink
    ):
        await enable_text_rules(db, rules)
        await db.set_setting(CHAT_ID, "restrict_duration", "1m")
        await db.set_setting(CHAT_ID, "tiers.delete_and_restrict", 60)
        rules.invalidate(CHAT_ID)
        moment = [float(NOW)]
        engine.clock = lambda: moment[0]
        spammer = make_user(SPAMMER_ID, "Spam")
        await engine.handle_message(
            make_message(SPAM, user=spammer, message_id=1)
        )
        assert session.calls(RestrictChatMember)
        session.clear()
        await engine.handle_message(
            make_message("still muted", user=spammer, message_id=2)
        )
        assert session.calls(DeleteMessages)[0].message_ids == [2]
        session.clear()
        moment[0] += 61
        await engine.handle_message(
            make_message("hello", user=spammer, message_id=3)
        )
        assert session.calls(DeleteMessages) == []


class TestNameAndEditSignals:
    NAME = "Airdrop Hunter"

    @pytest.fixture
    async def flagged_names(self, db, rules):
        await db.set_setting(
            CHAT_ID, "pattern_add.display_name_pattern", ["airdrop"]
        )
        await db.set_setting(CHAT_ID, "pattern_add.link_pattern", [r"t\.me/\+"])
        rules.invalidate(CHAT_ID)

    async def send(self, engine, text, message_id, is_edit=False):
        user = make_user(SPAMMER_ID, self.NAME)
        return await engine.handle_message(
            make_message(text, user=user, message_id=message_id),
            is_edit=is_edit,
        )

    async def test_new_member_with_flagged_name_can_say_hello(
        self, engine, flagged_names, sink
    ):
        await engine.handle_join(join_event(SPAMMER_ID))
        for message_id in (1, 2, 3):
            await self.send(engine, "hello everyone", message_id)
        assert sink.actions == []

    async def test_flagged_name_still_adds_to_real_spam(
        self, engine, flagged_names, sink
    ):
        await engine.handle_join(join_event(SPAMMER_ID))
        evaluation = await self.send(engine, f"join {LINK}", 1)
        assert "display_name_pattern" in [h.detector for h in evaluation.hits]
        assert sink.actions[0].action == "delete_and_restrict"

    async def test_editing_after_renaming_is_not_spam(
        self, engine, flagged_names, sink
    ):
        evaluation = await self.send(engine, "fixed a typo", 1, is_edit=True)
        assert "edited_spam" not in [h.detector for h in evaluation.hits]
        assert sink.actions == []

    async def test_spam_added_by_editing_still_counts(
        self, engine, flagged_names, sink
    ):
        evaluation = await self.send(engine, LINK, 1, is_edit=True)
        assert "edited_spam" in [h.detector for h in evaluation.hits]


class TestEscalation:
    async def post(self, engine, text=LINK, message_id=1):
        user = make_user(SPAMMER_ID, "Spam")
        return await engine.handle_message(
            make_message(text, user=user, message_id=message_id)
        )

    @pytest.fixture
    async def active(self, engine, db, rules):
        await db.set_setting(CHAT_ID, "pattern_add.link_pattern", [r"t\.me/\+"])
        await db.set_setting(CHAT_ID, "tiers.delete", 20)
        rules.invalidate(CHAT_ID)
        return engine

    async def test_first_offence_only_deletes(self, active, session, sink):
        await self.post(active)
        assert session.calls(RestrictChatMember) == []
        assert sink.actions[0].action == "delete"

    async def test_second_offence_restricts(self, active, session, sink):
        await self.post(active, message_id=1)
        await self.post(active, message_id=2)
        assert session.calls(RestrictChatMember)[0].user_id == SPAMMER_ID
        action = sink.actions[1]
        assert action.action == "delete_and_restrict"
        assert action.until_ts is not None
        assert any("escalation: offence 2" in r for r in action.reasons)

    async def test_third_offence_bans_after_restriction_ends(
        self, active, session, sink
    ):
        moment = [float(NOW)]
        active.clock = lambda: moment[0]
        await self.post(active, message_id=1)
        await self.post(active, message_id=2)
        await active.db.set_setting(CHAT_ID, "restrict_duration", "1m")
        active.clear_sanction(CHAT_ID, SPAMMER_ID)
        await self.post(active, message_id=3)
        assert session.calls(BanChatMember)[0].user_id == SPAMMER_ID
        assert sink.actions[2].action == "delete_and_ban"

    async def test_message_during_restriction_is_not_a_new_offence(
        self, active, sink
    ):
        await self.post(active, message_id=1)
        await self.post(active, message_id=2)
        await self.post(active, message_id=3)
        assert len(sink.actions) == 2

    async def test_old_offences_fall_out_of_the_window(self, active, sink):
        moment = [float(NOW)]
        active.clock = lambda: moment[0]
        await self.post(active, message_id=1)
        moment[0] += 601
        await self.post(active, message_id=2)
        assert sink.actions[1].action == "delete"

    async def test_resolved_actions_do_not_count(self, active, db, sink):
        await self.post(active, message_id=1)
        await db.claim_resolution(sink.actions[0].id, 1, "A", "fp", NOW)
        await self.post(active, message_id=2)
        assert sink.actions[1].action == "delete"

    async def test_raid_restrictions_do_not_count(self, active, db, sink):
        await log_action(db, action="raid_restrict", now=active.now())
        await self.post(active)
        assert sink.actions[0].action == "delete"

    async def test_other_users_and_chats_do_not_count(self, active, db, sink):
        for chat_id, user_id in ((CHAT_ID, 1), (-5, SPAMMER_ID)):
            await log_action(
                db,
                chat_id=chat_id,
                user_id=user_id,
                action="delete",
                now=active.now(),
            )
        await self.post(active)
        assert sink.actions[0].action == "delete"

    async def test_escalation_never_lowers_a_penalty(self, active, sink):
        await self.post(active, DHM_SPAM_TEXT)
        assert sink.actions[0].action == "delete_and_ban"
        assert not any("escalation" in r for r in sink.actions[0].reasons)

    async def test_second_offence_keeps_a_higher_penalty(self, active, sink):
        await self.post(active, message_id=1)
        await self.post(active, DHM_SPAM_TEXT, message_id=2)
        assert sink.actions[1].action == "delete_and_ban"
        assert not any("escalation" in r for r in sink.actions[1].reasons)

    async def test_can_be_turned_off(self, active, db, rules, session, sink):
        await db.set_setting(CHAT_ID, "escalation.enabled", False)
        rules.invalidate(CHAT_ID)
        for message_id in (1, 2, 3):
            await self.post(active, message_id=message_id)
        assert [a.action for a in sink.actions] == ["delete"] * 3

    async def test_thresholds_are_configurable(self, active, db, rules, sink):
        await db.set_setting(CHAT_ID, "escalation.restrict_after", 3)
        await db.set_setting(CHAT_ID, "escalation.ban_after", 5)
        rules.invalidate(CHAT_ID)
        for message_id in (1, 2, 3):
            await self.post(active, message_id=message_id)
        assert [a.action for a in sink.actions] == [
            "delete",
            "delete",
            "delete_and_restrict",
        ]

    async def test_concurrent_posts_are_counted_one_at_a_time(
        self, active, sink
    ):
        await asyncio.gather(
            *(self.post(active, message_id=i) for i in range(1, 6))
        )
        assert sorted(a.action for a in sink.actions) == [
            "delete",
            "delete_and_ban",
            "delete_and_restrict",
        ]

    @pytest.mark.parametrize(
        "document",
        [
            {"restrict_after": 1},
            {"ban_after": 1},
            {"restrict_after": 4, "ban_after": 3},
            {"window": 600},
            {"unknown": 1},
        ],
    )
    def test_invalid_settings_are_rejected(self, document):
        with pytest.raises(ConfigError):
            parse_rules_document({"defaults": {"escalation": document}})

    async def test_confirmed_counts_but_corrections_do_not(self, db, services):
        resolve = services.moderation.resolve
        confirmed = await log_action(db, action="delete")
        await resolve(confirmed.id, "confirm", ADMIN_ID, "A")
        corrected = await log_action(db, action="delete")
        await resolve(corrected.id, "unban", ADMIN_ID, "A")
        assert await db.count_offences_since(CHAT_ID, SPAMMER_ID, 0) == 1


class TestJoinChurn:
    @pytest.fixture
    async def link_rules(self, engine, db, rules):
        await db.set_setting(CHAT_ID, "pattern_add.link_pattern", [r"t\.me/\+"])
        rules.invalidate(CHAT_ID)
        for _ in range(4):
            await db.bump_message_count(CHAT_ID, SPAMMER_ID, NOW)
        return engine

    async def post(self, engine, text, message_id=1):
        user = make_user(SPAMMER_ID, "Spam")
        return await engine.handle_message(
            make_message(text, user=user, message_id=message_id)
        )

    async def test_link_stays_after_only_one_join(self, link_rules, session):
        await link_rules.handle_join(join_event(SPAMMER_ID))
        evaluation = await self.post(link_rules, LINK)
        assert evaluation is not None and evaluation.score == 45
        assert session.calls(DeleteMessages) == []

    async def test_repeat_joiner_pushes_link_over_the_threshold(
        self, link_rules, session
    ):
        for _ in range(3):
            await link_rules.handle_join(join_event(SPAMMER_ID))
        evaluation = await self.post(link_rules, LINK)
        assert evaluation is not None and evaluation.score == 60
        assert "join_churn" in {hit.detector for hit in evaluation.hits}
        assert session.calls(DeleteMessages)

    async def test_rejoining_alone_never_acts(self, link_rules, session):
        for _ in range(10):
            await link_rules.handle_join(join_event(SPAMMER_ID))
        evaluation = await self.post(link_rules, "hello everyone")
        assert evaluation is not None
        assert "join_churn" in {hit.detector for hit in evaluation.hits}
        assert evaluation.tier is None
        assert session.calls(DeleteMessages) == []

    async def test_churn_cannot_act_alone_at_any_weight(
        self, link_rules, db, rules, session
    ):
        await db.set_setting(CHAT_ID, "detectors.join_churn.weight", 500)
        rules.invalidate(CHAT_ID)
        for _ in range(3):
            await link_rules.handle_join(join_event(SPAMMER_ID))
        evaluation = await self.post(link_rules, "hello everyone")
        assert evaluation is not None
        assert evaluation.score >= 500
        assert evaluation.tier is None
        assert session.calls(DeleteMessages) == []

    async def test_old_joins_fall_out_of_the_window(self, link_rules, db):
        for _ in range(3):
            await db.add_join(CHAT_ID, SPAMMER_ID, link_rules.now() - 601)
        await link_rules.handle_join(join_event(SPAMMER_ID))
        evaluation = await self.post(link_rules, LINK)
        assert evaluation is not None
        assert "join_churn" not in {hit.detector for hit in evaluation.hits}

    async def test_other_accounts_joins_do_not_count(self, link_rules):
        for user_id in (1, 2, 3):
            await link_rules.handle_join(join_event(user_id))
        evaluation = await self.post(link_rules, LINK)
        assert evaluation is not None
        assert "join_churn" not in {hit.detector for hit in evaluation.hits}

    async def test_can_be_turned_off(self, link_rules, db, rules):
        await db.set_setting(CHAT_ID, "detectors.join_churn.enabled", False)
        rules.invalidate(CHAT_ID)
        for _ in range(3):
            await link_rules.handle_join(join_event(SPAMMER_ID))
        evaluation = await self.post(link_rules, LINK)
        assert evaluation is not None and evaluation.score == 45


class TestLowestTierDeletesOneMessage:
    async def test_delete_tier_keeps_earlier_messages(
        self, engine, db, rules, session
    ):
        await db.set_setting(CHAT_ID, "pattern_add.link_pattern", [r"t\.me/\+"])
        rules.invalidate(CHAT_ID)
        spammer = make_user(SPAMMER_ID)
        await engine.handle_join(join_event(SPAMMER_ID))
        await engine.handle_message(
            make_message("hello", user=spammer, message_id=1)
        )
        await engine.handle_message(
            make_message(LINK, user=spammer, message_id=2)
        )
        assert session.calls(DeleteMessages)[0].message_ids == [2]
        assert await db.buffered_messages(CHAT_ID, SPAMMER_ID) == [1]

    async def test_escalation_then_bulk_deletes_everything(
        self, engine, db, rules, session
    ):
        await db.set_setting(CHAT_ID, "pattern_add.link_pattern", [r"t\.me/\+"])
        rules.invalidate(CHAT_ID)
        spammer = make_user(SPAMMER_ID)
        await engine.handle_join(join_event(SPAMMER_ID))
        await engine.handle_message(
            make_message("hello", user=spammer, message_id=1)
        )
        await engine.handle_message(
            make_message(LINK, user=spammer, message_id=2)
        )
        await engine.handle_message(
            make_message(LINK, user=spammer, message_id=3)
        )
        assert session.calls(DeleteMessages)[1].message_ids == [1, 3]


class TestPerUserLocks:
    async def test_flood_wait_does_not_block_another_user(
        self, engine, session
    ):
        blocked = asyncio.Event()
        release = asyncio.Event()
        real = engine.executor.apply

        async def apply(**kwargs):
            if kwargs["user_id"] == 1:
                blocked.set()
                await release.wait()
            return await real(**kwargs)

        engine.executor.apply = apply
        slow = asyncio.create_task(
            engine.handle_message(
                make_message(DHM_SPAM_TEXT, user=make_user(1), message_id=1)
            )
        )
        await blocked.wait()
        for key in range(2, 5):
            await asyncio.wait_for(
                engine.handle_message(
                    make_message(
                        DHM_SPAM_TEXT, user=make_user(key), message_id=key
                    )
                ),
                timeout=5,
            )
        release.set()
        await slow


SPAM_BOT = User(id=8800, is_bot=True, first_name="Direct advertising")


def bot_added(adder_id):
    return bot_member_event(
        ChatMemberMember(user=SPAM_BOT),
        ChatMemberLeft(user=SPAM_BOT),
        adder=adder_id,
    )


class TestOtherBots:
    async def test_spam_from_a_bot_is_actioned(self, engine, session):
        await engine.handle_message(make_message(DHM_SPAM_TEXT, user=SPAM_BOT))
        assert session.calls(BanChatMember)[0].user_id == SPAM_BOT.id

    async def test_guest_bot_summoned_by_member_is_banned(
        self, engine, session, sink
    ):
        await engine.handle_message(
            make_message(
                "hello",
                user=SPAM_BOT,
                message_id=78,
                guest_bot_caller_user=make_user(SPAMMER_ID),
            )
        )
        assert session.calls(DeleteMessages)[0].message_ids == [78]
        assert session.calls(BanChatMember)[0].user_id == SPAM_BOT.id
        assert sink.actions[0].action == TIER_BAN
        assert sink.actions[0].reasons == [
            "guest bot summoned by a non-admin: Alice"
        ]

    async def test_banned_guest_bot_is_not_banned_again(self, engine, session):
        for message_id in (78, 79):
            await engine.handle_message(
                make_message(
                    "hello",
                    user=SPAM_BOT,
                    message_id=message_id,
                    guest_bot_caller_user=make_user(SPAMMER_ID),
                )
            )
        assert session.calls(DeleteMessages)[1].message_ids == [79]
        assert len(session.calls(BanChatMember)) == 1

    async def test_guest_bot_removal_can_be_turned_off(
        self, engine, session, db, rules
    ):
        await db.set_setting(
            CHAT_ID, "remove_guest_bots_summoned_by_members", False
        )
        rules.invalidate(CHAT_ID)
        member = make_user(SPAMMER_ID)
        await engine.handle_message(
            make_message("hello", user=SPAM_BOT, guest_bot_caller_user=member)
        )
        assert session.calls(DeleteMessages) == []
        await engine.handle_message(
            make_message(
                DHM_SPAM_TEXT, user=SPAM_BOT, guest_bot_caller_user=member
            )
        )
        assert session.calls(BanChatMember)[0].user_id == SPAM_BOT.id

    async def test_guest_bot_summoned_by_a_channel_is_banned(
        self, engine, session
    ):
        session.on(GetChat, lambda method: full_chat())
        channel = make_chat(-1009, "Promo", "channel")
        await engine.handle_message(
            make_message("hello", user=SPAM_BOT, guest_bot_caller_chat=channel)
        )
        assert session.calls(BanChatMember)[0].user_id == SPAM_BOT.id

    @pytest.mark.parametrize(
        "caller",
        [
            {"guest_bot_caller_user": make_user(ADMIN_ID)},
            {"guest_bot_caller_user": make_user(MEMBER_ID)},
            {"guest_bot_caller_chat": make_chat()},
        ],
        ids=["admin", "trusted", "anonymous_admin"],
    )
    async def test_guest_bot_summoned_by_admin_or_trusted_is_kept(
        self, engine, session, db, caller
    ):
        await db.add_trusted(CHAT_ID, MEMBER_ID)
        await engine.handle_message(
            make_message(DHM_SPAM_TEXT, user=SPAM_BOT, **caller)
        )
        assert session.calls(DeleteMessages) == []

    async def test_admin_bots_are_exempt(self, engine, session, admins):
        session.set_admins(CHAT_ID, OWNER_ID, ADMIN_ID, SPAM_BOT.id)
        admins.invalidate(CHAT_ID)
        await engine.handle_message(make_message(DHM_SPAM_TEXT, user=SPAM_BOT))
        assert session.calls(DeleteMessages) == []

    async def test_bot_added_by_a_member_is_removed(
        self, engine, session, sink, caplog
    ):
        caplog.set_level("INFO")
        await engine.handle_join(bot_added(MEMBER_ID))
        assert "removed bot added by a non-admin" in caplog.messages
        assert session.calls(BanChatMember)[0].user_id == SPAM_BOT.id
        assert (sink.actions[0].user_id, sink.actions[0].action) == (
            SPAM_BOT.id,
            TIER_BAN,
        )

    async def test_removed_bot_can_be_unbanned_from_its_card(
        self, services, session, db
    ):
        await services.engine.handle_join(bot_added(MEMBER_ID))
        [action] = await db.recent_actions(CHAT_ID, 1)
        await services.moderation.resolve(action.id, "unban", ADMIN_ID, "A")
        assert session.calls(UnbanChatMember)[0].user_id == SPAM_BOT.id

    async def test_trusted_bot_is_not_removed_again(self, engine, db, session):
        await db.add_trusted(CHAT_ID, SPAM_BOT.id)
        await engine.handle_join(bot_added(MEMBER_ID))
        assert session.calls(BanChatMember) == []

    async def test_bot_added_by_an_admin_stays(self, engine, session, sink):
        await engine.handle_join(bot_added(ADMIN_ID))
        assert session.calls(BanChatMember) == []
        assert sink.actions == []

    async def test_removal_can_be_turned_off(self, engine, db, rules, session):
        await db.set_setting(CHAT_ID, "remove_bots_added_by_members", False)
        rules.invalidate(CHAT_ID)
        await engine.handle_join(bot_added(MEMBER_ID))
        assert session.calls(BanChatMember) == []

    async def test_bot_stays_when_admins_cannot_be_checked(
        self, engine, session
    ):
        session.on(
            GetChatAdministrators, lambda m: TelegramNetworkError(m, "down")
        )
        await engine.handle_join(bot_added(MEMBER_ID))
        assert session.calls(BanChatMember) == []


class TestShippedScamPatterns:
    @pytest.fixture
    def rules_file(self):
        return load_rules_file(SHIPPED_RULES)

    @pytest.mark.parametrize(
        "text",
        [
            "We can buy your USDT, message me",
            "Paying 10% higher than the market rate",
            "We provide real US phone numbers for SMS verification",
            "Due to Chinese regulations, please contact me",
            "We are unable to purchase cryptocurrency directly",
            "Paying a premium of 5% to 20% above the market rate",
            "Due to policies restricting large-scale USDT purchases",
            "join us t.me/+abcdefgh",
            USDT_BUYER_TEXT,
        ],
    )
    async def test_scam_from_a_longtime_member_is_deleted(
        self, engine, db, sink, text
    ):
        for _ in range(3):
            await db.bump_message_count(CHAT_ID, SPAMMER_ID, NOW)
        await engine.handle_message(
            make_message(text, user=make_user(SPAMMER_ID))
        )
        assert sink.actions[0].action == TIER_DELETE

    async def test_new_member_summoning_ad_bots_is_banned(
        self, engine, session
    ):
        await engine.handle_join(join_event(SPAMMER_ID))
        await engine.handle_message(
            make_message(
                "@NsiMzbPd_Dbot @Nwip5OphNbot @nZRjY3R50bot",
                user=make_user(SPAMMER_ID),
            )
        )
        assert session.calls(BanChatMember)[0].user_id == SPAMMER_ID

    async def test_new_member_tagging_two_friends_is_left_alone(
        self, engine, sink
    ):
        await engine.handle_join(join_event(SPAMMER_ID))
        await engine.handle_message(
            make_message("Hi @maria_k and @tom_b", user=make_user(SPAMMER_ID))
        )
        assert sink.actions == []

    @pytest.mark.parametrize(
        "text",
        [
            "Is USDT on Tron cheaper to send than on Ethereum?",
            "What's the USDT exchange rate today?",
            "Anyone know a good OTC desk? The best rates were on Kraken",
            "Paid via UPI, the settlement took 2 days",
        ],
    )
    async def test_ordinary_crypto_talk_is_left_alone(self, engine, sink, text):
        await engine.handle_message(
            make_message(text, user=make_user(SPAMMER_ID))
        )
        assert sink.actions == []
