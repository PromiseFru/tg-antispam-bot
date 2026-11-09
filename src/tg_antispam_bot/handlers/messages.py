from __future__ import annotations

from aiogram import F, Router
from aiogram.types import Message

from ..services import Services

router = Router(name="messages")
GROUP_FILTER = F.chat.type.in_({"group", "supergroup"})


@router.message(GROUP_FILTER)
async def on_message(message: Message, services: Services) -> None:
    await services.engine.handle_message(message)


@router.edited_message(GROUP_FILTER)
async def on_edited_message(message: Message, services: Services) -> None:
    await services.engine.handle_message(message, is_edit=True)
