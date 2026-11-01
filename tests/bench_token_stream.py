#!/usr/bin/env python3
"""Kostenmessung fuer den token-modalen Chat-Pfad des Gateways.

Replays a synthesized long token-mode SSE stream (real token ids from a real
material dump) through the gateway's own Chat path -- the function that consumes
the engine's ``arcint:token`` / ``arcint:span`` events and emits Chat
completions chunks, including feyntalk-render's ``TokenStreamNormalizer`` -- and
reports the processing cost per event against the stream position.

Usage:

    PYTHONPATH=... .venv/bin/python tests/bench_token_stream.py <material.json> [n_think] [n_answer] [chunk_events]

The material dump is never committed; the path is the only argument that names
it. One SSE event per ``read1`` is the default, because the engine flushes every
token and the gateway's 64 KiB read returns whatever is already buffered.

The number that matters: the engine's write timeout is 5 s while it streams
~150 events/s, so a single stall of > 5 s in this path makes the engine abort
("client gone"). A cost that grows with the stream position is exactly that
stall.
"""

import base64
import io
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _lade import lade  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))


def marker_ids(material):
    """The material's declared special ids (think opener/closer, tool tags)."""
    markers = set()
    special = material["special_tokens"]
    for family in ("think", "think_extended"):
        for pair in special.get(family) or []:
            markers.update(pair.get("open_ids") or [])
            markers.update(pair.get("close_ids") or [])
    tool = special["tool_call"]
    markers.update(tool.get("open_ids") or [])
    markers.update(tool.get("close_ids") or [])
    return markers


def _candidate_ids(material):
    """Non-marker ids whose piece decodes to visible text.

    Single printable ASCII bytes are preferred (small, regular chunks); if the
    material has too few, any non-empty well-formed piece will do, because the
    stream may repeat an id as often as it likes and the normaliser only cares
    about the token_index."""
    markers = marker_ids(material)
    single, other = [], []
    for token_id, raw in enumerate(material["decode"]["pieces_b64"]):
        if token_id in markers:
            continue
        try:
            text = base64.b64decode(raw).decode("utf-8")
        except Exception:
            continue
        if not text:
            continue
        if len(text) == 1 and (text.isalnum() or text == " "):
            single.append(token_id)
        else:
            other.append(token_id)
    out = single if len(single) >= 8 else other
    if len(out) < 2:
        raise SystemExit("material has too few usable ids")
    return out


def synthesize(material, n_think=1500, n_answer=2500, ids=None):
    """The engine's token and span events for one long thinking+answer turn."""
    ids = list(ids) if ids else _candidate_ids(material)
    think = material["special_tokens"]["think"][0]
    think_open_id = (think.get("open_ids") or [None])[0]
    think_close_id = (think.get("close_ids") or [None])[0]
    if think_open_id is None or think_close_id is None:
        raise SystemExit("material declares no thinking open/close id")
    events = [{"type": "response.created",
               "response": {"id": "resp_bench", "created_at": 1700000000,
                            "status": "in_progress"}}]
    index = 0
    events.append({"type": "arcint:span", "v": 1, "span": "thinking",
                   "phase": "start", "token_index": index, "detail": {"preopened": True}})
    events.append({"type": "arcint:token", "v": 1, "token_index": index,
                   "token_id": think_open_id, "field": "reasoning"})
    index += 1
    for i in range(n_think - 1):
        events.append({"type": "arcint:token", "v": 1, "token_index": index,
                       "token_id": ids[i % len(ids)], "field": "reasoning"})
        index += 1
    events.append({"type": "arcint:token", "v": 1, "token_index": index,
                   "token_id": think_close_id, "field": "reasoning"})
    events.append({"type": "arcint:span", "v": 1, "span": "thinking",
                   "phase": "end", "token_index": index, "detail": {"closed": True}})
    index += 1
    for i in range(n_answer):
        events.append({"type": "arcint:token", "v": 1, "token_index": index,
                       "token_id": ids[(i + 3) % len(ids)], "field": "text"})
        index += 1
    events.append({"type": "response.completed",
                   "response": {"id": "resp_bench", "created_at": 1700000000,
                                "status": "completed",
                                "output": [{"type": "message", "role": "assistant",
                                            "content": [{"type": "output_text",
                                                         "text": "answer"}]}],
                                "usage": {"input_tokens": 11, "output_tokens": index,
                                          "total_tokens": 11 + index}}})
    return events, index


def sse(event):
    return ("data: " + json.dumps(event, ensure_ascii=False) + "\n\n").encode("utf-8")


class FakeResp:
    """A backend response whose read1() returns pre-built chunks.

    It records, for every chunk, the wall time between returning that chunk and
    the next read1() call. That interval is exactly the time the gateway spends
    processing the chunk (normalise + emit Chat frames), because the caller does
    nothing else between the two reads."""

    def __init__(self, chunks):
        self.chunks = chunks
        self.index = 0
        self.status = 200
        self._returned_at = None
        self.costs = []          # (chunk_index, seconds) for non-empty chunks

    def read1(self, n):
        if self._returned_at is not None:
            self.costs.append((self.index - 1, time.perf_counter() - self._returned_at))
        if self.index >= len(self.chunks):
            self._returned_at = time.perf_counter()
            return b""
        chunk = self.chunks[self.index]
        self.index += 1
        self._returned_at = time.perf_counter()
        return chunk

    def finalize(self):
        if self._returned_at is not None:
            self.costs.append((self.index - 1, time.perf_counter() - self._returned_at))
            self._returned_at = None


def bench_handler(lp):
    class BenchHandler(lp.Handler):
        def __init__(self, wfile):
            self.wfile = wfile
            self.connection = None
            self.headers = {"get": lambda k, d=None: d,
                            "Content-Type": "application/json"}

        def send_response(self, *a, **k):
            pass

        def send_header(self, *a, **k):
            pass

        def end_headers(self):
            pass

    return BenchHandler


def bucket_report(costs, width):
    """(bucket_start, events, seconds, us_per_event) per window of `width` events."""
    rows = []
    for start in range(0, len(costs), width):
        window = costs[start:start + width]
        if not window:
            continue
        seconds = sum(c for _, c in window)
        rows.append((start, len(window), seconds, seconds / len(window) * 1e6))
    return rows


def main(argv):
    if len(argv) < 2:
        raise SystemExit(__doc__)
    material_path = argv[1]
    n_think = int(argv[2]) if len(argv) > 2 else 1500
    n_answer = int(argv[3]) if len(argv) > 3 else 2500
    chunk_events = int(argv[4]) if len(argv) > 4 else 1

    with open(material_path, "r", encoding="utf-8") as handle:
        material = json.load(handle)

    events, n_tokens = synthesize(material, n_think, n_answer)
    raw_chunks = [sse(e) for e in events]
    if chunk_events > 1:
        merged = []
        for i in range(0, len(raw_chunks), chunk_events):
            merged.append(b"".join(raw_chunks[i:i + chunk_events]))
        raw_chunks = merged
    # One extra read that returns b"" to close the stream.
    resp = FakeResp(raw_chunks + [b""])

    lp = lade(os.path.join(HERE, "..", "llm-proxy.py"))
    # Der Leserfaden darf dem Schreibpfad nicht davonlaufen: sonst misst
    # FakeResp.read1 den Leser-Schleifenaufwand statt der Verarbeitung. Die
    # kleine Schranke erzwingt genau eine Verarbeitung je read1-Intervall.
    lp.STREAM_QUEUE_MAX_BYTES = 8192
    request = {"material": material, "tools": None,
               "model": material["model"]["served_id"], "stream": True}
    handler = bench_handler(lp)(io.BytesIO())
    t0 = time.perf_counter()
    handler._stream_chat_from_tokens(resp, "bench", t0, t0, " bench", "POST",
                                     "/v1/chat/completions", request, sock=None)
    resp.finalize()
    total = time.perf_counter() - t0

    # Drop the synthetic empty-chunk entry, keep one row per event-bearing chunk.
    costs = [(i, c) for i, c in resp.costs if c > 0]
    per_event = [(i, c / chunk_events) for i, c in costs]

    print(f"material={os.path.basename(material_path)} tokens={n_tokens} "
          f"events={len(events)} chunks={len(raw_chunks)} chunk_events={chunk_events}")
    print(f"total={total:.3f}s  per_event={total / len(events) * 1e6:.1f}us  "
          f"engine_budget={1e3 / 150 * 1000:.1f}us/event (150 ev/s)")
    print("per 250 events:  start   n   ms      us/event")
    for start, count, seconds, us in bucket_report(per_event, 250):
        print(f"                 {start:6d} {count:4d} {seconds * 1e3:8.3f} {us:8.1f}")

    # Erstes Stueck = Normalizer-Aufbau, letztes = finish/Schlussrahmen:
    # beide gehoeren nicht zur Kostenkurve je Ereignis.
    curve = per_event[1:-1] or per_event
    quarters = [curve[i:i + len(curve) // 4]
                for i in range(0, len(curve), len(curve) // 4)][:4]
    if len(quarters) == 4:
        first = sum(c for _, c in quarters[0]) / len(quarters[0])
        last = sum(c for _, c in quarters[3]) / len(quarters[3])
        print(f"first_quarter={first * 1e6:.1f}us/event  "
              f"last_quarter={last * 1e6:.1f}us/event  ratio={last / first:.2f}x")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
