# Vendored third-party code

These two files are copied verbatim from the vLLM project's tokenizer package.
Their upstream SPDX headers — `Apache-2.0`, `Copyright contributors to the vLLM
project` — are kept untouched and are authoritative.

| file | upstream repository | upstream path |
|---|---|---|
| `deepseek_v4_encoding.py` | `https://github.com/vllm-project/vllm` | `vllm/tokenizers/deepseek_v4_encoding.py` |
| `deepseek_v41_encoding.py` | `https://github.com/vllm-project/vllm` | `vllm/tokenizers/deepseek_v41_encoding.py` |

Provenance, checkable against vLLM's public history (git blob ids compared with the files at each upstream
commit that touched them):

- `deepseek_v4_encoding.py` is byte-identical to `vllm/tokenizers/deepseek_v4_encoding.py` at
  vllm-project/vllm commit `1356635d837c4ef002ec98c1a0296e7ff60be3c1` (2026-09-02, "[New model][Multimodal]
  Add DeepSeek-V4-Flash-Vision-Exp support (#54566)"; git blob `fb5bc44e854b90310d5220908c714ec5663b9939`), the
  version upstream until `cc09352e5c50` (2026-09-18).
- `deepseek_v41_encoding.py` was taken unmodified from a vLLM build of 2026-10. Its code is identical to
  `vllm/tokenizers/deepseek_v41_encoding.py` at vllm-project/vllm commit
  `b47b01cf3383d50a5fae8569cadcbd0736076234` (2026-09-10, "[Model][Frontend] Support DeepSeek-V4.1-Flash in
  Rust and Python frontends (#56208)"); only the module docstring differs (two lines, as the build shipped it).

Upstream `vllm/parser/deepseek_v41.py` declares the same DSML terminals.

Licence: Apache License 2.0. The repository's `LICENSE` carries the full text;
the SPDX identifiers in each file are the governing notice. No local
modification is intended: `tests/test_tooltext_dsml.py` pins each file's sha256
so an accidental edit is visible.
