from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramMigrateToChat,
    TelegramNetworkError,
    TelegramRetryAfter,
)
from aiogram.methods import (
    EditMessageText,
    GetChatAdministrators,
    SendRichMessage,
)
from sqlalchemy import update

from conftest import (
    ADMIN_ID,
    CHAT_ID,
    OWNER_ID,
    SPAMMER_ID,
    card_text,
    tables,
    verbs,
)
from conftest import log_action as shared_log_action
from tg_antispam_bot.db import (
    ACTION_BAN,
    ACTION_DELETE,
    ACTION_RESTRICT,
    PREF_BANS_ONLY,
    PREF_DIGEST_ONLY,
)
from tg_antispam_bot.models import DigestRecord
from tg_antispam_bot.notify import NotifyService, RateLimiter

NOW = int(time.time())
OTHER_ADMIN = 102
LONG_TEXT = "spam " * 400


NOTIFY_ACTION = {
    "action": ACTION_DELETE,
    "user_name": "Spam <b>Bot</b>",
    "message_text": "buy now",
    "reasons": [
        "mention_count: 8 mentions (threshold 5)",
        "mention_pattern: 8 mentions match",
    ],
}


async def log_action(db, **fields):
    return await shared_log_action(db, **{**NOTIFY_ACTION, **fields})


@pytest.fixture
def make_notify(bot, db, rules, admins):
    def factory(**kwargs):
        options: dict[str, Any] = dict(
            per_admin_interval=0.001,
            global_per_second=10000,
            digest_edit_interval=0.0,
            verify_retry_seconds=0.001,
        )
        options.update(kwargs)
        return NotifyService(bot, db, rules, admins, **options)

    return factory


@pytest.fixture(autouse=True)
async def chat_row(db):
    await db.upsert_chat(CHAT_ID, "Test Group", "supergroup", NOW)


async def subscribe(db, *user_ids):
    for user_id in user_ids:
        await db.set_subscription(CHAT_ID, user_id, True, NOW)


async def publish(notify, db, count=1, **kwargs):
    for _ in range(count):
        notify.submit(await log_action(db, **kwargs))
        await notify.join()


class TestRateLimiter:
    async def test_pause_delays_next_slot(self):
        limiter = RateLimiter(1000.0)
        limiter.pause(0.05)
        started = time.monotonic()
        await limiter.acquire()
        assert time.monotonic() - started >= 0.04


class TestCards:
    async def test_card_has_required_fields_and_buttons(
        self, make_notify, db, session
    ):
        await subscribe(db, ADMIN_ID)
        notify = make_notify()
        await publish(notify, db, action=ACTION_BAN)
        sent = session.calls(SendRichMessage)
        assert len(sent) == 1
        call = sent[0]
        assert call.chat_id == ADMIN_ID
        text = card_text(call)
        assert "Test Group" in text
        assert str(SPAMMER_ID) in text
        assert "130" in text
        assert "8 mentions (threshold 5)" in text
        assert "buy now" in text
        assert "unban" in verbs(call.reply_markup)

    async def test_user_supplied_text_is_escaped(
        self, make_notify, db, session
    ):
        await subscribe(db, ADMIN_ID)
        await publish(make_notify(), db, message_text="<script>&")
        text = card_text(session.calls(SendRichMessage)[0])
        assert "<script>&" in text
        assert "Spam <b>Bot</b>" in text

    async def test_one_card_per_subscribed_admin(
        self, make_notify, db, session
    ):
        await subscribe(db, ADMIN_ID, OWNER_ID)
        await publish(make_notify(), db)
        assert sorted(
            call.chat_id for call in session.calls(SendRichMessage)
        ) == [
            OWNER_ID,
            ADMIN_ID,
        ]

    async def test_sent_cards_are_recorded_for_later_edits(
        self, make_notify, db
    ):
        await subscribe(db, ADMIN_ID)
        notify = make_notify()
        action = await log_action(db)
        notify.submit(action)
        await notify.join()
        assert [row[0] for row in await db.action_dms(action.id)] == [ADMIN_ID]


class TestRateLimiting:
    async def test_per_admin_interval_is_enforced(
        self, make_notify, db, session
    ):
        await subscribe(db, ADMIN_ID)
        notify = make_notify(per_admin_interval=0.05)
        for _ in range(4):
            notify.submit(await log_action(db))
        await notify.join()
        times = [
            stamp
            for stamp, request in zip(
                session.times, session.requests, strict=True
            )
            if isinstance(request, SendRichMessage)
        ]
        assert len(times) == 4
        gaps = [
            later - earlier
            for earlier, later in zip(times, times[1:], strict=False)
        ]
        assert min(gaps) >= 0.045

    async def test_global_cap_spaces_different_admins(
        self, make_notify, db, session
    ):
        users = [ADMIN_ID, OWNER_ID, OTHER_ADMIN, 103, 104]
        session.set_admins(CHAT_ID, *users)
        await subscribe(db, *users)
        notify = make_notify(global_per_second=20)
        notify.submit(await log_action(db))
        await notify.join()
        times = [
            stamp
            for stamp, request in zip(
                session.times, session.requests, strict=True
            )
            if isinstance(request, SendRichMessage)
        ]
        assert len(times) == 5
        assert times[-1] - times[0] >= 0.19

    @pytest.fixture(autouse=True)
    def short_retry_waits(self, monkeypatch):
        pause = RateLimiter.pause

        def shortened(limiter, seconds):
            pause(limiter, seconds / 10)

        monkeypatch.setattr(RateLimiter, "pause", shortened)

    async def test_retry_after_is_honoured_and_message_delivered(
        self, make_notify, db, session
    ):
        await subscribe(db, ADMIN_ID)
        attempts = []

        def flaky(method):
            attempts.append(time.monotonic())
            if len(attempts) == 1:
                return TelegramRetryAfter(method, "flood", retry_after=1)
            return None

        session.on(SendRichMessage, flaky)
        await publish(make_notify(), db)
        assert len(attempts) == 2
        assert attempts[1] - attempts[0] >= 0.09

    async def test_retry_after_pauses_other_admins_globally(
        self, make_notify, db, session
    ):
        await subscribe(db, ADMIN_ID, OWNER_ID)
        calls = []

        def flaky(method):
            calls.append((method.chat_id, time.monotonic()))
            if len(calls) == 1:
                return TelegramRetryAfter(method, "flood", retry_after=1)
            return None

        session.on(SendRichMessage, flaky)
        await publish(make_notify(), db)
        assert len(calls) == 3
        assert calls[-1][1] - calls[0][1] >= 0.09

    async def test_group_flood_limit_waits_without_pausing_dms(
        self, make_notify, db, session
    ):
        calls = []

        def flaky(method):
            calls.append(time.monotonic())
            if len(calls) == 1:
                return TelegramRetryAfter(method, "flood", retry_after=1)
            return None

        session.on(SendRichMessage, flaky)
        notify = make_notify(fallback_chat_id=-100999, group_interval=0.001)
        notify.submit(await log_action(db))
        while not calls:
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)
        started = time.monotonic()
        await notify.global_limiter.acquire()
        assert time.monotonic() - started < 0.5
        await notify.join()
        assert len(calls) == 2
        assert calls[1] - calls[0] >= 0.09

    async def test_exhausted_retries_drop_only_that_job(
        self, make_notify, db, session
    ):
        await subscribe(db, ADMIN_ID)
        session.on(
            SendRichMessage,
            lambda m: TelegramRetryAfter(m, "flood", retry_after=0),
        )
        await publish(make_notify(), db)
        assert len(session.calls(SendRichMessage)) == 5


def is_digest(call) -> bool:
    button = call.reply_markup.inline_keyboard[0][0]
    return (button.callback_data or "").startswith("dgl:")


def digest_sends(session):
    return [call for call in session.calls(SendRichMessage) if is_digest(call)]


def card_sends(session):
    return [
        call for call in session.calls(SendRichMessage) if not is_digest(call)
    ]


def counts(call) -> dict[str, str]:
    table = tables(call.rich_message)[0]
    header, values = table.cells
    return {
        name.text.text: value.text
        for name, value in zip(header, values, strict=True)
    }


class TestDigest:
    async def set_threshold(self, db, rules, value=2):
        await db.set_setting(CHAT_ID, "notifications.digest_threshold", value)
        rules.invalidate(CHAT_ID)

    async def test_switches_to_single_digest_during_burst(
        self, make_notify, db, rules, session
    ):
        await self.set_threshold(db, rules)
        await subscribe(db, ADMIN_ID)
        await publish(make_notify(), db, count=6)
        assert len(card_sends(session)) == 2
        assert len(digest_sends(session)) == 1
        edits = session.calls(EditMessageText)
        assert len(edits) == 3
        assert counts(edits[-1])["Total"] == "4"
        stored = await db.get_digest(CHAT_ID, ADMIN_ID)
        assert stored.total == 4

    async def test_digest_has_full_list_button(
        self, make_notify, db, rules, session
    ):
        await self.set_threshold(db, rules, 0)
        await subscribe(db, ADMIN_ID)
        await publish(make_notify(), db)
        button = digest_sends(session)[0].reply_markup.inline_keyboard[0][0]
        assert button.callback_data.startswith("dgl:")

    async def test_counts_by_action_type(self, make_notify, db, rules, session):
        await self.set_threshold(db, rules, 0)
        await subscribe(db, ADMIN_ID)
        notify = make_notify()
        await publish(notify, db, action=ACTION_DELETE)
        await publish(notify, db, action=ACTION_RESTRICT)
        await publish(notify, db, action=ACTION_BAN)
        totals = counts(session.calls(EditMessageText)[-1])
        assert totals == {
            "Total": "3",
            "Deleted": "1",
            "Restricted": "1",
            "Banned": "1",
        }

    async def test_raid_mode_forces_digest(self, make_notify, db, session):
        await subscribe(db, ADMIN_ID)
        await db.set_raid(CHAT_ID, NOW + 600)
        await publish(make_notify(), db)
        [digest] = digest_sends(session)
        assert "Raid mode active" in card_text(digest)

    async def test_stays_in_digest_until_idle_after_burst(
        self, make_notify, db, rules, session
    ):
        await self.set_threshold(db, rules, 0)
        await subscribe(db, ADMIN_ID)
        notify = make_notify()
        await publish(notify, db)
        await self.set_threshold(db, rules, 1000)
        await publish(notify, db)
        assert len(digest_sends(session)) == 1
        assert len(session.calls(EditMessageText)) == 1

    async def test_expired_digest_starts_new_message(
        self, make_notify, db, rules, session
    ):
        await self.set_threshold(db, rules, 0)
        await subscribe(db, ADMIN_ID)
        notify = make_notify()
        await publish(notify, db)
        async with db.session() as transaction:
            await transaction.execute(update(DigestRecord).values(expires_at=1))
        await publish(notify, db)
        assert len(digest_sends(session)) == 2
        assert (await db.get_digest(CHAT_ID, ADMIN_ID)).total == 1

    async def test_edits_coalesce_while_rate_limited(
        self, make_notify, db, rules, session
    ):
        await self.set_threshold(db, rules, 0)
        await subscribe(db, ADMIN_ID)
        notify = make_notify(digest_edit_interval=0.5)
        await publish(notify, db)
        for _ in range(10):
            notify.submit(await log_action(db))
        await notify.join()
        edits = session.calls(EditMessageText)
        assert len(edits) <= 2
        assert counts(edits[-1])["Total"] == "11"

    async def test_lost_digest_message_is_resent(
        self, make_notify, db, rules, session
    ):
        await self.set_threshold(db, rules, 0)
        await subscribe(db, ADMIN_ID)
        notify = make_notify()
        await publish(notify, db)
        session.on(
            EditMessageText,
            lambda m: TelegramBadRequest(m, "message to edit not found"),
        )
        await publish(notify, db)
        assert len(digest_sends(session)) == 2

    async def test_unmodified_digest_is_not_resent(
        self, make_notify, db, rules, session
    ):
        await self.set_threshold(db, rules, 0)
        await subscribe(db, ADMIN_ID)
        notify = make_notify()
        await publish(notify, db)
        session.on(
            EditMessageText,
            lambda m: TelegramBadRequest(m, "message is not modified"),
        )
        await publish(notify, db)
        assert len(digest_sends(session)) == 1


class TestSubscriptions:
    async def test_non_admin_subscription_is_dropped(
        self, make_notify, db, session
    ):
        await subscribe(db, ADMIN_ID, 999)
        await publish(make_notify(), db)
        assert [call.chat_id for call in session.calls(SendRichMessage)] == [
            ADMIN_ID
        ]
        assert await db.active_subscribers(CHAT_ID) == [ADMIN_ID]
        assert await db.subscription_state(CHAT_ID, 999) is None

    async def test_demoted_while_queued_is_verified_again_before_send(
        self, make_notify, db, session, admins
    ):
        await subscribe(db, ADMIN_ID)
        notify = make_notify()
        await notify._publish(await log_action(db))
        session.set_admins(CHAT_ID, OWNER_ID)
        admins.invalidate(CHAT_ID)
        await notify.join()
        assert session.calls(SendRichMessage) == []
        assert await db.active_subscribers(CHAT_ID) == []

    async def test_forbidden_marks_subscription_inactive(
        self, make_notify, db, session
    ):
        await subscribe(db, ADMIN_ID)
        session.on(
            SendRichMessage,
            lambda m: TelegramForbiddenError(m, "bot was blocked by the user"),
        )
        await publish(make_notify(), db)
        assert await db.subscription_state(CHAT_ID, ADMIN_ID) == (
            False,
            "blocked",
        )
        assert await db.active_subscribers(CHAT_ID) == []

    async def test_forbidden_on_digest_marks_inactive(
        self, make_notify, db, session
    ):
        await subscribe(db, ADMIN_ID)
        await db.set_raid(CHAT_ID, NOW + 600)
        session.on(
            SendRichMessage,
            lambda m: TelegramForbiddenError(m, "bot was blocked by the user"),
        )
        await publish(make_notify(), db)
        assert await db.subscription_state(CHAT_ID, ADMIN_ID) == (
            False,
            "blocked",
        )

    async def test_admin_lookup_outage_keeps_subscription_and_sends_nothing(
        self, make_notify, db, session
    ):
        await subscribe(db, ADMIN_ID)
        session.on(
            GetChatAdministrators, lambda m: TelegramNetworkError(m, "down")
        )
        await publish(make_notify(), db)
        assert session.calls(SendRichMessage) == []
        assert await db.active_subscribers(CHAT_ID) == [ADMIN_ID]
        assert len(session.calls(GetChatAdministrators)) >= 3

    async def test_digest_retry_gives_up_during_outage(
        self, make_notify, db, session
    ):
        await subscribe(db, ADMIN_ID)
        await db.set_raid(CHAT_ID, NOW + 600)
        session.on(
            GetChatAdministrators, lambda m: TelegramNetworkError(m, "down")
        )
        await publish(make_notify(), db)
        assert session.calls(SendRichMessage) == []
        assert await db.active_subscribers(CHAT_ID) == [ADMIN_ID]


class TestPreferences:
    async def test_bans_only_skips_other_actions(
        self, make_notify, db, session
    ):
        await subscribe(db, ADMIN_ID)
        await db.set_pref(ADMIN_ID, PREF_BANS_ONLY)
        notify = make_notify()
        await publish(notify, db, action=ACTION_DELETE)
        assert session.calls(SendRichMessage) == []
        await publish(notify, db, action=ACTION_BAN)
        assert len(session.calls(SendRichMessage)) == 1

    async def test_digest_only_never_sends_cards(
        self, make_notify, db, session
    ):
        await subscribe(db, ADMIN_ID)
        await db.set_pref(ADMIN_ID, PREF_DIGEST_ONLY)
        await publish(make_notify(), db, count=3)
        assert card_sends(session) == []
        assert len(digest_sends(session)) == 1
        assert len(session.calls(EditMessageText)) == 2

    async def test_preferences_are_per_admin(self, make_notify, db, session):
        await subscribe(db, ADMIN_ID, OWNER_ID)
        await db.set_pref(ADMIN_ID, PREF_DIGEST_ONLY)
        await publish(make_notify(), db)
        assert [call.chat_id for call in digest_sends(session)] == [ADMIN_ID]
        assert [call.chat_id for call in card_sends(session)] == [OWNER_ID]


class TestFallback:
    FALLBACK = -100999

    async def test_uses_fallback_without_subscribers(
        self, make_notify, db, session
    ):
        await publish(make_notify(fallback_chat_id=self.FALLBACK), db)
        assert [call.chat_id for call in session.calls(SendRichMessage)] == [
            self.FALLBACK
        ]

    async def test_uses_fallback_when_all_subscribers_dropped(
        self, make_notify, db, session
    ):
        await subscribe(db, 999)
        await publish(make_notify(fallback_chat_id=self.FALLBACK), db)
        assert [call.chat_id for call in session.calls(SendRichMessage)] == [
            self.FALLBACK
        ]

    async def test_log_only_without_fallback(self, make_notify, db, session):
        await publish(make_notify(), db)
        assert session.calls(SendRichMessage) == []

    async def test_subscribers_take_priority_over_fallback(
        self, make_notify, db, session
    ):
        await subscribe(db, ADMIN_ID)
        await publish(make_notify(fallback_chat_id=self.FALLBACK), db)
        assert [call.chat_id for call in session.calls(SendRichMessage)] == [
            ADMIN_ID
        ]

    async def test_fallback_is_not_mistaken_for_blocked_user(
        self, make_notify, db, session
    ):
        session.on(
            SendRichMessage, lambda m: TelegramForbiddenError(m, "kicked")
        )
        await publish(make_notify(fallback_chat_id=self.FALLBACK), db)
        assert await db.subscription_state(CHAT_ID, self.FALLBACK) is None


FALLBACK_ID = -123


UPGRADED_ID = -100123


class TestFallbackFollowsUpgrade:
    @pytest.fixture
    def make_notify(self, bot, db, rules, admins):
        def factory():
            return NotifyService(
                bot,
                db,
                rules,
                admins,
                FALLBACK_ID,
                per_admin_interval=0.001,
                global_per_second=10000,
                digest_edit_interval=0.0,
                group_interval=0.001,
            )

        return factory

    async def action(self, db):
        await db.upsert_chat(CHAT_ID, "G", "supergroup", NOW)
        return await log_action(
            db,
            user_name="S",
            score=1,
            reasons=[],
            message_text="",
            action="delete",
            deleted_count=0,
            now=int(time.time()),
        )

    async def test_every_queued_card_reaches_the_new_chat(
        self, make_notify, db, session
    ):
        session.on(
            SendRichMessage,
            lambda m: (
                TelegramMigrateToChat(m, "up", migrate_to_chat_id=UPGRADED_ID)
                if m.chat_id == FALLBACK_ID
                else None
            ),
        )
        notify = make_notify()
        for _ in range(3):
            notify.submit(await self.action(db))
        await notify.join()
        delivered = [
            call.chat_id
            for call in session.calls(SendRichMessage)
            if call.chat_id == UPGRADED_ID
        ]
        assert len(delivered) == 3

    async def test_open_digest_continues_in_the_new_chat(
        self, make_notify, db, rules, session
    ):
        await db.set_setting(CHAT_ID, "notifications.digest_threshold", 0)
        rules.invalidate(CHAT_ID)
        notify = make_notify()
        notify.submit(await self.action(db))
        await notify.join()
        assert (await db.get_digest(CHAT_ID, FALLBACK_ID)).total == 1
        notify.set_fallback(UPGRADED_ID)
        await notify.join()
        assert await db.get_digest(CHAT_ID, FALLBACK_ID) is None
        session.clear()
        notify.submit(await self.action(db))
        await notify.join()
        moved = await db.get_digest(CHAT_ID, UPGRADED_ID)
        assert moved.total == 2
        call = session.calls(SendRichMessage)[0]
        assert call.chat_id == UPGRADED_ID
        assert "activity digest" in card_text(call)
