from __future__ import annotations

from aiogram import Router

from . import callbacks, group_admin, members, messages, private


def build_router() -> Router:
    root = Router(name="root")
    root.include_router(members.router)
    root.include_router(group_admin.router)
    root.include_router(private.router)
    root.include_router(callbacks.router)
    root.include_router(messages.router)
    return root
