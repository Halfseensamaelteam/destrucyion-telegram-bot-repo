"""
tests/unit/bot/test_bot_config.py
Guards the /connect ConversationHandler configuration itself.

Regression coverage for: a user who starts /connect and abandons it
mid-flow (no /cancel) getting permanently stuck — every future /connect
from them silently matches nothing and gets dropped with NO error and NO
log line, because PTB treats them as "still in a conversation" and only
re-enters via entry_points when allow_reentry=True.
"""

from app.bot.bot import create_bot_app


def _find_connect_conversation(application):
    from telegram.ext import ConversationHandler

    for group_handlers in application.handlers.values():
        for handler in group_handlers:
            if isinstance(handler, ConversationHandler) and handler.name == "connect_conversation":
                return handler
    raise AssertionError("connect_conversation handler not registered")


def test_connect_conversation_allows_reentry():
    app = create_bot_app()
    handler = _find_connect_conversation(app)
    assert handler.allow_reentry is True


def test_connect_conversation_has_a_timeout():
    """An abandoned /connect attempt must eventually expire rather than
    being stuck forever (allow_reentry alone still leaves a stale entry in
    the persisted conversation store until it times out or is replaced)."""
    app = create_bot_app()
    handler = _find_connect_conversation(app)
    assert handler.conversation_timeout is not None
    assert handler.conversation_timeout > 0
