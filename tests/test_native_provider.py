#!/usr/bin/env python3
"""Native OpenAI-compatible providers (native-providers.json), e.g. a subscription endpoint.

RED when: a listed model is not offered or not routed; an unlisted one (image/audio) is; a
provider without its key variable is offered or routed; the id prefix is not stripped or the
path does not land under the base path; the Bearer key is missing; a "[free]" tag lets a paid
provider pass the regulator as free; mode 'free'/'local' does not block or hide it; 'cheap'
lets it through without a stated price, or blocks it with one below the limit; a provider 4xx
does not pass through; a stream is not relayed. A bad config entry crashes the loader.
"""
import http.server
import io
import socket
import json
import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _lade import lade  # noqa: E402

lp = lade()
fail = 0
checks = 0


def check(cond, msg):
    global fail, checks
    checks += 1
    if not cond:
        print("ROT:", msg)
        fail += 1


# --- a fake provider -----------------------------------------------------------------------
class P(http.server.BaseHTTPRequestHandler):
    seen = []
    status = 200

    def log_message(self, *a):
        pass

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        body = json.loads(raw)
        P.seen.append({"path": self.path, "auth": self.headers.get("Authorization"), "body": body})
        if P.status != 200:
            out = json.dumps({"error": {"message": "quota"}}).encode()
            self.send_response(P.status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            if P.status == 429:
                self.send_header("Retry-After", "9")
            self.end_headers()
            self.wfile.write(out)
            return
        if body.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for piece in ("Hal", "lo"):
                chunk = {"id": "x", "object": "chat.completion.chunk", "model": body["model"],
                         "choices": [{"index": 0, "delta": {"content": piece}, "finish_reason": None}]}
                self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
            self.wfile.write(b'data: {"id":"x","object":"chat.completion.chunk","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n')
            return
        out = json.dumps({"id": "x", "object": "chat.completion", "model": body["model"],
                          "choices": [{"index": 0, "message": {"role": "assistant", "content": "Hallo"},
                                       "finish_reason": "stop"}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), P)
threading.Thread(target=srv.serve_forever, daemon=True).start()
port = srv.server_address[1]

cfg = {
    "qwenplan": {"base_url": f"http://127.0.0.1:{port}/compatible-mode/v1", "key_env": "TEST_QWEN_KEY",
                 "cost_class": "paid", "owned_by": "qwen-token-plan",
                 "models": {"qwen3.8-max": {"ctx": 262144}, "qwen3.8-flash": {"prompt_price": 0.0000001}}},
    "BAD prefix": {"base_url": "x"},
    "nokey": {"base_url": "https://example.invalid/v1", "cost_class": "paid", "models": {"m": {}}},
    "badurl": {"base_url": "ftp://x/v1", "key_env": "K", "models": {"m": {}}},
    "filekey": {"base_url": f"http://127.0.0.1:{port}/fk/v1", "key_file": os.path.join(lp.CONFIG_DIR, "fk.key"),
                "models": {"m1": {}}},
    "badfile": {"base_url": "https://example.invalid/v1", "key_file": "/nonexistent/key", "models": {"m": {}}},
    "qwen": {"base_url": "https://example.invalid/v1", "key_env": "TEST_QWEN_KEY", "models": {"m": {}}},
    "opencode": {"base_url": "https://example.invalid/v1", "key_env": "TEST_QWEN_KEY", "models": {"m": {}}},
    "crlf": {"base_url": "https://example.invalid/v1", "key_file": os.path.join(lp.CONFIG_DIR, "crlf.key"),
             "models": {"m": {}}},
    "gratis": {"base_url": "https://example.invalid/v1", "key_env": "TEST_QWEN_KEY", "cost_class": "free",
               "models": {"g": {}}},
    "closed": {"base_url": "http://127.0.0.1:1/v1", "key_env": "TEST_CLOSED_KEY", "models": {"c": {}}},
    "dashna": {"base_url": "https://example.invalid/v1", "key_env": "TEST_QWEN_KEY", "models": {"x-na": {}}},
}
with open(os.path.join(lp.CONFIG_DIR, "crlf.key"), "w") as f:
    f.write("sk-a\r\nb\n")
with open(os.path.join(lp.CONFIG_DIR, "fk.key"), "w") as f:
    f.write("sk-file-456\n")
with open(os.path.join(lp.CONFIG_DIR, "native-providers.json"), "w") as f:
    json.dump(cfg, f)
lp.NATIVE_PROVIDERS = lp._load_native_providers()
check(set(lp.NATIVE_PROVIDERS) == {"qwenplan", "filekey", "gratis", "closed", "dashna"},
      f"loader kept {sorted(lp.NATIVE_PROVIDERS)} (bad, shadowing and CRLF-key entries must go)")
check(lp._native_of("filekey/m1") is not None and lp._native_key(lp.NATIVE_PROVIDERS["filekey"]) == "sk-file-456",
      "key_file not read (or newline kept)")
check("sk-file-456" not in "".join(lp._testlog) and "sk-file-456" not in repr(sorted(lp.NATIVE_PROVIDERS)),
      "key value in the log or in the provider names")

# --- without the key: neither offered nor routed --------------------------------------------
os.environ.pop("TEST_QWEN_KEY", None)
check(lp._native_of("qwenplan/qwen3.8-max") is None, "routed without the key variable")
os.environ["TEST_QWEN_KEY"] = "sk-test-123"

# --- identity, routing, classification -------------------------------------------------------
check(lp._native_of("qwenplan/qwen3.8-max")[2] == "qwen3.8-max", "listed model not recognised")
check(lp._native_of("qwenplan/wan2.7-image") is None, "unlisted model recognised")
check(lp._native_of("other/qwen3.8-max") is None, "foreign prefix recognised")
check(lp._ziel("paid", "qwenplan/qwen3.8-max") == "native", "not routed native")
check(lp._ziel("local", "qwenplan/qwen3.8-max") == "local", "kind local must stay local")
for raw in ("[$] qwenplan/qwen3.8-max", "[free] qwenplan/qwen3.8-max", "qwenplan/qwen3.8-max"):
    check(lp._classify(raw, lp.strip_tag(raw)) == "paid", f"{raw!r} not classified paid")

# --- regulator -------------------------------------------------------------------------------
def blocked(mode, model, price_limit=0.5):
    lp._policy = lambda: {"mode": mode, "cheap_max_prompt_price": price_limit}
    return lp._policy_blocked(model, lp.strip_tag(model))[0]


check(not blocked("max", "[$] qwenplan/qwen3.8-max"), "max blocks it")
check(blocked("free", "[free] qwenplan/qwen3.8-max"), "free mode lets a paid native model through via its tag")
check(blocked("local", "qwenplan/qwen3.8-max"), "local mode lets it through")
check(blocked("cheap", "qwenplan/qwen3.8-max"), "cheap passes a model without a stated price")
check(not blocked("cheap", "qwenplan/qwen3.8-flash"), "cheap blocks a model priced below the limit")
check(blocked("cheap", "qwenplan/qwen3.8-flash", price_limit=0.01), "cheap passes a model above the limit")

# --- catalog -----------------------------------------------------------------------------------
class _W:
    def __init__(self, o):
        self.o = o

    def write(self, b):
        self.o.body = b


class _Fake:
    def __init__(self):
        self.body = None
        self.wfile = _W(self)

    def send_response(self, c):
        pass

    def send_header(self, k, v):
        pass

    def end_headers(self):
        pass


def catalog(mode):
    lp.LOCAL_BACKENDS = []
    lp._aktiver_modus = lambda: mode
    lp.discover_cloud = lambda: None
    lp._merge_catalogs = lambda: {}
    fake = _Fake()
    lp.Handler._handle_models(fake, 0.0)
    return {e["id"]: e for e in json.loads(fake.body)["data"]}


cat = catalog("max")
e = cat.get("[$] qwenplan/qwen3.8-max")
check(e is not None and e.get("owned_by") == "qwen-token-plan" and e.get("cost_class") == "paid"
      and e.get("ctx") == 262144, f"catalog entry: {e}")
check(not any("wan2.7" in k or "nokey" in k for k in cat), "unlisted or keyless model in the catalog")
check(not any("qwenplan" in k for k in catalog("free")), "free mode lists the paid provider")
os.environ.pop("TEST_QWEN_KEY")
check(not any("qwenplan" in k for k in catalog("max")), "listed although the key variable is empty")
os.environ["TEST_QWEN_KEY"] = "sk-test-123"


# --- forwarding --------------------------------------------------------------------------------
class H(lp.Handler):
    def __init__(self):
        self.wfile = io.BytesIO()
        self.status = None
        self.kopf = {}
        self.connection, self._peer = socket.socketpair()

    def send_response(self, status, *a, **k):
        self.status = status

    def send_header(self, key, value):
        self.kopf[key] = value

    def end_headers(self):
        pass


def send(stream, status=200, model="qwenplan/qwen3.8-max", method="POST", path="/v1/chat/completions", extra=None):
    P.seen.clear()
    P.status = status
    h = H()
    b = {"model": model, "stream": stream, "messages": [{"role": "user", "content": "Hi"}]}
    b.update(extra or {})
    h._proxy_native(method, path, json.dumps(b).encode(), "", 0.0, lp._native_of(model), is_streaming=stream)
    return h


h = send(False)
s = P.seen[0] if P.seen else {}
check(s.get("path") == "/compatible-mode/v1/chat/completions", f"upstream path: {s.get('path')}")
check(s.get("auth") == "Bearer sk-test-123", "Bearer key not sent")
check(s.get("body", {}).get("model") == "qwen3.8-max", f"prefix not stripped: {s.get('body', {}).get('model')}")
out = h.wfile.getvalue()
check(h.status == 200 and b'"Hallo"' in out, f"non-stream answer: {h.status} {out[:120]!r}")

h = send(True)
out = h.wfile.getvalue().decode(errors="replace")
check(out.count("data:") >= 3 and "Hal" in out and "lo" in out and "[DONE]" in out, f"stream not relayed: {out[:200]!r}")
check(h.kopf.get("Content-Type", "").startswith("text/event-stream"), f"stream content type: {h.kopf}")

# pinned path: nothing but POST /v1/chat/completions reaches the provider (with the key)
for meth, pth in (("POST", "/v1/embeddings"), ("POST", "/v1/responses"), ("GET", "/v1/chat/completions"),
                  ("POST", "/v1/../x")):
    h = send(False, method=meth, path=pth)
    check(h.status == 404 and not P.seen, f"{meth} {pth} forwarded: {h.status} {P.seen[:1]}")

# max_tokens that is not a positive int is dropped (as Zen)
h = send(False, extra={"max_tokens": 0})
check(P.seen and "max_tokens" not in P.seen[0]["body"], f"max_tokens 0 forwarded: {P.seen[:1]}")
h = send(False, extra={"max_tokens": 100})
check(P.seen and P.seen[0]["body"].get("max_tokens") == 100, "a valid max_tokens was dropped")

# failure path: the key is in neither the client body nor the log
os.environ["TEST_CLOSED_KEY"] = "sk-closed-789"
h = send(False, model="closed/c")
check(h.status == 502 and b"sk-closed-789" not in h.wfile.getvalue(), f"key in the 502 body: {h.wfile.getvalue()[:200]!r}")
check(not any("sk-closed-789" in l for l in lp._testlog), "key in the log on a failed connect")
os.environ["TEST_CLOSED_KEY"] = "sk-x\r\ny"
check(lp._native_of("closed/c") is None, "a CR/LF env key counts as set")
os.environ.pop("TEST_CLOSED_KEY")

# -na: a native id that really ends in -na stays itself
check(lp._anfrage_id("dashna/x-na") == "dashna/x-na" if hasattr(lp, "_anfrage_id") else True,
      "native -na id mangled")

# free provider under mode free: listed and passed
check(any(k == "[free] gratis/g" for k in catalog("free")), "free native provider missing under mode free")
check(not blocked("free", "gratis/g"), "free native provider blocked under mode free")

# cheap lists only what it would pass
lp._policy = lambda: {"mode": "cheap", "cheap_max_prompt_price": 0.5}
c = catalog("cheap")
check("[$] qwenplan/qwen3.8-flash" in c and "[$] qwenplan/qwen3.8-max" not in c,
      f"cheap catalog: {[k for k in c if 'qwenplan' in k]}")

h = send(False, status=429)
check(h.status == 429 and h.kopf.get("Retry-After") == "9", f"429 not passed through: {h.status} {h.kopf}")
h = send(False, status=500)
check(h.status == 502, f"5xx not mapped to 502: {h.status}")

# key_file provider: its key is the one sent
P.seen.clear(); P.status = 200
hf = H()
hf._proxy_native("POST", "/v1/chat/completions", json.dumps({"model": "filekey/m1", "messages": []}).encode(),
                 "", 0.0, lp._native_of("filekey/m1"), is_streaming=False)
check(P.seen and P.seen[0]["auth"] == "Bearer sk-file-456" and P.seen[0]["path"] == "/fk/v1/chat/completions",
      f"key_file provider request: {P.seen[:1]}")
check(not any("sk-test-123" in l or "sk-file-456" in l for l in lp._testlog), "a key value reached the log")

# --- SIGHUP reload picks the file up -------------------------------------------------------------
os.remove(os.path.join(lp.CONFIG_DIR, "native-providers.json"))
check(lp._load_native_providers() == {}, "missing file does not give {}")

srv.shutdown()
print(f"{checks - fail}/{checks} checks ok")
sys.exit(1 if fail else 0)
