#!/usr/bin/env python3
"""Material-change cache drop: ein 409 auf arcint.renderer_hash wirft den Faehigkeits-Cache weg.

Nach einem Materialwechsel (dieselbe Einheit liefert eine andere GGUF) faellt
der Text-Retry sonst 30 s lang auf einen veralteten Cache zurueck. Der 409 auf
arcint.render.prompt_sha256 (Prompt passt nicht, Material bleibt) darf den Cache
NICHT verwerfen.

ROT auf bb2b8d0: der 409 wirft nie etwas weg und nennt die Art nicht.
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
        self.headers = {"get": lambda k, d=None: d,
                        "Content-Type": "application/json"}

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


def chat_body():
    return {"model": MODEL, "stream": True,
            "messages": [{"role": "user", "content": "Hi"}]}


def run_chat(host, port, body):
    a, b = socket.socketpair()
    h = H(io.BytesIO(), a)
    try:
        h._proxy_local("POST", "/v1/chat/completions", json.dumps(body).encode(),
                       "", 0.0, model=MODEL, is_streaming=True)
        return h.wfile.getvalue()
    finally:
        a.close()
        b.close()


def cache_keys(name):
    with lp.FEYNTALK_CACHE_LOCK:
        return sorted(k for k in lp.FEYNTALK_CACHE
                      if k == name or k.startswith(name + "|"))


def materials():
    with lp.FEYNTALK_CACHE_LOCK:
        return sorted(lp.FEYNTALK_MATERIALS)


material = make_material()

# 1) Materialwechsel (arcint.renderer_hash): Cache und Material weg, Refetch.
srv, E = start_engine(material, mode="409", param="arcint.renderer_hash",
                      caps=CAPS, n_ctx=100000)
reset("eng", *srv.server_address[:2], setting="tokens")
del lp._testlog[:]
out = run_chat(*srv.server_address[:2], chat_body())
check(b"data: " in out, "409-Retry lieferte keinen Textstrom")
check(not cache_keys("eng"), f"Faehigkeits-Cache nicht verworfen: {cache_keys('eng')}")
check(not materials(), f"Material nicht verworfen: {materials()}")
check(any("renderer hash moved" in z and "dropping capability cache" in z
          for z in lp._testlog),
      f"keine Material-Logzeile: {lp._testlog[-2:]}")
hits = E.prop_hits
run_chat(*srv.server_address[:2], chat_body())
check(E.prop_hits > hits, f"kein /props-Refetch nach Drop: prop_hits={E.prop_hits}")
srv.shutdown()

# 2) Prompt-Mismatch (arcint.render.prompt_sha256): Cache und Material bleiben.
srv, E = start_engine(material, mode="409", param="arcint.render.prompt_sha256",
                      caps=CAPS, n_ctx=100000)
reset("eng", *srv.server_address[:2], setting="tokens")
del lp._testlog[:]
out = run_chat(*srv.server_address[:2], chat_body())
check(b"data: " in out, "409-Retry (Prompt) lieferte keinen Textstrom")
check(cache_keys("eng"), f"Cache wurde faelschlich verworfen: {cache_keys('eng')}")
check(materials(), "Material wurde faelschlich verworfen")
check(any("prompt mismatch" in z and "keeping capability cache" in z
          for z in lp._testlog),
      f"keine Prompt-Logzeile: {lp._testlog[-2:]}")
hits = E.prop_hits
run_chat(*srv.server_address[:2], chat_body())
check(E.prop_hits == hits, f"/props ohne Drop neu geholt: prop_hits={E.prop_hits}")
srv.shutdown()

print(f"GRUEN: {checks} Faelle" if not fail else f"ROT ({fail} von {checks})")
sys.exit(1 if fail else 0)
