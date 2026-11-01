#!/usr/bin/env python3
"""Token mode: Chat Completions from token mode: one normalised stream, no salvage.

ROT, wenn der Token-Pfad weiter an /v1/chat/completions geht, reasoning_content
oder der Antworttext aus den falschen Feldern kommt, ein echter tool_call ohne
call_id ankommt, ein Material-Marker im sichtbaren Text steht, der Token-Strom
durch ReasoningSeam/ChatMLGuard/ToolTextSalvage laeuft, oder der
Responses-Passthrough die arcint-Events nicht byte-gleich weiterreicht.
"""
import io
import json
import os
import socket
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _lade import lade  # noqa: E402
from _feyntalk_fixture import (MODEL, THINK_CLOSE, THINK_OPEN, TOOLCALL_CLOSE,
                               TOOLCALL_OPEN, engine_stream, make_material,
                               start_engine)  # noqa: E402
from feyntalk_render.render import render_prompt  # noqa: E402

lp = lade()
fail = 0
checks = 0


def check(cond, msg):
    global fail, checks
    checks += 1
    if not cond:
        print("ROT:", msg)
        fail += 1


class H(lp.Handler):
    def __init__(self, wfile, conn):
        self.wfile, self.connection = wfile, conn
        self.headers = {"get": lambda k, d=None: d, "Content-Type": "application/json"}

    def send_response(self, *a, **k):
        pass

    def send_header(self, *a, **k):
        pass

    def end_headers(self):
        pass


def reset(name, host, port, setting="tokens"):
    lp.LOCAL_BACKENDS[:] = [(name, host, port)]
    lp.BACKEND_FEYNTALK.clear()
    lp.BACKEND_FEYNTALK[name] = setting
    lp._FEYNTALK_WARNED.clear()
    with lp.FEYNTALK_CACHE_LOCK:
        lp.FEYNTALK_CACHE.clear()
        lp.FEYNTALK_MATERIALS.clear()
    lp._build_sems({name: 4})
    lp._disc_cache.clear()


def run_chat(host, port, body, is_streaming=True):
    a, b = socket.socketpair()
    h = H(io.BytesIO(), a)
    try:
        h._proxy_local("POST", "/v1/chat/completions", json.dumps(body).encode(),
                       "", 0.0, model=MODEL, is_streaming=is_streaming)
        return h.wfile.getvalue()
    finally:
        a.close()
        b.close()


def parse_chat(out):
    content = ""
    reasoning = ""
    calls = []
    finish = []
    for block in out.decode("utf-8").split("\n\n"):
        line = block.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload == "[DONE]":
            continue
        j = json.loads(payload)
        ch = j["choices"][0]
        delta = ch.get("delta") or {}
        content += delta.get("content") or ""
        reasoning += delta.get("reasoning_content") or ""
        calls += delta.get("tool_calls") or []
        if ch.get("finish_reason"):
            finish.append(ch["finish_reason"])
    return content, reasoning, calls, finish


def chat_body(stream=True):
    return {"model": MODEL, "stream": stream,
            "messages": [{"role": "system", "content": "sys"},
                         {"role": "user", "content": "Hi"}]}


# 1) Antwort: reasoning_content und content, keine Marker im sichtbaren Text
srv, E = start_engine(make_material(), case="answer")
reset("eng", *srv.server_address[:2])
out = run_chat(*srv.server_address[:2], chat_body())
content, reasoning, calls, finish = parse_chat(out)
check(E.paths.count("POST /v1/responses") == 1, f"Token-Pfad ging nicht an /v1/responses: {E.paths}")
check("POST /v1/chat/completions" not in E.paths, f"Token-Pfad ging an die Chat-Route: {E.paths}")
check(reasoning == "Why?", f"reasoning_content: {reasoning!r}")
check(content == "Hello Grüße", f"content: {content!r}")
check(calls == [], f"unerwartete tool_calls: {calls}")
check(finish == ["stop"], f"finish_reason: {finish}")
for marker in (THINK_OPEN, THINK_CLOSE, TOOLCALL_OPEN, TOOLCALL_CLOSE):
    check(marker.encode() not in out, f"Marker im sichtbaren Strom: {marker!r}")

body = E.bodies.get("/v1/responses")
check(body is not None, "kein Token-Request aufgezeichnet")
tok = json.loads(body) if body else {}
check(tok.get("arcint", {}).get("token_mode") is True, f"arcint.token_mode fehlt: {tok.get('arcint')}")
check(tok.get("arcint", {}).get("renderer_hash") == E.material["hash"],
      f"renderer_hash falsch: {tok.get('arcint')}")
expected = render_prompt(E.material, chat_body()["messages"], extra_context={})
check(tok.get("arcint", {}).get("render", {}).get("prompt_sha256") == expected.prompt_sha256,
      f"prompt_sha256 falsch: {tok.get('arcint')}")

# 2) echter tool_call: Name/Argumente aus dem Span, id aus dem function_call-Item
srv.shutdown()
srv, E = start_engine(make_material(), case="tool")
reset("eng", *srv.server_address[:2])
out = run_chat(*srv.server_address[:2], chat_body())
content, reasoning, calls, finish = parse_chat(out)
check(reasoning == "Why?", f"tool-Durchlauf reasoning: {reasoning!r}")
check(content == "", f"tool-Durchlauf content: {content!r}")
check(len(calls) == 1, f"{len(calls)} tool_calls statt 1: {calls}")
if calls:
    f = calls[0]["function"]
    check(calls[0].get("id") == "call_real_1", f"call_id nicht aus dem Item: {calls[0]}")
    check(f["name"] == "get_weather", f"Tool-Name: {f['name']}")
    check(json.loads(f["arguments"]) == {"city": "Berlin", "count": "3"},
          f"Argumente: {f['arguments']}")
    check("get_weather" not in content, "Tool-Body leakte in den Antworttext")
check(finish == ["tool_calls"], f"finish_reason: {finish}")

# 3) Nicht-Strom: derselbe normalisierte Strom als chat.completion
srv.shutdown()
srv, E = start_engine(make_material(), case="answer")
reset("eng", *srv.server_address[:2])
out = run_chat(*srv.server_address[:2], chat_body(stream=False), is_streaming=False)
j = json.loads(out)
m = j["choices"][0]["message"]
check(j["object"] == "chat.completion", f"object: {j.get('object')}")
check(m.get("reasoning_content") == "Why?", f"Nicht-Strom reasoning: {m.get('reasoning_content')}")
check(m.get("content") == "Hello Grüße", f"Nicht-Strom content: {m.get('content')}")
check(j["choices"][0]["finish_reason"] == "stop", f"Nicht-Strom finish: {j['choices'][0]}")
check(j.get("usage", {}).get("prompt_tokens") == 11, f"usage: {j.get('usage')}")

# 3b) Nicht-Strom mit tool_call: dieselbe id aus dem function_call-Item
srv.shutdown()
srv, E = start_engine(make_material(), case="tool")
reset("eng", *srv.server_address[:2])
out = run_chat(*srv.server_address[:2], chat_body(stream=False), is_streaming=False)
j = json.loads(out)
m = j["choices"][0]["message"]
tc = m.get("tool_calls") or []
check(len(tc) == 1 and tc[0].get("id") == "call_real_1",
      f"Nicht-Strom tool_calls: {tc}")
check(m.get("content") is None, f"Nicht-Strom content neben tool_calls: {m.get('content')!r}")
check(j["choices"][0]["finish_reason"] == "tool_calls",
      f"Nicht-Strom tool finish: {j['choices'][0]}")

# 4) Token-Frames laufen nie durch die Rettungsklassen
srv.shutdown()
srv, E = start_engine(make_material(), case="tool")
reset("eng", *srv.server_address[:2])
orig = (lp.ReasoningSeam, lp.ChatMLGuard, lp.ToolTextSalvage)

class Boom:
    def __init__(self, *a, **k):
        raise AssertionError("Token-Frames liefen durch eine Rettungsklasse")

lp.ReasoningSeam = lp.ChatMLGuard = lp.ToolTextSalvage = Boom
try:
    out = run_chat(*srv.server_address[:2], chat_body())
finally:
    lp.ReasoningSeam, lp.ChatMLGuard, lp.ToolTextSalvage = orig
check(b"call_real_1" in out, "tool_call im Strom fehlt nach dem Bypass-Test")

# 5) /v1/responses Passthrough: Token- und Span-Events byte-gleich
srv.shutdown()
srv, E = start_engine(make_material(), case="answer")
reset("eng", *srv.server_address[:2])
req_body = {"model": MODEL, "stream": True,
            "input": [{"type": "message", "role": "user", "content": "Hi"}]}
a, b = socket.socketpair()
h = H(io.BytesIO(), a)
try:
    h._proxy_local("POST", "/v1/responses", json.dumps(req_body).encode(), "", 0.0,
                   model=MODEL, is_streaming=True)
    out = h.wfile.getvalue()
finally:
    a.close()
    b.close()
check(out == engine_stream("answer"), "Responses-Passthrough ist nicht byte-gleich")
check(b"arcint:token" in out and b"arcint:span" in out,
      "arcint-Events fehlen im Responses-Passthrough")
tok = json.loads(E.bodies.get("/v1/responses") or b"{}")
check(tok.get("arcint", {}).get("token_mode") is True, "Responses-Request ohne arcint.token_mode")

srv.shutdown()
print(f"GRUEN: {checks} Faelle" if not fail else f"ROT ({fail} von {checks})")
sys.exit(1 if fail else 0)
