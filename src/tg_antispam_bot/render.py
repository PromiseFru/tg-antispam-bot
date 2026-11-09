from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from html import escape

from aiogram.filters.callback_data import CallbackData
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputRichBlockDetails,
    InputRichBlockList,
    InputRichBlockListItem,
    InputRichBlockParagraph,
    InputRichBlockTable,
    InputRichBlockUnion,
    InputRichMessage,
    LinkPreviewOptions,
    RichBlockTableCell,
    RichMessageButton,
    RichTextBold,
    RichTextButton,
    RichTextCode,
    RichTextDateTime,
    RichTextTextMention,
    RichTextUnion,
    User,
)

from .config import LOG_MAX, ChatRules
from .db import (
    ACTION_BAN,
    ACTION_DELETE,
    ACTION_RAID_RESTRICT,
    ACTION_RESTRICT,
    PREF_ALL,
    PREF_BANS_ONLY,
    PREF_DIGEST_ONLY,
    ActionRecord,
    DigestRecord,
)
from .models import RESOLUTION_CONFIRMED

VERB_UNBAN = "unban"
VERB_TRUST = "trust"
VERB_FALSE_POSITIVE = "fp"
VERB_CONFIRM = RESOLUTION_CONFIRMED
VERB_LABELS = {
    VERB_UNBAN: "Unban",
    VERB_TRUST: "Trust",
    VERB_FALSE_POSITIVE: "False positive",
    VERB_CONFIRM: "Confirm",
}

RESOLVED_LABEL = "Resolved"
CONFIRM_LABEL = "✓ Confirm, keep as is"
BOT_ACTOR = "Anti-spam"
RESOLVED_LABELS = {
    VERB_TRUST: "Trusted",
    VERB_CONFIRM: "Confirmed",
    VERB_FALSE_POSITIVE: "False positive",
}
LIFTS = {
    ACTION_BAN: ("Unban", "Unbanned"),
    ACTION_RESTRICT: ("Unrestrict", "Restriction lifted"),
    ACTION_RAID_RESTRICT: ("Unrestrict", "Restriction lifted"),
}

DOT_RED = "🔴"
DOT_YELLOW = "🟡"
DOT_GREEN = "🟢"
DOT_GRAY = "⚪"

ACTION_NAMES = {
    ACTION_DELETE: "Message deleted",
    ACTION_RESTRICT: "Restricted",
    ACTION_BAN: "Banned",
    ACTION_RAID_RESTRICT: "Restricted by raid mode",
}
ACTION_DOTS = {
    ACTION_DELETE: DOT_GRAY,
    ACTION_RESTRICT: DOT_YELLOW,
    ACTION_BAN: DOT_RED,
    ACTION_RAID_RESTRICT: DOT_YELLOW,
}

STYLE_SUCCESS = "success"
STYLE_PRIMARY = "primary"
STYLE_DANGER = "danger"
VERB_STYLES = {
    VERB_UNBAN: STYLE_SUCCESS,
    VERB_FALSE_POSITIVE: STYLE_PRIMARY,
}

SURFACE_DM = "d"
SURFACE_EPHEMERAL = "e"

SUBSCRIBE_PAYLOAD_PREFIX = "sub_"
LIST_PAGE_SIZE = 8
NAME_LIMIT = 64
LOG_NAME_LIMIT = 30
STATUS_ADMIN_LIMIT = 15
LOCAL_TIME_FORMAT = "dt"
MESSAGE_LIMIT = 4096
TRUNCATION_MARK = "…"
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)

PREF_LABELS = {
    PREF_ALL: "All actions",
    PREF_BANS_ONLY: "Bans only",
    PREF_DIGEST_ONLY: "Digest only",
}


class ActionCb(CallbackData, prefix="act"):
    action_id: int
    verb: str
    surface: str


class DismissCb(CallbackData, prefix="dis"):
    action_id: int


class SubCb(CallbackData, prefix="sub"):
    chat_id: int
    on: int


class PrefCb(CallbackData, prefix="pref"):
    mode: str


class DigestListCb(CallbackData, prefix="dgl"):
    chat_id: int
    page: int
    new: int


class LogPageCb(CallbackData, prefix="lgp"):
    chat_id: int
    page: int
    size: int


class LogViewCb(CallbackData, prefix="lgv"):
    chat_id: int
    action_id: int
    page: int
    size: int


class CardCb(CallbackData, prefix="card"):
    action_id: int


def utc_text(timestamp: int) -> str:
    moment = dt.datetime.fromtimestamp(timestamp, tz=dt.UTC)
    return moment.strftime("%Y-%m-%d %H:%M UTC")


def format_time(timestamp: int) -> str:
    return (
        f'<tg-time unix="{timestamp}" format="{LOCAL_TIME_FORMAT}">'
        f"{utc_text(timestamp)}</tg-time>"
    )


def clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + TRUNCATION_MARK


def fit_blocks(blocks: Sequence[str]) -> str:
    kept = list(blocks)
    text = "\n".join(kept)
    while len(text) > MESSAGE_LIMIT and kept:
        kept.pop()
        text = "\n".join([*kept, TRUNCATION_MARK])
    return text


def user_label(action: ActionRecord) -> str:
    name = escape(clip(action.user_name or str(action.user_id), NAME_LIMIT))
    if action.user_id > 0:
        return f'<a href="tg://user?id={action.user_id}">{name}</a>'
    return f"<code>{name}</code>"


def resolved_label(action: ActionRecord) -> str:
    if action.resolution == VERB_UNBAN:
        lift = LIFTS.get(action.action)
        return lift[1] if lift else RESOLVED_LABEL
    return RESOLVED_LABELS.get(action.resolution or "", RESOLVED_LABEL)


def action_dot(action: ActionRecord) -> str:
    if action.is_overturned:
        return DOT_GREEN
    return ACTION_DOTS.get(action.action, DOT_GRAY)


def action_title(action: ActionRecord) -> str:
    name = ACTION_NAMES.get(action.action, action.action)
    if action.is_resolved and not action.is_overturned:
        return f"{name} · Confirmed"
    if action.is_resolved:
        return resolved_label(action)
    return name


def local_time(timestamp: int) -> RichTextDateTime:
    return RichTextDateTime(
        text=utc_text(timestamp),
        unix_time=timestamp,
        date_time_format=LOCAL_TIME_FORMAT,
    )


def mention(user_id: int, name: str, limit: int = NAME_LIMIT) -> RichTextUnion:
    name = clip(name or str(user_id), limit)
    if user_id < 0:
        return name
    user = User(id=user_id, is_bot=False, first_name=name)
    return RichTextTextMention(text=name, user=user)


def rich_user(action: ActionRecord, limit: int = NAME_LIMIT) -> RichTextUnion:
    return mention(action.user_id, action.user_name, limit)


def card_row(label: str, value: RichTextUnion) -> list[RichBlockTableCell]:
    return cells(RichTextBold(text=label), value)


def card_rows(
    action: ActionRecord, chat_title: str
) -> list[list[RichBlockTableCell]]:
    rows = [
        card_row("Group", clip(chat_title, NAME_LIMIT)),
        card_row("User", rich_user(action)),
        card_row("ID", RichTextCode(text=str(action.user_id))),
    ]
    if action.score:
        rows.append(card_row("Score", points(action.score)))
    if action.deleted_count:
        unit = "message" if action.deleted_count == 1 else "messages"
        rows.append(card_row("Deleted", f"{action.deleted_count} {unit}"))
    if action.until_ts and not action.is_overturned:
        rows.append(card_row("Until", local_time(action.until_ts)))
    return rows


def history_table(action: ActionRecord) -> InputRichBlockTable:
    taken = ACTION_NAMES.get(action.action, action.action)
    dot = ACTION_DOTS.get(action.action, DOT_GRAY)
    rows = [
        header_row("What happened", "By", "When"),
        cells(f"{dot} {taken}", BOT_ACTOR, local_time(action.created_at)),
    ]
    if action.is_resolved:
        who = action.resolved_by_name or str(action.resolved_by)
        rows.append(
            cells(
                f"{DOT_GREEN} {resolved_label(action)}",
                clip(who, NAME_LIMIT),
                local_time(action.resolved_at or 0),
            )
        )
    return grid(rows)


def reason_list(reasons: Sequence[str]) -> InputRichBlockList:
    return InputRichBlockList(
        items=[
            InputRichBlockListItem(
                blocks=[InputRichBlockParagraph(text=clip(reason, 240))]
            )
            for reason in reasons
        ]
    )


def rich_card(
    action: ActionRecord, chat_title: str, preview_chars: int
) -> InputRichMessage:
    title = f"{action_dot(action)} {action_title(action)} · #{action.id}"
    blocks: list[InputRichBlockUnion] = [
        heading(title),
        grid(card_rows(action, chat_title)),
        history_table(action),
    ]
    blocks.extend(
        InputRichBlockParagraph(text=f"⚠️ {clip(error, 200)}")
        for error in action.errors
    )
    if action.reasons:
        blocks.append(
            InputRichBlockDetails(
                summary="Why flagged",
                blocks=[reason_list(action.reasons)],
                is_open=not action.is_resolved,
            )
        )
    if action.message_text.strip():
        preview = clip(action.message_text, preview_chars)
        blocks.append(
            InputRichBlockDetails(
                summary="Message",
                blocks=[InputRichBlockParagraph(text=preview)],
            )
        )
    return InputRichMessage(blocks=blocks, skip_entity_detection=True)


def action_verbs(action: ActionRecord) -> list[tuple[str, str]]:
    verbs = [
        (verb, VERB_LABELS[verb]) for verb in (VERB_TRUST, VERB_FALSE_POSITIVE)
    ]
    lift = LIFTS.get(action.action)
    return [(VERB_UNBAN, lift[0]), *verbs] if lift else verbs


def verb_rows(
    action: ActionRecord, surface: str
) -> list[list[InlineKeyboardButton]]:
    def button(verb: str, label: str) -> InlineKeyboardButton:
        return InlineKeyboardButton(
            text=label,
            style=VERB_STYLES.get(verb),
            callback_data=ActionCb(
                action_id=action.id, verb=verb, surface=surface
            ).pack(),
        )

    corrections = [button(verb, label) for verb, label in action_verbs(action)]
    return [corrections, [button(VERB_CONFIRM, CONFIRM_LABEL)]]


def action_keyboard(action: ActionRecord) -> InlineKeyboardMarkup:
    if action.is_resolved:
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="Dismiss",
                        style=STYLE_DANGER,
                        callback_data=DismissCb(action_id=action.id).pack(),
                    )
                ]
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=verb_rows(action, SURFACE_DM))


def points(value: int) -> str:
    return f"{value} pts"


def header_row(*labels: str) -> list[RichBlockTableCell]:
    return [
        RichBlockTableCell(
            align="left",
            valign="middle",
            text=RichTextBold(text=label),
            is_header=True,
        )
        for label in labels
    ]


def cells(*values: RichTextUnion) -> list[RichBlockTableCell]:
    return [
        RichBlockTableCell(align="left", valign="middle", text=value)
        for value in values
    ]


def grid(rows: list[list[RichBlockTableCell]]) -> InputRichBlockTable:
    return InputRichBlockTable(cells=rows, is_bordered=True, is_compact=True)


def heading(text: str) -> InputRichBlockParagraph:
    return InputRichBlockParagraph(text=RichTextBold(text=text))


def actions_table(
    actions: Sequence[ActionRecord],
    link: Callable[[ActionRecord], str] | None = None,
) -> InputRichBlockTable:
    rows = [header_row("#", "User", "Action", "Score")]
    for action in actions:
        label = f"{action_dot(action)} {action.id}"
        number: RichTextUnion = label
        if link is not None:
            button = RichMessageButton(
                text=f"{label} ›", callback_data=link(action)
            )
            number = RichTextButton(button=button)
        rows.append(
            cells(
                number,
                rich_user(action, LOG_NAME_LIMIT),
                action_title(action),
                points(action.score) if action.score else "-",
            )
        )
    return grid(rows)


def page_count(total: int, size: int) -> int:
    return max(1, (total + size - 1) // size)


def actions_page(
    title: str,
    actions: Sequence[ActionRecord],
    page: int,
    total: int,
    size: int,
    link: Callable[[ActionRecord], str] | None = None,
) -> InputRichMessage:
    blocks: list[InputRichBlockUnion] = [heading(title)]
    if not actions:
        blocks.append(InputRichBlockParagraph(text="No actions recorded."))
        return InputRichMessage(blocks=blocks, skip_entity_detection=True)
    blocks.append(actions_table(actions, link))
    blocks.append(
        InputRichBlockParagraph(
            text=f"Page {page + 1} of {page_count(total, size)} · "
            f"{total} actions"
        )
    )
    return InputRichMessage(blocks=blocks, skip_entity_detection=True)


def navigation_row(
    page: int, total: int, size: int, make: Callable[[int], str]
) -> list[InlineKeyboardButton]:
    row = []
    if page > 0:
        row.append(
            InlineKeyboardButton(text="‹ Newer", callback_data=make(page - 1))
        )
    if (page + 1) * size < total:
        row.append(
            InlineKeyboardButton(text="Older ›", callback_data=make(page + 1))
        )
    return row


def digest_card(
    digest: DigestRecord,
    chat_title: str,
    latest: Sequence[ActionRecord],
    raid_until: int,
    now: int,
) -> InputRichMessage:
    counts = grid(
        [
            header_row("Total", "Deleted", "Restricted", "Banned"),
            cells(
                str(digest.total),
                str(digest.deleted),
                str(digest.restricted),
                str(digest.banned),
            ),
        ]
    )
    blocks: list[InputRichBlockUnion] = [
        heading(f"{clip(chat_title, NAME_LIMIT)} · activity digest"),
        InputRichBlockParagraph(text=["Since ", local_time(digest.started_at)]),
        counts,
    ]
    if raid_until > now:
        blocks.append(
            InputRichBlockParagraph(
                text=[
                    f"{DOT_RED} Raid mode active until ",
                    local_time(raid_until),
                ]
            )
        )
    if latest:
        blocks.append(heading("Latest"))
        blocks.append(actions_table(latest))
    return InputRichMessage(blocks=blocks, skip_entity_detection=True)


def digest_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="View full list",
                    callback_data=DigestListCb(
                        chat_id=chat_id, page=0, new=1
                    ).pack(),
                )
            ]
        ]
    )


def list_page_card(
    chat_title: str, actions: Sequence[ActionRecord], page: int, total: int
) -> InputRichMessage:
    title = f"{clip(chat_title, NAME_LIMIT)} · all actions"
    return actions_page(title, actions, page, total, LIST_PAGE_SIZE)


def list_page_keyboard(
    chat_id: int, actions: Sequence[ActionRecord], page: int, total: int
) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=(
                    f"Review #{action.id}: "
                    f"{clip(action.user_name, LOG_NAME_LIMIT)}"
                ),
                callback_data=CardCb(action_id=action.id).pack(),
            )
        ]
        for action in actions
        if not action.is_resolved
    ]
    navigation = navigation_row(
        page,
        total,
        LIST_PAGE_SIZE,
        lambda target: DigestListCb(chat_id=chat_id, page=target, new=0).pack(),
    )
    if navigation:
        rows.append(navigation)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def log_card(
    chat_title: str,
    actions: Sequence[ActionRecord],
    page: int,
    total: int,
    size: int,
) -> InputRichMessage:
    title = f"{clip(chat_title, NAME_LIMIT)} · recent actions"

    def link(action: ActionRecord) -> str:
        return LogViewCb(
            chat_id=action.chat_id, action_id=action.id, page=page, size=size
        ).pack()

    return actions_page(title, actions, page, total, size, link)


def log_keyboard(
    chat_id: int, page: int, total: int, size: int
) -> InlineKeyboardMarkup | None:
    navigation = navigation_row(
        page,
        total,
        size,
        lambda target: LogPageCb(
            chat_id=chat_id, page=target, size=size
        ).pack(),
    )
    return (
        InlineKeyboardMarkup(inline_keyboard=[navigation])
        if navigation
        else None
    )


def log_size(requested: int) -> int:
    return max(1, min(requested, LOG_MAX))


def log_detail_keyboard(
    action: ActionRecord, page: int, size: int
) -> InlineKeyboardMarkup:
    rows = []
    if not action.is_resolved:
        rows.extend(verb_rows(action, SURFACE_EPHEMERAL))
    back = LogPageCb(chat_id=action.chat_id, page=page, size=size).pack()
    rows.append(
        [InlineKeyboardButton(text="‹ Back to log", callback_data=back)]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


@dataclass(frozen=True)
class AdminRow:
    user_id: int
    name: str
    state: str


def on_off(enabled: bool) -> str:
    return f"{DOT_GREEN} On" if enabled else f"{DOT_GRAY} Off"


def admin_table(admins: Sequence[AdminRow]) -> InputRichBlockTable:
    rows = [header_row("Admin", "Alerts")]
    rows.extend(
        cells(mention(admin.user_id, admin.name), admin.state)
        for admin in admins
    )
    return grid(rows)


@dataclass(frozen=True)
class DetectorRow:
    name: str
    weight: int
    description: str


def status_card(
    chat_title: str,
    rules: ChatRules,
    rights: str,
    raid_until: int,
    now: int,
    detectors: Sequence[DetectorRow],
    skipped: Sequence[str],
    admins: Sequence[AdminRow] | None,
) -> InputRichMessage:
    tiers = rules.tiers
    raid: RichTextUnion = on_off(False)
    if raid_until > now:
        raid = [f"{DOT_RED} Until ", local_time(raid_until)]
    alerts = rules.notifications.live_ephemeral_alerts
    settings = grid(
        [
            card_row("Anti-spam", on_off(rules.enabled)),
            card_row("Bot rights", rights),
            card_row("Raid mode", raid),
            card_row("Live alerts", on_off(alerts)),
        ]
    )
    thresholds = grid(
        [
            header_row("Action", "From score"),
            cells("Delete message", points(tiers.delete)),
            cells("Restrict sender", points(tiers.delete_and_restrict)),
            cells("Ban sender", points(tiers.delete_and_ban)),
        ]
    )
    blocks: list[InputRichBlockUnion] = [
        heading(f"{clip(chat_title, NAME_LIMIT)} · status"),
        settings,
        heading("Thresholds"),
        thresholds,
    ]
    if skipped:
        ignored = ", ".join(skipped)
        blocks.append(
            InputRichBlockParagraph(
                text=f"⚠️ Ignored settings, invalid with the current rules: "
                f"{ignored}"
            )
        )
    blocks.append(heading("Admins"))
    if admins is None:
        blocks.append(InputRichBlockParagraph(text="Admin list unavailable."))
    else:
        blocks.append(admin_table(admins[:STATUS_ADMIN_LIMIT]))
        hidden = len(admins) - STATUS_ADMIN_LIMIT
        if hidden > 0:
            blocks.append(
                InputRichBlockParagraph(text=f"And {hidden} more admins.")
            )
    detector_rows = [header_row("Detector", "Adds", "What it checks")]
    detector_rows.extend(
        cells(
            RichTextCode(text=row.name),
            f"+{points(row.weight)}",
            row.description,
        )
        for row in detectors
    )
    blocks.append(
        InputRichBlockDetails(
            summary=f"Detectors ({len(detectors)} on)",
            blocks=[grid(detector_rows)],
        )
    )
    return InputRichMessage(blocks=blocks, skip_entity_detection=True)


def alert_text(action: ActionRecord, chat_title: str) -> str:
    title = escape(clip(chat_title, NAME_LIMIT))
    score = f", {points(action.score)}" if action.score else ""
    return (
        f"{action_dot(action)} {title}: {user_label(action)}: "
        f"{escape(action_title(action))}{score}"
    )


NO_GROUPS_TEXT = (
    "You are not an admin of any group where this bot is present.\n"
    "Add the bot to a group as an admin, then send /groups here."
)


def group_row(
    chat_id: int, title: str, subscribed: bool
) -> list[RichBlockTableCell]:
    dot = DOT_GREEN if subscribed else DOT_GRAY
    button = RichMessageButton(
        text="Turn off" if subscribed else "Turn on",
        style=STYLE_DANGER if subscribed else STYLE_SUCCESS,
        callback_data=SubCb(chat_id=chat_id, on=0 if subscribed else 1).pack(),
    )
    return cells(f"{dot} {clip(title, 50)}", RichTextButton(button=button))


def groups_card(
    rows: Sequence[tuple[int, str, bool]], intro: str = ""
) -> InputRichMessage:
    blocks: list[InputRichBlockUnion] = []
    if intro:
        blocks.append(InputRichBlockParagraph(text=intro))
    blocks.append(
        grid(
            [header_row("Group", "Alerts"), *(group_row(*row) for row in rows)]
        )
    )
    blocks.append(
        InputRichBlockParagraph(
            text="Use /prefs to choose which actions reach you."
        )
    )
    return InputRichMessage(blocks=blocks)


def prefs_text(mode: str) -> str:
    return (
        "Alert preference: "
        f"<b>{PREF_LABELS.get(mode, PREF_LABELS[PREF_ALL])}</b>\n\n"
        "All actions: one message per action, "
        "switching to a digest during bursts.\n"
        "Bans only: only ban actions reach you.\n"
        "Digest only: a single digest message per group that keeps updating."
    )


def prefs_keyboard(mode: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"{DOT_GREEN + ' ' if option == mode else ''}{label}",
                    callback_data=PrefCb(mode=option).pack(),
                )
            ]
            for option, label in PREF_LABELS.items()
        ]
    )
