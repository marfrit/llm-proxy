#!/usr/bin/env python3
"""Exakter Tokenzaehler fuer die Denken-Drop-Entscheidung (1.3.1).

ROT, wenn eine reine Schaetzung ueber die Schwelle den Render zu frueh fallen
laesst, obwohl die Engine weniger Tokens zaehlt; wenn eine exakte Zahl ueber der
Schwelle nicht droppt; wenn der Zaehler ohne Not befragt wird; wenn ein
fehlender/kaputter/zu langsamer /tokenize die Anfrage kippt statt auf die
Schaetzung zurueckzufallen; wenn die Authorization nicht mitgeht; oder wenn
Logzeile und think-Feld die Herkunft der Zahl (~N estimate / N exact) nicht
nennen.
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
    def __init__(self, wfile, conn, auth=None):
        self.wfile, self.connection = wfile, conn
        self.status = None
        kopf = {"Content-Type": "application/json"}
        if auth:
            kopf["Authorization"] = auth
        self.headers = kopf

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
    getattr(lp, "_FEYNTALK_TOKEN_RATIO", {}).clear()


def chat_body(**extra):
    body = {"model": MODEL, "stream": True,
            "messages": [{"role": "system", "content": "sys"},
                         {"role": "user", "content": "x" * 200}]}
    body.update(extra)
    return body


def run(host, port, body, auth=None, path="/v1/chat/completions"):
    a, b = socket.socketpair()
    h = H(io.BytesIO(), a, auth=auth)
    try:
        h._proxy_local("POST", path, json.dumps(body).encode(), "", 0.0,
                       model=MODEL, is_streaming=True)
        return h.wfile.getvalue(), h.status
    finally:
        a.close()
        b.close()


def upstream_arcint(E):
    raw = E.bodies.get("/v1/responses")
    if raw is None:
        return None
    try:
        return json.loads(raw).get("arcint")
    except ValueError:
        return None


material = make_material(template=template_text_preserve())

# Die Groessen: n_ctx=50 -> Schwelle 35, Vor-Schwelle 28.
#   "x"*200 rendert 266 Zeichen -> Schaetzung 88,7 (ueber der Schwelle).
#   "Hi"    rendert  68 Zeichen -> Schaetzung 22,7 (unter der Vor-Schwelle).

# 1) Schaetzung ueber der Schwelle, exakt darunter: behalten.
srv, E = start_engine(material, caps=CAPS, n_ctx=50, tokenize_count=5)
reset("eng", *srv.server_address[:2])
del lp._testlog[:]
out, status = run(*srv.server_address[:2], chat_body())
arc = upstream_arcint(E)
check(status == 200, f"Status {status} statt 200")
check(arc is not None and arc.get("preserve_thinking") is True,
      f"Schaetzung ueber Schwelle liess den Render trotz exakt=5 fallen: {arc}")
check(E.tokenize_hits == 1, f"/tokenize {E.tokenize_hits} mal befragt, erwartet 1")
check(any("kept(exact)" in z for z in lp._testlog),
      f"kein think=kept(exact): {lp._testlog[-2:]}")
check(not any("think dropped" in z for z in lp._testlog), "faelschlich gedroppt")
srv.shutdown()

# 2) Exakt ueber der Schwelle: droppen.
srv, E = start_engine(material, caps=CAPS, n_ctx=50, tokenize_count=100)
reset("eng", *srv.server_address[:2])
del lp._testlog[:]
out, status = run(*srv.server_address[:2], chat_body())
arc = upstream_arcint(E)
check(status == 200, f"Status {status} statt 200")
check(arc is not None and arc.get("preserve_thinking") is False,
      f"exakt=100 > 35 wurde nicht gedroppt: {arc}")
check(E.tokenize_hits == 1, f"/tokenize {E.tokenize_hits} mal befragt, erwartet 1")
check(any("dropped(exact)" in z for z in lp._testlog),
      f"kein think=dropped(exact): {lp._testlog[-2:]}")
check(any("100 tokens (exact)" in z for z in lp._testlog),
      f"Drop-Zeile ohne '100 tokens (exact)': {lp._testlog[-2:]}")
srv.shutdown()

# 2b) Auch die aeltere Antwortform ohne `count` ({tokens}) zaehlt.
srv, E = start_engine(material, caps=CAPS, n_ctx=50, tokenize_count=100,
                      tokenize_shape="tokens")
reset("eng", *srv.server_address[:2])
out, status = run(*srv.server_address[:2], chat_body())
arc = upstream_arcint(E)
check(status == 200 and arc is not None and arc.get("preserve_thinking") is False,
      f"tokens-Form ohne count nicht gedroppt: {arc}")
check(E.tokenize_hits == 1, "tokens-Form: /tokenize nicht befragt")
srv.shutdown()

# 3) Schaetzung unter der Vor-Schwelle: gar kein /tokenize-Aufruf.
srv, E = start_engine(material, caps=CAPS, n_ctx=100000, tokenize_count=5)
reset("eng", *srv.server_address[:2])
del lp._testlog[:]
out, status = run(*srv.server_address[:2],
                  chat_body(messages=[{"role": "system", "content": "sys"},
                                      {"role": "user", "content": "Hi"}]))
arc = upstream_arcint(E)
check(status == 200 and arc is not None and arc.get("preserve_thinking") is True,
      f"kleiner Render nicht behalten: {arc}")
check(E.tokenize_hits == 0, f"/tokenize unnoetig befragt: {E.tokenize_hits} Aufrufe")
check(any("kept(estimate)" in z for z in lp._testlog),
      f"kein think=kept(estimate): {lp._testlog[-2:]}")
srv.shutdown()

# 3b/3c) Schaetzung im Band [0,8, 1,0) * Schwelle (hier 30,0 bei 50 ctx -> 35):
# die exakte Zahl entscheidet in beide Richtungen. Ein Rueckfall auf
# `if est > limit:` VOR dem /tokenize-Aufruf faellt hier auf (est 30 < 35).
for exakt, erwartet in ((45, False), (5, True)):
    srv, E = start_engine(material, caps=CAPS, n_ctx=50, tokenize_count=exakt)
    reset("eng", *srv.server_address[:2])
    del lp._testlog[:]
    out, status = run(*srv.server_address[:2],
                      chat_body(messages=[{"role": "system", "content": "sys"},
                                          {"role": "user", "content": "x" * 24}]))
    arc = upstream_arcint(E)
    check(status == 200 and arc is not None
          and arc.get("preserve_thinking") is erwartet,
          f"Band: exakt={exakt} ergab {arc}")
    check(E.tokenize_hits == 1, f"Band (exakt={exakt}): /tokenize nicht befragt")
    check(any(("kept(exact)" if erwartet else "dropped(exact)") in z
              for z in lp._testlog),
          f"Band (exakt={exakt}): keine exakte Entscheidung in {lp._testlog[-2:]}")
    srv.shutdown()

# 3d) Ein einmal gelernter exact/Schaetzung-Verhaeltnis haelt die Entscheidung,
# auch wenn /tokenize im naechsten Zug ausfaellt: ohne Skalierung laege die rohe
# Schaetzung (88,7) ueber der Schwelle (35) und wuerde faelschlich droppen.
srv, E = start_engine(material, caps=CAPS, n_ctx=50, tokenize_count=20)
reset("eng", *srv.server_address[:2])
del lp._testlog[:]
out, status = run(*srv.server_address[:2], chat_body())
arc = upstream_arcint(E)
check(status == 200 and arc is not None and arc.get("preserve_thinking") is True,
      f"Verhaeltnis-Lauf 1 (exakt=20) nicht behalten: {arc}")
E.tokenize_status = 500   # ab jetzt faellt der Zaehler aus
out, status = run(*srv.server_address[:2], chat_body())
arc = upstream_arcint(E)
check(status == 200 and arc is not None and arc.get("preserve_thinking") is True,
      f"Ausfall kippte eine behaltene Entscheidung: {arc}")
check(E.tokenize_hits == 2, f"Verhaeltnis: {E.tokenize_hits} /tokenize-Aufrufe, erwartet 2")
check(any("kept(estimate, scaled)" in z for z in lp._testlog),
      f"kein think=kept(estimate, scaled): {lp._testlog[-2:]}")
check(any("using the scaled estimate" in z for z in lp._testlog),
      f"Fallback-Log nennt die Skalierung nicht: {lp._testlog[-3:]}")
check(not any("think dropped" in z for z in lp._testlog),
      f"trotz Verhaeltnis gedroppt: {lp._testlog[-2:]}")
srv.shutdown()

# 3e) Das Verhaeltnis haengt am Material-Hash, nicht am Backend-Namen: ein unter dem
# Namen gemerktes (z. B. vom Tokenizer eines vorher hier geladenen Modells) darf
# nicht skalieren. Ohne gueltiges Verhaeltnis bleibt es bei der rohen Schaetzung.
srv, E = start_engine(material, caps=CAPS, n_ctx=50, tokenize_count=20)
reset("eng", *srv.server_address[:2])
E.tokenize_status = 500
lp._FEYNTALK_TOKEN_RATIO["eng"] = 0.2          # fremdes Verhaeltnis unter dem Namen
del lp._testlog[:]
out, status = run(*srv.server_address[:2], chat_body())
arc = upstream_arcint(E)
check(status == 200 and arc is not None and arc.get("preserve_thinking") is False,
      f"Namens-Verhaeltnis wurde benutzt (haette roh droppen muessen): {arc}")
check(not any("scaled" in z for z in lp._testlog),
      f"mit fremdem Verhaeltnis skaliert: {lp._testlog[-2:]}")
srv.shutdown()

# 4) /tokenize fehlt (404): Schaetzung, eine Zeile je Backend, Anfrage laeuft.
srv, E = start_engine(material, caps=CAPS, n_ctx=50)   # tokenize_count=None -> 404
reset("eng", *srv.server_address[:2])
del lp._testlog[:]
for _ in range(2):
    out, status = run(*srv.server_address[:2], chat_body())
arc = upstream_arcint(E)
check(status == 200 and arc is not None and arc.get("preserve_thinking") is False,
      f"404 liess die Anfrage nicht mit der Schaetzung durchlaufen: {status} {arc}")
check(E.tokenize_hits == 2, f"404: {E.tokenize_hits} /tokenize-Aufrufe, erwartet 2")
zeilen = [z for z in lp._testlog if "exact token count unavailable" in z]
check(len(zeilen) == 1, f"404 nicht genau einmal geloggt: {zeilen}")
check(any("~89 tokens (estimate)" in z for z in lp._testlog),
      f"Drop-Zeile ohne '~89 tokens (estimate)': {lp._testlog[-3:]}")
srv.shutdown()

# 4b) 500: dasselbe.
srv, E = start_engine(material, caps=CAPS, n_ctx=50, tokenize_status=500)
reset("eng", *srv.server_address[:2])
del lp._testlog[:]
out, status = run(*srv.server_address[:2], chat_body())
arc = upstream_arcint(E)
check(status == 200 and arc is not None and arc.get("preserve_thinking") is False,
      f"500 liess die Anfrage nicht durchlaufen: {status} {arc}")
check(any("exact token count unavailable" in z for z in lp._testlog),
      "500 nicht geloggt")
srv.shutdown()

# 4c) Timeout: dasselbe, die Frist bricht den Zaehler ab.
alter = lp.FEYNTALK_TOKENIZE_TIMEOUT
lp.FEYNTALK_TOKENIZE_TIMEOUT = 0.25
srv, E = start_engine(material, caps=CAPS, n_ctx=50, tokenize_count=5,
                      tokenize_delay=0.8)
reset("eng", *srv.server_address[:2])
del lp._testlog[:]
try:
    out, status = run(*srv.server_address[:2], chat_body())
finally:
    lp.FEYNTALK_TOKENIZE_TIMEOUT = alter
arc = upstream_arcint(E)
check(status == 200 and arc is not None and arc.get("preserve_thinking") is False,
      f"Timeout liess die Anfrage nicht durchlaufen: {status} {arc}")
check(any("exact token count unavailable" in z for z in lp._testlog),
      "Timeout nicht geloggt")
srv.shutdown()

# 5) Die Authorization des Anrufers geht an /tokenize.
srv, E = start_engine(material, caps=CAPS, n_ctx=50, tokenize_count=5)
reset("eng", *srv.server_address[:2])
out, status = run(*srv.server_address[:2], chat_body(), auth="Bearer sekrit")
check(status == 200, f"Status {status} statt 200")
check(E.tokenize_hits == 1, f"/tokenize {E.tokenize_hits} mal befragt")
check(E.tokenize_auth == ["Bearer sekrit"],
      f"Authorization fehlt/falsch am /tokenize: {E.tokenize_auth}")
srv.shutdown()

print(f"GRUEN: {checks} Faelle" if not fail else f"ROT ({fail} von {checks})")
sys.exit(1 if fail else 0)
