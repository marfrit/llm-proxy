#!/usr/bin/env python3
"""Render input: der Render-Input des Gateways muss byte-gleich zu dem sein, was der
Engine-Render aus derselben Anfrage baut.

ROT, wenn Tools in Einfuegereihenfolge statt in der nlohmann-Reihenfolge
(Schluessel alphabetisch, rekursiv) gerendert werden, oder ein
Arguments-String bei einem Template mit `arguments|items` nicht als Objekt
(sortiert) uebergeben wird. Die echte Qwen-Coder-Vorlage liegt in
tests/fixtures/.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _lade import lade  # noqa: E402
from _feyntalk_fixture import MODEL, make_material, start_engine  # noqa: E402

from feyntalk_render.render import _minja_tojson, render_prompt  # noqa: E402

lp = lade()
HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATE = open(os.path.join(HERE, "fixtures", "qwen3.6-35b-a3b-chat-template.jinja")).read()

fail = 0
checks = 0


def check(cond, msg):
    global fail, checks
    checks += 1
    if not cond:
        print("ROT:", msg)
        fail += 1


def nlohmann(obj):
    # The engine hands the template nlohmann::json, whose objects are std::map:
    # keys sorted recursively at every level. minja's tojson keeps that order
    # and uses these separators. json.dumps(sort_keys=True) is the reference a
    # Python dict must reproduce by insertion order.
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(", ", ": "))


# The tools block's tags, built from parts: no marker string is spelled here.
_TOOLS_OPEN = "<" + "tools" + ">"
_TOOLS_CLOSE = "</" + "tools" + ">"


def tools_block(text):
    start = text.index(_TOOLS_OPEN) + len(_TOOLS_OPEN)
    return text[start:text.index(_TOOLS_CLOSE)].strip()


FLAT = [{"type": "function", "name": "read_file", "description": "Read a file.",
         "parameters": {"type": "object",
                        "properties": {"path": {"type": "string"}},
                        "required": ["path"]}}]
CHAT = [{"type": "function",
         "function": {"name": "read_file", "description": "Read a file.",
                      "parameters": {"type": "object",
                                     "properties": {"path": {"type": "string"}},
                                     "required": ["path"]}}}]
EXPECTED = [{"type": "function",
             "function": {"name": "read_file", "description": "Read a file.",
                          "parameters": {"type": "object",
                                         "properties": {"path": {"type": "string"}},
                                         "required": ["path"]}}}]

# 1) Beide Request-Formen werden zur Engine-Form in nlohmann-Reihenfolge
flat_render = lp._responses_tools_for_render(FLAT)
chat_render = lp._chat_tools_for_render(CHAT)
check(json.dumps(flat_render, ensure_ascii=False) == nlohmann(EXPECTED),
      f"Responses-Tools nicht in nlohmann-Reihenfolge: {flat_render!r}")
check(json.dumps(chat_render, ensure_ascii=False) == nlohmann(EXPECTED),
      f"Chat-Tools nicht in nlohmann-Reihenfolge: {chat_render!r}")
check(list(flat_render[0]) == ["function", "type"],
      f"Schluessel oben nicht sortiert: {list(flat_render[0])}")
check(list(flat_render[0]["function"]) == ["description", "name", "parameters"],
      f"function-Schluessel nicht sortiert: {list(flat_render[0]['function'])}")
check(list(flat_render[0]["function"]["parameters"]) == ["properties", "required", "type"],
      f"parameters-Schluessel nicht sortiert: {list(flat_render[0]['function']['parameters'])}")

# 2) Die echte Vorlage rendert diese Tools in genau dieser Reihenfolge
material = make_material(template=TEMPLATE)
res = render_prompt(material, [{"role": "user", "content": "Hi"}],
                    tools=flat_render, add_generation_prompt=True)
check(tools_block(res.text) == nlohmann(EXPECTED[0]),
      f"echte Vorlage rendert Tools anders: {tools_block(res.text)!r}")

# 2b) Tool-Parameter gehen durch nlohmanns Float-Format ([1e15, 1e16) exponent).
float_tool = lp._responses_tools_for_render(
    [{"type": "function", "name": "f",
      "parameters": {"type": "number", "minimum": 1e15}}])
res_float = render_prompt(material, [{"role": "user", "content": "Hi"}],
                          tools=float_tool, add_generation_prompt=True)
check('"minimum": 1e+15' in tools_block(res_float.text),
      f"Tool-Parameter nicht im nlohmann-Float-Format: {tools_block(res_float.text)!r}")

# 3) Fehlende description/parameters sind die Engine-Defaults "", {}
empty = lp._responses_tools_for_render([{"type": "function", "name": "noop"}])
check(empty[0]["function"]["description"] == "", f"description fehlt: {empty!r}")
check(empty[0]["function"]["parameters"] == {}, f"parameters fehlt: {empty!r}")

# 4) /props' supports_object_arguments kommt in der Faehigkeit an
srv, E = start_engine(make_material(template=TEMPLATE),
                      caps={"supports_object_arguments": True})
lp.LOCAL_BACKENDS[:] = [("eng",) + srv.server_address[:2]]
lp.BACKEND_FEYNTALK.clear()
lp.BACKEND_FEYNTALK["eng"] = "tokens"
lp._FEYNTALK_WARNED.clear()
with lp.FEYNTALK_CACHE_LOCK:
    lp.FEYNTALK_CACHE.clear()
    lp.FEYNTALK_MATERIALS.clear()
lp._build_sems({"eng": 4})
lp._disc_cache.clear()
cap = lp._feyntalk_capability(("eng",) + srv.server_address[:2])
check(cap.get("mode") == "tokens", f"Capability: {cap}")
check(cap.get("object_arguments") is True, f"object_arguments fehlt: {cap}")

# 5) Responses-Anfrage mit Tool-Historie: die Argumente werden Objekt, der
#    Prompt-Hash trifft den Referenz-Render (sortierte Tools, sortierte Args).
input_items = [
    {"type": "message", "role": "user", "content": "Hi"},
    {"type": "function_call", "call_id": "call_1", "name": "read_file",
     "arguments": '{"path": "data.txt", "mode": "r"}'},
]
request = {"model": MODEL, "stream": True, "input": input_items, "tools": FLAT}
token = lp._feyntalk_responses_request(json.dumps(request).encode(), cap, MODEL)
ref_messages = [
    {"role": "user", "content": "Hi"},
    {"role": "assistant", "content": None,
     "tool_calls": [{"id": "call_1", "type": "function",
                     "function": {"name": "read_file",
                                  "arguments": {"mode": "r", "path": "data.txt"}}}]},
]
ref_tools = json.loads(nlohmann(EXPECTED))  # keys in sorted order
ref = render_prompt(cap["material"], ref_messages, tools=ref_tools,
                    add_generation_prompt=True)
sent = json.loads(token["body"])
check(sent["arcint"]["render"]["prompt_sha256"] == ref.prompt_sha256,
      f"Prompt-Hash der Tool-Historie weicht ab: {sent['arcint']['render']}")
check("call_1" in token["body"].decode(), "function_call fehlt im Engine-Request")
check(b'"arguments": "{\\"path\\"' in token["body"] or b'"arguments":"{\\"path\\"' in token["body"],
      "der Engine-Request muss die Argumente als String behalten")

# 5b) Ein Template ohne `arguments|items` behaelt den String: kein Divergenz-Risiko.
#     Ohne chat_template_caps in /props wird das einmal pro Backend geloggt.
srv.shutdown()
srv2, E2 = start_engine(make_material(), caps=None)
lp.LOCAL_BACKENDS[:] = [("eng2",) + srv2.server_address[:2]]
lp.BACKEND_FEYNTALK.clear()
lp.BACKEND_FEYNTALK["eng2"] = "tokens"
lp._FEYNTALK_WARNED.clear()
with lp.FEYNTALK_CACHE_LOCK:
    lp.FEYNTALK_CACHE.clear()
    lp.FEYNTALK_MATERIALS.clear()
lp._disc_cache.clear()
cap2 = lp._feyntalk_capability(("eng2",) + srv2.server_address[:2])
check(cap2.get("object_arguments") is False, f"ohne Caps nicht False: {cap2}")
nocaps = [z for z in lp._testlog if "chat_template_caps" in z]
check(len(nocaps) == 1, f"fehlende Caps nicht genau einmal geloggt: {nocaps}")
msgs = lp._responses_messages_from_input(input_items)
lp._render_tool_arguments(msgs, cap2.get("object_arguments"))
check(msgs[-1]["tool_calls"][0]["function"]["arguments"] == '{"path": "data.txt", "mode": "r"}',
      f"String-Template bekam ein Objekt: {msgs[-1]['tool_calls'][0]['function']['arguments']!r}")
# Cache neu laden, Warnung bleibt aus (einmal pro Backend).
with lp.FEYNTALK_CACHE_LOCK:
    lp.FEYNTALK_CACHE.clear()
cap2b = lp._feyntalk_capability(("eng2",) + srv2.server_address[:2])
check(cap2b.get("mode") == "tokens", f"zweiter Abruf: {cap2b}")
check(len([z for z in lp._testlog if "chat_template_caps" in z]) == 1,
      f"fehlende Caps mehrfach geloggt: {lp._testlog}")
srv2.shutdown()

# 6) Der gemessene nlohmann-Argumentkatalog: Objekte werden byte-gleich zur
#    Engine serialisiert, Nicht-Objekte und abgelehnte Dokumente bleiben Strings.
fixture_dir = os.path.join(HERE, "fixtures")
case_lines = open(os.path.join(fixture_dir, "nlohmann_arguments_cases.jsonl"),
                  encoding="utf-8").read().split("\n")
expected_lines = open(os.path.join(fixture_dir, "nlohmann_arguments_expected.txt"),
                      encoding="utf-8").read().split("\n")
while case_lines and case_lines[-1] == "":
    case_lines.pop()
while expected_lines and expected_lines[-1] == "":
    expected_lines.pop()
check(len(case_lines) == len(expected_lines) == 31,
      f"Katalog: {len(case_lines)} Faelle, {len(expected_lines)} Erwartungen")
for case, expected in zip(case_lines, expected_lines):
    value = lp._arguments_for_render(case, True)
    if expected == "DISCARDED" or not case.lstrip().startswith("{"):
        # nlohmann refuses it, or it is not an object: the engine keeps the string.
        check(value == case, f"{case!r}: roher String erwartet, bekam {value!r}")
        continue
    rendered = _minja_tojson(value)
    check(rendered == expected, f"{case!r}: {rendered!r} != Engine {expected!r}")

# 6b) Die Konstanten- und Zahlengrenzen, die der Katalog nicht enthaelt.
check(lp._arguments_for_render('{"x": -Infinity}', True) == '{"x": -Infinity}',
      "-Infinity muss roher String bleiben")
check(lp._arguments_for_render('{"x": -9223372036854775808}', True)
      == {"x": -9223372036854775808}, "int64-Minimum muss Integer bleiben")
check(lp._arguments_for_render('{"x": -9223372036854775809}', True)
      == {"x": float(-9223372036854775809)}, "unter int64 wird zum Double")
check(lp._arguments_for_render('{"x": 18446744073709551615}', True)
      == {"x": 18446744073709551615}, "uint64-Maximum muss Integer bleiben")

print(f"GRUEN: {checks} Faelle" if not fail else f"ROT ({fail} von {checks})")
sys.exit(1 if fail else 0)
