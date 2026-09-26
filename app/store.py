"""In-memory, thread-safe state: versioned contexts, conversations, suppression and per-merchant signals.

The judge requires the bot to keep state for the whole test window (no restarts); in-memory is
explicitly allowed. `/v1/teardown` wipes everything.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Optional

SCOPES = ("category", "merchant", "customer", "trigger")


@dataclass
class Conversation:
    conversation_id: str
    merchant_id: Optional[str]
    customer_id: Optional[str]
    trigger_id: Optional[str]
    kind: str = ""
    send_as: str = "vera"
    deliverable: str = ""
    stage: str = "pitched"          # pitched -> actioned -> confirmed ; or waiting / ended
    status: str = "open"            # open | waiting | ended
    end_reason: str = ""            # optout | hostile | auto_reply | declined | done
    wait_until: Optional[str] = None
    bot_bodies: list[str] = field(default_factory=list)
    turns: list[dict] = field(default_factory=list)
    no_count: int = 0
    hostile_count: int = 0
    offtopic_count: int = 0
    question_count: int = 0
    auto_reply_count: int = 0
    slots: list[str] = field(default_factory=list)


class Store:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.reset()

    def reset(self) -> None:
        with getattr(self, "lock", threading.RLock()):
            self.contexts: dict[tuple[str, str], dict[str, Any]] = {}
            self.conversations: dict[str, Conversation] = {}
            self.sent_suppression: set[str] = set()
            self.merchant_optout: set[str] = set()
            self.merchant_auto_msgs: dict[str, dict[str, int]] = {}   # merchant -> normalized msg -> count
            self.merchant_auto_total: dict[str, int] = {}
            self.merchant_last_body: dict[str, str] = {}              # anti-repetition across conversations
            self.merchant_unanswered: dict[str, int] = {}             # proactive sends since last human reply
            self.pending: dict[str, str] = {}                         # deferred trigger ids (ordered queue)

    # ------------------------------------------------------------------ contexts
    def put_context(self, scope: str, context_id: str, version: int, payload: dict) -> tuple[bool, Optional[int]]:
        """Returns (accepted, current_version_if_rejected)."""
        with self.lock:
            key = (scope, context_id)
            cur = self.contexts.get(key)
            if cur is not None and cur["version"] >= version:
                return False, cur["version"]
            self.contexts[key] = {"version": version, "payload": payload}
            return True, None

    def get(self, scope: str, context_id: Optional[str]) -> Optional[dict]:
        if not context_id:
            return None
        with self.lock:
            entry = self.contexts.get((scope, str(context_id)))
            return entry["payload"] if entry else None

    def counts(self) -> dict[str, int]:
        with self.lock:
            out = {s: 0 for s in SCOPES}
            for scope, _ in self.contexts:
                out[scope] = out.get(scope, 0) + 1
            return out

    def find_merchant(self, merchant_id: Optional[str]) -> Optional[dict]:
        m = self.get("merchant", merchant_id)
        if m is not None or not merchant_id:
            return m
        # tolerate short ids like "m_001_drmeera" vs "m_001_drmeera_dentist_delhi"
        with self.lock:
            for (scope, cid), entry in self.contexts.items():
                if scope == "merchant" and (cid.startswith(merchant_id) or merchant_id.startswith(cid)):
                    return entry["payload"]
        return None

    def find_customer(self, customer_id: Optional[str]) -> Optional[dict]:
        c = self.get("customer", customer_id)
        if c is not None or not customer_id:
            return c
        with self.lock:
            for (scope, cid), entry in self.contexts.items():
                if scope == "customer" and (cid.startswith(customer_id) or customer_id.startswith(cid)):
                    return entry["payload"]
        return None

    def category_for(self, merchant: Optional[dict]) -> dict:
        if not merchant:
            return {}
        return self.get("category", merchant.get("category_slug")) or {}


store = Store()
