"""Synthetic feyntalk material and a fake engine for the token-mode gateway tests.

Every special string here is fake and declared by the material; nothing names a
real model's tags. The fake engine speaks /props, the material endpoint, the
token-mode SSE and the text Chat route over loopback only.
"""
import base64
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    from feyntalk_render.material import canonical_hash
    import feyntalk_render.render  # noqa: F401  (the gateway needs it; it pulls in jinja2)
except ImportError:
    # Token mode is optional; without feyntalk-render installed these tests have nothing to test.
    print("SKIP: feyntalk_render not importable (pip install feyntalk-render)")
    sys.exit(0)

THINK_OPEN = "[[R]]"
THINK_CLOSE = "[[/R]]"
TOOLCALL_OPEN = "[[T]]"
TOOLCALL_CLOSE = "[[/T]]"
TURN_OPEN = "[[S]]"
TURN_CLOSE = "[[E]]"
FN_OPEN = "[[F:"
FN_CLOSE = "[[/F]]"
PARAM_OPEN = "[[P:"
PARAM_END = "[[/P]]"

MODEL = "fixture-model"

PIECES = {
    0: b"",
    1: b"Hello",
    2: b" ",
    3: b"Gr",
    4: b"\xc3",
    5: b"\xbc",
    6: b"\xc3\x9f",
    7: b"e",
    8: b"\xf0\x9f",
    9: b"\x98\x82",
    10: THINK_OPEN.encode(),
    11: THINK_CLOSE.encode(),
    12: TOOLCALL_OPEN.encode(),
    13: TOOLCALL_CLOSE.encode(),
    14: TURN_OPEN.encode(),
    15: TURN_CLOSE.encode(),
    16: (FN_OPEN + "get_weather>").encode(),
    17: (PARAM_OPEN + "city>Berlin" + PARAM_END).encode(),
    18: FN_CLOSE.encode(),
    19: (PARAM_OPEN + "count>3" + PARAM_END).encode(),
    20: b"?",
    21: b"Why",
}


def _b64(payload):
    return base64.b64encode(payload).decode("ascii")


def _valid_utf8(payload):
    try:
        payload.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def template_text():
    ts, te = TURN_OPEN, TURN_CLOSE
    return (
        "{% for message in messages %}"
        + ts + "{{ message['role'] }}" + te
        + "{{ message['content'] }}" + te
        + "{% endfor %}"
        + "{% if tools %}" + ts + "tools" + te + "{{ tools | tojson }}" + te + "{% endif %}"
        + "{% if add_generation_prompt %}" + ts + "assistant" + te + "{% endif %}"
    )


def template_text_preserve():
    """template_text plus a branch that reads preserve_thinking.

    Die Engine leitet supports_preserve_thinking aus dem Vorlagentext ab; der
    Zweig macht zusaetzlich den Prompt-Hash von der Flagge abhaengig."""
    return template_text() + "{{ preserve_thinking }}"


def make_material(template=None):
    core = {
        "decode": {
            "pieces_b64": [_b64(PIECES[i]) for i in range(max(PIECES) + 1)],
            "eos": 0,
            "eog_ids": [0],
        },
        "encode": {
            "model": "bpe",
            "tokens": ["" for _ in range(max(PIECES) + 1)],
            "merges": [],
            "token_types": [1 for _ in range(max(PIECES) + 1)],
            "pre": "fixture",
            "add_bos": False,
            "special_text": {
                str(i): payload.decode("utf-8")
                for i, payload in PIECES.items() if payload and _valid_utf8(payload)
            },
        },
        "chat_template": {"text": template if template is not None else template_text(),
                          "source": "gguf"},
        "special_tokens": {
            "think": [{"open_ids": [10], "close_ids": [11]}],
            "think_extended": [{"open_ids": [], "close_ids": []}],
            "tool_call": {
                "open_ids": [12], "close_ids": [13],
                "open_text": TOOLCALL_OPEN, "close_text": TOOLCALL_CLOSE,
                "fn_open": FN_OPEN, "fn_close": FN_CLOSE,
                "param_open": PARAM_OPEN, "param_end": PARAM_END,
            },
        },
        "parser": {"reasoning": "ids/1", "tag_set": "qwen", "tool_calls": "qwen3-coder/1"},
    }
    return {"schema": "feyntalk-render/1", "hash": canonical_hash(core),
            "model": {"served_id": MODEL, "canonical_id": MODEL, "quant": "none",
                      "arch": "fixture"}, **core}


def token_event(index, token_id, field):
    return {"type": "arcint:token", "v": 1, "response_id": "resp_fixture",
            "token_index": index, "token_id": token_id, "field": field}


def span_event(name, phase, index, detail=None):
    return {"type": "arcint:span", "v": 1, "span": name, "phase": phase,
            "token_index": index, "detail": detail or {}}


def sse(event):
    return ("data: " + json.dumps(event, ensure_ascii=False) + "\n\n").encode()


def engine_stream(case, context=None, loop_guard=None, truncated=None):
    """The engine's token and span events for one turn, then its terminal.

    `loop_guard`, `context` and `truncated` are the turn's object events if the
    fake engine is to carry them; the engine emits this trio in that order,
    after the spans and before the terminal (src/api/responses_stream.cpp
    terminal()). Default: none, the present byte shape."""
    head = sse({"type": "response.created",
                "response": {"id": "resp_fixture", "created_at": 1700000000,
                             "status": "in_progress"}})
    if case == "cut":
        # a thinking block that never closed (the turn hit max_output_tokens): reasoning only.
        # The engine ends the span at the last token with closed:false (Dory capture
        # 2026-11-11, c4: start 0 {preopened}, end 47 {closed:false}, response.incomplete).
        events = [
            span_event("thinking", "start", 0, {"preopened": True}),
            token_event(0, 10, "reasoning"),
            token_event(1, 21, "reasoning"),
            token_event(2, 20, "reasoning"),
            span_event("thinking", "end", 2, {"closed": False}),
        ]
        out = head + b"".join(sse(e) for e in events)
        out += sse({"type": "response.incomplete",
                    "response": {"id": "resp_fixture", "created_at": 1700000000,
                                 "status": "incomplete",
                                 "incomplete_details": {"reason": "max_output_tokens"},
                                 "output": [],
                                 "usage": {"input_tokens": 11, "output_tokens": 3,
                                           "total_tokens": 14}}})
        out += b"data: [DONE]\n\n"
        return out
    if case == "tool":
        events = [
            span_event("thinking", "start", 0, {"preopened": True}),
            token_event(0, 10, "reasoning"),
            token_event(1, 21, "reasoning"),
            token_event(2, 20, "reasoning"),
            span_event("thinking", "end", 3, {"closed": True}),
            token_event(3, 11, "reasoning"),
            span_event("tool_call", "start", 4, {"call_index": 0}),
            token_event(4, 12, "text"),
            token_event(5, 16, "text"),
            token_event(6, 17, "text"),
            token_event(7, 19, "text"),
            token_event(8, 18, "text"),
            span_event("tool_call", "end", 9, {"call_index": 0}),
            token_event(9, 13, "text"),
        ]
        output = [{"type": "function_call", "call_id": "call_real_1",
                   "name": "get_weather",
                   "arguments": '{"city":"Berlin","count":"3"}'}]
    else:
        events = [
            span_event("thinking", "start", 0, {"preopened": True}),
            token_event(0, 10, "reasoning"),
            token_event(1, 21, "reasoning"),
            token_event(2, 20, "reasoning"),
            span_event("thinking", "end", 3, {"closed": True}),
            token_event(3, 11, "reasoning"),
            token_event(4, 1, "text"),
            token_event(5, 2, "text"),
            token_event(6, 3, "text"),
            token_event(7, 4, "text"),
            token_event(8, 5, "text"),
            token_event(9, 6, "text"),
            token_event(10, 7, "text"),
        ]
        output = [{"type": "message", "role": "assistant",
                   "content": [{"type": "output_text", "text": "Hello Grüße"}]}]
    out = head + b"".join(sse(e) for e in events)
    for ev in (loop_guard, context, truncated):
        if ev is not None:
            out += sse(ev)
    out += sse({"type": "response.completed",
                "response": {"id": "resp_fixture", "created_at": 1700000000,
                             "status": "completed", "output": output,
                             "usage": {"input_tokens": 11, "output_tokens": 6,
                                       "total_tokens": 17}}})
    out += b"data: [DONE]\n\n"
    return out


def chat_text_stream(model):
    out = sse({"id": "chatcmpl-text", "object": "chat.completion.chunk",
               "model": model,
               "choices": [{"index": 0, "delta": {"role": "assistant"},
                            "finish_reason": None}]})
    out += sse({"id": "chatcmpl-text", "object": "chat.completion.chunk",
                "model": model,
                "choices": [{"index": 0, "delta": {"content": "text-mode answer"},
                             "finish_reason": None}]})
    out += sse({"id": "chatcmpl-text", "object": "chat.completion.chunk",
                "model": model,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
    out += b"data: [DONE]\n\n"
    return out


def start_engine(material, mode="ok", case="answer", caps=None, n_ctx=None,
                 context=None, loop_guard=None, truncated=None,
                 engine_version="1.4.0", contract=1, token_mode="ready",
                 param="arcint.renderer_mismatch", renderer_delay=0.0,
                 tokenize_count=None, tokenize_status=200, tokenize_delay=0.0,
                 tokenize_shape="count", kwargs_default=None):
    """(server, handler class). mode '409' refuses the token request before it is served.

    `caps` is the engine's /props chat_template_caps object; None leaves it out
    (a backend that reports no capability). `n_ctx` is reported as model.n_ctx.
    `engine_version`/`contract` are the /props handshake (build.version, arcint.v).
    `token_mode` is what /props says (`ready` for token mode). `param` is the
    409 error.param (arcint.renderer_hash vs arcint.render.prompt_sha256).
    `renderer_delay` sleeps before the renderer endpoint answers.
    `tokenize_count` is POST /tokenize's answer: an int, a callable of the parsed
    body, or None for 404 (an engine without the route). `tokenize_status` forces
    an error status (404/500/...). `tokenize_shape` is "count" ({tokens,count})
    or "tokens" (only {tokens}, the older content shape). `tokenize_delay` sleeps
    before answering, for the timeout path. Hits and headers are recorded on the
    handler class (`tokenize_hits`, `tokenize_auth`). `kwargs_default` is /props
    chat_template_kwargs_default (the unit's --chat-template-kwarg values), None leaves it out."""
    class E(BaseHTTPRequestHandler):
        paths = []
        bodies = {}
        all_bodies = []
        prop_hits = 0
        renderer_hits = 0
        tokenize_hits = 0
        tokenize_auth = []

        def log_message(self, *args):
            pass

        def _json(self, payload, status=200):
            raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            try:
                self.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError):
                pass   # der Client hat die Frist abgebrochen (Timeout-Test)

        def _sse(self, raw, status=200):
            self.send_response(status)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("X-Fixture", "engine")
            self.end_headers()
            try:
                self.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_GET(self):
            E.paths.append("GET " + self.path)
            if self.path == "/props":
                E.prop_hits += 1
                payload = {"token_mode": E.token_mode,
                           "renderer_hash": E.material["hash"],
                           "build": {"version": E.engine_version},
                           "arcint": {"v": E.contract}}
                if E.caps is not None:
                    payload["chat_template_caps"] = E.caps
                if E.kwargs_default is not None:
                    payload["chat_template_kwargs_default"] = E.kwargs_default
                if E.n_ctx is not None:
                    payload["model"] = {"n_ctx": E.n_ctx}
                self._json(payload)
            elif self.path == "/v1/responses/renderer":
                E.renderer_hits += 1
                if E.renderer_delay:
                    time.sleep(E.renderer_delay)
                self._json(E.material)
            elif self.path == "/v1/models":
                self._json({"data": [{"id": MODEL, "object": "model"}]})
            else:
                self._json({"error": "not found"}, 404)

        def do_POST(self):
            E.paths.append("POST " + self.path)
            raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            E.bodies[self.path] = raw
            E.all_bodies.append((self.path, raw))
            if self.path == "/tokenize":
                E.tokenize_hits += 1
                E.tokenize_auth.append(self.headers.get("Authorization"))
                if E.tokenize_delay:
                    time.sleep(E.tokenize_delay)
                if E.tokenize_status != 200:
                    self._json({"error": "tokenize refused"}, E.tokenize_status)
                    return
                if E.tokenize_count is None:
                    self._json({"error": "not found"}, 404)
                    return
                try:
                    doc = json.loads(raw)
                except ValueError:
                    doc = None
                n = (E.tokenize_count(doc) if callable(E.tokenize_count)
                     else E.tokenize_count)
                if E.tokenize_shape == "tokens":
                    self._json({"tokens": [0] * int(n)})
                else:
                    self._json({"tokens": [0] * int(n), "count": int(n)})
                return
            if self.path == "/v1/responses":
                if E.mode == "409":
                    try:
                        arc = (json.loads(raw) or {}).get("arcint")
                    except ValueError:
                        arc = None
                    # Nur die Token-Anfrage (arcint.token_mode) wird abgelehnt;
                    # der Text-Retry derselben Route bekommt den Strom.
                    if isinstance(arc, dict) and arc.get("token_mode"):
                        self._json({"error": {"type": "invalid_request",
                                              "code": "renderer_mismatch",
                                              "param": E.param,
                                              "message": "renderer mismatch"}}, 409)
                        return
                self._sse(engine_stream(E.case, E.context, E.loop_guard, E.truncated))
            elif self.path == "/v1/chat/completions":
                self._sse(chat_text_stream(MODEL))
            else:
                self._json({"error": "not found"}, 404)

    E.material = material
    E.mode = mode
    E.case = case
    E.context = context
    E.loop_guard = loop_guard
    E.truncated = truncated
    E.caps = caps
    E.kwargs_default = kwargs_default
    E.n_ctx = n_ctx
    E.engine_version = engine_version
    E.contract = contract
    E.token_mode = token_mode
    E.param = param
    E.renderer_delay = renderer_delay
    E.tokenize_count = tokenize_count
    E.tokenize_status = tokenize_status
    E.tokenize_delay = tokenize_delay
    E.tokenize_shape = tokenize_shape
    srv = ThreadingHTTPServer(("localhost", 0), E)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, E
