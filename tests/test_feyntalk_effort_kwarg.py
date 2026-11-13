#!/usr/bin/env python3
"""Token mode: chat_template_kwargs.reasoning_effort wins over the top-level reasoning_effort.

RED when the kwarg level does not reach the engine as reasoning.effort, when a kwarg "none" leaves thinking
on, or when the text path's thinking switch (_local_thinking_switch, run by _proxy BEFORE the token path)
reads only the top-level field and so overrides the kwarg.
"""
import io
import json
import os
import socket
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _lade import lade  # noqa: E402
from _feyntalk_fixture import MODEL, make_material, start_engine  # noqa: E402

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


def reset(name, host, port):
    lp.LOCAL_BACKENDS[:] = [(name, host, port)]
    lp.BACKEND_FEYNTALK.clear()
    lp.BACKEND_FEYNTALK[name] = "tokens"
    lp._FEYNTALK_WARNED.clear()
    with lp.FEYNTALK_CACHE_LOCK:
        lp.FEYNTALK_CACHE.clear()
        lp.FEYNTALK_MATERIALS.clear()
    lp._build_sems({name: 4})
    lp._disc_cache.clear()


def run_chat(body, is_streaming=True):
    a, b = socket.socketpair()
    h = H(io.BytesIO(), a)
    try:
        h._proxy_local("POST", "/v1/chat/completions", json.dumps(body).encode(),
                       "", 0.0, model=MODEL, is_streaming=is_streaming)
        return h.wfile.getvalue()
    finally:
        a.close()
        b.close()


def parse_stream(out):
    content_deltas, reasoning, calls, finish = [], "", [], []
    for block in out.decode("utf-8").split("\n\n"):
        line = block.strip()
        if not line.startswith("data:") or line[5:].strip() == "[DONE]":
            continue
        ch = json.loads(line[5:].strip())["choices"][0]
        delta = ch.get("delta") or {}
        if delta.get("content"):
            content_deltas.append(delta["content"])
        reasoning += delta.get("reasoning_content") or ""
        calls += delta.get("tool_calls") or []
        if ch.get("finish_reason"):
            finish.append(ch["finish_reason"])
    return content_deltas, reasoning, calls, finish


def body(stream=True, kwargs=None):
    b = {"model": MODEL, "stream": stream,
         "messages": [{"role": "user", "content": "Hi"}]}
    if kwargs is not None:
        b["chat_template_kwargs"] = kwargs
    return b


def turn(case, kwargs_default, request_kwargs, stream):
    srv, E = start_engine(make_material(), case=case, kwargs_default=kwargs_default)
    try:
        reset("eng", *srv.server_address[:2])
        return run_chat(body(stream, request_kwargs), is_streaming=stream)
    finally:
        srv.shutdown()



# chat_template_kwargs.reasoning_effort wins over the top-level field (as the engine's chat route) and
# reaches the engine as reasoning.effort in token mode (Dory's per-request loop count reads it).
E = lp._feyntalk_enable
check(E(MODEL, {"chat_template_kwargs": {"reasoning_effort": "low"}, "reasoning_effort": "high"}, True) == (True, "low"),
      "kwarg effort does not win")
check(E(MODEL, {"chat_template_kwargs": {"reasoning_effort": "none"}}, True) == (False, "none"), "kwarg none does not switch off")
check(E(MODEL, {"chat_template_kwargs": {"reasoning_effort": ""}, "reasoning_effort": "medium"}, True) == (True, "medium"),
      "empty kwarg effort overrides the field")
# the live order: _proxy runs _local_thinking_switch first, then the token path reads the result
for kwargs, top, want in (({"reasoning_effort": "none"}, None, (False, "none")),
                          ({"reasoning_effort": "none"}, "high", (False, "none")),
                          ({"reasoning_effort": "low"}, "none", (True, "low"))):
    b = {"chat_template_kwargs": dict(kwargs)}
    if top is not None:
        b["reasoning_effort"] = top
    lp._local_thinking_switch(b)
    check(E(MODEL, b, True) == want, f"after the thinking switch {kwargs}/{top}: {E(MODEL, b, True)} want {want}")
for kwargs, top, want in (({"reasoning_effort": "low"}, "high", "low"), (None, "high", "high")):
    srv, Eng = start_engine(make_material(), case="answer")
    try:
        reset("eng", *srv.server_address[:2])
        b = {"model": MODEL, "stream": True, "messages": [{"role": "user", "content": "Hi"}], "reasoning_effort": top}
        if kwargs:
            b["chat_template_kwargs"] = kwargs
        run_chat(b, is_streaming=True)
        sent = json.loads(Eng.bodies.get("/v1/responses") or "{}")
        check((sent.get("reasoning") or {}).get("effort") == want, f"engine got reasoning {sent.get('reasoning')} want {want}")
    finally:
        srv.shutdown()

# Dory's loop count (engine: response.arcint.dory on the terminal) reaches the chat client as arcint.dory
for stream in (True, False):
    srv, Eng = start_engine(make_material(), case="answer", dory={"loops": 4, "reprefill": True})
    try:
        reset("eng", *srv.server_address[:2])
        out = run_chat({"model": MODEL, "stream": stream, "messages": [{"role": "user", "content": "Hi"}]}, is_streaming=stream)
        txt = out.decode("utf-8")
        if stream:
            finals = [json.loads(l[5:]) for l in (b.strip() for b in txt.split("\n\n")) if l.startswith("data:") and "finish_reason\": \"" in l]
            got = (finals[-1].get("arcint") or {}).get("dory") if finals else None
        else:
            got = (json.loads(txt).get("arcint") or {}).get("dory")
        check(got == {"loops": 4, "reprefill": True}, f"arcint.dory {'stream' if stream else 'plain'}: {got}")
    finally:
        srv.shutdown()

print(f"{checks - fail}/{checks} checks ok")
sys.exit(1 if fail else 0)
