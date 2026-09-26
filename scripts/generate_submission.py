"""Produce submission.jsonl (challenge-brief.md §7.2) for the 30 canonical test pairs.

Usage:  python scripts/generate_submission.py [--dataset challenge/dataset/expanded] [--out submission.jsonl]
Runs the dataset generator first if the expanded dataset is missing.
Always pure-deterministic (LLM polish disabled): the brief requires identical output for identical inputs, and a
rate-limited LLM would make the file depend on API quota. Pass --with-llm to polish anyway.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# the original challenge folder is the source of truth; `challenge/` is an older extracted copy
CHALLENGE = next((p for p in (ROOT / "magicpin-ai-challenge", ROOT / "challenge") if p.exists()), ROOT / "magicpin-ai-challenge")
sys.path.insert(0, str(ROOT))

from bot import compose  # noqa: E402


def load(p):
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", default=str(CHALLENGE / "dataset" / "expanded"))
    ap.add_argument("--out", default=str(ROOT / "submission.jsonl"))
    ap.add_argument("--with-llm", action="store_true", help="apply the optional LLM polish (non-reproducible under quota)")
    args = ap.parse_args()
    if not args.with_llm:
        os.environ["LLM_PROVIDER"] = "none"
    ds = Path(args.dataset)
    if not (ds / "test_pairs.json").exists():
        subprocess.run([sys.executable, "generate_dataset.py", "--out", str(ds)], cwd=ds.parent, check=True,
                       env=dict(os.environ, PYTHONUTF8="1"))
    cats = {d["slug"]: d for d in map(load, glob.glob(str(ds / "categories" / "*.json")))}
    with open(args.out, "w", encoding="utf-8") as out:
        for p in load(ds / "test_pairs.json")["pairs"]:
            trg = load(ds / "triggers" / f"{p['trigger_id']}.json")
            m = load(ds / "merchants" / f"{p['merchant_id']}.json")
            c = load(ds / "customers" / f"{p['customer_id']}.json") if p.get("customer_id") else None
            msg = compose(cats[m["category_slug"]], m, trg, c)
            out.write(json.dumps({"test_id": p["test_id"], **msg}, ensure_ascii=False) + "\n")
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
