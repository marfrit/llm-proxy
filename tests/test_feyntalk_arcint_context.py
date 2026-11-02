#!/usr/bin/env python3
"""Der Kontext-Waechter der Engine erreicht einen Chat-Completions-Client.

ROT, wenn ein Token-Modus-Chat-Strom die Objekt-Ereignisse der Engine aus dem
`terminal()`-Pfad verschluckt: der letzte Rahmen muss den Objekt-Schluessel
`arcint` neben `usage` tragen, mit `context`, `loop_guard` und `truncated` -- je
Schluessel genau die Felder, die die Engine geschickt hat (ohne den
Ereignis-Diskriminator `type`) -- und die Nicht-Stream-Antwort denselben
Schluessel im Rumpf. ROT auch, wenn ohne das Ereignis ein `arcint`-Schluessel
auftaucht (dann ist der Rahmen nicht byte-gleich zum heutigen), oder wenn der
Text-Modus etwas dazubekommt. Der Responses-Passthrough reicht die Ereignisse
roh weiter und wird hier nicht angefasst.
"""
import io
import json
import os
import socket
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _lade import lade  # noqa: E402
from _feyntalk_fixture import MODEL, engine_stream, make_material, start_engine  # noqa: E402

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


def run_chat(host, port, body, is_streaming=True):
    a, b = socket.socketpair()
    h = H(io.BytesIO(), a)
    try:
        h._proxy_local("POST", "/v1/chat/completions", json.dumps(body).encode(),
                       "", 0.0, model=MODEL, is_streaming=is_streaming)
        return h.wfile.getvalue()
    finally:
        a.close()
        b.close()


def chat_body(stream=True):
    return {"model": MODEL, "stream": stream,
            "messages": [{"role": "system", "content": "sys"},
                         {"role": "user", "content": "Hi"}]}


def final_frame(out):
    """Der letzte Datenrahmen vor [DONE]."""
    frames = []
    for block in out.decode("utf-8").split("\n\n"):
        line = block.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload == "[DONE]":
            continue
        frames.append(json.loads(payload))
    return frames[-1] if frames else None


CONTEXT = {"type": "arcint:context", "v": 1, "response_id": "resp_fixture",
           "used": 11, "n_ctx": 4096, "remaining": 4085, "forced_close": False}
# Die Engine schickt `token_index` nur, wenn der Waechter wirklich geschlossen hat.
CONTEXT_FORCED = {"type": "arcint:context", "v": 1, "response_id": "resp_fixture",
                  "used": 4097, "n_ctx": 4096, "remaining": 0,
                  "forced_close": True, "token_index": 7}

# Die Loop-Guard-Zaehler: neun Schluessel, wie loop_guard_json() sie baut.
LOOP_GUARD = {"type": "arcint:loop_guard", "v": 1, "response_id": "resp_fixture",
              "detections": 1, "nudges": 1, "slaps": 1, "aborts": 0,
              "self_closes": 1, "door_masked": 2, "door_eog_masked": 0,
              "context_closes": 0, "context_no_closer": 0}

# Die Fit-Pass-Zahlen: fuenf Schluessel, wie truncation_json() sie baut.
TRUNCATED = {"type": "arcint:truncated", "v": 1, "response_id": "resp_fixture",
             "blocks_dropped": 2, "tokens_freed": 640,
             "prompt_tokens_before": 70000, "prompt_tokens_after": 69360,
             "reserve": 2048}


def erwartet(event):
    """Nur die Felder der Engine, ohne den Diskriminator."""
    return {k: v for k, v in event.items() if k != "type"}


# 1) Strom mit Kontext-Ereignis: letzter Rahmen traegt arcint.context neben usage
srv, E = start_engine(make_material(), case="answer", context=CONTEXT)
reset("eng", *srv.server_address[:2])
out = run_chat(*srv.server_address[:2], chat_body())
last = final_frame(out)
check(last is not None, "kein letzter Rahmen im Strom")
check(last is not None and last.get("usage") == {"prompt_tokens": 11,
                                                 "completion_tokens": 6,
                                                 "total_tokens": 17},
      f"usage im letzten Rahmen: {last and last.get('usage')}")
check(last is not None and last.get("arcint") == {"context": erwartet(CONTEXT)},
      f"arcint.context im letzten Rahmen: {last and last.get('arcint')}")
check(last is not None and last.get("choices", [{}])[0].get("finish_reason") == "stop",
      f"finish_reason: {last and last.get('choices')}")
check(b"arcint:context" not in out, "Rohes Ereignis im Strom gelassen")
# Nur ein Rahmen traegt das Objekt (keine Wiederholung in frueheren Rahmen)
traeger = [f for f in (json.loads(b.strip()[5:]) for b in out.decode("utf-8").split("\n\n")
                       if b.strip().startswith("data:")
                       and b.strip()[5:].strip() != "[DONE]") if "arcint" in f]
check(len(traeger) == 1, f"arcint in {len(traeger)} Rahmen statt einem")

# 2) Geschlossener Waechter: token_index reist mit
srv, E = start_engine(make_material(), case="answer", context=CONTEXT_FORCED)
reset("eng", *srv.server_address[:2])
last = final_frame(run_chat(*srv.server_address[:2], chat_body()))
check(last is not None and last.get("arcint") == {"context": erwartet(CONTEXT_FORCED)},
      f"arcint.context mit token_index: {last and last.get('arcint')}")

# 3) Nicht-Streaming: derselbe Schluessel im Antwort-Rumpf
srv, E = start_engine(make_material(), case="answer", context=CONTEXT)
reset("eng", *srv.server_address[:2])
out = run_chat(*srv.server_address[:2], chat_body(stream=False), is_streaming=False)
try:
    body = json.loads(out.decode("utf-8"))
except ValueError:
    body = None
check(body is not None, f"keine JSON-Antwort: {out[:120]!r}")
check(body is not None and body.get("arcint") == {"context": erwartet(CONTEXT)},
      f"arcint.context im Nicht-Stream-Rumpf: {body and body.get('arcint')}")

# 3b) Loop-Guard allein: nur sein Objekt, seine neun Zaehler
srv, E = start_engine(make_material(), case="answer", loop_guard=LOOP_GUARD)
reset("eng", *srv.server_address[:2])
last = final_frame(run_chat(*srv.server_address[:2], chat_body()))
check(last is not None and last.get("arcint") == {"loop_guard": erwartet(LOOP_GUARD)},
      f"arcint.loop_guard: {last and last.get('arcint')}")

# 3c) Truncated allein: nur seine Fit-Pass-Zahlen
srv, E = start_engine(make_material(), case="answer", truncated=TRUNCATED)
reset("eng", *srv.server_address[:2])
last = final_frame(run_chat(*srv.server_address[:2], chat_body()))
check(last is not None and last.get("arcint") == {"truncated": erwartet(TRUNCATED)},
      f"arcint.truncated: {last and last.get('arcint')}")

# 3d) Alle drei in einem Zug: drei Schluessel, jeder mit genau seinen Feldern
srv, E = start_engine(make_material(), case="answer", context=CONTEXT,
                      loop_guard=LOOP_GUARD, truncated=TRUNCATED)
reset("eng", *srv.server_address[:2])
out = run_chat(*srv.server_address[:2], chat_body())
last = final_frame(out)
alle = {"context": erwartet(CONTEXT), "loop_guard": erwartet(LOOP_GUARD),
        "truncated": erwartet(TRUNCATED)}
check(last is not None and last.get("arcint") == alle,
      f"arcint mit allen drei: {last and last.get('arcint')}")
check(last is not None and sorted(last.get("arcint", {})) == ["context", "loop_guard", "truncated"],
      f"arcint-Schluessel: {last and sorted(last.get('arcint', {}))}")
check(last is not None and last.get("usage") is not None, "usage fehlt neben den Objekten")
check(b"arcint:loop_guard" not in out and b"arcint:truncated" not in out,
      "Rohes Objekt-Ereignis im Strom gelassen")

# 3e) Dieselben drei im Nicht-Stream-Rumpf
srv, E = start_engine(make_material(), case="answer", context=CONTEXT,
                      loop_guard=LOOP_GUARD, truncated=TRUNCATED)
reset("eng", *srv.server_address[:2])
out = run_chat(*srv.server_address[:2], chat_body(stream=False), is_streaming=False)
try:
    body = json.loads(out.decode("utf-8"))
except ValueError:
    body = None
check(body is not None and body.get("arcint") == alle,
      f"arcint im Nicht-Stream-Rumpf: {body and body.get('arcint')}")

# 4) Ohne Ereignis: kein arcint-Schluessel, Rahmen wie heute
srv, E = start_engine(make_material(), case="answer")
reset("eng", *srv.server_address[:2])
out = run_chat(*srv.server_address[:2], chat_body())
last = final_frame(out)
check(last is not None and "arcint" not in last,
      f"arcint ohne Ereignis im Strom: {last and last.get('arcint')}")
check(last is not None and last.get("usage") is not None, "usage fehlt ohne Ereignis")
check(b"arcint:loop_guard" not in out and b"arcint:truncated" not in out,
      "Rohes Objekt-Ereignis im Strom gelassen")
srv, E = start_engine(make_material(), case="answer")
reset("eng", *srv.server_address[:2])
out = run_chat(*srv.server_address[:2], chat_body(stream=False), is_streaming=False)
try:
    body = json.loads(out.decode("utf-8"))
except ValueError:
    body = None
check(body is not None and "arcint" not in body,
      f"arcint ohne Ereignis im Nicht-Stream-Rumpf: {body and body.get('arcint')}")

# 5) Text-Modus: unveraendert, kein arcint
srv, E = start_engine(make_material(), case="answer", context=CONTEXT)
reset("eng", *srv.server_address[:2], setting="off")
out = run_chat(*srv.server_address[:2], chat_body())
check(b"arcint" not in out, "Text-Modus bekam einen arcint-Schluessel")
check("POST /v1/chat/completions" in E.paths, f"Text-Modus ging nicht an die Chat-Route: {E.paths}")

print(f"{checks} Pruefungen, {fail} rot")
sys.exit(1 if fail else 0)
