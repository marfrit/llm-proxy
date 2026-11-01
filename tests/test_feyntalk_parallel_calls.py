#!/usr/bin/env python3
"""Parallel tool calls: several tool calls in one assistant turn ride token mode.

ROT, wenn `_chat_messages_to_input` einen Assistant-Turn mit mehreren Calls
(oder Text neben Calls) ablehnt, `_responses_messages_from_input` die
AssistantRun-Merge-Regel nicht exakt nachbildet, oder der Prompt der
Chat-Route vom Prompt der Responses-Route derselben Historie abweicht.

Der geteilte Fixture tests/fixtures/parallel-tool-calls.json (Schema
feyntalk-w6g/1) traegt sechs Historien aus Responses-`items` und den Chat-
`messages`, in die sie rendern muessen.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _lade import lade  # noqa: E402
from _feyntalk_fixture import MODEL, make_material  # noqa: E402
from feyntalk_render.render import Renderer  # noqa: E402

lp = lade()
HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURE = os.path.join(HERE, "fixtures", "parallel-tool-calls.json")
TEMPLATE = open(os.path.join(HERE, "fixtures", "qwen3.6-35b-a3b-chat-template.jinja")).read()

fail = 0
checks = 0


def check(cond, msg):
    global fail, checks
    checks += 1
    if not cond:
        print("ROT:", msg)
        fail += 1


def normalize_items(items):
    """The engine only reads a reasoning item's `content`, and an empty
    reasoning item restarts the run without erasing anything, so it is
    equivalent whether the gateway emits it or not."""
    out = []
    for item in items:
        if item.get("type") == "reasoning":
            if not item.get("content"):
                continue
            item = {k: v for k, v in item.items() if k != "summary"}
        out.append(item)
    return out


fx = json.load(open(FIXTURE, encoding="utf-8"))
check(fx.get("schema") == "feyntalk-w6g/1", f"schema: {fx.get('schema')!r}")
check(len(fx.get("histories", {})) == 6, f"Historien: {len(fx.get('histories', {}))}")

material = make_material(template=TEMPLATE)
cap = {"renderer": Renderer(material), "hash": material["hash"],
       "material": material, "object_arguments": True}

for name, history in fx["histories"].items():
    items = history["items"]
    messages = history["messages"]
    # (b) Responses items -> the Chat messages chat_messages_json renders.
    check(lp._responses_messages_from_input(items) == messages,
          f"{name}: items->messages: {lp._responses_messages_from_input(items)!r}")
    # (a) Chat messages -> Responses items. Exact except for the reasoning
    # decoration (no `summary`) and the empty reasoning item, both of which the
    # engine maps identically (AssistantRun::add_reasoning).
    forward = lp._chat_messages_to_input(messages)
    check(normalize_items(forward) == normalize_items(items),
          f"{name}: messages->items: {forward!r}")
    check(lp._responses_messages_from_input(forward) == messages,
          f"{name}: round-trip: {lp._responses_messages_from_input(forward)!r}")
    # (c) both routes render the identical prompt for the same history.
    chat_body = {"model": MODEL, "stream": True, "messages": messages}
    responses_body = {"model": MODEL, "stream": True, "input": items}
    chat_token = lp._feyntalk_chat_request(json.dumps(chat_body).encode(), cap, MODEL, True)
    responses_token = lp._feyntalk_responses_request(json.dumps(responses_body).encode(), cap, MODEL)
    chat_hash = json.loads(chat_token["body"])["arcint"]["render"]["prompt_sha256"]
    responses_hash = json.loads(responses_token["body"])["arcint"]["render"]["prompt_sha256"]
    check(chat_hash == responses_hash, f"{name}: prompt hashes differ: {chat_hash} vs {responses_hash}")

# parallel-tool-calls blocker: chat_messages_json (src/exec/chat_json.h ~31) writes content =
# null for a turn that has tool_calls and empty content. A template that
# distinguishes none/string must therefore see null on BOTH routes. This
# history is not in the shared fixture (the engine's copy gets it next round).
CLASSIFY = ("{% for message in messages %}"
            "{% if message.role == 'assistant' %}"
            "[{{ 'null' if message.content is none else ('string' if message.content is string else 'other') }}]"
            "{% endif %}{% endfor %}")
classify = Renderer(make_material(template=CLASSIFY))
empty_messages = [
    {"role": "user", "content": "read both files"},
    {"role": "assistant", "content": "", "tool_calls": [
        {"id": "call_a", "type": "function",
         "function": {"name": "read_file", "arguments": "{\"path\":\"a.txt\"}"}},
    ]},
]
empty_items = [
    {"type": "message", "role": "user", "content": "read both files"},
    {"type": "message", "role": "assistant", "content": ""},
    {"type": "function_call", "call_id": "call_a", "name": "read_file",
     "arguments": "{\"path\":\"a.txt\"}"},
]
chat_items = lp._chat_messages_to_input(empty_messages)
check(not any(it.get("type") == "message" and it.get("role") == "assistant"
              for it in chat_items),
      f"leerer Content sendet ein assistant message item: {chat_items!r}")
chat_msgs = lp._render_tool_arguments(lp._responses_messages_from_input(chat_items), True)
resp_msgs = lp._render_tool_arguments(lp._responses_messages_from_input(empty_items), True)
check(classify.render(chat_msgs, tools=[]).text == "[null]",
      f"Chat-Route content ist nicht null: {classify.render(chat_msgs, tools=[]).text!r}")
check(classify.render(resp_msgs, tools=[]).text == "[null]",
      f"Responses-Route content ist nicht null: {classify.render(resp_msgs, tools=[]).text!r}")

print(f"GRUEN: {checks} Faelle" if not fail else f"ROT ({fail} von {checks})")
sys.exit(1 if fail else 0)
