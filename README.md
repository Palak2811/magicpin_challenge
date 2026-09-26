# Vera — magicpin AI Challenge bot

A merchant-engagement bot (deterministic rules, with an optional validated LLM wording pass) that implements the judge's HTTP contract
(`/v1/context`, `/v1/tick`, `/v1/reply`, `/v1/healthz`, `/v1/metadata`, plus optional `/v1/teardown`)
and the `compose(category, merchant, trigger, customer)` function from the brief.

## Approach
- **Composer (`app/composer.py`)**: routes on `trigger.kind` (30+ kinds, merchant- and customer-facing, including
  `weather_heatwave`, `local_news_event` and `unplanned_slot_open`). An unknown kind still states the facts from its
  own payload as the reason for messaging now. Each
  handler anchors on facts that exist in the pushed contexts (digest source and trial size, the merchant's own
  views, calls and CTR against peer averages, live offers, review quotes, real slots), states why the message is
  being sent now, and ends with one low-friction CTA. `send_as` is `merchant_on_behalf` for customer triggers.
- **No fabrication**: 75 of the 100 generated triggers carry `{"placeholder": true}` payloads. Their handlers fall
  back to real merchant and category data (e.g. the weakest `delta_7d` metric for a dip) and never invent
  competitor names, prices or slots. Customers with no consent or unknown customers are never messaged.
- **Language**: Hindi-English code-mix when Hindi is the merchant's primary regional language (or the customer's
  `language_pref` starts with `hi`). Replies detect the language on every turn.
- **Tick queue**: at most 1 message per merchant, and per customer, in each tick (`MAX_PER_RECIPIENT_PER_TICK`).
  Extra triggers queue for later ticks. A customer trigger waits until that customer's context arrives. A second
  message of the same kind isn't stacked while the first is unanswered, and an identical text is never re-sent.
- **Language**: Tamil, Telugu, Kannada and Marathi-first merchants and customers get a greeting in their language
  (Vanakkam, Namaskaram, Namaskara, Namaskar).
- **Replies**: price questions are answered only with figures from the pushed contexts (renewal amount or live
  offers); otherwise the bot says it doesn't have the price. There are at most 6 bot messages per conversation.
  Auto-reply means a canned phrase, or the same text 3+ times (the brief's rule).
- **Tick**: most-urgent first, dedup by `suppression_key`, max 20 actions, skips opted-out merchants. After 3
  unanswered nudge rounds to a merchant, only urgent triggers (urgency 4 or higher) go out until the merchant replies. It always
  reads the latest context version, so injected digests and performance updates show up in the next send.
- **Reply engine (`app/conversation.py`)**: an ordered rule classifier with this order:
  1. opt-out: `end`
  2. auto-reply (canned phrasing, or the same text repeated; tracked per merchant, across conversations): one
     owner nudge, then `wait` 4h, then `end`
  3. hostile: apologise and offer STOP; a second hostile reply ends the conversation
  4. commitment ("let's do it", "haan kar do", "join"): action mode immediately, delivering the draft with no
     re-qualifying
  5. off-topic (GST and similar): polite decline, then redirect to the task
  6. later: `wait`
  7. question: answer, then one CTA
  8. first "no": offer a smaller option; a second "no": `end`

  A body is never sent twice in the same conversation. An ended conversation stays ended (only `end` is returned),
  unless the merchant restarts with "Hi Vera", or a real person replies on a thread that was closed because of
  auto-replies.
- **Optional LLM (`app/llm.py`)**: set `LLM_PROVIDER` (`gemini`, `openai` or `anthropic`) and `LLM_API_KEY`. The
  Gemini default is `gemini-3.5-flash-lite`. After a 429 (quota) response it pauses LLM calls for 20s. The
  LLM only rewrites the wording of the rule-based draft, at temperature 0 with cached results. A validator rejects
  any rewrite that adds a number, a URL or a taboo word, or that drops the CTA, the ₹ price or the sender and
  recipient names, and the bot then
  sends the draft unchanged. Tick polishing runs in parallel within an 18s budget (the judge's timeout is 30s),
  and any failure or timeout falls back to the rules. With no key, the bot is purely rule-based and needs no API.
- **One signal per message**: following the site's judging guidance ("choose the one signal that should drive the
  next message"), each handler leads with a single driving fact, e.g. the biggest fixable cause of a dip rather than
  every signal.
- **Submission setting: LLM off.** The challenge site rejects "unstable responses" and requires deterministic output,
  so deploy with `LLM_PROVIDER=none`. The LLM path is kept for experiments only.
- **Tradeoff**: the rules guarantee grounded facts and millisecond responses. The LLM pass adds natural phrasing,
  and the validator stops it from inventing facts.

## Run locally
```bash
pip install -r requirements-dev.txt
uvicorn bot:app --host 0.0.0.0 --port 8080
```
Dataset: `cd magicpin-ai-challenge/dataset && python generate_dataset.py --out ./expanded` (on Windows set
`PYTHONUTF8=1`). `magicpin-ai-challenge/` is the challenge source of truth.
Tests: `pytest -q`. For a live HTTP check of every endpoint and edge case, run `python scripts/e2e_check.py --url
http://127.0.0.1:8080` against a running server (it wipes the server's state).
Submission file: `python scripts/generate_submission.py` writes `submission.jsonl`. It is always rule-based and
reproducible; `--with-llm` opts in to the LLM pass.
Load the full dataset into a running server: `python scripts/load_dataset.py`.
Provided simulator: `BOT_URL=http://127.0.0.1:8080 python scripts/run_judge_offline.py`. Add `JUDGE_LLM_PROVIDER` and
`JUDGE_LLM_KEY`, `JUDGE_LLM_MODEL` (e.g. `gemini-3.5-flash-lite`) and `JUDGE_MIN_INTERVAL=4.5` (free-tier pacing) to
get real LLM scores. Settings: see `.env.example`.

## Deploy
Render: `render.yaml` (native Python, health check
`/v1/healthz`). Use **one worker on an always-on instance**, because state is in memory. Set `TEAM_NAME`,
`TEAM_MEMBERS` (comma-separated) and `CONTACT_EMAIL` for `/v1/metadata`.

## What would help most
Real open slots and prices per merchant (the generated merchants have no offers), the review count and rating in
`performance`, and a `now` that matches trigger simulation time.
