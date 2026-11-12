#!/usr/bin/env python3
"""Token mode: force_nonempty_content, applied by the proxy (the engine exempts token turns).

RED when a cut thinking block (reasoning only, blank answer) does not become the content
under the unit default (/props chat_template_kwargs_default) or a request kwarg; when an
explicit request false does not turn it off; when it fires without the default or kwarg;
when a real tool-call turn (blank text by construction) is promoted; when a normal
answer changes; or when the promoted stream carries a tool-call delta. Reference:
arcint src/core/chat.cpp promote_reasoning_to_content and the engine's chat stream
(Dory capture 2026-11-11: reasoning deltas, then ONE content delta, finish length).
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


ON = {"force_nonempty_content": True}

# 1) cut thinking + unit default on, stream: reasoning deltas, then ONE content delta = the reasoning
deltas, reasoning, calls, finish = parse_stream(turn("cut", ON, None, True))
check(reasoning == "Why?", f"1 reasoning deltas: {reasoning!r}")
check(deltas == ["Why?"], f"1 expected one content delta 'Why?': {deltas!r}")
check(calls == [], f"1 tool-call delta on a promoted turn: {calls}")
check(finish == ["length"], f"1 finish_reason: {finish}")

# 2) the same, non-stream: content = reasoning, no reasoning_content, no tool_calls
j = json.loads(turn("cut", ON, None, False))
m = j["choices"][0]["message"]
check(m.get("content") == "Why?", f"2 content: {m.get('content')!r}")
check("reasoning_content" not in m, f"2 reasoning_content kept after promotion: {m}")
check("tool_calls" not in m, f"2 tool_calls on a promoted turn: {m}")
check(j["choices"][0]["finish_reason"] == "length", f"2 finish: {j['choices'][0]}")

# 3) request kwarg false beats the unit default
deltas, reasoning, calls, finish = parse_stream(turn("cut", ON, {"force_nonempty_content": False}, True))
check(deltas == [], f"3 promoted although the request said false: {deltas!r}")
j = json.loads(turn("cut", ON, {"force_nonempty_content": False}, False))
m = j["choices"][0]["message"]
check(m.get("content") is None and m.get("reasoning_content") == "Why?", f"3 non-stream: {m}")

# 4) no unit default, no kwarg: nothing promoted
deltas, _, _, _ = parse_stream(turn("cut", None, None, True))
check(deltas == [], f"4 promoted without default or kwarg: {deltas!r}")

# 5) no unit default, request kwarg true: promoted
deltas, _, _, _ = parse_stream(turn("cut", None, ON, True))
check(deltas == ["Why?"], f"5 request kwarg true did not promote: {deltas!r}")

# 6) a real tool call (blank text by construction) is never promoted
deltas, reasoning, calls, finish = parse_stream(turn("tool", ON, None, True))
check(deltas == [], f"6 tool turn promoted: {deltas!r}")
check(len(calls) == 1 and finish == ["tool_calls"], f"6 call lost: {calls} {finish}")
j = json.loads(turn("tool", ON, None, False))
m = j["choices"][0]["message"]
check(m.get("content") is None and m.get("reasoning_content") == "Why?" and len(m.get("tool_calls") or []) == 1,
      f"6 non-stream tool turn changed: {m}")

# 1b) order: the content delta comes after the last reasoning delta and before the finish chunk
seq = []
for block in turn("cut", ON, None, True).decode("utf-8").split("\n\n"):
    line = block.strip()
    if not line.startswith("data:") or line[5:].strip() == "[DONE]":
        continue
    ch = json.loads(line[5:].strip())["choices"][0]
    d = ch.get("delta") or {}
    seq.append("R" if d.get("reasoning_content") else "C" if d.get("content") else
               "F" if ch.get("finish_reason") else "-")
check("C" in seq and "F" in seq and max(i for i, k in enumerate(seq) if k == "R") < seq.index("C") < seq.index("F"),
      f"1b delta order: {seq}")

# 4b/5b) the same two cases non-stream
m = json.loads(turn("cut", None, None, False))["choices"][0]["message"]
check(m.get("content") is None and m.get("reasoning_content") == "Why?", f"4b non-stream promoted: {m}")
m = json.loads(turn("cut", None, ON, False))["choices"][0]["message"]
check(m.get("content") == "Why?" and "reasoning_content" not in m, f"5b non-stream not promoted: {m}")

# 8) a request kwarg that is present but not JSON true turns it off, as in the engine
for odd in (1, "true", None):
    deltas, _, _, _ = parse_stream(turn("cut", ON, {"force_nonempty_content": odd}, True))
    check(deltas == [], f"8 request kwarg {odd!r} promoted: {deltas!r}")

# 9) a unit default that is not an object: no crash, no promotion
for odd in ([], "x", 3):
    deltas, reasoning, _, finish = parse_stream(turn("cut", odd, None, True))
    check(deltas == [] and reasoning == "Why?" and finish == ["length"], f"9 kwargs_default {odd!r}: {deltas!r} {finish}")

# 10) the blank rule itself (arcint text_is_blank: ASCII whitespace only)
P = lp._feyntalk_promote
check(P(True, "r", " \n\t", []) == ("", "r", True), "10 ASCII-blank text not treated as blank")
check(P(True, " \n", "", [])[2] is False, "10 blank reasoning promoted")
check(P(True, "r", "\u00a0", [])[2] is False, "10 NBSP text treated as blank (the engine does not)")
check(P(True, "r", "", ["call"])[2] is False, "10 a turn with a call promoted")
check(P(False, "r", "", [])[2] is False, "10 promoted with the rule off")

# 7) a normal answer is untouched
deltas, reasoning, calls, finish = parse_stream(turn("answer", ON, None, True))
check("".join(deltas) == "Hello Grüße" and reasoning == "Why?", f"7 answer changed: {deltas!r} {reasoning!r}")

print(f"{checks - fail}/{checks} checks ok")
sys.exit(1 if fail else 0)
