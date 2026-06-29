# Zenvyk Guardian

An **AI Runtime Verification Proxy**. It sits in front of LLMs, fans each prompt
out to multiple models, and verifies every output for **hallucination** and
**drift** before returning it — producing a **PASS / FLAGGED / BLOCKED** verdict
via multi-model consensus.

Drop-in design: any app using the OpenAI API can point at Guardian by changing
only its `base_url`.

## How it works

1. A prompt comes in.
2. Guardian fans it out to N models concurrently via **LiteLLM**.
3. `guardian_filter` verifies:
   - **Semantic drift / consensus** — average pairwise cosine similarity of the
     responses (`all-MiniLM-L6-v2`).
   - **NLI entailment** — each response is checked for entailment against a
     reference response (`typeform/distilbert-base-uncased-mnli`).
4. Returns a **verdict** + the verified response + scores.

### Verdict logic

```
total = number of usable responses
PASS    if entail_votes >= ceil(0.6 * total) AND consensus_score >= 0.70
BLOCKED if entail_votes <= 1               OR consensus_score <  0.50
FLAGGED otherwise
```

Thresholds are configurable via env vars.

## Endpoints

| Method | Path                   | Purpose                                       |
|--------|------------------------|-----------------------------------------------|
| POST   | `/v1/verify`           | Verify a prompt → verdict + scores            |
| POST   | `/v1/chat/completions` | OpenAI-compatible drop-in verifying proxy     |
| GET    | `/health`              | Liveness `{ "status": "ok" }`                 |
| GET    | `/stats`               | In-memory running totals (for the dashboard)  |

## Run locally

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
# CPU-only torch keeps things light:
pip install torch --index-url https://download.pytorch.org/whl/cpu
pip install -r requirements.txt

cp .env.example .env   # add whatever provider keys you have
uvicorn app.main:app --reload
```

First run downloads the Hugging Face models (~a few hundred MB, one-time).

## Try it

```bash
# Health
curl http://localhost:8000/health
# -> {"status":"ok"}

# Verify a prompt
curl -X POST http://localhost:8000/v1/verify \
  -H "Content-Type: application/json" \
  -d '{"prompt":"What is the capital of France?"}'
# -> {"verdict":"PASS","consensus_score":0.93,"agreement":"3/3",...}

# Drop-in OpenAI-compatible call
curl -X POST http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"zenvyk-guardian","messages":[{"role":"user","content":"What is the capital of France?"}]}'
# -> OpenAI-shaped response, header: x-guardian-verdict: PASS

# Stats
curl http://localhost:8000/stats
```

### Using it from the OpenAI SDK

```python
from openai import OpenAI
client = OpenAI(base_url="http://localhost:8000/v1", api_key="unused")
resp = client.chat.completions.create(
    model="zenvyk-guardian",
    messages=[{"role": "user", "content": "What is the capital of France?"}],
)
print(resp.choices[0].message.content)
```

## Configuration

See [.env.example](.env.example). Key vars:

- `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY` — provider keys (use
  whichever you have; even 2 models works).
- `GUARDIAN_MODELS` — comma-separated LiteLLM model names to fan out to.
- `CONSENSUS_PASS_THRESHOLD` (default `0.70`), `CONSENSUS_BLOCK_THRESHOLD`
  (default `0.50`).

## Deploy to Railway

1. Push this repo to GitHub.
2. Railway → New Project → Deploy from GitHub.
3. Start command: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
   (also set in `railway.json` / `Procfile`).
4. Add env vars: `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`
   (+ optional config).
5. Deploy → grab the live URL → test `/health` and `/v1/verify`.

## Notes / caveats

- **Latency:** querying multiple models + running NLI takes seconds, not
  sub-200ms. Correctness first for the MVP.
- **Memory:** `torch` + models are heavy. On Railway's free tier, install
  CPU-only torch (handled in the Dockerfile) and stick to the small
  MiniLM/DistilBERT models.
- In-memory store is cleared on restart (no DB by design for the MVP).
