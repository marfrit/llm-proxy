#!/usr/bin/env python3
"""Token-mode fallback and the off default.

ROT, wenn ein 409 renderer_mismatch nicht genau einmal als Text-Anfrage
wiederholt wird (und das geloggt ist), ein nicht konfiguriertes Backend den
Token-Pfad betritt oder /props gefragt wird, oder der Textpfad nicht mehr
byte-gleich zum Backend-Strom ist.
"""
import io
import json
import os
import socket
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _lade import lade  # noqa: E402
from _feyntalk_fixture import MODEL, chat_text_stream, make_material, start_engine  # noqa: E402

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


def reset(name, host, port, setting):
    lp.LOCAL_BACKENDS[:] = [(name, host, port)]
    lp.BACKEND_FEYNTALK.clear()
    lp.BACKEND_FEYNTALK[name] = setting
    lp._FEYNTALK_WARNED.clear()
    with lp.FEYNTALK_CACHE_LOCK:
        lp.FEYNTALK_CACHE.clear()
        lp.FEYNTALK_MATERIALS.clear()
    lp._build_sems({name: 4})
    lp._disc_cache.clear()


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


BODY = {"model": MODEL, "stream": True,
        "messages": [{"role": "user", "content": "Hi"}]}

# 1) 409 renderer mismatch: einmal als Text wiederholt, geloggt
srv, E = start_engine(make_material(), mode="409")
reset("eng", *srv.server_address[:2], "tokens")
out = run_chat(*srv.server_address[:2], BODY)
check(E.paths.count("POST /v1/responses") == 1,
      f"Token-Anfrage nicht genau einmal: {E.paths}")
check(E.paths.count("POST /v1/chat/completions") == 1,
      f"Text-Wiederholung nicht genau einmal: {E.paths}")
check(out == chat_text_stream(MODEL), "Text-Wiederholung ist nicht der Text-Strom")
check(any("409" in z and "text mode" in z and "retrying" in z for z in lp._testlog),
      f"Fallback nicht geloggt: {lp._testlog[-4:]}")

# 2) 409-Erkennung: das Param entscheidet, ein unbekannter 409 ist auch ein Fallback
check(lp._feyntalk_is_renderer_mismatch(409, json.dumps({
    "error": {"param": "arcint.renderer_mismatch"}}).encode()), "Mismatch nicht erkannt")
check(lp._feyntalk_is_renderer_mismatch(409, json.dumps({
    "error": {"param": "arcint.render.prompt_sha256"}}).encode()), "Prompt-Mismatch nicht erkannt")
check(not lp._feyntalk_is_renderer_mismatch(400, b"{}"), "400 als Mismatch gewertet")

# 3) Default off: kein Token-Pfad, kein /props, Text byte-gleich
srv.shutdown()
srv, E = start_engine(make_material())
reset("eng", *srv.server_address[:2], "off")
out = run_chat(*srv.server_address[:2], BODY)
check("POST /v1/responses" not in E.paths,
      f"off-Backend betrat den Token-Pfad: {E.paths}")
check(E.paths.count("POST /v1/chat/completions") == 1,
      f"off-Backend rief die Chat-Route nicht genau einmal: {E.paths}")
check(out == chat_text_stream(MODEL), "Textpfad off ist nicht byte-gleich")

# 4) explizit text: ebenfalls kein Token-Pfad
reset("eng", *srv.server_address[:2], "text")
E.paths.clear()
out = run_chat(*srv.server_address[:2], BODY)
check("POST /v1/responses" not in E.paths,
      f"text-Pin betrat den Token-Pfad: {E.paths}")
check(E.paths.count("POST /v1/chat/completions") == 1,
      f"text-Pin rief die Chat-Route nicht genau einmal: {E.paths}")
check(out == chat_text_stream(MODEL), "text-Pin ist nicht byte-gleich")

srv.shutdown()
print(f"GRUEN: {checks} Faelle" if not fail else f"ROT ({fail} von {checks})")
sys.exit(1 if fail else 0)
