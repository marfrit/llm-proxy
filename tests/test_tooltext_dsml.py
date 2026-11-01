#!/usr/bin/env python3
"""Tool-Aufrufe als DSML-Text (DeepSeek V4.1 / V4): ToolTextSalvage rettet sie - und NUR sie.

Die Bloecke sind NICHT von Hand getippt. Sie entstehen aus DeepSeeks Referenz-Encoder, genau so,
wie das Modell seine eigenen Aufrufe im Verlauf sieht (und darum nachahmt): ein Assistenten-Zug
mit tool_calls, gerendert von encode_messages() und zwischen <｜Assistant｜></think> und
<｜end▁of▁sentence｜> herausgeschnitten. Die Encoder liegen unveraendert in tests/vendor/:
    deepseek_v41_encoding.py  sha256 fa501415211cd0e0c9f06ee6033c5466046e293b6153bca6200b70695b88fc73
    deepseek_v4_encoding.py   sha256 bfbb51e563b89c7d3581864cd0ea4d975b96e14fed034a222caf9d50c549b99d
= vllm/tokenizers/ aus vLLM (Herkunft je Datei mit Upstream-Commit: tests/vendor/README.md),
wie ein vLLM-Server mit --tool-call-parser deepseek_v41 sie ausliefert. vllm/parser/deepseek_v41.py hat
dieselben Terminale (<｜DSML｜ calls>, <｜DSML｜ invoke name=", <｜DSML｜ parameter).

ROT, wenn
  - ein vollstaendiger V4.1-Block (Prosa davor, string="true" und string="false") nicht als
    echter tool_call mit finish_reason=tool_calls ankommt, die Prosa verloren geht oder DSML im
    content bleibt; ebenso fuer zwei Aufrufe, einen doppelten Aufruf (nur einmal), einen
    JSON-Wert vom Typ array, boolean und number und die V4-Schreibweise ohne Leerzeichen;
  - ein Block im Codeblock, mitten in der Zeile, mit fremdem Namen, fremdem Parameter, fehlendem
    Pflichtparameter, Beispiel im Zaun nach einem Aufruf, falschem Typ (string="true" fuer integer, string="false" mit Nicht-JSON, NaN,
    integer fuer boolean, boolean fuer number),
    ohne string-Angabe, ohne Schluss-Huelle, unvollstaendig, mit finish_reason=length, gemischt
    mit der anderen Schreibweise oder ohne Tools in der Anfrage doch ausgefuehrt wuerde;
  - der Zug vom 2026-10-21 (nur die SCHLUESSE eines Aufrufs nach Prosa, kein Anfang, kein Name)
    veraendert wuerde - der ist nicht zu retten, er muss Text bleiben.
Gegenprobe: jeder Fall laeuft mit 1-, 7-, 64- und 100000-Byte-Haeppchen; die Rettungsfaelle
MUESSEN retten (gegen v1.0.8 sind sie rot), und am Ende wird gezaehlt, wie viele Faelle liefen.

Aufruf:  python3 tests/test_tooltext_dsml.py [pfad-zu-llm-proxy.py]
"""
import importlib.util
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from _lade import lade  # noqa: E402

lp = lade()
fail = 0
ran = 0


def check(cond, msg):
    global fail
    if not cond:
        print("ROT:", msg)
        fail += 1


def encoder(fname):
    spec = importlib.util.spec_from_file_location(fname[:-3], os.path.join(HERE, "vendor", fname))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


V41 = encoder("deepseek_v41_encoding.py")
V4 = encoder("deepseek_v4_encoding.py")


def turn(enc, content, calls):
    """Assistenten-Zug, wie der Encoder ihn rendert: content, dann der DSML-Block."""
    msgs = [{"role": "user", "content": "go"},
            {"role": "assistant", "content": content, "tool_calls": [
                {"type": "function", "function": {"name": n, "arguments": json.dumps(a)}} for n, a in calls]}]
    out = enc.encode_messages(msgs, thinking_mode="chat")
    out = out.split(enc.ASSISTANT_SP_TOKEN, 1)[1]
    assert out.startswith(enc.thinking_end_token) and out.endswith(enc.eos_token), repr(out)
    return out[len(enc.thinking_end_token):-len(enc.eos_token)]


def block(enc, calls):
    return turn(enc, "", calls).lstrip("\n")


def closer(enc, tag):
    """Schluss-Tag aus den Encoder-Konstanten (V4.1 hat Namen mit fuehrendem Leerzeichen)."""
    names = {"calls": enc.tool_calls_block_name,
             "invoke": getattr(enc, "tool_call_tag_name", "invoke"),
             "parameter": getattr(enc, "tool_parameter_tag_name", "parameter")}
    return f"</{enc.dsml_token}{names[tag]}>"


TOOLS = lp._tool_schemas([
    {"type": "function", "function": {"name": "bash", "parameters": {
        "type": "object", "properties": {"command": {"type": "string"}, "timeout": {"type": "integer"}},
        "required": ["command"]}}},
    {"type": "function", "function": {"name": "read", "parameters": {
        "type": "object", "properties": {"path": {"type": "string"}, "lines": {"type": "array"}},
        "required": ["path"]}}},
    {"type": "function", "function": {"name": "grep", "parameters": {
        "type": "object", "properties": {"pattern": {"type": "string"}, "ignore_case": {"type": "boolean"},
                                         "context": {"type": "number"}},
        "required": ["pattern"]}}},
])


def frame(delta, finish=None):
    return ("data: " + json.dumps({"id": "x", "object": "chat.completion.chunk", "model": "ds41-flash",
                                   "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]},
                                  ensure_ascii=False) + "\n\n").encode()


def stream(text, finish="stop", piece=5):
    out = frame({"role": "assistant", "content": ""})
    for i in range(0, len(text), piece):
        out += frame({"content": text[i:i + piece]})
    return out + frame({}, finish) + b"data: [DONE]\n\n"


def run(raw, size, tools=TOOLS):
    s = lp.ToolTextSalvage(tools)
    out = b""
    for i in range(0, len(raw), size):
        out += s.feed(raw[i:i + size])
    out += s.flush()
    return out, s.result


def parse(out):
    content, calls, fins, ok = "", [], [], True
    for ev in out.decode().split("\n\n"):
        datas = [l for l in ev.split("\n") if l.startswith("data: ")]
        ok = ok and len(datas) <= 1
        for l in datas:
            if l[6:] == "[DONE]":
                continue
            ch = json.loads(l[6:])["choices"][0]
            d = ch.get("delta") or {}
            content += d.get("content") or ""
            calls += d.get("tool_calls") or []
            if ch.get("finish_reason"):
                fins.append(ch["finish_reason"])
    return content, calls, fins, ok


def salvaged(name, text, want_calls, want_prose, finish="stop"):
    global ran
    for size in (1, 7, 64, 100000):
        for piece in (1, 5, len(text)):
            ran += 1
            out, res = run(stream(text, finish, piece), size)
            c, calls, fins, ok = parse(out)
            tag = f"{name} [{size}B, Stueck {piece}]"
            check(ok, f"{tag}: zwei JSON in einem SSE-Ereignis")
            got = [(x["function"]["name"], json.loads(x["function"]["arguments"])) for x in calls]
            check(got == want_calls, f"{tag}: Aufrufe {got} statt {want_calls} ({res})")
            check([x["index"] for x in calls] == list(range(len(calls))), f"{tag}: Indizes")
            check(fins == ["tool_calls"], f"{tag}: finish {fins} ({res})")
            check(V41.dsml_token not in c and V4.dsml_token not in c and c.split() == want_prose.split(), f"{tag}: content {c!r}")


def stays_text(name, text, finish="stop", tools=TOOLS):
    global ran
    for size in (1, 7, 64, 100000):
        for piece in (1, 5, len(text)):
            ran += 1
            out, res = run(stream(text, finish, piece), size, tools)
            c, calls, fins, ok = parse(out)
            tag = f"{name} [{size}B, Stueck {piece}]"
            check(not calls and fins == [finish], f"{tag}: ausgefuehrt ({res})")
            check(c == text, f"{tag}: Text veraendert {c!r}")


CMD = 'set -e\necho "a <b> $X"; tail -4'
BASH = ("bash", {"command": CMD, "timeout": 120})
READ = ("read", {"path": "/etc/hosts", "lines": [1, 20]})

# Selbstkontrolle der Konstruktion: der Encoder schreibt string="true" UND string="false"
b41 = block(V41, [BASH])
check('string="true"' in b41 and 'string="false"' in b41 and b41.startswith("<" + V41.dsml_token + " "),
      f"Encoder lieferte nicht die erwartete V4.1-Form: {b41!r}")

# ---- wird gerettet
prose = "Ich schaue nach."
salvaged("V4.1 Prosa + Aufruf", turn(V41, prose, [BASH]), [BASH], prose)
salvaged("V4.1 zwei Aufrufe", turn(V41, prose, [BASH, READ]), [BASH, READ], prose)
salvaged("V4.1 doppelter Aufruf", turn(V41, prose, [BASH, BASH]), [BASH], prose)
salvaged("V4.1 nur Block", block(V41, [READ]), [READ], "")
GREP = ("grep", {"pattern": "a|b", "ignore_case": True, "context": 2.5})
salvaged("V4.1 boolean und number", turn(V41, prose, [GREP]), [GREP], prose)
salvaged("V4 ohne Leerzeichen", turn(V4, prose, [BASH]), [BASH], prose)
salvaged("V4.1 Entwurf, Prosa, Fassung",
         turn(V41, prose, [("bash", {"command": "ls"})]) + "\n\nWait, longer listing.\n\n" + block(V41, [BASH]),
         [BASH], prose + " Wait, longer listing.")

# ---- bleibt Text
T = turn(V41, prose, [BASH])
P_OPEN = f'<{V41.dsml_token}{V41.tool_parameter_tag_name} name="'
cases = {
    "Codeblock": "So sieht das aus:\n\n```\n" + b41 + "\n```\n",
    "mitten in der Zeile": "Das Format ist " + b41 + " und so weiter.",
    "fremder Name": turn(V41, prose, [("rm_rf", {"command": "x"})]),
    "Parameter ausserhalb des Schemas": turn(V41, prose, [("bash", {"command": "ls", "sudo": True})]),
    "Pflicht fehlt": turn(V41, prose, [("bash", {"timeout": 5})]),
    'string="true" fuer integer': turn(V41, prose, [("bash", {"command": "ls", "timeout": "120"})]),
    'string="false" fuer string': turn(V41, prose, [("bash", {"command": 7})]),
    'string="false" mit Nicht-JSON': T.replace('string="false">120<', 'string="false">zwei Minuten<'),
    "integer fuer boolean": turn(V41, prose, [("grep", {"pattern": "x", "ignore_case": 1})]),
    "boolean fuer number": turn(V41, prose, [("grep", {"pattern": "x", "context": True})]),
    "NaN fuer number": turn(V41, prose, [("grep", {"pattern": "x", "context": 1})]).replace(
        'string="false">1<', 'string="false">NaN<'),
    "ohne string-Angabe": T.replace(' string="false"', ""),
    "ohne Schluss-Huelle": T[:-len(closer(V41, "calls"))].rstrip("\n"),
    "unvollstaendig": T[:T.rindex(closer(V41, "invoke"))],
    "V4.1 und V4 gemischt": T + "\n" + block(V4, [READ]),
    "Beispiel im Zaun nach Aufruf": T + "\n\nFor reference the format is:\n```\n" + block(V41, [READ]) + "\n```\n",
    "Erwaehnung nach Aufruf": T + "\n\nDas Format beginnt mit <" + V41.dsml_token + V41.tool_calls_block_name + ">.",
    # Form vom 2026-10-21: Prosa, dann NUR die Schluesse eines Aufrufs (kein <calls>, kein
    # <invoke name=...>). Ohne Namen nichts zu retten.
    "Vorfall 2026-10-21 (nur Schluesse)":
        "Ich lese weiter.\n\n" + T[T.index(closer(V41, "parameter")):],
}
for name, text in cases.items():
    stays_text(name, text)
stays_text("length", T, finish="length")
# ohne Tools in der Anfrage wird der Filter gar nicht gebaut; der Parser rettet dann auch nichts
check(lp.parse_text_tool_calls(b41, None) is None, "Parser rettet ohne Tools")
check(lp._tool_schemas(None) is None, "ohne Tools nicht None")
# Selbstkontrolle der Faelle: jeder enthaelt wirklich DSML (sonst pruefte er nichts)
for name, text in cases.items():
    check(V41.dsml_token in text, f"Fall {name} enthaelt kein DSML - prueft nichts")

if ran < 100:
    check(False, f"nur {ran} Laeufe")
print(f"GRUEN ({ran} Laeufe)" if not fail else f"ROT ({fail} von {ran} Laeufen)")
sys.exit(1 if fail else 0)
