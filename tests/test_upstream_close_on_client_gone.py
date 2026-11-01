#!/usr/bin/env python3
"""Ein voller Client-Schluss mitten im Strom kappt den Upstream (alle drei Wege).

Der Client-Wachhund schliesst die Backend-Verbindung, sobald der Anrufer weg
ist; die Engine stoppt an ihrem naechsten Schreibvorgang. Geprueft wird der
Token-Chat, der Responses-Passthrough und das Text-Relais.

ROT, wenn ein Weg den Upstream nach dem vollen Schluss nicht binnen ~2 s kappt.
"""
import http.server
import json
import os
import socket
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _lade import lade  # noqa: E402
from _feyntalk_fixture import MODEL, make_material  # noqa: E402

lp = lade()
fail = 0
checks = 0
MATERIAL = make_material()
CAPS = {"supports_preserve_thinking": True, "supports_object_arguments": False}


def check(cond, msg):
    global fail, checks
    checks += 1
    if not cond:
        print("ROT:", msg)
        fail += 1


def _sse(obj):
    return ("data: " + json.dumps(obj, ensure_ascii=False) + "\n\n").encode()


class Backend(http.server.BaseHTTPRequestHandler):
    closed = []

    def log_message(self, *a):
        pass

    def _json(self, payload, status=200):
        raw = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path == "/props":
            self._json({"token_mode": "ready", "renderer_hash": MATERIAL["hash"],
                        "build": {"version": "1.4.0"}, "arcint": {"v": 1},
                        "chat_template_caps": CAPS})
        elif self.path == "/v1/responses/renderer":
            self._json(MATERIAL)
        elif self.path == "/v1/models":
            self._json({"data": [{"id": MODEL, "object": "model"}]})
        else:
            self._json({"error": "not found"}, 404)

    def _stream(self, frames):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            for frame in frames:
                self.wfile.write(frame)
                self.wfile.flush()
                time.sleep(0.05)
        except OSError:
            Backend.closed.append(time.monotonic())

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        if self.path == "/v1/responses":
            frames = [_sse({"type": "response.created",
                            "response": {"id": "r", "created_at": 1700000000,
                                         "status": "in_progress"}})]
            frames += [_sse({"type": "arcint:token", "v": 1, "token_index": i,
                             "token_id": 1, "field": "text"}) for i in range(200)]
            self._stream(frames)
        else:
            frames = [_sse({"id": "c", "object": "chat.completion.chunk", "model": "m",
                            "choices": [{"index": 0, "delta": {"content": "x"},
                                         "finish_reason": None}]}) for _ in range(200)]
            self._stream(frames)


backend = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Backend)
backend.daemon_threads = True
threading.Thread(target=backend.serve_forever, daemon=True).start()
bp = backend.server_address[1]
proxy = http.server.ThreadingHTTPServer(("127.0.0.1", 0), lp.Handler)
proxy.daemon_threads = True
threading.Thread(target=proxy.serve_forever, daemon=True).start()
pp = proxy.server_address[1]


def reset(setting):
    lp.LOCAL_BACKENDS[:] = [("eng", "127.0.0.1", bp)]
    lp.BACKEND_FEYNTALK.clear()
    lp.BACKEND_FEYNTALK["eng"] = setting
    lp._FEYNTALK_WARNED.clear()
    with lp.FEYNTALK_CACHE_LOCK:
        lp.FEYNTALK_CACHE.clear()
        lp.FEYNTALK_MATERIALS.clear()
    lp._FEYNTALK_WARM.clear()
    lp._build_sems({"eng": 4})
    lp._disc_cache.clear()


def close_mid_stream(path, body):
    Backend.closed.clear()
    s = socket.create_connection(("127.0.0.1", pp), timeout=10)
    raw = json.dumps(body).encode()
    s.sendall((f"POST {path} HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
               f"Content-Length: {len(raw)}\r\n\r\n").encode() + raw)
    s.settimeout(5)
    got = b""
    try:
        while len(got) < 200:
            chunk = s.recv(4096)
            if not chunk:
                break
            got += chunk
    except OSError:
        pass
    t0 = time.monotonic()
    s.close()
    while time.monotonic() - t0 < 2.5 and not Backend.closed:
        time.sleep(0.05)
    return time.monotonic() - t0 if Backend.closed else None


chat = {"model": MODEL, "stream": True, "messages": [{"role": "user", "content": "hi"}]}
resp = {"model": MODEL, "stream": True,
        "input": [{"type": "message", "role": "user", "content": "hi"}]}

reset("tokens")
dt = close_mid_stream("/v1/chat/completions", chat)
check(dt is not None, "Token-Chat: Upstream nach vollem Client-Schluss nicht gekappt")

reset("off")
dt = close_mid_stream("/v1/chat/completions", chat)
check(dt is not None, "Text-Relais: Upstream nach vollem Client-Schluss nicht gekappt")

reset("tokens")
dt = close_mid_stream("/v1/responses", resp)
check(dt is not None, "Responses-Passthrough: Upstream nach vollem Client-Schluss nicht gekappt")

print(f"GRUEN: {checks} Faelle" if not fail else f"ROT ({fail} von {checks})")
sys.exit(1 if fail else 0)
