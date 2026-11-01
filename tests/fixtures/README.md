# Fixture data

## `qwen3.6-35b-a3b-chat-template.jinja`

`chat_template.jinja` from the Hugging Face repository
[Qwen/Qwen3.6-35B-A3B](https://huggingface.co/Qwen/Qwen3.6-35B-A3B) at revision
`995ad96eacd98c81ed38be0c5b274b04031597b0`, unchanged: git blob
`a8755d827c0a7b614c246c4060dfd58ab352a8ff` (7,764 bytes), the blob id the repository's tree lists for that file.
Licensed Apache-2.0 by the Qwen team. It is data for the render tests, not test code: it iterates
`tool_call.arguments|items` and serialises tools with `tojson`, the two constructs the gateway's render input
must mirror.

Provenance lives here rather than in the file: the tests read the template into a material whose canonical
hash covers `chat_template.text`, so a header comment would change the fixture's bytes and the pinned hashes.

## `parallel-tool-calls.json`

The shared parallel-tool-calls contract (`schema: feyntalk-w6g/1`), copied verbatim from the
engine's `tests/feyntalk/fixtures/parallel-tool-calls.json`. It holds six
histories, each with the Responses `items` and the Chat `messages` they must
render to: a run of `[reasoning]? [assistant message]? [function_call]+` with no
boundary item in between is ONE assistant turn (several calls per turn, text
then calls, reasoning before the turn, an empty reasoning item that restarts the
run without erasing). `tests/test_feyntalk_parallel_calls.py` pins the schema,
both mapping directions, the round-trip and the equal prompt of the Chat and
Responses routes for every history.

## `nlohmann_arguments_cases.jsonl` / `nlohmann_arguments_expected.txt`

A measured oracle for the gateway's tool-call argument mapping. One JSON
document per case line; the expected file has one line per case.

Provenance: a probe was built against the engine's own vendored
`nlohmann::json` and `minja` — the same headers the engine renders with. For
every input line it ran
`nlohmann::json::parse(line, nullptr, /*allow_exceptions=*/false)` (exactly what
the engine's `tool_call_arguments_for_template` does) and then rendered
`{{ v | tojson }}` through minja, whose `Value` is an ordered_json filled from
the parsed, `std::map`-sorted object. `DISCARDED` means nlohmann refused the
document; the engine then keeps the raw arguments string. The expected lines are
verbatim engine output.

The cases cover object key order, the int64/uint64/double number model, floats,
the via `format_buffer` bands and their overflow/underflow edges, Unicode,
control characters, U+2028/U+2029, NUL, duplicate keys (last wins) and nesting.
Three probe sets were concatenated: the base set, the float-format set and the
edge set (all probe output, expected lines verbatim). Read the files with
`.split("\n")`, not `splitlines()`: the expected lines carry raw U+2028/U+2029,
which `splitlines()` would split on.
