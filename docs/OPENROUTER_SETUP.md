# OpenRouter setup for AI Highlights

AI Highlights uses [OpenRouter](https://openrouter.ai) to reach an AI model. OpenRouter is the only
AI provider SaveMe talks to. Transcription doesn't need it: it runs locally, and **Transcribe only**
works without a key.

## 1. Create a key

1. Sign in at <https://openrouter.ai> and add a few dollars of credit (Settings → Credits).
2. Create a key at <https://openrouter.ai/keys>.
3. **Set a credit limit on the key.** This is the only hard spending cap. SaveMe's own limit
   (`AI_MAX_COST_USD_PER_JOB`) is a guard based on estimates, not a billing cap.

## 2. Give it to SaveMe

Add the key to `.env` next to `compose.yaml` (copy `.env.example` if you have no `.env`):

```env
OPENROUTER_API_KEY=sk-or-v1-...
```

Then run `docker compose up -d`. To run without Docker, export the variable in the shell that starts
`python -m app`.

The key stays on the server:

- it is read from the environment only, and can't be entered in the page;
- it is never returned by any API route (`/api/ai/status` says only whether a key is set);
- it is never written to the log, and it is kept out of the settings' `repr`;
- it is scrubbed from any OpenRouter message shown on the page.

`.env` is in `.gitignore`. Keep it out of source control.

## 3. Check it

Open a video in Clip Studio, choose the **AI Highlights** tab, and choose **Test connection**. It
calls `GET /key` (the key's usage and limit) and reads the model's entry in OpenRouter's catalogue.
It shows the model's prices and what the key has spent. No completion is made, so it costs nothing.

## Choosing a model

| Setting | Default |
| --- | --- |
| `OPENROUTER_MODEL` | `anthropic/claude-haiku-5.5` |

The default was chosen on **24 November 2026** from OpenRouter's live catalogue
(`GET https://openrouter.ai/api/v1/models`, 474 models at the time). It was the best fit for SaveMe:

- **price:** $0.10 per million input tokens and $0.50 per million output tokens (a higher tier
  applies to prompts over 100k tokens; SaveMe's chunks stay far below that);
- **context:** 1,000,000 tokens, with up to 128,000 output tokens;
- **structured outputs:** `structured_outputs` and `response_format` are listed on its providers
  (Anthropic, Google Vertex and Azure list `structured_outputs`; Bedrock and the AWS platform list
  `response_format`);
- **reasoning is optional:** effort `none` is offered, so no hidden "thinking" tokens are paid for;
- **language:** strong multilingual quality, which matters for Malayalam and other transcripts.

Cheaper models exist (for example `deepseek/deepseek-v4.1-flash-1017` at $0.03/$0.20, with reasoning
on by default). Any id from <https://openrouter.ai/models> can be set. Before each run SaveMe checks
the model in the catalogue, and refuses it with a clear message if it:

- doesn't take and return text;
- has no fixed price (for example routers such as `openrouter/auto`, priced `-1`), so the spending
  limit couldn't be kept;
- has a context window too small for a transcript chunk and its answer;
- can't be given a maximum answer length (`max_tokens` or `max_completion_tokens`).

Model prices and availability change. **Test connection** shows the current prices.

## What SaveMe sends

One `POST /chat/completions` per transcript chunk (up to about 6,000 tokens or 20 minutes of
speech), in this shape:

```json
{
  "model": "anthropic/claude-haiku-5.5",
  "messages": [
    {"role": "system", "content": "…fixed rules and answer format…"},
    {"role": "user", "content": "…video length, clip rules, <instruction>…</instruction>, <transcript>[12] 5:25.5-5:31.0 …</transcript>"}
  ],
  "max_tokens": 2150,
  "response_format": {"type": "json_schema", "json_schema": {"name": "highlights", "strict": true, "schema": {…}}},
  "reasoning": {"effort": "none"},
  "provider": {"require_parameters": true, "data_collection": "deny"}
}
```

- **Only parameters the model lists are sent.** With `require_parameters: true` (so a JSON schema is
  never silently ignored), an unlisted parameter would rule out every provider. For example,
  Claude Haiku 5.5 lists no `temperature`, so none is sent.
- A model without `structured_outputs` gets `{"type": "json_object"}`, or no response format at all;
  the answer is validated the same way in every case.
- **Privacy:** `OPENROUTER_DATA_COLLECTION=deny` (the default) routes only to providers that don't
  store or train on prompts. With `allow`, any provider may be used.
- **Never sent:** audio, video, thumbnails, file names, file paths, or the
  source link. The `HTTP-Referer` and `X-Title` attribution headers aren't sent either.

## Costs and limits

| Setting | Default | Effect |
| --- | --- | --- |
| `AI_MAX_COST_USD_PER_JOB` | `0.25` | A run is refused if its upper-bound estimate is over this, and it stops before a call that could pass it. |
| `AI_MAX_CLIPS` | `10` | Highest "Number of clips" the page offers. |
| `AI_MAX_CONCURRENT_JOBS` | `1` | Runs at the same time; others wait. Downloads are never held up. |

- **Before a run**, the page shows an estimate and an upper bound. Without a transcript yet, they
  are worked out from the video's length (cautiously, about 1,800 bytes of transcript a minute).
  With one, they come from the real transcript.
- **The upper bound holds for any model.** It counts one token per byte of the prompt, since no
  tokenizer makes tokens smaller than a byte, plus the whole answer budget. The estimate uses a
  typical 3 bytes a token. SaveMe refuses a run whose bound is over the limit, and makes no call or
  retry that could pass it.
- **After a run**, it shows the cost OpenRouter reported (`usage.cost`, returned by default) and the
  token counts. If a call reported no cost, its tokens are priced from the catalogue and the total
  is marked as an estimate.
- An unusable answer is asked for once more, then that chunk is skipped, and both calls count.
- A request that failed after reaching the model may have been billed: a read timeout, a broken
  connection after sending, or an error inside a 200. It counts at its reported usage, or else at
  its upper bound, and the total is then shown as an estimate. A reply without usage counts the
  same way. Failures before anything was generated aren't counted: a refused connection, or a
  429/5xx from OpenRouter.
- A run you cancel while a request is in flight may still be billed for that request.

A one-hour talk is typically 3-4 chunks: about $0.001-$0.005 on the default model.

## Errors and retries

| Problem | SaveMe's behaviour |
| --- | --- |
| Network error, timeout, 408, 429, 500, 502, 503, 504, or an error inside a 200 reply | Retried 3 times, after 1 s, 2 s, 4 s (plus jitter), or after `Retry-After` when given (up to 60 s). |
| 429 that persists, or `Retry-After` over 60 s | "OpenRouter is limiting requests right now." |
| 401 | "OpenRouter didn't accept the API key." Not retried. |
| 402 | "The OpenRouter account or key is out of credits." Not retried. |
| Other 4xx (for example 404 "No endpoints found matching your data policy") | "OpenRouter refused the request (status): reason". Not retried. |
| Unknown model, no fixed price, too little context | "The model … can't be used for AI Highlights: reason". |

## Live check (optional)

The standard test suite never calls OpenRouter. One opt-in test runs a real analysis of a short
built-in transcript (well under $0.01):

```sh
OPENROUTER_API_KEY=sk-or-v1-... .venv/bin/pytest -m network tests/network/test_openrouter_live.py -v
```

## Other OpenRouter-compatible endpoints

`OPENROUTER_BASE_URL` must be an `https://` URL with no login, query or fragment. It exists for
OpenRouter's own regional or proxy addresses. Other providers are out of scope.
