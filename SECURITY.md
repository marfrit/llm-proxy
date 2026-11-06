# Security

llm-proxy is a local gateway in front of OpenRouter and local model servers. In
feyntalk token mode it is the **trust boundary**: it renders the prompt the
engine runs and translates the engine's token stream back into Chat output.

## What it does not do

- It **executes nothing from model output.** Text, reasoning and tool-call
  arguments are parsed and re-framed, never evaluated. A tool call is emitted to
  the client as a `tool_calls` entry; who runs it is the client's decision.
- It runs no plugins, loads no pickles, and imports no code named by a request.

## What it trusts

- **The engine's renderer material, by hash.** The material is fetched once per
  backend, its canonical hash is recomputed from its parts, and a mismatch with
  the hash the engine advertises (`/props.renderer_hash`) is refused. A changed
  template is picked up, not silently discarded.
- **The renderer package version.** Token mode requires `feyntalk-render`
  `>= 1.0.0` (`__version__`, else package metadata) and an engine whose `/props`
  reports `build.version >= 1.4.0`. If the engine also reports an `arcint.v`
  contract, it must be `1`; when it reports none, no contract check runs.

## What it refuses

- An **unknown or mismatched renderer material hash** — on a name without a token
  alias the turn falls back to the text path with one log line. On a `-ft` alias
  (`"feyntalk": "tokens"`) it fails with `502` and `code: renderer_mismatch`; only a
  moved hash gets one fresh fetch and one more token attempt first.
- A **renderer older than 1.0.0** or an **engine contract mismatch** — same split:
  text path with a log line, or on a `-ft` alias `502` and `code: token_mode_unavailable`.
  A request token mode cannot serve fails there with `400` and
  `code: token_mode_unsupported_request`.
- A **non-boolean client switch** (`chat_template_kwargs.preserve_thinking`,
  `arcint.preserve_thinking`) — answered with `400` and never forwarded.
- A request whose model is known but fails the cost policy, with `403`, before
  any paid request leaves the host.

## Configuration and secrets

Backend lists, API keys and catalog state live in `LLM_PROXY_CONFIG_DIR`
(default `/etc/llm-proxy`) and are **local only**. The repository ships only
`*.example.json` placeholders; no real host, key or catalog is committed.
`OPENROUTER_API_KEY` is read from the environment and substituted server-side —
clients never send it.

## No authentication

`/v1` carries **no authentication**. Every client that can reach the port is
served, and the server binds `0.0.0.0` by default (hardcoded — there is no
listen-address setting). Run it on a trusted network, or put it behind an
authenticating reverse proxy. Never expose it directly to the internet. The
browser-origin and media-type checks below are CSRF hardening for that LAN, not
access control.

## LAN and browser surface

The LAN is a trust zone: LAN clients need no token. Only the browser path is
closed off. A request carrying an `Origin` whose host is not listed in
`LLM_PROXY_ALLOWED_ORIGINS` is refused with `403`; requests without `Origin`
(curl, agents, servers) pass unchanged. A non-empty POST body must be
`application/json`, otherwise `415` (which forces a CORS preflight for browsers).
`POST /admin/recheck` is rate-limited by `LLM_PROXY_RECHECK_MIN_INTERVAL`
(default 300 s); a call inside the window gets `429`.

## Reporting a vulnerability

Report privately by e-mail to **mfritsche@reauktion.de**. Please do not open a
public issue for a security report. This is the only contact channel for
security matters.
