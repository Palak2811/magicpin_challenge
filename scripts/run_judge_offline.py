"""Run the provided judge_simulator.py against a live bot.

Without a key it uses a stub scorer (warmup / auto-reply / intent / hostile checks still run).
With JUDGE_LLM_PROVIDER + JUDGE_LLM_KEY (e.g. gemini / openai / anthropic) it uses the simulator's real LLM judge.

Usage:  BOT_URL=http://127.0.0.1:8080 [SCENARIO=all|full_evaluation|phase2_short] python scripts/run_judge_offline.py
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# the original challenge folder is the source of truth; `challenge/` is an older extracted copy
CHALLENGE = next((p for p in (ROOT / "magicpin-ai-challenge", ROOT / "challenge") if p.exists()), ROOT / "magicpin-ai-challenge")
sys.path.insert(0, str(CHALLENGE))

import judge_simulator as js  # noqa: E402


class StubLLM(js.LLMProvider):
    def complete(self, prompt, system=None):
        return "{}"

    def name(self):
        return "stub (no scoring)"


js.BOT_URL = os.getenv("BOT_URL", "http://localhost:8080")
if os.getenv("JUDGE_LLM_KEY"):
    js.LLM_PROVIDER = os.getenv("JUDGE_LLM_PROVIDER", "gemini")
    js.LLM_API_KEY = os.getenv("JUDGE_LLM_KEY")
    js.LLM_MODEL = os.getenv("JUDGE_LLM_MODEL", "")
    scorer = js.create_provider()
    # free-tier keys allow ~15 requests/min: space out judge calls (JUDGE_MIN_INTERVAL seconds)
    import time
    _gap, _last = float(os.getenv("JUDGE_MIN_INTERVAL", "0")), [0.0]
    _complete = scorer.complete

    def _paced(prompt, system=None):
        wait = _gap - (time.time() - _last[0])
        if wait > 0:
            time.sleep(wait)
        _last[0] = time.time()
        return _complete(prompt, system)
    scorer.complete = _paced
else:
    scorer = StubLLM()
judge = js.JudgeSimulator(scorer)
judge.client = js.BotClient(js.BOT_URL)
ok = judge.run(os.getenv("SCENARIO", "all"))
sys.exit(0 if ok else 1)
