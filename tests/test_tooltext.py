#!/usr/bin/env python3
"""Tool-Aufrufe als Text: ToolTextSalvage rettet sie - und NUR sie.

ROT, wenn
  - der Fall vom 2026-10-17 (zwei gleiche <invoke>-Bloecke mit Prosa dazwischen, der Marker
    ueber zwei Deltas zerrissen) nicht als EIN echter tool_call mit finish_reason=tool_calls
    ankommt, oder der Text davor verloren geht;
  - ein Beispiel im Codeblock, eine Erwaehnung mitten in der Zeile, ein fremder Tool-Name,
    ein Parameter ausserhalb des Schemas, ein fehlender Pflichtparameter, ein falscher Typ
    oder eine abgeschnittene Antwort (finish_reason=length) doch ausgefuehrt wuerde;
  - ein echter tool_call oder ein Strom ohne Marker veraendert wird;
  - zwei JSON-Objekte in einem SSE-Ereignis landen (der Fehler von 2026-08-08).
Gegenprobe: jeder Fall laeuft mit 1-, 7- und 64-Byte-Haeppchen, damit ein Filter, der nur
ganze Frames versteht, nicht durchkommt; und der Rettungsfall MUSS retten, sonst bestuende
ein Filter, der nie etwas tut, alle "bleibt Text"-Faelle.

Aufruf:  python3 tests/test_tooltext.py [pfad-zu-llm-proxy.py]
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _lade import lade  # noqa: E402

lp = lade()
fail = 0


def check(cond, msg):
    global fail
    if not cond:
        print("ROT:", msg)
        fail += 1


TOOLS = lp._tool_schemas([
    {"type": "function", "function": {"name": "bash", "parameters": {
        "type": "object", "properties": {"command": {"type": "string"}, "timeout": {"type": "integer"}},
        "required": ["command"]}}},
    {"type": "function", "function": {"name": "read", "parameters": {
        "type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}},
])


def frame(delta, finish=None):
    return ("data: " + json.dumps({"id": "x", "object": "chat.completion.chunk", "model": "ds",
                                   "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]})
            + "\n\n").encode()


def stream(pieces, finish="stop", reasoning=True):
    out = frame({"role": "assistant", "content": ""})
    if reasoning:
        out += frame({"reasoning": "I should run it."})
    for p in pieces:
        out += frame({"content": p})
    out += frame({}, finish) + b"data: [DONE]\n\n"
    return out


def run(raw, size, tools=TOOLS):
    s = lp.ToolTextSalvage(tools)
    out = b""
    for i in range(0, len(raw), size):
        out += s.feed(raw[i:i + size])
    out += s.flush()
    return out, s.result


def parse(out):
    content, calls, fins, events_ok = "", [], [], True
    for ev in out.decode().split("\n\n"):
        datas = [l for l in ev.split("\n") if l.startswith("data: ")]
        if len(datas) > 1:
            events_ok = False
        for l in datas:
            p = l[6:]
            if p == "[DONE]":
                continue
            ch = json.loads(p)["choices"][0]
            d = ch.get("delta") or {}
            content += d.get("content") or ""
            calls += d.get("tool_calls") or []
            if ch.get("finish_reason"):
                fins.append(ch["finish_reason"])
    return content, calls, fins, events_ok


BLOCK = ('<invoke name="bash">\n<parameter name="command">set -e\necho "a <b> $X"; tail -4</parameter>\n'
         '<parameter name="timeout">120</parameter>\n</invoke>')
INCIDENT = ["Profiling now.\n\n<inv", "oke" + BLOCK[len("<invoke"):][:30], BLOCK[len("<invoke") + 30:],
            "\n\nLet me send.\n\n", BLOCK, "\n"]


def all_sizes(name, raw, want, tools=TOOLS):
    for size in (1, 7, 64, 100000):
        out, res = run(raw, size, tools)
        content, calls, fins, ok = parse(out)
        check(ok, f"{name} [{size}B]: zwei JSON in einem SSE-Ereignis")
        want(name, size, content, calls, fins, res)


# 1. der Vorfall: wird EIN tool_call, Text davor bleibt, Prosa dazwischen bleibt Text
def w_incident(name, size, content, calls, fins, res):
    check(len(calls) == 1, f"{name} [{size}B]: {len(calls)} tool_calls statt 1 (dedupliziert): {res}")
    if calls:
        f = calls[0]["function"]
        a = json.loads(f["arguments"])
        check(f["name"] == "bash" and a == {"command": 'set -e\necho "a <b> $X"; tail -4', "timeout": 120},
              f"{name} [{size}B]: falsche Argumente {a}")
    check(fins == ["tool_calls"], f"{name} [{size}B]: finish {fins}")
    check(content.startswith("Profiling now.") and "Let me send." in content and "<invoke" not in content,
          f"{name} [{size}B]: content {content!r}")


all_sizes("Vorfall", stream(INCIDENT), w_incident)


def stays_text(text):
    def w(name, size, content, calls, fins, res):
        check(not calls and fins == ["stop"], f"{name} [{size}B]: wurde ausgefuehrt ({res})")
        check(content == text, f"{name} [{size}B]: Text veraendert {content!r}")
    return w


# 2.-9. bleibt Text
cases = {
    "Codeblock": "So sieht das aus:\n\n```\n" + BLOCK + "\n```\n",
    "mitten in der Zeile": "Das Format ist " + BLOCK + " und so weiter.",
    "fremder Name": BLOCK.replace('name="bash"', 'name="rm_rf"'),
    "Parameter ausserhalb des Schemas": BLOCK.replace('name="timeout"', 'name="sudo"'),
    "Pflicht fehlt": '<invoke name="bash">\n<parameter name="timeout">5</parameter>\n</invoke>',
    "falscher Typ": BLOCK.replace(">120<", ">zwei Minuten<"),
    "unvollstaendig": BLOCK[:-len("</invoke>")],
    "Huelle ohne Schluss": "<function_calls>\n" + BLOCK,
    "Schluss ohne Huelle": BLOCK + "\n</function_calls>",
    "kein Marker": "Ganz normale Antwort mit < und > und <inv aber ohne Aufruf.",
}
for name, text in cases.items():
    all_sizes(name, stream([text[i:i + 9] for i in range(0, len(text), 9)]), stays_text(text))

# mitten in der Zeile, aber in EINEM Delta (dann greift nur die Zeilenanfang-Pruefung beim Fund)
mid = "Das Format ist " + BLOCK + " und so weiter."
all_sizes("mitten in der Zeile, ein Delta", stream([mid]), stays_text(mid))

# halb zurueckgehaltener Marker, dann ein ECHTER tool_call: Text zuerst, Aufruf unveraendert, kein Zusatz
mixed = (frame({"role": "assistant", "content": ""}) + frame({"content": "Gleich:\n<inv"})
         + frame({"tool_calls": [{"index": 0, "id": "c1", "type": "function",
                                  "function": {"name": "bash", "arguments": "{\"command\":\"ls\"}"}}]})
         + frame({}, "tool_calls") + b"data: [DONE]\n\n")
for size in (1, 7, 64):
    out, _ = run(mixed, size)
    c, calls, fins, ok = parse(out)
    txt = out.decode()
    check(c == "Gleich:\n<inv" and len(calls) == 1 and calls[0]["id"] == "c1" and fins == ["tool_calls"],
          f"echter Aufruf nach Marker-Anfang [{size}B]: {c!r} {calls}")
    check(txt.find("<inv") < txt.find('"c1"'), f"echter Aufruf nach Marker-Anfang [{size}B]: Zurueckgehaltenes kam nach dem Aufruf")

# --- Review 2026-10-17: Marker am Delta-Anfang ist NICHT automatisch Zeilenanfang (B1)
RM = BLOCK.replace('set -e\necho "a <b> $X"; tail -4', "rm -rf /tmp/x")
for name, pieces in {
    "B1 eigenes Delta nach Satzanfang": ["Das Format ist ", RM, " und so weiter."],
    "B1 '<' und 'invoke' getrennt": ["Das Format ist", " <", "invoke", RM[len("<invoke"):]],
    "B1 Inline-Code": ["Use `", RM, "` to call."],
    "B1 Zitat": ["> ", RM],
    "vier Leerzeichen eingerueckt": ["Beispiel:\n\n    ", RM.replace("\n", "\n    ")],
    "Tilde-Zaun": ["So:\n~~~\n", RM, "\n~~~\n"],
}.items():
    text = "".join(pieces)
    for size in (1, 7, 100000):
        out, res = run(stream(pieces), size)
        c, calls, fins, ok = parse(out)
        check(not calls and fins == ["stop"] and c == text, f"{name} [{size}B]: ausgefuehrt/veraendert ({res}) {c!r}")

# B2: Beispiel im Zaun NACH einem echten Aufruf verdraengt ihn nicht und laeuft nicht
real_then_example = (BLOCK.replace('set -e\necho "a <b> $X"; tail -4', "make test")
                     + "\n\nFor reference the format is:\n```\n" + RM + "\n```\n")
all_sizes("B2 Beispiel nach Aufruf", stream([real_then_example]), stays_text(real_then_example))
mid_after = BLOCK + "\n\nAlso possible: " + RM
all_sizes("B2 Erwaehnung nach Aufruf", stream([mid_after]), stays_text(mid_after))

# ein einzelnes ``` mitten im Satz kippt den Zaunzaehler nicht (der naechste Zaun-Block bleibt Text)
flip = "Wrap it in ``` fences like this:\n\n```\n" + RM + "\n```\n"
all_sizes("Inline-``` kippt nichts", stream([flip]), stays_text(flip))

# H1: <function_calls> IN einem Parameterwert bleibt erhalten
grepcmd = BLOCK.replace('set -e\necho "a <b> $X"; tail -4', "grep -c '<function_calls>' log")
all_sizes("H1 Huelle im Wert", stream([grepcmd]),
          lambda n, s, c, calls, fins, r: check(
              calls and json.loads(calls[0]["function"]["arguments"])["command"] == "grep -c '<function_calls>' log",
              f"{n} [{s}B]: Wert veraendert {calls and calls[0]['function']['arguments']} {r}"))

# H2: die Huelle landet nicht im content, auch im selben Delta wie der Text davor
all_sizes("H2 Huelle im selben Delta", stream(["Ok.\n<function_calls>\n" + BLOCK + "\n</function_calls>"]),
          lambda n, s, c, calls, fins, r: check(len(calls) == 1 and "function_calls" not in c and c == "Ok.\n",
                                                f"{n} [{s}B]: content {c!r} {r}"))

# M1: kaputtes Schema wirft nicht und rettet nichts Falsches
check(lp._tool_schemas([{"type": "function", "function": {"name": "bash", "parameters": {
    "type": "object", "properties": {"command": {"type": "string"}}, "required": True}}}]) is not None,
      "M1: required=true warf oder verlor das Tool")
check(lp._tool_schemas([{"function": {"name": "x", "parameters": {"required": [{"a": 1}]}}}]) is not None,
      "M1: required=[dict] warf")

# M2: Tool-Call-IDs sind je Aufruf neu
ids = set()
for _ in range(3):
    out, _ = run(stream([BLOCK]), 100000)
    ids |= {x["id"] for x in parse(out)[1]}
check(len(ids) == 3, f"M2: IDs wiederholen sich: {ids}")

# M3: tool_choice none -> nichts retten; benannte Funktion -> nur diese
check(lp._tool_schemas([{"type": "function", "function": {"name": "bash"}}], "none") is None, "M3: none")
only_read = lp._tool_schemas([{"type": "function", "function": {"name": "bash", "parameters": {}}},
                              {"type": "function", "function": {"name": "read", "parameters": {}}}],
                             {"type": "function", "function": {"name": "read"}})
check(list(only_read) == ["read"], f"M3: benannte Funktion {only_read}")
# parallel_tool_calls=false: zwei Aufrufe nicht raten
s2 = lp.ToolTextSalvage(TOOLS, parallel=False)
out = s2.feed(stream([BLOCK + "\n" + '<invoke name="read">\n<parameter name="path">/x</parameter>\n</invoke>']))
check(not parse(out + s2.flush())[1], "M3: parallel=false fuehrte zwei Aufrufe aus")

# M4: Haltegrenze - ein riesiger "Block" wird freigegeben statt endlos zurueckgehalten
L = lp._TT_MAX_HOLD
huge = "<invoke name=\"bash\">\n" + "x" * (L + 5000)
s3 = lp.ToolTextSalvage(TOOLS)
mid = s3.feed(frame({"content": huge[:L - 1000]}) + frame({"content": huge[L - 1000:]}))
c_mid = parse(mid)[0]
check(len(c_mid) == len(huge), f"M4: vor dem Zugende nur {len(c_mid)} von {len(huge)} Zeichen freigegeben")

# n>1 choices: nicht anfassen
two_choice = ("data: " + json.dumps({"choices": [{"index": 0, "delta": {"content": BLOCK}, "finish_reason": None},
                                                {"index": 1, "delta": {"content": "x"}, "finish_reason": None}]})
              + "\n\n").encode() + frame({}, "stop") + b"data: [DONE]\n\n"
out, res = run(two_choice, 100000)
check(not parse(out)[1], f"n>1: ausgefuehrt ({res})")

# Byte-Gleichheit ohne Marker: jeder Frame geht unveraendert raus
plain = stream(["Ganz normal.\n", "Zweite Zeile ", "mit Text.\n", "Ende"])
out, _ = run(plain, 100000)
check(out == plain, "ohne Marker nicht byte-gleich")

# verschachtelte Zaeune (CommonMark): ```bash in ````markdown, ~~~ in ```, ```bash in ``` - alles Inhalt
for name, text in {
    "````markdown um ```bash": "So zeigt man es:\n\n````markdown\n```bash\n" + RM + "\n```\n````\n",
    "~~~ in ```": "```\n~~~\n" + RM + "\n~~~\n```\n",
    "```bash in offenem ```": "```\n```bash\n" + RM + "\n```\n",
}.items():
    for size in (1, 7, 100000):
        for pieces in ([text], [text[i:i + 5] for i in range(0, len(text), 5)]):
            out, res = run(stream(pieces), size)
            c, calls, fins, ok = parse(out)
            check(not calls and c == text, f"Zaun {name} [{size}B, {len(pieces)} Stuecke]: ausgefuehrt ({res})")
# und ein Block NACH einem sauber geschlossenen verschachtelten Zaun darf laufen
after = "````markdown\n```bash\nx\n```\n````\n" + BLOCK
all_sizes("Aufruf nach geschlossenem Zaun", stream([after]),
          lambda n, s, c, calls, fins, r: check(len(calls) == 1 and fins == ["tool_calls"], f"{n} [{s}B]: {r}"))

# tief verschachteltes JSON in einem array-Parameter: kein Absturz, bleibt Text
DEEP = lp._tool_schemas([{"type": "function", "function": {"name": "put", "parameters": {
    "type": "object", "properties": {"v": {"type": "array"}}, "required": ["v"]}}}])
deep = '<invoke name="put">\n<parameter name="v">' + "[" * 20000 + "]" * 20000 + '</parameter>\n</invoke>'
try:
    out, res = run(stream([deep]), 100000, tools=DEEP)
    c, calls, fins, ok = parse(out)
    check(not calls and c == deep and fins == ["stop"], f"tiefes JSON: {res}")
except RecursionError:
    check(False, "tiefes JSON: RecursionError bis zum Relais")

# abgeschnitten: finish_reason=length
all_sizes("length", stream([BLOCK], finish="length"),
          lambda n, s, c, calls, fins, r: check(not calls and fins == ["length"] and c == BLOCK,
                                                f"{n} [{s}B]: {r} {fins}"))

# Entwurf, Prosa, verbesserter Block (Nachricht 241 vom 2026-10-17): nur der letzte laeuft
revised = BLOCK.replace("tail -4", "tail -8")
all_sizes("Entwurf und Fassung", stream([BLOCK + "\n\nWait, grep broadly.\n\n" + revised]),
          lambda n, s, c, calls, fins, r: check(
              len(calls) == 1 and "tail -8" in calls[0]["function"]["arguments"] and fins == ["tool_calls"]
              and "<invoke" not in c and "grep broadly" in c, f"{n} [{s}B]: {r} {c!r}"))

# zwei verschiedene Aufrufe -> zwei tool_calls mit Index 0 und 1
two = BLOCK + "\n" + '<invoke name="read">\n<parameter name="path">/etc/hosts</parameter>\n</invoke>'
all_sizes("zwei Aufrufe", stream([two]),
          lambda n, s, c, calls, fins, r: check(
              [x["function"]["name"] for x in calls] == ["bash", "read"] and [x["index"] for x in calls] == [0, 1]
              and fins == ["tool_calls"], f"{n} [{s}B]: {r} {[x['function']['name'] for x in calls]}"))

# <function_calls>-Huelle (Anthropic-Form) wird ebenso erkannt
all_sizes("function_calls-Huelle", stream(["<function_calls>\n" + BLOCK + "\n</function_calls>"]),
          lambda n, s, c, calls, fins, r: check(len(calls) == 1 and fins == ["tool_calls"], f"{n} [{s}B]: {r}"))

# echter tool_call vom Backend: byte-gleich durchgereicht
real = (frame({"role": "assistant", "content": ""})
        + frame({"tool_calls": [{"index": 0, "id": "c1", "type": "function",
                                 "function": {"name": "bash", "arguments": "{\"command\":\"ls\"}"}}]})
        + frame({}, "tool_calls") + b"data: [DONE]\n\n")
for size in (1, 7, 64):
    out, _ = run(real, size)
    check(out == real, f"echter tool_call [{size}B]: veraendert")

# ohne Tools in der Anfrage: _tool_schemas liefert None, der Filter wird gar nicht gebaut
check(lp._tool_schemas(None) is None and lp._tool_schemas([]) is None, "ohne Tools nicht None")
check(lp.parse_text_tool_calls(BLOCK, None) is None, "Parser rettet ohne Tools")

# Strom endet ohne finish_reason: Zurueckgehaltenes darf nicht verschwinden
raw = frame({"content": "vorher\n"}) + frame({"content": BLOCK})
out, res = run(raw, 5)
c, calls, fins, ok = parse(out)
check(c == "vorher\n" + BLOCK and not calls, f"Stromende ohne finish: {c!r} {res}")

print("GRUEN" if not fail else f"ROT ({fail})")
sys.exit(1 if fail else 0)
