#!/usr/bin/env python3
"""Der /props-Rueckfall fuer ctx, wenn /v1/models schweigt.

ROT, wenn eine nackte Modellliste (arcint/OpenArc-Form) den Kontext nicht aus
/props holt, die llama.cpp-Form (default_generation_settings.n_ctx) nicht liest,
oder ein fehlendes n_ctx nicht None ergibt.
"""
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

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


ZUSTAND = {"props": {"model": {"id": "qwen3.6-coder", "n_ctx": 262144}}}


class Stub(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path == "/v1/models":
            # arcint/OpenArc-Form: nacktes Modellobjekt, KEIN context_length
            koerper = {"object": "list",
                       "data": [{"id": "qwen3.6-coder", "object": "model",
                                 "owned_by": "arcint"}]}
        elif self.path == "/props":
            koerper = ZUSTAND["props"]
        else:
            self.send_response(404)
            self.end_headers()
            return
        roh = json.dumps(koerper).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(roh)))
        self.end_headers()
        self.wfile.write(roh)


srv = HTTPServer(("127.0.0.1", 0), Stub)
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()


def lauf():
    with lp._local_ctx_lock:
        lp._local_ctx.clear()
    lp._fetch_backend_models(("stub", "127.0.0.1", port))
    return lp.local_ctx("qwen3.6-coder")


a = lauf()
check(a == 262144, f"arcint-Form (model.n_ctx) nicht gelesen: {a}")
ZUSTAND["props"] = {"default_generation_settings": {"n_ctx": 32768}}
b = lauf()
check(b == 32768, f"llama.cpp-Form (default_generation_settings.n_ctx) nicht gelesen: {b}")
ZUSTAND["props"] = {"model": {"id": "qwen3.6-coder"}}
c = lauf()
check(c is None, f"fehlendes n_ctx ergab nicht None: {c}")
srv.shutdown()

print(f"GRUEN: {checks} Faelle" if not fail else f"ROT ({fail} von {checks})")
sys.exit(1 if fail else 0)
