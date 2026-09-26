import pytest

from app import llm
from tests.conftest import push
from tests.test_api import NOW, _reply, _start


@pytest.fixture()
def fake_llm():
    calls = []

    def install(fn):
        def wrapped(prompt, system):
            calls.append(prompt)
            return fn(prompt, system)
        llm._override = wrapped
        llm.clear_cache()
        return calls
    yield install
    llm._override = None
    llm.clear_cache()


def _draft_of(prompt):
    return prompt.split("DRAFT:\n", 1)[1]


# --------------------------------------------------------------------------- validator

def test_validator_rules():
    d = "Dr. Meera, 38% lower recurrence (JIDA p.14), ₹299 cleaning. Want the abstract? Reply YES."
    ok = "Dr. Meera — JIDA p.14 shows 38% lower recurrence; your ₹299 cleaning fits. Want the abstract? Reply YES."
    assert llm.validate(d, ok, []) == ok
    assert llm.validate(d, ok.replace("38%", "45%"), []) is None            # changed number
    assert llm.validate(d, ok + " Only 5 slots left!", []) is None          # invented number
    assert llm.validate(d, ok.replace("Reply YES.", ""), []) is None        # CTA dropped
    assert llm.validate(d, ok + " https://x.co", []) is None                # URL
    assert llm.validate(d, ok.replace("fits", "is guaranteed"), ["guaranteed"]) is None  # taboo
    assert llm.validate(d, "", []) is None


# --------------------------------------------------------------------------- integration

def test_disabled_by_default(loaded):
    assert not llm.enabled()
    assert "no LLM" in loaded.get("/v1/metadata").json()["model"]


def test_tick_uses_valid_polish(loaded, fake_llm):
    calls = fake_llm(lambda p, s: "Quick one: " + _draft_of(p))
    a = loaded.post("/v1/tick", json={"now": NOW, "available_triggers": ["trg_001_research_digest_dentists"]}).json()["actions"][0]
    assert calls and a["body"].startswith("Quick one: ") and "polished by LLM" in a["rationale"]
    # follow-up in same conversation never repeats the polished opener verbatim
    j = _reply(loaded, a["conversation_id"], a["merchant_id"], "Ok lets do it")
    assert j["action"] == "send" and j["body"] != a["body"]


def test_tick_rejects_fabricating_llm(loaded, fake_llm):
    fake_llm(lambda p, s: _draft_of(p) + " 97% of dentists agree.")
    a = loaded.post("/v1/tick", json={"now": NOW, "available_triggers": ["trg_001_research_digest_dentists"]}).json()["actions"][0]
    assert "97%" not in a["body"] and "polished" not in a["rationale"]


def test_llm_failure_falls_back(loaded, fake_llm):
    def boom(p, s):
        raise TimeoutError("slow")
    fake_llm(boom)
    a = loaded.post("/v1/tick", json={"now": NOW, "available_triggers": ["trg_001_research_digest_dentists"]}).json()["actions"][0]
    assert a["body"].startswith("Dr. Meera")
    conv, mid = a["conversation_id"], a["merchant_id"]
    assert _reply(loaded, conv, mid, "Ok lets do it")["action"] == "send"


def test_polish_is_deterministic(fake_llm):
    fake_llm(lambda p, s: "Hey: " + _draft_of(p))
    a = llm.polish("Draft 12 things. Reply YES.", kind="x")
    b = llm.polish("Draft 12 things. Reply YES.", kind="x")
    assert a == b == ("Hey: Draft 12 things. Reply YES.", True)


# --------------------------------------------------------------------------- cadence / restart / limits

def test_stop_after_three_unanswered_nudge_rounds(loaded):
    low = ["trg_001_research_digest_dentists", "trg_022_cde_webinar_dentists", "trg_023_competitor_opened_dentist"]
    for t in low:
        assert len(loaded.post("/v1/tick", json={"now": NOW, "available_triggers": [t]}).json()["actions"]) == 1
    # 4th low-urgency nudge to the same silent merchant is held back ...
    extra = {"id": "trg_extra", "scope": "merchant", "kind": "curious_ask_due", "source": "internal",
             "merchant_id": "m_001_drmeera_dentist_delhi", "payload": {}, "urgency": 1,
             "suppression_key": "extra", "expires_at": "2026-12-01T00:00:00Z"}
    push(loaded, "trigger", "trg_extra", extra)
    assert loaded.post("/v1/tick", json={"now": NOW, "available_triggers": ["trg_extra"]}).json()["actions"] == []
    # ... but urgent compliance still goes out
    assert len(loaded.post("/v1/tick", json={"now": NOW, "available_triggers": ["trg_002_compliance_dci_radiograph"]}).json()["actions"]) == 1


def test_reply_resets_nudge_counter(loaded):
    conv, mid = _start(loaded)
    _start(loaded, "trg_022_cde_webinar_dentists")
    _start(loaded, "trg_023_competitor_opened_dentist")
    _reply(loaded, conv, mid, "Tell me more about the trial size?")
    extra = {"id": "trg_extra2", "scope": "merchant", "kind": "curious_ask_due", "merchant_id": mid, "payload": {},
             "urgency": 1, "suppression_key": "extra2"}
    push(loaded, "trigger", "trg_extra2", extra)
    assert len(loaded.post("/v1/tick", json={"now": NOW, "available_triggers": ["trg_extra2"]}).json()["actions"]) == 1


def test_hi_vera_restarts_after_optout(loaded):
    conv, mid = _start(loaded)
    assert _reply(loaded, conv, mid, "Why are you bothering me. This is useless. Stop sending these.")["action"] == "end"
    j = _reply(loaded, "conv_restart", mid, "Hi Vera", 2)
    assert j["action"] == "send" and ("welcome back" in j["body"].lower() or "wapas swagat" in j["body"].lower())
    assert len(loaded.post("/v1/tick", json={"now": NOW, "available_triggers": ["trg_002_compliance_dci_radiograph"]}).json()["actions"]) == 1


def test_context_payload_cap(client):
    big = {"scope": "merchant", "context_id": "m_big", "version": 1, "payload": {"blob": "x" * 600_000}}
    r = client.post("/v1/context", json=big)
    assert r.status_code == 413 and r.json()["reason"] == "payload_too_large"


def test_ended_conversation_stays_closed(loaded):
    conv, mid = _start(loaded)
    assert _reply(loaded, conv, mid, "Not interested. Stop messaging me.")["action"] == "end"
    # api-call-examples §2.6: no further messages on this conversation_id
    for t, msg in enumerate(["ok fine what is it?", "yes", "tell me more"], 3):
        assert _reply(loaded, conv, mid, msg, t)["action"] == "end"
    # ... unless the merchant explicitly restarts
    assert _reply(loaded, conv, mid, "Hi Vera", 6)["action"] == "send"


def test_auto_reply_closed_thread_reopens_for_real_human(loaded):
    conv, mid = _start(loaded)
    canned = "Thank you for contacting us! Our team will respond shortly."
    acts = [_reply(loaded, conv, mid, canned, t)["action"] for t in range(2, 5)]
    assert acts[-1] == "end"
    assert _reply(loaded, conv, mid, canned, 5)["action"] == "end"          # still the bot -> stays closed
    j = _reply(loaded, conv, mid, "Sorry, owner here. Yes send the abstract please", 6)
    assert j["action"] == "send"                                             # a real person -> resume


def test_new_trigger_kinds_use_payload_facts(loaded):
    trgs = {
        "trg_heat": {"id": "trg_heat", "scope": "merchant", "kind": "weather_heatwave", "merchant_id": "m_005_pizzajunction_restaurant_delhi",
                     "payload": {"temperature_c": 42, "city": "Delhi"}, "urgency": 3, "suppression_key": "heat"},
        "trg_news": {"id": "trg_news", "scope": "merchant", "kind": "local_news_event", "merchant_id": "m_007_powerhouse_gym_bangalore",
                     "payload": {"headline": "Outer Ring Road closed near HSR", "duration_hours": 3}, "urgency": 3, "suppression_key": "news"},
        "trg_new": {"id": "trg_new", "scope": "merchant", "kind": "totally_new_kind", "merchant_id": "m_009_apollo_pharmacy_jaipur",
                    "payload": {"query": "generic medicine", "delta_pct": 0.34}, "urgency": 2, "suppression_key": "new"},
        "trg_slot": {"id": "trg_slot", "scope": "customer", "kind": "unplanned_slot_open", "merchant_id": "m_001_drmeera_dentist_delhi",
                     "customer_id": "c_001_priya_for_m001", "payload": {"available_slots": [{"label": "Fri 7 Nov, 6:30pm"}]},
                     "urgency": 2, "suppression_key": "slot"},
    }
    for tid, t in trgs.items():
        push(loaded, "trigger", tid, t)
    acts = {a["trigger_id"]: a for a in loaded.post("/v1/tick", json={"now": NOW, "available_triggers": list(trgs)}).json()["actions"]}
    assert "42°C" in acts["trg_heat"]["body"]
    assert "Outer Ring Road closed near HSR" in acts["trg_news"]["body"]
    assert "generic medicine" in acts["trg_new"]["body"] and "+34%" in acts["trg_new"]["body"]
    assert acts["trg_slot"]["send_as"] == "merchant_on_behalf" and "Fri 7 Nov, 6:30pm" in acts["trg_slot"]["body"]


def test_price_answer_uses_only_context_figures(loaded):
    # merchant with priced live offer -> quotes the real offer
    j = _reply(loaded, "conv_price1", "m_003_studio11_salon_hyderabad", "What is the price for this?")
    assert "Haircut @ ₹99" in j["body"] and "no separate charge" not in j["body"].lower()
    # merchant with no priced data -> says it doesn't know, invents nothing
    j = _reply(loaded, "conv_price2", "m_010_sunrisepharm_pharmacy_lucknow", "how much does this cost?")
    assert "₹" not in j["body"] and ("don't have an exact price" in j["body"] or "pricing mere paas" in j["body"])
    # renewal trigger -> renewal amount from the payload
    a = loaded.post("/v1/tick", json={"now": NOW, "available_triggers": ["trg_005_renewal_due_bharat"]}).json()["actions"][0]
    j = _reply(loaded, a["conversation_id"], a["merchant_id"], "What is the price?")
    assert "₹4,999" in j["body"]


def test_turn_cap_six_bot_messages(loaded):
    conv, mid = _start(loaded)
    msgs = ["what is this about?", "and how does it work?", "which patients exactly?", "why now though?",
            "is it expensive?", "ok and then?", "tell me more?", "hmm?"]
    acts = [_reply(loaded, conv, mid, m, t)["action"] for t, m in enumerate(msgs, 2)]
    from app.store import store
    assert len(store.conversations[conv].bot_bodies) <= 6 and acts[-1] == "end"


def test_regional_greetings(data):
    from bot import compose
    zen = data["merchants"]["m_008_zenyoga_gym_chennai"]                 # languages en, ta, hi -> Tamil first
    cat = data["categories"]["gyms"]
    body = compose(cat, zen, data["triggers"]["trg_024_perf_spike_zen"])["body"]
    assert body.startswith("Vanakkam Padma")
    sumitra = data["customers"]["c_011_sumitra_for_m008"]                # language_pref ta-en mix
    trg = {"id": "t", "scope": "customer", "kind": "customer_lapsed_soft", "merchant_id": zen["merchant_id"],
           "customer_id": sumitra["customer_id"], "payload": {}, "suppression_key": "t"}
    assert compose(cat, zen, trg, sumitra)["body"].startswith("Vanakkam Sumitra")
    meera = data["merchants"]["m_001_drmeera_dentist_delhi"]             # Hindi-first -> no regional greeting
    b = compose(data["categories"]["dentists"], meera, data["triggers"]["trg_001_research_digest_dentists"])["body"]
    assert b.startswith("Dr. Meera")
