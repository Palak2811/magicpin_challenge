"""Optional multi-turn contract (challenge-brief.md §7.4).

`respond(state, merchant_message)` wraps the same engine /v1/reply uses. `state` is a dict with at least
`conversation_id` and `merchant_id`; the store must already hold the merchant/category contexts.
"""
from __future__ import annotations

from app.conversation import ReplyEngine
from app.store import store

_engine = ReplyEngine(store)


def respond(state: dict, merchant_message: str) -> dict:
    body = {
        "conversation_id": state.get("conversation_id", "conv_local"),
        "merchant_id": state.get("merchant_id"),
        "customer_id": state.get("customer_id"),
        "from_role": state.get("from_role", "merchant"),
        "message": merchant_message,
        "received_at": state.get("received_at"),
        "turn_number": state.get("turn_number", 2),
    }
    return _engine.handle(body)
