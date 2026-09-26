"""HTTP surface required by the judge harness (challenge-testing-brief.md §2).

    POST /v1/context   versioned, idempotent context push
    POST /v1/tick      proactive sends for currently-active triggers
    POST /v1/reply     next move in a conversation (send / wait / end)
    GET  /v1/healthz   liveness + loaded context counts
    GET  /v1/metadata  bot identity
    POST /v1/teardown  wipe state at end of test (optional in spec)
"""
from __future__ import annotations

import json
import os
import time
from typing import Any

from concurrent.futures import ThreadPoolExecutor, wait as wait_futures

from fastapi import FastAPI, Request
from starlette.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from . import llm
from .composer import compose_draft
from .conversation import ReplyEngine
from .store import SCOPES, Conversation, store
from .util import g, parse_dt, utc_now_iso

START = time.time()
MAX_ACTIONS_PER_TICK = 20
MAX_CONTEXT_BYTES = 500_000       # testing brief §5
MAX_UNANSWERED_NUDGES = 3        # challenge brief §12.5: stop after 3 unanswered nudges
TICK_LLM_BUDGET_S = float(os.getenv("TICK_LLM_BUDGET", "18"))  # judge timeout is 30s
VERSION = "1.0.0"

app = FastAPI(title="Vera — magicpin merchant assistant", version=VERSION)
engine = ReplyEngine(store)


def _doc(example: dict) -> dict:
    """Show an editable JSON request box (pre-filled) in the /docs page."""
    return {"requestBody": {"required": True, "content": {"application/json": {
        "schema": {"type": "object"}, "example": example}}}}


CTX_EXAMPLE = {"scope": "merchant", "context_id": "m_demo", "version": 1, "delivered_at": "2026-04-26T10:00:00Z",
               "payload": {"merchant_id": "m_demo", "category_slug": "dentists",
                           "identity": {"name": "Demo Clinic", "owner_first_name": "Meera", "languages": ["en", "hi"]}}}
TICK_EXAMPLE = {"now": "2026-04-26T10:30:00Z",
                "available_triggers": ["trg_001_research_digest_dentists", "trg_003_recall_due_priya"]}
REPLY_EXAMPLE = {"conversation_id": "conv_m_001_drmeera_dentist_delhi_trg_001_research_digest_dentists",
                 "merchant_id": "m_001_drmeera_dentist_delhi", "customer_id": None, "from_role": "merchant",
                 "message": "Ok lets do it", "received_at": "2026-04-26T10:45:00Z", "turn_number": 2}


def _bad(reason: str, details: str = "", status: int = 400) -> JSONResponse:
    return JSONResponse(status_code=status, content={"accepted": False, "reason": reason, "details": details})


async def _json(request: Request) -> Any:
    raw = await request.body()
    try:
        return json.loads(raw.decode("utf-8") or "null")
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(str(exc))


# --------------------------------------------------------------------------- health / metadata

@app.get("/")
def root() -> dict:
    return {"service": "vera-bot", "endpoints": ["/v1/context", "/v1/tick", "/v1/reply", "/v1/healthz", "/v1/metadata"]}


@app.get("/v1/healthz")
def healthz() -> dict:
    return {"status": "ok", "uptime_seconds": int(time.time() - START), "contexts_loaded": store.counts()}


@app.get("/v1/metadata")
def metadata() -> dict:
    members = [m.strip() for m in os.getenv("TEAM_MEMBERS", "Vera Bot Team").split(",") if m.strip()]
    return {
        "team_name": os.getenv("TEAM_NAME", "Vera Bot Team"),
        "team_members": members,
        "model": llm.model_name(),
        "approach": (("LLM wording polish (temperature 0, validated: no new numbers/URLs/taboos, CTA preserved; "
                      "falls back to the deterministic draft) over a " if llm.enabled() else "") +
                     "Deterministic composer: trigger.kind router with per-kind templates grounded only in pushed "
                     "category/merchant/trigger/customer context (placeholder-safe fallbacks, no fabrication), "
                     "suppression + expiry aware tick scheduler, and a rule-based multi-turn engine for auto-reply "
                     "detection, intent->action handoff, hostile/off-topic handling, and graceful exits."),
        "contact_email": os.getenv("CONTACT_EMAIL", ""),
        "version": VERSION,
        "submitted_at": os.getenv("SUBMITTED_AT", "2026-09-26T00:00:00Z"),
    }


# --------------------------------------------------------------------------- context

@app.post("/v1/context", openapi_extra=_doc(CTX_EXAMPLE))
async def push_context(request: Request):
    if int(request.headers.get("content-length") or 0) > MAX_CONTEXT_BYTES:
        await request.body()   # drain the upload first; replying mid-upload makes clients see a connection reset
        return _bad("payload_too_large", f"context payload cap is {MAX_CONTEXT_BYTES} bytes", 413)
    try:
        body = await _json(request)
    except ValueError as exc:
        return _bad("malformed_json", str(exc))
    if not isinstance(body, dict):
        return _bad("invalid_body", "request body must be a JSON object")
    scope = body.get("scope")
    if scope not in SCOPES:
        return _bad("invalid_scope", f"scope must be one of {list(SCOPES)}")
    context_id = body.get("context_id")
    if not isinstance(context_id, str) or not context_id.strip():
        return _bad("invalid_context_id", "context_id must be a non-empty string")
    version = body.get("version")
    if isinstance(version, bool) or not isinstance(version, int) or version < 0:
        return _bad("invalid_version", "version must be a non-negative integer")
    payload = body.get("payload")
    if not isinstance(payload, dict):
        return _bad("invalid_payload", "payload must be a JSON object")

    accepted, current = store.put_context(scope, context_id, version, payload)
    if not accepted:
        return JSONResponse(status_code=409, content={"accepted": False, "reason": "stale_version", "current_version": current})
    return {"accepted": True, "ack_id": f"ack_{context_id}_v{version}", "stored_at": utc_now_iso()}


# --------------------------------------------------------------------------- tick

def _consented(customer: dict) -> bool:
    consent = customer.get("consent") or {}
    if not consent.get("scope") and not consent.get("opted_in_at"):
        return False
    if g(customer, "preferences", "channel") == "none_recorded":
        return False
    return True


SEND, DEFER, DROP = "send", "defer", "drop"
MAX_PENDING = 500
# messages per recipient per tick; extra triggers queue for later ticks (the harness ticks every 5 sim-minutes)
MAX_PER_RECIPIENT_PER_TICK = max(1, int(os.getenv("MAX_PER_RECIPIENT_PER_TICK", "1")))


def _open_same_kind(merchant_id: str, customer_id, kind: str) -> bool:
    """Is there still an unanswered conversation of this kind with this recipient? (don't stack same-kind nudges)"""
    for c in store.conversations.values():
        if (c.merchant_id == merchant_id and c.customer_id == customer_id and c.kind == kind and c.trigger_id
                and c.status != "ended" and not any(t.get("from") != "bot" for t in c.turns)):
            return True
    return False


def _decide(tid: str, trg, now, queued_only: bool, recipients: list, nudged: set):
    """Returns (decision, reason, context tuple). Pure decision; no state is mutated here."""
    if not trg:
        return DEFER, "trigger context not received yet", None
    supp = str(trg.get("suppression_key") or f"{trg.get('kind')}:{trg.get('merchant_id')}:{tid}")
    if supp in store.sent_suppression:
        return DROP, "suppression key already used", None
    # the judge's `available_triggers` list is authoritative for what is active; expires_at is only checked for
    # triggers we are carrying in our own queue (the judge may use wall-clock `now` vs simulated-time triggers)
    if queued_only:
        exp, cur = parse_dt(trg.get("expires_at")), parse_dt(now)
        if exp and cur and cur > exp:
            return DROP, "queued trigger expired", None
    merchant = store.find_merchant(trg.get("merchant_id") or g(trg, "payload", "merchant_id"))
    if not merchant:
        return DEFER, "merchant context not received yet", None
    merchant_id = merchant.get("merchant_id") or trg.get("merchant_id")
    category = store.category_for(merchant)
    if not category:
        return DEFER, "category context not received yet", None
    customer, customer_id = None, customer_id_of(trg)
    if customer_id is not None or trg.get("scope") == "customer":
        customer = store.find_customer(customer_id)
        if not customer:
            return DEFER, "waiting for customer context", None          # judge pushes customers mid-test
        if not _consented(customer):
            return DROP, "customer has no recorded consent", None
        recipient = ("c", customer_id)
    else:
        if merchant_id in store.merchant_optout:
            return DROP, "merchant opted out", None
        if (trg.get("urgency") or 0) < 4 and store.merchant_unanswered.get(merchant_id, 0) >= MAX_UNANSWERED_NUDGES:
            return DEFER, "3 unanswered nudge rounds; waiting for a reply", None
        recipient = ("m", merchant_id)
    if recipients.count(recipient) >= MAX_PER_RECIPIENT_PER_TICK:
        return DEFER, "recipient already messaged this tick", None      # default: max 1 per recipient per tick
    if _open_same_kind(merchant_id, customer_id, str(trg.get("kind", ""))):
        return DEFER, "same-kind conversation still unanswered", None
    return SEND, "", (supp, merchant, merchant_id, category, customer, customer_id, recipient)


def plan_tick(now: str | None, trigger_ids: list[str]) -> list[dict]:
    actions: list[dict] = []
    with store.lock:
        listed = list(dict.fromkeys(trigger_ids))
        queued = [t for t in store.pending if t not in listed]
        ordered = [(tid, store.get("trigger", tid), tid not in listed) for tid in listed + queued]
        # most urgent first; ties keep judge order, then queue order (stable sort)
        ordered.sort(key=lambda x: -((x[1] or {}).get("urgency") or 0))
        recipients: list = []
        nudged: set = set()
        for tid, trg, queued_only in ordered:
            decision, _reason, ctx = (DEFER, "tick action cap reached", None) if len(actions) >= MAX_ACTIONS_PER_TICK \
                else _decide(tid, trg, now, queued_only, recipients, nudged)
            if decision == SEND:
                action = _build_action(tid, trg, now, ctx, nudged)
                if action is None:
                    decision = DROP
                else:
                    recipients.append(ctx[6])
                    actions.append(action)
            if decision == DEFER and trg is not None:
                store.pending.setdefault(tid, now or "")
            else:
                store.pending.pop(tid, None)
        while len(store.pending) > MAX_PENDING:                          # bounded memory
            store.pending.pop(next(iter(store.pending)))
    _polish_actions(actions)
    return actions


def _build_action(tid: str, trg: dict, now, ctx, nudged: set):
    supp, merchant, merchant_id, category, customer, customer_id, _recipient = ctx
    draft, send_as, template_name = compose_draft(category, merchant, trg, customer, now)
    if not draft.body.strip():
        return None
    if store.merchant_last_body.get(f"{merchant_id}|{customer_id}") == draft.body:
        return None                                                    # anti-repetition across conversations
    conv_id = f"conv_{merchant_id}_{tid}" + (f"_{customer_id}" if customer_id else "")
    if conv_id in store.conversations:
        return None                                                    # never reuse a conversation_id in tick
    store.conversations[conv_id] = Conversation(
        conversation_id=conv_id, merchant_id=merchant_id, customer_id=customer_id, trigger_id=tid,
        kind=str(trg.get("kind", "")), send_as=send_as, deliverable=draft.deliverable,
        bot_bodies=[draft.body], turns=[{"from": "bot", "msg": draft.body, "at": now}],
        slots=[s.get("label") for s in (g(trg, "payload", "available_slots") or []) if isinstance(s, dict) and s.get("label")],
    )
    store.sent_suppression.add(supp)
    store.merchant_last_body[f"{merchant_id}|{customer_id}"] = draft.body
    if send_as == "vera" and merchant_id not in nudged:                 # one nudge round per tick
        nudged.add(merchant_id)
        store.merchant_unanswered[merchant_id] = store.merchant_unanswered.get(merchant_id, 0) + 1
    return {
        "conversation_id": conv_id,
        "merchant_id": merchant_id,
        "customer_id": customer_id,
        "send_as": send_as,
        "trigger_id": tid,
        "template_name": template_name,
        "template_params": draft.template_params,
        "body": draft.body,
        "cta": draft.cta,
        "suppression_key": supp,
        "rationale": draft.rationale,
        "_polish": {"kind": str(trg.get("kind", "")), "slug": category.get("slug", ""),
                    "keep": _identities(merchant, customer, send_as),
                    "tone": str(g(category, "voice", "tone") or ""),
                    "taboos": list(g(category, "voice", "vocab_taboo") or [])},
    }


def _identities(merchant: dict, customer, send_as: str) -> list[str]:
    """Names the LLM rewrite must keep: who is speaking and who is addressed."""
    from .util import customer_address, merchant_salutation
    keep = []
    if send_as == "merchant_on_behalf":
        keep.append(str(g(merchant, "identity", "name") or ""))
        greet, subject = customer_address(customer)
        keep.append(greet or subject.replace(" ji", ""))
    else:
        keep.append(merchant_salutation(merchant, str(merchant.get("category_slug", ""))))
    return [k for k in keep if k]


def customer_id_of(trg: dict):
    return trg.get("customer_id") if (trg.get("scope") == "customer" or trg.get("customer_id")) else None


def _polish_actions(actions: list[dict]) -> None:
    """Optional LLM wording pass, in parallel, inside the tick budget. Unfinished ones keep their draft."""
    meta = [a.pop("_polish") for a in actions]
    if not llm.enabled() or not actions:
        return
    pool = ThreadPoolExecutor(max_workers=min(10, len(actions)))
    futures = {pool.submit(llm.polish, a["body"], kind=m["kind"], category_slug=m["slug"], voice_tone=m["tone"],
                           taboos=m["taboos"], must_keep=m["keep"],
                           audience="customer" if a["send_as"] == "merchant_on_behalf" else "merchant"): a
               for a, m in zip(actions, meta)}
    done, _ = wait_futures(futures, timeout=TICK_LLM_BUDGET_S)
    pool.shutdown(wait=False, cancel_futures=True)
    with store.lock:
        for fut in done:
            a = futures[fut]
            try:
                body, changed = fut.result()
            except Exception:
                continue
            if not changed:
                continue
            conv = store.conversations.get(a["conversation_id"])
            if conv and conv.bot_bodies:
                conv.bot_bodies[0] = body
                conv.turns[0]["msg"] = body
            store.merchant_last_body[f"{a['merchant_id']}|{a['customer_id']}"] = body
            a["body"] = body
            a["rationale"] += " Wording polished by LLM and validated against the draft's facts."


@app.post("/v1/tick", openapi_extra=_doc(TICK_EXAMPLE))
async def tick(request: Request):
    try:
        body = await _json(request)
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"actions": [], "error": "malformed_json", "details": str(exc)})
    if not isinstance(body, dict):
        return JSONResponse(status_code=400, content={"actions": [], "error": "invalid_body"})
    triggers = body.get("available_triggers") or []
    if not isinstance(triggers, list):
        return JSONResponse(status_code=400, content={"actions": [], "error": "available_triggers must be a list"})
    now = body.get("now") if isinstance(body.get("now"), str) else None
    try:
        actions = await run_in_threadpool(plan_tick, now, [str(t) for t in triggers if isinstance(t, (str, int))])
    except Exception:  # a bad context must never break the tick budget
        actions = []
    return {"actions": actions}


# --------------------------------------------------------------------------- reply

@app.post("/v1/reply", openapi_extra=_doc(REPLY_EXAMPLE))
async def reply(request: Request):
    try:
        body = await _json(request)
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"error": "malformed_json", "details": str(exc)})
    if not isinstance(body, dict) or not isinstance(body.get("conversation_id"), str) or not body["conversation_id"].strip():
        return JSONResponse(status_code=400, content={"error": "invalid_body", "details": "conversation_id (string) is required"})
    if not isinstance(body.get("message", ""), str):
        return JSONResponse(status_code=400, content={"error": "invalid_body", "details": "message must be a string"})
    try:
        result = await run_in_threadpool(engine.handle, body)
        if result.get("action") == "send" and llm.enabled():
            result = await run_in_threadpool(_polish_reply, body, result)
        return result
    except Exception:
        return {"action": "wait", "wait_seconds": 1800, "rationale": "Internal error while composing; backing off safely."}


def _polish_reply(req: dict, result: dict) -> dict:
    conv = store.conversations.get(req["conversation_id"])
    merchant = store.find_merchant(req.get("merchant_id") or (conv.merchant_id if conv else None)) or {}
    category = store.category_for(merchant)
    body, changed = llm.polish(result["body"], kind=(conv.kind if conv else "reply"), category_slug=category.get("slug", ""),
                               voice_tone=str(g(category, "voice", "tone") or ""),
                               taboos=list(g(category, "voice", "vocab_taboo") or []),
                               must_keep=[str(g(merchant, "identity", "name") or "")] if req.get("from_role") == "customer" else [],
                               audience="customer" if req.get("from_role") == "customer" else "merchant")
    if not changed:
        return result
    with store.lock:
        if conv:
            if body in conv.bot_bodies:           # never repeat a body in a conversation
                return result
            if conv.bot_bodies and conv.bot_bodies[-1] == result["body"]:
                conv.bot_bodies[-1] = body
                conv.turns[-1]["msg"] = body
    result = dict(result, body=body)
    result["rationale"] += " Wording polished by LLM and validated against the draft's facts."
    return result


# --------------------------------------------------------------------------- teardown

@app.post("/v1/teardown")
def teardown() -> dict:
    store.reset()
    return {"ok": True, "wiped_at": utc_now_iso()}
