# llm-proxy

An OpenAI-compatible HTTP proxy that routes `/v1` requests to either OpenRouter
(cloud) or local model servers. One file, Python standard library only, no
dependencies.

## What it is

One file, Python standard library only, no dependencies. It fronts OpenRouter
and local model servers with a single OpenAI-compatible `/v1` API. In the
feyntalk chain it can run a local engine in token mode and translate the
engine's token stream back into Chat output, keeping an assistant turn's
thinking across turns.

| piece | version | link |
|---|---|---|
| engine (arcint) | 1.4.0 | [https://github.com/marfrit/arcint](https://github.com/marfrit/arcint/releases/tag/v1.4.0) |
| renderer (feyntalk-render) | 1.0.0 | [https://github.com/marfrit/feyntalk-render](https://github.com/marfrit/feyntalk-render/releases/tag/v1.0.0) |
| gateway (llm-proxy) | 1.3.1 | this repository |

The wiring guide for the whole chain is arcint's [`docs/feyntalk.md`](https://github.com/marfrit/arcint/blob/v1.4.0/docs/feyntalk.md). The runtime enforces these
versions; see Version handshake.

## Quick start

    mkdir -p /etc/llm-proxy
    cp local-backends.example.json /etc/llm-proxy/local-backends.json
    cp model-aliases.example.json   /etc/llm-proxy/model-aliases.json   # optional
    OPENROUTER_API_KEY=sk-or-... python3 llm-proxy.py 8082

Point an OpenAI-compatible client at `http://<host>:8082/v1`. Cloud routing
needs the key; without it, local routing works. To run a local engine in token
mode, keep its `local-backends.json` entry classic (`"feyntalk": "off"`, or
omit the key) and add a `-ft` alias with `"feyntalk": "tokens"`:

    {"qwen3.6-coder-ft": {"model": "qwen3.6-coder", "feyntalk": "tokens"}}

Lightweight clients that speak OpenAI directly to the engine keep using the
classic name (text); agents use the `-ft` name.

## NOTICE

`tests/vendor/deepseek_v4_encoding.py` and `tests/vendor/deepseek_v41_encoding.py`
are vendored from the vLLM project (Apache-2.0);
`tests/fixtures/qwen3.6-35b-a3b-chat-template.jinja` is the Qwen team's chat template (Apache-2.0). See
`tests/vendor/README.md` and `tests/fixtures/README.md`.

## What it does

- Presents a single OpenAI-compatible `/v1` API in front of many backends.
- Routes each request by model id: cloud-prefixed ids go to OpenRouter; other
  ids go to the local backend that advertises them.
- Serves a merged `/v1/models` list — the OpenRouter catalog plus every
  reachable local server's models — with each id tagged by source and cost tier.
- Maintains the cloud catalog in the background: adds newly-free models, drops
  models that stopped being free, marks unavailable ones, and discovers cheap
  paid models.

## Requirements

- Python 3, standard library only.
- `OPENROUTER_API_KEY` in the environment for cloud routing. Without it, only
  local routing works.

## Run

    OPENROUTER_API_KEY=sk-or-... python3 llm-proxy.py 8082

The listen port is the first argument (default `8082`). The server binds `0.0.0.0`.

### Browser path

The LAN stays a trust zone: LAN clients need no token. Only the browser path is
closed off. A request carrying an `Origin` header whose host is not in
`LLM_PROXY_ALLOWED_ORIGINS` is refused with `403`; requests without `Origin` pass
unchanged. A non-empty POST body must be `application/json`, otherwise `415` —
which forces a CORS preflight for browsers. `OPTIONS` never answers `*`: it echoes
an allowed origin, otherwise it sends no CORS headers.

## Configuration

### Environment

| Variable | Default | Purpose |
|---|---|---|
| `OPENROUTER_API_KEY` | — | Bearer key for OpenRouter. Substituted server-side; clients never send it. |
| `LLM_PROXY_CONFIG_DIR` | `/etc/llm-proxy` | Directory holding the files below. |
| `LLM_PROXY_REFERER` | `http://localhost` | `HTTP-Referer` sent to OpenRouter. |
| `LLM_PROXY_TITLE` | `llm-proxy` | `X-Title` sent to OpenRouter. |
| `LLM_PROXY_ALLOWED_ORIGINS` | empty | Comma-separated browser-origin hosts allowed to call the proxy. Empty = no browser origin is allowed. Requests without an `Origin` header (curl, agents, servers) always pass. |
| `LLM_PROXY_RECHECK_MIN_INTERVAL` | `300` | Seconds between two `POST /admin/recheck` calls; a call inside the window gets `429`. |
| `LLM_PROXY_LOG_FILE` | `/var/log/llm-proxy.log` | Log file. Every line starts with ISO date and time. Empty means stdout; an unwritable path falls back to stdout. |
| `LLM_PROXY_LOG_MAX_BYTES` | `10485760` | Rotate when the next line would exceed this size. `0` disables rotation. |
| `LLM_PROXY_LOG_BACKUPS` | `5` | Number of rotated files kept (`proxy.log.1` … `proxy.log.N`). |

### Files (in `LLM_PROXY_CONFIG_DIR`)

| File | In repo | Purpose |
|---|---|---|
| `local-backends.json` | example only | Local backends: `{name, host, port, slots, feyntalk?}`. `feyntalk` is `"off"` (default), `"tokens"` or `"text"`. Copy `local-backends.example.json`. |
| `friendly-names.json` | example only | Optional display aliases `{model-id: label}`. Copy `friendly-names.example.json`. |
| `model-aliases.json` | example only | More names for a local model, optionally with a prompt cap and a feyntalk mode: `{alias: {model, max_prompt_tokens?, feyntalk?}}`. Copy `model-aliases.example.json`. See Model aliases. |
| `local-max-output.json` | example only | Max output tokens per local model id: `{model-id: int}`. Copy `local-max-output.example.json`. See Local max output. |
| `cloud-catalog.json` | no (state) | Persisted OpenRouter catalog + status. Written by the proxy. |
| `last-known-models.json` | no (state) | Last-seen model list. Written by the proxy. |

If `local-backends.json` is absent, the proxy falls back to one `127.0.0.1:8080`
backend.

## Endpoints

- `POST /v1/chat/completions` — routed by model id (see Routing); streaming supported.
- `POST /v1/responses` — responses pass-through; token mode for `feyntalk: "tokens"` backends (see feyntalk token mode).
- `GET /v1/models` — merged local + cloud catalog.
- `POST /admin/recheck` — trigger a catalog re-check (at most once per `LLM_PROXY_RECHECK_MIN_INTERVAL` seconds).

Resolved requests are forwarded to the chosen backend and the response streamed back.

Every incoming request carries an `X-Request-Id`: a client value that satisfies
arcint's grammar (1–128 chars of `[A-Za-z0-9._:-]`) is kept, otherwise the gateway
generates `req-` plus 16 lowercase hex. The id goes to every backend attempt of
that request (failover included), comes back on every response the gateway sends
(SSE included), and is on the per-request log line as `rid=<id>` — the same key
arcint writes to its journal, so the two lines match by grep. A client value
outside the grammar is never forwarded or logged.

Upstream error statuses are passed through where they mean something: a 429 stays
429 (with `Retry-After` when the upstream sends it), other 4xx stay 4xx, only
network and 5xx errors become 502. Error logs carry request size, model and a short
hash — never prompt text.

## Routing

1. A **cloud-prefixed** model id (`openai/`, `anthropic/`, `google/`,
   `deepseek/`, `qwen/`, `x-ai/`, …) goes to OpenRouter over HTTPS with the
   server-side key.
2. A **known local** model goes to the backend whose `/v1/models` advertises it
   (cached 30 s).
3. Unknown model or empty body: the first reachable local backend.

The `/v1/models` ids carry display tags — `[$]` paid cloud, `[free]` free cloud,
`[local]` local. The tag is stripped before routing, so a client may send back
the exact id it was shown.

## Stream repairs (local backends)

Three filters sit on the streaming path of local backends. Frames they have no
reason to change go out byte for byte; cloud and Zen streams are not filtered.

- **`</think>` seam** (`ReasoningSeam`): a reasoning tail that the backend's
  parser pushed into `content` is cut back out. Since 1.0.5 the held answer start goes out
  before `finish_reason` also when both arrive in one frame (short answers). Only streams
  of local models known to reason run through it; a stream whose first content delta has no
  reasoning before it releases the held start immediately.
- **ChatML markers in the answer** (`ChatMLGuard`, 1.0.6): `<|im_end|>` or
  `<|im_start|>` in `content` ends the turn right there (`finish_reason:
  "stop"`, backend cut). Reasoning and tool-call arguments may contain them.
  Streaming requests no longer carry these as `stop` strings — a stop string
  also ended turns whose *thinking* mentioned a chat template. Non-streaming
  local requests keep the `stop` strings. Log: `chatml=cut`. A cut turn carries
  no `usage` frame (the backend's comes after the cut).
- **Tool calls written as text** (`ToolTextSalvage`, 1.0.4): a model that writes
  `<invoke name="…"><parameter name="…">…</parameter></invoke>` into `content`
  instead of a tool call (DS-4.1 on the 2-bit pack from ~460k tokens on, and
  then by copying its own broken turn) gets it delivered as a real `tool_calls`
  delta with `finish_reason: "tool_calls"`. Since 1.0.9 the same holds for DeepSeek's own
  DSML markup, which vLLM's `deepseek_v41` parser misses when it lands in `content`:
  `<｜DSML｜ calls>` / `<｜DSML｜ invoke name="…">` / `<｜DSML｜ parameter name="…"
  string="true|false">…</｜DSML｜ parameter>` (V4.1, with the space) and the V4 spelling
  without the space and with `tool_calls` as the wrapper. `string="true"` is raw text and
  must be allowed as a string by the schema; `string="false"` is a JSON value that must match
  the schema type. The form a turn starts with applies to the whole turn; wrappers must be
  balanced. Only when everything fits:
  - the request carried `tools` and no `tool_choice: "none"` (a named
    `tool_choice` limits it to that function, `parallel_tool_calls: false` to one
    call), one choice only;
  - **every** block starts a line (at most three spaces before it), outside a
    ```` ``` ```` or `~~~` fence, and is complete;
  - the answer ended with `stop`, not `length`;
  - every name is one of the request's tools, every parameter is in its schema
    with the right type, all required parameters are present.

  Blocks separated by prose are drafts: only the last group runs, and no block
  stays in the text. Values are passed verbatim. Otherwise the held text is
  passed on unchanged (also beyond 64k held characters). The access log says
  `tooltext=salvaged …` or `tooltext=kept (…)`. Non-streaming responses are not
  touched. Known limit: an unfenced, complete, schema-valid block at a line
  start is taken as a call, whatever the prose around it says.

## feyntalk token mode (opt-in per backend)

The gateway can consume the engine's token mode and build the client output from
the same normalised stream. It is **off by default**: without a `feyntalk` field a
backend takes exactly the paths it took before.

```json
{"name": "engine", "host": "engine.example.lan", "port": 8080, "slots": 1,
 "feyntalk": "tokens"}
```

| value | meaning |
|---|---|
| absent / `"off"` | never ask, never change the request or the stream |
| `"tokens"` | ask `/props` + `/v1/responses/renderer`; token mode when the engine is ready. On a backend: text mode (logged) otherwise. On a `-ft` alias: an error otherwise, never the text path |
| `"text"` | pin text mode without asking |

Requires `feyntalk-render` (Python 3.11+, Jinja2 3.1.6) importable by the proxy.
Install it from the renderer repository until packaging ships it:

    pip install /path/to/feyntalk-render

Without the import the proxy still runs; a `feyntalk: "tokens"` backend then logs
once that token mode is unavailable and stays on the text path.

### Discovery and material cache

For a `"tokens"` backend the proxy reads `/props` (`token_mode: "ready"` and
`renderer_hash`) and `GET /v1/responses/renderer`. The material is validated by
the renderer's canonical hash check and cached by hash for 30 s alongside one
`Renderer`. A changed hash refetches; an unready engine, an unreachable endpoint,
a material that fails the hash check or a template the renderer refuses falls back
to text with one log line. `/props` is never on the request path for an `off`
backend. As soon as the probe sees a ready engine, the material is fetched and
the renderer built in the background, so the first request finds it ready. A
request whose client has already hung up during its prepare is cancelled before
any backend request goes out (`CLIENT-CANCELLED before send`).

### Requests and output

- Chat Completions: the proxy renders the prompt from the wire messages with the
  material's template, sends the engine an Open Responses request with
  `arcint.token_mode`, `arcint.renderer_hash` and `arcint.render.prompt_sha256`,
  and translates the token/span events back into `reasoning_content`, `content`
  and real `tool_calls`. A tool call's name and arguments come from its span; the
  call id comes from the terminal `function_call` item. Streaming and non-stream
  Chat requests are both served. A turn on which the engine reports its own
  numbers — the context guard's arithmetic (`arcint:context`), the loop-guard
  counters (`arcint:loop_guard`) and the fit pass's totals (`arcint:truncated`),
  all from the engine's `terminal()` path — carries them on the last stream
  frame and on the non-stream body as `arcint: {context: {...}, loop_guard:
  {...}, truncated: {...}}` next to `usage`. Each key holds exactly the fields
  the engine sent for that event, `type` dropped, nothing else: `token_index`
  only when the guard closed, and a key is absent when its event was absent. A
  turn without any of them adds no `arcint` object, so the shape stays
  byte-identical; the text path is untouched, and `/v1/responses` keeps passing
  the raw events through.
- `/v1/responses` (and `/responses`): a token-mode request is augmented with the
  same render hashes and the engine's SSE is passed through unchanged, `arcint:`
  events included. Token mode there is stream-only.
- No salvage filter runs on a token-mode stream: `ReasoningSeam`, `ChatMLGuard` and
  `ToolTextSalvage` stay built only for the text/cloud paths.

A 409 from the token request is the pre-token renderer refusal: the proxy logs it
and re-sends that same turn once as a text request. The next request discovers
again; nothing degrades silently.

### preserve_thinking (thinking replay)

Replaying an assistant turn — a new user message such as a pi steering turn
re-renders every earlier turn — would drop its thinking unless the Qwen3.6 template
sees `preserve_thinking`. The gateway keeps it by default: when the engine's
`/props` reports `chat_template_caps.supports_preserve_thinking`, the render gets
`preserve_thinking: true` in its context and the upstream request gets
`arcint.preserve_thinking: true`, so both sides render the same bytes (a backend
without the capability is untouched). A client may override it: Chat
`chat_template_kwargs.preserve_thinking` or Responses `arcint.preserve_thinking`
(strict bool; anything else is a gateway 400, not forwarded). As the first
compaction stage, once the preserved render would exceed
`FEYNTALK_THINK_DROP_FRACTION` (default 0.7) of the engine's context length
(`/props` `model.n_ctx`), the gateway renders again with `preserve_thinking:
false` and sends that; the decision is taken on the preserved render's size. The
exact count is deterministic per prompt, and when `/tokenize` fails the estimate is
scaled by the last observed exact/estimate ratio for that backend, so a decision
once taken stays put and both caches stay warm. As long as the estimate stays below
`FEYNTALK_TOKENIZE_PRE_FRACTION` (0.8) of the threshold, the size is
`len(prompt) / FEYNTALK_CHARS_PER_TOKEN` (default 3.0), because the `/props` probe
carries no cheap token count. Above that it would only take one overestimation to
drop the thinking early, so the gateway asks the engine for the exact count of the
preserved render (`POST /tokenize`, same Authorization as every engine route) and
takes the decision on that number; if the route is absent, answers non-200 or runs
into the timeout, the gateway falls back to the estimate scaled by the last observed
exact/estimate ratio (the raw estimate if none was observed yet) and logs that once
per backend -- the counter never fails a request. Without a known context length
the gateway never drops and logs that once per material. Each token-mode request
line carries `think=kept|dropped|off` (`off` when the capability is absent or
thinking is disabled); an automatic decision also names where the number came from,
`think=kept(exact)`, `think=kept(estimate)`, `think=dropped(exact)`,
`think=kept(estimate, scaled)` or `think=dropped(estimate, scaled)`, and the
one-time drop line writes `N tokens (exact)`, `~N tokens (estimate)` or
`~N tokens (estimate, scaled)`.

### Version handshake

Token mode requires a compatible chain. The gateway reads the installed
`feyntalk-render` version (`__version__`, else package metadata) and needs
`>= 1.0.0`; it reads `/props` for the engine's version (`build.version`) and the
`arcint.v` contract and needs the contract it speaks. A mismatch logs one line per
backend and stays on the text path — never a failed request.

### Request shapes that stay text (logged, one line)

Images, `stop` strings, `n > 1`, a message `name`, a non-stream `/v1/responses`
request, and a `previous_response_id` continuation cannot be round-tripped through
the engine's Responses input mapping in this version. They take the text path,
unchanged. Several tool calls in one assistant turn and content beside a call do
ride token mode now: the engine merges a run of `[reasoning]? [assistant
message]? [function_call]+` into ONE assistant turn (parallel tool calls), and the gateway's
mapping is its exact inverse.

The token-mode tests use a fake engine over loopback and synthetic material: they
prove the gateway seam, not a served model. Byte equality against a real engine is
the per-turn prompt hash and shadow check of the render input.


## Background catalog maintenance

A thread runs every 3 hours and:

- adds newly-free models over 7 B parameters to the free catalog,
- removes models that are no longer free,
- probes each free model with a 1-token request and suffixes `-na` on HTTP 429,
- discovers paid models under `$0.50` / M-token from known prefixes.

The paid catalog is rebuilt from that discovery on every successful check: a
model that got pricier or left OpenRouter drops out. There is no hand-curated
paid list.

`reachable` is the readiness field a client (e.g. a rotation extension) should use; local
entries carry `ctx`/`n_ctx`, not `context_length`.

## Model aliases (`model-aliases.json`)

vLLM has one `max_model_len` for all of a backend's slots. To offer one backend as two lengths —
e.g. `ds41-flash` with the full 1M context and `ds41-flash-light` capped at 256k for subagents —
the cap lives here and only here, never in clients.

- An alias is listed in `/v1/models` next to its target (`[local] <alias>`, same `owned_by`; a
  capped alias reports `ctx` = its cap) and is routed to the target's backend under the target's
  name (same slots, same admission). Without `max_prompt_tokens` it is a plain second name (used for
  renames: the old name keeps working).
- A capped alias counts the prompt with the model's own tokenizer: `POST /tokenize` on the backend
  with the request's `messages`, `tools` and `chat_template_kwargs` (vLLM: includes the chat
  template). If that fails, a deliberately conservative estimate is used: characters of the JSON of
  messages+tools / 2.0 (measured on pi agent transcripts: 3.1-3.2 characters per token), so it
  rejects rather than admits in doubt.
- Over the cap: `400 {"error": {"type": "context_length_exceeded", "code": "prompt_too_long_for_alias",
  "prompt_tokens": n, "max_prompt_tokens": cap, "counted_by": "tokenize"|"estimate"}}`; the message
  names the uncapped model to use instead.
- Every context field of a capped alias's catalog entry reports the cap (`max_model_len`, `ctx`,
  `context_length`, `n_ctx`, `max_context_length`, `meta.n_ctx`/`n_ctx_train`,
  `properties.context_window`), so clients that size their compaction from the catalog stay under it.
- A name that a backend serves itself is never rewritten (so an alias for an old name stays inert
  until the backend is renamed).
- Reloaded on `SIGHUP` like the other files.

### `-ft` names: one engine unit under two modes

The same engine unit can be reachable under its classic name (text, unchanged) and
a `<name>-ft` alias that runs in token mode — still exactly one backend entry, one
slot pool, one admission gate. The alias inherits the target's context, reasoning,
max-output, reasoning-effort map, friendly name and cost class, and `/v1/models`
marks it with `"feyntalk": "tokens"` and the target's catalog id in `"alias_of"`
(a generator lists only the `-ft` variant to agents by that field). When the target
is down, the alias carries the same `-down` suffix.

```json
{"qwen3.6-coder-ft": {"model": "qwen3.6-coder", "feyntalk": "tokens"},
 "qwen3.6-coder-classic": {"model": "qwen3.6-coder", "feyntalk": "text"}}
```

`feyntalk` accepts `"tokens"`, `"text"` and `"off"`, validated like the backend
setting: an unknown value is a config error logged at load/`SIGHUP` and the alias is
skipped. If the engine is not ready (or the handshake fails), the `-ft` alias falls
back to text like the classic name — it never fails where the classic name works.

## Local max output (`local-max-output.json`)

Local backends (vLLM, llama.cpp) do not report a max output token count, so agent clients that
read the catalog fall back to their own default of 16384 — too small for the main local
reasoning model. `local-max-output.json` maps a model id to a positive int, e.g.
`{"ds41-flash": 32768}`. The catalog entry then carries `max_output`, and a downstream generator
maps that field to the client's output-token limit. Like `local-ctx.json`, the file is a
correction, not the source: a `max_output` the backend itself reports always wins, and the file
only fills silence. Only positive ints and floats are kept (cast to int); `true`, strings, zero
and negatives are dropped. Reloaded on `SIGHUP` like the other files.

## Native providers

`$LLM_PROXY_CONFIG_DIR/native-providers.json` (see `native-providers.example.json`) adds OpenAI-compatible
clouds next to OpenRouter, one entry per id prefix:

    {"myplan": {"base_url": "https://api.example.com/compatible-mode/v1",
                "key_file": "/etc/llm-proxy/keys/myplan", "cost_class": "paid", "owned_by": "my-plan",
                "models": {"model-a": {}, "model-b": {"prompt_price": 0.0000004}}}}

The catalog lists `[$] myplan/model-a` (or `[free] ...` for `cost_class: free`) while the key is set.
Only the listed models are offered and routed, only `POST /v1/chat/completions` is forwarded, and the
provider's cost class is what the cost regulator sees, whatever tag a client writes. The key comes from
`key_file` (one line, mode 0600) or `key_env` (an environment variable that must reach the service);
it never appears in responses or the log. `prompt_price` ($/token, as OpenRouter's pricing) lets mode
`cheap` decide; without it `cheap` refuses the model and does not list it.

## Signals

- `SIGHUP` — reload `local-backends.json`, `friendly-names.json`, `reasoning-models.json`, `local-ctx.json`, `local-max-output.json`, `model-aliases.json`, `reasoning-effort-map.json` (since 1.0.9), `native-providers.json` (since 1.3.5). `rc-service llm-proxy reload` sends it (packaged init script since 1.0.9).
- `SIGUSR1` — drop discovery caches and re-check the cloud catalog now.

## Scope

llm-proxy has no virtual keys, budgets, caching, observability callbacks or admin
UI. Besides OpenRouter it routes to further OpenAI-compatible clouds only through
an explicit allow-list (`native-providers.json`, see below). It trades those for a single
dependency-free file plus OpenRouter free/cheap-tier auto-curation. For the
larger feature set, use a full gateway such as litellm.

## Provenance

llm-proxy was built as an optional component of an internal agent
framework. It depends only on a generic OpenAI-compatible `/v1` endpoint, so it
fits any agent framework that speaks one.

## License

Apache License 2.0 — see [LICENSE](LICENSE).
