#!/usr/bin/env python3
"""ReasoningSeam: kommt der Text im selben Frame wie finish_reason, muss er VOR dem Ende raus.

Gemessen 2026-10-18 mit DS-4.1 (kurze Antwort, letzter content-Frame traegt finish_reason "stop"):
bis 1.0.4 ging der finish-Frame zuerst raus und der zurueckgehaltene Text erst bei [DONE]. Clients,
die nach finish_reason nichts mehr lesen, verlieren die Antwort; ToolTextSalvage dahinter hielt den
Zug fuer beendet und rettete den Text-Aufruf nicht.

ROT, wenn content nach dem finish_reason ankommt, Text verloren geht oder zwei JSON in einem
SSE-Ereignis landen - fuer die Antwort ohne Naht, mit </think>-Naht im letzten Frame, und fuer die
Kette Seam -> ToolTextSalvage mit genau dem Strom, den DS-4.1 beim Rauchtest lieferte.
Gegenprobe: ein langer Strom (Seam gibt vorher auf) bleibt unveraendert in der Reihenfolge.
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


def frame(delta, finish=None):
    return ("data: " + json.dumps({"id": "c", "object": "chat.completion.chunk", "model": "ds",
                                   "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]})
            + "\n\n").encode()


def order(out):
    """[('c', text) | ('f', reason) | ('t', name)] in Sendereihenfolge, plus SSE-Gueltigkeit."""
    seq, ok = [], True
    for ev in out.decode().split("\n\n"):
        datas = [l for l in ev.split("\n") if l.startswith("data: ")]
        ok &= len(datas) <= 1
        for l in datas:
            if l[6:] == "[DONE]":
                seq.append(("D", ""))
                continue
            ch = json.loads(l[6:])["choices"][0]
            d = ch.get("delta") or {}
            if d.get("content"):
                seq.append(("c", d["content"]))
            for t in d.get("tool_calls") or []:
                seq.append(("t", t["function"]["name"]))
            if ch.get("finish_reason"):
                seq.append(("f", ch["finish_reason"]))
    return seq, ok


def through_seam(raw, size, salvage_tools=None):
    seam = lp.ReasoningSeam()
    sal = lp.ToolTextSalvage(salvage_tools) if salvage_tools else None
    out = b""
    for i in range(0, len(raw), size):
        part = seam.feed(raw[i:i + size])
        out += sal.feed(part) if sal and part else part
    rest = seam.flush()
    if sal:
        out += (sal.feed(rest) if rest else b"") + sal.flush()
    else:
        out += rest
    return out, (sal.result if sal else None)


def text_before_finish(seq):
    fi = [i for i, (k, _) in enumerate(seq) if k == "f"]
    return fi and all(i < fi[0] for i, (k, _) in enumerate(seq) if k in ("c", "t"))


cases = {
    "kurze Antwort, finish im content-Frame":
        (frame({"role": "assistant", "content": ""}) + frame({"content": "O"}) + frame({"content": "K"}, "stop")
         + b"data: [DONE]\n\n", "OK"),
    "Naht im letzten Frame":
        (frame({"role": "assistant", "content": ""}) + frame({"reasoning": "hm"})
         + frame({"content": "denk.</think>Ja"}, "stop") + b"data: [DONE]\n\n", "Ja"),
    "reasoning, Naht und finish in EINEM Frame":
        (frame({"role": "assistant", "content": ""})
         + frame({"reasoning": "tail", "content": "x</think>Ja"}, "stop") + b"data: [DONE]\n\n", "Ja"),
    "role, Text und finish in EINEM Frame":
        (frame({"role": "assistant", "content": "OK"}, "stop") + b"data: [DONE]\n\n", "OK"),
}


def order_full(out):
    """Reihenfolge inklusive role/reasoning."""
    seq = []
    for ev in out.decode().split("\n\n"):
        for l in ev.split("\n"):
            if l.startswith("data: {"):
                ch = json.loads(l[6:])["choices"][0]
                d = ch.get("delta") or {}
                for k in ("role", "reasoning", "content"):
                    if d.get(k):
                        seq.append(k)
                if ch.get("finish_reason"):
                    seq.append("finish")
    return seq


for size in (1, 7, 100000):
    out, _ = through_seam(cases["reasoning, Naht und finish in EINEM Frame"][0], size)
    sq = [x for x in order_full(out) if x != "role"]
    check(sq == ["reasoning", "content", "finish"], f"reasoning vor Text vor finish [{size}B]: {sq}")
    out, _ = through_seam(cases["role, Text und finish in EINEM Frame"][0], size)
    check(order_full(out) == ["role", "content", "finish"], f"role vor Text vor finish [{size}B]: {order_full(out)}")

for name, (raw, want) in cases.items():
    for size in (1, 7, 100000):
        out, _ = through_seam(raw, size)
        seq, ok = order(out)
        text = "".join(t for k, t in seq if k == "c")
        check(ok, f"{name} [{size}B]: zwei JSON in einem Ereignis")
        check(text == want, f"{name} [{size}B]: Text {text!r} statt {want!r}")
        check(text_before_finish(seq), f"{name} [{size}B]: Text nach finish_reason: {seq}")

# die Kette mit dem echten Rauchtest-Strom von DS-4.1 (2026-10-18): wird ein tool_call
SMOKE = [frame({"role": "assistant", "content": ""}), frame({"content": "<invoke name="}),
         frame({"content": "\"bash\">\n<parameter"}), frame({"content": " name=\"command\">"}),
         frame({"content": "echo smoke"}), frame({"content": "-1.0.4"}), frame({"content": "</parameter>\n"}),
         frame({"content": "</invoke>"}, "stop"), b"data: [DONE]\n\n"]
TOOLS = lp._tool_schemas([{"type": "function", "function": {"name": "bash", "parameters": {
    "type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}}}], "auto")
for size in (1, 7, 100000):
    out, res = through_seam(b"".join(SMOKE), size, TOOLS)
    seq, ok = order(out)
    check(ok and ("t", "bash") in seq and ("f", "tool_calls") in seq and text_before_finish(seq),
          f"Rauchtest-Strom [{size}B]: {res} {seq}")

# Gegenprobe: langer Strom, die Naht gibt vor dem Ende auf - Reihenfolge wie gehabt
long_raw = (frame({"role": "assistant", "content": ""})
            + b"".join(frame({"content": f"Wort{i} "}) for i in range(30)) + frame({}, "stop") + b"data: [DONE]\n\n")
out, _ = through_seam(long_raw, 64)
seq, ok = order(out)
check(ok and "".join(t for k, t in seq if k == "c") == "".join(f"Wort{i} " for i in range(30))
      and text_before_finish(seq), f"langer Strom: {seq[-3:]}")

print("GRUEN" if not fail else f"ROT ({fail})")
sys.exit(1 if fail else 0)
