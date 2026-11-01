#!/usr/bin/env python3
"""Thinking replay (preserve_thinking): Denken beim Replay behalten, Stufe-1-Kompaktierung.

ROT, wenn eine fehlende Faehigkeit die Anfrage aendert, ein Client-Override
nicht gewinnt, ein Nicht-bool nicht als 400 endet, der Default das Denken
nicht behaelt, der Schwellwert es nicht fallen laesst, die Flagge nicht in
Render-Kontext und Upstream-Body steht, oder der 409-Textretry sie verliert.
"""
import io
import json
import os
import socket
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _lade import lade  # noqa: E402
from _feyntalk_fixture import (MODEL, make_material, start_engine,  # noqa: E402
                               template_text_preserve)
from feyntalk_render.render import render_prompt  # noqa: E402

lp = lade()
fail = 0
checks = 0
CAPS = {"supports_preserve_thinking": True, "supports_object_arguments": False}


def check(cond, msg):
    global fail, checks
    checks += 1
    if not cond:
        print("ROT:", msg)
        fail += 1


class H(lp.Handler):
    def __init__(self, wfile, conn):
        self.wfile, self.connection = wfile, conn
        self.status = None
        self.headers = {"get": lambda k, d=None: d,
                        "Content-Type": "application/json"}

    def send_response(self, code, *a, **k):
        self.status = code

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


def chat_body(**extra):
    body = {"model": MODEL, "stream": True,
            "messages": [{"role": "system", "content": "sys"},
                         {"role": "user", "content": "Hi"}]}
    body.update(extra)
    return body


def run(host, port, body, path="/v1/chat/completions"):
    a, b = socket.socketpair()
    h = H(io.BytesIO(), a)
    try:
        h._proxy_local("POST", path, json.dumps(body).encode(), "", 0.0,
                       model=MODEL, is_streaming=True)
        return h.wfile.getvalue(), h.status
    finally:
        a.close()
        b.close()


def expected_hash(material, preserve):
    extra = {"preserve_thinking": preserve} if preserve is not None else {}
    return render_prompt(material, chat_body()["messages"],
                         extra_context=extra).prompt_sha256


def upstream_arcint(E):
    raw = E.bodies.get("/v1/responses")
    if raw is None:
        return None
    try:
        return json.loads(raw).get("arcint")
    except ValueError:
        return None


# 1) Faehigkeit fehlt: byte-gleiche Anfrage, keine Flagge, kein preserve im Hash.
material = make_material()
srv, E = start_engine(material, caps=None)
reset("eng", *srv.server_address[:2])
run(*srv.server_address[:2], chat_body())
arc = upstream_arcint(E)
check(arc is not None and "preserve_thinking" not in arc,
      f"ohne Faehigkeit wurde preserve_thinking gesendet: {arc}")
check(arc is not None and arc["render"]["prompt_sha256"] == expected_hash(material, None),
      "Hash ohne Faehigkeit weicht ab")
srv.shutdown()

# 2) Default (Faehigkeit da, viel Kontext): behalten.
material = make_material(template=template_text_preserve())
srv, E = start_engine(material, caps=CAPS, n_ctx=100000)
reset("eng", *srv.server_address[:2])
run(*srv.server_address[:2], chat_body())
arc = upstream_arcint(E)
check(arc is not None and arc.get("preserve_thinking") is True,
      f"Default behielt das Denken nicht: {arc}")
check(arc is not None and arc["render"]["prompt_sha256"] == expected_hash(material, True),
      "Hash preserve=true weicht ab")
srv.shutdown()

# 3) Schwelle: knapper Kontext -> Denken faellt als erste Stufe.
srv, E = start_engine(material, caps=CAPS, n_ctx=9)   # 9 * 0.7 = 6,3 Tokens
reset("eng", *srv.server_address[:2])
run(*srv.server_address[:2], chat_body())
arc = upstream_arcint(E)
check(arc is not None and arc.get("preserve_thinking") is False,
      f"Schwelle liess das Denken nicht fallen: {arc}")
check(arc is not None and arc["render"]["prompt_sha256"] == expected_hash(material, False),
      "Hash preserve=false weicht ab")
srv.shutdown()

# 4) Chat-Override gewinnt (beide Werte).
for wert in (True, False):
    srv, E = start_engine(material, caps=CAPS, n_ctx=100000)
    reset("eng", *srv.server_address[:2])
    run(*srv.server_address[:2],
        chat_body(chat_template_kwargs={"preserve_thinking": wert}))
    arc = upstream_arcint(E)
    check(arc is not None and arc.get("preserve_thinking") is wert,
          f"Chat-Override {wert} verlor: {arc}")
    check(arc is not None and arc["render"]["prompt_sha256"] == expected_hash(material, wert),
          f"Chat-Override-Hash {wert} weicht ab")
    srv.shutdown()

# 5) Responses-Override gewinnt.
req = {"model": MODEL, "stream": True,
       "input": [{"type": "message", "role": "user", "content": "Hi"}],
       "arcint": {"preserve_thinking": True}}
srv, E = start_engine(material, caps=CAPS, n_ctx=100000)
reset("eng", *srv.server_address[:2])
run(*srv.server_address[:2], req, path="/v1/responses")
arc = upstream_arcint(E)
check(arc is not None and arc.get("preserve_thinking") is True,
      f"Responses-Override verlor: {arc}")
srv.shutdown()

# 6) Nicht-bool ist ein 400, nichts geht an die Engine, der Slot bleibt frei.
srv, E = start_engine(material, caps=CAPS, n_ctx=100000)
reset("eng", *srv.server_address[:2])
vorher = lp._backend_inflight.get("eng", 0)
out, status = run(*srv.server_address[:2],
                  chat_body(chat_template_kwargs={"preserve_thinking": "ja"}))
check(status == 400, f"Nicht-bool (Chat) ergab Status {status} statt 400")
check(b"preserve_thinking" in out, f"400 (Chat) nennt preserve_thinking nicht: {out[:120]}")
check("POST /v1/responses" not in E.paths and "POST /v1/chat/completions" not in E.paths,
      f"Nicht-bool (Chat) wurde gesendet: {E.paths}")
check(lp._backend_inflight.get("eng", 0) == vorher,
      f"Slot nach Nicht-bool (Chat) nicht frei: {lp._backend_inflight.get('eng')}")
reset("eng", *srv.server_address[:2])
E.paths.clear()
vorher = lp._backend_inflight.get("eng", 0)
out, status = run(*srv.server_address[:2],
                  {"model": MODEL, "stream": True,
                   "input": [{"type": "message", "role": "user", "content": "Hi"}],
                   "arcint": {"preserve_thinking": 1}}, path="/v1/responses")
check(status == 400, f"Nicht-bool (Responses) ergab Status {status} statt 400")
check("POST /v1/responses" not in E.paths and "POST /v1/chat/completions" not in E.paths,
      f"Nicht-bool (Responses) wurde gesendet: {E.paths}")
check(lp._backend_inflight.get("eng", 0) == vorher,
      f"Slot nach Nicht-bool (Responses) nicht frei: {lp._backend_inflight.get('eng')}")
srv.shutdown()

# 7) 409-Textretry traegt dieselbe Flagge, je Route richtig.
srv, E = start_engine(material, mode="409", caps=CAPS, n_ctx=100000)
reset("eng", *srv.server_address[:2])
run(*srv.server_address[:2], chat_body())
retries = [raw for path, raw in E.all_bodies if path == "/v1/chat/completions"]
check(bool(retries), "kein Chat-Textretry nach 409 aufgezeichnet")
if retries:
    kw = json.loads(retries[-1]).get("chat_template_kwargs") or {}
    check(kw.get("preserve_thinking") is True,
          f"409-Textretry (Chat) verlor die Flagge: {kw}")
srv.shutdown()

# 7b) Responses-Route: der Retry bleibt auf /v1/responses und traegt
#     arcint.preserve_thinking, ohne andere arcint-Schluessel zu ueberschreiben.
srv, E = start_engine(material, mode="409", caps=CAPS, n_ctx=100000)
reset("eng", *srv.server_address[:2])
run(*srv.server_address[:2],
    {"model": MODEL, "stream": True,
     "input": [{"type": "message", "role": "user", "content": "Hi"}],
     "arcint": {"keep_me": 7}}, path="/v1/responses")
resp_bodies = [raw for path, raw in E.all_bodies if path == "/v1/responses"]
check(len(resp_bodies) >= 2, f"kein Responses-Textretry: {E.all_bodies}")
if len(resp_bodies) >= 2:
    retry = json.loads(resp_bodies[-1])
    check(isinstance(retry.get("arcint"), dict)
          and retry["arcint"].get("preserve_thinking") is True,
          f"Responses-Retry ohne arcint.preserve_thinking: {retry}")
    check(retry.get("arcint", {}).get("keep_me") == 7,
          f"Responses-Retry ueberschrieb arcint-Schluessel: {retry.get('arcint')}")
    check("chat_template_kwargs" not in retry,
          f"Responses-Retry traegt top-level chat_template_kwargs: {retry}")
srv.shutdown()

# 8) Das Logfeld think= traegt kept/dropped/off.
srv, E = start_engine(material, caps=CAPS, n_ctx=100000)
reset("eng", *srv.server_address[:2])
del lp._testlog[:]
run(*srv.server_address[:2], chat_body())
check(any("think=kept" in z for z in lp._testlog), f"kein think=kept: {lp._testlog[-2:]}")
del lp._testlog[:]
run(*srv.server_address[:2], chat_body(chat_template_kwargs={"preserve_thinking": False}))
check(any("think=dropped" in z for z in lp._testlog), f"kein think=dropped: {lp._testlog[-2:]}")
del lp._testlog[:]
run(*srv.server_address[:2], chat_body(chat_template_kwargs={"enable_thinking": False}))
check(any("think=off" in z for z in lp._testlog), f"kein think=off: {lp._testlog[-2:]}")
srv.shutdown()

# 9) Kein Flattern: die Entscheidung faellt auf der Groesse des
#    preserve=true-Renders. Wachsende Historie bleibt gedroppt, erst eine
#    schrumpfende (Client-Kompaktierung) bringt kept zurueck.
def _lang(n):
    return "x" * n


srv, E = start_engine(material, caps=CAPS, n_ctx=50)
reset("eng", *srv.server_address[:2])
run(*srv.server_address[:2], chat_body(messages=[{"role": "system", "content": "sys"},
                                                 {"role": "user", "content": _lang(200)}]))
check(upstream_arcint(E).get("preserve_thinking") is False, "lange Historie nicht gedroppt")
run(*srv.server_address[:2], chat_body(messages=[{"role": "system", "content": "sys"},
                                                 {"role": "user", "content": _lang(4000)}]))
check(upstream_arcint(E).get("preserve_thinking") is False,
      "wachsende Historie flappte auf kept")
run(*srv.server_address[:2], chat_body())
check(upstream_arcint(E).get("preserve_thinking") is True,
      "schrumpfende Historie blieb gedroppt")
srv.shutdown()

print(f"GRUEN: {checks} Faelle" if not fail else f"ROT ({fail} von {checks})")
sys.exit(1 if fail else 0)
