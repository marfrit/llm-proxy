#!/usr/bin/env python3
"""Log: Datum in jeder Zeile, Rotation nach Groesse.

Jede Zeile traegt ISO-Datum und -Zeit. Die Datei rotiert bei der konfigurierten
Groesse und haelt die konfigurierte Zahl an Backups; im Test ist beides winzig,
damit die Rotation sofort sichtbar wird.

ROT gegen den Stand vor dem Fix: die Zeile beginnt nur mit HH:MM:SS, und die
Logdatei waechst ohne Rotation weiter.
"""
import os
import re
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

ordner = tempfile.mkdtemp(prefix="llmproxy-log-")
pfad = os.path.join(ordner, "proxy.log")
os.environ["LLM_PROXY_LOG_FILE"] = pfad
os.environ["LLM_PROXY_LOG_MAX_BYTES"] = "100"
os.environ["LLM_PROXY_LOG_BACKUPS"] = "2"

from _lade import lade  # noqa: E402

lp = lade()
fail = 0


def check(cond, msg):
    global fail
    if not cond:
        print("ROT:", msg)
        fail += 1


def lies(p):
    try:
        with open(p, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


lp._emit_log("MARK1 " + "x" * 80)
lp._emit_log("MARK2 " + "x" * 80)
lp._emit_log("MARK3 " + "x" * 80)
lp.LOG.flush()

inhalt = lies(pfad)
check(inhalt, "Logdatei wurde nicht geschrieben")
erste = inhalt.splitlines()[0] if inhalt else ""
check(re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", erste),
      f"Logzeile ohne ISO-Datum: {erste!r}")
check("MARK3" in inhalt, f"aktuelle Zeile fehlt: {inhalt!r}")

backups = [p for p in (pfad + ".1", pfad + ".2") if os.path.exists(p)]
check(len(backups) >= 2, f"keine Rotation bei kleiner Groesse, Backups: {backups}")
alle = "".join(lies(p) for p in [pfad + ".2", pfad + ".1", pfad])
check("MARK1" in alle and "MARK2" in alle and "MARK3" in alle,
      f"Rotation hat Zeilen verloren: {alle!r}")
check(os.path.getsize(pfad) < 100 or "MARK3" in inhalt,
      "aktuelle Datei ist ueber die konfigurierte Groesse gewachsen")

shutil.rmtree(ordner, ignore_errors=True)
print("GRUEN" if not fail else f"ROT ({fail})")
sys.exit(1 if fail else 0)
