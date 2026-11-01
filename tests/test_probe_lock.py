#!/usr/bin/env python3
"""Stream hardening, round 7: the probe treats CRLF frame ends as boundaries, and a failed probe
does not hold the client lock while _declare_gone waits for the backend connection.

1) CRLF: after a write ending in "\\r\\n\\r\\n" and a half-close, the watchdog probes.
2) CRLF mid-frame (ends in a single "\\r\\n"): no probe.
3) Failed probe: _declare_gone may wait up to ~10 s for get_conn(); during that time
   a write from the handler must not block on the client lock.
"""
import os
import socket
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _lade import lade  # noqa: E402

lp = lade()
fail = 0
checks = 0


def check(cond, msg):
    global fail, checks
    checks += 1
    if not cond:
        print("ROT:", msg)
        fail += 1


class H(lp.Handler):
    def __init__(self, wfile, conn):
        self.wfile, self.connection = wfile, conn

    def log_message(self, *a):
        pass


def neu():
    ca, cb = socket.socketpair()
    h = H(ca.makefile("wb", buffering=0), ca)
    h._client_lock = threading.Lock()
    h._client_wrote = threading.Event()
    h._client_at_frame_end = False
    h._client_streaming = True
    return h, ca, cb


def zu(h, ca, cb, stop):
    stop.set()
    for x in (h.wfile, ca, cb):
        try:
            x.close()
        except OSError:
            pass


def lies(cb, sekunden):
    cb.settimeout(0.2)
    out = b""
    ende = time.monotonic() + sekunden
    while time.monotonic() < ende:
        try:
            st = cb.recv(4096)
        except socket.timeout:
            continue
        except OSError:
            break
        if not st:
            break
        out += st
    return out


class Conn:
    sock = None

    def close(self):
        pass


# 1) CRLF frame end -> probe after a half-close.
h, ca, cb = neu()
gone, stop = threading.Event(), threading.Event()
h._client_write(b"data: x\r\n\r\n")
h._client_watchdog(lambda: Conn(), gone, stop)
cb.shutdown(socket.SHUT_WR)
got = lies(cb, 2.0)
check(got.startswith(b"data: x\r\n\r\n") and b":\n\n" in got[len(b"data: x\r\n\r\n"):],
      f"no probe after a CRLF frame end: {got!r}")
check(not gone.is_set(), "half-close after a CRLF frame counted as hang-up")
zu(h, ca, cb, stop)

# 2) CRLF mid-frame -> no probe.
h, ca, cb = neu()
gone, stop = threading.Event(), threading.Event()
h._client_write(b"data: x\r\n")
h._client_watchdog(lambda: Conn(), gone, stop)
cb.shutdown(socket.SHUT_WR)
got = lies(cb, 2.0)
check(got == b"data: x\r\n", f"probe inside a CRLF frame: {got!r}")
zu(h, ca, cb, stop)

class ZaehlWfile:
    """Delegates to the real wfile and counts probe writes that failed, so the test
    pins the probe-failure path (not the peek-RST path)."""

    def __init__(self, inner):
        self.inner, self.probe_failed = inner, 0

    def write(self, data):
        try:
            return self.inner.write(data)
        except (OSError, ValueError):
            if data == b":\n\n":
                self.probe_failed += 1
            raise

    def flush(self):
        return self.inner.flush()

    def close(self):
        return self.inner.close()


# 3) Failed probe: no lock held while _declare_gone waits for get_conn().
h, ca, cb = neu()
h.wfile = ZaehlWfile(h.wfile)
gone, stop = threading.Event(), threading.Event()
h._client_write(b"data: x\n\n")
h._client_watchdog(lambda: None, gone, stop)   # conn never appears -> ~10 s wait
# Drain first: closing with unread data makes the peek raise (RST), and that path
# declares the client gone outside the lock - we need the probe write to fail.
check(lies(cb, 0.3) == b"data: x\n\n", "frame not delivered")
cb.close()                                     # full close: the probe write fails
check(gone.wait(3.0), "failed probe did not declare the client gone")
check(h.wfile.probe_failed == 1,
      f"client declared gone, but not by a failed probe (probe_failed={h.wfile.probe_failed})")
dauer = {}


def schreibe():
    t = time.monotonic()
    try:
        h._client_write(b"data: y\n\n")
    except lp.ClientGone:
        pass
    except Exception as e:  # noqa: BLE001
        dauer["exc"] = e
    dauer["s"] = time.monotonic() - t


w = threading.Thread(target=schreibe, daemon=True)
w.start()
w.join(2.0)
check(not w.is_alive() and dauer.get("s", 99) < 1.0,
      f"handler write blocked on the client lock while _declare_gone waited "
      f"({dauer.get('s', '>2')} s)")
check("exc" not in dauer, f"unexpected exception: {dauer.get('exc')!r}")
zu(h, ca, cb, stop)
w.join(11)

print(f"GRUEN: {checks} Faelle" if not fail else f"ROT ({fail} von {checks})")
sys.exit(1 if fail else 0)
