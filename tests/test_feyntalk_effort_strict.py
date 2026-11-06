#!/usr/bin/env python3
"""Effort in the render, thinking off through the proxy, and `-ft` names failing loudly.

ROT, wenn:
- der Render einer Anfrage mit Effort die Stufe nicht so an die Vorlage gibt, wie die
  Engine sie gibt (dann weicht prompt_sha256 ab und jeder Zug bekommt 409);
- reasoning_effort none/off auf dem Textpfad enable_thinking true bekommt (das schlaegt
  den Effort in der Engine, die Vorlage denkt dann auf ihrem Standard);
- ein `-ft`-Name (Alias mit feyntalk "tokens") still auf den Textpfad faellt, statt zu
  scheitern; ein Name ohne Alias dagegen nicht mehr zurueckfaellt;
- ein bewegter Material-Hash auf `-ft` nicht genau einmal neu holt und wiederholt;
- Zaehler und Kopfzeile den gelaufenen Pfad nicht nennen.
"""
import io
import json
import os
import socket
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _lade import lade  # noqa: E402
from _feyntalk_fixture import MODEL, chat_text_stream, make_material, start_engine  # noqa: E402

from feyntalk_render.render import Renderer  # noqa: E402

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
        self.status_seen = None
        self.sent_headers = {}

    def send_response(self, code, message=None):
        self.status_seen = code

    def send_header(self, k, v):
        self.sent_headers[k] = v

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
    lp.FEYNTALK_COUNTS.clear()


def run_chat(body, feyntalk=None, path="/v1/chat/completions"):
    a, b = socket.socketpair()
    h = H(io.BytesIO(), a)
    try:
        h._proxy_local("POST", path, json.dumps(body).encode(), "", 0.0, model=MODEL,
                       is_streaming=bool(body.get("stream")), feyntalk=feyntalk)
        return h, h.wfile.getvalue()
    finally:
        a.close()
        b.close()


BODY = {"model": MODEL, "stream": True, "messages": [{"role": "user", "content": "Hi"}]}

QWEN38_STYLE = ("{% set e = reasoning_effort | default('xhigh') %}"
                "{% if e == 'low' %}LOW{% elif e == 'xhigh' %}XHIGH{% elif e == 'medium' %}MEDIUM{% endif %}"
                "{% for m in messages %}{{ m.role }}:{{ m.content }}\n{% endfor %}")
OPENAI_STYLE = ("{% set e = reasoning_effort | default('none') %}"
                "{% if e == 'low' %}L{% elif e == 'medium' %}M{% elif e == 'high' %}H{% endif %}"
                "{% for m in messages %}{{ m.role }}:{{ m.content }}\n{% endfor %}")
NO_LEVEL = "{% for m in messages %}{{ m.role }}:{{ m.content }}\n{% endfor %}"


def level(cap_template, effort):
    return lp._feyntalk_template_effort(effort, {"material": {"chat_template": {"text": cap_template}}})


# 1) Die Abbildung ist die der Engine (template_reasoning_effort in src/core/chat.cpp)
for effort, want in [("low", "low"), ("medium", "medium"), ("minimal", "low"), ("high", "xhigh"),
                     ("xhigh", "xhigh"), ("none", None), ("off", None), ("bogus", None), (None, None)]:
    got = level(QWEN38_STYLE, effort)
    check(got == want, f"Qwen3.8-Vorlage: {effort!r} -> {got!r}, soll {want!r}")
for effort, want in [("low", "low"), ("medium", "medium"), ("minimal", "low"), ("high", "high"),
                     ("xhigh", "high"), ("none", None)]:
    got = level(OPENAI_STYLE, effort)
    check(got == want, f"OpenAI-Vorlage: {effort!r} -> {got!r}, soll {want!r}")
for effort in ("low", "medium", "high", "xhigh", "minimal"):
    check(level(NO_LEVEL, effort) is None, f"Vorlage ohne Variable bekam {effort!r}")

# 2) Der gesendete prompt_sha256 ist der Render MIT der Stufe, das Feld geht nach oben
for effort, seen in [("low", "LOW"), ("medium", "MEDIUM"), ("high", "XHIGH"), ("xhigh", "XHIGH")]:
    srv, E = start_engine(make_material(QWEN38_STYLE))
    reset("eng", *srv.server_address[:2], "tokens")
    run_chat(dict(BODY, reasoning_effort=effort), feyntalk="tokens")
    sent = json.loads(E.bodies["/v1/responses"])
    material = make_material(QWEN38_STYLE)
    want = Renderer(material).render([{"role": "user", "content": "Hi"}], add_generation_prompt=True,
                                     extra_context={"enable_thinking": True, "reasoning_effort": level(QWEN38_STYLE, effort)})
    check(want.text.startswith(seen), f"Vorlage nicht wie gedacht fuer {effort}: {want.text[:20]!r}")
    check(sent["arcint"]["render"]["prompt_sha256"] == want.prompt_sha256,
          f"prompt_sha256 ohne die Stufe gerendert bei effort {effort}")
    check(sent.get("reasoning") == {"effort": effort}, f"Effort nicht nach oben: {sent.get('reasoning')}")
    srv.shutdown()

# 3) Ohne Effort-Feld kommt der Standard der lokalen Faehigkeit: dieselbe Stufe wie oben
srv, E = start_engine(make_material(QWEN38_STYLE))
reset("eng", *srv.server_address[:2], "tokens")
orig_lr = lp._local_reasoning
lp._local_reasoning = lambda m: True
run_chat(BODY, feyntalk="tokens")
lp._local_reasoning = orig_lr
sent = json.loads(E.bodies["/v1/responses"])
want = Renderer(make_material(QWEN38_STYLE)).render([{"role": "user", "content": "Hi"}], add_generation_prompt=True,
                                                    extra_context={"enable_thinking": True, "reasoning_effort": "medium"})
check(sent.get("reasoning") == {"effort": "medium"}, f"Standard-Effort fehlt: {sent.get('reasoning')}")
check(sent["arcint"]["render"]["prompt_sha256"] == want.prompt_sha256, "Standard-Effort nicht im Render")
srv.shutdown()

# 4) off zaehlt wie none: Denken aus, keine Stufe
check(lp._feyntalk_enable("m", {"reasoning_effort": "off"}, True) == (False, "none"),
      f"off nicht als aus gelesen: {lp._feyntalk_enable('m', {'reasoning_effort': 'off'}, True)}")
check(lp._feyntalk_enable("m", {"reasoning_effort": "none"}, True) == (False, "none"), "none nicht aus")
# Der Schalter, den _proxy selbst setzt, darf die Stufe nicht verdecken; ein Schalter des Anrufers gewinnt.
for body, want in [({"reasoning_effort": "high", "chat_template_kwargs": {"enable_thinking": True}}, (True, "high")),
                   ({"reasoning_effort": "none", "chat_template_kwargs": {"enable_thinking": False}}, (False, "none")),
                   ({"reasoning_effort": "high", "chat_template_kwargs": {"enable_thinking": False}}, (False, "none")),
                   ({"reasoning_effort": "none", "chat_template_kwargs": {"enable_thinking": True}}, (None, None)),
                   ({"chat_template_kwargs": {"enable_thinking": True}}, (True, "medium")),
                   ({"chat_template_kwargs": {"enable_thinking": False}}, (False, "none"))]:
    got = lp._feyntalk_enable("m", body, True)
    check(got == want, f"_feyntalk_enable {body}: {got}, soll {want}")

# 5) Textpfad: none/off duerfen kein enable_thinking true bekommen
for effort, want in [("none", False), ("off", False), ("medium", True), (None, True)]:
    j = {"model": "m"}
    if effort is not None:
        j["reasoning_effort"] = effort
    changed = lp._local_thinking_switch(j)
    check(changed and j["chat_template_kwargs"]["enable_thinking"] is want,
          f"Textpfad effort {effort!r}: {j.get('chat_template_kwargs')}")
j = {"model": "m", "reasoning_effort": "none", "chat_template_kwargs": {"enable_thinking": True}}
check(not lp._local_thinking_switch(j) and j["chat_template_kwargs"]["enable_thinking"] is True,
      "Ein enable_thinking des Anrufers wurde ueberschrieben")

# 6) -ft + 409 prompt mismatch: Fehler, keine Text-Wiederholung
srv, E = start_engine(make_material(), mode="409", param="arcint.render.prompt_sha256")
reset("eng", *srv.server_address[:2], "tokens")
h, out = run_chat(BODY, feyntalk="tokens")
check("POST /v1/chat/completions" not in E.paths, f"-ft fiel auf den Textpfad: {E.paths}")
check(h.status_seen == 502, f"-ft 409: Status {h.status_seen}")
err = json.loads(out)["error"]
check(err.get("code") == "renderer_mismatch" and "eng" in err.get("message", ""), f"Fehlerrumpf: {err}")
check(lp.FEYNTALK_COUNTS["eng"]["errors"] == 1, f"Fehler nicht gezaehlt: {dict(lp.FEYNTALK_COUNTS)}")
check(any("409" in z and "failing" in z for z in lp._testlog), f"Nicht geloggt: {lp._testlog[-3:]}")
srv.shutdown()

# 7) -ft + 409 Material bewegt: genau ein neuer Versuch im Token-Modus, dann Fehler
srv, E = start_engine(make_material(), mode="409", param="arcint.renderer_hash")
reset("eng", *srv.server_address[:2], "tokens")
h, out = run_chat(BODY, feyntalk="tokens")
check(E.paths.count("POST /v1/responses") == 2, f"Token-Versuche: {E.paths}")
check("POST /v1/chat/completions" not in E.paths, f"Hash bewegt fiel auf den Textpfad: {E.paths}")
check(h.status_seen == 502 and json.loads(out)["error"].get("code") == "renderer_mismatch",
      f"Zweiter 409: {h.status_seen} {out[:120]!r}")
srv.shutdown()

# 8) -ft auf einem Backend ohne Token-Modus: Fehler mit dem Grund, keine Chat-Anfrage
srv, E = start_engine(make_material(), token_mode="off")
reset("eng", *srv.server_address[:2], "tokens")
h, out = run_chat(BODY, feyntalk="tokens")
check("POST /v1/chat/completions" not in E.paths, f"-ft ohne Token-Modus lief als Text: {E.paths}")
check(h.status_seen == 502 and json.loads(out)["error"].get("code") == "token_mode_unavailable",
      f"Kein Token-Modus: {h.status_seen} {out[:160]!r}")
srv.shutdown()

# 8b) Ein gemerktes "kein Token-Modus" ueberlebt den Neustart der Einheit nicht
srv, E = start_engine(make_material(), token_mode="off")
reset("eng", *srv.server_address[:2], "tokens")
h, out = run_chat(BODY, feyntalk="tokens")
check(h.status_seen == 502, f"Vor dem Neustart: {h.status_seen}")
E.token_mode = "ready"
h, out = run_chat(BODY, feyntalk="tokens")
check(h.status_seen == 200 and "/v1/responses" in E.bodies,
      f"Nach dem Neustart kein Token-Zug: {h.status_seen} {out[:120]!r}")
srv.shutdown()

# 9) -ft mit einer Anfrage, die der Token-Modus nicht kann: 400, kein Backend-Zug
srv, E = start_engine(make_material())
reset("eng", *srv.server_address[:2], "tokens")
h, out = run_chat(dict(BODY, stop=["x"]), feyntalk="tokens")
check(h.status_seen == 400 and json.loads(out)["error"].get("code") == "token_mode_unsupported_request",
      f"stop auf -ft: {h.status_seen} {out[:160]!r}")
check("POST /v1/chat/completions" not in E.paths and "POST /v1/responses" not in E.paths,
      f"stop auf -ft erreichte das Backend: {E.paths}")
srv.shutdown()

# 10) Name ohne Alias: der Textpfad bleibt, wie er war, und wird gezaehlt
srv, E = start_engine(make_material(), mode="409", param="arcint.render.prompt_sha256")
reset("eng", *srv.server_address[:2], "tokens")
h, out = run_chat(BODY)
check(E.paths.count("POST /v1/chat/completions") == 1, f"Kein Text-Fallback ohne Alias: {E.paths}")
check(out == chat_text_stream(MODEL), "Fallback ist nicht der Text-Strom")
check(lp.FEYNTALK_COUNTS["eng"]["fallbacks"] == 1, f"Fallback nicht gezaehlt: {dict(lp.FEYNTALK_COUNTS)}")
check(h._ft_path == "text", f"Pfad: {h._ft_path}")
srv.shutdown()

# 11) Ein gelungener Token-Zug: Zaehler und Kopfzeile
srv, E = start_engine(make_material())
reset("eng", *srv.server_address[:2], "tokens")
h, out = run_chat(BODY, feyntalk="tokens")
check(h.status_seen == 200 and lp.FEYNTALK_COUNTS["eng"]["tokens"] == 1,
      f"Token-Zug: {h.status_seen} {dict(lp.FEYNTALK_COUNTS)}")
check(h._ft_path == "tokens", f"Pfad: {h._ft_path}")
srv.shutdown()

# 12) Die echte send_response traegt den Pfad als Kopfzeile
a, b = socket.socketpair()
h = H(io.BytesIO(), a)
h._ft_path = "tokens"
h.request_version = "HTTP/1.1"
h.requestline = "POST / HTTP/1.1"
h.log_request = lambda *x, **k: None
lp.Handler.send_response(h, 200)
a.close()
b.close()
check(h.sent_headers.get("X-Llm-Proxy-Path") == "tokens", f"Kopfzeile: {h.sent_headers}")

# Ab hier der ganze Anfragepfad (_proxy), den die Faelle oben umgehen: dort setzt der Proxy
# selbst enable_thinking und wendet die Effort-Tabelle an, bevor der Render dran ist.
import http.client  # noqa: E402
import http.server  # noqa: E402
import threading  # noqa: E402


class Proxy:
    def __init__(self):
        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), lp.Handler)
        self.srv.daemon_threads = True
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.port = self.srv.server_address[1]

    def post(self, body):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        c.request("POST", "/v1/chat/completions", json.dumps(body), {"Content-Type": "application/json"})
        r = c.getresponse()
        data = r.read()
        c.close()
        return r.status, r.getheader("X-Llm-Proxy-Path"), data

    def close(self):
        self.srv.shutdown()


def real(template, setting="tokens", **engine):
    srv, E = start_engine(make_material(template), **engine)
    reset("eng", *srv.server_address[:2], setting)
    lp.MODEL_ALIASES = {"m-ft": {"model": MODEL, "feyntalk": "tokens"}}
    return srv, E, Proxy()


def want_sha(template, **extra):
    return Renderer(make_material(template)).render([{"role": "user", "content": "Hi"}], add_generation_prompt=True,
                                                    extra_context=extra).prompt_sha256


lp._local_reasoning = lambda m: True

# 13) -ft ueber _proxy: die Stufe des Anrufers erreicht Render und Engine
for effort, lvl in [("high", "xhigh"), ("low", "low"), ("minimal", "low")]:
    srv, E, px = real(QWEN38_STYLE)
    status, path, _ = px.post(dict(BODY, model="m-ft", reasoning_effort=effort))
    sent = json.loads(E.bodies["/v1/responses"])
    check(status == 200 and path == "tokens", f"-ft {effort}: {status} {path}")
    check(sent.get("reasoning") == {"effort": effort}, f"-ft {effort}: Engine bekam {sent.get('reasoning')}")
    check(sent["arcint"]["render"]["prompt_sha256"] == want_sha(QWEN38_STYLE, enable_thinking=True, reasoning_effort=lvl),
          f"-ft {effort}: nicht mit {lvl} gerendert")
    px.close(); srv.shutdown()

# 14) -ft ueber _proxy mit none: Denken aus, keine Stufe
srv, E, px = real(QWEN38_STYLE)
status, path, _ = px.post(dict(BODY, model="m-ft", reasoning_effort="none"))
sent = json.loads(E.bodies["/v1/responses"])
check(sent.get("reasoning") == {"effort": "none"}, f"-ft none: {sent.get('reasoning')}")
check(sent["arcint"]["render"]["prompt_sha256"] == want_sha(QWEN38_STYLE, enable_thinking=False), "-ft none: Render falsch")
px.close(); srv.shutdown()

# 15) Die Effort-Tabelle gilt genau einmal, auch bei einer Kette
srv, E, px = real(QWEN38_STYLE)
lp.EFFORT_OVERRIDES[:] = [(MODEL, {"medium": "low", "low": "xhigh"})]
px.post(dict(BODY, model="m-ft", reasoning_effort="medium"))
sent = json.loads(E.bodies["/v1/responses"])
lp.EFFORT_OVERRIDES[:] = []
check(sent.get("reasoning") == {"effort": "low"}, f"Tabelle doppelt angewandt: {sent.get('reasoning')}")
px.close(); srv.shutdown()

# 16) Textpfad ueber _proxy: none kommt als enable_thinking false an, sonst true
for effort, want in [("none", False), ("off", False), ("high", True)]:
    srv, E, px = real(QWEN38_STYLE, setting="text")
    status, path, _ = px.post(dict(BODY, reasoning_effort=effort))
    sent = json.loads(E.bodies["/v1/chat/completions"])
    check(status == 200 and path == "text", f"Text {effort}: {status} {path}")
    check(sent["chat_template_kwargs"]["enable_thinking"] is want, f"Text {effort}: {sent['chat_template_kwargs']}")
    px.close(); srv.shutdown()


# 17) Bewegter Hash, zweiter Versuch gelingt: Token-Zug, kein Text
class Once:
    left = 1

    def __eq__(self, other):
        if other == "409" and Once.left:
            Once.left -= 1
            return True
        return False

    __hash__ = None


srv, E, px = real(QWEN38_STYLE, param="arcint.renderer_hash")
E.mode = Once()
status, path, data = px.post(dict(BODY, model="m-ft", reasoning_effort="low"))
check(status == 200 and path == "tokens", f"Hash bewegt, dann ok: {status} {path} {data[:80]!r}")
check(E.paths.count("POST /v1/responses") == 2 and "POST /v1/chat/completions" not in E.paths, f"Pfade: {E.paths}")
check(lp.FEYNTALK_COUNTS["eng"] == {"tokens": 1, "fallbacks": 0, "errors": 0}, f"Zaehler: {lp.FEYNTALK_COUNTS['eng']}")
px.close(); srv.shutdown()

# 17b) -ft ohne Strom ueber _proxy: der Proxy haengt seine Stop-Strings nicht an; die des Anrufers scheitern
srv, E, px = real(QWEN38_STYLE)
status, path, data = px.post(dict(BODY, model="m-ft", stream=False, reasoning_effort="low"))
check(status == 200 and path == "tokens", f"-ft ohne Strom: {status} {path} {data[:100]!r}")
status, path, data = px.post(dict(BODY, model="m-ft", stream=False, stop=["x"]))
check(status == 400 and b"token_mode_unsupported_request" in data, f"-ft mit stop: {status} {data[:100]!r}")
px.close(); srv.shutdown()
srv, E, px = real(QWEN38_STYLE, setting="text")
status, path, data = px.post(dict(BODY, stream=False))
check(status == 200 and "stop" in json.loads(E.bodies["/v1/chat/completions"]), "Textpfad ohne Strom verlor die Stop-Strings")
px.close(); srv.shutdown()

# 18) Keep-Alive: eine spaetere Anfrage auf demselben Handler traegt keinen alten Pfad
a, b = socket.socketpair()
h = H(io.BytesIO(), a)
h.path = "/health"
h._ft_path = "tokens"
lp.Handler._proxy(h, "GET")
health = json.loads(h.wfile.getvalue())
a.close()
b.close()
check(h._ft_path is None, f"/health behielt den alten Pfad: {h._ft_path}")
check(health.get("status") == "ok" and isinstance(health.get("feyntalk"), dict), f"/health: {health}")

print(f"GRUEN: {checks} Faelle" if not fail else f"ROT ({fail} von {checks})")
sys.exit(1 if fail else 0)
