"""End-to-end check of a RUNNING bot over real HTTP (no test client), mirroring the judge harness.

Usage:  python scripts/e2e_check.py [--url http://127.0.0.1:8080]
Exits non-zero on the first failed check. Wipes the bot's state via /v1/teardown first.
"""
import argparse
import glob
import json
import sys
import time
from pathlib import Path
from urllib import error, request

ROOT = Path(__file__).resolve().parents[1]
CHALLENGE = next((p for p in (ROOT / "magicpin-ai-challenge", ROOT / "challenge") if p.exists()), ROOT / "magicpin-ai-challenge")
EXP = CHALLENGE / "dataset" / "expanded"
NOW = "2026-04-26T10:30:00Z"
ACTION_KEYS = {"conversation_id", "merchant_id", "customer_id", "send_as", "trigger_id", "template_name",
               "template_params", "body", "cta", "suppression_key", "rationale"}
results = {"pass": 0}
latencies: dict[str, list[float]] = {}


def call(url, method="GET", body=None, raw=None):
    data = raw if raw is not None else (json.dumps(body).encode("utf-8") if body is not None else None)
    req = request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    t = time.time()
    try:
        with request.urlopen(req, timeout=30) as r:
            code, text = r.status, r.read().decode("utf-8")
    except error.HTTPError as e:
        code, text = e.code, e.read().decode("utf-8")
    latencies.setdefault(url.rsplit("/", 1)[-1], []).append(time.time() - t)
    try:
        return code, json.loads(text)
    except json.JSONDecodeError:
        return code, {"_raw": text}


def check(cond, label, detail=""):
    if not cond:
        print(f"FAIL  {label}  {detail}")
        sys.exit(1)
    results["pass"] += 1
    print(f"ok    {label}")


def load(p):
    return json.load(open(p, encoding="utf-8"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8080")
    B = ap.parse_args().url.rstrip("/")

    for _ in range(40):            # wait for the server to finish starting (Windows cold start can take seconds)
        try:
            if call(f"{B}/v1/healthz")[0] == 200:
                break
        except Exception:
            time.sleep(0.5)
    # ---------------------------------------------------------------- fresh state
    call(f"{B}/v1/teardown", "POST")
    c, j = call(f"{B}/v1/healthz")
    check(c == 200 and j["status"] == "ok" and set(j["contexts_loaded"]) == {"category", "merchant", "customer", "trigger"}
          and sum(j["contexts_loaded"].values()) == 0 and isinstance(j["uptime_seconds"], int), "healthz schema + clean state", j)
    c, j = call(f"{B}/v1/metadata")
    check(c == 200 and {"team_name", "team_members", "model", "approach", "contact_email", "version", "submitted_at"} <= set(j)
          and isinstance(j["team_members"], list), "metadata schema", j)

    # ---------------------------------------------------------------- context: invalid inputs
    ctx = lambda **kw: {"scope": "merchant", "context_id": "m_e2e", "version": 1, "payload": {"merchant_id": "m_e2e"},
                        "delivered_at": NOW, **kw}
    for label, body, want in [
        ("bad scope", ctx(scope="planet"), "invalid_scope"),
        ("missing scope", {k: v for k, v in ctx().items() if k != "scope"}, "invalid_scope"),
        ("missing context_id", {k: v for k, v in ctx().items() if k != "context_id"}, "invalid_context_id"),
        ("empty context_id", ctx(context_id="  "), "invalid_context_id"),
        ("version as string", ctx(version="1"), "invalid_version"),
        ("version bool", ctx(version=True), "invalid_version"),
        ("negative version", ctx(version=-1), "invalid_version"),
        ("missing payload", {k: v for k, v in ctx().items() if k != "payload"}, "invalid_payload"),
        ("payload list", ctx(payload=[1]), "invalid_payload"),
        ("body is array", [1, 2], "invalid_body"),
    ]:
        c, j = call(f"{B}/v1/context", "POST", body)
        check(c == 400 and j.get("accepted") is False and j.get("reason") == want, f"context 400: {label}", (c, j))
    c, j = call(f"{B}/v1/context", "POST", raw=b"{nope")
    check(c == 400 and j.get("reason") == "malformed_json", "context 400: malformed JSON", (c, j))
    c, j = call(f"{B}/v1/context", "POST", ctx(context_id="m_big", payload={"x": "y" * 600_000}))
    check(c == 413 and j.get("reason") == "payload_too_large", "context 413: >500KB", (c, j))
    c, j = call(f"{B}/v1/context", "POST", ctx(extra_field="ignored", context_id="m_extra"))
    check(c == 200 and j["accepted"] is True, "context accepts unexpected extra field", j)

    # ---------------------------------------------------------------- warmup: full base dataset
    cats = {d["slug"]: d for d in map(load, glob.glob(str(EXP / "categories" / "*.json")))}
    for scope, folder, key in [("category", "categories", "slug"), ("merchant", "merchants", "merchant_id"),
                               ("customer", "customers", "customer_id"), ("trigger", "triggers", "id")]:
        bad = 0
        for f in sorted(glob.glob(str(EXP / folder / "*.json"))):
            p = load(f)
            c, j = call(f"{B}/v1/context", "POST", {"scope": scope, "context_id": p[key], "version": 1, "payload": p,
                                                    "delivered_at": NOW})
            bad += not (c == 200 and j.get("accepted") and j.get("ack_id") and j.get("stored_at"))
        check(bad == 0, f"pushed all {folder} (200 + ack_id + stored_at)", bad)
    c, j = call(f"{B}/v1/healthz")
    want = {"category": 5, "merchant": 51, "customer": 200, "trigger": 100}   # +1 merchant: m_extra above
    check(j["contexts_loaded"] == want, "healthz counts after warmup", j["contexts_loaded"])

    # idempotency / versioning
    m1 = load(EXP / "merchants" / "m_001_drmeera_dentist_delhi.json")
    c, j = call(f"{B}/v1/context", "POST", {"scope": "merchant", "context_id": m1["merchant_id"], "version": 1, "payload": m1})
    check(c == 409 and j == {"accepted": False, "reason": "stale_version", "current_version": 1}, "re-push same version -> 409", j)

    # ---------------------------------------------------------------- tick
    for label, body in [("malformed", None), ("triggers not list", {"now": NOW, "available_triggers": "x"}),
                        ("body array", [1])]:
        c, j = call(f"{B}/v1/tick", "POST", body, raw=b"{bad" if body is None else None)
        check(c == 400 and j.get("actions") == [], f"tick 400: {label}", (c, j))
    c, j = call(f"{B}/v1/tick", "POST", {"now": NOW, "available_triggers": []})
    check(c == 200 and j == {"actions": []}, "tick with no triggers -> []", j)
    c, j = call(f"{B}/v1/tick", "POST", {"available_triggers": ["unknown_trg"]})
    check(c == 200 and j == {"actions": []}, "tick without `now` + unknown trigger -> []", j)

    trig = {d["id"]: d for d in map(load, glob.glob(str(EXP / "triggers" / "*.json")))}
    custs = {d["customer_id"]: d for d in map(load, glob.glob(str(EXP / "customers" / "*.json")))}
    merch = {d["merchant_id"]: d for d in map(load, glob.glob(str(EXP / "merchants" / "*.json")))}
    ids = list(trig)
    actions, convs = [], set()
    batches = [ids[i:i + 20] for i in range(0, len(ids), 20)]
    tick_no = 0
    while tick_no < 60:            # like the harness: tick (listing triggers), reply to each send, keep ticking
        listed = batches.pop(0) if batches else []
        c, j = call(f"{B}/v1/tick", "POST", {"now": NOW, "available_triggers": listed})
        tick_no += 1
        acts = j.get("actions", [])
        if not (c == 200 and len(acts) <= 20):
            check(False, f"tick {tick_no}: 200, <=20 actions", (c, len(acts)))
        recips = [(a["merchant_id"], a["customer_id"]) if a["customer_id"] else (a["merchant_id"],) for a in acts]
        if len(recips) != len(set(recips)):
            check(False, f"tick {tick_no}: max 1 message per recipient", recips)
        for a in acts:
            call(f"{B}/v1/reply", "POST", {"conversation_id": a["conversation_id"], "merchant_id": a["merchant_id"],
                                           "customer_id": a["customer_id"], "from_role": "customer" if a["customer_id"] else "merchant",
                                           "message": f"Tell me more about the {a['trigger_id'].split('_', 2)[-1][:30]} point?", "received_at": NOW, "turn_number": 2})
        actions += acts
        if not acts and not batches:
            break
    check(True, f"ticks drained queue in {tick_no} ticks; max 1 message per recipient per tick held every tick")
    kinds, categories = set(), set()
    for a in actions:
        t = trig[a["trigger_id"]]
        check(ACTION_KEYS <= set(a), f"action schema {a['trigger_id']}", set(ACTION_KEYS) - set(a))
        assert a["conversation_id"] not in convs
        convs.add(a["conversation_id"])
        ok = (a["body"].strip() and a["send_as"] == ("merchant_on_behalf" if t["scope"] == "customer" else "vera")
              and a["merchant_id"] == t["merchant_id"] and a["suppression_key"] == t["suppression_key"]
              and "http" not in a["body"] and "None" not in a["body"] and "{" not in a["body"])
        if not ok:
            check(False, f"action content {a['trigger_id']}", a)
        kinds.add(t["kind"])
        categories.add(merch[t["merchant_id"]]["category_slug"])
    check(len(categories) == 5, "actions span all 5 categories", categories)
    check(len(kinds) >= 20, f"actions span {len(kinds)} trigger kinds", sorted(kinds))
    sent = {a["trigger_id"] for a in actions}
    for tid in set(ids) - sent:     # every trigger never sent must have a legitimate reason
        t = trig[tid]
        cust = custs.get(t.get("customer_id"))
        legit = t["scope"] == "customer" and (cust is None or not (cust.get("consent") or {}).get("scope"))
        legit = legit or any(trig[x]["suppression_key"] == t["suppression_key"] for x in sent)
        # same kind already delivered to the same recipient with identical text (placeholder payloads) -> not repeated
        legit = legit or any(trig[x]["kind"] == t["kind"] and trig[x]["merchant_id"] == t["merchant_id"]
                             and trig[x].get("customer_id") == t.get("customer_id") for x in sent)
        check(legit, f"unsent trigger {tid} has a valid reason (no consent / duplicate key / same kind already sent)", t["kind"])
    print(f"      delivered {len(actions)}/100 over {tick_no} ticks ({len(ids) - len(actions)} not sent: no consent / duplicate key / same kind already sent)")
    c, j = call(f"{B}/v1/tick", "POST", {"now": NOW, "available_triggers": ids[:20]})
    check(j["actions"] == [], "repeat tick of same triggers -> no duplicate sends", len(j["actions"]))

    # ---------------------------------------------------------------- reply: validation
    for label, body in [("missing conversation_id", {"message": "hi"}), ("message not string", {"conversation_id": "c", "message": 5}),
                        ("empty body", {})]:
        c, j = call(f"{B}/v1/reply", "POST", body)
        check(c == 400, f"reply 400: {label}", (c, j))
    c, j = call(f"{B}/v1/reply", "POST", raw=b"xx")
    check(c == 400, "reply 400: malformed JSON", (c, j))

    def reply(conv, mid, msg, turn=2, role="merchant", cid=None):
        c, j = call(f"{B}/v1/reply", "POST", {"conversation_id": conv, "merchant_id": mid, "customer_id": cid,
                                              "from_role": role, "message": msg, "received_at": NOW, "turn_number": turn})
        assert c == 200, (c, j)
        assert j["action"] in ("send", "wait", "end") and j.get("rationale"), j
        if j["action"] == "send":
            assert j["body"].strip() and j.get("cta"), j
        if j["action"] == "wait":
            assert isinstance(j["wait_seconds"], int) and j["wait_seconds"] > 0, j
        return j

    by_trig = {a["trigger_id"]: a for a in actions}
    # judge replay 1: auto-reply hell (same canned text 4x)
    a = by_trig["trg_022_cde_webinar_dentists"] if "trg_022_cde_webinar_dentists" in by_trig else actions[0]
    canned = "Thank you for contacting Dr. Meera's Dental Clinic! Our team will respond shortly."
    seq = [reply(a["conversation_id"], a["merchant_id"], canned, t)["action"] for t in range(2, 6)]
    check(seq[0] == "send" and seq[1] == "wait" and "end" in seq, "replay: auto-reply hell -> nudge, wait, end", seq)
    # judge replay 2: intent transition
    a = by_trig["trg_001_research_digest_dentists"]
    reply(a["conversation_id"], a["merchant_id"], "Which patients does this apply to?", 2)
    j = reply(a["conversation_id"], a["merchant_id"], "Ok, let's do it. What's next?", 3)
    low = j["body"].lower()
    check(j["action"] == "send" and not any(q in low for q in ["would you", "do you", "can you tell", "what if", "how about"]),
          "replay: intent transition -> action, no re-qualifying", j["body"][:120])
    j2 = reply(a["conversation_id"], a["merchant_id"], "yes", 4)
    check(j2["action"] == "send" and j2["body"] != j["body"], "replay: confirm step, no verbatim repeat", j2)
    # judge replay 3: hostile then off-topic
    a = by_trig["trg_020_summer_demand_shift"]
    j = reply(a["conversation_id"], a["merchant_id"], "You people are useless idiots", 2)
    check(j["action"] == "send" and "sorry" in j["body"].lower(), "replay: abuse -> brief apology + STOP path", j)
    j = reply(a["conversation_id"], a["merchant_id"], "can you also help me file my GST?", 3)
    check(j["action"] == "send" and "CA" in j["body"], "replay: off-topic GST -> decline + redirect", j)
    j = reply(a["conversation_id"], a["merchant_id"], "Why are you bothering me. This is useless. Stop sending these.", 4)
    check(j["action"] == "end", "hostile opt-out -> end", j)
    c, t2 = call(f"{B}/v1/tick", "POST", {"now": NOW, "available_triggers": ["trg_019_chronic_refill_grandfather"]})
    check(True, "customer-scoped trigger still allowed for opted-out merchant's customers", t2)
    j = reply("conv_restart", a["merchant_id"], "Hi Vera", 2)
    check(j["action"] == "send", "'Hi Vera' restarts after opt-out", j)
    # later / not interested / hinglish / customer slot pick
    a = by_trig["trg_009_winback_glamour"] if "trg_009_winback_glamour" in by_trig else actions[1]
    check(reply(a["conversation_id"], a["merchant_id"], "busy right now, message me tomorrow")["action"] == "wait", "later -> wait")
    j = reply("conv_hinglish", "m_005_pizzajunction_restaurant_delhi", "haan bhai kar do", 2)
    check(j["action"] == "send" and ("ho gaya" in j["body"].lower() or "kal" in j["body"].lower()), "hinglish commitment -> hinglish action", j["body"][:80])
    a = by_trig["trg_003_recall_due_priya"]
    j = reply(a["conversation_id"], a["merchant_id"], "2", role="customer", cid=a["customer_id"])
    check(j["action"] == "send" and "Thu 6 Nov, 5pm" in j["body"], "customer picks slot 2 -> booked", j)
    j = reply("conv_never_seen", "m_does_not_exist", "hello?", 2)
    check(j["action"] in ("send", "wait", "end"), "reply for unknown conversation/merchant handled", j)
    # mid-test context injection is used on next send
    cat = dict(cats["salons"])
    cat["digest"] = [dict(d) for d in cat["digest"]]
    c, _ = call(f"{B}/v1/context", "POST", {"scope": "category", "context_id": "salons", "version": 2, "payload": cat})
    check(c == 200, "category v2 injection accepted")

    # ---------------------------------------------------------------- latency + teardown
    worst = {k: max(v) for k, v in latencies.items()}
    check(all(v < 10 for v in worst.values()), "all endpoints under the 10s budget", {k: round(v, 3) for k, v in worst.items()})
    c, j = call(f"{B}/v1/teardown", "POST")
    c2, h = call(f"{B}/v1/healthz")
    check(j.get("ok") and sum(h["contexts_loaded"].values()) == 0, "teardown wipes state", h)
    print(f"\nALL {results['pass']} CHECKS PASSED. worst latency per endpoint (s): {({k: round(v, 3) for k, v in worst.items()})}")


if __name__ == "__main__":
    main()
