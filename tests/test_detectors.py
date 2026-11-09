from __future__ import annotations

import asyncio
import datetime as dt
import time
from typing import Any

import pytest
from aiogram.methods import (
    BanChatMember,
    DeleteMessages,
)
from aiogram.types import (
    Chat,
    Location,
    MessageEntity,
    MessageOriginChannel,
    Poll,
    PollOption,
    User,
    Venue,
)

from conftest import (
    DHM_SPAM_TEXT,
    FRIENDS_TEXT,
    SPAMMER_ID,
    make_message,
    make_user,
    utf16_len,
)
from tg_antispam_bot.config import Detectors
from tg_antispam_bot.detectors.base import MessageContext
from tg_antispam_bot.detectors.cas import CasDetector
from tg_antispam_bot.detectors.membership import (
    EarlyMessagesDetector,
    EditedSpamDetector,
    ForwardedFromChannelDetector,
    NewMemberDetector,
    RaidDetector,
)
from tg_antispam_bot.detectors.mentions import (
    MentionCountDetector,
    MentionPatternDetector,
)
from tg_antispam_bot.detectors.patterns import (
    DisplayNamePatternDetector,
    LinkPatternDetector,
    TextPatternDetector,
)

NOW = 1_800_000_000


async def raid_on() -> bool:
    return True


def rule(name: str, weight: int = 10, **options):
    return type(getattr(Detectors(), name))(weight=weight, **options)


def ctx_for(message=None, **kwargs) -> MessageContext:
    defaults: dict[str, Any] = dict(
        message=message or make_message(),
        user_id=500,
        display_name="Alice",
        now=NOW,
    )
    defaults.update(kwargs)
    return MessageContext(**defaults)


class TestMentionCount:
    async def test_mass_mention_hits(self):
        detector = MentionCountDetector(rule("mention_count", 40, threshold=5))
        result = await detector.check(ctx_for(make_message(DHM_SPAM_TEXT)))
        assert result is not None
        assert result.score == 40
        assert "8 mentions" in result.reason

    async def test_two_friends_below_threshold(self):
        detector = MentionCountDetector(rule("mention_count", 40, threshold=5))
        assert await detector.check(ctx_for(make_message(FRIENDS_TEXT))) is None

    async def test_threshold_is_inclusive(self):
        detector = MentionCountDetector(rule("mention_count", threshold=2))
        assert (
            await detector.check(ctx_for(make_message(FRIENDS_TEXT)))
            is not None
        )

    async def test_counts_text_mention_entities(self):
        target = User(id=7, is_bot=False, first_name="Bob")
        entities = [
            MessageEntity(type="text_mention", offset=0, length=3, user=target),
            MessageEntity(type="mention", offset=4, length=4),
        ]
        detector = MentionCountDetector(rule("mention_count", threshold=2))
        assert (
            await detector.check(
                ctx_for(make_message("Bob @abc", entities=entities))
            )
            is not None
        )

    async def test_counts_caption_entities(self):
        caption = "look @aaa @bbb @ccc"
        entities = [
            MessageEntity(type="mention", offset=5, length=4),
            MessageEntity(type="mention", offset=10, length=4),
            MessageEntity(type="mention", offset=15, length=4),
        ]
        message = make_message(None, caption=caption, caption_entities=entities)
        detector = MentionCountDetector(rule("mention_count", threshold=3))
        assert await detector.check(ctx_for(message)) is not None


class TestMentionPattern:
    def detector(self, min_matches=2):
        return MentionPatternDetector(
            rule(
                "mention_pattern",
                60,
                patterns=["^dhm_"],
                min_matches=min_matches,
            )
        )

    async def test_matches_case_insensitively(self):
        result = await self.detector().check(
            ctx_for(make_message(DHM_SPAM_TEXT))
        )
        assert result is not None
        assert result.score == 60
        assert "@Dhm_alpha" in result.reason

    async def test_emoji_before_mentions_uses_utf16_offsets(self):
        text = "🚀🚀🚀 @Dhm_one 🌿 @Dhm_two"
        message = make_message(text)
        assert utf16_len(text) != len(text)
        assert await self.detector().check(ctx_for(message)) is not None

    async def test_friends_do_not_match(self):
        assert (
            await self.detector().check(ctx_for(make_message(FRIENDS_TEXT)))
            is None
        )

    async def test_single_match_below_minimum(self):
        assert (
            await self.detector().check(ctx_for(make_message("hi @Dhm_fan")))
            is None
        )

    async def test_prefix_is_anchored(self):
        message = make_message("@xdhm_a @xdhm_b")
        assert await self.detector().check(ctx_for(message)) is None

    async def test_text_mention_username_is_checked(self):
        target = User(
            id=7, is_bot=False, first_name="Bob", username="DHM_hidden"
        )
        entities = [
            MessageEntity(type="text_mention", offset=0, length=3, user=target)
        ]
        message = make_message("Bob", entities=entities)
        assert (
            await self.detector(min_matches=1).check(ctx_for(message))
            is not None
        )

    async def test_invalid_pattern_is_skipped(self):
        detector = MentionPatternDetector(
            rule("mention_pattern", patterns=["(", "^dhm_"], min_matches=1)
        )
        assert [p.pattern for p in detector.patterns] == ["^dhm_"]


class TestNewMember:
    detector = NewMemberDetector(rule("new_member", 20, window="1h"))

    async def test_recent_join_hits(self):
        assert (
            await self.detector.check(ctx_for(joined_at=NOW - 60)) is not None
        )

    async def test_old_join_misses(self):
        assert await self.detector.check(ctx_for(joined_at=NOW - 3601)) is None

    async def test_unknown_join_misses(self):
        assert await self.detector.check(ctx_for(joined_at=None)) is None


class TestEarlyMessages:
    detector = EarlyMessagesDetector(rule("early_messages", 10, first_n=3))

    async def test_within_first_n(self):
        assert await self.detector.check(ctx_for(message_count=3)) is not None

    async def test_past_first_n(self):
        assert await self.detector.check(ctx_for(message_count=4)) is None


class TestTextPattern:
    detector = TextPatternDetector(
        rule("text_pattern", 40, patterns=[r"free\s+crypto"])
    )

    async def test_match(self):
        result = await self.detector.check(
            ctx_for(make_message("Get FREE   crypto now"))
        )
        assert result is not None
        assert result.score == 40

    async def test_no_match(self):
        assert (
            await self.detector.check(ctx_for(make_message("free lunch")))
            is None
        )

    async def test_fullwidth_text_is_normalized(self):
        result = await self.detector.check(
            ctx_for(make_message("ｆｒｅｅ ｃｒｙｐｔｏ"))
        )
        assert result is not None

    async def test_empty_text_misses(self):
        message = make_message(None)
        assert await self.detector.check(ctx_for(message)) is None


class TestRegexSafety:
    EVIL = "(a|aa)+$"
    SUBJECT = "a" * 5000 + "!"

    async def test_catastrophic_pattern_is_bounded(self):
        detector = TextPatternDetector(
            rule("text_pattern", 40, patterns=[self.EVIL])
        )
        started = time.monotonic()
        result = await detector.check(ctx_for(make_message(self.SUBJECT)))
        assert time.monotonic() - started < 1.0
        assert result is None

    async def test_timed_out_pattern_does_not_hide_later_patterns(self):
        detector = TextPatternDetector(
            rule("text_pattern", 40, patterns=[self.EVIL, "aaa"])
        )
        result = await detector.check(ctx_for(make_message(self.SUBJECT)))
        assert result is not None
        assert "aaa" in result.reason

    async def test_slow_pattern_does_not_block_event_loop(self):
        detector = TextPatternDetector(
            rule("text_pattern", 40, patterns=[self.EVIL] * 10)
        )
        ticks = 0

        async def ticker():
            nonlocal ticks
            while True:
                await asyncio.sleep(0.01)
                ticks += 1

        task = asyncio.create_task(ticker())
        await detector.check(ctx_for(make_message(self.SUBJECT)))
        task.cancel()
        assert ticks >= 5


class TestLinkPattern:
    detector = LinkPatternDetector(
        rule("link_pattern", 25, patterns=[r"t\.me/\+"])
    )

    async def test_visible_link(self):
        assert (
            await self.detector.check(ctx_for(make_message("join t.me/+abc")))
            is not None
        )

    async def test_hidden_text_link(self):
        entities = [
            MessageEntity(
                type="text_link", offset=0, length=5, url="https://t.me/+secret"
            )
        ]
        message = make_message("click", entities=entities)
        assert await self.detector.check(ctx_for(message)) is not None

    async def test_other_link_misses(self):
        assert (
            await self.detector.check(
                ctx_for(make_message("https://example.com"))
            )
            is None
        )


class TestDisplayName:
    detector = DisplayNamePatternDetector(
        rule("display_name_pattern", 35, patterns=["airdrop"])
    )

    async def test_match(self):
        assert (
            await self.detector.check(ctx_for(display_name="AIRDROP king"))
            is not None
        )

    async def test_no_match(self):
        assert await self.detector.check(ctx_for(display_name="Alice")) is None


class TestForwardedFromChannel:
    detector = ForwardedFromChannelDetector(rule("forwarded_from_channel", 15))

    def forwarded(self):
        origin = MessageOriginChannel(
            date=dt.datetime.now(tz=dt.UTC),
            chat=Chat(id=-100555, type="channel", title="Promo"),
            message_id=1,
        )
        return make_message("look", forward_origin=origin)

    async def test_new_member_forward_hits(self):
        result = await self.detector.check(
            ctx_for(self.forwarded(), joined_at=NOW - 10)
        )
        assert result is not None
        assert "Promo" in result.reason

    async def test_old_member_forward_misses(self):
        assert (
            await self.detector.check(
                ctx_for(self.forwarded(), joined_at=NOW - 999999)
            )
            is None
        )

    async def test_unknown_join_misses(self):
        assert await self.detector.check(ctx_for(self.forwarded())) is None

    async def test_plain_message_misses(self):
        assert await self.detector.check(ctx_for(joined_at=NOW - 10)) is None


class TestEditedSpam:
    detector = EditedSpamDetector(rule("edited_spam", 20))

    async def test_edit_hits(self):
        assert await self.detector.check(ctx_for(is_edit=True)) is not None

    async def test_new_message_misses(self):
        assert await self.detector.check(ctx_for(is_edit=False)) is None


class TestRaid:
    detector = RaidDetector(rule("raid", 10, joins=3, window="1m"))

    class Counter:
        def __init__(self, value):
            self.value = value
            self.seen = []

        async def __call__(self, seconds):
            self.seen.append(seconds)
            return self.value

    def counter(self, value):
        return self.Counter(value)

    async def test_rate_above_limit_hits(self):
        count = self.counter(4)
        ctx = ctx_for(joined_at=NOW - 5, count_joins=count)
        assert await self.detector.check(ctx) is not None
        assert count.seen == [60]

    async def test_rate_at_limit_misses(self):
        ctx = ctx_for(joined_at=NOW - 5, count_joins=self.counter(3))
        assert await self.detector.check(ctx) is None

    async def test_veteran_is_not_scored(self):
        ctx = ctx_for(joined_at=None, count_joins=self.counter(99))
        assert await self.detector.check(ctx) is None

    async def test_raid_mode_scores_recent_joiner(self):
        ctx = ctx_for(joined_at=NOW - 5, raid_active=raid_on)
        assert await self.detector.check(ctx) is not None


class TestCas:
    detector = CasDetector(rule("cas", 100, timeout="1s", cache_ttl="1m"))

    @staticmethod
    def lookup(value):
        async def run(user_id):
            return value

        return run

    async def test_listed_user_hits(self):
        ctx = ctx_for(message_count=1, cas_lookup=self.lookup(True))
        result = await self.detector.check(ctx)
        assert result is not None
        assert result.score == 100

    async def test_clean_user_misses(self):
        ctx = ctx_for(message_count=1, cas_lookup=self.lookup(False))
        assert await self.detector.check(ctx) is None

    async def test_veteran_is_not_looked_up(self):
        async def explode(user_id):
            raise AssertionError("should not be called")

        ctx = ctx_for(message_count=50, cas_lookup=explode)
        assert await self.detector.check(ctx) is None


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


class TestMentionExtraction:
    def mentions_in(self, question):
        ctx = MessageContext(
            message=poll_message(question),
            user_id=SPAMMER_ID,
            display_name="Spam",
            now=NOW,
        )
        return [mention.username for mention in ctx.mentions]

    def test_usernames_in_a_poll_count_as_mentions(self):
        assert self.mentions_in("hi @Dhm_alpha and @Dhm_bravo") == [
            "Dhm_alpha",
            "Dhm_bravo",
        ]

    @pytest.mark.parametrize(
        "question",
        [
            "mail me at someone@example.com",
            "too short @abcd",
            "starts wrong @1abcde",
            "no at sign Dhm_alpha",
            "double @@Dhm_alpha",
            "dotted a.@Dhm_alpha",
        ],
    )
    def test_things_that_are_not_mentions(self, question):
        assert self.mentions_in(question) == []

    def test_username_length_limit(self):
        assert self.mentions_in("@" + "a" * 32) == ["a" * 32]
        assert self.mentions_in("@" + "a" * 33) == ["a" * 32]

    def test_venue_text_is_read(self):
        venue = Venue(
            location=Location(latitude=1, longitude=1),
            title="@Dhm_alpha",
            address="@Dhm_bravo",
        )
        ctx = MessageContext(
            message=make_message(None, venue=venue),
            user_id=SPAMMER_ID,
            display_name="Spam",
            now=NOW,
        )
        assert len(ctx.mentions) == 2

    def test_entities_in_normal_text_are_not_counted_twice(self):
        ctx = MessageContext(
            message=make_message("hi @Dhm_alpha"),
            user_id=SPAMMER_ID,
            display_name="Spam",
            now=NOW,
        )
        assert len(ctx.mentions) == 1

    async def test_mass_mention_wave_in_a_poll_is_banned(
        self, engine, session, sink
    ):
        question = " ".join(
            f"@Dhm_{name}"
            for name in ("alpha", "bravo", "charlie", "delta", "echo")
        )
        await engine.handle_message(poll_message(question))
        assert session.calls(BanChatMember)[0].user_id == SPAMMER_ID
        reasons = " ".join(sink.actions[0].reasons)
        assert "mention_count" in reasons
        assert "mention_pattern" in reasons

    async def test_two_friends_in_a_poll_are_left_alone(self, engine, session):
        await engine.handle_message(poll_message("@maria_k or @tom_bb?"))
        assert session.calls(DeleteMessages) == []

    def mentions(self, text):
        ctx = MessageContext(
            message=make_message(text),
            user_id=SPAMMER_ID,
            display_name="Spam",
            now=NOW,
        )
        return [mention.username for mention in ctx.mentions]

    def test_repeats_count_once_regardless_of_case(self):
        assert self.mentions("@bob_one @BOB_ONE @bob_one @amy_two") == [
            "bob_one",
            "amy_two",
        ]

    async def test_repeating_one_name_is_not_a_mass_mention(
        self, engine, session
    ):
        text = "@dhm_support can you ask @dhm_support about this?"
        await engine.handle_message(
            make_message(text, user=make_user(SPAMMER_ID))
        )
        assert session.calls(DeleteMessages) == []

    async def test_same_name_as_entity_and_text_mention_is_distinct_by_user(
        self,
    ):
        target = User(id=7, is_bot=False, first_name="Bob")
        entities = [
            MessageEntity(type="text_mention", offset=0, length=3, user=target),
            MessageEntity(type="text_mention", offset=4, length=3, user=target),
        ]
        ctx = MessageContext(
            message=make_message("Bob Bob", entities=entities),
            user_id=SPAMMER_ID,
            display_name="Spam",
            now=NOW,
        )
        assert len(ctx.mentions) == 1
