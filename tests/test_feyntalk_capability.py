#!/usr/bin/env python3
"""Token-mode capability discovery: /props ready + the material endpoint, cached by hash.

ROT, wenn ein Backend ohne `feyntalk`-Pin token mode bekommt, ein nicht
bereites /props oder ein Material, das die Renderer-Pruefung nicht besteht,
nicht auf text zurueckfaellt (und das nicht laut sagt), der Material-Hash nicht
als Cache-Schluessel wirkt (gleicher Hash = kein zweiter Abruf, neuer Hash =
Neuladen), oder der Proxy ohne feyntalk_render nicht mehr laeuft.
"""
import copy
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _lade import lade  # noqa: E402
from _feyntalk_fixture import MODEL, make_material, start_engine  # noqa: E402

lp = lade()
fail = 0
checks = 0


def check(cond, msg):
    global fail, checks
    checks += 1
    if not cond:
        print("ROT:", msg)
        fail += 1


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


# 1) ohne Pin bleibt alles aus; /props wird nicht einmal gefragt
srv, E = start_engine(make_material())
reset("eng", *srv.server_address[:2], "off")
cap = lp._feyntalk_capability(("eng",) + srv.server_address[:2])
check(cap.get("mode") == "off", f"ohne Pin: {cap}")
check(E.prop_hits == 0 and E.renderer_hits == 0, "ohne Pin wurde /props gefragt")

# 2) Pin tokens + ready: Material geladen, Hash und Renderer da
reset("eng", *srv.server_address[:2], "tokens")
backend = ("eng",) + srv.server_address[:2]
cap = lp._feyntalk_capability(backend)
check(cap.get("mode") == "tokens", f"ready-Backend nicht tokens: {cap}")
check(cap.get("hash") == E.material["hash"], "Material-Hash nicht uebernommen")
check(cap.get("renderer") is not None, "kein Renderer gebaut")
check(cap.get("template_source") == "gguf", f"template source: {cap.get('template_source')}")
hits = (E.prop_hits, E.renderer_hits)
cap2 = lp._feyntalk_capability(backend)
check(cap2.get("mode") == "tokens" and (E.prop_hits, E.renderer_hits) == hits,
      "gleicher Hash hat erneut /props oder Material abgerufen")

# 3) neuer Hash: neu laden, nicht aus dem Cache
neu = make_material(template="NEU " + E.material["chat_template"]["text"])
E.material = neu
lp.FEYNTALK_TTL = 0
cap3 = lp._feyntalk_capability(backend)
check(cap3.get("mode") == "tokens", f"neuer Hash nicht tokens: {cap3}")
check(cap3.get("hash") == neu["hash"] and cap3.get("hash") != cap["hash"],
      f"Hash-Wechsel nicht erkannt: {cap3.get('hash')}")
check(E.renderer_hits > hits[1], "neuer Hash hat das Material nicht neu abgerufen")

# 4) Material, das die Hash-Pruefung nicht besteht -> text, eine laute Zeile
# (Merkzettel und Warmlauf leeren: derselbe Hash wie in Fall 2 ist sonst schon warm)
with lp.FEYNTALK_CACHE_LOCK:
    lp.FEYNTALK_MATERIALS.clear()
lp._FEYNTALK_WARM.clear()
bad = copy.deepcopy(make_material())
bad["decode"]["pieces_b64"][1] = "Qg=="
E.material = bad
lp.FEYNTALK_TTL = 0
lp._FEYNTALK_WARNED.clear()
cap4 = lp._feyntalk_capability(backend)
check(cap4.get("mode") == "text", f"gebrochenes Material nicht text: {cap4}")
check("material hash" in (cap4.get("reason") or ""), f"Grund fehlt: {cap4.get('reason')}")
check(any("text mode" in z and "material hash" in z for z in lp._testlog),
      f"Rueckfall nicht laut geloggt: {lp._testlog[-3:]}")

# 5) /props nicht ready -> text
srv2, E2 = start_engine(make_material())
E2.do_GET_alt = True
# Der Fixture-Server meldet ready; hier wird der Pfad direkt geprueft.
lp.FEYNTALK_TTL = 30
reset("eng2", *srv2.server_address[:2], "tokens")
orig_props = lp._fetch_backend_props
lp._fetch_backend_props = lambda host, port: {"token_mode": "off"}
cap5 = lp._feyntalk_capability(("eng2",) + srv2.server_address[:2])
lp._fetch_backend_props = orig_props
check(cap5.get("mode") == "text", f"nicht ready nicht text: {cap5}")

# 6) feyntalk_render fehlt: Proxy laeuft, token mode sagt es einmal
reset("eng3", *srv.server_address[:2], "tokens")
lp.FEYNTALK_AVAILABLE = False
lp._FEYNTALK_WARNED.clear()
cap6 = lp._feyntalk_capability(("eng3",) + srv.server_address[:2])
cap6b = lp._feyntalk_capability(("eng3",) + srv.server_address[:2])
lp.FEYNTALK_AVAILABLE = True
check(cap6.get("mode") == "text", f"ohne Renderer nicht text: {cap6}")
check(sum(1 for z in lp._testlog if "not importable" in z) == 1,
      f"Warnzeile nicht genau einmal: {lp._testlog}")

srv.shutdown()
srv2.shutdown()
print(f"GRUEN: {checks} Faelle" if not fail else f"ROT ({fail} von {checks})")
sys.exit(1 if fail else 0)
