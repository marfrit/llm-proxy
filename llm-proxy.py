#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Markus Fritsche
# SPDX-License-Identifier: Apache-2.0
"""LLM router: model-prefix → OpenRouter (cloud) or local model-aware fanout.

Local backends: model-name routed when possible, first-up failover otherwise.
  - On POST with a known model id, the request goes to the backend whose
    /v1/models advertises it (cached 30s).
  - On unknown model or empty body, falls back to first-up wins,
    preserving legacy semantics.

Cloud routing: HTTPS to openrouter.ai, server-side Bearer auth from OPENROUTER_API_KEY.

/v1/models: merged cloud catalog (live-filtered against OpenRouter's
/api/v1/models) + union of every reachable local backend's model list.
Entries are visually tagged in their id:
  - "[$] vendor/model"     paid cloud (auto-discovered, see d)
  - "[free] vendor/model"  free cloud (auto-detected by :free suffix)
  - "[local] alias"        local backend

A periodic background thread (every 3 h) re-fetches the full OpenRouter model
list and:
  a) adds newly free models >7 B parameters to the curated free catalog
  b) removes models that are no longer free
  c) probes each free model with a tiny 1-token request; if it returns 429 the
     model id gets a "-na" suffix (not available).
  d) auto-discovers cheap paid models (prompt < $0.50/MTok) from known
     prefixes and adds them to the paid catalog.

The paid catalog is derived like the free one: each successful check rebuilds it
from what OpenRouter currently lists below the price threshold, so a model that
got pricier or disappeared drops out. (Until 1.0.2 entries were never removed,
to protect hand-added ones; there are none, and the rule kept models that had
become expensive.) A failed fetch skips the cycle and leaves the catalog as is.

Inbound requests have the tag + optional "-na" suffix stripped before routing,
so OWUI sending back the displayed id Just Works.

Config:
  /etc/llm-proxy/local-backends.json  — list of {name, host, port} dicts
  /etc/llm-proxy/cloud-catalog.json   — persisted cloud model catalog + status

Signals:
  SIGHUP   reload local-backends.json
  SIGUSR1  drop discover caches + trigger an immediate background re-check
  POST /admin/recheck  same as SIGUSR1, via HTTP (202 = scheduled; async -
                       poll /v1/models afterwards). For an external tool after a unit switch.
"""
import http.server
import http.client
import ssl
import select
import socket
import collections
import sys
import os
import os.path
import time
import json
import threading
from concurrent.futures import ThreadPoolExecutor
import signal
import re
import uuid
import copy
import traceback
import urllib.parse
import hashlib
import math

# Die eigene Version; geht in die Startzeile und in die Release-Notiz.
__version__ = "1.3.4"

# feyntalk: the optional renderer dependency. The proxy runs without it;
# token mode is then unavailable and says so once (see _feyntalk_unavailable).
try:
    import feyntalk_render
    from feyntalk_render.material import MaterialError as _FTMaterialError, from_dict as _ft_from_dict
    from feyntalk_render.render import RenderError as _FTRenderError, Renderer as _FTRenderer
    from feyntalk_render.render import sha256 as _ft_sha256
    from feyntalk_render.spans import SpanError as _FTSpanError, TokenStreamNormalizer as _FTNormalizer
    from feyntalk_render.parser import schemas_from_tools as _ft_schemas_from_tools
    FEYNTALK_AVAILABLE = True
    FEYNTALK_IMPORT_ERROR = None
except Exception as _ft_e:  # noqa: BLE001 - any import failure means unavailable
    feyntalk_render = None
    FEYNTALK_AVAILABLE = False
    FEYNTALK_IMPORT_ERROR = repr(_ft_e)

# feyntalk: per-backend opt-in. "off" (the default) is byte-identical to
# the pre-feyntalk proxy; "tokens" asks the backend and falls back to text when it
# cannot serve token mode; "text" pins text mode without asking.
FEYNTALK_SETTINGS = {"off": "off", "tokens": "tokens", "text": "text"}
BACKEND_FEYNTALK = {}
FEYNTALK_CACHE = {}
FEYNTALK_CACHE_LOCK = threading.Lock()
# Ein Merkzettel, kein Besitz: jeder Faehigkeits-Eintrag haelt seine eigenen
# material-/renderer-Objekte, und der TTL-Pfad konsultiert den Merkzettel nie.
# Deshalb braucht er keine Referenzzaehlung; ein Backend, das denselben Hash
# teilt, baut den Renderer nur einmal.
FEYNTALK_MATERIALS = {}          # hash -> {material, renderer}
_FEYNTALK_WARM = {}              # hash -> {evt, ok}: ein Bau je Hash gleichzeitig
_FEYNTALK_WARM_RUN = {"active": False, "pending": False}   # hoechstens ein Lauf
_FEYNTALK_WARM_RUN_LOCK = threading.Lock()
FEYNTALK_TTL = 30.0              # seconds between /props checks per backend
_FEYNTALK_WARNED = set()         # one log line per distinct reason/Capability
# preserve_thinking: eine Assistant-Runde behaelt ihr Denken beim Replay
# (eine neue User-Nachricht, etwa ein Steering-Zug, rendert es sonst weg).
# Stufe-1-Kompaktierung: erst wenn der behaltene Render sich dem Kontextlimit
# naehert, wird ohne Denken gerendert. Anteil von n_ctx und Zeichen je Token
# fuer die Groessenschaetzung (der /props-Probe fehlt eine Tokenzahl).
FEYNTALK_THINK_DROP_FRACTION = float(os.environ.get("FEYNTALK_THINK_DROP_FRACTION", "0.7"))
FEYNTALK_CHARS_PER_TOKEN = float(os.environ.get("FEYNTALK_CHARS_PER_TOKEN", "3.0"))
# Ab welchem Anteil der Drop-Schwelle der exakte Tokenzaehler der Engine
# befragt wird (POST /tokenize, siehe _feyntalk_exact_tokens). chars/3.0
# ueberschaetzte im gemessenen Fall (2026-11-01, Qwen3.8 Flash-Next, 131072 ctx)
# um ~1,5x: ein Render mit 0,8 * Schwelle kann real noch ueber der Schwelle
# liegen. Unterhalb von 0,8 ist selbst diese Ueberschaetzung so weit von der
# Schwelle entfernt, dass keine Entscheidung kippen kann -- der zusaetzliche
# Aufruf (eine Runde) waere dort nur Last ohne Ergebnis.
FEYNTALK_TOKENIZE_PRE_FRACTION = float(os.environ.get("FEYNTALK_TOKENIZE_PRE_FRACTION", "0.8"))
FEYNTALK_TOKENIZE_TIMEOUT = 3.0   # ein Zaehler, kein Turn; bei Frist gilt der Rueckfall unten
# Je Backend das zuletzt beobachtete Verhaeltnis exact/Schaetzung (ein float).
# Faellt /tokenize einmal aus, wird die Schaetzung damit skaliert, statt roh
# ueber die Schwelle zu laufen: eine behaltene Entscheidung kann so nicht an
# einem einzelnen Fehlschlag umkippen (genau den Re-Prefill, den diese Fassung
# vermeiden soll). Ein Wert je Backend, also keine Historie und kein Wachstum.
_FEYNTALK_TOKEN_RATIO = {}
# Handshake: Token-Modus nur mit kompatibler Kette. Der Renderer ist eine
# Python-Abhaengigkeit (Paketversion), die Engine meldet sich in /props.
FEYNTALK_MIN_RENDERER_VERSION = "1.0.0"
FEYNTALK_MIN_ENGINE_VERSION = "1.4.0"
FEYNTALK_WARM_TIMEOUT = 60.0      # ein Request wartet so lange auf einen laufenden Warmbau
FEYNTALK_CONTRACT_V = 1
RESPONSES_PATHS = ("/v1/responses", "/responses")

CONFIG_DIR          = os.environ.get("LLM_PROXY_CONFIG_DIR", "/etc/llm-proxy")
LOCAL_BACKENDS_FILE = os.path.join(CONFIG_DIR, "local-backends.json")
CLOUD_CATALOG_FILE  = os.path.join(CONFIG_DIR, "cloud-catalog.json")

# Fallback only, when local-backends.json is absent. Real backends belong in
# $LLM_PROXY_CONFIG_DIR/local-backends.json (see local-backends.example.json).
DEFAULT_LOCAL_BACKENDS = [
    ("local", "127.0.0.1", 8080),
]
# Optional display aliases {"<model-id>": "<label>"} from
# $LLM_PROXY_CONFIG_DIR/friendly-names.json (see friendly-names.example.json).
def _load_friendly_names():
    try:
        with open(os.path.join(CONFIG_DIR, "friendly-names.json")) as f:
            return json.load(f)
    except Exception:
        return {}
FRIENDLY_NAMES = _load_friendly_names()


# Does a local model think? Ask its backend first; the config file only corrects.
#
# It used to be the other way round: reasoning-models.json held substrings and a
# match was the ONLY way a local model could be marked as reasoning. That file said
# ["dspark"] while the models had been renamed to `deepseek-v4-flash` /
# `deepseek-v4-pro`, so the match silently stopped hitting -- no error, the flag just
# went away. Clients then saw `reasoning: null` and the proxy stopped sending
# `enable_thinking`. A capability keyed on a name breaks the day the name changes,
# and it breaks quietly. Hence: metadata decides, the file corrects.
#
# Two backend families advertise it differently, both are real metadata:
#   - vLLM-style /v1/models -> `supported_parameters`. OpenRouter spells the entry
#     `reasoning`, a backend spells it `reasoning_effort`; accept either. The field being
#     present at all makes the answer definitive, including a definitive False.
#   - llama.cpp /props -> `chat_template_caps.supports_preserve_reasoning`, which
#     llama.cpp derives from the chat template itself.
REASONING_PARAM_NAMES = ("reasoning", "reasoning_effort")

def _load_effort_overrides():
    """$LLM_PROXY_CONFIG_DIR/reasoning-effort-map.json -- Vokabelkorrekturen.

    Form: {"substr": {"vom_client": "ans_backend"}}. Ein Wert None bedeutet
    "Feld ganz weglassen". Fehlt die Datei, wird nichts uebersetzt -- die
    Abwesenheit einer Korrektur darf nie eine Anfrage veraendern.
    """
    try:
        with open(os.path.join(CONFIG_DIR, "reasoning-effort-map.json")) as f:
            data = json.load(f)
    except FileNotFoundError:
        return []
    except Exception as e:
        print("reasoning-effort-map.json unlesbar: %s" % e)
        return []
    out = []
    for substr, mapping in (data.items() if isinstance(data, dict) else []):
        if isinstance(substr, str) and isinstance(mapping, dict):
            out.append((substr, mapping))
    return out


EFFORT_OVERRIDES = _load_effort_overrides()


def _map_reasoning_effort(model, value):
    """Gibt (geaendert?, neuer_wert) zurueck. Unbekannt -> unveraendert."""
    for substr, mapping in EFFORT_OVERRIDES:
        if substr in str(model) and str(value) in mapping:
            return True, mapping[str(value)]
    return False, value



def _reasoning_from_meta(meta):
    """True/False from what the backend advertises, None if it says nothing.

    The two signals do NOT carry the same weight, and conflating them would
    reintroduce the bug in a new costume:

    * `supported_parameters` is a REQUEST SCHEMA, not a capability declaration.
      Finding a reasoning parameter in it proves the model reasons. NOT finding
      one proves nothing -- a model whose thinking is switched via
      `chat_template_kwargs.enable_thinking` (exactly the path this proxy uses)
      never lists a top-level parameter for it. So: hit -> True, miss -> unknown.
    * `chat_template_caps.supports_preserve_reasoning` IS a capability boolean,
      derived by llama.cpp from the chat template. It is definitive both ways.
    """
    if not isinstance(meta, dict):
        return None
    sp = meta.get("supported_parameters")
    if isinstance(sp, (list, tuple)) and any(str(p) in REASONING_PARAM_NAMES for p in sp):
        return True
    caps = meta.get("chat_template_caps")
    if isinstance(caps, dict) and "supports_preserve_reasoning" in caps:
        return bool(caps["supports_preserve_reasoning"])
    return None


def _load_reasoning_overrides():
    """$LLM_PROXY_CONFIG_DIR/reasoning-models.json -- corrections, not the source.

    Two accepted forms:
      ["substr", ...]               legacy: each substring forces True
      {"substr": true|false, ...}   explicit, and able to say False, because
                                    metadata can be wrong in both directions
    Returns a list of (substring, bool) pairs; absent/unreadable = no corrections.
    """
    try:
        with open(os.path.join(CONFIG_DIR, "reasoning-models.json")) as f:
            data = json.load(f)
    except Exception:
        return []
    if isinstance(data, list):
        return [(s, True) for s in data if isinstance(s, str)]
    if isinstance(data, dict):
        return [(k, bool(v)) for k, v in data.items() if isinstance(k, str)]
    return []
REASONING_OVERRIDES = _load_reasoning_overrides()


def _load_local_ctx_overrides():
    """$LLM_PROXY_CONFIG_DIR/local-ctx.json -- Kontextfenster fuer lokale Backends,
    die in /v1/models keines melden (OpenArc etwa liefert ein nacktes Modellobjekt:
    nur id/object/created/owned_by). Korrektur, nicht Quelle: ein vom Backend
    gemeldeter Wert gewinnt immer, der Eintrag hier greift nur bei Schweigen."""
    try:
        with open(os.path.join(CONFIG_DIR, "local-ctx.json")) as f:
            d = json.load(f)
        return {k: int(v) for k, v in d.items()
                if isinstance(v, (int, float)) and v > 0}
    except FileNotFoundError:
        return {}
    except Exception as e:
        print("local-ctx.json unlesbar: %s" % e)
        return {}


LOCAL_CTX_OVERRIDES = _load_local_ctx_overrides()


def _load_local_max_output():
    """$LLM_PROXY_CONFIG_DIR/local-max-output.json -- max Output-Token fuer lokale
    Backends, die keines melden (vLLM und llama.cpp melden es nicht). Korrektur,
    nicht Quelle: ein vom Backend gemeldeter Wert gewinnt immer, der Eintrag hier
    greift nur bei Schweigen. Ohne das Feld nehmen die Klienten ihren
    Vorgabewert (16384) an - zu klein fuer das hauptlokale Denkmodell.
    bool ist in Python ein int und wird deshalb ausdruecklich verworfen.
    """
    try:
        with open(os.path.join(CONFIG_DIR, "local-max-output.json")) as f:
            d = json.load(f)
        return {k: int(v) for k, v in d.items()
                if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0}
    except FileNotFoundError:
        return {}
    except Exception as e:
        print("local-max-output.json unlesbar: %s" % e)
        return {}


LOCAL_MAX_OUTPUT = _load_local_max_output()


# --- Modell-Aliase mit optionaler Laengengrenze (model-aliases.json) ---------------------------
#
# Weitere Namen fuer ein lokales Modell, auf Wunsch mit eigener Obergrenze fuer den Prompt:
#   {"ds41-flash-light": {"model": "ds41-flash", "max_prompt_tokens": 262144},
#    "DeepSeek-v4.1-Flash-EXL3": {"model": "ds41-flash"}}
# Warum im Proxy (2026-10-21): vLLM kennt nur EIN max_model_len fuer alle Plaetze. Ein
# asymmetrisches Paar - ein 1M-Agent und ein 256k-Subagent auf demselben Backend - laesst sich nur
# hier erzwingen, nie im Client. Ein Alias geht an dasselbe Backend wie sein Ziel (gleiche Plaetze,
# gleiche Admission); ohne max_prompt_tokens ist er ein reiner Zweitname (Uebergang alter Namen).
# Gezaehlt wird mit dem Tokenizer des Modells: POST /tokenize am Backend (vLLM; gleiche messages,
# tools und chat_template_kwargs, also inklusive Chat-Template). Antwortet das nicht, gilt eine
# bewusst vorsichtige Schaetzung: Zeichen des JSON von messages+tools / CAP_CHARS_PER_TOKEN (2,0;
# auf pi-Agentenverlaeufen sind es gemessen 3,1-3,2 Zeichen je Token) - sie weist im Zweifel ab und
# laesst nie mehr durch als erlaubt.
CAP_CHARS_PER_TOKEN = 2.0


def _load_model_aliases():
    try:
        with open(os.path.join(CONFIG_DIR, "model-aliases.json")) as f:
            d = json.load(f)
        out = {}
        for alias, v in d.items():
            if not (isinstance(v, dict) and isinstance(v.get("model"), str)) or alias == v["model"]:
                continue
            ft = v.get("feyntalk")
            if ft is not None and ft not in FEYNTALK_SETTINGS:
                # Konfigurationsfehler, kein stiller Durchlauf: unbekannter Wert.
                print(f"model-aliases.json: unbekanntes feyntalk {ft!r} fuer {alias!r}; "
                      "Alias uebersprungen")
                continue
            mx = v.get("max_prompt_tokens")
            out[alias] = {"model": v["model"],
                          "feyntalk": ft,
                          "max_prompt_tokens": int(mx) if isinstance(mx, (int, float)) and mx > 0 else None}
        return out
    except FileNotFoundError:
        return {}
    except Exception as e:
        print("model-aliases.json unlesbar: %s" % e)
        return {}


MODEL_ALIASES = _load_model_aliases()


def _apply_model_alias(raw_model, model):
    """(raw_model, model) des Auftrags -> (raw_model, model, alias): ein Alias wird auf sein Ziel
    umgeschrieben (das Praefix bleibt); alias = {"alias", "model", "max_prompt_tokens"} oder None."""
    c = MODEL_ALIASES.get(model)
    if c is None:
        return raw_model, model, None
    try:  # a name a backend serves itself is never rewritten (old name still live during a rename)
        if find_backend_for_model(model) is not None:
            return raw_model, model, None
    except Exception:
        pass
    target = c["model"]
    raw_model = raw_model[: len(raw_model) - len(model)] + target if raw_model.endswith(model) else target
    return raw_model, target, {"alias": model, "model": target,
                              "max_prompt_tokens": c.get("max_prompt_tokens"),
                              "feyntalk": c.get("feyntalk")}


def _estimate_prompt_tokens(j):
    blob = json.dumps({"messages": j.get("messages"), "tools": j.get("tools")}, ensure_ascii=False)
    return int(len(blob) / CAP_CHARS_PER_TOKEN) + 1


def _count_prompt_tokens(backend, j):
    """(tokens, method): exakt ueber /tokenize am Backend, sonst die vorsichtige Schaetzung."""
    body = {"model": j.get("model"), "messages": j.get("messages") or []}
    if j.get("tools"):
        body["tools"] = j["tools"]
    if isinstance(j.get("chat_template_kwargs"), dict):
        body["chat_template_kwargs"] = j["chat_template_kwargs"]
    if backend is not None:
        try:
            conn = http.client.HTTPConnection(backend[1], backend[2], timeout=120)
            data = json.dumps(body).encode()
            conn.request("POST", "/tokenize", data, {"Content-Type": "application/json",
                                                     "Content-Length": str(len(data))})
            r = conn.getresponse()
            raw = r.read()
            conn.close()
            if r.status == 200:
                n = json.loads(raw).get("count")
                if isinstance(n, int):
                    return n, "tokenize"
        except Exception:
            pass
    return _estimate_prompt_tokens(j), "estimate"


def _cap_violation(alias, n, how):
    """None wenn erlaubt, sonst der Fehlerkoerper (400)."""
    mx = alias.get("max_prompt_tokens")
    if mx is None or n <= mx:
        return None
    return {"error": {
        "type": "context_length_exceeded",
        "code": "prompt_too_long_for_alias",
        "message": (f"'{alias['alias']}' nimmt hoechstens {mx} Prompt-Token; dieser Auftrag hat {n} "
                    f"({'Tokenizer des Modells' if how == 'tokenize' else 'vorsichtige Schaetzung'}). "
                    f"Fuer laengere Kontexte '{alias['model']}' verwenden."),
        "prompt_tokens": n, "max_prompt_tokens": mx, "counted_by": how}}


def _alias_catalog_entries(local_entries):
    """Fuer jeden Alias, dessen Ziel im lokalen Katalog steht, ein eigener Eintrag (gleiches
    Backend, ctx = Obergrenze falls gesetzt); ein totes Ziel vererbt seinen DOWN_SUFFIX."""
    out = []
    by_id = {e.get("id"): e for e in local_entries}
    for alias, c in MODEL_ALIASES.items():
        for down in ("", DOWN_SUFFIX):
            src = by_id.get(TAG_LOCAL + c["model"] + down)
            if src is None or TAG_LOCAL + alias + down in by_id:
                continue
            e = copy.deepcopy(src)
            e["id"] = TAG_LOCAL + alias + down
            # Welcher Eintrag das Ziel ist (pi listet anhand von alias_of nur
            # die -ft-Variante) und der Modus der Kette.
            e["alias_of"] = src.get("id")
            ft = c.get("feyntalk")
            if ft is not None:
                e["feyntalk"] = ft
            if ft == "tokens":
                basis = e.get("name") if isinstance(e.get("name"), str) and e.get("name") else c["model"]
                e["name"] = f"{basis} -ft"
            mx = c["max_prompt_tokens"]
            if mx is not None:
                # EVERY context field the entry carries gets the cap, not only `ctx`: an agent host's
                # models-json read max_model_len (vLLM's own field), so a light alias that passed the
                # backend's 1048576 on let sessions grow into the 400 instead of compacting (1.0.7).
                def _cap(v):
                    return min(v, mx) if isinstance(v, int) and v > 0 else mx
                e["ctx"] = _cap(e.get("ctx"))
                for k in _CTX_KEYS:
                    if k in e:
                        e[k] = _cap(e[k])
                meta = e.get("meta")
                if isinstance(meta, dict):
                    for k in ("n_ctx", "n_ctx_train"):
                        if k in meta:
                            meta[k] = _cap(meta[k])
                for p in e.get("properties") or []:
                    if p.get("property_id") == "context_window":
                        p["value"] = _cap(p.get("value"))
            out.append(e)
    return out


def _reasoning_override(model):
    for sub, val in REASONING_OVERRIDES:
        if sub in model:
            return val
    return None


def _local_reasoning(model, meta=None):
    """True / False / None for a local model. Metadata first, file as corrective.

    Precedence, spelled out because it gets re-litigated: a configured override
    WINS, including over metadata that disagrees. That is the point of it -- the
    brief is "metadata first, overridden locally if the metadata is missing OR
    WRONG", and only a human can judge "wrong". The disagreement is logged so the
    claim stays auditable instead of silently diverging.

    `meta` may be passed by callers that just fetched it, to save a second lookup.
    """
    if not model:
        return None
    ov = _reasoning_override(model)
    meta_val = _reasoning_from_meta(local_meta(model) if meta is None else meta)
    if meta_val is None:
        return ov
    if ov is not None and ov != meta_val:
        # Say it out loud: overriding the backend is a claim that it is lying.
        log(f"reasoning: override {ov} beats backend metadata {meta_val} for '{model}'")
        return ov
    return meta_val


# Woertliche ChatML-Marker, die lokale Modelle im TEXT erzeugen koennen und die
# eine Agentenkette mittendrin abbrechen lassen. Siehe die Begruendung an der
# Einfuegestelle in _proxy(). Als Text nie legitimer Inhalt.
CHATML_STOPS = ["<|im_end|>", "<|im_start|>"]


def _is_local_model(model):
    """True, wenn das Modell von einem der lokalen Backends bedient wird.

    Nutzt bewusst find_backend_for_model() statt eines eigenen Vergleichs: die
    Eintraege in local-backends.json tragen BACKEND-Namen ("backend-a"), die
    ausgelieferten Modell-IDs heissen anders ("deepseek-v4-flash-dspark"). Ein
    Vergleich gegen b[0] geht deshalb immer ins Leere - erste Fassung dieses
    Patches hatte genau den Fehler.
    """
    if not model:
        return False
    try:
        return find_backend_for_model(model) is not None
    except Exception:
        return False


CONNECT_TIMEOUT      = 3
READ_TIMEOUT         = 2500  # 2026-08-20: > Coder-Budget 2400; 1800 riss lange Generierungen (502 bei laufendem Backend)
DISCOVERY_TTL        = 30
CLOUD_DISCOVERY_TTL  = 600
CHECK_INTERVAL       = 10800
PROBE_TIMEOUT        = 15
# Wie lange ein Anrufer nichts abholen darf, waehrend sein Puffer voll ist,
# bevor wir ihn fuer weg halten. Deckt den Fall ab, dass er den Socket offen
# laesst statt ihn zu schliessen (pi-Agent beim Benutzer-Stopp).
CLIENT_WRITE_TIMEOUT = 120
# Obergrenze fuer den rohen SSE-Strom, den der Leserfaden vor dem
# Schreibpfad puffern darf. In BYTES, damit die Speicherobergrenze nicht an
# der Rahmen Groesse haengt; am Limit staut der Leser zurueck (Backpressure) und
# protokolliert einmal - ein Token wird nie still verworfen.
STREAM_QUEUE_MAX_BYTES = 16 * 1024 * 1024
NA_SUFFIX            = "-na"
DOWN_SUFFIX          = "-down"
LAST_KNOWN_FILE      = os.path.join(CONFIG_DIR, "last-known-models.json")

OPENROUTER_HOST = "openrouter.ai"
OPENROUTER_PORT = 443
CLOUD_PREFIXES = (
    "moonshotai/", "poolside/", "z-ai/", "cognitivecomputations/", "liquid/",
    "openrouter/", "anthropic/", "openai/", "google/", "deepseek/", "meta-llama/",
    "mistralai/", "qwen/", "x-ai/", "nvidia/", "cohere/", "perplexity/",
    "amazon/", "microsoft/", "nousresearch/",
)

PAID_DISCOVER_THRESHOLD = 0.50  # max prompt $/MTok for auto-discover

TAG_PAID  = "[$] "
TAG_FREE  = "[free] "
TAG_LOCAL = "[local] "

# Felder, die dieser Proxy selbst setzt und die kein Backend ueberschreiben darf.
PROXY_EIGENE_FELDER = frozenset(("id", "object", "owned_by", "cost_class", "reachable"))

# Modelle, die ueber OpenCode Zen laufen. Sie stehen in KEINEM der beiden
# OpenRouter-Kataloge, deshalb muessen sie hier stehen - und zwar an EINER
# Stelle, aus der sich sowohl die Modellliste als auch der Router bedienen.
#
# Vorher hing big-pickle nur fest verdrahtet in der Antwort von GET /v1/models.
# _classify() kannte es nicht, fiel auf "unknown" durch und wies den nackten
# Namen mit 404 model_not_found ab; nur die getaggte Anzeigeform "[free] ..."
# kam durch. Alle anderen tag-losen Namen rettet der :free-Anhaengetrick gegen
# den OpenRouter-Katalog - big-pickle nicht, es kommt von woanders.
# (Gemessen 2026-08-09: "opencode/big-pickle" -> 404, "[free] opencode/
# big-pickle" -> 200. Der Kostenregler stand die ganze Zeit korrekt auf free.)
#
# Zens eigenes /zen/v1/models antwortet mit id/object/created/owned_by und
# sonst nichts - keine Kontextlaenge, keine Faehigkeiten. Die Zahlen stammen
# deshalb aus models.dev, der Registry, die opencode selbst liest (Eintrag
# `opencode` -> `big-pickle`: context 200000, input 160000, output 32000,
# reasoning true). Absichtlich fest verdrahtet statt abgerufen: eine weitere
# Netzabhaengigkeit im Katalogpfad bringt fuer ein einzelnes Modell wenig.
# Faengt Zen an, es selbst zu melden, faellt das hier weg und wird gelesen wie
# bei allen anderen.
#
# Ohne `ctx` faellt jeder Abnehmer auf seinen eigenen Vorgabewert zurueck - pi
# auf 128k, was ein 200k-Modell in eine falsch bemessene Sitzung geschickt hat.
ZEN_MODELS = {
    "opencode/big-pickle": {
        "reasoning": True,
        "ctx": 200000,
        "max_output": 32000,
    },
}


def catalog_entry(model_id, owned_by, meta=None):
    """One /v1/models entry, carrying what this proxy already knows (an external catalog consumer's card 1).

    /v1/models used to return id/object/owned_by and - for cloud models - `reasoning`,
    while `name`, `ctx` and `prompt_price` sat in the very same metadata dict and were
    dropped on the floor. No consumer downstream could decide on cost, so every further
    Every further step depended on this one.

    `cost_class` is its own field rather than something callers parse out of the `[$]` /
    `[free]` display prefix: the prefix is decoration for humans, and a machine reading the
    cost class must not depend on it. Likewise `reachable` - availability lived only as the
    prose "temporarily unavailable" inside owned_by.

    A MISSING price is not the price 0.0. A free cloud model genuinely costs zero, which is
    a statement; a local model has no price at all, which is a different one. Absent
    metadata therefore produces no key rather than a placeholder.

    Built through a contract-first design pass (2026-08-02): 14 documented cases
    against an architecture pass that named this the
    first card and the only one that stays useful on its own.
    """
    entry = {"id": model_id, "object": "model", "owned_by": owned_by}
    if model_id.startswith(TAG_PAID):
        entry["cost_class"] = "paid"
    elif model_id.startswith(TAG_FREE):
        entry["cost_class"] = "free"
    elif model_id.startswith(TAG_LOCAL):
        entry["cost_class"] = "local"
    else:
        entry["cost_class"] = "unknown"
    # Only a local backend can be temporarily unavailable; cloud entries are catalogue rows,
    # and their liveness is the admission gate's business, not this field's.
    #
    # `reachable` ist seit 1.0.x die boolesche Bereitschaft je Modell: True, wenn das
    # Backend oben ist und diese id fuehrt; False fuer den -down-Eintrag. Die Rotation im
    # pi-Plugin entscheidet daran, ob ein Hop erlaubt ist. Deshalb kommt kein zweites Feld
    # (`ready`) dazu - zwei Namen fuer denselben Fakt widersprechen sich frueher oder
    # spaeter, und der -down-Suffix bleibt die Anzeigeform.
    if model_id.startswith(TAG_LOCAL):
        entry["reachable"] = "temporarily unavailable" not in (owned_by or "")
    else:
        entry["reachable"] = True
    # Vollstaendiges Durchreichen (2026-08-05). Vorher stand hier eine
    # Positivliste aus vier Schluesseln; alles andere, was ein Backend ueber
    # sein Modell wusste, fiel auf den Boden - darunter context_length. Die
    # Klienten fielen deshalb auf fest verdrahtete Zahlen zurueck, die bei
    # jeder Aenderung in jedem Container einzeln nachgezogen werden mussten.
    #
    # Die Felder, die der Proxy selbst verantwortet, gewinnen immer: `id`
    # traegt das [local]/[free]/[$]-Praefix, `owned_by` benennt das liefernde
    # Backend, `cost_class` und `reachable` sind Aussagen dieses Proxys. Wuerde
    # ein Backend sie ueberschreiben, waere der Katalog unbrauchbar.
    if meta:
        for key, value in meta.items():
            if key not in PROXY_EIGENE_FELDER:
                entry[key] = value

    # Kontextfenster und Reasoning zusaetzlich als property_id/value-Liste.
    #
    # pi-model-discovery liest aus einem gewoehnlichen Modelleintrag KEIN
    # Kontextfeld - weder `ctx` noch `meta.n_ctx`. Ohne Angabe nimmt es
    # GLOBAL_DEFAULTS.contextWindow = 128000 an, was bei einem 32k-Modell
    # mitten in der Sitzung als HTTP 400 endet.
    #
    # Diese Liste wertet es dagegen fuer jeden openai-kompatiblen Anbieter aus
    # (parseOpenAIModelEntries ruft readCloudflareProperties(entry.properties)
    # ohne Fallunterscheidung). Der Name im pi-Quelltext nennt Cloudflare, die
    # Funktion prueft aber nichts Anbieterspezifisches - wir bedienen ein
    # Format, keinen Anbieter.
    eigenschaften = []
    ctx_wert = entry.get("ctx")
    if isinstance(ctx_wert, int) and ctx_wert > 0:
        eigenschaften.append({"property_id": "context_window", "value": ctx_wert})
    if isinstance(entry.get("reasoning"), bool):
        eigenschaften.append({"property_id": "reasoning", "value": entry["reasoning"]})
    if eigenschaften:
        entry["properties"] = eigenschaften
    return entry

TAGS = (TAG_PAID, TAG_FREE, TAG_LOCAL)

OR_KEY = os.environ.get("OPENROUTER_API_KEY", "")
ZEN_HOST = "opencode.ai"
ZEN_PORT = 443


def _int_env(name, vorgabe):
    try:
        return int(os.environ.get(name, vorgabe))
    except (TypeError, ValueError):
        return vorgabe


LOG_FILE = os.environ.get("LLM_PROXY_LOG_FILE", "/var/log/llm-proxy.log")
LOG_MAX_BYTES = _int_env("LLM_PROXY_LOG_MAX_BYTES", 10 * 1024 * 1024)
LOG_BACKUPS = _int_env("LLM_PROXY_LOG_BACKUPS", 5)


class LogDatei:
    """Logdatei mit Groessenrotation, nur Standardbibliothek.

    Die Rotation laeuft beim Schreiben, nicht per Signal: eine umbenannte Datei
    ohne offenen Deskriptor bleibt so immer die aktuelle. 0 Bytes schaltet die
    Rotation ab.
    """
    def __init__(self, path, max_bytes, backups):
        self.path = path
        self.max_bytes = max_bytes
        self.backups = max(0, backups)
        self._fh = open(path, "a", encoding="utf-8")
        self._size = os.path.getsize(path)
        self._lock = threading.Lock()

    def write(self, text):
        with self._lock:
            n = len(text.encode("utf-8"))
            # Vor dem Schreiben rotieren, damit die neue Zeile in der frischen
            # Datei landet. Eine einzelne Zeile darf die Grenze ueberschreiten.
            if self.max_bytes > 0 and self._size and self._size + n > self.max_bytes:
                self._rotate()
            self._fh.write(text)
            self._fh.flush()
            self._size += n

    def flush(self):
        with self._lock:
            self._fh.flush()

    def _rotate(self):
        try:
            self._fh.close()
        except OSError:
            pass
        if self.backups <= 0:
            self._fh = open(self.path, "w", encoding="utf-8")
            self._size = 0
            return
        for i in range(self.backups - 1, 0, -1):
            alt, neu = f"{self.path}.{i}", f"{self.path}.{i + 1}"
            if os.path.exists(alt):
                try:
                    os.replace(alt, neu)
                except OSError:
                    pass
        try:
            os.replace(self.path, self.path + ".1")
        except OSError:
            pass
        self._fh = open(self.path, "a", encoding="utf-8")
        self._size = 0


class LogStrom:
    """Oeffnet die Logdatei beim ersten Schreiben; scheitert das, geht es nach stdout."""
    def __init__(self):
        self._ziel = None
        self._lock = threading.Lock()

    def _oeffnen(self):
        if self._ziel is not None:
            return
        if LOG_FILE:
            try:
                self._ziel = LogDatei(LOG_FILE, LOG_MAX_BYTES, LOG_BACKUPS)
                return
            except OSError as e:
                sys.stderr.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} "
                                 f"Logdatei {LOG_FILE} nicht beschreibbar ({e}), "
                                 f"schreibe nach stdout\n")
        self._ziel = sys.stdout

    def write(self, text):
        with self._lock:
            self._oeffnen()
        self._ziel.write(text)

    def flush(self):
        with self._lock:
            self._oeffnen()
        self._ziel.flush()


LOG = LogStrom()

# Browserpfad: Das LAN bleibt Vertrauenszone, deshalb keine Anmeldung fuer
# LAN-Clients. Geschlossen wird nur der Weg, den ein fremder Browser oeffnen
# koennte: Anfragen MIT Origin muessen zu einem erlaubten Host gehoeren.
# Leer = kein Browser-Origin erlaubt; Anfragen ohne Origin gehen unveraendert
# durch (curl, Agenten, Server). Kommagetrennte Hosts, z.B.
# LLM_PROXY_ALLOWED_ORIGINS=owui.example,localhost
ALLOWED_ORIGINS = {h.strip().lower() for h in
                   os.environ.get("LLM_PROXY_ALLOWED_ORIGINS", "").split(",")
                   if h.strip()}
# /admin/recheck prueft alle freien Wolkenmodelle. Hoechstens einmal je Fenster.
RECHECK_MIN_INTERVAL = float(os.environ.get("LLM_PROXY_RECHECK_MIN_INTERVAL", "300"))
_recheck_lock = threading.Lock()
_recheck_last = 0.0


def _origin_allowed(origin):
    """True, wenn der Host dieses Origin in der Allowlist steht."""
    if not origin:
        return False
    try:
        host = urllib.parse.urlsplit(origin).hostname
    except Exception:
        return False
    return bool(host) and host.lower() in ALLOWED_ORIGINS


def _recheck_erlaubt(jetzt=None):
    """Drossel fuer /admin/recheck. True, wenn der Aufruf laufen darf."""
    jetzt = time.monotonic() if jetzt is None else jetzt
    global _recheck_last
    with _recheck_lock:
        if jetzt - _recheck_last < RECHECK_MIN_INTERVAL:
            return False
        _recheck_last = jetzt
        return True

_disc_cache    = {}
_disc_lock     = threading.Lock()

_cloud_disc    = {"ids": None, "expiry": 0.0}
_cloud_lock    = threading.Lock()

LOCAL_BACKENDS = []
_backends_lock = threading.Lock()

_free_catalog        = {}
_paid_catalog        = {}
_cloud_catalog_lock  = threading.Lock()
_catalog_dirty       = False
_model_metadata       = {}     # model_id -> {name, ctx, reasoning}
_backends_up         = set()
_backends_up_lock    = threading.Lock()
_last_known          = {}    # backend_name -> [model_ids]
_last_known_lock     = threading.Lock()

_checker_pending = threading.Event()
_checker_exit    = threading.Event()

# ---- admission control: protect single-slot local backends from pile-up ----
ADMIT_TIMEOUT = 90       # max seconds a request waits for a backend slot
DEFAULT_SLOTS = 1        # local llama.cpp servers are single-slot by default
QUEUE_FACTOR  = 4        # max in-system (waiting+running) per backend = slots*QUEUE_FACTOR
_backend_sems     = {}   # name -> BoundedSemaphore(slots)
_backend_slots    = {}   # name -> int
_backend_inflight = {}   # name -> waiting+running count
_admit_lock       = threading.Lock()


def _build_sems(slots_map):
    """Semaphore je Backend. Bestehende mit unveraenderter Slot-Zahl werden BEHALTEN.

    Bis 1.0.1 entstand bei jedem SIGHUP eine frische, volle Semaphore, waehrend
    laufende Anfragen noch die alte hielten: bei slots=1 liefen danach zwei Anfragen
    gleichzeitig auf einem Ein-Slot-Backend. SIGHUP kommt von einem externen Werkzeug
    nach jedem Unit-Wechsel (228 im Log bis 2026-10-14), also mitten im Betrieb.
    Aendert sich die Slot-Zahl wirklich, gibt es eine neue Semaphore; was noch auf
    der alten laeuft, gibt sie dort zurueck (_admit liefert das belegte Objekt) -
    der Uebergang kann dann kurz alt+neu zulassen, aber nur bei echter Aenderung.
    """
    global _backend_sems, _backend_slots
    sems, slots = {}, {}
    for name, n in slots_map.items():
        n = max(1, int(n))
        alt = _backend_sems.get(name)
        sems[name] = alt if alt is not None and _backend_slots.get(name) == n \
            else threading.BoundedSemaphore(n)
        slots[name] = n
    _backend_sems, _backend_slots = sems, slots


def _admit(name):
    """Reserve a slot on backend `name`. Returns a token (truthy) the caller must
    hand back to _release, or None if rejected (queue full or waited too long).
    The token is the semaphore actually acquired, so a SIGHUP in between cannot
    send the release to a different object."""
    sem = _backend_sems.get(name)
    if sem is None:
        return True
    cap = _backend_slots.get(name, DEFAULT_SLOTS) * QUEUE_FACTOR
    with _admit_lock:
        if _backend_inflight.get(name, 0) >= cap:
            return None
        _backend_inflight[name] = _backend_inflight.get(name, 0) + 1
    if sem.acquire(timeout=ADMIT_TIMEOUT):
        return sem
    with _admit_lock:
        _backend_inflight[name] = max(0, _backend_inflight.get(name, 0) - 1)
    return None


def _release(name, token):
    if token is True or token is None:
        return
    try:
        token.release()
    except ValueError:
        pass
    with _admit_lock:
        _backend_inflight[name] = max(0, _backend_inflight.get(name, 0) - 1)


def _emit_log(msg):
    zeile = f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {msg}\n"
    try:
        LOG.write(zeile)
        LOG.flush()
    except Exception:
        sys.stdout.write(zeile)
        sys.stdout.flush()


def log(msg):
    _emit_log(msg)


# X-Request-Id: the arcint 1.4.2 grammar (1..128 chars of [A-Za-z0-9._:-]).
# A client value outside it is never logged or forwarded; it is replaced by a
# generated id in exactly the format the arcint server uses for its own.
_RID_MUSTER = re.compile(r"[A-Za-z0-9._:-]{1,128}")


def _rid_gueltig(wert):
    return isinstance(wert, str) and bool(_RID_MUSTER.fullmatch(wert))


def _rid_neu():
    return "req-" + uuid.uuid4().hex[:16]


def _anfrage_rid(wert):
    """Die Anfrage-ID dieses Zuges: gueltiger Client-Wert oder neu erzeugt."""
    if _rid_gueltig(wert):
        return wert
    return _rid_neu()


def _anfrage_kurz(body, modell=""):
    """Groesse, Modell und ein kurzer Hash -- genug zum Zuordnen, kein Prompt."""
    h = hashlib.sha256(body).hexdigest()[:12] if body else "-"
    return f"bytes={len(body)} model={modell or '-'} hash={h}"


def parse_param_count(text):
    if not text:
        return None
    m = re.search(r'(?<![\d.])(\d+\.?\d*)\s*[bB](?![a-zA-Z])', text)
    if m:
        return float(m.group(1))
    m = re.search(r'(\d+)\s*[bB]\s*(?:a\d+\s*[bB])?', text, re.IGNORECASE)
    if m:
        return float(m.group(1))
    return None


def _save_catalog():
    with _cloud_catalog_lock:
        meta_count = len(_model_metadata)
        data = {
            "last_update": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "free": dict(_free_catalog),
            "paid": dict(_paid_catalog),
            "metadata": dict(_model_metadata),
        }
    log(f"save catalog: {len(data['free'])} free + {len(data['paid'])} paid + {meta_count} metadata")
    tmp = CLOUD_CATALOG_FILE + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        os.rename(tmp, CLOUD_CATALOG_FILE)
    except Exception as e:
        log(f"save catalog FAIL: {type(e).__name__}: {e}")


def _save_last_known():
    with _last_known_lock:
        data = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "backends": {k: list(v) for k, v in _last_known.items()},
        }
    # Ein FESTER tmp-Name war ein Rennen: zwei parallele Speicherer (seit der
    # parallelen Discovery real) schreiben denselben Pfad und der zweite rename
    # findet ihn nicht mehr (FileNotFoundError, gemessen 2026-08-23 19:42).
    # Eindeutiger Name je Aufruf; das rename selbst bleibt atomar.
    tmp = f"{LAST_KNOWN_FILE}.tmp.{os.getpid()}.{threading.get_ident()}"
    try:
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        os.rename(tmp, LAST_KNOWN_FILE)
    except Exception as e:
        log(f"save last-known FAIL: {type(e).__name__}: {e}")


def _load_last_known():
    try:
        with open(LAST_KNOWN_FILE) as f:
            data = json.load(f)
        return data.get("backends", {})
    except FileNotFoundError:
        return {}
    except Exception as e:
        log(f"load last-known FAIL ({type(e).__name__}: {e})")
        return {}


def _load_catalog():
    try:
        with open(CLOUD_CATALOG_FILE) as f:
            data = json.load(f)
        free_m = data.get("free") or data.get("models") or {}
        paid_m = data.get("paid") or {}
        meta = data.get("metadata") or {}
        log(f"loaded {len(free_m)} free + {len(paid_m)} paid + {len(meta)} metadata from {CLOUD_CATALOG_FILE}")
        return free_m, paid_m, meta
    except FileNotFoundError:
        log(f"{CLOUD_CATALOG_FILE} not found, starting empty")
        return {}, {}, {}
    except Exception as e:
        log(f"load catalog FAIL ({type(e).__name__}: {e}), starting empty")
        return {}, {}, {}


def reload_backends(signum, frame):
    """SIGHUP: Backendliste UND alle Korrekturdateien neu einlesen (seit 1.0.9 auch
    reasoning-effort-map.json).

    Bis 2026-09-13 las SIGHUP nur local-backends.json. friendly-names.json,
    reasoning-models.json und local-ctx.json wurden EINMAL beim Start gelesen --
    wer eine davon aenderte, musste den Proxy neu starten und schnitt dabei jeden
    laufenden Strom ab. Fuer Dateien, deren eigener Zweck "corrections, not the
    source" ist, ist das die falsche Betriebsart.

    Ausloeser: qwen38-flash-next-e meldete sich nicht als denkendes
    Modell, weil vLLM -- anders als llama.cpp mit /props -- keine
    supported_parameters liefert und die Ableitung aus den Metadaten deshalb
    leer ausgeht. Die Korrektur stand binnen Sekunden in der Datei und kostete
    trotzdem einen vollen Neustart.

    Ohne Sperre, mit Absicht: das Neubinden eines Modul-Globals ist in CPython
    atomar, Leser sehen entweder die alte oder die neue Liste, nie eine halbe.
    LOCAL_BACKENDS behaelt seine Sperre, weil dort zusaetzlich die
    Admission-Semaphore daranhaengt.
    """
    global LOCAL_BACKENDS, FRIENDLY_NAMES, REASONING_OVERRIDES, LOCAL_CTX_OVERRIDES, MODEL_ALIASES
    global EFFORT_OVERRIDES, LOCAL_MAX_OUTPUT
    new = _load_local_backends()
    with _backends_lock:
        LOCAL_BACKENDS = new
    FRIENDLY_NAMES = _load_friendly_names()
    REASONING_OVERRIDES = _load_reasoning_overrides()
    LOCAL_CTX_OVERRIDES = _load_local_ctx_overrides()
    LOCAL_MAX_OUTPUT = _load_local_max_output()
    MODEL_ALIASES = _load_model_aliases()
    # bis 1.0.8 fehlte sie hier: ein neuer Eintrag (2026-10-21 ds41-flash, medium->high) wirkte
    # erst nach einem Neustart
    EFFORT_OVERRIDES = _load_effort_overrides()
    _feyntalk_warm_all()
    log(f"SIGHUP: reloaded local backends ({len(new)} entries), "
        f"friendly-names ({len(FRIENDLY_NAMES)}), "
        f"reasoning-models ({len(REASONING_OVERRIDES)}), "
        f"local-ctx ({len(LOCAL_CTX_OVERRIDES)}), local-max-output ({len(LOCAL_MAX_OUTPUT)}), "
        f"model-aliases ({len(MODEL_ALIASES)}), "
        f"reasoning-effort-map ({len(EFFORT_OVERRIDES)})")


def _do_recheck(reason):
    with _disc_lock:
        n_local = len(_disc_cache)
        _disc_cache.clear()
    with _cloud_lock:
        _cloud_disc["ids"] = None
        _cloud_disc["expiry"] = 0.0
    with _backends_up_lock:
        _backends_up.clear()
    log(f"{reason}: cleared caches (local={n_local}, backends_up=0) + scheduling re-check")
    _feyntalk_warm_all()
    _checker_pending.set()


def schedule_recheck(signum, frame):
    _do_recheck("SIGUSR1")


def _load_local_backends():
    global BACKEND_FEYNTALK
    try:
        with open(LOCAL_BACKENDS_FILE) as f:
            data = json.load(f)
        result = []
        slots_map = {}
        fts = {}
        for entry in data:
            result.append((entry["name"], entry["host"], int(entry["port"])))
            slots_map[entry["name"]] = int(entry.get("slots", DEFAULT_SLOTS))
            mode = entry.get("feyntalk", "off")
            if mode not in FEYNTALK_SETTINGS:
                log(f"feyntalk: backend {entry['name']} has unknown mode {mode!r}; off")
                mode = "off"
            fts[entry["name"]] = mode
        _build_sems(slots_map)
        BACKEND_FEYNTALK = fts
        with FEYNTALK_CACHE_LOCK:
            FEYNTALK_CACHE.clear()
        log(f"loaded {len(result)} local backends from {LOCAL_BACKENDS_FILE}: "
            f"{[(b[0], slots_map[b[0]]) for b in result]}")
        return result
    except FileNotFoundError:
        _build_sems({n: DEFAULT_SLOTS for n, _, _ in DEFAULT_LOCAL_BACKENDS})
        BACKEND_FEYNTALK = {n: "off" for n, _, _ in DEFAULT_LOCAL_BACKENDS}
        with FEYNTALK_CACHE_LOCK:
            FEYNTALK_CACHE.clear()
        log(f"{LOCAL_BACKENDS_FILE} not found, using built-in defaults")
        return list(DEFAULT_LOCAL_BACKENDS)
    except Exception as e:
        _build_sems({n: DEFAULT_SLOTS for n, _, _ in DEFAULT_LOCAL_BACKENDS})
        BACKEND_FEYNTALK = {n: "off" for n, _, _ in DEFAULT_LOCAL_BACKENDS}
        with FEYNTALK_CACHE_LOCK:
            FEYNTALK_CACHE.clear()
        log(f"local-backends config error ({type(e).__name__}: {e}), using defaults")
        return list(DEFAULT_LOCAL_BACKENDS)


def is_cloud_model(model):
    return any(model.startswith(p) for p in CLOUD_PREFIXES)


def strip_tag(mid):
    for t in TAGS:
        if mid.startswith(t):
            return mid[len(t):]
    return mid


def _anfrage_id(raw_model):
    """Angezeigte Katalog-id -> die id, unter der geroutet wird.

    Zieht das [..]-Praefix ab, das -na eines rate-limitierten Modells und haengt
    ":free" wieder an, wenn das Gratismodell so im Katalog steht (die Anzeige laesst
    es weg). Bis 1.0.1 stand hier `\\-na(?=:|\\$)` - `\\$` ist ein woertliches
    Dollarzeichen, und die Anzeigeform "[free] base-na" kam nie auf "base:free" zurueck.
    Ein Modell, das wirklich auf "-na" endet und so im Katalog steht, bleibt.
    """
    model = strip_tag(raw_model)
    if raw_model.startswith(TAG_LOCAL) and model.endswith(DOWN_SUFFIX):
        # Anzeige eines gerade toten lokalen Backends: auf die echte id zurueck, damit
        # die Anfrage ehrlich mit 502/503 scheitert statt als 'unbekannt' mit 404.
        return model[:-len(DOWN_SUFFIX)]
    with _cloud_catalog_lock:
        # Fuer das -na-Abziehen zaehlt alles, was OpenRouter fuehrt: eine nur in den
        # Metadaten bekannte id, die wirklich auf -na endet, bleibt.
        echt = model in _free_catalog or model in _paid_catalog or model in _model_metadata
        bekannt = model in _free_catalog or model in _paid_catalog
    if not echt:
        model = re.sub(r"-na(?=:|$)", "", model)
        with _cloud_catalog_lock:
            bekannt = model in _free_catalog or model in _paid_catalog
    # ":free" nur fuer ein Gratismodell, das sonst niemand meint: nie fuer einen
    # [local]- oder [$]-Auftrag, nie fuer eine id, die ein lokales Backend fuehrt
    # oder die als bezahltes Modell im Katalog steht. Sonst wird aus "[local] x"
    # still die Wolken-id "x:free" (DSGVO-Fall unter 'local') bzw. aus "[$] y" die
    # Gratisvariante.
    if ":" not in model and not bekannt and not raw_model.startswith((TAG_LOCAL, TAG_PAID)):
        with _cloud_catalog_lock:
            frei = model + ":free" in _free_catalog
        if frei and find_backend_for_model(model) is None:
            model = model + ":free"
    return model


# --- Kostenregler -----------------------------------------------------------
# Ein SCHALTER, keine Mauer: standardmaessig aus, damit interaktive Nutzung
# unveraendert bleibt. Eingeschaltet weist der Proxy kostenpflichtige Modelle ab.
#
# Warum ueberhaupt im Gateway: die Regel "kein Paid-Offloading" stand nur im
# Rollen-Prompt EINES Agenten. Ein anderer Agent, der diesen Prompt nicht liest,
# empfahl beim ersten Widerstand prompt ein kostenpflichtiges Modell
# (2026-08-01, @foreman beim HumanEval-Plateau). Eine Budgetregel, die nur in
# einem Prompt steht, gilt fuer niemanden sonst - siehe die Hausregel
# "allowlist is mechanical, not a prompt". Das Gateway sieht JEDE Anfrage.
POLICY_FILE = os.path.join(CONFIG_DIR, "policy.json")
DEFAULT_MODE = "max"          # ohne policy.json bleibt alles wie bisher
STRENGSTER_MODUS = "local"    # policy.json da, aber unbrauchbar: so streng wie moeglich
CHEAP_MAX_PROMPT_PRICE = 0.50  # $/MTok, ueberschreibbar in policy.json
MODES = ("local", "free", "cheap", "max")
_policy_cache = {"key": None, "data": {}}
_policy_lock = threading.Lock()


def _policy():
    """Liest policy.json bei jeder Aenderung neu - kein Neustart noetig.

    Fehlt die Datei, gilt DEFAULT_MODE (dokumentiert: dann bleibt alles wie vorher).
    Ist sie DA, aber unlesbar, leer oder kein Objekt, gilt STRENGSTER_MODUS - bis
    1.0.1 fiel sie dann auf 'max', und ein Komma zu viel oder ein Editor mitten im
    Schreiben liess unter 'local' (DSGVO) bezahlte Wolkenanfragen durch. Ein
    Kostenregler, der beim Tippfehler aufmacht, ist keiner.
    Schluessel ist mtime_ns+Groesse: zwei Schreibvorgaenge in derselben Sekunde
    (leer, dann voll) muessen beide gesehen werden.

    Neu geladen wird unter einer Sperre, und der Schluessel wird ERST NACH den Daten
    gesetzt: wer waehrend des Parsens kommt, wartet, statt den alten Stand zu
    bekommen (beim Start waere das {} = 'max'). Ein unbekannter Modus wird hier
    einmal auf den strengsten gesetzt und einmal protokolliert, nicht je Anfrage.
    """
    try:
        st = os.stat(POLICY_FILE)
    except OSError:
        return {}
    key = (st.st_mtime_ns, st.st_size)
    if key == _policy_cache["key"]:
        return _policy_cache["data"]
    with _policy_lock:
        if key == _policy_cache["key"]:
            return _policy_cache["data"]
        try:
            with open(POLICY_FILE) as f:
                data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError(f"kein JSON-Objekt, sondern {type(data).__name__}")
            if data.get("mode", DEFAULT_MODE) not in MODES:
                log(f"policy: unbekannter Modus '{data.get('mode')}' - gilt als "
                    f"'{STRENGSTER_MODUS}'")
                data = dict(data, mode=STRENGSTER_MODUS)
            else:
                log(f"policy reloaded: {data}")
        except Exception as e:
            data = {"mode": STRENGSTER_MODUS, "_unlesbar": str(e)}
            log(f"policy.json unlesbar ({e}) - Kostenregler steht auf "
                f"'{STRENGSTER_MODUS}', bis die Datei repariert ist")
        _policy_cache["data"] = data
        _policy_cache["key"] = key
        return data


def _modus(pol):
    """Gueltiger Modus. _policy() normalisiert schon; das hier faengt nur Aufrufer
    ab, die ein eigenes dict reichen - ebenfalls zum strengsten hin."""
    mode = pol.get("mode", DEFAULT_MODE)
    return mode if mode in MODES else STRENGSTER_MODUS


def _pro_mtok(preis):
    """OpenRouters pricing.prompt ist $/TOKEN; Grenzen hier sind $/MTok.

    Bis 1.0.1 wurde die Zahl direkt mit 0.50 verglichen - jede Zahl pro Token liegt
    darunter: Modus 'cheap' liess jedes Modell durch, und die Auto-Entdeckung nahm
    o1-pro (150 $/MTok) als 'billig' auf. Der Katalog behaelt OpenRouters Einheit
    (Klienten lesen prompt_price aus /v1/models); umgerechnet wird nur beim Vergleich.
    """
    return float(preis) * 1_000_000


def _classify(raw_model, model):
    """'local' | 'free' | 'paid' | 'unknown' - woher kaeme die Antwort?

    'unknown' heisst: weder ein lokales Backend noch die Wolkenkataloge noch
    OpenRouters Metadaten (alle gefuehrten Modelle, auch teure) kennen es. Frueher fiel dieser Fall unter 'paid',
    weil das die Rueckfallantwort war - mit der Folge, dass ein Tippfehler im
    Modellnamen unter mode=free als Kostenverstoss gemeldet wurde. Fehlendes
    Wissen ist aber keine Kostenaussage.
    """
    if raw_model.startswith(TAG_LOCAL):
        # Das Praefix allein ist kein Beweis: "[local] openai/x" ohne lokales Backend
        # ging bis 1.0.2 als 'local' am Regler vorbei und dann - weil das Routing die
        # Einstufung nicht fragte - in die Wolke, auch unter Modus 'local' (DSGVO).
        try:
            if find_backend_for_model(model) is not None:
                return "local"
        except Exception:
            pass
        return "unknown"
    if raw_model.startswith(TAG_FREE) or model.endswith(":free"):
        return "free"
    if raw_model.startswith(TAG_PAID):
        return "paid"
    try:
        if find_backend_for_model(model) is not None:
            return "local"
    except Exception:
        pass
    if model in ZEN_MODELS:
        return "free"
    with _cloud_catalog_lock:
        if model in _free_catalog:
            return "free"
        if model in _paid_catalog:
            return "paid"
        # Die Kataloge sind die ANZEIGE (nur Billiges im Bezahlkatalog). Bekannt ist,
        # was OpenRouter fuehrt: ein teures, aber existierendes Modell ist 'paid' und
        # wird vom Regler nach Kosten beurteilt - nicht als 'unknown' mit 404 abgewiesen,
        # auch nicht unter 'max'.
        if model in _model_metadata:
            return "paid"
    return "unknown"


def _policy_blocked(raw_model, model, kind=None):
    """(True, Grund) wenn der eingestellte Modus dieses Modell nicht zulaesst.

    Vier Stufen, aufsteigend durchlaessig:
      local - NUR lokale Backends. Fuer Faelle, in denen keine Daten das Haus
              verlassen duerfen (DSGVO). Auch "free" ist Cloud.
      free  - lokal + kostenlose Cloud-Modelle.
      cheap - zusaetzlich kostenpflichtige bis cheap_max_prompt_price ($/MTok).
              UNBEKANNTER Preis gilt als teuer und wird abgewiesen: bei einem
              Kostenregler ist fail-closed die richtige Richtung.
      max   - alles. Standard, wenn keine policy.json existiert.
    """
    pol = _policy()
    mode = _modus(pol)
    if mode == "max":
        return (False, "")

    # Nur einstufen, wenn der Aufrufer es nicht schon getan hat. Zwei
    # unabhaengige Urteile ueber dieselbe Anfrage koennen auseinanderlaufen,
    # sobald der discover()-Zwischenspeicher dazwischen ablaeuft.
    if kind is None:
        kind = _classify(raw_model, model)
    if kind == "local":
        return (False, "")
    if kind == "unknown":
        # Fail-closed, aber die Begruendung sagt die Wahrheit. Die Aufrufstelle
        # macht daraus ein 404 model_not_found statt eines 403 policy_blocked.
        return (True, f"Modell '{raw_model}' ist unbekannt")
    if mode == "local":
        return (True, f"Modus 'local': nur lokale Modelle, '{raw_model}' ist {kind}")
    if kind == "free":
        return (False, "")
    if mode == "free":
        return (True, f"Modus 'free': keine kostenpflichtigen Modelle")

    # mode == "cheap": Preis entscheidet
    try:
        limit = float(pol.get("cheap_max_prompt_price", CHEAP_MAX_PROMPT_PRICE))
    except (TypeError, ValueError):
        return (True, "Modus 'cheap': cheap_max_prompt_price in policy.json ist "
                      "keine Zahl - fail-closed")
    with _cloud_catalog_lock:
        meta = _model_metadata.get(model) or {}
    price = meta.get("prompt_price")
    try:
        mtok = _pro_mtok(price) if price is not None else None
    except (TypeError, ValueError):
        mtok = None
    if mtok is None:
        return (True, f"Modus 'cheap': kein Preis bekannt fuer '{model}' "
                      f"(Katalog noch nicht aufgefrischt) - fail-closed")
    if mtok > limit:
        return (True, f"Modus 'cheap': ${mtok:g}/MTok ueber Grenze ${limit:g}/MTok")
    return (False, "")


def _aktiver_modus():
    """Der gueltige Modus aus policy.json - dieselbe Regel wie fuer Anfragen."""
    return _modus(_policy())


def _katalog_zeigt(mode, kind, unavailable):
    """Soll /v1/models diesen Eintrag im eingestellten Modus anbieten?

    kind:        'local' | 'free' | 'paid' - dieselbe Einstufung wie _classify().
    unavailable: lokales Backend gerade unten (-down) oder Wolkenmodell gerade
                 nicht erreichbar (-na).

    In 'local' und 'free' zeigt der Katalog nur, was der Kostenregler auch
    durchlaesst UND was gerade antwortet. Vorher bot /v1/models in jedem Modus
    alles an: Clients sahen bezahlte Modelle, die bei der Anfrage mit 403
    abgewiesen wurden, und -down/-na-Leichen, die nie antworteten (beobachtet
    2026-10-02). 'cheap' und 'max' bleiben unveraendert - dort entscheidet der
    Preis erst bei der Anfrage, und die Anhaengsel informieren.
    """
    if mode not in ("local", "free"):
        return True
    if unavailable:
        return False
    if mode == "local":
        return kind == "local"
    return kind in ("local", "free")


# Kontextfenster der lokalen Modelle, aus derselben /v1/models-Antwort, aus der ohnehin
# die id gelesen wird. Getrennt gehalten, weil der Rueckgabewert von
# _fetch_backend_models durch discover(), _last_known und eine persistierte Datei laeuft
# - dort den Typ zu aendern haette Reichweite. Bleibt die Tabelle leer, verhaelt sich
# alles wie vorher: kein ctx im Katalog, die Oberflaeche nimmt ihren Vorgabewert.
_local_ctx = {}
# Das vollstaendige Modellobjekt je lokalem Modell, so wie das Backend es
# meldet. Frueher wurde nur der Kontext herausgezogen und der Rest verworfen.
_local_meta = {}
_local_ctx_lock = threading.Lock()

# vLLM sagt max_model_len, llama.cpp n_ctx (teils unter "meta"), andere context_length.
_CTX_KEYS = ("max_model_len", "context_length", "ctx", "n_ctx", "max_context_length")


def _ctx_of(m):
    for k in _CTX_KEYS:
        v = m.get(k)
        if isinstance(v, int) and v > 0:
            return v
    meta = m.get("meta")
    if isinstance(meta, dict):
        # n_ctx VOR n_ctx_train. Die Reihenfolge war umgekehrt und damit falsch:
        #   n_ctx        womit der Server laeuft (llama.cpp -c ...)
        #   n_ctx_train  wofuer das Modell trainiert wurde
        # Wer eine Anfrage zusammenstellt, braucht die Laufzeitgrenze. Das
        # Trainingsfenster ist eine Modelleigenschaft und sagt nichts darueber,
        # was dieser Server annimmt.
        #
        # 2026-08-07: qwen3.6-coding meldete so 262144 statt 32768, ein Client zeigte
        # 66k an und lief in HTTP 400 (in_chars=101709). Sah aus wie ein
        # Modellproblem, war eine falsche Zahl.
        for k in ("n_ctx", "n_ctx_train"):
            v = meta.get(k)
            if isinstance(v, int) and v > 0:
                return v
    return None


def local_ctx(mid):
    with _local_ctx_lock:
        return _local_ctx.get(mid)


def local_meta(mid):
    """Alles, was das Backend ueber dieses Modell gemeldet hat."""
    with _local_ctx_lock:
        m = _local_meta.get(mid)
        return dict(m) if m else None


def _fetch_backend_props(host, port):
    """The backend's /props document, or None.

    One /props-speaking server serves one model, so whatever it says applies to
    every id that server advertises. vLLM-style backends have no /props and
    answer 404 -- which is why this is only asked when /v1/models stayed silent
    about something we need.

    Returns the whole document rather than one field: two callers want different
    parts of it (chat_template_caps and the context length), and asking twice
    for the same JSON would be a second round trip for nothing.
    """
    conn = None
    try:
        conn = http.client.HTTPConnection(host, port, timeout=CONNECT_TIMEOUT)
        conn.request("GET", "/props")
        resp = conn.getresponse()
        if resp.status != 200:
            resp.read()
            return None
        props = json.loads(resp.read())
        return props if isinstance(props, dict) else None
    except Exception:
        return None
    finally:
        try:
            if conn:
                conn.close()
        except Exception:
            pass


# --------------------------------------------------------- feyntalk token mode
# The gateway half of token mode. Off by
# default: a backend only enters this path when local-backends.json pins
# `"feyntalk": "tokens"` (or "text"). The engine's material is the contract;
# feyntalk-render 0.1 is optional here and token mode is unavailable without it.
#
# Vocabulary note: the tags are data from the material. This file never spells
# a model's markers; the fixtures use synthetic ones.

class _FeyntalkRefusal(Exception):
    """This request cannot be rendered for token mode; fall back to text."""


class _FeyntalkStrict(Exception):
    """A `-ft` name can't run token mode: fail the request instead of the text path.
    Operator decision: failures must show."""

    def __init__(self, status, code, message):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


# Per backend: token turns served, turns that fell back to text, `-ft` turns failed.
FEYNTALK_COUNTS = {}
_FEYNTALK_COUNTS_LOCK = threading.Lock()


def _feyntalk_count(name, what):
    with _FEYNTALK_COUNTS_LOCK:
        c = FEYNTALK_COUNTS.setdefault(name, {"tokens": 0, "fallbacks": 0, "errors": 0})
        c[what] += 1


class _FeyntalkBadRequest(Exception):
    """Ein preserve_thinking des Anrufers ist kein bool: 400, nicht weiterleiten."""


def _feyntalk_log_once(key, message):
    if key in _FEYNTALK_WARNED:
        return
    _FEYNTALK_WARNED.add(key)
    log(message)


def _feyntalk_unavailable():
    if FEYNTALK_AVAILABLE:
        return False
    _feyntalk_log_once("import", "feyntalk: feyntalk_render is not importable "
                                f"({FEYNTALK_IMPORT_ERROR}); token mode is unavailable")
    return True


def _version_tuple(text):
    """(major, minor, patch, rank) aus einem Versionsstring, sonst None.

    Ein fuehrendes "v"/"V" wird entfernt, Build-Metadaten nach "+" ebenfalls.
    Ein Vorab-Release ("1.4.0-rc1") bekommt rank 0 und ist damit KLEINER als die
    Freigabe (rank 1): eine Release-Candidate besteht ein >=-Gate nicht. Nicht
    parsbares (kein Ziffernbeginn in einem der drei Teile) ist None und laesst
    das Gate mit Nennung des Strings scheitern."""
    if not isinstance(text, str) or not text:
        return None
    t = text.strip()
    if t[:1] in ("v", "V"):
        t = t[1:]
    core = t.split("+", 1)[0]
    main, _, pre = core.partition("-")
    chunks = main.split(".")
    if not chunks or not chunks[0]:
        return None
    parts = []
    for chunk in chunks[:3]:
        digits = ""
        for ch in chunk:
            if ch.isdigit():
                digits += ch
            else:
                break
        if not digits:
            return None
        parts.append(int(digits))
    while len(parts) < 3:
        parts.append(0)
    return (parts[0], parts[1], parts[2], 1 if pre == "" else 0)


def _feyntalk_renderer_version():
    """Die installierte feyntalk-render-Version, sonst None."""
    v = getattr(feyntalk_render, "__version__", None) if feyntalk_render else None
    if isinstance(v, str) and v:
        return v
    try:
        import importlib.metadata as _md
        return _md.version("feyntalk-render")
    except Exception:
        return None


def _feyntalk_engine_handshake(props):
    """(version, contract) aus /props, je None wenn nicht gemeldet.

    version: props['version'] oder props['build']['version'].
    contract: props['arcint']['v'], props['contract'] oder props['contract_version']."""
    version = props.get("version")
    build = props.get("build")
    if not isinstance(version, str) and isinstance(build, dict):
        version = build.get("version")
    arcint = props.get("arcint")
    contract = arcint.get("v") if isinstance(arcint, dict) else None
    if contract is None:
        contract = props.get("contract", props.get("contract_version"))
    return (version if isinstance(version, str) and version else None,
            contract if isinstance(contract, int) and not isinstance(contract, bool) else None)


def _feyntalk_setting(name):
    return BACKEND_FEYNTALK.get(name, "off")


def _fetch_renderer_material(host, port):
    """GET /v1/responses/renderer -> (dict|None, reason). No auth, like /props."""
    conn = None
    try:
        conn = http.client.HTTPConnection(host, port, timeout=max(CONNECT_TIMEOUT, 10.0))
        conn.request("GET", "/v1/responses/renderer")
        resp = conn.getresponse()
        raw = resp.read()
        if resp.status != 200:
            return None, f"status {resp.status}"
        material = json.loads(raw)
        return material if isinstance(material, dict) else None, "not a JSON object"
    except Exception as e:  # noqa: BLE001 - a failed fetch is a text-mode reason
        return None, f"{type(e).__name__}: {e}"
    finally:
        try:
            if conn:
                conn.close()
        except Exception:
            pass


def _feyntalk_store(result, backend_name):
    with FEYNTALK_CACHE_LOCK:
        FEYNTALK_CACHE[backend_name] = result


def _feyntalk_drop_cache(name):
    """Verwirft die Faehigkeits-Eintraege dieses Backends und ihr Material.

    Schluessel sind `name` (das eigene Setting) und `name|<setting>` (ein Alias);
    beide muessen weg. Das Material wird nur verworfen, wenn ein Eintrag dieses
    Backends darauf zeigt."""
    with FEYNTALK_CACHE_LOCK:
        hashes = set()
        for key in [k for k in FEYNTALK_CACHE if k == name or k.startswith(name + "|")]:
            entry = FEYNTALK_CACHE.pop(key, None)
            if isinstance(entry, dict) and isinstance(entry.get("hash"), str):
                hashes.add(entry["hash"])
        for material_hash in hashes:
            FEYNTALK_MATERIALS.pop(material_hash, None)


def _feyntalk_409_kind(body_bytes):
    """'material' | 'prompt' | None aus dem 409-error.param.

    arcint.renderer_hash heisst: das Material hat sich bewegt, der Cache muss
    weg. arcint.render.prompt_sha256 heisst: nur der Prompt passt nicht, das
    Material bleibt."""
    try:
        err = (json.loads(body_bytes or b"{}") or {}).get("error")
    except ValueError:
        return None
    param = err.get("param") if isinstance(err, dict) else None
    if param == "arcint.renderer_hash":
        return "material"
    if param == "arcint.render.prompt_sha256":
        return "prompt"
    return None


def _feyntalk_n_ctx(props):
    """Die Kontextlaenge des Backends aus /props.

    arcint meldet sie unter model.n_ctx (src/api/handlers.cpp props()); die
    uebrigen Schluessel kommen aus _ctx_of, falls sich die Form bewegt."""
    model = props.get("model")
    if isinstance(model, dict):
        n = _ctx_of(model)
        if n:
            return n
    return _ctx_of(props)


def _feyntalk_warm_build(name, host, port, served_hash, entry):
    """Liest und baut das Material einmal; setzt danach das Warten-Event."""
    t0 = time.monotonic()
    built = None
    try:
        raw, why = _fetch_renderer_material(host, port)
        if raw is None:
            raise _FTMaterialError("renderer endpoint: " + why)
        material = _ft_from_dict(raw)
        if served_hash and served_hash != material.get("hash"):
            raise _FTMaterialError(f"props hash {served_hash} != material hash {material.get('hash')}")
        built = {"material": material, "renderer": _FTRenderer(material)}
    except Exception as e:  # noqa: BLE001 - ein geplatzter Warmlauf ist kein Request-Fehler
        _feyntalk_log_once(name + "|warm|" + str(served_hash),
                           f"feyntalk: {name} material warm-up failed ({type(e).__name__}: {e})")
    try:
        if built is not None:
            with FEYNTALK_CACHE_LOCK:
                FEYNTALK_MATERIALS[served_hash] = built
            log(f"feyntalk: {name} material warmed in {time.monotonic()-t0:.1f}s")
    finally:
        entry["evt"].set()


def _feyntalk_warm_and_wait(name, host, port, served_hash):
    """Ein Bau je Hash: der erste Aufrufer startet ihn, spaetere warten darauf.

    Gibt den Merkzettel-Eintrag zurueck oder None (noch nicht fertig / geplatzt)."""
    with FEYNTALK_CACHE_LOCK:
        known = FEYNTALK_MATERIALS.get(served_hash)
        if known is not None:
            return known
        entry = _FEYNTALK_WARM.get(served_hash)
        start = entry is None
        if start:
            entry = {"evt": threading.Event()}
            _FEYNTALK_WARM[served_hash] = entry
    if start:
        try:
            threading.Thread(target=_feyntalk_warm_build,
                             args=(name, host, port, served_hash, entry), daemon=True).start()
        except RuntimeError as e:
            # Kein Faden: Event setzen und Eintrag entfernen, sonst warten
            # spaetere Requests den vollen Timeout.
            _feyntalk_log_once(name + "|warmstart|" + str(served_hash),
                               f"feyntalk: {name} material warm-up thread failed: {e}")
            with FEYNTALK_CACHE_LOCK:
                _FEYNTALK_WARM.pop(served_hash, None)
            entry["evt"].set()
    entry["evt"].wait(FEYNTALK_WARM_TIMEOUT)
    with FEYNTALK_CACHE_LOCK:
        return FEYNTALK_MATERIALS.get(served_hash)


def _feyntalk_warm_all():
    """Stoesst EINEN Hintergrundlauf an, der die konfigurierten Paare probt.

    Der Aufrufer kehrt sofort zurueck: die Alias-Aufloesung
    (find_backend_for_model -> discover) und die Probes koennen Netz-I/O sein
    und duerfen weder den Listen-Socket noch den SIGHUP-Pfad aufhalten. Laeuft
    schon ein Lauf, wird nur ein Folgelauf vorgemerkt (SIGHUP-Sturm): der
    laufende Faden nimmt die dann gueltige Konfiguration genau einmal mit."""
    with _FEYNTALK_WARM_RUN_LOCK:
        if _FEYNTALK_WARM_RUN["active"]:
            _FEYNTALK_WARM_RUN["pending"] = True
            return
        _FEYNTALK_WARM_RUN["active"] = True
    try:
        threading.Thread(target=_feyntalk_warm_loop, daemon=True).start()
    except Exception as e:  # noqa: BLE001 - kein Faden: den Lauf freigeben, nie den Aufrufer (SIGHUP) stoeren
        with _FEYNTALK_WARM_RUN_LOCK:
            _FEYNTALK_WARM_RUN["active"] = False
            _FEYNTALK_WARM_RUN["pending"] = False
        _feyntalk_log_once("warmrun|start|" + type(e).__name__,
                           f"feyntalk: material warm-up run could not start: {e}")


def _feyntalk_warm_loop():
    while True:
        try:
            _feyntalk_warm_run()
        except Exception:  # noqa: BLE001 - der Warmlauf darf nie den Prozess stoeren
            pass
        with _FEYNTALK_WARM_RUN_LOCK:
            if _FEYNTALK_WARM_RUN["pending"]:
                _FEYNTALK_WARM_RUN["pending"] = False
                continue
            _FEYNTALK_WARM_RUN["active"] = False
            return


def _feyntalk_warm_run():
    for backend in list(LOCAL_BACKENDS):
        name = backend[0]
        settings = {_feyntalk_setting(name)}
        for c in MODEL_ALIASES.values():
            ft = c.get("feyntalk")
            if ft not in ("tokens", "text"):
                continue
            try:
                target = find_backend_for_model(c["model"])
            except Exception:  # noqa: BLE001 - Discovery darf den Warmlauf nicht stoppen
                target = None
            if target is not None and target[0] == name:
                settings.add(ft)
        for setting in settings:
            if setting in ("tokens", "text"):
                threading.Thread(
                    target=lambda b=backend, st=setting: _feyntalk_warm_probe(b, st),
                    daemon=True).start()


def _feyntalk_warm_probe(backend, setting):
    try:
        _feyntalk_capability(backend, setting=setting)
    except Exception:  # noqa: BLE001 - der Warmlauf darf nie den Prozess stoeren
        pass


def _feyntalk_capability(backend, setting=None):
    """What this backend can serve: {mode: off|text|tokens, hash, material,
    renderer, reason, checked}. Cached for FEYNTALK_TTL; a changed hash refetches.

    `setting` overrides the backend's own feyntalk setting for one request (the
    alias's mode); it gets its own cache key so the two names never mix."""
    name, host, port = backend
    setting = _feyntalk_setting(name) if setting is None else setting
    cache_key = name if setting == _feyntalk_setting(name) else name + "|" + setting
    if setting == "off":
        return {"mode": "off"}
    if setting == "text":
        return {"mode": "text", "reason": "configured text"}
    if _feyntalk_unavailable():
        return {"mode": "text", "reason": "feyntalk_render unavailable"}

    now = time.monotonic()
    rv = _feyntalk_renderer_version()
    if _version_tuple(rv) is None or _version_tuple(rv) < _version_tuple(FEYNTALK_MIN_RENDERER_VERSION):
        reason = f"renderer {rv or 'unknown'} < {FEYNTALK_MIN_RENDERER_VERSION}"
        result = {"mode": "text", "reason": reason, "checked": now}
        _feyntalk_log_once(cache_key + "|renderer",
                           f"feyntalk: {name} {reason}; text mode")
        _feyntalk_store(result, cache_key)
        return result
    with FEYNTALK_CACHE_LOCK:
        cached = FEYNTALK_CACHE.get(cache_key)
        if cached is not None and cached.get("checked", 0.0) + FEYNTALK_TTL > now:
            return cached

    props = _fetch_backend_props(host, port)
    if not isinstance(props, dict) or props.get("token_mode") != "ready":
        reason = "props: token_mode %r" % (None if not isinstance(props, dict)
                                           else props.get("token_mode"))
        result = {"mode": "text", "reason": reason, "checked": now}
        _feyntalk_log_once(cache_key + "|props", f"feyntalk: {name} {reason}; text mode")
        _feyntalk_store(result, cache_key)
        return result

    eversion, contract = _feyntalk_engine_handshake(props)
    if _version_tuple(eversion) is None or _version_tuple(eversion) < _version_tuple(FEYNTALK_MIN_ENGINE_VERSION):
        reason = f"engine version {eversion or 'unknown'} < {FEYNTALK_MIN_ENGINE_VERSION}"
        result = {"mode": "text", "reason": reason, "checked": now}
        _feyntalk_log_once(cache_key + "|engine",
                           f"feyntalk: {name} {reason}; text mode")
        _feyntalk_store(result, cache_key)
        return result
    if contract is not None and contract != FEYNTALK_CONTRACT_V:
        reason = f"engine contract v{contract} != v{FEYNTALK_CONTRACT_V}"
        result = {"mode": "text", "reason": reason, "checked": now}
        _feyntalk_log_once(cache_key + "|contract",
                           f"feyntalk: {name} {reason}; text mode")
        _feyntalk_store(result, cache_key)
        return result

    served_hash = props.get("renderer_hash")
    if cached is not None and cached.get("mode") == "tokens" and served_hash:
        if cached.get("hash") == served_hash and cached.get("renderer") is not None:
            result = dict(cached)
            result["checked"] = now
            _feyntalk_store(result, cache_key)
            return result

    known = None
    if served_hash:
        # Der Warmlauf hat das Material womoeglich schon gebaut; ein Request
        # wartet dann auf diesen Bau statt einen zweiten zu starten.
        known = _feyntalk_warm_and_wait(name, host, port, served_hash)
    if known is not None:
        material = known["material"]
        material_hash = served_hash
    else:
        raw, why = _fetch_renderer_material(host, port)
        if raw is None:
            reason = "renderer endpoint: " + why
            result = {"mode": "text", "reason": reason, "checked": now}
            _feyntalk_log_once(cache_key + "|fetch", f"feyntalk: {name} {reason}; text mode")
            _feyntalk_store(result, cache_key)
            return result
        try:
            material = _ft_from_dict(raw)
        except _FTMaterialError as e:
            reason = f"material hash check: {e}"
            result = {"mode": "text", "reason": reason, "checked": now}
            _feyntalk_log_once(cache_key + "|material", f"feyntalk: {name} {reason}; text mode")
            _feyntalk_store(result, cache_key)
            return result
        if served_hash and served_hash != material.get("hash"):
            reason = f"props hash {served_hash} != material hash {material.get('hash')}"
            result = {"mode": "text", "reason": reason, "checked": now}
            _feyntalk_log_once(cache_key + "|hash", f"feyntalk: {name} {reason}; text mode")
            _feyntalk_store(result, cache_key)
            return result
        material_hash = material["hash"]
        with FEYNTALK_CACHE_LOCK:
            known = FEYNTALK_MATERIALS.get(material_hash)
        if known is None:
            try:
                renderer = _FTRenderer(material)
            except _FTRenderError as e:
                reason = f"template refusal: {e}"
                result = {"mode": "text", "reason": reason, "checked": now}
                _feyntalk_log_once(cache_key + "|template", f"feyntalk: {name} {reason}; text mode")
                _feyntalk_store(result, cache_key)
                return result
            known = {"material": material, "renderer": renderer}
            with FEYNTALK_CACHE_LOCK:
                FEYNTALK_MATERIALS[material_hash] = known

    changed = cached is None or cached.get("hash") != material_hash
    caps = props.get("chat_template_caps")
    if not isinstance(caps, dict):
        # The capability decides whether a replayed call's arguments reach the
        # template as an object. Without it they stay strings, and a template
        # with arguments|items would 409 with no other hint.
        _feyntalk_log_once(cache_key + "|nocaps",
                           f"feyntalk: {name} /props has no chat_template_caps; "
                           "tool-call arguments stay strings")
    result = {"mode": "tokens", "hash": material_hash,
              "name": name, "host": host, "port": port,
              "material": known["material"], "renderer": known["renderer"],
              "template_source": material["chat_template"].get("source"),
              # The engine's minja capability: a template that iterates the
              # call arguments wants an object, not the JSON string.
              "object_arguments": bool(isinstance(caps, dict)
                                       and caps.get("supports_object_arguments")),
              # preserve_thinking: nur wenn die Vorlage preserve_thinking liest, darf der
              # Gateway die Flagge senden (sonst byte-gleich wie zuvor).
              "supports_preserve_thinking": bool(
                  isinstance(caps, dict) and caps.get("supports_preserve_thinking")),
              "n_ctx": _feyntalk_n_ctx(props),
              "checked": now}
    if changed:
        log(f"feyntalk: {name} token mode, material {material_hash} "
            f"({result['template_source']})")
    _feyntalk_store(result, cache_key)
    return result


def _chat_content_string(content):
    """The exact ChatMessage.content: string, or text parts joined without a
    separator (core/chat.cpp read_content). Images are refused in token mode."""
    if content is None:
        return None
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for part in content:
            if not isinstance(part, dict) or not isinstance(part.get("type"), str):
                raise _FeyntalkRefusal("content part is not a typed object")
            kind = part["type"]
            if kind in ("text", "input_text", "output_text"):
                text = part.get("text")
                if not isinstance(text, str):
                    raise _FeyntalkRefusal("text part has no text")
                out.append(text)
            elif kind == "image_url":
                raise _FeyntalkRefusal("images are not served in token mode")
            else:
                raise _FeyntalkRefusal(f"content part {kind!r} is not served")
        return "".join(out)
    raise _FeyntalkRefusal("content must be a string, an array or null")


def _responses_content_string(content):
    """What the engine's read_message_content builds: parts joined with newlines."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for part in content:
            if not isinstance(part, dict) or not isinstance(part.get("type"), str):
                raise _FeyntalkRefusal("content part is not a typed object")
            kind = part["type"]
            if kind == "input_image":
                raise _FeyntalkRefusal("images are not served in token mode")
            if kind not in ("input_text", "output_text", "text"):
                raise _FeyntalkRefusal(f"content part {kind!r} is not served")
            text = part.get("text")
            if not isinstance(text, str):
                raise _FeyntalkRefusal("content part has no text")
            out.append(text)
        return "\n".join(out)
    raise _FeyntalkRefusal("content must be a string, an array or null")


def _reasoning_text(item):
    content = item.get("content")
    if content is None:
        return ""
    if not isinstance(content, list):
        raise _FeyntalkRefusal("reasoning.content must be an array")
    out = []
    for part in content:
        if not isinstance(part, dict) or not isinstance(part.get("type"), str):
            raise _FeyntalkRefusal("reasoning part is not a typed object")
        if part["type"] not in ("reasoning_text", "text"):
            raise _FeyntalkRefusal("reasoning part is not reasoning_text")
        text = part.get("text")
        if not isinstance(text, str):
            raise _FeyntalkRefusal("reasoning part has no text")
        out.append(text)
    return "\n".join(out)


def _tool_arguments_text(value):
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


# nlohmann::json's number model: an integer literal is int64 when it fits,
# uint64 when it fits only that, and the nearest double otherwise. The bounds
# are closed: -2**63 and 2**64-1 stay integers.
_NLOHMANN_INT_MIN = -(2 ** 63)
_NLOHMANN_UINT_MAX = 2 ** 64 - 1


def _nlohmann_reject_constant(name):
    """json.loads' parse_constant: nlohmann refuses NaN/Infinity/-Infinity."""
    raise ValueError(f"nlohmann does not parse the constant {name}")


def _nlohmann_float_literal(text):
    """json.loads' parse_float: a number that overflows a double is refused.

    nlohmann parses the literal as a double; when that rounds to infinity the
    whole document is discarded and the engine keeps the raw string. Underflow
    is fine (it rounds to 0.0)."""
    value = float(text)
    if math.isinf(value):
        raise ValueError(f"nlohmann refuses a number beyond double range: {text}")
    return value


def _nlohmann_int_literal(text):
    """json.loads' parse_int: nlohmann's int64/uint64/double model, measured.

    An integer literal is int64 when it fits, uint64 when it fits only that,
    and otherwise the nearest double (2**64 -> 1.8446744073709552e+19). A
    literal beyond the double range is refused like an overflowing float."""
    value = int(text)
    if _NLOHMANN_INT_MIN <= value <= _NLOHMANN_UINT_MAX:
        return value
    try:
        as_float = float(text)
    except OverflowError:
        raise ValueError(f"nlohmann refuses a number beyond double range: {text}") from None
    if math.isinf(as_float):
        raise ValueError(f"nlohmann refuses a number beyond double range: {text}")
    return as_float


def _json_sorted(value):
    """nlohmann::json's object order and number model for the render input.

    The engine builds its template context from `nlohmann::json`, whose objects
    are `std::map`: every level's keys are sorted alphabetically, recursively,
    and minja's `tojson` keeps that order. Python dicts keep insertion order,
    so a tool or arguments object must be rebuilt in that order here, or the
    rendered prompt (and its sha256) differs from the engine's. Arrays keep
    their order. An integer outside int64/uint64 is the nearest double, as
    nlohmann stores it."""
    if isinstance(value, bool):
        return value
    if isinstance(value, dict):
        return {key: _json_sorted(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [_json_sorted(item) for item in value]
    if isinstance(value, int):
        if _NLOHMANN_INT_MIN <= value <= _NLOHMANN_UINT_MAX:
            return value
        try:
            return float(value)
        except OverflowError:  # beyond double: not a measured case; stay exact
            return value
    return value


def _arguments_for_render(arguments, object_arguments):
    """The template's `tool_call.arguments`, as chat_messages_json builds it.

    A template that iterates the arguments (minja's `requires_object_arguments`,
    `arguments|items`) gets the parsed object, anything else the raw string
    (core/chat.cpp tool_call_arguments_for_template). The parsed object is
    sorted recursively and follows nlohmann's number model; a document
    nlohmann refuses (NaN/Infinity) stays the raw string too."""
    if not object_arguments or not isinstance(arguments, str):
        return arguments
    try:
        parsed = json.loads(arguments, parse_constant=_nlohmann_reject_constant,
                            parse_float=_nlohmann_float_literal,
                            parse_int=_nlohmann_int_literal)
    except ValueError:
        return arguments
    if not isinstance(parsed, dict):
        return arguments
    return _json_sorted(parsed)


def _render_tool_arguments(messages, object_arguments):
    """Apply the engine's arguments mapping to freshly built render messages."""
    if not object_arguments:
        return messages
    for message in messages:
        for call in message.get("tool_calls") or []:
            fn = call.get("function") if isinstance(call.get("function"), dict) else None
            if fn is not None and isinstance(fn.get("arguments"), str):
                fn["arguments"] = _arguments_for_render(fn["arguments"], True)
    return messages


def _chat_messages_to_input(messages):
    """Chat Completions messages -> the Responses input items that rebuild the
    same ChatMessages in src/api/responses.cpp read_item."""
    if not isinstance(messages, list):
        raise _FeyntalkRefusal("messages must be an array")
    items = []
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            raise _FeyntalkRefusal(f"messages[{index}] is not an object")
        role = message.get("role")
        if role == "tool":
            call_id = message.get("tool_call_id")
            if not isinstance(call_id, str) or not call_id:
                raise _FeyntalkRefusal(f"messages[{index}]: tool message needs tool_call_id")
            content = _chat_content_string(message.get("content"))
            items.append({"type": "function_call_output", "call_id": call_id,
                          "output": content if content is not None else ""})
            continue
        if role not in ("system", "developer", "user", "assistant"):
            raise _FeyntalkRefusal(f"messages[{index}]: role {role!r} is not served")
        if message.get("name"):
            # read_item has no name field for a message item; keeping it would
            # change the render's messages.
            raise _FeyntalkRefusal(f"messages[{index}]: a message name cannot be carried")
        reasoning = message.get("reasoning_content")
        if reasoning is not None and not isinstance(reasoning, str):
            raise _FeyntalkRefusal(f"messages[{index}]: reasoning_content must be a string")
        if reasoning and role != "assistant":
            raise _FeyntalkRefusal(f"messages[{index}]: reasoning on role {role!r}")
        calls = message.get("tool_calls") or []
        content = _chat_content_string(message.get("content"))
        if calls:
            if role != "assistant":
                raise _FeyntalkRefusal(f"messages[{index}]: tool_calls on role {role!r}")
            if not isinstance(calls, list):
                raise _FeyntalkRefusal(f"messages[{index}]: tool_calls must be an array")
            # arcint's design record: the engine merges a run of
            # [reasoning]? [assistant message]? [function_call]+ into ONE
            # assistant turn, so the inverse shape is that order, however many
            # calls the turn carries.
            if reasoning:
                items.append({"type": "reasoning",
                              "content": [{"type": "reasoning_text", "text": reasoning}]})
            # chat_messages_json renders content as null when a turn has
            # tool_calls and empty content, so no message item is emitted for
            # an empty content here either (the engine's merge then sees none).
            if content is not None and content != "":
                items.append({"type": "message", "role": "assistant", "content": content})
            seen = set()
            for call_index, call in enumerate(calls):
                if not isinstance(call, dict):
                    raise _FeyntalkRefusal(
                        f"messages[{index}]: tool_calls[{call_index}] is not an object")
                fn = call.get("function") if isinstance(call.get("function"), dict) else {}
                call_id = call.get("id")
                name = fn.get("name")
                if not isinstance(call_id, str) or not call_id or not isinstance(name, str) or not name:
                    raise _FeyntalkRefusal(f"messages[{index}]: tool call needs id and name")
                if call_id in seen:
                    raise _FeyntalkRefusal(
                        f"messages[{index}]: duplicate call_id {call_id!r} in one assistant turn")
                seen.add(call_id)
                items.append({"type": "function_call", "call_id": call_id, "name": name,
                              "arguments": _tool_arguments_text(fn.get("arguments", "{}"))})
            continue
        if reasoning:
            items.append({"type": "reasoning",
                          "content": [{"type": "reasoning_text", "text": reasoning}]})
        item = {"type": "message", "role": role}
        if content is not None:
            item["content"] = content
        items.append(item)
    return items


def _responses_messages_from_input(input_value, instructions=None):
    """The inverse: Responses input -> the messages chat_messages_json renders.

    Mirrors src/api/responses.cpp AssistantRun (arcint's design record,
    parallel tool calls): a run of [reasoning]? [assistant message]? [function_call]+ is ONE
    assistant message; a reasoning item restarts the run but keeps the reasoning
    it already carried; a boundary item (a user/developer/system message or a
    function_call_output) closes the run and clears the reasoning.
    """
    messages = []
    pending = ""
    open_run = False

    def add_reasoning(text):
        nonlocal pending, open_run
        if text:
            pending = (pending + "\n" + text) if pending else text
        open_run = False

    def add_message(message):
        nonlocal pending, open_run
        if pending:
            message["reasoning_content"] = pending
        pending = ""
        messages.append(message)
        open_run = True

    def add_call(call):
        nonlocal pending, open_run
        if open_run and messages and messages[-1].get("role") == "assistant":
            messages[-1].setdefault("tool_calls", []).append(call)
            # chat_messages_json (src/exec/chat_json.h) writes content = null
            # for a turn with tool_calls and empty content; the render side
            # must produce that shape, not "".
            if messages[-1].get("content") == "":
                messages[-1]["content"] = None
        else:
            message = {"role": "assistant", "content": None}
            if pending:
                message["reasoning_content"] = pending
            pending = ""
            message["tool_calls"] = [call]
            messages.append(message)
            open_run = True

    def close():
        nonlocal pending, open_run
        open_run = False
        pending = ""

    if input_value is None:
        return messages
    if isinstance(input_value, str):
        messages.append({"role": "user", "content": input_value})
        return messages
    if not isinstance(input_value, list):
        raise _FeyntalkRefusal("input must be a string, an array or null")
    for index, item in enumerate(input_value):
        if not isinstance(item, dict):
            raise _FeyntalkRefusal(f"input[{index}] is not an object")
        kind = item.get("type")
        if kind is None and isinstance(item.get("role"), str):
            kind = "message"
        if kind == "message":
            role = item.get("role")
            if role not in ("system", "developer", "user", "assistant"):
                raise _FeyntalkRefusal(f"input[{index}]: role {role!r} is not a message role")
            content = _responses_content_string(item.get("content"))
            message = {"role": role, "content": content}
            if role == "assistant":
                add_message(message)
            else:
                close()
                messages.append(message)
            continue
        if kind == "function_call":
            call_id = item.get("call_id")
            name = item.get("name")
            arguments = item.get("arguments")
            if not isinstance(call_id, str) or not call_id or not isinstance(name, str) or not name:
                raise _FeyntalkRefusal(f"input[{index}]: function_call needs call_id and name")
            if not isinstance(arguments, str):
                raise _FeyntalkRefusal(f"input[{index}]: function_call arguments must be a string")
            if open_run and messages and messages[-1].get("role") == "assistant":
                if any(c["id"] == call_id for c in messages[-1].get("tool_calls", [])):
                    raise _FeyntalkRefusal(
                        f"input[{index}]: duplicate call_id {call_id!r} in one assistant turn")
            add_call({"id": call_id, "type": "function",
                      "function": {"name": name, "arguments": arguments}})
            continue
        if kind == "function_call_output":
            call_id = item.get("call_id")
            if not isinstance(call_id, str) or not call_id:
                raise _FeyntalkRefusal(f"input[{index}]: function_call_output needs call_id")
            close()
            output = item.get("output")
            if isinstance(output, str):
                content = output
            elif isinstance(output, list):
                parts = []
                for part in output:
                    if not isinstance(part, dict) or not isinstance(part.get("type"), str):
                        raise _FeyntalkRefusal(f"input[{index}]: output part is not typed")
                    if part["type"] != "input_text" or not isinstance(part.get("text"), str):
                        raise _FeyntalkRefusal(f"input[{index}]: output part is not text")
                    parts.append(part["text"])
                content = "\n".join(parts)
            else:
                raise _FeyntalkRefusal(f"input[{index}]: output must be a string or an array")
            messages.append({"role": "tool", "tool_call_id": call_id, "content": content})
            continue
        if kind == "reasoning":
            add_reasoning(_reasoning_text(item))
            continue
        close()
        raise _FeyntalkRefusal(f"input[{index}]: item type {kind!r} is not served")
    return messages


def _chat_tools_for_render(tools):
    """Chat OpenAI tools -> chat_messages_json's tools shape."""
    if not isinstance(tools, list) or not tools:
        return None
    out = []
    for index, tool in enumerate(tools):
        if not isinstance(tool, dict):
            raise _FeyntalkRefusal(f"tools[{index}] is not an object")
        fn = tool.get("function") if isinstance(tool.get("function"), dict) else None
        if fn is None:
            raise _FeyntalkRefusal(f"tools[{index}] has no function object")
        name = fn.get("name")
        if not isinstance(name, str) or not name:
            raise _FeyntalkRefusal(f"tools[{index}] has no name")
        out.append({"type": "function",
                    "function": {"name": name,
                                 "description": fn.get("description") if isinstance(fn.get("description"), str) else "",
                                 "parameters": fn.get("parameters") if isinstance(fn.get("parameters"), dict) else {}}})
    return _json_sorted(out)


def _responses_tools_for_render(tools):
    """Responses flat tools -> chat_messages_json's tools shape."""
    if not isinstance(tools, list) or not tools:
        return None
    out = []
    for index, tool in enumerate(tools):
        if not isinstance(tool, dict):
            raise _FeyntalkRefusal(f"tools[{index}] is not an object")
        if tool.get("type") != "function":
            raise _FeyntalkRefusal(f"tools[{index}] type {tool.get('type')!r} is not served")
        name = tool.get("name")
        if not isinstance(name, str) or not name:
            raise _FeyntalkRefusal(f"tools[{index}] has no name")
        out.append({"type": "function",
                    "function": {"name": name,
                                 "description": tool.get("description") if isinstance(tool.get("description"), str) else "",
                                 "parameters": tool.get("parameters") if isinstance(tool.get("parameters"), dict) else {}}})
    return _json_sorted(out)


def _tools_to_responses(tools):
    if not isinstance(tools, list):
        return None
    out = []
    for tool in tools:
        fn = tool.get("function") if isinstance(tool, dict) else None
        if not isinstance(fn, dict):
            continue
        item = {"type": "function", "name": fn.get("name")}
        if isinstance(fn.get("description"), str):
            item["description"] = fn["description"]
        if isinstance(fn.get("parameters"), dict):
            item["parameters"] = fn["parameters"]
        out.append(item)
    return out


def _tool_choice_to_responses(choice):
    if choice is None:
        return None
    if isinstance(choice, str):
        return choice
    if isinstance(choice, dict):
        if choice.get("type") == "function":
            name = choice.get("name") or (choice.get("function") or {}).get("name")
            if isinstance(name, str) and name:
                return {"type": "function", "name": name}
    raise _FeyntalkRefusal("tool_choice cannot be carried")


def _feyntalk_enable(model, body, chat):
    """Enable-thinking for the render, and the field that carries it upstream.

    Chat: reasoning_effort, with chat_template_kwargs.enable_thinking as the switch
    over it; else that switch alone, else the local reasoning capability
    the proxy already applies to Chat requests. Responses: reasoning.effort only.
    """
    if chat:
        kwargs = body.get("chat_template_kwargs")
        kw = kwargs.get("enable_thinking") if isinstance(kwargs, dict) else None
        # _proxy has already applied the per-model effort map and may have added
        # enable_thinking itself, so the level has to be read before the switch: the
        # switch only says on or off. As in the engine, an explicit switch wins.
        effort = body.get("reasoning_effort")
        if isinstance(effort, str):
            off = effort in ("none", "off")
            if kw is False or (off and kw is not True):
                return False, "none"
            if off:
                # Switch on, level off: name neither, so both renders leave the choice to
                # the template's own default.
                return None, None
            return True, effort
        if isinstance(kw, bool):
            return kw, ("medium" if kw else "none")
        if _local_reasoning(model):
            return True, "medium"
        return None, None
    reasoning = body.get("reasoning")
    if isinstance(reasoning, dict) and isinstance(reasoning.get("effort"), str):
        effort = reasoning["effort"]
        if effort in ("none", "off"):
            return False, "none"
        return True, effort
    return None, None


def _template_quotes(src, token):
    return f"'{token}'" in src or f'"{token}"' in src


def _feyntalk_template_effort(effort, cap):
    """The level the template gets, as the engine computes it from the same text
    (template_reasoning_effort in arcint's src/core/chat.cpp), or None for no level.
    A render without it hashes differently on any template that reads the variable."""
    material = cap.get("material") if isinstance(cap, dict) else None
    template = material.get("chat_template") if isinstance(material, dict) else None
    src = template.get("text") if isinstance(template, dict) else None
    if not isinstance(effort, str) or not isinstance(src, str) or "reasoning_effort" not in src:
        return None
    level = "low" if effort == "minimal" else effort
    if level not in ("low", "medium", "high", "xhigh"):
        return None
    if _template_quotes(src, level):
        return level
    if level == "high" and _template_quotes(src, "xhigh"):
        return "xhigh"
    if level == "xhigh":
        return "high"
    return level


def _feyntalk_render(cap, messages, tools, enable, preserve=None, effort=None):
    extra = {}
    if enable is not None:
        extra["enable_thinking"] = enable
    level = _feyntalk_template_effort(effort, cap)
    if level is not None:
        extra["reasoning_effort"] = level
    if preserve is not None:
        extra["preserve_thinking"] = bool(preserve)
    return cap["renderer"].render(messages, tools=tools, add_generation_prompt=True,
                                  extra_context=extra)


def _feyntalk_arcint(cap, rendered, preserve=None):
    out = {"v": 1, "token_mode": True, "renderer_hash": cap["hash"],
           "render": {"prompt_sha256": rendered.prompt_sha256}}
    if preserve is not None:
        out["preserve_thinking"] = bool(preserve)
    return out


def _feyntalk_client_preserve_chat(request):
    """chat_template_kwargs.preserve_thinking, bool oder None.

    Ein Nicht-bool ist ein Client-Fehler (400), nicht etwas, das man
    stillschweigend weglaesst."""
    kwargs = request.get("chat_template_kwargs")
    if not isinstance(kwargs, dict) or "preserve_thinking" not in kwargs:
        return None
    value = kwargs["preserve_thinking"]
    if not isinstance(value, bool):
        raise _FeyntalkBadRequest("chat_template_kwargs.preserve_thinking must be a boolean")
    return value


def _feyntalk_client_preserve_responses(request):
    """arcint.preserve_thinking aus dem Client-Body, bool oder None."""
    arcint = request.get("arcint")
    if not isinstance(arcint, dict) or "preserve_thinking" not in arcint:
        return None
    value = arcint["preserve_thinking"]
    if not isinstance(value, bool):
        raise _FeyntalkBadRequest("arcint.preserve_thinking must be a boolean")
    return value


def _feyntalk_exact_tokens(cap, rendered, auth):
    """Der exakte Prompt-Zaehler der Engine (POST /tokenize), (count|None, reason).

    Der behaltene Render ist bereits die exakte Prompt-Zeichenkette; die Engine
    zaehlt sie mit ihrem eigenen Tokenizer. `content` ist der llama-server-nahe
    Rumpf, die Antwort traegt die Zahl (auch als `count`; aeltere Engines geben
    nur `tokens`, dann ist dessen Laenge die Zahl). None heisst: die Schaetzung
    bleibt; ein Zaehler ist nie ein Fehlergrund fuer die Anfrage."""
    host, port = cap.get("host"), cap.get("port")
    if not host or not port:
        return None, "no backend address"
    conn = None
    try:
        raw = json.dumps({"content": rendered.text}).encode("utf-8")
        headers = {"Content-Type": "application/json", "Content-Length": str(len(raw))}
        if auth:
            headers["Authorization"] = auth
        conn = http.client.HTTPConnection(host, port, timeout=FEYNTALK_TOKENIZE_TIMEOUT)
        conn.request("POST", "/tokenize", body=raw, headers=headers)
        resp = conn.getresponse()
        payload = resp.read()
        if resp.status != 200:
            return None, f"status {resp.status}"
        doc = json.loads(payload)
        count = doc.get("count")
        if not isinstance(count, int) or isinstance(count, bool):
            tokens = doc.get("tokens")
            count = len(tokens) if isinstance(tokens, list) else None
        if not isinstance(count, int) or count < 0:
            return None, "no count in the answer"
        return count, None
    except Exception as e:  # noqa: BLE001 - der Zaehler darf die Anfrage nie kippen
        return None, f"{type(e).__name__}: {e}"
    finally:
        try:
            if conn:
                conn.close()
        except Exception:
            pass


def _feyntalk_think_policy(cap, messages, tools, enable, override, model, auth=None, effort=None):
    """Denken behalten oder als erste Kompaktierungsstufe fallen lassen.

    Rueckgabe (preserve, rendered, mode): preserve True/False/None (None = die
    Faehigkeit fehlt, es wird keine Flagge gesendet), rendered der zu sendende
    Render, mode kept|dropped|off (bei automatischer Entscheidung mit der
    Herkunft der Zahl: kept(exact)/kept(estimate)/...). Die Entscheidung faellt
    auf der Groesse des preserve=true-Renders; der exakte Zaehler ist pro Prompt
    deterministisch, und wenn er einmal ausfaellt, wird die Schaetzung mit dem
    zuletzt beobachteten exact/Schaetzung-Verhaeltnis dieses Backends skaliert --
    so bleibt eine einmal getroffene Entscheidung stabil, statt an einem
    einzelnen Fehlschlag zu kippen."""
    if enable is False or not cap.get("supports_preserve_thinking"):
        return None, _feyntalk_render(cap, messages, tools, enable, effort=effort), "off"
    if override is not None:
        rendered = _feyntalk_render(cap, messages, tools, enable, override, effort=effort)
        return override, rendered, ("kept" if override else "dropped")
    rendered = _feyntalk_render(cap, messages, tools, enable, True, effort=effort)
    n_ctx = cap.get("n_ctx")
    if not isinstance(n_ctx, int) or n_ctx <= 0:
        _feyntalk_log_once(cap["hash"] + "|noctx",
                           f"feyntalk: {model} keep thinking: /props reports no "
                           "context length; never dropping")
        return True, rendered, "kept"
    est = len(rendered.text) / FEYNTALK_CHARS_PER_TOKEN
    limit = n_ctx * FEYNTALK_THINK_DROP_FRACTION
    name = cap.get("name", cap["hash"])
    count, source = est, "estimate"
    if est >= limit * FEYNTALK_TOKENIZE_PRE_FRACTION:
        # Die Schaetzung ist nah genug an der Schwelle, dass chars/3.0 sie
        # verfehlen kann: die Engine exakt zaehlen lassen und auf DIESER Zahl
        # entscheiden. Faellt sie aus, wird die Schaetzung mit dem letzten
        # exact/Schaetzung-Verhaeltnis dieses Backends skaliert; ohne ein
        # solches Verhaeltnis bleibt es bei der rohen Schaetzung (eine Zeile je
        # Backend, nie ein Fehler). Das Verhaeltnis haengt am Material-Hash, nicht
        # am Backend-Namen: nach einem Modellwechsel unter demselben Namen gilt das
        # alte Tokenizer-Verhaeltnis nicht mehr.
        exact, why = _feyntalk_exact_tokens(cap, rendered, auth)
        if exact is not None:
            count, source = float(exact), "exact"
            if est > 0:
                _FEYNTALK_TOKEN_RATIO[cap["hash"]] = exact / est
        else:
            ratio = _FEYNTALK_TOKEN_RATIO.get(cap["hash"])
            if ratio is not None:
                count, source = est * ratio, "estimate, scaled"
            _feyntalk_log_once(name + "|tokenize",
                               f"feyntalk: {model} exact token count unavailable "
                               f"({why}); using the "
                               + ("scaled estimate" if ratio is not None else "estimate"))
    if count > limit:
        dropped = _feyntalk_render(cap, messages, tools, enable, False, effort=effort)
        gezaehlt = (f"{count:.0f} tokens ({source})" if source == "exact"
                    else f"~{count:.0f} tokens ({source})")
        _feyntalk_log_once(cap["hash"] + "|drop",
                           f"feyntalk: {model} think dropped: {gezaehlt} > "
                           f"{limit:.0f} ({n_ctx} ctx x {FEYNTALK_THINK_DROP_FRACTION}); "
                           "stage-1 compaction")
        return False, dropped, "dropped(" + source + ")"
    return True, rendered, "kept(" + source + ")"


def _feyntalk_retry_body(body, preserve, chat):
    """Der Text-Wiederholungsrumpf nach 409 traegt dieselbe Flagge.

    Chat liest preserve_thinking ueber chat_template_kwargs, die Responses-
    Flaeche ueber arcint.preserve_thinking (sie lehnt unbekannte Top-Level-
    Felder ab)."""
    if preserve is None:
        return body
    request = json.loads(body)
    if chat:
        kwargs = request.get("chat_template_kwargs")
        if not isinstance(kwargs, dict):
            kwargs = {}
            request["chat_template_kwargs"] = kwargs
        kwargs["preserve_thinking"] = bool(preserve)
    else:
        arcint = request.get("arcint")
        if not isinstance(arcint, dict):
            arcint = {}
            request["arcint"] = arcint
        arcint["preserve_thinking"] = bool(preserve)
    return json.dumps(request).encode()


def _feyntalk_chat_request(body, cap, model, client_stream, auth=None):
    """A Chat Completions body -> the engine's Open Responses request."""
    request = json.loads(body)
    if request.get("n") not in (None, 1):
        raise _FeyntalkRefusal("n > 1 is not served in token mode")
    if request.get("stop") not in (None, [], ""):
        raise _FeyntalkRefusal("stop strings are not served in token mode")
    messages = request.get("messages")
    if not isinstance(messages, list) or not messages:
        raise _FeyntalkRefusal("messages are missing")
    input_items = _chat_messages_to_input(messages)
    render_messages = _render_tool_arguments(_responses_messages_from_input(input_items),
                                             cap.get("object_arguments"))
    tools_render = _chat_tools_for_render(request.get("tools"))
    enable, effort = _feyntalk_enable(model, request, chat=True)
    override = _feyntalk_client_preserve_chat(request)
    preserve, rendered, think = _feyntalk_think_policy(
        cap, render_messages, tools_render, enable, override, model,
        auth=auth, effort=effort)

    out = {"model": model, "input": input_items, "stream": True,
           "arcint": _feyntalk_arcint(cap, rendered, preserve)}
    for key in ("temperature", "top_p", "presence_penalty", "frequency_penalty"):
        if request.get(key) is not None:
            out[key] = request[key]
    max_tokens = request.get("max_tokens")
    if max_tokens is None:
        max_tokens = request.get("max_completion_tokens")
    if max_tokens is not None:
        if not isinstance(max_tokens, int) or max_tokens < 16:
            raise _FeyntalkRefusal("max_tokens below the Responses minimum of 16")
        out["max_output_tokens"] = max_tokens
    if request.get("tools") is not None:
        out["tools"] = _tools_to_responses(request.get("tools"))
    if request.get("tool_choice") is not None:
        out["tool_choice"] = _tool_choice_to_responses(request.get("tool_choice"))
    if request.get("parallel_tool_calls") is not None:
        out["parallel_tool_calls"] = request["parallel_tool_calls"]
    if effort is not None:
        out["reasoning"] = {"effort": effort}
    if request.get("truncation") is not None:
        out["truncation"] = request["truncation"]
    return {"path": "/v1/responses", "body": json.dumps(out).encode(),
            "kind": "chat", "model": model, "stream": client_stream,
            "material": cap["material"], "tools": request.get("tools"),
            "think": think, "retry_body": _feyntalk_retry_body(body, preserve, True)}


def _feyntalk_responses_request(body, cap, model, auth=None):
    """A Responses body -> the same body with token mode and both hashes."""
    request = json.loads(body)
    if request.get("stream") is not True:
        raise _FeyntalkRefusal("token mode is stream-only on the Responses route")
    if request.get("previous_response_id") is not None:
        raise _FeyntalkRefusal("previous_response_id continuation is not renderable here")
    render_messages = _render_tool_arguments(
        _responses_messages_from_input(request.get("input"), request.get("instructions")),
        cap.get("object_arguments"))
    tools_render = _responses_tools_for_render(request.get("tools"))
    enable, effort = _feyntalk_enable(model, request, chat=False)
    override = _feyntalk_client_preserve_responses(request)
    preserve, rendered, think = _feyntalk_think_policy(
        cap, render_messages, tools_render, enable, override, model,
        auth=auth, effort=effort)

    out = dict(request)
    out["stream"] = True
    out["arcint"] = _feyntalk_arcint(cap, rendered, preserve)
    return {"path": "/v1/responses", "body": json.dumps(out).encode(),
            "kind": "responses", "model": model, "stream": True,
            "material": cap["material"], "tools": request.get("tools"),
            "think": think, "retry_body": _feyntalk_retry_body(body, preserve, False)}


def _feyntalk_model_of(body):
    try:
        request = json.loads(body)
    except Exception:
        return ""
    model = request.get("model")
    return model if isinstance(model, str) else ""


def _local_thinking_switch(j):
    """Text path: set enable_thinking for a reasoning model unless the client did.
    It follows reasoning_effort, because the engine lets this switch win over the effort.
    Returns True if the request changed."""
    ck = j.get("chat_template_kwargs")
    ck = ck if isinstance(ck, dict) else {}
    if "enable_thinking" in ck:
        return False
    ck["enable_thinking"] = j.get("reasoning_effort") not in ("none", "off")
    j["chat_template_kwargs"] = ck
    return True


def _feyntalk_prepare(backend, method, path, body, feyntalk=None, auth=None):
    """None = the text path, unchanged. A dict = send this token-mode request.

    An alias with feyntalk "tokens" (the `-ft` names) has no text path: whatever would
    send it there raises _FeyntalkStrict instead.

    `feyntalk` overrides the backend's own setting for this request (the alias's
    mode); None keeps the backend setting. `auth` ist die Authorization des
    Anrufers, wie sie auch an die Engine-Routen geht -- /tokenize liegt hinter
    demselben Schluessel und bekommt darum dieselbe Kopfzeile."""
    name = backend[0]
    strict = feyntalk == "tokens"
    if method != "POST" or (_feyntalk_setting(name) if feyntalk is None else feyntalk) == "off":
        return None
    cap = _feyntalk_capability(backend, setting=feyntalk)
    if strict and cap.get("mode") != "tokens":
        # A cached "no token mode" can outlive a unit restart; ask again before failing.
        _feyntalk_drop_cache(name)
        cap = _feyntalk_capability(backend, setting=feyntalk)
    if cap.get("mode") != "tokens":
        if strict:
            raise _FeyntalkStrict(502, "token_mode_unavailable",
                                  f"backend '{name}' cannot serve token mode: "
                                  f"{cap.get('reason', 'unknown reason')}")
        return None
    model = _feyntalk_model_of(body)
    try:
        if path in RESPONSES_PATHS:
            return _feyntalk_responses_request(body, cap, model, auth=auth)
        if "/chat/completions" in path:
            is_stream = bool(json.loads(body).get("stream"))
            return _feyntalk_chat_request(body, cap, model, is_stream, auth=auth)
        if strict:
            raise _FeyntalkStrict(400, "token_mode_unsupported_request",
                                  f"token mode does not serve {path}")
        return None
    except _FeyntalkRefusal as e:
        if strict:
            raise _FeyntalkStrict(400, "token_mode_unsupported_request", str(e)) from e
        _feyntalk_log_once(name + "|request|" + str(e),
                           f"feyntalk: {name} request stays text ({e})")
        return None
    except (ValueError, _FTRenderError) as e:
        if strict:
            raise _FeyntalkStrict(502, "token_mode_unavailable",
                                  f"backend '{name}' render failed: {type(e).__name__}: {e}") from e
        _feyntalk_log_once(name + "|render|" + str(e),
                           f"feyntalk: {name} render stays text ({type(e).__name__}: {e})")
        return None


def _feyntalk_is_renderer_mismatch(status, body_bytes):
    # Any 409 from the token request is the pre-token refusal the gateway falls
    # back from; the two documented params are the common shape.
    return status == 409


def _chat_chunk(rid, model, created, delta, finish=None):
    return ("data: " + json.dumps({"id": rid, "object": "chat.completion.chunk",
                                   "created": created, "model": model,
                                   "choices": [{"index": 0, "delta": delta,
                                                "finish_reason": finish}]},
                                  ensure_ascii=False) + "\n\n").encode("utf-8")


def _chat_finish_reason(resource, calls):
    if calls:
        return "tool_calls"
    if resource is None or resource.get("status") == "completed":
        return "stop"
    incomplete = resource.get("incomplete_details") or {}
    reason = incomplete.get("reason") if isinstance(incomplete, dict) else None
    return {"max_output_tokens": "length", "loop_guard_abort": "loop",
            "cancelled": "stop"}.get(reason, "stop")


# Die Objekt-Ereignisse des engine-seitigen terminal()-Pfads, in der Reihenfolge,
# in der die Engine sie schickt (src/api/responses_stream.cpp). Ihr Schluessel
# im Chat-Objekt ist der Name hinter `arcint:`.
_CHAT_ARCINT_KINDS = ("arcint:loop_guard", "arcint:context", "arcint:truncated")


def _chat_arcint_fields(event):
    """Die Felder eines Objekt-Ereignisses, ohne seinen Diskriminator.

    Nur die Felder, die wirklich kamen: ein Zug ohne `token_index` bekommt
    keinen, ein Zug ohne das Ereignis gar keinen Schluessel.
    """
    if not isinstance(event, dict):
        return None
    out = {k: v for k, v in event.items() if k != "type"}
    return out or None


def _chat_arcint_object(events):
    """Das `arcint`-Objekt eines Chat-Terminals aus den gesammelten Ereignissen.

    `events` ist Ereignisart -> rohes Ereignis. Die Engine schickt die drei
    Objekt-Ereignisse einmal je Zug, nach den Spans und vor dem Terminal
    (src/api/responses_stream.cpp, terminal()); ein Chat-Client liest sie als
    `arcint: {context|loop_guard|truncated: {...}}` neben `usage`. Reihenfolge
    und Inhalt folgen der Engine. Kein Ereignis: kein Objekt, damit der Rahmen
    ohne Waechter byte-gleich bleibt.
    """
    out = {}
    for kind in _CHAT_ARCINT_KINDS:
        fields = _chat_arcint_fields(events.get(kind))
        if fields:
            out[kind.split(":", 1)[1]] = fields
    return out or None


def _chat_usage(resource):
    usage = resource.get("usage") if isinstance(resource, dict) else None
    if not isinstance(usage, dict):
        return None
    out = {}
    for src, dst in (("input_tokens", "prompt_tokens"), ("output_tokens", "completion_tokens"),
                     ("total_tokens", "total_tokens")):
        if isinstance(usage.get(src), int):
            out[dst] = usage[src]
    return out or None


def _chat_tool_call_ids(output_items):
    ids = []
    for item in output_items if isinstance(output_items, list) else []:
        if isinstance(item, dict) and item.get("type") == "function_call":
            ids.append(item.get("call_id"))
    return ids


def _chat_tool_call_frames(calls, output_items, rid, model, created):
    frames = []
    ids = _chat_tool_call_ids(output_items)
    entries = []
    for index, call in enumerate(calls):
        call_id = ids[index] if index < len(ids) and isinstance(ids[index], str) else "call_%d" % index
        entries.append({"index": index, "id": call_id, "type": "function",
                        "function": {"name": call.name, "arguments": call.arguments}})
    if entries:
        frames.append(_chat_chunk(rid, model, created, {"tool_calls": entries}))
    return frames


def _ctx_from_props(props):
    """Context length out of a /props document, or None.

    Two shapes in the fleet, and neither puts the number where /v1/models would:

      llama.cpp   default_generation_settings.n_ctx
      arcint      model.n_ctx

    This is what keeps a backend off the static override list. An OpenAI-shaped
    /v1/models entry carries no context field at all, and a proxy that guesses
    there sends clients into HTTP 400 at a boundary nobody advertised.
    """
    if not isinstance(props, dict):
        return None
    for schluessel in ("default_generation_settings", "model"):
        teil = props.get(schluessel)
        if isinstance(teil, dict):
            c = _ctx_of(teil)
            if c:
                return c
    return _ctx_of(props)


def _fetch_backend_models(backend):
    name, host, port = backend
    conn = None
    try:
        conn = http.client.HTTPConnection(host, port, timeout=CONNECT_TIMEOUT)
        conn.request("GET", "/v1/models")
        resp = conn.getresponse()
        if resp.status != 200:
            return None
        data = json.loads(resp.read())
        ids = []
        gefunden = {}
        meta = {}
        for quelle in ("data", "models"):
            for m in data.get(quelle, []):
                if not isinstance(m, dict):
                    continue
                mid = m.get("id") or m.get("name") or m.get("model")
                if not mid or mid in ids:
                    continue
                ids.append(mid)
                c = _ctx_of(m) or LOCAL_CTX_OVERRIDES.get(mid)
                if c:
                    gefunden[mid] = c
                # Das ganze Objekt aufheben, nicht nur den Kontext: was das
                # Backend ueber sein Modell weiss, soll die Klienten erreichen.
                meta[mid] = m
        # Capabilities for backends that keep them out of /v1/models (llama.cpp).
        # Only asked when no entry mentioned supported_parameters, so a vLLM-style
        # backend costs no extra round trip.
        stumm = [
            i for i in ids
            if not (isinstance(meta.get(i), dict) and "supported_parameters" in meta[i])
        ]
        # Same for the context length: OpenArc and arcint both answer /v1/models
        # with a bare model object, so the number is only in /props. Without this
        # a client sees ctx=None and either guesses or refuses to size a request.
        ohne_ctx = [i for i in ids if i not in gefunden]
        if stumm or ohne_ctx:
            props = _fetch_backend_props(host, port)
            caps = props.get("chat_template_caps") if isinstance(props, dict) else None
            if isinstance(caps, dict):
                # Only the ids that stayed silent, and a copy each: one shared dict
                # handed to every entry is a footgun the day something edits it.
                for i in stumm:
                    entry = meta.setdefault(i, {})
                    if isinstance(entry, dict):
                        entry.setdefault("chat_template_caps", dict(caps))
            props_ctx = _ctx_from_props(props)
            if props_ctx:
                for i in ohne_ctx:
                    gefunden[i] = props_ctx
        if gefunden or meta:
            with _local_ctx_lock:
                _local_ctx.update(gefunden)
                _local_meta.update(meta)
        return ids
    except Exception:
        return None
    finally:
        try:
            if conn:
                conn.close()
        except Exception:
            pass


def discover(backend):
    name = backend[0]
    now = time.monotonic()
    frisch = True
    ids = None
    with _disc_lock:
        cached = _disc_cache.get(name)
        if cached and cached[0] > now:
            ids, frisch = cached[1], False
    if frisch:
        ids = _fetch_backend_models(backend)
        with _disc_lock:
            _disc_cache[name] = (now + DISCOVERY_TTL, ids)
        if ids is not None:
            with _last_known_lock:
                _last_known[name] = ids
            _save_last_known()
    if ids is not None:
        with _backends_up_lock:
            _backends_up.add(name)
        return ids
    # Backend nicht erreichbar. Der Rueckfall auf den letzten bekannten Stand
    # muss auch fuer den ZWISCHENGESPEICHERTEN Fehlschlag gelten - vorher kehrte
    # der Aufruf oben um und lieferte 30 s lang None, obwohl _last_known die
    # Modelle kannte. Das Netz haengte genau dann nicht, wenn man hineinfiel.
    with _backends_up_lock:
        _backends_up.discard(name)
    with _last_known_lock:
        return _last_known.get(name)


def _fetch_openrouter_models():
    if not OR_KEY:
        return None
    conn = None
    try:
        conn = http.client.HTTPSConnection(
            OPENROUTER_HOST, OPENROUTER_PORT,
            timeout=10, context=ssl.create_default_context(),
        )
        conn.request("GET", "/api/v1/models",
                     headers={"Authorization": f"Bearer {OR_KEY}",
                              "Accept": "application/json"})
        resp = conn.getresponse()
        if resp.status != 200:
            log(f"openrouter discovery: HTTP {resp.status}")
            return None
        data = json.loads(resp.read())
        ids = frozenset(m["id"] for m in data.get("data", []) if "id" in m)
        return ids, data.get("data", [])
    except Exception as e:
        log(f"openrouter discovery FAIL: {type(e).__name__}: {e}")
        return None
    finally:
        try:
            if conn:
                conn.close()
        except Exception:
            pass


def discover_cloud():
    now = time.monotonic()
    with _cloud_lock:
        if _cloud_disc["ids"] is not None and _cloud_disc["expiry"] > now:
            return _cloud_disc["ids"]
    result = _fetch_openrouter_models()
    with _cloud_lock:
        if result is not None:
            ids, _ = result
            _cloud_disc["ids"] = ids
            _cloud_disc["expiry"] = now + CLOUD_DISCOVERY_TTL
        return _cloud_disc["ids"]


def find_backend_for_model(model_id):
    backends = list(LOCAL_BACKENDS)
    # 1. Check if it's a direct match in any backend
    for b in backends:
        ids = discover(b)
        if ids and model_id in ids:
            return b
            
    # 2. Check if it's a friendly name.
    #
    # The map is {"<model-id>": "<label>"}, as the docstring above FRIENDLY_NAMES
    # says -- a label FOR A MODEL ID, not for a backend. This used to look the key
    # up as a backend NAME and additionally require the label to be a substring of
    # the reported alias, which defeats the purpose: a friendly name exists because
    # it is unlike the alias. Measured 2026-08-12: `localcoder` and `localdeepseek`
    # both answered 404 model_not_found under the old form.
    #
    # Attaching the label to the id also removes an ambiguity the old form could
    # not express: backend-a serves both deepseek-v4-flash and -pro, so a label on
    # the backend could not say which of the two it meant.
    #
    # The request body still carries the friendly name to the backend. llama.cpp
    # ignores the model field and answers with what is loaded, so the chosen
    # backend replies correctly; the cost is that fail-over does not engage for a
    # friendly name, because the fail-over test matches the real id against the
    # other backends. No local model is served by two backends today.
    friendly_to_id = {v: k for k, v in FRIENDLY_NAMES.items()}
    if model_id in friendly_to_id:
        target_id = friendly_to_id[model_id]
        for b in backends:
            ids = discover(b)
            if ids and target_id in ids:
                return b
    return None


def _probe_model(api_key, model_id):
    if not api_key:
        return None
    conn = None
    try:
        body = json.dumps({
            "model": model_id,
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 1,
            "stream": False,
        }).encode()
        conn = http.client.HTTPSConnection(
            OPENROUTER_HOST, OPENROUTER_PORT,
            timeout=PROBE_TIMEOUT,
            context=ssl.create_default_context(),
        )
        conn.request("POST", "/api/v1/chat/completions",
                     body=body,
                     headers={
                         "Authorization": f"Bearer {api_key}",
                         "Content-Type": "application/json",
                         "Accept": "application/json",
                     })
        resp = conn.getresponse()
        resp.read()
        if resp.status == 429:
            return False
        if resp.status == 200:
            return True
        return None
    except Exception:
        return None
    finally:
        try:
            if conn:
                conn.close()
        except Exception:
            pass


def _run_catalog_check():
    result = _fetch_openrouter_models()
    if result is None:
        log("check: openrouter fetch failed, skipping cycle")
        return
    _, raw_models = result
    if not raw_models:
        # 200 mit leerer Liste ist ein Ausfall, kein Katalog: abgeleitet wuerde er alle
        # drei Kataloge leeren und jede Wolken-id bis zum naechsten Durchlauf (3 h) zu
        # 'unknown' machen.
        log("check: openrouter lieferte keine Modelle, Durchlauf uebersprungen")
        return

    # --- Free catalog (auto-discovered :free models) ---
    free_ids = {}
    paid_info = {}
    new_metadata = {}
    for m in raw_models:
        # Eine kaputte Zeile kostet diese Zeile, nicht den Thread: bis 1.0.1 beendete
        # ein "prompt": null den Katalog-Thread fuer immer (kein try in _checker_loop).
        if not isinstance(m, dict) or not isinstance(m.get("id"), str):
            continue
        mid = m["id"]
        name = str(m.get("name") or "")
        ctx = m.get("context_length") if isinstance(m.get("context_length"), int) else 0
        pricing = m.get("pricing") if isinstance(m.get("pricing"), dict) else {}
        sp = m.get("supported_parameters")
        supported_params = sp if isinstance(sp, list) else []
        has_reasoning = "reasoning" in supported_params
        meta_entry = {"name": name, "ctx": ctx, "reasoning": has_reasoning}
        # Preis MITSCHREIBEN: der Kostenregler (Stufe "cheap") braucht ihn zur
        # Laufzeit. In OpenRouters Einheit ($/Token). Fehlt er, fehlt der Schluessel:
        # ein fehlender Preis ist weder 0 noch 999.
        try:
            preis = float(pricing["prompt"]) if pricing.get("prompt") is not None else None
        except (TypeError, ValueError):
            preis = None
        if preis is not None:
            meta_entry["prompt_price"] = preis
        new_metadata[mid] = meta_entry
        if mid.endswith(":free"):
            free_ids[mid] = {"name": name, "ctx": ctx}
        elif any(mid.startswith(p) for p in CLOUD_PREFIXES):
            if preis is not None and _pro_mtok(preis) <= PAID_DISCOVER_THRESHOLD:
                paid_info[mid] = {"name": name, "ctx": ctx, "prompt_price": _pro_mtok(preis)}

    if not new_metadata:
        # Auch eine Liste nur aus kaputten Zeilen (kein String-id) ist ein Ausfall.
        log("check: openrouter lieferte keine verwertbaren Modelle, Durchlauf uebersprungen")
        return

    current_free = dict(_free_catalog)
    new_free = {}
    changes = []
    for mid, info in free_ids.items():
        param = parse_param_count(mid) or parse_param_count(info["name"])
        if param is not None and param <= 7.0:
            continue
        if param is None and info["ctx"] < 32768:
            continue
        status = current_free.get(mid, "ok")
        new_free[mid] = status
        if mid not in current_free:
            changes.append(f"+{mid} (param={param}, ctx={info['ctx']})")

    for mid in current_free:
        if mid not in new_free:
            changes.append(f"-{mid}")

    # --- Paid catalog: abgeleitet wie der Gratiskatalog ---
    # Bis 1.0.2 blieb jeder einmal aufgenommene Eintrag fuer immer ("manuell
    # gepflegte schuetzen") - es gibt keine manuellen, und die Regel hielt Modelle,
    # die teurer geworden oder verschwunden waren. Jetzt: genau das, was OpenRouter
    # gerade billig fuehrt. Ein gescheiterter Abruf kommt hier nie an (oben: return).
    current_paid = dict(_paid_catalog)
    new_paid = {mid: current_paid.get(mid, "ok") for mid in paid_info}
    paid_changes = []
    for mid, info in paid_info.items():
        if mid not in current_paid:
            paid_changes.append(f"+{mid} (${info['prompt_price']:g}/MTok)")
    for mid in current_paid:
        if mid not in new_paid:
            paid_changes.append(f"-{mid}")

    if changes or paid_changes:
        log(f"check: free changes: {', '.join(changes) if changes else 'none'}; "
            f"paid changes: {', '.join(paid_changes) if paid_changes else 'none'}")
    else:
        log("check: no catalog changes")

    # Probe free models for rate-limit status
    for mid in list(new_free.keys()):
        if not OR_KEY:
            break
        old_status = new_free[mid]
        available = _probe_model(OR_KEY, mid)
        if available is False:
            new_free[mid] = "na"
            if old_status != "na":
                log(f"check: {mid} -> RATE-LIMITED (na)")
        elif available is True:
            new_free[mid] = "ok"
            if old_status != "ok":
                log(f"check: {mid} -> recovered (ok)")

    with _cloud_catalog_lock:
        _free_catalog.clear()
        _free_catalog.update(new_free)
        _paid_catalog.clear()
        _paid_catalog.update(new_paid)
        _model_metadata.clear()
        _model_metadata.update(new_metadata)
    _save_catalog()
    n_na = sum(1 for v in new_free.values() if v == "na")
    log(f"check: done ({len(new_free)} free/{n_na} na, {len(new_paid)} paid)")


def _checker_zyklus():
    """Ein Durchlauf, der den Thread nie mitnimmt - der Fehler steht im Protokoll."""
    try:
        _run_catalog_check()
    except Exception as e:
        log(f"check: Durchlauf abgebrochen ({type(e).__name__}: {e}) - naechster in {CHECK_INTERVAL}s")
        traceback.print_exc(file=LOG)


def _checker_loop():
    _checker_zyklus()
    while not _checker_exit.is_set():
        triggered = _checker_pending.wait(timeout=CHECK_INTERVAL)
        if _checker_exit.is_set():
            break
        if triggered:
            _checker_pending.clear()
        _checker_zyklus()


def _ziel(kind, model):
    """Wohin geht die Anfrage: 'local' | 'zen' | 'cloud'.

    Entschieden wird nach der EINSTUFUNG, die der Kostenregler gesehen hat - nie an
    ihr vorbei. Bis 1.0.2 fragte das Routing sie nicht: was als 'local' durch den
    Regler ging, konnte trotzdem an OpenRouter gehen (Wolkenpraefix, Katalog- oder
    Metadateneintrag). Ein lokal gefuehrtes Modell bleibt lokal, auch wenn es wie
    eine OpenRouter-id heisst. kind=None (Anfrage ohne messages) hat kein Modell und
    geht wie bisher an die lokale Kette.
    """
    if kind == "local":
        return "local"
    if model and model.startswith("opencode/"):
        return "zen"
    if model and (is_cloud_model(model) or _is_in_any_catalog(model)):
        return "cloud"
    return "local"


def _is_in_any_catalog(model):
    """Routing: geht das an OpenRouter? Auch fuer bekannte Modelle ausserhalb der
    (nur das Billige anzeigenden) Kataloge und ohne einen der CLOUD_PREFIXES."""
    with _cloud_catalog_lock:
        return model in _free_catalog or model in _paid_catalog or model in _model_metadata


def _get_model_status(model):
    with _cloud_catalog_lock:
        st = _free_catalog.get(model)
        if st is not None:
            return st
        return _paid_catalog.get(model)


def _merge_catalogs():
    with _cloud_catalog_lock:
        return {**_free_catalog, **_paid_catalog}


# ---------------------------------------------------------------- </think>-Naht
# ---------------------------------------------------------------- Randschluessel
# Frames, die ein Filter neu baut, setzen nur die Huelle (id/object/created/model/
# system_fingerprint) und `choices`. Alles andere am Frame-Rand ist eine Aussage des
# Backends und muss mit: `loop_guard` (Rotation im pi-Plugin), `usage`, `timings`,
# was immer arcint oder llama.cpp mitschickt. Ohne das verliert ein Klient nach einem
# Umbau genau den Grund, aus dem der Zug endete - ein stiller falscher Negativ.
_FRAME_HUELLE = frozenset(("id", "object", "created", "model", "system_fingerprint", "choices"))


def _frame_zusatz(j):
    """Was ein Frame ausser der Huelle traegt - der Teil, der unveraendert weitergeht."""
    return {k: v for k, v in j.items() if k not in _FRAME_HUELLE}


class StromSignale:
    """Sammelt aus den zum Klienten gehenden Frames, wie der Zug endete.

    Der Proxy entscheidet den finish_reason nicht (ausser ChatMLGuard) - er muss ihn
    aber protokollieren, damit eine spaetere Rotation im Proxy-Log nachvollziehbar ist.
    Eine Zeile je beendetem Strom: finish_reason und, falls vorhanden, die
    loop_guard-Zaehler. Kein Prompttext, keine Deltas.
    """

    def __init__(self):
        self.finish = None
        self.loop_guard = None
        self._buf = b""

    def feed(self, chunk: bytes) -> None:
        self._buf += chunk
        while True:
            nl = self._buf.find(b"\n")
            if nl < 0:
                break
            zeile, self._buf = self._buf[:nl + 1], self._buf[nl + 1:]
            s = zeile.strip()
            if not s.startswith(b"data: ") or s[6:] == b"[DONE]":
                continue
            try:
                j = json.loads(s[6:])
            except Exception:
                continue
            lg = j.get("loop_guard")
            if isinstance(lg, dict):
                self.loop_guard = lg
            try:
                fin = j["choices"][0].get("finish_reason")
            except Exception:
                fin = None
            if fin is not None:
                self.finish = fin

    def zeile(self) -> str:
        """Was an die Schlusszeile angehaengt wird - leer, wenn nichts bekannt ist."""
        teile = []
        if self.finish is not None:
            teile.append(f"finish={self.finish}")
        if isinstance(self.loop_guard, dict):
            z = ", ".join(f"{k}={v}" for k, v in self.loop_guard.items())
            teile.append(f"loop_guard({z})")
        return (" " + " ".join(teile)) if teile else ""


# ReasoningSeam - repariert die </think>-Naht in einem SSE-Strom.
# 
# Warum das noetig ist (gemessen 2026-08-03):
# Der Proxy haengt lokalen Anfragen stop=['<|im_end|>','<|im_start|>'] an (Fix gegen
# das <|im_end|>-Leck vom 2026-07-31). Stop-Strings zwingen vLLM, ueber Tokengrenzen
# zu puffern, damit ein mehrteiliger Treffer erkannt wird. Das verschiebt die
# Delta-Grenzen - und faellt der Uebergang vom Denkblock zur Antwort dadurch in ein
# Delta, schiebt vLLMs Streaming-Reasoning-Parser das ganze Delta in den content.
# Der Client sieht dann 'schwanz</think>Antwort'. Direkt gemessen: ohne stop 3/3
# sauber, mit stop 3/3 Leck.
# 
# Zwei Eigenschaften der echten Daten bestimmen den Entwurf - beide widerlegen die
# naheliegende Loesung "im Frame ersetzen":
#   1. Der Tag ist ueber Deltas ZERRISSEN: 'rt.</' + 'think>'. Eine Pruefung je Frame
#      findet ihn nicht. Deshalb wird ueber die ersten content-Deltas akkumuliert.
#   2. Der erste content-Frame traegt zugleich ein reasoning-Delta. Er darf also
#      nicht verworfen werden - nur sein content-Feld wird entfernt.
# 
# Angewendet wird der Seam nur auf Stroeme lokaler Denkmodelle (_req_seam): Ein
# Wolkenstrom hat keinen  response-Block aus einem lokalen Parser zu reparieren, dort
# wuerde der Seam nur den Antwortanfang zurueckhalten. Innerhalb des Stroms ist die
# Reparatur aus, sobald sie nichts mehr finden kann: kommt das erste content-Delta
# ohne vorheriges reasoning, geht der Anfang sofort raus. Danach - Naht gefunden
# oder Schwelle ueberschritten - ist der Rest reines Relais.
TAG = "</think>"
MAX_CHARS = 120     # so viel Antwortanfang wird hoechstens gepuffert
MAX_DELTAS = 8      # ... bzw. so viele content-Deltas, was zuerst kommt


class ReasoningSeam:
    def __init__(self):
        self._done = False        # True = nur noch durchreichen
        self._pending = ""        # zurueckgehaltener Antwortanfang
        self._n_deltas = 0
        self._buf = b""           # angebrochene Zeile zwischen zwei Lesevorgaengen
        self._tmpl = None         # letzter content-Frame als Vorlage zum Wiederaufbau
        self._saw_reasoning = False   # kam vor dem Antworttext ein reasoning-Delta?

    # ------------------------------------------------------------------ oeffentlich
    def feed(self, chunk: bytes) -> bytes:
        """Nimmt rohe Bytes, gibt die weiterzuleitenden Bytes zurueck."""
        if self._done:
            return chunk
        self._buf += chunk
        out = bytearray()
        while True:
            nl = self._buf.find(b"\n")
            if nl < 0:
                break
            line, self._buf = self._buf[:nl + 1], self._buf[nl + 1:]
            out += self._line(line)
            if self._done:
                out += self._buf
                self._buf = b""
                return bytes(out)
        return bytes(out)

    def flush(self) -> bytes:
        """Am Stromende: Rest ausgeben, nichts verschlucken."""
        out = bytearray()
        if not self._done and self._pending:
            out += self._emit(self._pending)
        self._done = True
        if self._buf:
            out += self._buf
            self._buf = b""
        return bytes(out)

    # ------------------------------------------------------------------ intern
    def _line(self, line: bytes) -> bytes:
        stripped = line.strip()
        if not stripped.startswith(b"data: "):
            return line
        payload = stripped[6:]
        if payload == b"[DONE]":
            return self._giveup() + line
        try:
            j = json.loads(payload)
            d = j["choices"][0]["delta"]
        except Exception:
            return line                      # nicht interpretierbar: unangetastet

        # KEINE Bedingung auf zuvor gesehenes reasoning: genau die war 2026-08-03 das
        # Loch. Ein Strom kann den Tag liefern, ohne dass je ein reasoning-Delta kam -
        # dann schiebt der Parser den ganzen Denkblock in den content statt nur seinen
        # Schwanz, und die Naht sitzt am Anfang der Antwort.
        if any(d.get(k) for k in ("reasoning", "reasoning_content")):
            self._saw_reasoning = True
        c = d.get("content")
        if not c:                            # kein/leeres content-Delta
            if j["choices"][0].get("finish_reason") is not None and self._pending:
                # Der Zug endet hier. Was noch zurueckgehalten wird, MUSS vor dem
                # abschliessenden Frame raus - sonst sieht der Client erst
                # finish_reason und danach noch ein content-Delta und meldet
                # "Stream ended without finish_reason" (gemessen 2026-08-09 mit
                # lokalem deepseek: Antwort "OK", zu kurz, um die Naht vorher
                # aufzuloesen, also lag sie bis [DONE] fest).
                return self._concat(self._giveup(), line)
            return line

        if not self._saw_reasoning:
            # Kein Denkblock vor dem Text: eine </think>-Naht kann nicht mehr folgen.
            # Den Anfang jetzt rausgeben statt bis zur Schwelle zu warten.
            self._done = True
            return self._giveup() + line
        self._pending += c
        self._n_deltas += 1
        self._tmpl = j
        # Endet der Zug in DIESEM Frame (content + finish_reason zugleich - bei kurzen Antworten
        # der Normalfall, gemessen 2026-10-18 mit DS-4.1), muss der Text VOR dem Frame mit dem
        # finish_reason raus. Bis 1.0.4 ging der finish-Frame zuerst und der Text erst bei [DONE]
        # hinterher; ToolTextSalvage dahinter hielt den Zug dann fuer beendet.
        ends_here = j["choices"][0].get("finish_reason") is not None

        i = self._pending.find(TAG)
        if i >= 0:
            rest = self._pending[i + len(TAG):]
            self._pending = ""
            self._done = True
            if ends_here:
                return self._end_in_frame(j, d, line, rest)
            keep = self._strip_content(j, d, line)
            return self._concat(keep, self._emit(rest) if rest else b"")

        if len(self._pending) >= MAX_CHARS or self._n_deltas >= MAX_DELTAS or ends_here:
            rest, self._pending = self._pending, ""
            self._done = True
            if ends_here:
                return self._end_in_frame(j, d, line, rest)
            keep = self._strip_content(j, d, line)
            return self._concat(keep, self._emit(rest))

        # noch unentschieden: content zurueckhalten, alles andere durchlassen
        return self._strip_content(j, d, line)

    def _end_in_frame(self, j, d, line, rest) -> bytes:
        """Der Zug endet in diesem Frame: erst was er sonst traegt (role, reasoning), dann der
        Antworttext, zuletzt finish_reason allein - die Reihenfolge, die ein getrennter
        finish-Frame auch haette."""
        fin = j["choices"][0].get("finish_reason")
        j["choices"][0]["finish_reason"] = None
        keep = self._strip_content(j, d, line)          # ohne content, ohne finish; ggf. b""
        end = json.loads(json.dumps(j))
        end["choices"][0]["delta"] = {}
        end["choices"][0]["finish_reason"] = fin
        tail = b"data: " + json.dumps(end, ensure_ascii=False).encode() + b"\n"
        body = self._concat(self._emit(rest), tail) if rest else tail
        return self._concat(keep, body) if keep else body

    @staticmethod
    def _concat(keep: bytes, tail: bytes) -> bytes:
        """Append one SSE frame to another, with the blank line events need.

        SSE joins the `data:` fields of ONE event with "\\n", so two data lines
        without a blank line between them arrive as a SINGLE event carrying
        "json1\\njson2". A strict parser then aborts -- the openai SDK reports
        "Unexpected non-whitespace character after JSON ... (line 2 column 1)",
        which is how this surfaced in the pi agent on 2026-08-08.

        `_strip_content` deliberately ends its frame with a single "\\n": the
        blank line that follows it in the source stream is relayed unchanged.
        That is correct while it is the last thing we write for this line --
        but not when `_emit` appends a second frame right behind it, which is
        exactly what happens at the seam. Hence the separator here.
        """
        if keep and tail and not keep.endswith(b"\n\n"):
            return keep + b"\n" + tail
        return keep + tail

    @staticmethod
    def _strip_content(j, d, line) -> bytes:
        """Frame ohne sein content-Feld weitergeben - oder ganz weglassen, wenn er
        sonst nichts traegt. Der erste content-Frame fuehrt reasoning mit; ihn zu
        verwerfen wuerde den Denkblock beschaedigen."""
        rest = {k: v for k, v in d.items() if k != "content"}
        if not any(rest.get(k) for k in ("reasoning", "reasoning_content", "tool_calls", "role")) \
           and j["choices"][0].get("finish_reason") is None \
           and not _frame_zusatz(j):        # Randschluessel sind Grund genug, den Frame zu behalten
            return b""
        j["choices"][0]["delta"] = rest
        return b"data: " + json.dumps(j, ensure_ascii=False).encode() + b"\n"

    def _emit(self, text: str) -> bytes:
        """Den zurueckgehaltenen Antwortanfang als EIN content-Delta nachreichen."""
        if self._tmpl is None:
            return b""
        j = json.loads(json.dumps(self._tmpl))     # Vorlage nicht veraendern
        j["choices"][0]["delta"] = {"content": text}
        j["choices"][0]["finish_reason"] = None
        return b"data: " + json.dumps(j, ensure_ascii=False).encode() + b"\n\n"

    def _giveup(self) -> bytes:
        out = self._emit(self._pending) if self._pending else b""
        self._pending = ""
        self._done = True
        return out


# ChatMLGuard - ChatML-Marker im ANTWORTTEXT beenden den Zug; im Denken nicht.
#
# Bis 1.0.5 hingen lokale Anfragen stop=["<|im_end|>","<|im_start|>"] an (DS-4.1 schrieb
# "<|im_end|>" als Text und brach Agentenketten ab, 2026-07-31). Ein Stop-String gilt bei vLLM
# fuer ALLES Erzeugte, auch das Denken: am 2026-10-18 endete eine pi-Sitzung zweimal still,
# mitten im Gedanken, als das Modell ueber das Qwen-Chat-Template seines Text-Encoders
# nachdachte. Fuer Stroeme macht der Proxy das jetzt selbst und nur im content: taucht ein
# Marker dort auf, endet der Zug an dieser Stelle mit finish_reason "stop", das Backend wird
# gekappt. reasoning und echte tool_calls laufen unberuehrt durch. (Nicht-Stroeme behalten den
# Stop-String: dort gibt es keinen Strom, den man filtern koennte.)
_CHATML = tuple(CHATML_STOPS)


class ChatMLGuard:
    def __init__(self):
        self.ended = False
        self.cut = False
        self._buf = b""
        self._hold = ""
        self._tmpl = None
        self._extra = {}          # Randschluessel, die das Backend bisher geschickt hat

    def _merke(self, j):
        """Randschluessel des Frames behalten - der Schnitt baut den Endframe neu."""
        self._extra.update(_frame_zusatz(j))

    def feed(self, chunk: bytes) -> bytes:
        if self.ended:
            return b""                               # nach dem Schnitt kommt nichts mehr
        self._buf += chunk
        out = bytearray()
        while not self.ended:
            nl = self._buf.find(b"\n")
            if nl < 0:
                break
            line, self._buf = self._buf[:nl + 1], self._buf[nl + 1:]
            out += self._line(line)
        if self.ended:
            self._buf = b""
        return bytes(out)

    def flush(self) -> bytes:
        out = b""
        if not self.ended and self._hold:
            out = self._frame({"content": self._hold})
        self._hold = ""
        rest, self._buf = self._buf, b""
        return out + (b"" if self.ended else rest)

    def _frame(self, delta, finish=None) -> bytes:
        t = self._tmpl or {}
        j = {k: t[k] for k in ("id", "object", "created", "model", "system_fingerprint") if k in t}
        j.update(self._extra)
        j["choices"] = [{"index": 0, "delta": delta, "finish_reason": finish}]
        return b"data: " + json.dumps(j, ensure_ascii=False).encode() + b"\n\n"

    def _line(self, line: bytes) -> bytes:
        stripped = line.strip()
        if not stripped.startswith(b"data: "):
            return line
        payload = stripped[6:]
        if payload == b"[DONE]":
            out = self._frame({"content": self._hold}) if self._hold else b""
            self._hold = ""
            return out + line
        try:
            j = json.loads(payload)
            ch = j["choices"][0]
            d = ch.get("delta") or {}
            if not isinstance(d, dict):
                return line
        except Exception:
            return line
        self._merke(j)
        c = d.get("content")
        if not c or not isinstance(c, str):
            if self._hold:                         # Zurueckgehaltenes vor jedem anderen Delta raus
                self._tmpl = self._tmpl or j
                out, self._hold = self._frame({"content": self._hold}), ""
                return out + line
            return line
        self._tmpl = j
        s, self._hold = self._hold + c, ""
        hits = [i for i in (s.find(m) for m in _CHATML) if i >= 0]
        if hits:
            pre = s[:min(hits)]
            rest = {k: v for k, v in d.items() if k != "content"}
            if pre:
                rest["content"] = pre
            out = self._frame(rest) if any(rest.get(k) for k in ("content", "reasoning", "reasoning_content",
                                                                "role", "tool_calls")) else b""
            self.ended = self.cut = True
            log(f"chatml: Marker im content beendet den Zug, finish_reason selbst auf "
                f"'stop' gesetzt (Backend: {ch.get('finish_reason')!r}) - Randschluessel "
                f"behalten: {', '.join(sorted(_frame_zusatz(j))) or 'keine'}")
            return out + self._frame({}, "stop") + b"data: [DONE]\n\n"
        for k in range(min(len(s), max(map(len, _CHATML)) - 1), 0, -1):
            if any(m.startswith(s[-k:]) for m in _CHATML):
                self._hold = s[-k:]
                s = s[:-k]
                break
        if s == c and ch.get("finish_reason") is None:
            return line                              # unveraendert: byte-gleich
        rest = {k: v for k, v in d.items() if k != "content"}
        if s:
            rest["content"] = s
        fin = ch.get("finish_reason")
        out = b""
        if any(rest.get(k) for k in ("content", "reasoning", "reasoning_content", "role", "tool_calls")):
            out = self._frame(rest)
        if fin is not None:
            if self._hold:
                out += self._frame({"content": self._hold})
                self._hold = ""
            out += self._frame({}, fin)
        return out


# ToolTextSalvage - Tool-Aufrufe, die als TEXT kamen, als echte tool_calls ausliefern.
#
# Warum (gemessen 2026-10-17, Agentensitzungen, DS-4.1 im 2,0-bit-Pack): ab ~459k
# Kontext schreibt DS-4.1 seinen Aufruf als Text - `<invoke name="bash"><parameter
# name="command">...</parameter></invoke>` -, vLLMs Tool-Parser erkennt ihn nicht, pi zeigt
# ihn nur an und fuehrt nichts aus. Schlimmer: der kaputte Zug bleibt im Verlauf, und das
# Modell ahmt ihn ab da nach (nach einer Kompaktierung schon bei 58k wieder). Ein Hinweis im
# Prompt hilft dagegen nicht (er legt genau diese Token in den Kontext und kostet bei jeder
# Aenderung den Prompt-Cache); hier wird der Aufruf so ausgeliefert, wie er gemeint war -
# derselbe Aufruf ueber dasselbe Werkzeug, keine Rechte dazu.
#
# Gerettet wird NUR, wenn alles stimmt: die Anfrage schickte Tools mit, der Block beginnt am
# Zeilenanfang und steht nicht in einem ```-Codeblock, er ist vollstaendig, die Antwort endete
# mit "stop" (nicht "length"), jeder Name steht in der Tool-Liste, jeder Parameter im Schema,
# alle Pflichtparameter sind da. Gleiche Aufrufe gehen nur einmal raus (das Modell schrieb
# jeden Block doppelt). Sonst wird der zurueckgehaltene Text unveraendert nachgereicht.
#
# Zweite Form (2026-10-21, pi-Sitzung auf ds41-flash = DeepSeek-V4.1-Flash-1017): DeepSeeks
# eigenes DSML. Die Zeichen stammen aus dem Referenz-Encoder, den vLLM fuer das Modell nutzt
# (vllm/tokenizers/deepseek_v41_encoding.py: dsml_token, tool_calls_block_name " calls",
# tool_call_tag_name " invoke", tool_parameter_tag_name " parameter"; vllm/parser/deepseek_v41.py
# hat dieselben Terminale) - V4.1 schreibt sie MIT Leerzeichen nach dem Token:
#     <｜DSML｜ calls>
#     <｜DSML｜ invoke name="bash">
#     <｜DSML｜ parameter name="command" string="true">ls</｜DSML｜ parameter>
#     <｜DSML｜ parameter name="timeout" string="false">60</｜DSML｜ parameter>
#     </｜DSML｜ invoke>
#     </｜DSML｜ calls>
# V4 (deepseek_v4_encoding.py) dasselbe ohne Leerzeichen und mit "tool_calls" als Huelle; die
# Form wird ebenso erkannt, denn vLLMs deepseek_v41-Parser kennt nur die V4.1-Schreibweise.
# string="true" = roher Text, string="false" = JSON-Wert; beides wird gegen das Schema geprueft.
# Gleiche Regeln wie oben; die Form, mit der der Mitschnitt beginnt, gilt fuer den ganzen Zug.
_DSML = "\uff5cDSML\uff5c"     # "｜DSML｜" (Vollbreiten-Striche), ein eigenes Token im Vokabular


def _tt_dialect(invoke, param, wrapper, typed):
    e = re.escape
    return {
        "markers": (f"<{invoke} ", f"<{wrapper}>"),
        "invoke": re.compile(rf'<{e(invoke)}\s+name="([^"]+)"\s*>(.*?)</{e(invoke)}>', re.S),
        "param": re.compile(rf'<{e(param)}\s+name="([^"]+)"\s+string="(true|false)"\s*>(.*?)</{e(param)}>'
                            if typed else rf'<{e(param)}\s+name="([^"]+)"[^>]*>(.*?)</{e(param)}>', re.S),
        "wrapper": re.compile(rf'^[ ]{{0,3}}</?{e(wrapper)}>[ \t]*$', re.M),
        "typed": typed,
    }


_TT_DIALECTS = (
    _tt_dialect("invoke", "parameter", "function_calls", False),              # Anthropic-artig
    _tt_dialect(_DSML + " invoke", _DSML + " parameter", _DSML + " calls", True),   # DS-4.1
    _tt_dialect(_DSML + "invoke", _DSML + "parameter", _DSML + "tool_calls", True),  # DS-4
)
_TT_MARKERS = tuple(m for d in _TT_DIALECTS for m in d["markers"])
# Reste, die nach dem Herausnehmen der Bloecke nirgends in der Prosa stehen duerfen
_TT_LEFTOVERS = ("<invoke", "</parameter>", "function_calls>", _DSML)
_TT_TYPES = {"integer": int, "number": (int, float), "boolean": bool, "array": list, "object": dict}
_TT_MAX_HOLD = 262144     # so viel wird hoechstens zurueckgehalten (auch ein write mit langem Inhalt)


def _tool_schemas(tools, tool_choice=None):
    """{name: (properties, required)} aus einer OpenAI-tools-Liste; None ohne Tools oder bei
    tool_choice "none". Ein benannter tool_choice beschraenkt auf diese eine Funktion.
    Wirft nie: ein kaputtes Schema heisst nur "nichts retten"."""
    if tool_choice == "none":
        return None
    only = None
    if isinstance(tool_choice, dict):
        only = (tool_choice.get("function") or {}).get("name")
    out = {}
    for t in tools if isinstance(tools, list) else []:
        f = t.get("function") if isinstance(t, dict) else None
        if not isinstance(f, dict) or not isinstance(f.get("name"), str):
            continue
        if only is not None and f["name"] != only:
            continue
        p = f.get("parameters") if isinstance(f.get("parameters"), dict) else {}
        props = p.get("properties") if isinstance(p.get("properties"), dict) else {}
        req = p.get("required") if isinstance(p.get("required"), list) else []
        out[f["name"]] = (props, {r for r in req if isinstance(r, str)})
    return out or None


def _tt_no_constant(c):
    raise ValueError(f"{c} ist kein JSON")      # sonst ginge {"n": NaN} als arguments raus


def _tt_value(raw, schema, string=None):
    """Wert eines Parameters, geprueft gegen sein Schema. string=None (Form ohne Angabe): der
    Typ kommt aus dem Schema. string=True (DSML string="true"): roher Text, nur wo das Schema
    einen String zulaesst oder keinen Typ nennt. string=False: JSON-Wert, der zum Typ passt."""
    typ = (schema or {}).get("type") if isinstance(schema, dict) else None
    typs = typ if isinstance(typ, list) else [typ]
    untyped = not any(t in _TT_TYPES or t in ("string", "null") for t in typs)
    if string is True:
        if untyped or "string" in typs:
            return raw
        raise ValueError(f"string={raw!r}, Schema verlangt {typ}")
    if string is None and (not any(t in _TT_TYPES for t in typs) or "string" in typs):
        return raw                                   # string, untypisiert, oder string erlaubt
    v = json.loads(raw.strip(), parse_constant=_tt_no_constant)   # NaN/Infinity sind kein JSON
    if string is False and untyped:
        return v
    if string is False and "string" in typs and isinstance(v, str):
        return v
    for t in typs:
        if t == "null" and v is None:
            return v
        if t in _TT_TYPES and isinstance(v, _TT_TYPES[t]) and not (isinstance(v, bool) and t != "boolean"):
            return v
    raise ValueError(f"{raw!r} passt nicht zu {typ}")


def _tt_line_start(text, i):
    """Steht text[i] am Zeilenanfang, mit hoechstens drei Leerzeichen davor (Markdown: vier
    Leerzeichen oder ein Tab waeren schon ein eingerueckter Codeblock)?"""
    j = text.rfind("\n", 0, i) + 1
    pre = text[j:i]
    return len(pre) <= 3 and pre == " " * len(pre)


def _tt_fence_step(state, line):
    """Ein Zeilenschritt des Codeblock-Zustands nach CommonMark: state = None (kein Block) oder
    (Zeichen, Laenge) des oeffnenden Zauns. Oeffnen: bis 3 Leerzeichen, dann >= 3 ` oder ~.
    Schliessen nur mit DEMSELBEN Zeichen, mindestens so lang, ohne Info-Text danach - so bleibt
    ein ```bash in einem ````markdown-Block Inhalt (2026-10-17, Review-Fund)."""
    st = line.lstrip(" ")
    if len(line) - len(st) > 3 or st[:1] not in ("`", "~"):
        return state
    ch = st[0]
    run = len(st) - len(st.lstrip(ch))
    if run < 3:
        return state
    if state is None:
        return (ch, run)
    if ch == state[0] and run >= state[1] and not st[run:].strip():
        return None
    return state


def _tt_fence_open(text, i):
    """Ist an Stelle i ein Codeblock offen? (Der Mitschnitt beginnt immer ausserhalb.)"""
    state = None
    for line in text[:i].split("\n")[:-1] if i else []:
        state = _tt_fence_step(state, line)
    return state is not None


def parse_text_tool_calls(text, tools, parallel=True):
    """(calls, prose, drafts) fuer den zurueckgehaltenen Text (beginnt am Zeilenanfang mit einem
    Aufruf-Anfang); None, wenn auch nur ein Teil nicht passt. calls = [(name, arguments-dict)].

    JEDER Block muss am Zeilenanfang stehen und ausserhalb eines Codeblocks - ein spaeteres
    Beispiel im Zaun oder mitten im Satz macht das Ganze zu Text. Bloecke direkt nacheinander
    sind EINE Gruppe paralleler Aufrufe; steht Prosa zwischen zwei Bloecken, hat das Modell
    umgedacht (2026-10-17: Entwurf, "Wait, I should double check...", verbesserter Block):
    dann zaehlt nur die LETZTE Gruppe (`drafts` = Zahl der verworfenen Aufrufe). Keiner der
    Bloecke bleibt in der Prosa - ein Text-Aufruf im Verlauf wird sonst nachgeahmt.
    `<function_calls>`-Huellen zaehlen nur als eigene Zeilen zwischen den Bloecken.
    Die Form (`<invoke>` oder DSML, siehe _TT_DIALECTS) bestimmt der Anfang des Texts; eine
    zweite Form im selben Zug macht das Ganze zu Text."""
    if not tools:
        return None
    body = text.lstrip(" ")
    dia = next((d for d in _TT_DIALECTS if any(body.startswith(mk) for mk in d["markers"])), None)
    if dia is None:
        return None
    groups, prose, pos, shells = [], [], 0, 0

    def unwrap(gap):                                 # Huellenzeilen raus, offen/zu mitzaehlen
        nonlocal shells
        for w in dia["wrapper"].finditer(gap):
            shells += -1 if w.group(0).lstrip(" ").startswith("</") else 1
            if shells < 0:
                shells = -999                        # Schluss vor Anfang: nie mehr ausgleichbar
        return dia["wrapper"].sub("", gap)

    for m in dia["invoke"].finditer(text):
        if not _tt_line_start(text, m.start()) or _tt_fence_open(text, m.start()):
            return None
        gap = unwrap(text[pos:m.start()])
        prose.append(gap)
        if not groups or gap.strip():
            groups.append([])
        pos = m.end()
        name, inner = m.group(1), m.group(2)
        if name not in tools:
            return None
        props, required = tools[name]
        args = {}
        for pm in dia["param"].finditer(inner):
            pname = pm.group(1)
            if pname not in props or pname in args:
                return None
            raw, string = (pm.group(3), pm.group(2) == "true") if dia["typed"] else (pm.group(2), None)
            try:
                args[pname] = _tt_value(raw, props[pname], string)
            except (ValueError, TypeError):
                return None
        if dia["param"].sub("", inner).strip() or not required <= set(args):
            return None                              # Reste im Block / Pflicht fehlt
        if (name, args) not in groups[-1]:
            groups[-1].append((name, args))
    prose.append(unwrap(text[pos:]))
    rest = "".join(prose)
    if not groups or shells != 0 or any(t in rest for t in _TT_LEFTOVERS):
        return None                                  # unvollstaendig / Huelle offen / mitten im Text
    if not parallel and len(groups[-1]) > 1:
        return None                                  # parallel_tool_calls=false: nicht raten
    return groups[-1], rest.strip(), sum(len(g) for g in groups[:-1])


class ToolTextSalvage:
    """SSE-Filter hinter ReasoningSeam. Ein Zeilenautomat ueber den content des GANZEN Stroms:
    am Zeilenanfang wird zurueckgehalten, solange die Zeile ein Marker-Anfang sein kann; ist sie
    keiner, geht sie sofort raus. Ein voller Marker ausserhalb eines Codeblocks schaltet auf
    Mitschnitt bis zum Zugende. Frames, an denen nichts zu aendern ist, gehen byte-gleich raus."""

    def __init__(self, tools, parallel=True):
        self.tools = tools
        self.parallel = parallel
        self.result = None        # fuers Protokoll: "salvaged N" / Grund fuer "kept"
        self._done = False
        self._buf = b""
        self._at_start = True     # steht der naechste Buchstabe am Zeilenanfang?
        self._cur = ""            # bisher gesendeter Teil der laufenden Zeile
        self._fence = None        # offener Codeblock im gesendeten Text: (Zeichen, Laenge)
        self._hold = ""           # zurueckgehalten: Zeilenanfang-Kandidat oder Mitschnitt
        self._capturing = False
        self._tmpl = None
        self._extra = {}          # Randschluessel, die das Backend bisher geschickt hat

    def _merke(self, j):
        """Randschluessel des Frames behalten - gerettete Frames werden neu gebaut."""
        self._extra.update(_frame_zusatz(j))

    # ------------------------------------------------------------ oeffentlich
    def feed(self, chunk: bytes) -> bytes:
        if self._done:
            return chunk
        self._buf += chunk
        out = bytearray()
        while not self._done:
            nl = self._buf.find(b"\n")
            if nl < 0:
                break
            line, self._buf = self._buf[:nl + 1], self._buf[nl + 1:]
            out += self._line(line)
        if self._done:
            out += self._buf
            self._buf = b""
        return bytes(out)

    def flush(self) -> bytes:
        out = bytearray()
        if not self._done and self._hold:
            out += self._frame({"content": self._hold})
            self.result = self.result or "kept (stream ended without finish_reason)"
        self._hold, self._done = "", True
        out += self._buf
        self._buf = b""
        return bytes(out)

    # ------------------------------------------------------------ intern
    def _frame(self, delta, finish=None) -> bytes:
        t = self._tmpl or {}
        j = {k: t[k] for k in ("id", "object", "created", "model", "system_fingerprint") if k in t}
        j.update(self._extra)
        j["choices"] = [{"index": 0, "delta": delta, "finish_reason": finish}]
        return b"data: " + json.dumps(j, ensure_ascii=False).encode() + b"\n\n"

    def _sent(self, text):
        """Buchfuehrung ueber Gesendetes: Zeilenanfang und Codeblock-Zustand. Von der laufenden
        Zeile reicht der Anfang (Zaunzeilen sind kurz; eine lange Zeile ist kein Schluss-Zaun)."""
        if not text:
            return
        parts = text.split("\n")
        head = self._cur + parts[0]
        for nxt in parts[1:]:
            self._fence = _tt_fence_step(self._fence, head)
            head = nxt
        self._cur = head[:256]
        self._at_start = head == ""

    def _content(self, c) -> str:
        """Nimmt ein content-Stueck, gibt zurueck, was sofort raus darf."""
        if self._capturing:
            self._hold += c
            if len(self._hold) > _TT_MAX_HOLD:   # kein Aufruf ist so lang: freigeben, aufgeben
                out, self._hold, self._capturing, self._done = self._hold, "", False, True
                self.result = "kept (longer than the hold limit)"
                return out
            return ""
        if not self._hold and not self._at_start and "\n" not in c:
            self._sent(c)                           # mitten in der Zeile: nichts zu pruefen
            return c
        out = []
        for k, ch in enumerate(c):
            if self._capturing:                     # ab dem Marker: Rest des Stuecks mitschneiden
                self._hold += c[k:]
                break
            if self._hold or (self._at_start and self._fence is None):
                cand = self._hold + ch
                body = cand.lstrip(" ")
                if len(cand) - len(body) <= 3 and (not body or any(mk.startswith(body) or body.startswith(mk)
                                                                for mk in _TT_MARKERS)):
                    if any(body.startswith(mk) for mk in _TT_MARKERS):
                        self._capturing, self._hold = True, cand
                        continue
                    self._hold = cand         # kann noch ein Marker werden
                    continue
                self._hold = ""               # ist keiner: alles Zurueckgehaltene raus
                out.append(cand)
                self._sent(cand)
                continue
            out.append(ch)
            self._sent(ch)
        if self._capturing and len(self._hold) > _TT_MAX_HOLD:
            return "".join(out) + self._content("")
        return "".join(out)

    def _finish(self, finish, line) -> bytes:
        """Zugende: Block auswerten und Schlussframe(s) liefern."""
        self._done = True
        if not self._capturing:
            out = self._frame({"content": self._hold}) if self._hold else b""
            self._hold = ""
            return out + line
        if finish != "stop":
            self.result = f"kept (finish_reason={finish})"
        else:
            try:
                parsed = parse_text_tool_calls(self._hold, self.tools, self.parallel)
            except Exception as e:                    # z. B. RecursionError in tief verschachteltem JSON
                parsed = None
                log(f"tooltext: Pruefung scheiterte ({type(e).__name__}) - bleibt Text")
            if parsed is None:
                self.result = "kept (block does not fit the request's tools or sits in quoted text)"
            else:
                calls, prose, drafts = parsed
                out = self._frame({"content": "\n" + prose}) if prose else b""
                out += self._frame({"tool_calls": [
                    {"index": n, "id": f"call_{uuid.uuid4().hex[:24]}", "type": "function",
                     "function": {"name": name, "arguments": json.dumps(a, ensure_ascii=False)}}
                    for n, (name, a) in enumerate(calls)]})
                self.result = (f"salvaged {len(calls)} call(s): {', '.join(c[0] for c in calls)}"
                               + (f", {drafts} draft(s) dropped" if drafts else ""))
                return out + self._frame({}, "tool_calls")
        out = self._frame({"content": self._hold})
        self._hold = ""
        return out + line

    def _giveup(self, line) -> bytes:
        out = self._frame({"content": self._hold}) if self._hold else b""
        self._hold, self._done = "", True
        return out + line

    def _line(self, line: bytes) -> bytes:
        stripped = line.strip()
        if not stripped.startswith(b"data: "):
            return line
        payload = stripped[6:]
        if payload == b"[DONE]":
            if self._capturing or self._hold:          # kein finish_reason gesehen
                self.result = "kept (no finish_reason before [DONE])"
            return self._giveup(line)
        try:
            j = json.loads(payload)
            chs = j["choices"]
            ch = chs[0]
            d = ch.get("delta") or {}
        except Exception:
            return line
        self._merke(j)
        if len(chs) != 1 or ch.get("index", 0) != 0:   # n>1: nicht unser Fall
            self.result = "kept (more than one choice)"
            return self._giveup(line)
        if d.get("tool_calls"):                        # echter Aufruf: nichts zu retten
            return self._giveup(line)
        fin = ch.get("finish_reason")
        c = d.get("content")
        if c:
            now = self._content(c)
            if self._done:                             # Haltegrenze: ab hier reines Relais
                self._tmpl = self._tmpl or json.loads(json.dumps(j))
                return self._frame(dict(d, content=now), fin) if fin is None else \
                    self._frame(dict(d, content=now)) + self._frame({}, fin)
            if now == c and fin is None:
                return line                            # nichts geaendert: byte-gleich
            self._tmpl = json.loads(json.dumps(j))
            rest = {k: v for k, v in d.items() if k != "content"}
            if now:
                rest["content"] = now
            keep = b""
            if any(rest.get(k) for k in ("content", "reasoning", "reasoning_content", "role")):
                keep = self._frame(rest)
            if fin is not None:
                return keep + self._finish(fin, self._frame({}, fin)[:-1])
            return keep
        if fin is not None:
            if self._tmpl is None:
                self._tmpl = json.loads(json.dumps(j))
            return self._finish(fin, line)
        return line


class StromAbgeschnitten(ConnectionResetError):
    """Die Backend-Verbindung wurde geschlossen, waehrend der Strom noch lief.

    Zweiter Ort desselben Wettlaufs, den getresponse() seit 2026-08-07 abfaengt:
    _client_watchdog schliesst die Verbindung, sobald der Anrufer auflegt. Faellt
    das mit einem laufenden resp.read1() zusammen, setzt http.client sein fp auf
    None *zwischen* _get_chunk_left() und dem eigentlichen Lesen, und die naechste
    Zeile stirbt an `NoneType object has no attribute read1`. Ein AttributeError
    faellt durch jede except-Klausel in _try_backend, landet als Traceback im Log
    und verschluckt dabei genau die Zeile, die erklaeren wuerde, was los war --
    116 mal am 2026-09-10, nachdem der andere Ort dasselbe 73 mal getan hatte.

    Eigener Typ statt nacktem ConnectionResetError, weil an dieser Stelle die
    Kopfzeilen schon beim Anrufer sind: Weiterreichen an ein anderes Backend ist
    nicht mehr moeglich, und ein _send_error(502) wuerde JSON hinter den halben
    Strom haengen. Deshalb behandelt _try_backend diesen Typ eigens und
    beantwortet ihn mit "aborted".
    """


class BackendTimeout(Exception):
    """Das Backend hat nicht mehr geantwortet -- nicht der Anrufer.

    `gesendet` sagt, ob schon Bytes beim Anrufer sind. Solange nichts raus ist,
    darf _try_backend weiterreichen; danach bleibt nur ein ehrlicher Abbruch,
    weil ein Fehlerrumpf hinter halben Kopfzeilen nichts mehr rettet.
    """
    def __init__(self, msg, gesendet):
        super().__init__(msg)
        self.gesendet = gesendet


class ClientGone(Exception):
    """Das Schreiben an den Anrufer ist gescheitert -- nicht das Backend.

    Ein halb geschlossener Client darf weiterlesen (dann gelingen die
    Schreibvorgaenge); erst ein fehlgeschlagener Schreibvorgang beweist, dass
    niemand mehr liest. _try_backend behandelt das als CLIENT-CANCELLED, nie
    als Backend-Ausfall (sonst folgt ein sinnloser routed-FAIL)."""


class _UpstreamBuffer:
    """Der Leserfaden legt rohe SSE-Rahmen hier ab; der Schreibpfad holt sie ab.

    Die Obergrenze gilt in Bytes (plus ein Rahmen), nicht in Rahmen: `put`
    blockiert, sobald sie erreicht waere, und ruft `on_full` genau einmal.
    Nichts wird verworfen. Ein gesetztes `stop` weckt einen wartenden `put`
    und einen wartenden `get`, damit ein auflegender Anrufer nicht in einem
    vollen Puffer haengt."""

    def __init__(self, max_bytes):
        self.max_bytes = max_bytes
        self._items = collections.deque()
        self._bytes = 0
        self._stop = False
        self._warned = False
        self._cv = threading.Condition()

    def put(self, chunk, on_full):
        with self._cv:
            while self._bytes + len(chunk) > self.max_bytes and not self._stop:
                if not self._warned:
                    self._warned = True
                    on_full()
                self._cv.wait(timeout=0.25)
            if self._stop:
                return False
            self._items.append(chunk)
            self._bytes += len(chunk)
            self._cv.notify()
            return True

    def get(self):
        with self._cv:
            while not self._items and not self._stop:
                self._cv.wait(timeout=0.25)
            if not self._items:
                return None
            chunk = self._items.popleft()
            self._bytes -= len(chunk)
            self._cv.notify()
            return chunk

    def stop(self):
        with self._cv:
            self._stop = True
            self._cv.notify_all()


def _drain_reader(resp, buf, on_full):
    """Eigener Faden: rohe SSE-Rahmen in den Puffer, Fehler in die Rueckgabeliste.

    Das Lesen darf nie am Schreiben oder Verarbeiten haengen. Faellt
    read1 aus, landet die Ausnahme in der Liste; der Konsument entscheidet,
    was sie bedeutet. Der Faden wird mit zurueckgegeben, damit der Aufrufer
    ihn nach `buf.stop()` und dem Socket-shutdown beitreten kann: erst dann
    darf die Verbindung geschlossen werden (siehe _stop_reader)."""
    errors = []

    def _run():
        try:
            while True:
                chunk = resp.read1(65536)
                if not chunk:
                    break
                if not buf.put(chunk, on_full):
                    break
        except BaseException as e:          # noqa: BLE001 - an den Konsumenten weiterreichen
            errors.append(e)
        finally:
            buf.stop()

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return errors, t


def _stop_reader(buf, sock, thread):
    """Lesefaden beenden, bevor die Verbindung geschlossen wird.

    Der Wachhund kappt den Backend-Socket mit shutdown(); danach bekommt der
    Lesefaden ein sauberes EOF und sein read1 schliesst den fp. Schliesst
    gleichzeitig ein conn.close() aus einem zweiten Faden, treffen sich beide
    in `_close_conn`, der zweite liest `self.fp = None` und faellt an
    `None.close` -- im Protokoll als `Lesefaden abgerissen: AttributeError:
    'NoneType' object has no attribute 'close'`. Deshalb: Puffer wecken,
    Socket kappen, dem Faden beitreten; erst danach schliesst der Hauptfaden
    unter `_backend_close_lock` -- genau einmal.
    """
    buf.stop()
    if sock is not None:
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
    if thread is not None:
        thread.join(timeout=0.5)    # shutdown weckt read1; die Frist ist nur Rueckfall


def _lesefaden_abriss(err, gone_evt):
    """Der Fehler des Lesefadens als Stromabriss, mit der richtigen Herkunft.

    Ein AttributeError ist der bekannte Wettlauf, wenn der Wachhund den Socket
    tatsaechlich gekappt hat (gone gesetzt). Ohne diesen Beweis ist es ein
    echter Programmfehler: dann bleiben Typ und Meldung in der Logzeile, damit
    er nicht als Client-Abbruch getarnt wird.
    """
    if isinstance(err, AttributeError) and gone_evt is not None and gone_evt.is_set():
        return StromAbgeschnitten(
            "die Backend-Verbindung wurde unter dem Lesefaden geschlossen")
    return StromAbgeschnitten(f"Lesefaden abgerissen: {type(err).__name__}: {err}")


class Handler(http.server.BaseHTTPRequestHandler):
    def _rid_jetzt(self):
        """Die ID dieses Zuges; fehlt sie noch, wird sie hier bestimmt.

        Direkt aufgerufene Handlerteile (Tests, Einzelpfade) laufen ohne
        _proxy, deshalb entsteht die ID auch hier -- nicht als Attributfehler."""
        rid = getattr(self, "_rid", None)
        if rid is None:
            kopf = getattr(self, "headers", None)
            wert = kopf.get("X-Request-Id") if kopf is not None else None
            rid = self._rid = _anfrage_rid(wert)
        return rid

    def send_response(self, code, message=None):
        # Jede Antwort traegt X-Request-Id: Erfolg, eigener Fehler, SSE vor
        # dem ersten Ereignis (die Kopfzeilen gehen vor dem Strom raus).
        rid = self._rid_jetzt()
        super().send_response(code, message)
        self.send_header("X-Request-Id", rid)
        if getattr(self, "_ft_path", None):
            self.send_header("X-Llm-Proxy-Path", self._ft_path)

    def _backend_rid_pruefen(self, resp, backend_name, method, path, hint):
        """Der Backend-Wert weicht ab: das Gateway hat seinen geschickt, das
        Backend hat ihn ersetzt. Einmal je Anfrage, der eigene Wert bleibt."""
        try:
            back = resp.getheader("X-Request-Id")
        except Exception:
            back = None
        if getattr(self, "_rid_ersetzt_gemeldet", False):
            return
        eigene = self._rid_jetzt()
        if _rid_gueltig(back) and back != eigene:
            self._rid_ersetzt_gemeldet = True
            log(f"{method} {path} -> {backend_name} "
                f"backend_rid={back} (backend replaced the request id){hint}")

    def send_header(self, keyword, value):
        if keyword.lower() == "access-control-allow-origin":
            self._cors_gesetzt = True
        super().send_header(keyword, value)

    def end_headers(self):
        # Eine erlaubte Browser-Anfrage darf die Antwort auch lesen: ohne ACAO
        # blockt der Browser sie, obwohl der Preflight bestanden hat. Und ohne
        # Expose-Headers kommt X-Request-Id im Browser nicht an.
        if getattr(self, "_cors_origin", None):
            if not getattr(self, "_cors_gesetzt", False):
                self.send_header("Access-Control-Allow-Origin", self._cors_origin)
                self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Expose-Headers", "X-Request-Id")
        super().end_headers()

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(length) if length else b""

    def _fwd_headers_local(self, body_len):
        h = {}
        for k in ("Content-Type", "Authorization", "Accept", "Accept-Encoding"):
            v = self.headers.get(k)
            if v:
                h[k] = v
        h["X-Request-Id"] = self._rid_jetzt()
        if body_len:
            h["Content-Length"] = str(body_len)
        return h

    def _fwd_headers_cloud(self, body_len):
        h = {
            "Content-Type": self.headers.get("Content-Type", "application/json"),
            "Authorization": f"Bearer {OR_KEY}",
            "Accept": self.headers.get("Accept", "application/json"),
            "Accept-Encoding": "identity",
            "HTTP-Referer": os.environ.get("LLM_PROXY_REFERER", "http://localhost"),
            "X-Title": os.environ.get("LLM_PROXY_TITLE", "llm-proxy"),
            "X-Request-Id": self._rid_jetzt(),
        }
        if body_len:
            h["Content-Length"] = str(body_len)
        return h

    # ---------------------------------------------- feyntalk token mode output
    # Both methods consume the engine's Open Responses SSE. The Chat path builds
    # its frames from feyntalk-render's normalised stream; the Responses path
    # relays the engine's bytes unchanged (the arcint events included), while
    # feeding the same normaliser so a contract violation is at least logged.

    def _events_from_buffer(self, buf, chunk):
        buf += chunk
        events = []
        while True:
            nl = buf.find(b"\n")
            if nl < 0:
                break
            line, buf = buf[:nl + 1], buf[nl + 1:]
            line = line.strip()
            if not line.startswith(b"data:"):
                continue
            payload = line[5:].strip()
            if payload == b"[DONE]":
                events.append(None)
                continue
            try:
                event = json.loads(payload)
            except Exception:
                continue
            if isinstance(event, dict):
                events.append(event)
        return buf, events

    def _stream_chat_from_tokens(self, resp, backend_name, t0, t_try, hint, method, path,
                                 request, sock=None, gone_evt=None):
        material = request["material"]
        tools = request["tools"]
        model = request["model"]
        client_stream = bool(request.get("stream"))
        self._client_streaming = client_stream
        if sock is not None:
            try:
                sock.settimeout(READ_TIMEOUT)
            except OSError:
                pass
        erster = None
        try:
            erster = resp.read1(65536)
        except TimeoutError as e:
            raise BackendTimeout(f"kein erstes Byte in {CONNECT_TIMEOUT}s: {e}", False)
        norm = _FTNormalizer(material, _ft_schemas_from_tools(tools))
        rid = "chatcmpl-" + uuid.uuid4().hex
        created = int(time.time())
        resource = None
        sse_buf = b""
        emitted_reasoning = 0
        emitted_text = 0
        terminal = False
        # Die Objekt-Ereignisse des engine-seitigen terminal()-Pfads
        # (loop_guard, context, truncated), sofern der Zug welche schickte; sie
        # reisen unveraendert als `arcint`-Objekt am Terminal mit.
        arcint_events = {}

        # Der Upstream muss immer abgeflossen werden:
        # ein eigener Lesefaden schiebt die rohen SSE-Rahmen in einen
        # bytebegrenzten Puffer, den der Schreibpfad unten abholt. Ein
        # stockender Verarbeitungs- oder Schreibschritt kann das Lesen nicht
        # mehr fuer > 5 s anhalten (der Motor bricht sonst seine
        # Schreibfrist ab und meldet "client gone").
        buf = _UpstreamBuffer(STREAM_QUEUE_MAX_BYTES)

        def _reader_warn():
            log(f"{method} {path} -> {backend_name} feyntalk: Leser laeuft dem "
                f"Schreibpfad davon ({STREAM_QUEUE_MAX_BYTES // (1024 * 1024)} MiB "
                f"gepuffert), Rueckstau - kein Token verworfen")

        reader_error, reader_thread = _drain_reader(resp, buf, _reader_warn)
        self._backend_reader = reader_thread
        self._backend_buf = buf

        if client_stream:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.send_header("X-LLM-Backend", backend_name)
            self.end_headers()
            self._client_write(_chat_chunk(rid, model, created, {"role": "assistant"}))
            self._client_flush()

        def _emit_deltas():
            nonlocal emitted_reasoning, emitted_text
            if not client_stream:
                return
            # Nur die NEU angehaengten Stuecke verbinden, nicht das ganze
            # Praefix je Ereignis: ein join ueber den gesamten bisherigen Text
            # war die superlineare Stelle (Kosten je Ereignis wuchsen mit der
            # Position, gemessen 2026-11-01 mit tests/bench_token_stream.py).
            count = len(norm._reasoning)
            if count > emitted_reasoning:
                delta = "".join(norm._reasoning[emitted_reasoning:count])
                emitted_reasoning = count
                if delta:      # leere Stuecke erzeugten auf main keinen Rahmen
                    self._client_write(_chat_chunk(rid, model, created,
                                                   {"reasoning_content": delta}))
            count = len(norm._text)
            if count > emitted_text:
                delta = "".join(norm._text[emitted_text:count])
                emitted_text = count
                if delta:
                    self._client_write(_chat_chunk(rid, model, created, {"content": delta}))
            self._client_flush()

        def _feed(chunk):
            nonlocal sse_buf, rid, created, resource, terminal, arcint_events
            sse_buf, events = self._events_from_buffer(sse_buf, chunk)
            for event in events:
                if event is None:
                    terminal = True
                    continue
                kind = event.get("type")
                if kind in ("arcint:token", "arcint:span"):
                    try:
                        norm.feed(event)
                    except _FTSpanError as e:
                        log(f"{method} {path} -> {backend_name} feyntalk: {e}")
                        raise StromAbgeschnitten("feyntalk span contract broken")
                elif kind in _CHAT_ARCINT_KINDS:
                    # Zahlen, kein Strom: kein Token, kein Span, nur das Objekt.
                    arcint_events[kind] = event
                elif kind in ("response.created", "response.in_progress"):
                    res = event.get("response")
                    if isinstance(res, dict):
                        if isinstance(res.get("id"), str):
                            rid = res["id"]
                        if isinstance(res.get("created_at"), int):
                            created = res["created_at"]
                elif kind in ("response.completed", "response.incomplete"):
                    res = event.get("response")
                    if isinstance(res, dict):
                        resource = res
                        if isinstance(res.get("id"), str):
                            rid = res["id"]
                    terminal = True
                elif kind == "response.failed":
                    log(f"{method} {path} -> {backend_name} feyntalk: response.failed")
            _emit_deltas()

        try:
            if erster:
                _feed(erster)
            while True:
                chunk = buf.get()
                if chunk is None:
                    if reader_error:
                        err = reader_error[0]
                        if isinstance(err, TimeoutError):
                            raise err
                        raise _lesefaden_abriss(err, gone_evt)
                    break
                _feed(chunk)
        except TimeoutError as e:
            raise BackendTimeout(f"kein Byte in {READ_TIMEOUT}s: {e}", True)
        except _FTSpanError as e:
            log(f"{method} {path} -> {backend_name} feyntalk: {e}")
            raise StromAbgeschnitten(
                f"feyntalk span contract broken after the headers were out: {e}")
        finally:
            _stop_reader(buf, sock, reader_thread)

        try:
            output = norm.finish()
        except _FTSpanError as e:
            log(f"{method} {path} -> {backend_name} feyntalk: {e}")
            raise StromAbgeschnitten(
                f"feyntalk stream end violated the contract: {e}")
        _emit_deltas()   # der finish()-Schwanz (gehaltener Rest) faellt sonst weg

        calls = output.tool_calls
        output_items = resource.get("output") if isinstance(resource, dict) else None
        finish_reason = _chat_finish_reason(resource, calls)
        usage = _chat_usage(resource)
        arcint = _chat_arcint_object(arcint_events)
        if not terminal:
            log(f"{method} {path} -> {backend_name} feyntalk: stream ended without a terminal event")
        if client_stream:
            for frame in _chat_tool_call_frames(calls, output_items, rid, model, created):
                self._client_write(frame)
            final = {"choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}]}
            if usage:
                final["usage"] = usage
            if arcint:
                final["arcint"] = arcint
            self._client_write(("data: " + json.dumps(final, ensure_ascii=False) + "\n\n").encode("utf-8"))
            self._client_write(b"data: [DONE]\n\n")
            self._client_flush()
        else:
            message = {"role": "assistant", "content": output.text or None}
            if output.reasoning:
                message["reasoning_content"] = output.reasoning
            if calls:
                entries = []
                ids = _chat_tool_call_ids(output_items)
                for index, call in enumerate(calls):
                    call_id = ids[index] if index < len(ids) else None
                    if not isinstance(call_id, str) or not call_id:
                        call_id = "call_%d" % index
                    entries.append({"id": call_id, "type": "function",
                                    "function": {"name": call.name, "arguments": call.arguments}})
                message["tool_calls"] = entries
                if not output.text:
                    message["content"] = None
            body = {"id": rid, "object": "chat.completion", "created": created, "model": model,
                    "choices": [{"index": 0, "message": message,
                                 "finish_reason": finish_reason}]}
            if usage:
                body["usage"] = usage
            if arcint:
                body["arcint"] = arcint
            raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("X-LLM-Backend", backend_name)
            self.end_headers()
            self._client_write(raw)
            self._client_flush()
            log(f"{method} {path} -> {backend_name} feyntalk chat status=200 "
                f"total={time.monotonic()-t0:.2f}s bytes={len(raw)}{hint}")
            return

        log(f"{method} {path} -> {backend_name} feyntalk chat status={resp.status} "
            f"ttfb={time.monotonic()-t_try:.2f}s total={time.monotonic()-t0:.2f}s "
            f"finish={finish_reason} think={request.get('think', 'off')}{hint}")

    def _stream_responses_from_tokens(self, resp, backend_name, t0, t_try, hint, method, path,
                                      request, sock=None, gone_evt=None):
        if sock is not None:
            try:
                sock.settimeout(READ_TIMEOUT)
            except OSError:
                pass
        erster = None
        try:
            erster = resp.read1(65536)
        except TimeoutError as e:
            raise BackendTimeout(f"kein erstes Byte in {CONNECT_TIMEOUT}s: {e}", False)
        # Auch der Responses-Passthrough liest in einem eigenen Faden: sonst
        # haelt ein blockierender Schreibvorgang (bis CLIENT_WRITE_TIMEOUT) das
        # Upstream-Lesen an und die Engine laeuft in ihre Schreibfrist.
        buf = _UpstreamBuffer(STREAM_QUEUE_MAX_BYTES)

        def _reader_warn():
            log(f"{method} {path} -> {backend_name} feyntalk: Leser laeuft dem "
                f"Schreibpfad davon ({STREAM_QUEUE_MAX_BYTES // (1024 * 1024)} MiB "
                f"gepuffert), Rueckstau - kein Token verworfen")

        reader_error, reader_thread = _drain_reader(resp, buf, _reader_warn)
        self._backend_reader = reader_thread
        self._backend_buf = buf
        # Der Responses-Passthrough ist immer ein Event-Strom.
        self._client_streaming = True
        self.send_response(resp.status)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.send_header("X-LLM-Backend", backend_name)
        self.end_headers()
        norm = None
        try:
            norm = _FTNormalizer(request["material"], _ft_schemas_from_tools(request.get("tools")))
        except Exception as e:
            log(f"{method} {path} -> {backend_name} feyntalk: normaliser not built: {e}")
        sse_buf = b""
        bytes_out = 0

        def _relay(chunk):
            nonlocal sse_buf, bytes_out
            self._client_write(chunk)
            self._client_flush()
            bytes_out += len(chunk)
            if norm is not None:
                sse_buf, events = self._events_from_buffer(sse_buf, chunk)
                for event in events:
                    if event is None:
                        continue
                    if event.get("type") in ("arcint:token", "arcint:span"):
                        try:
                            norm.feed(event)
                        except _FTSpanError as e:
                            log(f"{method} {path} -> {backend_name} feyntalk passthrough: {e}")

        try:
            if erster:
                _relay(erster)
            while True:
                chunk = buf.get()
                if chunk is None:
                    if reader_error:
                        err = reader_error[0]
                        if isinstance(err, TimeoutError):
                            raise BackendTimeout(
                                f"kein Byte in {READ_TIMEOUT}s: {err}", True)
                        raise _lesefaden_abriss(err, gone_evt)
                    break
                _relay(chunk)
        finally:
            _stop_reader(buf, sock, reader_thread)
        if norm is not None:
            try:
                norm.finish()
            except _FTSpanError as e:
                log(f"{method} {path} -> {backend_name} feyntalk passthrough end: {e}")
        log(f"{method} {path} -> {backend_name} feyntalk responses status={resp.status} "
            f"ttfb={time.monotonic()-t_try:.2f}s total={time.monotonic()-t0:.2f}s "
            f"bytes={bytes_out} think={request.get('think', 'off')}{hint}")

    def _stream_response(self, resp, backend_name, t0, t_try, hint, method, path, is_streaming=False, gone_evt=None,
                         salvage_tools=None, chatml_guard=False, sock=None):
        # Erst das erste Byte lesen, dann die Kopfzeilen an den Anrufer geben:
        # solange nichts raus ist, kann ein stockendes Backend noch durch ein
        # anderes ersetzt werden. Die kurze Failover-Frist gilt hier fuer
        # Verbindung und erstes Byte.
        erster = None
        if is_streaming:
            try:
                erster = resp.read1(65536)
            except TimeoutError as e:
                raise BackendTimeout(
                    f"kein erstes Byte in {CONNECT_TIMEOUT}s: {e}", False)
            except AttributeError:
                raise StromAbgeschnitten(
                    "0 Bytes gingen raus, dann war die Verbindung zu")
        # Ab dem ersten Byte gilt die lange Frist -- nicht die kurze Frist des
        # Failovers fuer den ganzen Strom.
        if sock is not None:
            try:
                sock.settimeout(READ_TIMEOUT)
            except OSError:
                pass
        # Der Text-Relaisweg schrieb bisher read1 und wfile.write im selben
        # Faden: ein blockierender Anrufer (bis CLIENT_WRITE_TIMEOUT) hielt das
        # Upstream-Lesen an und die Engine lief in ihre Schreibfrist. Jetzt
        # fuellt ein eigener Faden einen bytebegrenzten Puffer.
        buf = _UpstreamBuffer(STREAM_QUEUE_MAX_BYTES)

        def _reader_warn():
            log(f"{method} {path} -> {backend_name} feyntalk: Leser laeuft dem "
                f"Schreibpfad davon ({STREAM_QUEUE_MAX_BYTES // (1024 * 1024)} MiB "
                f"gepuffert), Rueckstau - kein Token verworfen")

        reader_error, reader_thread = _drain_reader(resp, buf, _reader_warn)
        self._backend_reader = reader_thread
        self._backend_buf = buf
        t_hdr = time.monotonic()
        self.send_response(resp.status)
        content_type = resp.getheader("Content-Type", "").lower()
        # Probe nur fuer echte Event-Stroeme: ein gestreamter Auftrag, der als
        # JSON beantwortet wird, darf keine Kommentare bekommen.
        self._client_streaming = "text/event-stream" in content_type
        # For SSE/streaming responses, NEVER forward Content-Length —
        # it breaks chunked framing and causes clients to hang waiting
        # for exactly N bytes that arrive incrementally.
        if is_streaming or "text/event-stream" in content_type:
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            # Don't forward Content-Length or Transfer-Encoding
        else:
            for hname in ("Content-Type", "Content-Encoding", "Content-Length"):
                v = resp.getheader(hname)
                if v:
                    self.send_header(hname, v)
        self.send_header("X-LLM-Backend", backend_name)
        self.end_headers()
        bytes_out = 0
        # </think>-Naht reparieren (siehe ReasoningSeam oben). Nur im Streaming-Pfad;
        # ausserhalb bleibt der Relaisweg unveraendert byte-rein.
        seam = ReasoningSeam() if is_streaming and getattr(self, "_req_seam", False) else None
        # Tool-Aufrufe als Text (siehe ToolTextSalvage): nur Strom, nur mit Tools in der Anfrage.
        salvage = ToolTextSalvage(salvage_tools, getattr(self, "_req_parallel", True)) \
            if is_streaming and salvage_tools else None
        # ChatML-Marker im Antworttext (siehe ChatMLGuard): ersetzt den Stop-String fuer Stroeme.
        guard = ChatMLGuard() if is_streaming and chatml_guard else None
        # Was der Klient als Ende sieht (finish_reason, loop_guard) - fuer die Schlusszeile.
        signale = StromSignale() if is_streaming else None

        def _relay(data):
            nonlocal bytes_out
            if seam is not None:
                data = seam.feed(data)
            if guard is not None and data:
                data = guard.feed(data)
            if salvage is not None and data:
                data = salvage.feed(data)
            if not data:
                return
            if signale is not None:
                signale.feed(data)
            self._client_write(data)
            self._client_flush()
            bytes_out += len(data)

        # Sendezeitsperre auf dem Anrufer-Socket: hoert er auf zu lesen, ohne die
        # Verbindung zu schliessen, laeuft das TCP-Fenster voll und der naechste
        # Schreibvorgang blockiert. Ohne Zeitsperre wartet der Proxy dort
        # unbegrenzt und das Backend rechnet weiter fuer niemanden.
        vorherige_frist = None
        abgeschnitten = None
        try:
            vorherige_frist = self.connection.gettimeout()
            self.connection.settimeout(CLIENT_WRITE_TIMEOUT)
        except OSError:
            pass
        try:
            if erster:
                _relay(erster)
            while not (guard is not None and guard.ended):
                # read1() holt jetzt der Lesefaden; hier wird nur abgeholt und
                # verarbeitet, damit ein stockender Schreibvorgang den Upstream
                # nicht mehr blockiert.
                chunk = buf.get()
                if chunk is None:
                    if reader_error:
                        err = reader_error[0]
                        if isinstance(err, TimeoutError):
                            try:
                                self.connection.settimeout(vorherige_frist)
                            except OSError:
                                pass
                            raise BackendTimeout(
                                f"kein Byte in {READ_TIMEOUT}s: {err}", True)
                        if isinstance(err, AttributeError):
                            abgeschnitten = StromAbgeschnitten(
                                f"{bytes_out} Bytes gingen raus, dann war die Verbindung zu")
                            break
                        raise err
                    break
                _relay(chunk)
                if guard is not None and guard.ended:
                    break                              # Zug am ChatML-Marker beendet: Backend kappen
        except TimeoutError:
            # Nicht das Backend ist langsam - der Anrufer holt nicht mehr ab.
            log(f"{method} {path} -> {backend_name} CLIENT-STALLED "
                f"(nichts abgeholt in {CLIENT_WRITE_TIMEOUT}s, Puffer voll) "
                f"nach {time.monotonic()-t_try:.2f}s, {bytes_out} Bytes - Backend wird gekappt")
        except http.client.IncompleteRead as e:
            if e.partial:
                try:
                    _relay(e.partial)
                except (OSError, ValueError):
                    pass
        except (OSError, ValueError):
            pass  # client disconnected, or backend conn closed by the cancel watchdog
        except AttributeError:
            # http.client hat sein fp verloren, weil der Wachhund zwischen
            # _get_chunk_left() und dem Lesen zugemacht hat. Hier nur merken:
            # erst muss die Naht geleert und die Frist zurueckgesetzt werden,
            # sonst bleibt Zurueckgehaltenes stehen und die Verbindung behaelt
            # die Sendezeitsperre. Geworfen wird unten, mit Bytes im Text.
            abgeschnitten = StromAbgeschnitten(
                f"{bytes_out} Bytes gingen raus, dann war die Verbindung zu")
        finally:
            _stop_reader(buf, sock, reader_thread)
        if seam is not None:
            # Was die Naht-Erkennung noch zurueckhaelt, darf nicht verschluckt werden.
            try:
                rest = seam.flush()
                if guard is not None:
                    rest = (guard.feed(rest) if rest else b"") + guard.flush()
                if salvage is not None:
                    rest = salvage.feed(rest) + salvage.flush() if rest else salvage.flush()
                if rest:
                    if signale is not None:
                        signale.feed(rest)
                    self._client_write(rest)
                    self._client_flush()
                    bytes_out += len(rest)
            except (OSError, ValueError):
                pass
        # Frist zuruecksetzen: die Verbindung wird per keep-alive weiterbenutzt.
        try:
            self.connection.settimeout(vorherige_frist)
        except OSError:
            pass
        if abgeschnitten is None and gone_evt is not None and gone_evt.is_set() \
                and not (guard is not None and guard.cut):   # ein gewollter Schnitt ist kein Auflegen
            # Der Wachhund hat das Backend per shutdown() gekappt; read1() lieferte
            # deshalb ein sauberes EOF statt eines Fehlers. Ohne diese Pruefung
            # stuende gleich eine Erfolgszeile "status=200 bytes=0" im Protokoll.
            abgeschnitten = StromAbgeschnitten(
                f"{bytes_out} Bytes gingen raus, dann legte der Anrufer auf")
        if abgeschnitten is not None:
            # Keine Erfolgszeile fuer einen Strom, der abgerissen ist:
            # _try_backend protokolliert ihn mit Grund und gibt "aborted"
            # zurueck, damit _proxy_local nicht weiterreicht.
            raise abgeschnitten
        dt_total = time.monotonic() - t0
        dt_ttfb = t_hdr - t_try
        tt = f" tooltext={salvage.result}" if salvage is not None and salvage.result else ""
        if guard is not None and guard.cut:
            tt += " chatml=cut"
        if signale is not None:
            tt += signale.zeile()
        log(f"{method} {path} -> {backend_name} status={resp.status} ttfb={dt_ttfb:.2f}s total={dt_total:.2f}s bytes={bytes_out} stream={is_streaming}{hint}{tt}")

    def _client_write(self, data):
        """An den Anrufer schreiben; ein Fehler dort ist kein Backend-Fehler.

        `_client_at_frame_end` merkt unter der Sperre, ob dieser Schreibvorgang
        auf einer SSE-Rahmengrenze endet. Nur dann darf der Wachhund sein
        Kommentar dazwischensetzen; die Relais-Wege schreiben rohe
        read1-Stuecke mit beliebigen TCP-Grenzen."""
        lock = getattr(self, "_client_lock", None)
        if lock is None:
            lock = self._client_lock = threading.Lock()
        with lock:
            try:
                self.wfile.write(data)
            except (OSError, ValueError) as e:
                raise ClientGone(f"Schreibfehler an den Anrufer: {type(e).__name__}: {e}") from e
            # LF- und CRLF-Rahmen; ein ueber zwei Stuecke zerrissenes Ende bleibt
            # False (vorsichtig: dann probt der Wachhund in diesem Umlauf nicht).
            self._client_at_frame_end = data.endswith((b"\n\n", b"\r\n\r\n"))
            wrote = getattr(self, "_client_wrote", None)
            if wrote is not None:
                wrote.set()

    def _client_flush(self):
        lock = getattr(self, "_client_lock", None)
        if lock is None:
            lock = self._client_lock = threading.Lock()
        with lock:
            try:
                self.wfile.flush()
            except (OSError, ValueError) as e:
                raise ClientGone(f"Schreibfehler an den Anrufer: {type(e).__name__}: {e}") from e

    def _close_backend(self, conn):
        """Backend-Verbindung genau einmal schliessen, serialisiert.

        Der Wachhund und der Hauptfaden koennen beide hier ankommen (Auflegen
        waehrend eines laufenden Stroms). Ohne Sperre schliessen zwei Faden
        denselben HTTPResponse; der zweite `_close_conn` faellt an
        `None.close`. Cloud-/Zen-Verbindungen haben keine solche Sperre -- dort
        schliesst nur ein Faden, der Aufruf bleibt der direkte close()."""
        if conn is None:
            return
        lock = getattr(self, "_backend_close_lock", None)
        if lock is None:
            try:
                conn.close()
            except Exception:
                pass
            return
        with lock:
            try:
                conn.close()
            except Exception:
                pass

    def flush_headers(self):
        """Kopfzeilen ueber den Client-Schreibweg: ein Fehler dort ist
        ClientGone (CLIENT-CANCELLED), kein Backend-FAIL."""
        if hasattr(self, "_headers_buffer"):
            data = b"".join(self._headers_buffer)
            self._headers_buffer = []
            self._client_write(data)
            self._client_flush()

    def _send_error(self, status, body, headers=None):
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self._client_write(body)
            self._client_flush()
        except (BrokenPipeError, ConnectionResetError, ClientGone):
            pass

    def _client_watchdog(self, get_conn, gone_evt, stop_evt):
        """Watch the CLIENT socket while a backend request is in flight. If the client
        hangs up (pi-agent stop / tab close), close the backend connection so the backend
        aborts generation and the admission slot is freed — instead of running a single-slot
        backend to completion for a request nobody is waiting on. Covers both the streaming
        read loop and the blocking non-streaming getresponse(). Returns the started thread.

        Ein FIN ist nicht eindeutig: ein voller Schluss sieht auf der Leseseite
        genauso aus wie ein halber Schluss, bei dem der Client weiterliest. Vor
        dem ersten Schreibvorgang (kein Strom, kalte Vorfuellung) gilt ein FIN
        als Auflegen. Nach dem ersten Schreibvorgang eines Event-Stroms probt
        der Faden etwa zweimal je Sekunde mit einem SSE-Kommentar; der zweite
        Schreibvorgang nach einem FIN faellt als RST auf und macht den vollen
        Schluss in ~1 s sichtbar, ohne den halben Schluss zu kappen."""
        cs = self.connection
        lock = getattr(self, "_client_lock", None)
        wrote = getattr(self, "_client_wrote", None)

        def _declare_gone():
            gone_evt.at = time.monotonic()  # wann der Anrufer auflegte (fuer das Protokoll)
            gone_evt.set()
            for _ in range(100):            # conn may not exist yet; wait up to ~10s
                if stop_evt.is_set():
                    return
                c = get_conn()
                if c is not None:
                    # close() allein reicht NICHT: http.client haelt ueber makefile()
                    # eine zweite Referenz, der fd bleibt offen und der Hauptthread
                    # haengt in recv(), bis das Backend sein erstes Byte schickt --
                    # bei einer kalten Vorfuellung Minuten, in denen das Backend fuer
                    # niemanden rechnet. shutdown() reisst den blockierten Aufruf
                    # sofort (gemessen 2026-09-22: 5 s+ haengend -> 0,0 s).
                    # Danach dem Lesefaden beitreten: erst dann darf geschlossen
                    # werden, sonst treffen zwei `_close_conn` aufeinander und
                    # der zweite faellt an `None.close`.
                    try:
                        sk = getattr(c, "sock", None)
                        if sk is not None:
                            sk.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                    buf = getattr(self, "_backend_buf", None)
                    if buf is not None:
                        buf.stop()          # weckt einen in put parkenden Leser
                    t = getattr(self, "_backend_reader", None)
                    if t is not None and t is not threading.current_thread():
                        t.join(timeout=CONNECT_TIMEOUT)
                    self._close_backend(c)    # unter der Sperre, genau einmal
                    return
                time.sleep(0.1)

        def _run():
            while not stop_evt.is_set():
                try:
                    r, _, _ = select.select([cs], [], [], 1.0)
                except (OSError, ValueError):
                    return
                if stop_evt.is_set():
                    return
                if not r:
                    continue
                try:
                    peek = cs.recv(1, socket.MSG_PEEK)
                except OSError:
                    _declare_gone()             # harter Socket-Fehler (RST): sicher weg
                    return
                if peek:
                    continue                    # client sent data (pipelined) — still there
                # FIN auf der Leseseite. Vor dem ersten Schreibvorgang (kein
                # Strom, kalte Vorfuellung) ist das ein Auflegen; danach kann
                # es ein halber Schluss sein, bei dem der Client weiterliest.
                # Die Flagge kommt aus dem tatsaechlichen Antwort-Content-Type,
                # nicht aus dem Wunsch des Anrufers: ein gestreamter Auftrag,
                # der als JSON beantwortet wird, darf keine Kommentare bekommen.
                if not bool(getattr(self, "_client_streaming", False)) \
                        or wrote is None or not wrote.is_set():
                    _declare_gone()
                    return
                # Nach dem ersten Schreibvorgang: ein SSE-Kommentar wird von
                # jedem Client verworfen. Der zweite Schreibvorgang nach einem
                # FIN faellt als RST auf und macht den vollen Schluss sichtbar;
                # der Faden bleibt fuer einen spaeteren RST am Leben.
                # Ein FIN zwischen dem letzten Schluss-Rahmen und stop.set()
                # darf noch einen Kommentar anhaengen - fuer SSE-Clients legal.
                if stop_evt.wait(0.5):
                    return
                if lock is None:
                    continue                    # ohne Sperre keine sichere Probe
                probe_failed = False
                with lock:
                    if getattr(self, "_client_at_frame_end", False):
                        try:
                            self.wfile.write(b":\n\n")
                            self.wfile.flush()
                        except (OSError, ValueError):
                            probe_failed = True
                    # sonst mitten in einem Rahmen: diesen Umlauf ueberspringen
                # Ausserhalb der Sperre: _declare_gone wartet bis ~10 s auf die
                # Backend-Verbindung und darf den Schreibpfad so lange nicht sperren.
                if probe_failed:
                    _declare_gone()
                    return

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        return t

    def _try_backend(self, backend, method, path, body, fwd, t0, hint, is_last, is_streaming=False, feyntalk=None):
        name, host, port = backend
        slot = _admit(name)
        if not slot:
            log(f"{method} {path} -> {name} BUSY (admission gate, slots={_backend_slots.get(name)}) total={time.monotonic()-t0:.2f}s{hint}")
            return "busy"
        t_try = time.monotonic()
        conn = None
        gone = threading.Event()
        stop = threading.Event()
        # Zustand fuer den Wachhund: bis zum ersten Schreibvorgang ist ein FIN
        # ein Auflegen; danach (nur bei einem Event-Stream) probt er mit einem
        # SSE-Kommentar, weil erst ein fehlgeschlagener Schreibvorgang einen
        # vollen Schluss verraet.
        self._client_lock = threading.Lock()
        self._client_wrote = threading.Event()
        self._client_streaming = is_streaming
        # Der Lesefaden dieses Versuchs und die Sperre um den Verbindungsschluss:
        # der Wachhund wartet auf den Faden, bevor er schliesst, und beide
        # Schluesse (Wachhund, Hauptfaden) laufen unter derselben Sperre.
        self._backend_reader = None
        self._backend_buf = None
        self._backend_close_lock = threading.Lock()
        self._client_watchdog(lambda: conn, gone, stop)   # frees the slot if the client bails
        # Sendezeitsperre auch auf dem Token-Pfad: ein Client, der halb
        # schliesst und dann nicht mehr liest, blockiert den Schreibvorgang;
        # die Sperre bricht ihn ab (der Wachhund kappt bei halbem Schluss
        # nicht mehr). _stream_response setzt/loescht sie zusaetzlich selbst.
        client_frist = None
        client_frist_gesetzt = False
        try:
            client_frist = self.connection.gettimeout()
            self.connection.settimeout(CLIENT_WRITE_TIMEOUT)
            client_frist_gesetzt = True
        except OSError:
            pass
        strict = feyntalk == "tokens"
        self._ft_path = None
        try:
            token_request = _feyntalk_prepare(backend, method, path, body, feyntalk,
                                               auth=fwd.get("Authorization"))
            if gone.is_set():
                # Der Anrufer hat waehrend der Vorbereitung aufgelegt: nichts
                # an die Engine schicken. Die finally gibt den Slot frei.
                log(f"{method} {path} -> {name} CLIENT-CANCELLED before send")
                return "aborted"
            conn = http.client.HTTPConnection(host, port, timeout=CONNECT_TIMEOUT if not is_last else READ_TIMEOUT)
            send_body = token_request["body"] if token_request else body
            send_path = token_request["path"] if token_request else path
            fwd_use = dict(fwd)
            if token_request:
                fwd_use["Content-Length"] = str(len(send_body))
                fwd_use["Accept"] = "text/event-stream"
            conn.request(method, send_path, body=send_body, headers=fwd_use)
            # http.client setzt conn.sock nach getresponse() auf None; die Frist
            # fuer den Rest des Stroms muss vorher am echten Socket stehen.
            backend_sock = conn.sock
            resp = conn.getresponse()
            if resp is None:
                # Wettlauf mit dem eigenen Wachhund: legt der Anrufer auf,
                # schliesst _client_watchdog die Verbindung - faellt das mit
                # getresponse() zusammen, liefert http.client None statt zu
                # werfen. Ohne diese Pruefung stirbt der Aufruf gleich darauf
                # an `NoneType has no attribute read1` und wird als
                # unerwarteter Fehler protokolliert (73 mal bis 2026-08-07),
                # statt als das, was er ist. Unten steht die Behandlung
                # bereits: gone.is_set() -> "aborted", sonst "down".
                raise ConnectionResetError(
                    "Backend-Verbindung waehrend getresponse() geschlossen")
            self._backend_rid_pruefen(resp, name, method, path, hint)
            kind_409 = None
            if strict and token_request and resp.status == 409:
                kind_409 = _feyntalk_409_kind(resp.read())
            if kind_409 == "material":
                # The material moved, which a unit restart does legitimately: fetch it again
                # and try this turn once more in token mode. A second 409 fails below.
                _feyntalk_drop_cache(name)
                log(f"{method} {path} -> {name} feyntalk 409 renderer hash moved; "
                    f"dropping capability cache, retrying once in token mode (strict)")
                self._close_backend(conn)
                token_request = _feyntalk_prepare(backend, method, path, body, feyntalk,
                                                   auth=fwd.get("Authorization"))
                send_body = token_request["body"]
                fwd_use["Content-Length"] = str(len(send_body))
                conn = http.client.HTTPConnection(host, port,
                                                  timeout=CONNECT_TIMEOUT if not is_last else READ_TIMEOUT)
                conn.request(method, token_request["path"], body=send_body, headers=fwd_use)
                backend_sock = conn.sock
                resp = conn.getresponse()
                if resp is None:
                    raise ConnectionResetError(
                        "Backend-Verbindung waehrend getresponse() geschlossen")
                self._backend_rid_pruefen(resp, name, method, path, hint)
                kind_409 = "moved again" if resp.status == 409 else None
            if kind_409 is not None or (strict and token_request and resp.status == 409):
                wort = {"moved again": "renderer hash moved twice", "prompt": "prompt mismatch"}.get(
                    kind_409, "renderer mismatch")
                log(f"{method} {path} -> {name} feyntalk 409 {wort}; failing this turn (strict){hint}")
                raise _FeyntalkStrict(502, "renderer_mismatch",
                                      f"backend '{name}' refused the token-mode render: {wort}")
            if token_request and resp.status == 409:
                # The engine refused before any token and before the slot was
                # used for generation: re-send the same turn once as a text
                # request. This is the D6 fallback, logged, never silent.
                # Only names without a `-ft` alias get here.
                _feyntalk_count(name, "fallbacks")
                err_body = resp.read()
                kind = _feyntalk_409_kind(err_body)
                if kind == "material":
                    # Das Material hat sich bewegt: die Faehigkeits- und
                    # Material-Caches dieses Backends verwerfen, damit der
                    # naechste Zug /props und das Material neu holt.
                    _feyntalk_drop_cache(name)
                    log(f"{method} {path} -> {name} feyntalk 409 renderer hash moved; "
                        f"dropping capability cache, retrying this turn once in text mode")
                else:
                    wort = "prompt mismatch" if kind == "prompt" else "renderer mismatch"
                    log(f"{method} {path} -> {name} feyntalk 409 {wort}; "
                        f"keeping capability cache, retrying this turn once in text mode")
                self._close_backend(conn)
                conn = http.client.HTTPConnection(host, port,
                                                  timeout=CONNECT_TIMEOUT if not is_last else READ_TIMEOUT)
                retry = token_request.get("retry_body") if token_request else None
                retry_headers = dict(fwd)
                if retry is not None:
                    retry_headers["Content-Length"] = str(len(retry))
                conn.request(method, path, body=retry if retry is not None else body,
                             headers=retry_headers)
                backend_sock = conn.sock
                resp = conn.getresponse()
                if resp is None:
                    raise ConnectionResetError(
                        "Backend-Verbindung waehrend getresponse() geschlossen")
                self._backend_rid_pruefen(resp, name, method, path, hint)
                token_request = None
            self._ft_path = "tokens" if token_request else "text"
            if token_request:
                _feyntalk_count(name, "tokens")
                if token_request["kind"] == "chat":
                    self._stream_chat_from_tokens(resp, name, t0, t_try, hint, method, path,
                                                  token_request, sock=backend_sock, gone_evt=gone)
                else:
                    self._stream_responses_from_tokens(resp, name, t0, t_try, hint, method, path,
                                                       token_request, sock=backend_sock, gone_evt=gone)
            else:
                self._stream_response(resp, name, t0, t_try, hint, method, path, is_streaming=is_streaming, gone_evt=gone,
                                      salvage_tools=getattr(self, "_req_tools", None),
                                      chatml_guard=getattr(self, "_chatml_guard", False), sock=backend_sock)
            self._close_backend(conn)
            return "ok"
        except _FeyntalkStrict as e:
            self._close_backend(conn)
            _feyntalk_count(name, "errors")
            self._feyntalk_strict = e
            log(f"{method} {path} -> {name} {e.status} {e.code}: {e.message}{hint}")
            return "strict"
        except _FeyntalkBadRequest as e:
            # Ungueltiges preserve_thinking des Anrufers: 400, nicht an das
            # Backend weiterleiten und nicht in den Textmode fallen.
            self._close_backend(conn)
            self._feyntalk_bad_request = str(e)
            log(f"{method} {path} -> {name} 400 {e}")
            return "badrequest"
        except ClientGone as e:
            # Der Schreibvorgang an den Anrufer ist gescheitert: der Anrufer
            # ist weg, nicht das Backend. Kein Weiterreichen an ein anderes
            # Backend (die Kopfzeilen sind schon raus) und keine FAIL-Zeile.
            self._close_backend(conn)
            weg = getattr(gone, "at", None)
            wann = f"Anrufer weg nach {weg - t_try:.2f}s, " if weg else ""
            log(f"{method} {path} -> {name} CLIENT-CANCELLED, {wann}backend cut loose, "
                f"slot freed after {time.monotonic()-t_try:.2f}s ({e})")
            return "aborted"
        except BackendTimeout as e:
            self._close_backend(conn)
            if gone.is_set():   # der Anrufer hat aufgelegt, nicht das Backend
                weg = getattr(gone, "at", None)
                wann = f"Anrufer weg nach {weg - t_try:.2f}s, " if weg else ""
                log(f"{method} {path} -> {name} CLIENT-CANCELLED, {wann}backend cut loose, slot freed after {time.monotonic()-t_try:.2f}s")
                return "aborted"
            dt = time.monotonic() - t_try
            log(f"{method} {path} -> {name} BACKEND-TIMEOUT {dt:.2f}s "
                f"({e}), gesendet={'ja' if e.gesendet else 'nein'}{hint}")
            return "aborted" if e.gesendet else "down"
        except StromAbgeschnitten as e:
            # Muss vor der allgemeinen Klausel stehen: ConnectionResetError ist
            # ein OSError. Die Kopfzeilen sind beim Anrufer, also kein
            # Weiterreichen und kein Fehlerrumpf - nur eine ehrliche Zeile.
            self._close_backend(conn)
            grund = "CLIENT-CANCELLED" if gone.is_set() else "STREAM-CUT"
            weg = getattr(gone, "at", None)
            wann = f"Anrufer weg nach {weg - t_try:.2f}s, " if weg else ""
            log(f"{method} {path} -> {name} {grund} mitten im Strom: {wann}Backend gekappt nach "
                f"{time.monotonic()-t_try:.2f}s ({e}){hint}")
            return "aborted"
        except (ConnectionRefusedError, TimeoutError, OSError, http.client.HTTPException, ValueError) as e:
            self._close_backend(conn)
            if gone.is_set():   # the watchdog closed conn: client cancelled, not a backend fault
                weg = getattr(gone, "at", None)
                wann = f"Anrufer weg nach {weg - t_try:.2f}s, " if weg else ""
                log(f"{method} {path} -> {name} CLIENT-CANCELLED, {wann}backend cut loose, slot freed after {time.monotonic()-t_try:.2f}s")
                return "aborted"
            dt = time.monotonic() - t_try
            if isinstance(e, TimeoutError):
                log(f"{method} {path} -> {name} BACKEND-TIMEOUT {dt:.2f}s "
                    f"({type(e).__name__}: {e}){hint}")
            else:
                log(f"{method} {path} -> {name} FAIL {type(e).__name__} {dt:.2f}s")
            return "down"
        finally:
            stop.set()
            if client_frist_gesetzt:
                try:
                    self.connection.settimeout(client_frist)
                except OSError:
                    pass
            _release(name, slot)

    def _proxy_local(self, method, path, body, hint, t0, model=None, is_streaming=False, feyntalk=None):
        fwd = self._fwd_headers_local(len(body))
        backends = list(LOCAL_BACKENDS)
        # A Responses body has no `messages`, so _proxy's Chat parse leaves the
        # model empty. Only with feyntalk configured (or an alias) do we read it
        # here and resolve the alias; without either the old first-up routing is
        # untouched.
        if model is None and path in RESPONSES_PATHS and (
                any(mode != "off" for mode in BACKEND_FEYNTALK.values()) or MODEL_ALIASES):
            raw = _feyntalk_model_of(body) or ""
            raw2, target, als = _apply_model_alias(raw, _anfrage_id(raw))
            if raw2 != raw:
                j = json.loads(body)
                j["model"] = raw2
                body = json.dumps(j).encode()
                fwd = self._fwd_headers_local(len(body))
            if als is not None:
                feyntalk = als.get("feyntalk")
                # Der Token-Logpfad der Responses-Route traegt nur das
                # umgeschriebene Ziel; der Alias gehoert in die Zeile.
                hint += f" alias={raw} -> {target}"
            model = target or None

        chosen = None
        if model:
            chosen = find_backend_for_model(model)
            if chosen is None:
                available = set()
                for b in backends:
                    for m in (discover(b) or []):
                        available.add(m)
                # Die Schleife eben hat jedes Backend angefasst, also ist
                # _backends_up frisch. Wer fehlt, war nicht erreichbar.
                with _backends_up_lock:
                    stumm = sorted(b[0] for b in backends if b[0] not in _backends_up)
                if stumm:
                    # Wir wissen nicht, dass es das Modell nicht gibt - wir
                    # konnten nur nicht nachsehen. Das ist ein Ausfall (503),
                    # keine Aussage ueber den Modellnamen (404).
                    payload = json.dumps({"error": {
                        "message": (f"model '{model}' not found, but backend(s) "
                                    f"{', '.join(stumm)} are unreachable - "
                                    f"cannot tell whether the model is absent"),
                        "type": "backend_unavailable",
                        "unreachable": stumm,
                        "available": sorted(available),
                    }}).encode()
                    self._send_error(503, payload)
                    log(f"{method} {path} -> model={model!r} 503 (backend down: {','.join(stumm)}) total={time.monotonic()-t0:.2f}s{hint}")
                    return
                payload = json.dumps({"error": {
                    "message": f"model '{model}' not available on any local backend",
                    "type": "model_not_found",
                    "available": sorted(available),
                }}).encode()
                self._send_error(404, payload)
                log(f"{method} {path} -> model={model!r} 404 (not in any local backend) total={time.monotonic()-t0:.2f}s{hint}")
                return
        if chosen is not None:
            r = self._try_backend(chosen, method, path, body, fwd, t0, hint + " (routed)", is_last=True, is_streaming=is_streaming, feyntalk=feyntalk)
            if r == "ok":
                return
            if r == "aborted":
                return          # client hung up; nothing to send, don't fail over
            if r == "strict":
                self._send_feyntalk_strict()
                return
            if r == "badrequest":
                self._send_error(400, json.dumps({"error": {
                    "message": getattr(self, "_feyntalk_bad_request",
                                       "invalid preserve_thinking"),
                    "type": "invalid_request_error"}}).encode())
                return
            if r == "busy":
                self._send_error(503, b'{"error":{"message":"backend busy (admission gate), retry shortly","type":"backend_busy"}}')
                return
            # Weiterreichen NUR an Backends, die dieses Modell auch fuehren.
            # Frueher ging es an die volle Kette: llama.cpp prueft das
            # model-Feld nicht und antwortet mit dem geladenen Modell, ein
            # DS4-Auftrag bekam stillschweigend eine Qwen-Antwort.
            geeignet = [b for b in backends
                        if b is not chosen and model in (discover(b) or [])]
            if not geeignet:
                log(f"{method} {path} -> {chosen[0]} routed-FAIL, kein anderes Backend fuehrt {model!r} total={time.monotonic()-t0:.2f}s{hint}")
                self._send_error(502, json.dumps({"error": {
                    "message": (f"backend '{chosen[0]}' serving model "
                                f"'{model}' is unavailable, and no other local "
                                f"backend serves this model"),
                    "type": "backend_unavailable",
                    "model": model,
                }}).encode())
                return
            log(f"{method} {path} -> {chosen[0]} routed-FAIL, weiter an {[b[0] for b in geeignet]}{hint}")
            chain = geeignet
        else:
            chain = [b for b in backends if b is not chosen]
        busy_seen = False
        for i, b in enumerate(chain):
            is_last = (i == len(chain) - 1)
            r = self._try_backend(b, method, path, body, fwd, t0, hint, is_last, is_streaming=is_streaming, feyntalk=feyntalk)
            if r == "ok":
                return
            if r == "aborted":
                return          # client hung up; nothing to send, don't fail over
            if r == "strict":
                self._send_feyntalk_strict()
                return
            if r == "badrequest":
                self._send_error(400, json.dumps({"error": {
                    "message": getattr(self, "_feyntalk_bad_request",
                                       "invalid preserve_thinking"),
                    "type": "invalid_request_error"}}).encode())
                return
            if r == "busy":
                busy_seen = True
        if busy_seen:
            self._send_error(503, b'{"error":{"message":"all local backends busy (admission gate), retry shortly","type":"backend_busy"}}')
            log(f"{method} {path} -> ALL_BUSY total={time.monotonic()-t0:.2f}s{hint}")
        else:
            self._send_error(502, b'{"error":"all local backends down"}')
            log(f"{method} {path} -> ALL_DOWN total={time.monotonic()-t0:.2f}s{hint}")

    def _send_feyntalk_strict(self):
        e = self._feyntalk_strict
        self._send_error(e.status, json.dumps({"error": {
            "message": e.message, "type": "feyntalk_error", "code": e.code}}).encode())

    def _proxy_zen(self, method, path, body, hint, t0, is_streaming=False):
        upstream_path = "/zen/v1/chat/completions"
        modell = ""
        try:
            j = json.loads(body)
            raw_model = j.get("model", "")
            modell = raw_model
            if raw_model.startswith("opencode/"):
                j["model"] = raw_model[len("opencode/"):]
            for key in ("max_tokens", "max_completion_tokens"):
                val = j.get(key)
                if val is not None and (not isinstance(val, int) or val < 1):
                    j.pop(key, None)
            # Zen API does not support "developer" role; map to "system"
            for msg in j.get("messages", []):
                if msg.get("role") == "developer":
                    msg["role"] = "system"
            body = json.dumps(j).encode()
        except Exception:
            pass
        fwd = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Accept-Encoding": "identity",
            "X-Request-Id": self._rid_jetzt(),
        }
        t_try = time.monotonic()
        conn = None
        try:
            conn = http.client.HTTPSConnection(
                ZEN_HOST, ZEN_PORT,
                timeout=READ_TIMEOUT,
                context=ssl.create_default_context(),
            )
            conn.request(method, upstream_path, body=body, headers=fwd)
            resp = conn.getresponse()
            self._backend_rid_pruefen(resp, "opencode-zen", method, path, hint)
            if resp.status >= 400:
                resp_body = resp.read()
                # Der Anbieterstatus ist eine Aussage: 429 bleibt 429 (mit
                # Retry-After), 4xx bleibt 4xx. Nur ein 5xx ist ein Proxy-Fehler.
                status = resp.status if resp.status < 500 else 502
                kopf = {}
                if status == 429:
                    ra = resp.getheader("Retry-After")
                    if ra:
                        kopf["Retry-After"] = ra
                self._send_error(status, resp_body, headers=kopf)
                log(f"{method} {path} -> opencode-zen status={resp.status} "
                    f"{_anfrage_kurz(body, modell)} ttfb={time.monotonic()-t_try:.2f}s")
                self._close_backend(conn)
                return
            self._stream_response(resp, "opencode-zen", t0, t_try, hint, method, path, is_streaming=is_streaming)
            self._close_backend(conn)
        except Exception as e:
            err = json.dumps({"error": f"opencode-zen: {type(e).__name__}: {e}"}).encode()
            self._send_error(502, err)
            log(f"{method} {path} -> opencode-zen FAIL {type(e).__name__}: {e} "
                f"{_anfrage_kurz(body, modell)}")
            self._close_backend(conn)

    def _proxy_cloud(self, method, path, body, hint, t0, is_streaming=False):
        if not OR_KEY:
            self._send_error(500, b'{"error":"OPENROUTER_API_KEY not set"}')
            log(f"{method} {path} -> openrouter NO_KEY")
            return
        upstream_path = "/api" + path if path.startswith("/v1") else path
        fwd = self._fwd_headers_cloud(len(body))
        t_try = time.monotonic()
        conn = None
        try:
            conn = http.client.HTTPSConnection(
                OPENROUTER_HOST, OPENROUTER_PORT,
                timeout=READ_TIMEOUT,
                context=ssl.create_default_context(),
            )
            conn.request(method, upstream_path, body=body, headers=fwd)
            resp = conn.getresponse()
            self._backend_rid_pruefen(resp, "openrouter", method, path, hint)
            self._stream_response(resp, "openrouter", t0, t_try, hint, method, path, is_streaming=is_streaming)
            self._close_backend(conn)
        except Exception as e:
            err = json.dumps({"error": f"openrouter: {type(e).__name__}: {e}"}).encode()
            self._send_error(502, err)
            log(f"{method} {path} -> openrouter FAIL {type(e).__name__}: {e}{hint}")
            self._close_backend(conn)

    def _handle_models(self, t0):
        mode = _aktiver_modus()
        backends = list(LOCAL_BACKENDS)
        # Discovery parallel vorwaermen; die Schleife unten liest dann nur noch
        # frischen Cache. Seriell kostete der kalte Fall die SUMME aller Proben
        # (gemessen 2026-08-23: ~12,5 s bei 8 Backends, OWUI-Spinner) -- parallel
        # deckelt die langsamste Einzelprobe (CONNECT_TIMEOUT eines toten Hosts).
        # discover() ist unter _disc_lock/_last_known_lock, _save_last_known
        # schreibt atomar (tmp+rename); gleichzeitige Aufrufe sind unschaedlich.
        if backends:
            with ThreadPoolExecutor(max_workers=len(backends)) as ex:
                list(ex.map(discover, backends))
        local_entries = []
        seen_local = set()
        up_count = 0
        for backend in backends:
            name = backend[0]
            ids = discover(backend)
            if ids is None:
                continue
            is_down = name not in _backends_up
            if not _katalog_zeigt(mode, "local", is_down):
                continue
            if is_down:
                ids = list(ids)
            if not is_down:
                up_count += 1
            for mid in ids:
                if mid in seen_local:
                    continue
                if is_down:
                    display_id = mid + DOWN_SUFFIX
                    owned_by = f"local-{name} (temporarily unavailable)"
                else:
                    display_id = mid
                    owned_by = "local-" + name
                    seen_local.add(mid)
                # Der Proxy weiss laengst, welche lokalen Modelle denken -
                # reasoning-models.json steuert oben schon enable_thinking. Nur stand es
                # nie im Katalog, und eine Oberflaeche kann keine Denkstufe anbieten, die
                # ihr niemand ankuendigt (2026-08-03). mid statt display_id, weil
                # letzteres bei einem toten Backend den DOWN_SUFFIX traegt.
                # Erst alles, was das Backend gemeldet hat, dann die Zusaetze
                # dieses Proxys obendrauf - reasoning kennt nur er (aus
                # reasoning-models.json), und `ctx` ist der vereinheitlichte
                # Name fuer das, was Backends unterschiedlich benennen.
                lmeta = local_meta(mid) or {}
                rv = _local_reasoning(mid, meta=lmeta)
                if rv is not None:
                    # Publish False too. `null` reads as "nobody knows" and every
                    # client then falls back to its own default; an explicit False
                    # is an answer.
                    lmeta["reasoning"] = rv
                lctx = local_ctx(mid)
                if lctx:
                    lmeta["ctx"] = lctx
                # max_output: Korrektur, nicht Quelle - nur wenn das Backend schweigt
                # (kein positives int meldet). Klienten ohne Katalogwert nehmen 16384 an.
                mo = lmeta.get("max_output")
                if not (isinstance(mo, int) and not isinstance(mo, bool) and mo > 0):
                    mo = LOCAL_MAX_OUTPUT.get(mid)
                    if mo is not None:
                        lmeta["max_output"] = mo
                local_entries.append(
                    catalog_entry(TAG_LOCAL + display_id, owned_by, lmeta or None))

        live_or = discover_cloud()
        catalog_snapshot = _merge_catalogs()

        cloud_models = []
        with _cloud_catalog_lock:
            meta_snapshot = dict(_model_metadata)
        for mid, status in catalog_snapshot.items():
            if live_or is not None and mid not in live_or:
                continue
            if not _katalog_zeigt(mode, "free" if mid.endswith(":free") else "paid",
                                  status == "na"):
                continue
            display = mid
            if status == "na":
                base, suffix = mid.rsplit(":", 1) if ":" in mid else (mid, "")
                display = f"{base}-na:{suffix}" if suffix else f"{mid}{NA_SUFFIX}"
            if mid.endswith(":free"):
                tag = TAG_FREE
                owned = "openrouter-free"
                display = display[:-5] if display.endswith(":free") else display
            else:
                tag = TAG_PAID
                owned = "openrouter-paid"
            cloud_models.append(
                catalog_entry(tag + display, owned, meta_snapshot.get(mid)))

        # OpenCode Zen models -- aus ZEN_MODELS, derselben Quelle, aus der sich
        # auch _classify() bedient. Angebot und Wegewahl koennen so nicht mehr
        # auseinanderlaufen: was hier angeboten wird, ist dort bekannt.
        # Begruendung der Zahlen steht bei der Konstante.
        for zen_id, zen_meta in ZEN_MODELS.items():
            if not _katalog_zeigt(mode, "free", False):
                continue
            cloud_models.append(catalog_entry(
                TAG_FREE + zen_id, "opencode-zen", dict(zen_meta)))

        n_na = sum(1 for v in catalog_snapshot.values() if v == "na")
        local_entries = local_entries + _alias_catalog_entries(local_entries)
        merged = {"object": "list", "data": cloud_models + local_entries}
        body = json.dumps(merged).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        or_state = "live" if live_or is not None else "cached/down"
        log(f"GET /v1/models [mode={mode}] -> merged cloud={len(cloud_models)} (na={n_na}, OR={or_state}) local={len(local_entries)} (from {up_count} backends) total={time.monotonic()-t0:.2f}s")

    def _proxy(self, method):
        t0 = time.monotonic()
        path = self.path
        # Eine ID je Anfrage: gueltiger Client-Wert, sonst neu erzeugt. Sie
        # reist an jeden Upstream-Versuch, steht in Antwort und Logzeile.
        self._rid = _anfrage_rid(self.headers.get("X-Request-Id"))
        self._rid_ersetzt_gemeldet = False
        self._ft_path = None        # keep-alive: never the previous request's path
        hint = f" rid={self._rid}"
        try:
            self._cors_origin = None
            self._cors_gesetzt = False
            origin = self.headers.get("Origin")
            if origin:
                if not _origin_allowed(origin):
                    self._send_error(403, json.dumps({"error": {
                        "type": "origin_not_allowed",
                        "message": f"Origin '{origin}' ist nicht erlaubt"}}).encode())
                    log(f"{method} {path} -> 403 Origin nicht erlaubt ({origin}){hint}")
                    return
                self._cors_origin = origin

            if method == "GET" and path in ("/health", "/v1/health"):
                with _FEYNTALK_COUNTS_LOCK:
                    counts = {k: dict(v) for k, v in FEYNTALK_COUNTS.items()}
                resp_body = json.dumps({"status": "ok", "feyntalk": counts}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(resp_body)))
                self.end_headers()
                self.wfile.write(resp_body)
                return

            if method == "GET" and path in ("/v1/models", "/models"):
                self._handle_models(t0)
                return

            # Kleine Verwaltungs-API: dasselbe wie SIGUSR1, aber per HTTP -
            # gedacht fuer ein externes Werkzeug nach einem Unit-Switch (Modell auf einem
            # Backend-Port wechselt, Discovery-Cache ist stale). Asynchron:
            # 202 heisst "Re-Check angestossen", Ergebnis danach in /v1/models.
            if path in ("/admin/recheck", "/admin/refresh"):
                if method != "POST":
                    resp_body = json.dumps({"error": "use POST"}).encode()
                    self.send_response(405)
                elif not _recheck_erlaubt():
                    resp_body = json.dumps({"error": {
                        "type": "recheck_throttled",
                        "message": f"Recheck hoechstens alle {RECHECK_MIN_INTERVAL:g}s"}}).encode()
                    self.send_response(429)
                    self.send_header("Retry-After", str(int(RECHECK_MIN_INTERVAL)))
                else:
                    _do_recheck(f"HTTP {path}")
                    resp_body = json.dumps({
                        "status": "recheck scheduled",
                        "backends": len(LOCAL_BACKENDS)}).encode()
                    self.send_response(202)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(resp_body)))
                self.end_headers()
                self.wfile.write(resp_body)
                return

            body = self._read_body()
            # Ein "einfacher" Browser-POST braucht keinen Preflight. Nur mit
            # application/json erzwingt der Browser einen OPTIONS-Flug, und der
            # sieht das Origin oben. Formulare und text/plain fallen hier raus.
            if method == "POST" and body:
                typ = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
                if typ != "application/json":
                    self._send_error(415, json.dumps({"error": {
                        "type": "unsupported_media_type",
                        "message": "POST-Rumpf muss application/json sein"}}).encode())
                    log(f"{method} {path} -> 415 Content-Type {typ or '(fehlt)'}{hint}")
                    return
            self._req_tools = None   # je Anfrage neu: keep-alive benutzt den Handler weiter
            self._req_parallel = True
            self._chatml_guard = False
            self._req_seam = False   # nur lokale Denkmodelle bekommen den Seam
            model = ""
            alias = None
            stream = False
            geprueft = False   # hat der Kostenregler diese Anfrage gesehen?
            kind = None        # seine Einstufung; das Routing folgt ihr
            if body and b"messages" in body:
                try:
                    j = json.loads(body)
                    self._req_tools = _tool_schemas(j.get("tools"), j.get("tool_choice"))
                    self._req_parallel = j.get("parallel_tool_calls") is not False
                    raw_model = j.get("model", "")
                    model = _anfrage_id(raw_model)
                    raw_model, model, alias = _apply_model_alias(raw_model, model)
                    if model != raw_model or alias is not None:
                        j["model"] = model
                        body = json.dumps(j).encode()

                    # Kostenregler: ist er an, wird hier abgewiesen - vor dem
                    # Weiterreichen, damit gar keine kostenpflichtige Anfrage
                    # das Haus verlaesst.
                    # Unbekanntes Modell zuerst: sonst meldet der Kostenregler
                    # einen Preisverstoss fuer etwas, das es gar nicht gibt.
                    kind = _classify(raw_model, model)
                    # Der Seam repariert nur, was ein lokaler Denkblock-Parser
                    # verursacht. Ein Wolkenstrom laeuft byte-rein und ohne Halten.
                    self._req_seam = kind == "local" and bool(_local_reasoning(model))
                    if kind == "unknown":
                        verfuegbar = set()
                        for b in list(LOCAL_BACKENDS):
                            for m in (discover(b) or []):
                                verfuegbar.add(m)
                        msg = json.dumps({"error": {
                            "type": "model_not_found",
                            "message": (
                                f"model '{raw_model}' ist weder auf einem lokalen "
                                f"Backend noch im Wolkenkatalog bekannt"),
                            "available": sorted(verfuegbar),
                        }}).encode()
                        log(f"UNBEKANNT: {raw_model} - 404 model_not_found{hint}")
                        self.send_response(404)
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Content-Length", str(len(msg)))
                        self.end_headers()
                        self.wfile.write(msg)
                        return

                    blocked, why = _policy_blocked(raw_model, model, kind)
                    if blocked:
                        log(f"BLOCKED (Kostenregler): {raw_model} - {why}{hint}")
                        msg = json.dumps({"error": {
                            "type": "policy_blocked",
                            "message": (
                                f"'{raw_model}' abgewiesen: {why}. Regler steht in "
                                f"policy.json (mode: local|free|cheap|max). Nutze ein "
                                f"zugelassenes Modell oder stelle den Modus hoch.")}}).encode()
                        self.send_response(403)
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Content-Length", str(len(msg)))
                        self.end_headers()
                        self.wfile.write(msg)
                        return
                    if alias is not None and alias["max_prompt_tokens"] is not None:
                        if kind != "local":
                            why = f"Alias '{alias['alias']}' zeigt auf '{model}', das kein lokales Modell ist"
                            msg = json.dumps({"error": {"type": "model_not_found", "message": why}}).encode()
                            log(f"ALIAS: {why} - 404")
                            self.send_response(404)
                            self.send_header("Content-Type", "application/json")
                            self.send_header("Content-Length", str(len(msg)))
                            self.end_headers()
                            self.wfile.write(msg)
                            return
                        n_tok, how = _count_prompt_tokens(find_backend_for_model(model), j)
                        err = _cap_violation(alias, n_tok, how)
                        log(f"ALIAS: {alias['alias']} -> {model} prompt={n_tok} ({how}) "
                            f"max={alias['max_prompt_tokens']} {'REJECT 400' if err else 'ok'}")
                        if err is not None:
                            msg = json.dumps(err).encode()
                            self.send_response(400)
                            self.send_header("Content-Type", "application/json")
                            self.send_header("Content-Length", str(len(msg)))
                            self.end_headers()
                            self.wfile.write(msg)
                            return
                    geprueft = True
                    n_msgs = len(j.get("messages", []))
                    max_t = j.get("max_tokens") or j.get("max_completion_tokens") or "-"
                    total_chars = sum(len(str(m.get("content", ""))) for m in j.get("messages", []))
                    stream = j.get("stream", False)
                    hint += f" model={model} msgs={n_msgs} in_chars={total_chars} max_tok={max_t} stream={stream}"
                    # reasoning passthrough: ask reasoning-capable local models to split
                    # their thinking into a separate `reasoning` stream (chat-template kwarg),
                    # so clients can surface it. The byte-relay forwards the field untouched.
                    # Vokabel je Modell: DS-4.1 kennt kein "medium" (gemessen
                    # 2026-09-18), die Flotte schickt es aber. Tabelle statt
                    # Sonderfall im Code -- siehe reasoning-effort-map.json.
                    if _is_local_model(model) and "reasoning_effort" in j:
                        _hit, _new = _map_reasoning_effort(model, j["reasoning_effort"])
                        if _hit:
                            if _new is None:
                                j.pop("reasoning_effort", None)
                            else:
                                j["reasoning_effort"] = _new
                            body = json.dumps(j).encode()
                            hint += f" effort_mapped={_new}"

                    if _local_reasoning(model) and _local_thinking_switch(j):
                        body = json.dumps(j).encode()

                    # ChatML-Marker als Stop-Strings fuer LOKALE Backends.
                    #
                    # Warum: DeepSeek V4 schreibt gelegentlich woertlich "<|im_end|>"
                    # in den Text und bricht damit mitten in einer Agentenkette ab
                    # (beobachtet 2026-07-31). Das ist KEIN Template-Fehler
                    # und kein Sondertoken - im DeepSeek-Vokabular sind das sechs
                    # gewoehnliche Token ['<','|','im','_end','|','>'], die das Modell
                    # aus seinem Trainings-Schwanz erzeugt. Konfigurativ nicht
                    # abstellbar, nur abfangbar.
                    #
                    # Sicher fuer alle lokalen Modelle: als *Text* ist "<|im_end|>" nie
                    # legitimer Inhalt. Bei echten ChatML-Modellen (qwen3.6-coding) ist
                    # es ohnehin ein Sondertoken, das der Server selbst als EOS behandelt
                    # - der Text-Stop kommt dort nie zum Tragen.
                    #
                    # Client-gesetzte stop-Werte werden ERGAENZT, nicht ersetzt.
                    #
                    # Seit 1.0.6 nur noch fuer NICHT-Stroeme: ein Stop-String trifft auch das Denken
                    # (2026-10-18). Stroeme bekommt ChatMLGuard, der nur den Antworttext prueft.
                    if _is_local_model(model) and stream:
                        self._chatml_guard = True
                    elif _is_local_model(model) and not (alias and alias.get("feyntalk") == "tokens"):
                        # Not on a token alias: it has no text path, and token mode refuses stop
                        # strings, so these would fail every non-stream request there.
                        st = j.get("stop")
                        if st is None:
                            st = []
                        elif isinstance(st, str):
                            st = [st]
                        elif not isinstance(st, list):
                            st = []
                        added = [s for s in CHATML_STOPS if s not in st]
                        if added:
                            j["stop"] = st + added
                            body = json.dumps(j).encode()
                except Exception as e:
                    if model and not geprueft:
                        # Fail-closed: das Modell ist bekannt, der Kostenregler hat die
                        # Anfrage aber nie gesehen. Bis 1.0.1 schluckte `except: pass`
                        # das und reichte ungeprueft weiter - auch in die Wolke.
                        log(f"{method} {path} model={model!r} ABGEWIESEN: Pruefung "
                            f"scheiterte ({type(e).__name__}: {e})")
                        self._send_error(400, json.dumps({"error": {
                            "type": "invalid_request",
                            "message": f"Anfrage nicht pruefbar: {type(e).__name__}: {e}"}}).encode())
                        return

            # /v1/compress: proxy to compress-server backend
            if method in ("POST",) and path in ("/v1/compress", "/compress"):
                compress_backend = None
                for b in LOCAL_BACKENDS:
                    if b[0] == "compress":
                        compress_backend = b
                        break
                if compress_backend:
                    fwd = self._fwd_headers_local(len(body))
                    name, host, port = compress_backend
                    t_try = time.monotonic()
                    conn = None
                    try:
                        conn = http.client.HTTPConnection(host, port, timeout=600)
                        conn.request("POST", "/compress", body=body, headers=fwd)
                        resp = conn.getresponse()
                        data = resp.read()
                        self.send_response(resp.status)
                        for hn in ("Content-Type",):
                            v = resp.getheader(hn)
                            if v:
                                self.send_header(hn, v)
                        self.send_header("Content-Length", str(len(data)))
                        self.end_headers()
                        self.wfile.write(data)
                        conn.close()
                        log(f"POST /v1/compress -> compress-server status={resp.status} total={time.monotonic()-t0:.2f}s")
                        return
                    except Exception as e:
                        log(f"POST /v1/compress -> compress-server FAIL: {e}")
                self._send_error(502, b'{"error":"compress backend unavailable"}')
                return

            ziel = _ziel(kind, model)
            if ziel == "zen":
                self._proxy_zen(method, path, body, hint, t0, is_streaming=stream)
                return

            if ziel == "cloud":
                is_na = _get_model_status(model) == "na"
                if is_na:
                    payload = json.dumps({"error": {
                        "message": f"model '{model}' is rate-limited and currently not available",
                        "type": "rate_limited",
                    }}).encode()
                    self._send_error(429, payload)
                    log(f"{method} {path} -> model={model!r} 429 (na) total={time.monotonic()-t0:.2f}s{hint}")
                    return
                self._proxy_cloud(method, path, body, hint, t0, is_streaming=stream)
            else:
                self._proxy_local(method, path, body, hint, t0, model=model or None, is_streaming=stream, feyntalk=(alias.get("feyntalk") if alias else None))
        except BrokenPipeError:
            log(f"{method} {path} client-disconnect total={time.monotonic()-t0:.2f}s")
        except Exception as e:
            log(f"{method} {path} EXC {type(e).__name__}: {e}")
            traceback.print_exc(file=LOG)
            try:
                self._send_error(500, b'{"error":"internal"}')
            except Exception:
                pass

    def do_POST(self): self._proxy("POST")
    def do_GET(self): self._proxy("GET")
    def do_OPTIONS(self):
        self._rid = _anfrage_rid(self.headers.get("X-Request-Id"))
        self._rid_ersetzt_gemeldet = False
        self._ft_path = None        # keep-alive: never the previous request's path
        self._cors_gesetzt = False
        origin = self.headers.get("Origin")
        self._cors_origin = origin if origin and _origin_allowed(origin) else None
        self.send_response(200)
        if self._cors_origin:
            self.send_header("Access-Control-Allow-Origin", self._cors_origin)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers",
                             "Content-Type, Authorization, X-Request-Id")
        self.end_headers()

    def log_message(self, fmt, *args): pass


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8082

    free_cat, paid_cat, meta = _load_catalog()
    _free_catalog.clear()
    _free_catalog.update(free_cat)
    _paid_catalog.clear()
    _paid_catalog.update(paid_cat)
    _model_metadata.clear()
    _model_metadata.update(meta)
    _last_known = _load_last_known()

    reload_backends(None, None)  # populates global LOCAL_BACKENDS (und stoesst den Warmlauf an)
    signal.signal(signal.SIGHUP, reload_backends)
    signal.signal(signal.SIGUSR1, schedule_recheck)

    _checker_pending.clear()
    _checker_exit.clear()
    checker = threading.Thread(target=_checker_loop, daemon=True, name="catalog-checker")
    checker.start()

    server = http.server.ThreadingHTTPServer(("0.0.0.0", port), Handler)
    n_free = len(_free_catalog)
    n_paid = len(_paid_catalog)
    n_na = sum(1 for v in _free_catalog.values() if v == "na")
    log(f"llm-proxy {__version__} starting on :{port} local={[b[0] for b in LOCAL_BACKENDS]} "
        f"cloud={'enabled' if OR_KEY else 'DISABLED (no key)'} "
        f"catalog={n_free} free (na={n_na}) + {n_paid} paid "
        f"routing=model-aware+first-up-fallback+live-cloud-filter+auto-check")
    server.serve_forever()
