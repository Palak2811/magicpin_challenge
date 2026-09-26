"""Push the expanded dataset into a running bot (like the judge's warmup), then run one demo tick.

Usage:  python scripts/load_dataset.py [--url http://127.0.0.1:8080] [--no-triggers]
"""
import argparse
import glob
import json
import os
import subprocess
import sys
from pathlib import Path
from urllib import request

ROOT = Path(__file__).resolve().parents[1]
# the original challenge folder is the source of truth; `challenge/` is an older extracted copy
CHALLENGE = next((p for p in (ROOT / "magicpin-ai-challenge", ROOT / "challenge") if p.exists()), ROOT / "magicpin-ai-challenge")
DS = CHALLENGE / "dataset"


def post(url, body):
    req = request.Request(url, data=json.dumps(body).encode("utf-8"), method="POST",
                          headers={"Content-Type": "application/json"})
    try:
        with request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8"))
    except request.HTTPError as e:  # 409 = already loaded (same version)
        return json.loads(e.read().decode("utf-8"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8080")
    ap.add_argument("--no-triggers", action="store_true")
    a = ap.parse_args()
    if not (DS / "expanded" / "test_pairs.json").exists():
        subprocess.run([sys.executable, "generate_dataset.py", "--out", "./expanded"], cwd=DS, check=True,
                       env=dict(os.environ, PYTHONUTF8="1"))
    scopes = [("category", "categories", "slug"), ("merchant", "merchants", "merchant_id"),
              ("customer", "customers", "customer_id")] + ([] if a.no_triggers else [("trigger", "triggers", "id")])
    for scope, folder, key in scopes:
        n = 0
        for f in sorted(glob.glob(str(DS / "expanded" / folder / "*.json"))):
            payload = json.load(open(f, encoding="utf-8"))
            post(f"{a.url}/v1/context", {"scope": scope, "context_id": payload[key], "version": 1,
                                         "payload": payload, "delivered_at": "2026-04-26T10:00:00Z"})
            n += 1
        print(f"pushed {n} {scope} contexts")
    with request.urlopen(f"{a.url}/v1/healthz") as r:
        print("healthz:", r.read().decode())
    if not a.no_triggers:
        pairs = json.load(open(DS / "expanded" / "test_pairs.json"))["pairs"][:3]
        res = post(f"{a.url}/v1/tick", {"now": "2026-04-26T10:30:00Z",
                                        "available_triggers": [p["trigger_id"] for p in pairs]})
        print("\nDemo tick:")
        for act in res.get("actions", []):
            print(f"\n[{act['conversation_id']}]\n{act['body']}")


if __name__ == "__main__":
    main()
