from __future__ import annotations

from contextvars import ContextVar, Token

from app.auth.models import UserContext


_USER_CONTEXT: ContextVar[UserContext | None] = ContextVar("hth_user_context", default=None)


def get_user_context() -> UserContext | None:
    return _USER_CONTEXT.get()


def set_user_context(user_context: UserContext) -> Token[UserContext | None]:
    return _USER_CONTEXT.set(user_context)


def reset_user_context(token: Token[UserContext | None]) -> None:
    _USER_CONTEXT.reset(token)
