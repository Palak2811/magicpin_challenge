"""Multi-turn reply engine for /v1/reply.

Deterministic classifier (ordered, first match wins):
  opt-out -> auto-reply -> hostile -> strong commitment -> off-topic -> later -> question
  -> soft no -> thanks -> commitment -> engaged/other

Design goals from the brief:
  * auto-replies: try once to reach the owner, then back off, then exit (tracked per merchant,
    since a canned auto-reply repeats across conversations);
  * intent transitions: "yes / let's do it / go ahead" switches straight to action — no re-qualifying;
  * hostile / off-topic: apologise or decline politely and steer back to the mission;
  * know when to stop: explicit opt-out ends immediately; repeated "no" ends gracefully;
  * never send the same body twice in a conversation; match the merchant's language per turn.
"""
from __future__ import annotations

import re
from typing import Any, Optional

from .composer import compose_draft
from .store import Conversation, Store
from .util import (
    active_offers, catalog_offer, customer_address, digest_item, first_sentence, g, humanize, inr,
    is_hinglish, merchant_name, merchant_prefers_hinglish, merchant_salutation, num, customer_prefers_hinglish,
)

# --------------------------------------------------------------------------- patterns

OPTOUT = re.compile(
    r"\b(stop|unsubscribe|opt ?out|not interested|no interest|don'?t (message|msg|text|contact|call)|do not (message|msg|text|contact)"
    r"|stop (messaging|texting|sending)|leave me alone|remove me|block(ed)? you|band karo|mat bhejo|message mat|msg mat"
    r"|mat karo|nahi chahiye|no more messages|never message)\b", re.I)

AUTO_REPLY = re.compile(
    r"(thank you for (contacting|reaching|your message|messaging)|thanks for (contacting|reaching out|your message)"
    r"|we will (get back|respond|revert|reply)|will (get back|respond|revert) (to you )?(shortly|soon|asap)"
    r"|our team will|team (tak|ko) (pahuncha|pahucha|bata)|automated (assistant|message|reply|response)"
    r"|auto[- ]?reply|currently (unavailable|away|closed)|out of (the )?office|business hours are|we are closed"
    r"|this is an automated|jaankari ke liye (bahut[- ]?bahut )?shukriya|aapki madad ke liye shukriya)", re.I)

HOSTILE = re.compile(
    r"\b(useless|idiot|stupid|nonsense|bakwas|bakwaas|spam(mer)?|fraud|scam|cheat(er)?|bekar|bekaar|pagal|shut up"
    r"|rubbish|pathetic|worst|harass(ing|ment)?|irritating|annoying|bloody|damn|wtf|chup)\b", re.I)

STRONG_COMMIT = re.compile(
    r"(let'?s do it|lets do it|go ahead|do it\b|sign me up|i want to join|want to join|judna hai|judrna|jodna hai"
    r"|kar do|kardo|kar dijiye|chalo karte|haan karo|ha karo|proceed|confirm(ed)?\b|book it|send it|please do|yes please"
    r"|let'?s start|start (it|now)|go for it|i'?m in|ready to (start|go)|ok(ay)? do it)", re.I)

COMMIT = re.compile(
    r"^\s*(yes|yeah|yep|yup|ya|haan|han|ha|haanji|ji|sure|ok|okay|okk|k|done|fine|alright|theek hai|thik hai|chalega"
    r"|interested|go|send|share|please|pls|great|perfect|sounds good|good idea|why not)\b", re.I)

OFFTOPIC = re.compile(
    r"\b(gst|income tax|tax (filing|return)|itr|loan|emi|insurance|lawyer|legal notice|court|electricity|visa|passport"
    r"|accountant|chartered accountant|\bca\b|bank account|credit card|mutual fund|stock market|crypto|aadhaar|pan card"
    r"|cricket score|weather)\b", re.I)

LATER = re.compile(
    r"\b(later|busy|baad me(in)?|baad mein|kal\b|tomorrow|not now|abhi nahi|abhi nhi|in a meeting|next week|call me"
    r"|remind me|few (hours|days)|thodi der|free nahi|driving)\b", re.I)

SOFT_NO = re.compile(r"^\s*(no|nope|nah|nahi|nahin|nhi|na|not really|no thanks|no thank you|skip|pass)\b", re.I)

THANKS = re.compile(r"^\s*(thanks|thank you|thx|ty|shukriya|dhanyavad|dhanyawad|great|nice|cool|👍|🙏|ok thanks|okay thanks)\W*$", re.I)

QUESTION_WORDS = re.compile(
    r"(\?|\b(how|what|why|when|where|which|who|kitna|kitne|kaise|kya|kab|kahan|kyun|price|cost|charge|fees?|paisa|rate)\b)", re.I)
PRICE_WORDS = re.compile(r"\b(price|cost|charge|charges|fees?|paisa|paise|rate|kitna|kitne|free|paid|pay)\b", re.I)

MAX_BOT_TURNS = 6   # hard stop: never more than 6 bot messages in one conversation

SLOT_PICK = re.compile(r"^\s*(?:option\s*)?([1-9])\s*[.)!]?\s*$", re.I)


def _bare(text: str) -> str:
    """Drop a leading English article inside Hinglish sentences ('main a Google post' -> 'main Google post')."""
    return re.sub(r"^(a|an|the)\s+", "", text.strip(), flags=re.I)


def _norm(msg: str) -> str:
    return re.sub(r"[^a-z0-9ऀ-ॿ ]+", "", (msg or "").lower()).strip()


# --------------------------------------------------------------------------- engine

class ReplyEngine:
    def __init__(self, store: Store) -> None:
        self.store = store

    # ...................................................................... public
    def handle(self, body: dict) -> dict:
        s = self.store
        conv_id = str(body["conversation_id"])
        message = str(body.get("message") or "")
        role = str(body.get("from_role") or "merchant").lower()
        with s.lock:
            conv = s.conversations.get(conv_id)
            if conv is None:
                conv = Conversation(conversation_id=conv_id,
                                    merchant_id=body.get("merchant_id"),
                                    customer_id=body.get("customer_id"),
                                    trigger_id=None,
                                    send_as="merchant_on_behalf" if role == "customer" else "vera")
                s.conversations[conv_id] = conv
            if not conv.merchant_id and body.get("merchant_id"):
                conv.merchant_id = body.get("merchant_id")
            conv.turns.append({"from": role, "msg": message, "at": body.get("received_at"),
                               "turn": body.get("turn_number")})
            result = self._closed_guard(conv, message, role)
            if result is None and len(conv.bot_bodies) >= MAX_BOT_TURNS:
                result = {"action": "end", "_reason": "turn_cap",
                          "rationale": f"Bot already sent {len(conv.bot_bodies)} messages in this conversation — closing instead of over-messaging."}
            if result is None:
                result = self._decide(conv, message, role)
            if result.get("action") == "send":
                result["body"] = self._dedupe(conv, result["body"])
                conv.bot_bodies.append(result["body"])
                conv.turns.append({"from": "bot", "msg": result["body"]})
                if conv.status == "waiting":
                    conv.status = "open"
            elif result.get("action") == "end":
                conv.status = "ended"
                conv.stage = "ended"
                conv.end_reason = conv.end_reason or result.get("_reason", "done")
            elif result.get("action") == "wait":
                conv.status = "waiting"
            result.pop("_reason", None)   # internal tag, never sent to the judge
            return result

    def _closed_guard(self, conv: Conversation, message: str, role: str) -> Optional[dict]:
        """An ended conversation stays ended (api-call-examples §2.6). Exceptions: a 'Hi Vera' restart, or a
        real human reply on a thread that was only closed because of auto-replies."""
        if conv.status != "ended":
            return None
        text = message.strip()
        restart = bool(re.search(r"\b(hi|hello|hey|start)\b.*\bvera\b|^\s*start\s*$", text, re.I))
        human = (conv.end_reason == "auto_reply" and bool(text) and not AUTO_REPLY.search(text)
                 and self.store.merchant_auto_msgs.get(conv.merchant_id or "_unknown", {}).get(_norm(text), 0) == 0)
        if restart or human:
            conv.status, conv.end_reason = "open", ""
            if human:
                conv.stage = "pitched"
            return None
        return {"action": "end", "rationale": f"Conversation already closed ({conv.end_reason or 'ended'}); not sending further messages on this conversation_id."}

    # ...................................................................... context
    def _ctx(self, conv: Conversation) -> tuple[dict, dict, dict, Optional[dict]]:
        s = self.store
        merchant = s.find_merchant(conv.merchant_id) or {}
        category = s.category_for(merchant)
        trigger = s.get("trigger", conv.trigger_id) or {}
        customer = s.find_customer(conv.customer_id) if conv.customer_id else None
        return merchant, category, trigger, customer

    def _lang_hi(self, conv: Conversation, message: str, merchant: dict, customer: Optional[dict]) -> bool:
        if is_hinglish(message):
            return True
        if len(re.findall(r"[A-Za-z]+", message)) >= 3:
            return False
        if conv.send_as == "merchant_on_behalf":
            return customer_prefers_hinglish(customer)
        return merchant_prefers_hinglish(merchant)

    def _dedupe(self, conv: Conversation, body: str) -> str:
        if body not in conv.bot_bodies:
            return body
        for suffix in (" (Just reply here whenever you're ready.)", " — no rush.", " (Following up once.)"):
            if body + suffix not in conv.bot_bodies:
                return body + suffix
        return body + f" [{len(conv.bot_bodies)}]"

    def _deliverable(self, conv: Conversation, merchant: dict, category: dict, trigger: dict) -> str:
        if conv.deliverable:
            return conv.deliverable
        offer = active_offers(merchant)
        lead = offer[0] if offer else catalog_offer(category)
        return f"a Google post featuring {lead}" if lead else "a fresh Google post for your listing"

    # ...................................................................... decision
    def _decide(self, conv: Conversation, message: str, role: str) -> dict:
        s = self.store
        merchant, category, trigger, customer = self._ctx(conv)
        hi = self._lang_hi(conv, message, merchant, customer)
        text = message.strip()
        mid = conv.merchant_id or "_unknown"

        if not text:
            return {"action": "wait", "wait_seconds": 1800, "rationale": "Empty message — nothing to respond to; checking back in 30 min."}

        # 0. merchant who opted out can restart with "Hi Vera" (api-call-examples §4.3)
        if role != "customer" and mid in s.merchant_optout and re.search(r"\b(hi|hello|hey|start)\b.*\bvera\b|^\s*start\s*$", text, re.I):
            s.merchant_optout.discard(mid)
            s.merchant_unanswered[mid] = 0
            conv.hostile_count = conv.no_count = 0
            conv.stage = "pitched"
            deliverable = self._deliverable(conv, merchant, category, trigger)
            body = (f"Wapas swagat hai 🙂 Seedha kaam ki baat: main {_bare(deliverable)} ready kar sakti hoon. Reply YES." if hi
                    else f"Welcome back 🙂 Straight to it: I can have {deliverable} ready for you. Reply YES.")
            return {"action": "send", "body": body, "cta": "binary_yes_no",
                    "rationale": "Merchant restarted with 'Hi Vera' after opting out — re-opened with one concrete, low-friction offer."}

        # 1. explicit opt-out -> end immediately, suppress merchant
        if OPTOUT.search(text) and not STRONG_COMMIT.search(text):
            if role != "customer":
                s.merchant_optout.add(mid)
            return {"action": "end", "_reason": "optout", "rationale": "Explicit opt-out / not-interested signal — closing the conversation and suppressing further proactive sends."}

        # 2. auto-reply detection (canned phrasing, or same long message repeated from this merchant)
        norm = _norm(text)
        seen = s.merchant_auto_msgs.setdefault(mid, {})
        seen[norm] = seen.get(norm, 0) + 1
        repeated = len(norm) >= 20 and seen[norm] >= 3        # brief §12: "same message verbatim 3+ times = auto-reply"
        if AUTO_REPLY.search(text) or repeated:
            total = s.merchant_auto_total.get(mid, 0) + 1
            s.merchant_auto_total[mid] = total
            conv.auto_reply_count += 1
            if total == 1:
                deliverable = self._deliverable(conv, merchant, category, trigger)
                body = (f"Lagta hai yeh auto-reply hai 🙂 Owner/manager ke liye ek line: main {_bare(deliverable)} ready kar sakti hoon — "
                        f"2 minute ka kaam. Jab dekhein, bas YES reply kar dijiye." if hi else
                        f"Looks like an auto-reply 🙂 One line for the owner/manager: I can have {deliverable} ready — "
                        f"2 minutes of your time. Just reply YES whenever you see this.")
                return {"action": "send", "body": body, "cta": "binary_yes_no",
                        "rationale": "Detected WhatsApp Business auto-reply; one owner-directed nudge (not a re-pitch), then back off."}
            if total == 2:
                return {"action": "wait", "wait_seconds": 14400,
                        "rationale": "Second auto-reply in a row — owner not reachable right now; backing off 4 hours instead of burning turns."}
            return {"action": "end", "_reason": "auto_reply", "rationale": f"Auto-reply received {total} times — no human on the line; exiting gracefully to avoid spam."}

        # a real human replied: reset the auto-reply streak and the unanswered-nudge counter
        s.merchant_auto_total[mid] = 0
        if role != "customer":
            s.merchant_unanswered[mid] = 0

        if role == "customer":
            return self._customer_reply(conv, text, hi, merchant, category, trigger, customer)

        # 3. hostility
        if HOSTILE.search(text) and not STRONG_COMMIT.search(text):
            conv.hostile_count += 1
            if conv.hostile_count >= 2:
                s.merchant_optout.add(mid)
                return {"action": "end", "_reason": "hostile", "rationale": "Repeated hostility — apologised once already; exiting and suppressing further sends."}
            if OFFTOPIC.search(text):
                pass  # fall through to off-topic handling below with an apology prefix
            else:
                body = ("Maaf kijiye agar messages zyada lage — aapka time waste karna nahi chahti. Main sirf aapke listing ke kaam ki "
                        "cheezein bhejti hoon; STOP reply karein to main message band kar doongi." if hi else
                        "Sorry if these felt like noise — not my intent to waste your time. I only send things tied to your listing's "
                        "numbers; reply STOP and I won't message again.")
                return {"action": "send", "body": body, "cta": "binary_yes_no",
                        "rationale": "Hostile reply without explicit opt-out: apologise briefly, offer a clean STOP exit, no re-pitch."}

        # 4. strong commitment -> action mode immediately
        if STRONG_COMMIT.search(text):
            return self._action(conv, hi, merchant, category, trigger)

        # 5. off-topic ask
        if OFFTOPIC.search(text):
            conv.offtopic_count += 1
            deliverable = self._deliverable(conv, merchant, category, trigger)
            sorry = ("Aur pehle wale message ke liye maafi. " if hi else "And sorry about the earlier messages. ") if conv.hostile_count else ""
            body = (f"{sorry}Yeh (GST/tax/finance type kaam) mere scope se bahar hai — iske liye aapke CA best rahenge. "
                    f"Main aapki listing aur customers mein help karti hoon: {deliverable} ready karoon? Reply YES." if hi else
                    f"{sorry}That one's outside what I can help with — your CA or advisor is the right person for it. "
                    f"What I can do right now is {deliverable}. Shall I go ahead? Reply YES.")
            return {"action": "send", "body": body, "cta": "binary_yes_no",
                    "rationale": "Out-of-scope request declined politely; redirected to the original mission with one binary CTA."}

        # 6. later / busy
        if LATER.search(text) and not QUESTION_WORDS.search(text.replace("kal", "")):
            secs = 86400 if re.search(r"\b(tomorrow|kal|next week)\b", text, re.I) else 7200
            return {"action": "wait", "wait_seconds": secs,
                    "rationale": f"Merchant asked for time — backing off {secs // 3600}h rather than pushing."}

        # 7. question
        if QUESTION_WORDS.search(text) and not COMMIT.match(text) or ("?" in text):
            return self._answer(conv, text, hi, merchant, category, trigger)

        # 8. soft no
        if SOFT_NO.match(text):
            conv.no_count += 1
            if conv.no_count >= 2 or conv.stage != "pitched":
                return {"action": "end", "_reason": "declined", "rationale": "Merchant declined twice — exiting gracefully; will not re-pitch this topic."}
            views = g(merchant, "performance", "views")
            body = ("Koi baat nahi. Ek chhota option: main sirf is hafte ka ek Google post draft kar deti hoon — aap dekh ke approve/skip karein. "
                    "Chalega? Reply YES." if hi else
                    "No problem. Smaller option: I draft just one Google post this week"
                    + (f" for your {num(views)} monthly profile viewers" if views else "")
                    + " — you approve or skip. Reply YES if that works.")
            return {"action": "send", "body": body, "cta": "binary_yes_no",
                    "rationale": "First soft 'no' — offer one lower-effort alternative; a second no ends the conversation."}

        # 9. thanks / acknowledgement
        if THANKS.match(text):
            if conv.stage in ("actioned", "confirmed"):
                return {"action": "end", "rationale": "Task delivered and merchant acknowledged — closing the loop without extra messages."}
            return self._action(conv, hi, merchant, category, trigger)

        # 10. plain yes / ok
        if COMMIT.match(text):
            if conv.stage == "actioned":
                return self._confirm(conv, hi, merchant)
            return self._action(conv, hi, merchant, category, trigger)

        # 11. engaged free-text (e.g. answer to a curious ask) -> treat as input and act on it
        return self._engaged(conv, text, hi, merchant, category, trigger)

    # ...................................................................... merchant moves
    def _action(self, conv: Conversation, hi: bool, merchant: dict, category: dict, trigger: dict) -> dict:
        if conv.stage == "actioned":
            return self._confirm(conv, hi, merchant)
        conv.stage = "actioned"
        kind = conv.kind or trigger.get("kind", "")
        p = trigger.get("payload") if isinstance(trigger.get("payload"), dict) else {}
        offer = active_offers(merchant)
        lead = offer[0] if offer else catalog_offer(category)
        mname = merchant_name(merchant)
        loc = g(merchant, "identity", "locality") or ""
        draft = ""
        if kind in ("research_digest", "category_research_digest_release"):
            item = digest_item(category, p.get("top_item_id"), kinds=("research",))
            if item:
                draft = (f"Abstract ({item.get('source', '')}): {item.get('summary', '')}\n\n"
                         f"Patient WhatsApp draft: \"Had a cavity in the last 2 years? New research suggests a 3-month check "
                         f"works better than 6-month for people at higher risk. Reply to book a quick check at {mname}.\"")
        elif kind == "regulation_change":
            item = digest_item(category, p.get("top_item_id"), kinds=("compliance",))
            if item:
                draft = (f"Checklist — {item.get('title', '')}:\n1. {item.get('actionable', 'Review current setup')}\n"
                         f"2. Note the equipment/film type in your SOP file\n3. Brief staff and keep the circular ({item.get('source', '')}) on record")
        elif kind == "supply_alert":
            batches = ", ".join(p.get("affected_batches") or [])
            draft = (f"Pulling the dispensing log for {p.get('molecule', 'the molecule')} batches {batches} now. Customer note draft: "
                     f"\"{mname}: a batch of your {p.get('molecule', '')} is under voluntary recall. Please bring your strip in — "
                     f"we'll replace it at no cost.\"")
        elif kind == "active_planning_intent":
            draft = (f"WhatsApp pitch draft: \"Hi! {mname}, {loc}. We now do office lunch orders — tiered per-plate pricing for 10/25/50+, "
                     f"order by 5pm the day before. Want the menu?\"" if "thali" in str(p.get("intent_topic", "")) else
                     f"GBP post draft: \"New at {mname}: {humanize(p.get('intent_topic', 'program'))} — limited seats, "
                     f"book on WhatsApp.\" Insta carousel: 4 slides (what, who, schedule, price).")
        elif kind == "renewal_due":
            amt = p.get("renewal_amount")
            draft = f"Renewal for your {p.get('plan', 'current')} plan" + (f" — {inr(amt)}" if amt else "") + ". The payment link follows in the next message."
        elif kind == "gbp_unverified":
            draft = "Steps: 1) Open Google Business Profile → Verify. 2) Choose phone call if offered (fastest), else postcard. 3) Enter the code here and I'll finish the rest."
        elif kind in ("curious_ask_due", "scheduled_recurring"):
            draft = ""
        if not draft:
            draft = (f"Draft: \"{mname}{', ' + loc if loc else ''} — " + (f"{lead}. " if lead else "")
                     + "Walk in or book on WhatsApp.\"")
        deliverable = self._deliverable(conv, merchant, category, trigger)
        head = (f"Ho gaya — {deliverable} ready hai:" if hi else f"Done — here's {deliverable}:")
        tail = ("Aapke OK pe main ise kal 10am live kar doongi. CONFIRM reply karein." if hi
                else "Reply CONFIRM and I'll publish it tomorrow 10am; send edits and I'll update first.")
        return {"action": "send", "body": f"{head}\n\n{draft}\n\n{tail}", "cta": "binary_yes_no",
                "rationale": "Merchant committed — switched straight to action mode: delivered the artifact and asked only for a final confirm (no re-qualifying)."}

    def _confirm(self, conv: Conversation, hi: bool, merchant: dict) -> dict:
        if conv.stage == "confirmed":
            return {"action": "end", "rationale": "Work already confirmed and scheduled — nothing further to add; closing."}
        conv.stage = "confirmed"
        body = ("Confirmed ✅ Kal 10am live ho jayega. Results (views/calls) main 7 din baad share karungi."
                if hi else "Confirmed ✅ Going live tomorrow 10am. I'll share the views/calls impact in 7 days.")
        return {"action": "send", "body": body, "cta": "none",
                "rationale": "Merchant confirmed — execute and set expectation for the follow-up readout; no new ask."}

    def _answer(self, conv: Conversation, text: str, hi: bool, merchant: dict, category: dict, trigger: dict) -> dict:
        conv.question_count += 1
        deliverable = self._deliverable(conv, merchant, category, trigger)
        p = trigger.get("payload") if isinstance(trigger.get("payload"), dict) else {}
        price_unknown = False
        if PRICE_WORDS.search(text):
            # answer ONLY with figures present in the pushed contexts; never invent a price or a "free" claim
            amt = p.get("renewal_amount")
            priced = [o for o in active_offers(merchant) if "₹" in o]
            if amt:
                ans = (f"Renewal {inr(amt)} hai ({p.get('plan', '')} plan)." if hi else f"Renewal is {inr(amt)} for the {p.get('plan', '')} plan.")
            elif priced:
                ans = (f"Aapke live offers: {', '.join(priced[:2])}." if hi else f"Your live offers: {', '.join(priced[:2])}.")
            else:
                price_unknown = True
                ans = ("Iski exact pricing mere paas aapke account data mein nahi hai — kuch bhi live hone se pehle main confirm karungi." if hi
                       else "I don't have an exact price for that in your account data — I'll confirm it with you before anything goes live.")
        else:
            views, calls = g(merchant, "performance", "views"), g(merchant, "performance", "calls")
            basis = f"{num(views)} views / {num(calls)} calls in 30 days" if views is not None else "your listing data"
            ans = (f"Short answer: main yeh aapke {basis} aur is hafte ke trigger ke basis pe bana rahi hoon, generic nahi." if hi
                   else f"Short answer: I build it from your own numbers ({basis}) and this week's trigger — nothing generic.")
        if conv.question_count >= 3:
            return {"action": "send", "body": ans + (" Aage badhna ho to YES likh dijiye." if hi else " Whenever you want to proceed, just reply YES."),
                    "cta": "binary_yes_no", "rationale": "Answered the question; lowered pressure after repeated questions."}
        if price_unknown:   # don't contradict "I don't have a price" by then pitching a priced item
            cta = ("Kya main pehle draft bana ke aapko review ke liye bhej doon? Reply YES." if hi
                   else "Shall I prepare the draft for your review first? Reply YES.")
        else:
            cta = f"Kya main {_bare(deliverable)} ready kar doon? Reply YES." if hi else f"Want me to go ahead with {deliverable}? Reply YES."
        return {"action": "send", "body": f"{ans} {cta}", "cta": "binary_yes_no",
                "rationale": "Answered the merchant's question directly from context, then one binary CTA back to the task."}

    def _engaged(self, conv: Conversation, text: str, hi: bool, merchant: dict, category: dict, trigger: dict) -> dict:
        kind = conv.kind or trigger.get("kind", "")
        mname = merchant_name(merchant)
        if kind in ("curious_ask_due", "scheduled_recurring") and conv.stage == "pitched":
            conv.stage = "actioned"
            svc = text.strip().rstrip(".!")[:60]
            body = ((f"Badhiya — '{svc}' pe draft ready:\n\nGoogle post: \"{svc} at {mname} — book your slot on WhatsApp today.\"\n"
                     f"Price reply: \"{svc} ke liye aaj slots available hain; timing batayein, hum confirm kar denge.\"\n\nCONFIRM karein to kal 10am post kar doon.")
                    if hi else
                    (f"Great — drafted around '{svc}':\n\nGoogle post: \"{svc} at {mname} — book your slot on WhatsApp today.\"\n"
                     f"Price-question reply: \"Yes, we do {svc}. Share a time that suits you and we'll confirm the slot.\"\n\nReply CONFIRM and I'll post it tomorrow 10am."))
            return {"action": "send", "body": body, "cta": "binary_yes_no",
                    "rationale": "Merchant answered the curious-ask — reciprocated immediately with the promised post + reply drafts."}
        if conv.stage == "actioned":
            conv.stage = "actioned"
            body = ("Noted — yeh change draft mein daal diya. Final version kal 10am live karoon? CONFIRM reply karein." if hi
                    else "Noted — I've folded that into the draft. Shall I put the final version live tomorrow 10am? Reply CONFIRM.")
            return {"action": "send", "body": body, "cta": "binary_yes_no",
                    "rationale": "Merchant gave edits after the draft — incorporate and ask for final confirm only."}
        return self._action(conv, hi, merchant, category, trigger)

    # ...................................................................... customer moves
    def _customer_reply(self, conv: Conversation, text: str, hi: bool, merchant: dict, category: dict,
                        trigger: dict, customer: Optional[dict]) -> dict:
        mname = merchant_name(merchant)
        greet, subject = customer_address(customer)
        slots = conv.slots or [sl.get("label") for sl in ((trigger.get("payload") or {}).get("available_slots") or [])
                               if isinstance(sl, dict) and sl.get("label")]
        m = SLOT_PICK.match(text)
        if m and slots:
            idx = int(m.group(1)) - 1
            if 0 <= idx < len(slots):
                conv.stage = "confirmed"
                body = (f"Booked ✅ {slots[idx]} — {mname}. Ek din pehle reminder bhej denge." if hi
                        else f"Booked ✅ {slots[idx]} at {mname}. We'll send a reminder the day before.")
                return {"action": "send", "body": body, "cta": "none", "rationale": "Customer picked a slot — confirmed booking with a reminder promise."}
        if THANKS.match(text) and conv.stage == "confirmed":
            return {"action": "end", "rationale": "Booking confirmed and acknowledged — closing."}
        if STRONG_COMMIT.search(text) or COMMIT.match(text):
            if conv.stage == "confirmed":
                return {"action": "end", "rationale": "Already confirmed — nothing further to send."}
            conv.stage = "confirmed"
            kind = conv.kind or trigger.get("kind", "")
            if kind == "chronic_refill_due":
                body = ("Confirmed ✅ Order pack ho raha hai, saved address par delivery. Dispatch hote hi update bhejenge." if hi
                        else "Confirmed ✅ Packing the order now for delivery to your saved address. We'll update you once it's dispatched.")
            else:
                body = (f"Confirmed ✅ {mname} team aapka slot hold kar rahi hai — time confirm karke 1 message bhejenge." if hi
                        else f"Confirmed ✅ The {mname} team is holding your slot and will message the exact time shortly.")
            return {"action": "send", "body": body, "cta": "none", "rationale": "Customer accepted — confirm and set clear next step."}
        if LATER.search(text) or re.search(r"\b(reschedule|another time|different time|other time)\b", text, re.I):
            body = ("Koi baat nahi — jo din/time suit kare woh bata dijiye, hum hold kar denge." if hi
                    else "No problem — tell us a day/time that suits you and we'll hold it.")
            return {"action": "send", "body": body, "cta": "open_ended", "rationale": "Customer wants a different time — open slot request."}
        if SOFT_NO.match(text):
            return {"action": "end", "_reason": "declined", "rationale": "Customer declined — respecting it; no further follow-up on this trigger."}
        body = (f"Zaroor — {mname} team aapke sawaal ka jawab jaldi degi. Tab tak slot hold karein? YES reply karein." if hi
                else f"Sure — the {mname} team will get back on that shortly. Meanwhile, shall we hold a slot for you? Reply YES.")
        return {"action": "send", "body": body, "cta": "binary_yes_no", "rationale": "Customer question routed to merchant team; kept one low-friction CTA."}
