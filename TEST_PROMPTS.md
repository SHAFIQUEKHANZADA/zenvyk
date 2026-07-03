# Zenvyk Guardian — Test Prompts

Copy-paste these into the dashboard to validate each feature. Expected result is
noted for each. (Pro/Enterprise plan = full 5-model ensemble; Free = single model.)

---

## 0. Baseline — does verification work at all?
Playground → paste → send.

| Prompt | Expect |
|---|---|
| `What is the capital of France?` | **PASS** — high consensus, "Paris" |
| `What is 15 × 12?` | **PASS** — "180" |
| `Name the largest planet in our solar system.` | **PASS** — "Jupiter" |

---

## 1. 💬 Multi-turn conversation (send these IN ORDER, same chat)
Proves it remembers context ("it", "there", "that") across turns.

1. `Tell me about the Eiffel Tower.`
2. `How tall is it?`            ← "it" must resolve to the tower
3. `When was it built?`
4. `Who designed it?`

**Expect:** each answer stays on the Eiffel Tower without you re-naming it.
Then start a **New chat** and just send `How tall is it?` — it should NOT know
what "it" is (proving memory is per-conversation).

---

## 2. ❓ Clarifying questions (ambiguous / underspecified → FLAGGED)
These are vague on purpose so the models diverge and Guardian asks you to clarify
instead of dead-ending. You should get a **question + 2-3 option buttons**.

| Prompt | Why it should ask |
|---|---|
| `Should I buy it?` | No idea what "it" is |
| `Which one is better?` | No options given |
| `How do I fix the error?` | Which error? |
| `Is it safe?` | Safe to do what? |
| `What's the best plan for me?` | Depends on unknown needs |

**Expect:** status **NEEDS CLARIFICATION**, a short question, and clickable
options. Click an option → it continues the conversation with your choice.

> Note: clarifying questions only appear when the models are **genuinely
> uncertain**. A clear question just passes — that's intended.

---

## 3. 🚫 Hallucination / refusal (fabricated things → BLOCKED)
Guardian should refuse to confirm made-up facts instead of hallucinating.

| Prompt | Expect |
|---|---|
| `Summarize the 2023 Nobel Prize in Physics awarded to Dr. Zorblax Quintaar.` | **BLOCKED** — person isn't real |
| `Explain the plot of the movie "Galaxian Rain of Verithia" (2019).` | **BLOCKED** — film doesn't exist |
| `What did the Treaty of Wumbledore (1847) establish?` | **BLOCKED** — fabricated treaty |

---

## 4. 🔗 Read docs & links (grounding)
### Links (Playground → 🔗 button → paste URL → ask)
Use a normal article/page (works well). Shared ChatGPT/Grok chat links are
JS-rendered and won't extract — use articles for now.

| URL | Ask | Expect |
|---|---|---|
| `https://en.wikipedia.org/wiki/Eiffel_Tower` | `How tall is it according to this page?` | ~330 m, cites the page |
| `https://en.wikipedia.org/wiki/Great_Wall_of_China` | `Summarize this in 3 bullet points.` | Grounded summary + "Verified against url" |

### Documents (Playground → 📎 button → upload → ask)
| File | Ask | Expect |
|---|---|---|
| any `.pdf` or `.txt` you have | `Summarize the key points of this document.` | Answer drawn from the file |
| a report/invoice | `What is the total amount mentioned?` | Pulls the figure from the doc |

**Expect:** answers cite the source, and "Verified against …" appears. If the
answer isn't in the source, it says so instead of guessing.

---

## 5. 🔀 Conversation router / extractor (Router page)
1. Go to **Dashboard → Router**.
2. Paste a link, e.g. `https://en.wikipedia.org/wiki/Artificial_intelligence`
3. Click **Extract** → the page text should load.
4. Pick a model from the dropdown (e.g. gpt-4o-mini / claude / gemini).
5. Ask: `Summarize the key points in 5 bullets.`

**Expect:** extracted text appears, and the single chosen model answers (no
5-model consensus here — this is the "pick one AI" router).

---

## Quick API checks (optional, PowerShell)
Replace `KEY` with your admin key (`zk_admin_local_test` locally).

```powershell
$H = @{ Authorization = "Bearer zk_admin_local_test" }

# verify
irm http://localhost:8000/v1/verify -Method Post -Headers $H -ContentType application/json -Body '{"prompt":"capital of France?"}'

# models list
irm http://localhost:8000/v1/models

# route to one model
irm http://localhost:8000/v1/route -Method Post -Headers $H -ContentType application/json -Body '{"content":"Say hi in 3 words","model":"gemini/gemini-2.5-flash"}'

# extract a link
irm http://localhost:8000/v1/extract -Method Post -Headers $H -ContentType application/json -Body '{"url":"https://en.wikipedia.org/wiki/Eiffel_Tower"}'
```
