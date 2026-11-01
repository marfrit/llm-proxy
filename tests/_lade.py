"""Gemeinsamer Lader: llm-proxy.py als Modul, Konfiguration in einem Wegwerfordner."""
import atexit
import importlib.util
import os
import shutil
import sys
import tempfile


def lade(pfad=None):
    pfad = pfad or (sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "llm-proxy.py"))
    ordner = tempfile.mkdtemp(prefix="llmproxy-test-")
    atexit.register(shutil.rmtree, ordner, ignore_errors=True)
    os.environ["LLM_PROXY_CONFIG_DIR"] = ordner
    spec = importlib.util.spec_from_file_location("llm_proxy", pfad)
    lp = importlib.util.module_from_spec(spec)
    lp_log = []
    spec.loader.exec_module(lp)
    lp.log = lambda msg: lp_log.append(msg)      # Protokoll mitschneiden statt drucken
    lp._testlog = lp_log
    return lp
