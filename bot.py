"""Submission entry points (challenge-brief.md §7).

* `compose(category, merchant, trigger, customer)` — the pure composition contract (§7.1).
* `app` — the HTTP server for the judge harness:  uvicorn bot:app --host 0.0.0.0 --port 8080
"""
from __future__ import annotations

from typing import Optional

from app import llm
from app.composer import compose as _compose
from app.main import _identities, app  # noqa: F401  (app re-exported for `uvicorn bot:app`)


def compose(category: dict, merchant: dict, trigger: dict, customer: Optional[dict] = None) -> dict:
    """Deterministic: same inputs -> same output (LLM polish, if enabled, is temperature 0 + cached + validated). Returns body, cta, send_as, suppression_key, rationale."""
    out = _compose(category, merchant, trigger, customer)
    voice = (category or {}).get("voice") or {}
    body, polished = llm.polish(out["body"], kind=str((trigger or {}).get("kind", "")),
                                audience="customer" if out["send_as"] == "merchant_on_behalf" else "merchant",
                                category_slug=str((category or {}).get("slug", "")), voice_tone=str(voice.get("tone", "")),
                                taboos=list(voice.get("vocab_taboo") or []),
                                must_keep=_identities(merchant or {}, customer, out["send_as"]))
    if polished:
        out["body"], out["rationale"] = body, out["rationale"] + " Wording polished by LLM (temperature 0) and validated."
    return {k: out[k] for k in ("body", "cta", "send_as", "suppression_key", "rationale")}
