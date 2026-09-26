import re

from bot import compose
from tests.conftest import push

NOW = "2026-04-26T10:30:00Z"
ACTION_KEYS = {"conversation_id", "merchant_id", "customer_id", "send_as", "trigger_id", "template_name",
               "template_params", "body", "cta", "suppression_key", "rationale"}


# --------------------------------------------------------------------------- health / metadata

def test_healthz_empty(client):
    r = client.get("/v1/healthz")
    assert r.status_code == 200
    j = r.json()
    assert j["status"] == "ok" and isinstance(j["uptime_seconds"], int)
    assert j["contexts_loaded"] == {"category": 0, "merchant": 0, "customer": 0, "trigger": 0}


def test_metadata(client):
    j = client.get("/v1/metadata").json()
    for k in ("team_name", "team_members", "model", "approach", "contact_email", "version", "submitted_at"):
        assert k in j
    assert isinstance(j["team_members"], list)


# --------------------------------------------------------------------------- context

def test_context_versioning(client, data):
    m = data["merchants"]["m_001_drmeera_dentist_delhi"]
    r = push(client, "merchant", m["merchant_id"], m, 1)
    assert r.status_code == 200 and r.json()["accepted"] is True and r.json()["ack_id"]
    r = push(client, "merchant", m["merchant_id"], m, 1)
    assert r.status_code == 409 and r.json() == {"accepted": False, "reason": "stale_version", "current_version": 1}
    m2 = dict(m, performance=dict(m["performance"], views=2580))
    assert push(client, "merchant", m["merchant_id"], m2, 2).status_code == 200
    assert push(client, "merchant", m["merchant_id"], m, 1).status_code == 409
    from app.store import store
    assert store.get("merchant", m["merchant_id"])["performance"]["views"] == 2580


def test_context_validation(client):
    r = client.post("/v1/context", json={"scope": "planet", "context_id": "x", "version": 1, "payload": {}})
    assert r.status_code == 400 and r.json()["reason"] == "invalid_scope"
    r = client.post("/v1/context", json={"scope": "merchant", "context_id": "", "version": 1, "payload": {}})
    assert r.status_code == 400
    r = client.post("/v1/context", json={"scope": "merchant", "context_id": "m", "version": "1", "payload": {}})
    assert r.status_code == 400 and r.json()["reason"] == "invalid_version"
    r = client.post("/v1/context", json={"scope": "merchant", "context_id": "m", "version": 1, "payload": []})
    assert r.status_code == 400 and r.json()["reason"] == "invalid_payload"
    r = client.post("/v1/context", content=b"{not json", headers={"Content-Type": "application/json"})
    assert r.status_code == 400 and r.json()["reason"] == "malformed_json"


def test_warmup_counts(loaded):
    assert loaded.get("/v1/healthz").json()["contexts_loaded"] == {"category": 5, "merchant": 50, "customer": 200, "trigger": 100}


# --------------------------------------------------------------------------- tick

def _check_body(body, category):
    assert body.strip()
    assert not re.search(r"https?://|www\.", body), body  # URLs = -3 penalty (api-call-examples F.4)
    assert "None" not in body and "{" not in body and "}" not in body and "nan" not in body.split()
    for taboo in category["voice"].get("vocab_taboo", []):
        t = re.sub(r"\s*\(.*?\)", "", taboo).strip().lower()
        assert t not in body.lower(), (t, body)


def _drain(client, data, first_batches, max_ticks=40):
    """Mimic the judge harness: tick, reply to every action as the merchant/customer, keep ticking."""
    actions, ticks = [], 0
    pending_lists = list(first_batches)
    while ticks < max_ticks:
        listed = pending_lists.pop(0) if pending_lists else []
        r = client.post("/v1/tick", json={"now": NOW, "available_triggers": listed})
        assert r.status_code == 200
        acts = r.json()["actions"]
        ticks += 1
        # rule: at most one message per recipient (merchant for vera, customer for on-behalf) per tick
        recips = [(a["merchant_id"], a["customer_id"]) if a["customer_id"] else (a["merchant_id"],) for a in acts]
        assert len(recips) == len(set(recips)), recips
        for a in acts:
            role = "customer" if a["customer_id"] else "merchant"
            client.post("/v1/reply", json={"conversation_id": a["conversation_id"], "merchant_id": a["merchant_id"],
                                           "customer_id": a["customer_id"], "from_role": role,
                                           "message": f"Tell me more about the {a['trigger_id'].split('_', 2)[-1][:30]} point?", "received_at": NOW, "turn_number": 2})
        actions += acts
        if not acts and not pending_lists:
            break
    return actions, ticks


def test_tick_all_test_pairs(loaded, data):
    tids = [p["trigger_id"] for p in data["pairs"]]
    actions, ticks = _drain(loaded, data, [tids[i:i + 10] for i in range(0, len(tids), 10)])
    assert len(actions) == 30 and ticks > 3          # all 30 delivered, spread over extra ticks by the queue
    conv_ids = set()
    for a in actions:
        assert ACTION_KEYS <= set(a)
        trg = data["triggers"][a["trigger_id"]]
        m = data["merchants"][a["merchant_id"]]
        assert a["merchant_id"] == trg["merchant_id"]
        assert a["send_as"] == ("merchant_on_behalf" if trg["scope"] == "customer" else "vera")
        assert a["suppression_key"] == trg["suppression_key"]
        assert a["conversation_id"] not in conv_ids
        conv_ids.add(a["conversation_id"])
        _check_body(a["body"], data["categories"][m["category_slug"]])
        assert len(a["template_params"]) >= 2 and all(isinstance(p, str) for p in a["template_params"])
    # suppression: same triggers again -> nothing new
    r = loaded.post("/v1/tick", json={"now": NOW, "available_triggers": tids})
    assert r.json()["actions"] == []


def test_tick_skips_unknown_and_caps(loaded, data):
    r = loaded.post("/v1/tick", json={"now": NOW, "available_triggers": ["nope", 42]})
    assert r.json()["actions"] == []
    r = loaded.post("/v1/tick", json={"now": NOW, "available_triggers": list(data["triggers"])})
    acts = r.json()["actions"]
    assert 0 < len(acts) <= 20
    assert loaded.post("/v1/tick", json={"now": NOW, "available_triggers": []}).status_code == 200


def test_one_message_per_merchant_per_tick_rest_queued(loaded, data):
    m001 = ["trg_001_research_digest_dentists", "trg_022_cde_webinar_dentists", "trg_023_competitor_opened_dentist"]
    first = loaded.post("/v1/tick", json={"now": NOW, "available_triggers": m001}).json()["actions"]
    assert len(first) == 1                                     # one vera message to Dr. Meera this tick
    second = loaded.post("/v1/tick", json={"now": NOW, "available_triggers": []}).json()["actions"]
    assert len(second) == 1 and second[0]["trigger_id"] != first[0]["trigger_id"]   # queued one goes next tick


def test_customer_trigger_waits_for_customer_context(client, data):
    push(client, "category", "dentists", data["categories"]["dentists"])
    m = data["merchants"]["m_001_drmeera_dentist_delhi"]
    push(client, "merchant", m["merchant_id"], m)
    t = data["triggers"]["trg_003_recall_due_priya"]
    push(client, "trigger", t["id"], t)
    assert client.post("/v1/tick", json={"now": NOW, "available_triggers": [t["id"]]}).json()["actions"] == []
    push(client, "customer", t["customer_id"], data["customers"][t["customer_id"]])     # arrives later
    acts = client.post("/v1/tick", json={"now": NOW, "available_triggers": []}).json()["actions"]
    assert len(acts) == 1 and acts[0]["customer_id"] == t["customer_id"]


def test_same_kind_not_stacked_while_unanswered(loaded):
    base = {"scope": "merchant", "kind": "perf_dip", "merchant_id": "m_010_sunrisepharm_pharmacy_lucknow",
            "urgency": 3, "payload": {"metric": "calls", "delta_pct": -0.3}}
    push(loaded, "trigger", "dip_a", dict(base, id="dip_a", suppression_key="dip_a"))
    push(loaded, "trigger", "dip_b", dict(base, id="dip_b", suppression_key="dip_b", payload={"metric": "views", "delta_pct": -0.2}))
    a = loaded.post("/v1/tick", json={"now": NOW, "available_triggers": ["dip_a"]}).json()["actions"]
    assert len(a) == 1
    assert loaded.post("/v1/tick", json={"now": NOW, "available_triggers": ["dip_b"]}).json()["actions"] == []
    _reply(loaded, a[0]["conversation_id"], a[0]["merchant_id"], "what should I do about it?")   # merchant engages
    b = loaded.post("/v1/tick", json={"now": NOW, "available_triggers": []}).json()["actions"]
    assert len(b) == 1 and b[0]["trigger_id"] == "dip_b"


def test_tick_uses_updated_context(loaded, data):
    cat = dict(data["categories"]["dentists"])
    cat["digest"] = [dict(d) for d in cat["digest"]]
    cat["digest"][0]["title"] = "NEW INJECTED TITLE for fluoride recall"
    assert push(loaded, "category", "dentists", cat, 2).status_code == 200
    r = loaded.post("/v1/tick", json={"now": NOW, "available_triggers": ["trg_001_research_digest_dentists"]})
    assert "NEW INJECTED TITLE" in r.json()["actions"][0]["body"]


def test_customer_without_consent_not_messaged(client, data):
    push(client, "category", "pharmacies", data["categories"]["pharmacies"])
    m = data["merchants"]["m_010_sunrisepharm_pharmacy_lucknow"]
    push(client, "merchant", m["merchant_id"], m)
    c = data["customers"]["c_015_anonymous_for_m010"]
    push(client, "customer", c["customer_id"], c)
    trg = {"id": "trg_x", "scope": "customer", "kind": "chronic_refill_due", "source": "internal",
           "merchant_id": m["merchant_id"], "customer_id": c["customer_id"], "payload": {}, "urgency": 3,
           "suppression_key": "x", "expires_at": "2026-12-01T00:00:00Z"}
    push(client, "trigger", "trg_x", trg)
    assert client.post("/v1/tick", json={"now": NOW, "available_triggers": ["trg_x"]}).json()["actions"] == []


def test_compose_all_100_deterministic(data):
    for tid, t in data["triggers"].items():
        m = data["merchants"][t["merchant_id"]]
        c = data["customers"].get(t.get("customer_id")) if t.get("customer_id") else None
        cat = data["categories"][m["category_slug"]]
        a = compose(cat, m, t, c)
        b = compose(cat, m, t, c)
        assert a == b
        assert set(a) == {"body", "cta", "send_as", "suppression_key", "rationale"}
        _check_body(a["body"], cat)


def test_no_fabricated_competitor(data):
    t = data["triggers"]["trg_060_competitor_opened_m_006_southindiancafe_r"] if "trg_060_competitor_opened_m_006_southindiancafe_r" in data["triggers"] else None
    for tid, t in data["triggers"].items():
        if t["kind"] == "competitor_opened" and t["payload"].get("placeholder"):
            m = data["merchants"][t["merchant_id"]]
            body = compose(data["categories"][m["category_slug"]], m, t)["body"]
            assert "Smile Studio" not in body and " km " not in body


# --------------------------------------------------------------------------- reply / replay scenarios

def _start(loaded, tid="trg_001_research_digest_dentists"):
    a = loaded.post("/v1/tick", json={"now": NOW, "available_triggers": [tid]}).json()["actions"][0]
    return a["conversation_id"], a["merchant_id"]


def _reply(c, conv, mid, msg, turn=2, role="merchant", cid=None):
    r = c.post("/v1/reply", json={"conversation_id": conv, "merchant_id": mid, "customer_id": cid, "from_role": role,
                                  "message": msg, "received_at": NOW, "turn_number": turn})
    assert r.status_code == 200
    j = r.json()
    assert j["action"] in ("send", "wait", "end") and j.get("rationale")
    if j["action"] == "send":
        assert j["body"].strip() and j.get("cta")
    if j["action"] == "wait":
        assert isinstance(j["wait_seconds"], int) and j["wait_seconds"] > 0
    return j


def test_auto_reply_hell_same_conv(loaded):
    conv, mid = _start(loaded)
    msg = "Thank you for contacting Dr. Meera's Dental Clinic! Our team will respond shortly."
    acts = [_reply(loaded, conv, mid, msg, t)["action"] for t in range(2, 6)]
    assert acts[0] == "send" and acts[1] == "wait" and "end" in acts


def test_auto_reply_across_conversations(loaded):
    # judge_simulator uses a fresh conversation_id per turn
    acts = [_reply(loaded, f"conv_auto_{i}", "m_001_drmeera_dentist_delhi",
                   "Thank you for contacting us! Our team will respond shortly.", i + 1)["action"] for i in range(1, 5)]
    assert "end" in acts


def test_repeated_verbatim_detected_as_auto_reply(loaded):
    conv, mid = _start(loaded)
    msg = "Aapki jaankari ke liye dhanyavaad, hum jald sampark karenge"
    acts = [_reply(loaded, conv, mid, msg, t)["action"] for t in range(2, 7)]
    assert acts[0] == "send" and acts[1] == "send"      # twice could still be a person
    assert acts[2] == "send" and acts[3] == "wait" and acts[4] == "end"   # 3rd verbatim = auto-reply: nudge, wait, end


def test_intent_transition_to_action(loaded):
    conv, mid = _start(loaded)
    j = _reply(loaded, conv, mid, "Ok lets do it. Whats next?")
    assert j["action"] == "send"
    low = j["body"].lower()
    assert not any(q in low for q in ["would you", "do you", "can you tell", "what if", "how about"])
    assert any(w in low for w in ["done", "draft", "confirm", "here"])


def test_intent_hinglish_join(loaded):
    j = _reply(loaded, "conv_join", "m_002_bharat_dentist_mumbai", "Mujhe magicpin judrna hai")
    assert j["action"] == "send" and "ho gaya" in j["body"].lower()


def test_hostile_stop_ends_and_suppresses(loaded):
    conv, mid = _start(loaded)
    assert _reply(loaded, conv, mid, "Stop messaging me. This is useless spam.")["action"] == "end"
    r = loaded.post("/v1/tick", json={"now": NOW, "available_triggers": ["trg_002_compliance_dci_radiograph"]})
    assert r.json()["actions"] == []


def test_hostile_then_offtopic(loaded):
    conv, mid = _start(loaded)
    j1 = _reply(loaded, conv, mid, "You people are useless idiots", 2)
    assert j1["action"] == "send" and "sorry" in j1["body"].lower()
    j2 = _reply(loaded, conv, mid, "Can you also help me file my GST?", 3)
    assert j2["action"] == "send" and "ca" in j2["body"].lower() and j2["body"] != j1["body"]


def test_offtopic_redirects(loaded):
    conv, mid = _start(loaded)
    j = _reply(loaded, conv, mid, "Btw can you also help me with my GST filing this month?")
    assert j["action"] == "send" and "outside" in j["body"].lower()


def test_later_waits_and_not_interested_ends(loaded):
    conv, mid = _start(loaded)
    assert _reply(loaded, conv, mid, "Busy right now, message me tomorrow")["action"] == "wait"
    assert _reply(loaded, conv, mid, "Not interested.", 3)["action"] == "end"


def test_soft_no_twice_ends(loaded):
    conv, mid = _start(loaded)
    assert _reply(loaded, conv, mid, "no")["action"] == "send"
    assert _reply(loaded, conv, mid, "no", 3)["action"] == "end"


def test_full_happy_path_no_repeats(loaded):
    conv, mid = _start(loaded)
    bodies = []
    for t, msg in enumerate(["Yes please send the abstract. Also draft the patient WhatsApp.", "Looks good, change the tone a bit", "yes", "thanks"], 2):
        j = _reply(loaded, conv, mid, msg, t)
        if j["action"] == "send":
            bodies.append(j["body"])
    assert len(bodies) == len(set(bodies)) and len(bodies) >= 2


def test_curious_ask_reciprocates(loaded):
    conv, mid = _start(loaded, "trg_008_curious_ask_studio11")
    j = _reply(loaded, conv, mid, "Balayage and keratin mostly")
    assert j["action"] == "send" and "Balayage and keratin mostly" in j["body"]


def test_customer_slot_pick(loaded):
    a = loaded.post("/v1/tick", json={"now": NOW, "available_triggers": ["trg_003_recall_due_priya"]}).json()["actions"][0]
    assert a["send_as"] == "merchant_on_behalf" and "Priya" in a["body"]
    j = _reply(loaded, a["conversation_id"], a["merchant_id"], "2", role="customer", cid=a["customer_id"])
    assert j["action"] == "send" and "Thu 6 Nov, 5pm" in j["body"]


def test_reply_validation(client):
    assert client.post("/v1/reply", json={"message": "hi"}).status_code == 400
    assert client.post("/v1/reply", content=b"xx", headers={"Content-Type": "application/json"}).status_code == 400
    j = client.post("/v1/reply", json={"conversation_id": "c", "merchant_id": "unknown", "from_role": "merchant",
                                       "message": "hello", "turn_number": 2}).json()
    assert j["action"] in ("send", "wait", "end")


def test_teardown(loaded):
    assert loaded.post("/v1/teardown").json()["ok"] is True
    assert loaded.get("/v1/healthz").json()["contexts_loaded"]["merchant"] == 0


def test_one_driving_signal_per_message(data):
    """Site judging guidance: choose the one signal that drives the message, don't stack every fact."""
    t = data["triggers"]["trg_004_perf_dip_bharat"]
    m = data["merchants"][t["merchant_id"]]
    body = compose(data["categories"][m["category_slug"]], m, t)["body"]
    assert body.count("gap") == 1 and "renews" not in body and "CTR" not in body
    assert "no active offer" in body and "Dental Cleaning @ ₹299" in body


def test_default_is_deterministic_rules_only(client):
    import os
    assert os.getenv("LLM_PROVIDER", "none") == "none"
    assert "no LLM" in client.get("/v1/metadata").json()["model"]
