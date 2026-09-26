"""Optional LLM polish layer (challenge-brief §13: prompt + routing + post-LLM validation).

Flow: deterministic draft (composer) -> LLM rewrites wording only (temperature 0) -> validator.
If the rewrite adds any number, URL, taboo word, drops the CTA, or the call fails / times out,
the deterministic draft is used unchanged. Disabled unless LLM_PROVIDER + LLM_API_KEY are set.

Off by default and recommended off for the judged run: the challenge requires deterministic output and rejects
"unstable responses"; with an LLM (especially a rate-limited key) wording can differ between runs.

Env:
  LLM_PROVIDER   gemini | openai | anthropic | none (default none)
  LLM_API_KEY    provider key (never commit it)
  LLM_MODEL      optional override
  LLM_TIMEOUT    seconds per call (default 8)
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from urllib import error as urlerror
from typing import Callable, Optional
from urllib import request

# gemini-3.5-flash-lite: pinned version, fast, and has workable free-tier rate limits
DEFAULT_MODELS = {"gemini": "gemini-3.5-flash-lite", "openai": "gpt-4o-mini", "anthropic": "claude-haiku-4-5-20251001"}

SYSTEM = (
    "You rewrite WhatsApp messages written by Vera, magicpin's assistant for Indian merchants. "
    "Make the message sound natural, warm and concise like a knowledgeable peer — not promotional. "
    "HARD RULES: keep every fact, number, price, date, name and source exactly as given; do NOT add any new "
    "number, name, price, date, statistic, offer or URL; keep the same language mix (if the draft mixes Hindi and "
    "English, keep Hinglish in Latin script); keep exactly one call-to-action and keep it as the last sentence, "
    "including words like 'Reply YES' / 'Reply CONFIRM' / 'Reply 1/2' exactly; no greetings like 'I hope you are "
    "well'; do not introduce yourself. Keep line breaks for drafted lists. Output ONLY the rewritten message text."
)

_cache: dict[str, str] = {}
_lock = threading.Lock()
_cooldown_until = 0.0   # after a 429 (quota) we skip LLM calls until Google's retry window passes
# test hook: tests may set this to a fake callable(prompt, system) -> str
_override: Optional[Callable[[str, str], str]] = None


def provider() -> str:
    if _override is not None:
        return "override"
    p = os.getenv("LLM_PROVIDER", "none").strip().lower()
    return p if p in DEFAULT_MODELS and os.getenv("LLM_API_KEY") else "none"


def enabled() -> bool:
    return provider() != "none"


def model_name() -> str:
    p = provider()
    if p in ("none", "override"):
        return "deterministic-rules-v1 (no LLM)"
    return f"{p}:{os.getenv('LLM_MODEL') or DEFAULT_MODELS[p]} (wording polish over deterministic-rules-v1)"


def _post(url: str, body: dict, headers: dict) -> dict:
    req = request.Request(url, data=json.dumps(body).encode("utf-8"), method="POST",
                          headers={"Content-Type": "application/json", **headers})
    with request.urlopen(req, timeout=float(os.getenv("LLM_TIMEOUT", "8"))) as r:
        return json.loads(r.read().decode("utf-8"))


def _call(prompt: str) -> str:
    if _override is not None:
        return _override(prompt, SYSTEM)
    p, key = provider(), os.getenv("LLM_API_KEY", "")
    model = os.getenv("LLM_MODEL") or DEFAULT_MODELS.get(p, "")
    if p == "gemini":
        # 2.x models take thinkingBudget; 3.x models reject it and take thinkingLevel instead
        thinking = {"thinkingBudget": 0} if model.startswith("gemini-2") else {"thinkingLevel": "minimal"}
        d = _post(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                  {"systemInstruction": {"parts": [{"text": SYSTEM}]},
                   "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                   "generationConfig": {"temperature": 0, "maxOutputTokens": 800, "thinkingConfig": thinking}},
                  {"x-goog-api-key": key})
        return "".join(part.get("text", "") for part in d["candidates"][0]["content"]["parts"])
    if p == "openai":
        d = _post("https://api.openai.com/v1/chat/completions",
                  {"model": model, "temperature": 0, "max_tokens": 800,
                   "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}]},
                  {"Authorization": f"Bearer {key}"})
        return d["choices"][0]["message"]["content"]
    if p == "anthropic":
        d = _post("https://api.anthropic.com/v1/messages",
                  {"model": model, "max_tokens": 800, "temperature": 0, "system": SYSTEM,
                   "messages": [{"role": "user", "content": prompt}]},
                  {"x-api-key": key, "anthropic-version": "2023-06-01"})
        return d["content"][0]["text"]
    raise RuntimeError("LLM disabled")


# --------------------------------------------------------------------------- validation

_NUM = re.compile(r"\d+(?:[.,]\d+)*")


def _numbers(text: str) -> set[str]:
    return {n.replace(",", "") for n in _NUM.findall(text)}


def validate(draft: str, candidate: str, taboos: list[str], must_keep: Optional[list[str]] = None) -> Optional[str]:
    """Returns the cleaned candidate if it is safe, else None."""
    c = (candidate or "").strip().strip('"').strip()
    if not c or len(c) > max(400, int(len(draft) * 1.6)):
        return None
    if re.search(r"https?://|www\.", c, re.I):
        return None
    if not _numbers(c) <= _numbers(draft):          # no new numbers / prices / dates
        return None
    for t in taboos:
        t = re.sub(r"\s*\(.*?\)", "", str(t)).strip().lower()
        if t and t in c.lower() and t not in draft.lower():
            return None
    for token in ("Reply YES", "Reply CONFIRM", "CONFIRM", "Reply 1", "YES"):
        if token in draft and token not in c:       # CTA must survive
            return None
    if "₹" in draft and "₹" not in c:
        return None
    for name in must_keep or []:                    # sender / recipient identity must survive
        if name and name in draft and name.lower() not in c.lower():
            return None
    return c


def polish(draft: str, *, kind: str = "", audience: str = "merchant", category_slug: str = "",
           voice_tone: str = "", taboos: Optional[list[str]] = None,
           must_keep: Optional[list[str]] = None) -> tuple[str, bool]:
    """Returns (body, polished?). Deterministic for identical inputs (temperature 0 + cache)."""
    global _cooldown_until
    if not enabled() or not draft.strip():
        return draft, False
    taboos = taboos or []
    must_keep = [m for m in (must_keep or []) if m]
    prompt = (f"Category: {category_slug} (voice: {voice_tone}). Audience: {audience}. Trigger: {kind}.\n"
              f"Words you must never use: {', '.join(map(str, taboos)) or 'none'}.\n\nDRAFT:\n{draft}")
    key = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    with _lock:
        if key in _cache:
            return _cache[key], _cache[key] != draft
    if time.time() < _cooldown_until:
        return draft, False                          # quota window: don't cache, retry after cooldown
    try:
        out = validate(draft, _call(prompt), taboos, must_keep)
    except urlerror.HTTPError as exc:
        if exc.code == 429:
            _cooldown_until = time.time() + 20
        return draft, False                          # transient: not cached, may succeed later
    except Exception:
        return draft, False
    result = out or draft
    with _lock:
        _cache[key] = result                         # cache only real answers (accepted or rejected)
    return result, out is not None


def clear_cache() -> None:
    global _cooldown_until
    with _lock:
        _cache.clear()
        _cooldown_until = 0.0
