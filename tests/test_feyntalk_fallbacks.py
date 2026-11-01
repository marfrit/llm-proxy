#!/usr/bin/env python3
"""Fallback review: jeder Text-Rueckfall laeuft durch den echten Anfragepfad.

Bild-Teile (Chat image_url, Responses input_image) und Render-Fehler muessen
Text bleiben, eine Logzeile schreiben und das Backend als Textauftrag
erreichen - fuer den klassischen Namen UND den -ft-Alias. Dazu der
Versions-Handshake: der installierte Renderer besteht das Gate, _version_tuple
ordnet Vorab-Releases kleiner ein als die Freigabe.
"""
import http.server
import json
import os
import sys
import threading
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _lade import lade  # noqa: E402
from _feyntalk_fixture import MODEL, make_material, start_engine  # noqa: E402
import feyntalk_render  # noqa: E402

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


def reset(name, host, port, setting="tokens"):
    lp.LOCAL_BACKENDS[:] = [(name, host, port)]
    lp.BACKEND_FEYNTALK.clear()
    lp.BACKEND_FEYNTALK[name] = setting
    lp.MODEL_ALIASES = {}
    lp._FEYNTALK_WARNED.clear()
    with lp.FEYNTALK_CACHE_LOCK:
        lp.FEYNTALK_CACHE.clear()
        lp.FEYNTALK_MATERIALS.clear()
    lp._build_sems({name: 4})
    lp._disc_cache.clear()


class Proxy:
    def __init__(self):
        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), lp.Handler)
        self.srv.daemon_threads = True
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.port = self.srv.server_address[1]

    def post(self, body, path="/v1/chat/completions"):
        data = json.dumps(body).encode()
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data,
                                     {"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.read(), r.status
        except urllib.error.HTTPError as e:
            return e.read(), e.code

    def close(self):
        self.srv.shutdown()


IMAGE_CHAT = [{"type": "text", "text": "look"},
              {"type": "image_url", "image_url": {"url": "https://example.invalid/x.png"}}]
IMAGE_RESP = [{"type": "input_text", "text": "look"},
              {"type": "input_image", "image_url": "https://example.invalid/x.png"}]


def chat_body(model, content):
    return {"model": model, "stream": True,
            "messages": [{"role": "user", "content": content}]}


def resp_body(model, content):
    return {"model": model, "stream": True,
            "input": [{"type": "message", "role": "user", "content": content}]}


def body_has_token(body_bytes):
    try:
        return bool(json.loads(body_bytes or b"{}").get("arcint", {}).get("token_mode"))
    except ValueError:
        return False


material = make_material()

# 1) Bild im Chat: Text bleibt Text, mit Logzeile, fuer klassisch und -ft.
srv, E = start_engine(material, caps=CAPS, n_ctx=100000)
reset("eng", *srv.server_address[:2], setting="tokens")
lp.MODEL_ALIASES = {"qwen-ft": {"model": MODEL, "feyntalk": "tokens"}}
px = Proxy()
for name in (MODEL, "qwen-ft"):
    E.paths.clear(); E.bodies.clear(); del lp._testlog[:]; lp._FEYNTALK_WARNED.clear()
    out, status = px.post(chat_body(name, IMAGE_CHAT))
    check(status == 200, f"Bild (Chat, {name}) ergab Status {status}")
    check(not body_has_token(E.bodies.get("/v1/responses")),
          f"Bild (Chat, {name}) wurde tokenisiert")
    check("POST /v1/responses" not in E.paths,
          f"Bild (Chat, {name}) ging an die Token-Route: {E.paths}")
    check(any("images are not served in token mode" in z for z in lp._testlog),
          f"Bild (Chat, {name}) ohne Logzeile: {lp._testlog[-2:]}")
px.close(); srv.shutdown()

# 2) Bild in den Responses: dito (Textauftrag ohne arcint.token_mode).
srv, E = start_engine(material, caps=CAPS, n_ctx=100000)
reset("eng", *srv.server_address[:2], setting="tokens")
lp.MODEL_ALIASES = {"qwen-ft": {"model": MODEL, "feyntalk": "tokens"}}
px = Proxy()
for name in (MODEL, "qwen-ft"):
    E.paths.clear(); E.bodies.clear(); del lp._testlog[:]; lp._FEYNTALK_WARNED.clear()
    out, status = px.post(resp_body(name, IMAGE_RESP), path="/v1/responses")
    check(status == 200, f"Bild (Responses, {name}) ergab Status {status}")
    check(not body_has_token(E.bodies.get("/v1/responses")),
          f"Bild (Responses, {name}) wurde tokenisiert")
    check(any("images are not served in token mode" in z for z in lp._testlog),
          f"Bild (Responses, {name}) ohne Logzeile: {lp._testlog[-2:]}")
px.close(); srv.shutdown()

# 3) Render-Fehler: derselbe Rueckfall, beide Routen, klassisch und -ft.
srv, E = start_engine(material, caps=CAPS, n_ctx=100000)
reset("eng", *srv.server_address[:2], setting="tokens")
lp.MODEL_ALIASES = {"qwen-ft": {"model": MODEL, "feyntalk": "tokens"}}
orig_render = lp._FTRenderer.render


def _boom(self, *a, **k):
    raise lp._FTRenderError("boom")


lp._FTRenderer.render = _boom
px = Proxy()
try:
    for name in (MODEL, "qwen-ft"):
        E.paths.clear(); E.bodies.clear(); del lp._testlog[:]; lp._FEYNTALK_WARNED.clear()
        out, status = px.post(chat_body(name, [{"type": "text", "text": "hi"}]))
        check(status == 200 and not body_has_token(E.bodies.get("/v1/responses")),
              f"Render-Fehler (Chat, {name}) wurde nicht Text: status={status}")
        check(any("render stays text" in z for z in lp._testlog),
              f"Render-Fehler (Chat, {name}) ohne Logzeile: {lp._testlog[-2:]}")
        E.paths.clear(); E.bodies.clear(); del lp._testlog[:]; lp._FEYNTALK_WARNED.clear()
        out, status = px.post(resp_body(name, [{"type": "input_text", "text": "hi"}]),
                              path="/v1/responses")
        check(status == 200 and not body_has_token(E.bodies.get("/v1/responses")),
              f"Render-Fehler (Responses, {name}) wurde nicht Text: status={status}")
        check(any("render stays text" in z for z in lp._testlog),
              f"Render-Fehler (Responses, {name}) ohne Logzeile: {lp._testlog[-2:]}")
finally:
    lp._FTRenderer.render = orig_render
px.close(); srv.shutdown()

# 4) Responses-Logzeile nennt den Alias.
srv, E = start_engine(material, caps=CAPS, n_ctx=100000)
reset("eng", *srv.server_address[:2], setting="off")
lp.MODEL_ALIASES = {"qwen-ft": {"model": MODEL, "feyntalk": "tokens"}}
px = Proxy()
del lp._testlog[:]
out, status = px.post(resp_body("qwen-ft", [{"type": "input_text", "text": "hi"}]),
                      path="/v1/responses")
check(any("alias=qwen-ft -> fixture-model" in z for z in lp._testlog),
      f"Responses-Logzeile ohne Alias: {lp._testlog[-2:]}")
px.close(); srv.shutdown()

# 5) _version_tuple: v-Praefix, Vorab-Release kleiner, Unparsbares None.
check(lp._version_tuple("v1.4.0") == (1, 4, 0, 1), f"v-Praefix: {lp._version_tuple('v1.4.0')}")
check(lp._version_tuple("1.4") == (1, 4, 0, 1), f"kurz: {lp._version_tuple('1.4')}")
check(lp._version_tuple("1.4.0-rc1") == (1, 4, 0, 0), f"rc: {lp._version_tuple('1.4.0-rc1')}")
check(lp._version_tuple("1.4.0-rc1") < lp._version_tuple("1.4.0"),
      "Vorab-Release nicht kleiner als die Freigabe")
check(lp._version_tuple("1.4.0+build7") == (1, 4, 0, 1), f"build: {lp._version_tuple('1.4.0+build7')}")
check(lp._version_tuple("garbage") is None, f"Muell: {lp._version_tuple('garbage')}")
check(lp._version_tuple("1.x") is None, f"1.x: {lp._version_tuple('1.x')}")

# 6) Der installierte Renderer besteht das Gate.
rv = lp._feyntalk_renderer_version()
check(rv is not None and rv == getattr(feyntalk_render, "__version__", rv),
      f"Renderer-Version nicht gelesen: {rv!r}")
check(rv is not None and lp._version_tuple(rv) >= lp._version_tuple(lp.FEYNTALK_MIN_RENDERER_VERSION),
      f"installierter Renderer {rv!r} besteht das Gate nicht")
check(lp._version_tuple(lp.FEYNTALK_MIN_ENGINE_VERSION) >= (1, 4, 0, 0),
      "Engine-Minimum unter 1.4.0")

print(f"GRUEN: {checks} Faelle" if not fail else f"ROT ({fail} von {checks})")
sys.exit(1 if fail else 0)
