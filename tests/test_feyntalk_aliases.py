#!/usr/bin/env python3
"""-ft-Namen (Alias mit feyntalk-Modus) und der Versions-Handshake.

ROT, wenn ein Alias nicht in seinem Modus laeuft, ein unbekannter feyntalk-Wert
nicht uebersprungen wird, der Alias nicht vom Ziel erbt, /v1/models den Alias
nicht als alias_of markiert, ein -down-Ziel nicht -down vererbt, der Handshake
einen zu alten Renderer / eine zu alte Engine nicht ablehnt, oder der Alias
scheitert, wo der klassische Name im Textmodus funktioniert.
"""
import http.server
import io
import json
import os
import socket
import sys
import threading
import urllib.error
import urllib.request

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


def reset(name, host, port, setting="off"):
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


def chat_body(model=MODEL, **extra):
    body = {"model": model, "stream": True,
            "messages": [{"role": "system", "content": "sys"},
                         {"role": "user", "content": "Hi"}]}
    body.update(extra)
    return body


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


def alias_entry(entries, alias_id):
    for e in entries:
        if e.get("id") == alias_id:
            return e
    return None


# 1) Alias laeuft in seinem Modus: -ft = tokens, klassisch = text.
material = make_material(template=template_text_preserve())
srv, E = start_engine(material, caps=CAPS, n_ctx=100000)
reset("eng", *srv.server_address[:2], setting="off")
lp.MODEL_ALIASES = {"qwen-ft": {"model": MODEL, "feyntalk": "tokens",
                                "max_prompt_tokens": None}}
px = Proxy()
out, status = px.post(chat_body(model=MODEL))
check(status == 200 and "POST /v1/responses" not in E.paths,
      f"klassischer Name ging in den Tokenmodus: {E.paths}")
E.paths.clear()
out, status = px.post(chat_body(model="qwen-ft"))
check("POST /v1/responses" in E.paths,
      f"-ft-Alias lief nicht im Tokenmodus: {E.paths}")
tok = json.loads(E.bodies.get("/v1/responses") or b"{}")
check(tok.get("arcint", {}).get("token_mode") is True, f"Token-Anfrage fehlt: {tok.get('arcint')}")
px.close(); srv.shutdown()

# 2) Alias mit feyntalk text bei tokens-Backend bleibt text.
srv, E = start_engine(material, caps=CAPS, n_ctx=100000)
reset("eng", *srv.server_address[:2], setting="tokens")
lp.MODEL_ALIASES = {"qwen-classic": {"model": MODEL, "feyntalk": "text"}}
px = Proxy()
out, status = px.post(chat_body(model="qwen-classic"))
check("POST /v1/responses" not in E.paths and "POST /v1/chat/completions" in E.paths,
      f"text-Alias ging in den Tokenmodus: {E.paths}")
px.close(); srv.shutdown()

# 3) Unbekannter feyntalk-Wert: Konfigurationsfehler, Alias uebersprungen.
p = os.path.join(lp.CONFIG_DIR, "model-aliases.json")
with open(p, "w") as f:
    json.dump({"gut": {"model": MODEL, "feyntalk": "tokens"},
               "schlecht": {"model": MODEL, "feyntalk": "vielleicht"}}, f)
geladen = lp._load_model_aliases()
check("gut" in geladen, f"gueltiger Alias fehlt: {geladen}")
check("schlecht" not in geladen, f"unbekannter feyntalk wurde uebernommen: {geladen}")

# 4) Katalog: alias_of, feyntalk, vererbte Eigenschaften, -ft-Hinweis, -down.
lp.MODEL_ALIASES = {"qwen-ft": {"model": "ziel", "feyntalk": "tokens",
                                "max_prompt_tokens": None}}
lokal = [{"id": "[local] ziel", "object": "model", "owned_by": "local-eng",
          "cost_class": "local", "reachable": True, "ctx": 4096,
          "max_output": 777, "name": "Freundlich", "reasoning": True},
         {"id": "[local] ziel-down", "object": "model",
          "owned_by": "local-eng (temporarily unavailable)",
          "cost_class": "local", "reachable": False, "ctx": 4096, "name": "Freundlich"}]
katalog = lp._alias_catalog_entries(lokal)
e = alias_entry(katalog, "[local] qwen-ft")
check(e is not None, f"kein Alias-Eintrag: {katalog}")
if e is not None:
    check(e.get("alias_of") == "[local] ziel", f"alias_of falsch: {e.get('alias_of')}")
    check(e.get("feyntalk") == "tokens", f"feyntalk fehlt: {e.get('feyntalk')}")
    check(e.get("ctx") == 4096 and e.get("max_output") == 777,
          f"Ziel-Eigenschaften nicht geerbt: ctx={e.get('ctx')} max_output={e.get('max_output')}")
    check(e.get("cost_class") == "local", f"cost_class falsch: {e.get('cost_class')}")
    check(e.get("name") == "Freundlich -ft", f"Friendly-Hinweis fehlt: {e.get('name')}")
d = alias_entry(katalog, "[local] qwen-ft-down")
check(d is not None and d.get("alias_of") == "[local] ziel-down" and d.get("reachable") is False,
      f"-down nicht vererbt: {d}")

# 5) Handshake Renderer: zu alte Version -> Text, eine Logzeile.
srv, E = start_engine(material, caps=CAPS, n_ctx=100000)
reset("eng", *srv.server_address[:2], setting="tokens")
lp.MODEL_ALIASES = {"qwen-ft": {"model": MODEL, "feyntalk": "tokens"}}
alt = lp._feyntalk_renderer_version
lp._feyntalk_renderer_version = lambda: "0.9.0"
del lp._testlog[:]
px = Proxy()
out, status = px.post(chat_body(model="qwen-ft"))
check("POST /v1/responses" not in E.paths, f"alter Renderer nutzte Tokenmodus: {E.paths}")
check(any("renderer" in z and "text mode" in z for z in lp._testlog),
      f"Renderer-Handshake nicht geloggt: {lp._testlog[-2:]}")
lp._feyntalk_renderer_version = alt
px.close(); srv.shutdown()

# 6) Handshake Engine: zu alt oder falscher Vertrag -> Text.
for kw in ({"engine_version": "1.3.0"}, {"contract": 2}):
    srv, E = start_engine(material, caps=CAPS, n_ctx=100000, **kw)
    reset("eng", *srv.server_address[:2], setting="tokens")
    lp.MODEL_ALIASES = {"qwen-ft": {"model": MODEL, "feyntalk": "tokens"}}
    px = Proxy()
    out, status = px.post(chat_body(model="qwen-ft"))
    check("POST /v1/responses" not in E.paths,
          f"Engine-Handshake {kw} nutzte Tokenmodus: {E.paths}")
    px.close(); srv.shutdown()

# 7) Alias scheitert nicht, wo der klassische Name textet (Engine nicht bereit).
srv, E = start_engine(material, n_ctx=100000, token_mode="off")
reset("eng", *srv.server_address[:2], setting="off")
lp.MODEL_ALIASES = {"qwen-ft": {"model": MODEL, "feyntalk": "tokens"}}
px = Proxy()
klassisch, s1 = px.post(chat_body(model=MODEL))
alias, s2 = px.post(chat_body(model="qwen-ft"))
check(s1 == 200 and s2 == 200, f"Status klassisch={s1} alias={s2}")
check(klassisch.count(b"data: ") > 0 and alias.count(b"data: ") > 0,
      f"Textstrom fehlt: klassisch={klassisch[:40]!r} alias={alias[:40]!r}")
check("POST /v1/responses" not in E.paths, f"nicht bereite Engine wurde tokenisiert: {E.paths}")
px.close(); srv.shutdown()

# 8) Alias erbt reasoning und die Effort-Tabelle des Ziels.
srv, E = start_engine(material, caps={"supports_object_arguments": False}, n_ctx=100000)
reset("eng", *srv.server_address[:2], setting="tokens")
lp.MODEL_ALIASES = {"qwen-ft": {"model": MODEL, "feyntalk": "tokens"}}
lp.EFFORT_OVERRIDES = [("fixture-model", {"high": "low"})]
px = Proxy()
out, status = px.post(chat_body(model="qwen-ft", reasoning_effort="high"))
tok = json.loads(E.bodies.get("/v1/responses") or b"{}")
check(tok.get("reasoning", {}).get("effort") == "low",
      f"Alias erbte die Effort-Tabelle nicht: {tok.get('reasoning')}")
lp.EFFORT_OVERRIDES = []
px.close(); srv.shutdown()

# 8b) reasoning-Modell: das Ziel bringt enable_thinking in den Render.
srv, E = start_engine(material, caps={"supports_object_arguments": False}, n_ctx=100000)
reset("eng", *srv.server_address[:2], setting="tokens")
lp.MODEL_ALIASES = {"qwen-ft": {"model": MODEL, "feyntalk": "tokens"}}
lp.REASONING_OVERRIDES = [("fixture-model", True)]
px = Proxy()
out, status = px.post(chat_body(model="qwen-ft"))
tok = json.loads(E.bodies.get("/v1/responses") or b"{}")
expected = render_prompt(material, chat_body()["messages"],
                         extra_context={"enable_thinking": True}).prompt_sha256
check(tok.get("arcint", {}).get("render", {}).get("prompt_sha256") == expected,
      "Alias erbte reasoning nicht in den Render")
lp.REASONING_OVERRIDES = []
px.close(); srv.shutdown()

# 9) Der -ft-Alias funktioniert auch auf der Responses-Route.
srv, E = start_engine(material, caps=CAPS, n_ctx=100000)
reset("eng", *srv.server_address[:2], setting="off")
lp.MODEL_ALIASES = {"qwen-ft": {"model": MODEL, "feyntalk": "tokens"}}
px = Proxy()
out, status = px.post({"model": "qwen-ft", "stream": True,
                       "input": [{"type": "message", "role": "user", "content": "Hi"}]},
                      path="/v1/responses")
check(status == 200 and "POST /v1/responses" in E.paths,
      f"Responses-Alias lief nicht: status={status} paths={E.paths}")
tok = json.loads(E.bodies.get("/v1/responses") or b"{}")
check(tok.get("model") == MODEL and tok.get("arcint", {}).get("token_mode") is True,
      f"Responses-Alias nicht auf das Ziel umgeschrieben: model={tok.get('model')}")
px.close(); srv.shutdown()

print(f"GRUEN: {checks} Faelle" if not fail else f"ROT ({fail} von {checks})")
sys.exit(1 if fail else 0)
