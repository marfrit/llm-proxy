#!/usr/bin/env python3
"""Der Kostenregler darf bei kaputter policy.json nicht aufmachen.

Bis 1.0.1 lieferte _policy() bei einem Lesefehler {} und damit den Standardmodus
'max': ein Komma zu viel oder eine halb geschriebene Datei, und eine bezahlte
Wolkenanfrage ging durch, obwohl 'local' (DSGVO) eingestellt war.
ROT, wenn eine kaputte, leere, nicht-Objekt- oder Modus-unbekannte Datei eine
bezahlte Anfrage durchlaesst. Gegenprobe: fehlt die Datei ganz, bleibt 'max'
(dokumentiertes Verhalten) - sonst bestuende der Test auch bei "immer zu".
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _lade import lade

lp = lade()
lp._paid_catalog["anthropic/claude-x"] = "ok"
lp._model_metadata["anthropic/claude-x"] = {"prompt_price": 3.0}   # Preis bekannt: der Grenzpfad wird erreicht
lp._free_catalog["vendor/frei:free"] = "ok"
lp.find_backend_for_model = lambda m: None
pf = os.path.join(lp.CONFIG_DIR, "policy.json")
fehler = []


def setze(text, t):
    with open(pf, "w") as f:
        f.write(text)
    os.utime(pf, (t, t))


def bezahlt_gesperrt():
    return lp._policy_blocked("anthropic/claude-x", "anthropic/claude-x")[0]


if bezahlt_gesperrt():
    fehler.append("ohne policy.json wird 'bezahlt' gesperrt - Standard 'max' verletzt")

setze('{"mode": "local"}', 10)
if not bezahlt_gesperrt():
    fehler.append("gueltiges 'local' laesst bezahlt durch")

for t, (name, text) in enumerate([
        ("Tippfehler", '{"mode": "local",}'),
        ("leer (Editor schreibt gerade)", ''),
        ("kein Objekt", '"local"'),
        ("unbekannter Modus", '{"mode": "lokal"}'),
        ("Preisgrenze kein Zahlwert", '{"mode": "cheap", "cheap_max_prompt_price": null}'),
], start=20):
    setze(text, t)
    try:
        zu = bezahlt_gesperrt()
    except Exception as e:
        fehler.append(f"{name}: _policy_blocked wirft {type(e).__name__} statt zu sperren")
        continue
    if not zu:
        fehler.append(f"{name}: bezahlte Anfrage geht durch")
    if lp._aktiver_modus() in ("max", "cheap") and name != "Preisgrenze kein Zahlwert":
        fehler.append(f"{name}: /v1/models bietet im Modus '{lp._aktiver_modus()}' alles an")

# Gegenprobe: eine reparierte Datei gilt sofort wieder
setze('{"mode": "free"}', 40)
if lp._policy_blocked("vendor/frei:free", "vendor/frei:free")[0]:
    fehler.append("nach Reparatur ('free') wird ein freies Modell gesperrt")
if not bezahlt_gesperrt():
    fehler.append("nach Reparatur ('free') geht bezahlt durch")

# Gleichzeitiges Neuladen: wer waehrend des Parsens kommt, darf nicht den ALTEN
# Stand bekommen (beim Start ist das {} = 'max'). Parser kuenstlich verlangsamt.
import threading
import time
lp2 = lade()
with open(os.path.join(lp2.CONFIG_DIR, "policy.json"), "w") as f:
    f.write('{"mode": "local"}')
echt_load = lp2.json.load


def langsam(fp, *a, **k):
    time.sleep(0.3)
    return echt_load(fp, *a, **k)


lp2.json = type("J", (), {"load": staticmethod(langsam), "dumps": staticmethod(lp2.json.dumps),
                          "loads": staticmethod(lp2.json.loads)})
ergebnisse = []
th = [threading.Thread(target=lambda: ergebnisse.append(lp2._aktiver_modus())) for _ in range(2)]
for t in th:
    t.start()
    time.sleep(0.05)
for t in th:
    t.join()
if ergebnisse != ["local", "local"]:
    fehler.append(f"gleichzeitiges Neuladen liefert {ergebnisse} - der zweite sah den alten Stand")

# Unbekannter Modus: einmal protokolliert, nicht je Anfrage
lp3 = lade()
with open(os.path.join(lp3.CONFIG_DIR, "policy.json"), "w") as f:
    f.write('{"mode": "lokal"}')
for _ in range(5):
    lp3._aktiver_modus()
n = sum("lokal" in m for m in lp3._testlog)
if n != 1:
    fehler.append(f"unbekannter Modus {n}x protokolliert statt einmal")

for f in fehler:
    print("ROT:", f)
print("GRUEN" if not fehler else f"ROT ({len(fehler)})")
sys.exit(1 if fehler else 0)
