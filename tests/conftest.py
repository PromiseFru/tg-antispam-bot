from __future__ import annotations

import asyncio
import datetime as dt
import shutil
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.enums import ParseMode
from aiogram.methods import (
    GetChatAdministrators,
    GetMe,
    SendMessage,
    SendRichMessage,
    TelegramMethod,
)
from aiogram.types import (
    AcceptedGiftTypes,
    Chat,
    ChatFullInfo,
    ChatMemberAdministrator,
    ChatMemberLeft,
    ChatMemberMember,
    ChatMemberOwner,
    ChatMemberUpdated,
    Message,
    MessageEntity,
    Update,
    User,
)

from tg_antispam_bot.actions import ActionExecutor
from tg_antispam_bot.admin import AdminCache
from tg_antispam_bot.config import parse_rules_document
from tg_antispam_bot.db import ACTION_BAN, Database
from tg_antispam_bot.engine import Engine
from tg_antispam_bot.main import build_dispatcher
from tg_antispam_bot.rules_store import RulesStore

CHAT_ID = -1001000000001
BOT_ID = 4242
OWNER_ID = 100
ADMIN_ID = 101
SPAMMER_ID = 900
MEMBER_ID = 500
FAKE_TOKEN = "123456:TEST_TOKEN_FOR_UNIT_TESTS"
SHIPPED_RULES = (
    Path(__file__).resolve().parent.parent
    / "src"
    / "tg_antispam_bot"
    / "rules.yaml"
)
TEST_DB_KEY = "ab" * 32
OTHER_ALLOWED_CHAT_ID = -200
ALLOWED_CHATS = frozenset({CHAT_ID, OTHER_ALLOWED_CHAT_ID})


def utf16_len(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


def make_user(
    user_id: int = MEMBER_ID,
    first_name: str = "Alice",
    username: str | None = None,
) -> User:
    return User(
        id=user_id, is_bot=False, first_name=first_name, username=username
    )


def make_chat(
    chat_id: int = CHAT_ID,
    title: str = "Test Group",
    chat_type: str = "supergroup",
) -> Chat:
    return Chat(id=chat_id, type=chat_type, title=title)


def mention_entities(text: str, index: int = 0) -> list[MessageEntity]:
    entities: list[MessageEntity] = []
    while True:
        start = text.find("@", index)
        if start == -1:
            return entities
        end = start + 1
        while end < len(text) and (text[end].isalnum() or text[end] == "_"):
            end += 1
        entities.append(
            MessageEntity(
                type="mention",
                offset=utf16_len(text[:start]),
                length=utf16_len(text[start:end]),
            )
        )
        index = end


def auto_entities(text: str) -> list[MessageEntity]:
    if not text.startswith("/"):
        return mention_entities(text)
    command = text.split(maxsplit=1)[0]
    entity = MessageEntity(
        type="bot_command", offset=0, length=utf16_len(command)
    )
    return [entity, *mention_entities(text, len(command))]


def join_event(
    user_id: int, status_user: User | None = None, age_seconds: int = 0
) -> ChatMemberUpdated:
    user = status_user or make_user(user_id, f"U{user_id}")
    return ChatMemberUpdated(
        chat=make_chat(),
        from_user=user,
        date=dt.datetime.now(tz=dt.UTC) - dt.timedelta(seconds=age_seconds),
        old_chat_member=ChatMemberLeft(user=user),
        new_chat_member=ChatMemberMember(user=user),
    )


def make_message(
    text: str | None = "hello",
    user: User | None = None,
    chat: Chat | None = None,
    message_id: int = 1000,
    entities: list[MessageEntity] | None = None,
    auto_mentions: bool = True,
    **kwargs: Any,
) -> Message:
    if entities is None and auto_mentions and text:
        entities = auto_entities(text) or None
    return Message(
        message_id=message_id,
        date=kwargs.pop("date", dt.datetime.now(tz=dt.UTC)),
        chat=chat or make_chat(),
        from_user=user if user is not None else make_user(),
        text=text,
        entities=entities,
        **kwargs,
    )


DHM_SPAM_TEXT = (
    "🌿 quiet river tomorrow lantern 🚀 Best promo in town, "
    "purple seven window "
    + " ".join(
        f"@Dhm_{suffix}"
        for suffix in (
            "alpha",
            "bravo",
            "charlie",
            "delta",
            "echo",
            "foxtrot",
            "golf",
            "hotel",
        )
    )
    + " marble ocean ticket"
)
FRIENDS_TEXT = "Hey @maria_k and @tom_b, are you coming tonight?"


def full_chat(chat_id: int = CHAT_ID, **kwargs: Any) -> ChatFullInfo:
    return ChatFullInfo(
        id=chat_id,
        type="supergroup",
        accent_color_id=0,
        max_reaction_count=1,
        accepted_gift_types=AcceptedGiftTypes(
            unlimited_gifts=True,
            limited_gifts=True,
            unique_gifts=True,
            premium_subscription=True,
            gifts_from_channels=True,
        ),
        **kwargs,
    )


class FakeSession(BaseSession):
    def __init__(self) -> None:
        super().__init__()
        self.requests: list[TelegramMethod] = []
        self.times: list[float] = []
        self.handlers: dict[type, Callable[[TelegramMethod], Any]] = {}
        self.admins: dict[int | str, list[Any]] = {}
        self._next_message_id = 5000

    def calls(self, method_type: type) -> list[Any]:
        return [
            request
            for request in self.requests
            if isinstance(request, method_type)
        ]

    def on(self, method_type: type, handler: Callable[[Any], Any]) -> None:
        self.handlers[method_type] = handler

    def clear(self) -> None:
        self.requests.clear()
        self.times.clear()

    def set_admins(self, chat_id: int, *user_ids: int) -> None:
        members: list[Any] = []
        for user_id in user_ids:
            user = User(id=user_id, is_bot=False, first_name=f"Admin{user_id}")
            if user_id == OWNER_ID:
                members.append(ChatMemberOwner(user=user, is_anonymous=False))
            else:
                members.append(administrator(user))
        members.append(
            administrator(User(id=BOT_ID, is_bot=True, first_name="Bot"))
        )
        self.admins[chat_id] = members

    async def close(self) -> None:
        return None

    async def stream_content(self, *args: Any, **kwargs: Any):
        raise NotImplementedError
        yield b""

    async def make_request(
        self, bot: Bot, method: TelegramMethod, timeout: int | None = None
    ) -> Any:
        self.requests.append(method)
        self.times.append(time.monotonic())
        handler = self.handlers.get(type(method))
        if handler is not None:
            result = handler(method)
            if isinstance(result, Exception):
                raise result
            if result is not None:
                return result
        if isinstance(method, GetMe):
            return User(
                id=BOT_ID,
                is_bot=True,
                first_name="Bot",
                username="dekuspam_bot",
            )
        if isinstance(method, GetChatAdministrators):
            return self.admins.get(method.chat_id, [])
        if isinstance(method, (SendMessage, SendRichMessage)):
            self._next_message_id += 1
            chat_type = (
                "private"
                if isinstance(method.chat_id, int) and method.chat_id > 0
                else "supergroup"
            )
            return Message(
                message_id=self._next_message_id,
                date=dt.datetime.now(tz=dt.UTC),
                chat=Chat(id=int(method.chat_id), type=chat_type),
                text=getattr(method, "text", None),
            )
        return True


def administrator(user: User) -> ChatMemberAdministrator:
    return ChatMemberAdministrator(
        user=user,
        can_be_edited=False,
        is_anonymous=False,
        can_manage_chat=True,
        can_delete_messages=True,
        can_manage_video_chats=False,
        can_restrict_members=True,
        can_promote_members=False,
        can_change_info=False,
        can_invite_users=True,
        can_post_stories=False,
        can_edit_stories=False,
        can_delete_stories=False,
        can_send_welcome_messages=False,
    )


async def settle(services: Any) -> None:
    await services.notify.join()
    await services.ephemeral.join()


_SHARED_DISPATCHER: list[Any] = []
TEST_RULES: dict[str, Any] = {
    "defaults": {"detectors": {"mention_pattern": {"patterns": ["^dhm_"]}}}
}


def shared_dispatcher(services: Any) -> Any:
    if not _SHARED_DISPATCHER:
        _SHARED_DISPATCHER.append(build_dispatcher(services))
    _SHARED_DISPATCHER[0]["services"] = services
    return _SHARED_DISPATCHER[0]


@pytest.fixture
def session() -> FakeSession:
    fake = FakeSession()
    fake.set_admins(CHAT_ID, OWNER_ID, ADMIN_ID)
    return fake


@pytest.fixture
def bot(session: FakeSession) -> Bot:
    return Bot(
        token=FAKE_TOKEN,
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )


@pytest.fixture(scope="session")
def migrated_db_file(tmp_path_factory):
    path = tmp_path_factory.mktemp("template") / "test.db"

    async def build() -> None:
        database = Database(str(path), TEST_DB_KEY)
        await database.connect()
        await database.close()

    asyncio.run(build())
    return path


@pytest_asyncio.fixture
async def db(tmp_path, migrated_db_file):
    path = tmp_path / "test.db"
    shutil.copyfile(migrated_db_file, path)
    database = Database(str(path), TEST_DB_KEY)
    await database.connect()
    yield database
    await database.close()


@pytest.fixture
def rules_file():
    return parse_rules_document(TEST_RULES)


@pytest.fixture
def rules(db, rules_file) -> RulesStore:
    return RulesStore(db, rules_file)


class RecordingSink:
    def __init__(self) -> None:
        self.actions: list[Any] = []

    def submit(self, action: Any) -> None:
        self.actions.append(action)


@pytest.fixture
def sink() -> RecordingSink:
    return RecordingSink()


@pytest.fixture
def admins(bot: Bot) -> AdminCache:
    return AdminCache(bot, ttl_seconds=60)


@pytest_asyncio.fixture
async def engine(db, rules, admins, bot, sink) -> Engine:
    await db.upsert_chat(CHAT_ID, "Test Group", "supergroup", int(time.time()))
    return Engine(db, rules, admins, ActionExecutor(bot, db), sink=sink)


@pytest_asyncio.fixture
async def services(bot, db, rules):
    from tg_antispam_bot.services import build_services

    await db.upsert_chat(CHAT_ID, "Test Group", "supergroup", int(time.time()))
    built = build_services(
        bot,
        db,
        rules,
        ALLOWED_CHATS,
        per_admin_interval=0.001,
        global_per_second=10000,
        digest_edit_interval=0.0,
        verify_retry_seconds=0.001,
    )
    yield built
    await built.stop()


def rich_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return " ".join(rich_text(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return " ".join(rich_text(item) for item in value)
    return str(value)


def card_text(method: Any) -> str:
    return rich_text(method.rich_message.model_dump(exclude_none=True))


def tables(card: Any) -> list[Any]:
    return [block for block in card.blocks or [] if block.type == "table"]


def verbs(markup: Any) -> list[str]:
    return [
        button.callback_data.split(":")[2]
        for row in markup.inline_keyboard
        for button in row
        if button.callback_data and button.callback_data.startswith("act:")
    ]


NOW = int(time.time())


async def log_action(db: Any, **fields: Any) -> Any:
    defaults: dict[str, Any] = {
        "chat_id": CHAT_ID,
        "user_id": SPAMMER_ID,
        "user_name": "Spam",
        "score": 130,
        "reasons": ["mention_pattern: hit"],
        "message_text": "buy",
        "action": ACTION_BAN,
        "until_ts": None,
        "deleted_count": 1,
        "errors": [],
        "now": NOW,
    }
    return await db.insert_action(**{**defaults, **fields})


@pytest.fixture
def feed(bot, services):
    counter = [0]

    async def run(**fields):
        counter[0] += 1
        update = Update(update_id=counter[0], **fields)
        await shared_dispatcher(services).feed_update(bot, update)
        await settle(services)

    return run


def bot_member_event(
    new_member: Any,
    old_member: Any = None,
    adder: int = ADMIN_ID,
    chat: Chat | None = None,
) -> ChatMemberUpdated:
    bot_user = User(id=BOT_ID, is_bot=True, first_name="Bot")
    return ChatMemberUpdated(
        chat=chat or make_chat(),
        from_user=make_user(adder),
        date=dt.datetime.now(tz=dt.UTC),
        old_chat_member=old_member or ChatMemberLeft(user=bot_user),
        new_chat_member=new_member,
    )
