import glob
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
# the original challenge folder is the source of truth; `challenge/` is an older extracted copy
CHALLENGE = next((p for p in (ROOT / "magicpin-ai-challenge", ROOT / "challenge") if p.exists()), ROOT / "magicpin-ai-challenge")
sys.path.insert(0, str(ROOT))

from app.main import app  # noqa: E402
from app.store import store  # noqa: E402

DATASET = Path(os.getenv("DATASET_DIR", CHALLENGE / "dataset"))
EXPANDED = DATASET / "expanded"


def _load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture(scope="session")
def data():
    if not (EXPANDED / "test_pairs.json").exists():
        env = dict(os.environ, PYTHONUTF8="1")
        subprocess.run([sys.executable, "generate_dataset.py", "--out", "./expanded"], cwd=DATASET, check=True, env=env)
    return {
        "categories": {d["slug"]: d for d in map(_load, glob.glob(str(EXPANDED / "categories" / "*.json")))},
        "merchants": {d["merchant_id"]: d for d in map(_load, glob.glob(str(EXPANDED / "merchants" / "*.json")))},
        "customers": {d["customer_id"]: d for d in map(_load, glob.glob(str(EXPANDED / "customers" / "*.json")))},
        "triggers": {d["id"]: d for d in map(_load, glob.glob(str(EXPANDED / "triggers" / "*.json")))},
        "pairs": _load(EXPANDED / "test_pairs.json")["pairs"],
    }


@pytest.fixture()
def client():
    store.reset()
    with TestClient(app) as c:
        yield c
    store.reset()


def push(client, scope, cid, payload, version=1):
    return client.post("/v1/context", json={"scope": scope, "context_id": cid, "version": version,
                                            "payload": payload, "delivered_at": "2026-04-26T10:00:00Z"})


@pytest.fixture()
def loaded(client, data):
    for slug, c in data["categories"].items():
        assert push(client, "category", slug, c).status_code == 200
    for mid, m in data["merchants"].items():
        assert push(client, "merchant", mid, m).status_code == 200
    for cid, c in data["customers"].items():
        assert push(client, "customer", cid, c).status_code == 200
    for tid, t in data["triggers"].items():
        assert push(client, "trigger", tid, t).status_code == 200
    return client
