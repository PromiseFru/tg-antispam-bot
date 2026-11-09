from __future__ import annotations

import pytest
from aiogram.enums import InputRichBlockType
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import DeleteMessages

from conftest import CHAT_ID, SPAMMER_ID, TEST_RULES, tables
from tg_antispam_bot.actions import ActionExecutor
from tg_antispam_bot.config import parse_chat_rules, parse_rules_document
from tg_antispam_bot.db import (
    ACTION_BAN,
    ACTION_DELETE,
    ACTION_RAID_RESTRICT,
    ACTION_RESTRICT,
    ActionRecord,
)
from tg_antispam_bot.detectors import DETECTOR_CLASSES, build_detectors
from tg_antispam_bot.render import (
    LOG_MAX,
    NAME_LIMIT,
    STATUS_ADMIN_LIMIT,
    VERB_UNBAN,
    AdminRow,
    DetectorRow,
    action_keyboard,
    digest_keyboard,
    groups_card,
    list_page_keyboard,
    log_card,
    log_detail_keyboard,
    log_keyboard,
    rich_card,
    status_card,
)

NOW = 1_800_000_000
LARGE_CHAT_ID = -1009999999999
LARGE_ACTION_ID = 2**53
CALLBACK_DATA_LIMIT = 64
RICH_BLOCK_LIMIT = 500
MAX_REASONS = len(DETECTOR_CLASSES) + 1


def record(action: str = ACTION_BAN, **fields) -> ActionRecord:
    defaults = {
        "id": 7,
        "chat_id": CHAT_ID,
        "user_id": SPAMMER_ID,
        "user_name": "Spam",
        "score": 150,
        "reasons": ["mention_count: 6 mentions (+40 pts)"],
        "message_text": "",
        "action": action,
        "until_ts": None,
        "deleted_count": 0,
        "errors": [],
        "created_at": NOW,
        "resolved_at": None,
        "resolved_by": None,
        "resolved_by_name": None,
        "resolution": None,
    }
    return ActionRecord(**{**defaults, **fields})


def resolved(action: str = ACTION_BAN) -> ActionRecord:
    return record(
        action,
        resolved_at=NOW,
        resolved_by=1,
        resolved_by_name="Admin",
        resolution=VERB_UNBAN,
    )


def verbs(markup) -> list[str]:
    return [
        button.callback_data.split(":")[2]
        for button in markup.inline_keyboard[0]
    ]


BLOCK_TYPES = {kind.value for kind in InputRichBlockType}


def count_blocks(value) -> int:
    if isinstance(value, list):
        return sum(count_blocks(item) for item in value)
    if not isinstance(value, dict):
        return 0
    own = 1 if value.get("type") in BLOCK_TYPES else 0
    own += len(value.get("items") or [])
    own += len(value.get("cells") or [])
    return own + sum(count_blocks(item) for item in value.values())


def groups_table(card):
    return tables(card)[0]


@pytest.mark.parametrize(
    ("action", "expected"),
    [
        (ACTION_BAN, ["unban", "trust", "fp"]),
        (ACTION_RESTRICT, ["unban", "trust", "fp"]),
        (ACTION_RAID_RESTRICT, ["unban", "trust", "fp"]),
        (ACTION_DELETE, ["trust", "fp"]),
    ],
)
def test_lift_is_offered_only_when_there_is_a_penalty(action, expected):
    assert verbs(action_keyboard(record(action))) == expected
    assert verbs(log_detail_keyboard(record(action), 0, 5)) == expected


def test_resolved_card_only_offers_dismiss():
    markup = action_keyboard(resolved())
    assert markup is not None
    [[button]] = markup.inline_keyboard
    assert button.callback_data == "dis:7"


def test_resolved_detail_only_goes_back():
    [[back]] = log_detail_keyboard(resolved(), 3, 5).inline_keyboard
    assert back.callback_data == f"lgp:{CHAT_ID}:3:5"


@pytest.mark.parametrize(
    ("page", "total", "expected"),
    [
        (0, 5, []),
        (0, 6, ["lgp:-1001000000001:1:5"]),
        (1, 11, ["lgp:-1001000000001:0:5", "lgp:-1001000000001:2:5"]),
        (2, 11, ["lgp:-1001000000001:1:5"]),
    ],
)
def test_log_pages_link_to_their_neighbours(page, total, expected):
    markup = log_keyboard(CHAT_ID, page, total, 5)
    rows = markup.inline_keyboard if markup else []
    found = [button.callback_data for row in rows for button in row]
    assert found == expected


def test_full_list_pages_link_to_their_neighbours():
    markup = list_page_keyboard(CHAT_ID, [], 1, 30)
    found = [button.callback_data for button in markup.inline_keyboard[-1]]
    assert found == ["dgl:-1001000000001:0:0", "dgl:-1001000000001:2:0"]


def test_callback_data_fits_telegram_limit_for_large_ids():
    action = record(id=LARGE_ACTION_ID, chat_id=LARGE_CHAT_ID)
    markups = [
        action_keyboard(action),
        action_keyboard(resolved()),
        log_keyboard(LARGE_CHAT_ID, 10**6, 10**8, LOG_MAX),
        log_detail_keyboard(action, 10**6, LOG_MAX),
        list_page_keyboard(LARGE_CHAT_ID, [action], 10**6, 10**8),
        digest_keyboard(LARGE_CHAT_ID),
    ]
    for markup in markups:
        assert markup is not None
        for row in markup.inline_keyboard:
            for button in row:
                assert len(button.callback_data.encode()) <= CALLBACK_DATA_LIMIT
    table = groups_table(groups_card([(LARGE_CHAT_ID, "G", True)]))
    button = table.cells[1][1].text.button
    assert len(button.callback_data.encode()) <= CALLBACK_DATA_LIMIT
    log = log_card("G", [action], 10**6, 10**8, LOG_MAX)
    link = groups_table(log).cells[1][0].text.button
    assert len(link.callback_data.encode()) <= CALLBACK_DATA_LIMIT


@pytest.mark.parametrize(("subscribed", "on"), [(True, 0), (False, 1)])
def test_groups_toggle_does_the_opposite_of_the_state(subscribed, on):
    table = groups_table(groups_card([(CHAT_ID, "G", subscribed)]))
    button = table.cells[1][1].text.button
    assert button.callback_data == f"sub:{CHAT_ID}:{on}"


def test_every_view_disables_automatic_links():
    cards = [
        rich_card(record(message_text="t.me/+abc @spam"), "G", 300),
        log_card("G", [record()], 0, 1, 5),
        rich_card(record(message_text="t.me/+abc"), "G", 300),
    ]
    assert all(card.skip_entity_detection for card in cards)


def test_user_supplied_text_is_clipped():
    action = record(
        user_name="x" * 500,
        message_text="y" * 5000,
        reasons=["z" * 5000],
    )
    card = rich_card(action, "t" * 500, 300)
    text = str(card.model_dump())
    assert "x" * (NAME_LIMIT + 1) not in text
    assert "y" * 301 not in text
    assert "z" * 241 not in text
    assert "t" * (NAME_LIMIT + 1) not in text


def test_largest_log_views_stay_within_rich_block_limit():
    reasons = [f"reason {n}" for n in range(MAX_REASONS)]
    actions = [record(id=i, reasons=reasons) for i in range(LOG_MAX)]
    page = log_card("G", actions, 0, 100, LOG_MAX)
    assert count_blocks(page.model_dump()) < RICH_BLOCK_LIMIT
    detail = rich_card(actions[0], "G", 300)
    assert count_blocks(detail.model_dump()) < RICH_BLOCK_LIMIT


def test_largest_status_stays_within_rich_block_limit():
    rules = parse_chat_rules(parse_rules_document(TEST_RULES).defaults)
    admins = [AdminRow(i, f"A{i}", "On") for i in range(1, 60)]
    detectors = [DetectorRow(f"d{i}", 10, "checks") for i in range(20)]
    card = status_card("G", rules, "ok", 0, NOW, detectors, [], admins)
    assert count_blocks(card.model_dump()) < RICH_BLOCK_LIMIT
    admin_tables = tables(card)
    assert any(
        len(table.cells) == STATUS_ADMIN_LIMIT + 1 for table in admin_tables
    )


class TestDeletedCount:
    async def test_delete_tier_message_not_deleted_again(self, db, bot):
        executor = ActionExecutor(bot, db)
        await db.buffer_message(CHAT_ID, SPAMMER_ID, 1, NOW)
        await db.buffer_message(CHAT_ID, SPAMMER_ID, 2, NOW)
        first = await executor.apply(
            CHAT_ID, SPAMMER_ID, "delete", 2, 60, False, NOW
        )
        assert first.deleted_count == 1
        assert await db.buffered_messages(CHAT_ID, SPAMMER_ID) == [1]
        second = await executor.apply(
            CHAT_ID, SPAMMER_ID, "delete_and_ban", 3, 60, False, NOW
        )
        assert second.deleted_count == 2

    async def test_failed_delete_stays_buffered_for_retry(
        self, db, bot, session
    ):
        session.on(
            DeleteMessages, lambda m: TelegramBadRequest(m, "cannot delete")
        )
        executor = ActionExecutor(bot, db)
        await db.buffer_message(CHAT_ID, SPAMMER_ID, 2, NOW)
        result = await executor.apply(
            CHAT_ID, SPAMMER_ID, "delete", 2, 60, False, NOW
        )
        assert result.errors
        assert await db.buffered_messages(CHAT_ID, SPAMMER_ID) == [2]


def test_rule_description_overrides_the_built_in_one():
    document = {
        "defaults": {
            "detectors": {"mention_count": {"description": "Custom text"}}
        }
    }
    rules = parse_chat_rules(parse_rules_document(document).defaults)
    detectors = {d.name: d for d in build_detectors(rules)}
    assert detectors["mention_count"].summary == "Custom text"
    assert detectors["new_member"].summary == "Sender joined recently"
