# Changelog

## 1.3.6 - 2026-11-13

- **Effort per Kwarg.** `chat_template_kwargs.reasoning_effort` gewinnt vor dem Feld auf oberster Ebene, wie in der
  Chat-Route der Engine -- im Token-Pfad (`reasoning.effort` an die Responses-Anfrage) und im Denk-Schalter des
  Textpfads, der vorher nur das obere Feld las (ein Kwarg `none` schaltete dort das Denken ein).
- **`arcint.dory` im Chat.** Die Schleifenzahl eines Dory-Zuges (Engine: `response.arcint.dory = {loops, reprefill}`
  am Terminal im Token-Modus) erreicht Chat-Clients als `arcint.dory`, im Strom am Schluss-Chunk, ohne Strom im Objekt.

## 1.3.5 - 2026-11-12

- **Native Anbieter (`native-providers.json`).** Weitere OpenAI-kompatible Clouds neben OpenRouter und Zen,
  etwa ein Abo-Endpunkt. Je Eintrag ein id-Praefix mit `base_url`, Schluessel aus `key_file` (eine Zeile,
  0600; das paketierte Init-Skript reicht nur `OPENROUTER_API_KEY` durch) oder `key_env`, `cost_class`
  (`paid`|`free`), `owned_by` und einer Positivliste der Modelle. Katalog-ids `[$] <praefix>/<modell>`; die
  Kostenklasse des Anbieters gilt unabhaengig vom Tag, den der Client schreibt; `cheap` liest einen
  angegebenen `prompt_price` oder weist ab (und listet dann nicht). Weitergeleitet wird nur
  `POST /v1/chat/completions` (fester Pfad, sonst 404), Bearer, 4xx/429 durch (Retry-After), 5xx -> 502.
  Ein Schluessel mit CR/LF oder Leerzeichen zaehlt als nicht gesetzt; weder Antwort noch Log zeigen ihn.
  Praefixe, die einen OpenRouter-Anbieter oder `opencode` ueberdecken, werden abgewiesen. SIGHUP laedt neu.
- **`force_nonempty_content` im Token-Modus.** Die Engine nimmt Token-Zuege von dieser Regel aus; der Proxy
  wendet sie an wie arcints `promote_reasoning_to_content`: an (Anfrage-`chat_template_kwargs` gewinnt, auch
  ein `false`; sonst die Unit-Voreinstellung aus `/props` `chat_template_kwargs_default`), Denken nicht leer,
  Antwort leer -> das Denken wird zur Antwort. Token-Modus zusaetzlich: kein Tool-Aufruf im Zug. Chat-Strom:
  die Denk-Deltas wie bisher, dann ein Inhalts-Delta, kein Tool-Delta; ohne Strom `content` = Denken, kein
  `reasoning_content`. Leer = ASCII-Leerraum wie `text_is_blank`. Der Responses-Durchgang bleibt unveraendert.
  Geprueft an einer Dory1.0-4B-Aufzeichnung: Rendering 12/12 bytegleich zur Engine.

## 1.3.4 - 2026-11-06

- **`-ft` ohne Strom.** Der Proxy haengt nicht-gestreamten lokalen Anfragen ChatML-Stop-Strings an, und der
  Token-Modus lehnt Stop-Strings ab. Seit 1.3.3 scheiterte darum jede Nicht-Stream-Anfrage auf einem `-ft`-Alias mit
  `400 token_mode_unsupported_request`; davor hatte der stille Text-Rueckfall das verdeckt. Ein Token-Alias bekommt die
  Stop-Strings nicht mehr. Stop-Strings des Anrufers bleiben dort ein `400`.

## 1.3.3 - 2026-11-06

- **Effort im Render.** `_feyntalk_render` gab nur `enable_thinking` an die Vorlage,
  die Engine aber auch die Stufe. Auf jeder Vorlage, die `reasoning_effort` liest
  (Qwen3.8), wich `prompt_sha256` darum ab: jeder Zug mit Effort bekam 409 und lief
  still als Text. Jetzt rendert der Proxy mit derselben Stufe wie die Engine
  (`_feyntalk_template_effort`, Abbildung wie `template_reasoning_effort` in arcint).
- **`none`/`off` schalten das Denken ab.** Der Textpfad setzte `enable_thinking: true`
  auch bei `reasoning_effort: "none"`, und das schlaegt den Effort in der Engine: die
  Vorlage dachte auf ihrem Standard weiter. `off` zaehlt auf dem Tokenpfad jetzt wie `none`.
- **`-ft` scheitert laut.** Ein Alias mit `"feyntalk": "tokens"` hat
  keinen Textpfad mehr: 409 Prompt/Renderer -> `502 renderer_mismatch`, kein Token-Modus ->
  `502 token_mode_unavailable` (nach einmal neu fragen), Anfrage, die der Token-Modus nicht
  kann -> `400 token_mode_unsupported_request`. Ein bewegter Material-Hash holt einmal neu und
  versucht einmal im Token-Modus. Namen ohne Alias behalten den Text-Rueckfall.
- **Sichtbarkeit.** `X-Llm-Proxy-Path: tokens|text` an jeder lokalen Antwort, `/health` traegt
  je Backend `tokens`, `fallbacks`, `errors`.

## 1.3.2 - 2026-11-02

- **Kontext-Waechter am Chat-Rand.** Die Engine meldet mit `arcint:context`
  einmal je Zug die Arithmetik ihres Kontext-Waechters (`used`, `n_ctx`,
  `remaining`, `forced_close`, bei geschlossenem Waechter `token_index`), sofern
  die Anfrage den Waechter konfiguriert. Der Responses-Passthrough reichte das
  Ereignis schon roh weiter; der Token-Modus-Chat verschluckte es, weil er seine
  Rahmen aus dem normalisierten Strom baut und nur `arcint:token`/`arcint:span`
  einspeist. Jetzt traegt der letzte Rahmen eines Chat-Stroms und der Rumpf
  einer Nicht-Stream-Antwort den Objekt-Schluessel `arcint: {context: {...}}`
  neben `usage` -- mit genau den Feldern, die die Engine geschickt hat, ohne den
  Ereignis-Diskriminator `type`. Ein Zug ohne das Ereignis bekommt keinen
  `arcint`-Schluessel; `usage` und `finish_reason` bleiben, wo sie waren. Der
  Text-Modus ist unveraendert.

- **Client-Abbruch mitten im Strom ohne Lesefaden-Fehler.** Der Client-Wachhund
  kappt den Backend-Socket mit `shutdown()`; der Lesefaden bekam dabei ein EOF
  und schloss in `read1` denselben `HTTPResponse`, waehrend der Wachhund danach
  die Verbindung schloss. Beide `_close_conn` trafen sich, der zweite las
  `self.fp = None` und fiel an `AttributeError: 'NoneType' object has no attribute
  'close'` -- im Protokoll als `Lesefaden abgerissen: ...`, obwohl der Abbruch
  selbst funktionierte. Jetzt wartet der Wachhund
  auf den Lesefaden und schliesst unter einer Sperre; der Hauptfaden schliesst
  nach dem Beitritt unter derselben Sperre. Das Backend sieht den Abbruch
  weiterhin per `shutdown()`, die Verbindung wird genau einmal geschlossen, und
  die Zeile entfaellt.

- **Auch die anderen Objekt-Ereignisse am Chat-Rand.** Aus demselben
  `terminal()`-Pfad der Engine kommen zwei weitere Zahlen-Objekte, die der
  Token-Modus-Chat bisher stumm fallen liess: `arcint:loop_guard` (die neun
  Zaehler: `detections`, `nudges`, `slaps`, `aborts`, `self_closes`,
  `door_masked`, `door_eog_masked`, `context_closes`, `context_no_closer`) und
  `arcint:truncated` (die Fit-Pass-Zahlen: `blocks_dropped`, `tokens_freed`,
  `prompt_tokens_before`, `prompt_tokens_after`, `reserve`). Beide reisen jetzt
  im selben `arcint`-Objekt neben `usage`, unter `loop_guard` und `truncated`,
  mit genau den Feldern, die die Engine schickte -- je Ereignis ein Schluessel,
  fehlt das Ereignis, fehlt der Schluessel. Ein Zug ohne Objekt-Ereignis bleibt
  byte-gleich; Text-Modus und Responses-Passthrough unveraendert.

- **X-Request-Id, die Client-Seite von arcint 1.4.2.** Jede eingehende Anfrage
  bekommt eine ID: ein Client-Wert, der arcints Grammatik erfuellt (1..128
  Zeichen aus `[A-Za-z0-9._:-]`), bleibt; sonst erzeugt das Gateway eine im
  selben Format (`req-` + 16 Hex, zufaellig). Die ID reist an jeden
  Upstream-Versuch des Zuges -- auch beim Weiterreichen an ein anderes Backend
  bleibt sie dieselbe -- und kommt auf jeder Antwort des Gateways zurueck
  (Erfolg, eigener Fehler, SSE schon in den Kopfzeilen vor dem ersten
  Ereignis). Ein ungueltiger Client-Wert wird nie weitergereicht oder
  protokolliert. Antwortet ein Backend mit einer anderen gueltigen ID, gewinnt
  die Gateway-ID; die Abweichung steht einmal im Protokoll
  (`rid=<uns> backend_rid=<seins> (backend replaced the request id)`). Die
  per-Request-Logzeile traegt ` rid=<id>` neben Modell, Backend, Groessen und
  Zeiten, sodass sie sich mit der arcint-Journalzeile per grep paaren laesst.

## 1.3.1 - unreleased

- **Thinking replay, exact count.** Die Drop-Entscheidung der ersten Kompaktierungsstufe
  fiel bisher nur auf der Zeichen-Schaetzung (`len(prompt) / FEYNTALK_CHARS_PER_TOKEN`, 3.0).
  Gemessen (2026-11-01, Qwen3.8 Flash-Next, 131072 ctx) ueberschaetzte sie um ~1,5x und liess
  das Denken bei real ~57-60 % Kontext fallen, was im naechsten Zug eine volle Neu-Vorberechnung
  kostete. Liegt die Schaetzung nun bei mindestens `FEYNTALK_TOKENIZE_PRE_FRACTION` (0,8) der
  Drop-Schwelle, fragt das Gateway die Engine exakt (`POST /tokenize` mit dem behaltenen Render;
  dieselbe Authorization wie an die Engine-Routen) und entscheidet auf dieser Zahl. Fehlt die
  Route, antwortet sie nicht 200 oder laeuft der Zaehler in die Frist, wird die Schaetzung mit
  dem zuletzt beobachteten exact/Schaetzung-Verhaeltnis dieses Backends skaliert (ein float je
  Backend, ohne Verhaeltnis die rohe Schaetzung), mit einer Logzeile je Backend -- der Zaehler
  kippt nie eine Anfrage, und ein einzelner Fehlschlag kippt keine behaltene Entscheidung. Die
  Logzeile nennt die Herkunft (`N tokens (exact)` bzw. `~N tokens (estimate)` / `~N tokens
  (estimate, scaled)`), das `think=`-Feld traegt sie als `kept(exact)`/`kept(estimate)`/
  `kept(estimate, scaled)`/`dropped(exact)`/`dropped(estimate)`/`dropped(estimate, scaled)`.

## 1.3.0 - 2026-11-01

Part of the **feyntalk chain**, released and tested together with [arcint 1.4.0](https://github.com/marfrit/arcint/releases/tag/v1.4.0)
(the engine) and [feyntalk-render 1.0.0](https://github.com/marfrit/feyntalk-render/releases/tag/v1.0.0) (the renderer); the wiring guide is
arcint's [`docs/feyntalk.md`](https://github.com/marfrit/arcint/blob/v1.4.0/docs/feyntalk.md).

- **feyntalk token mode.** The gateway renders the prompt itself with feyntalk-render, sends it to the engine in
  token mode (`arcint.token_mode` with the material's `renderer_hash` and the prompt's `prompt_sha256`), and turns
  the engine's token and span events back into plain Chat Completions or Responses output: reasoning, answer and
  tool calls are split by the engine's spans, not by parsing marker text. `/v1/responses` passes the engine's SSE
  through. Opt-in per backend (`"feyntalk": "tokens"` in `local-backends.json`) or per alias (below); text mode
  otherwise.
- **Render input like the engine's.** Tool schemas and call arguments reach the template in the engine's shape
  (key order, object arguments, nlohmann's number format including float digits and integer overflow), so the
  gateway's render and the engine's are byte-identical; parallel tool calls of one assistant turn are one turn,
  as the engine builds it, and `content` beside calls is `null` when empty.
- **Fallbacks, never silent.** A request token mode does not serve (an image part, `n > 1`, a stop sequence, a
  render the renderer refuses) stays a text request with one log line per reason. A prompt-hash mismatch (409) is
  retried once in text mode; a 409 on a stale `renderer_hash` (the unit now serves other material) also drops the
  cached material, so the next turn fetches the new one. The material is fetched and built in the background as soon as a
  probe sees a token-mode backend ready, so the first request after a start does not wait for it; a request
  whose client hangs up before it is sent never reaches the engine.
- **Stream hardening.** The upstream socket is always drained (reader thread, byte-bounded buffer); the per-event
  work no longer grows with the stream; a half-closed client keeps its stream while a full close cancels the
  turn within about a second; the keep-alive probe only goes out on SSE frame boundaries and only on real event
  streams.
- **Thinking replay (`preserve_thinking`).** When the engine reports `supports_preserve_thinking`, the gateway
  renders with `preserve_thinking=true` so a steering message (a user message in the middle of a tool loop) does
  not drop the loop's reasoning and with it the engine's prefix cache; it falls back to the template's default as
  the first compaction stage once the preserved prompt passes `FEYNTALK_THINK_DROP_FRACTION` (0.7) of the
  context. A client's own `chat_template_kwargs.preserve_thinking` / `arcint.preserve_thinking` wins; a non-bool
  is a 400. Request log field `think=kept|dropped|off`.
- **`-ft` names.** `model-aliases.json` entries may carry `"feyntalk": "tokens"`: one engine unit under two names,
  the classic name in text mode for lightweight clients and `<name>-ft` in token mode, one backend and one slot
  pool. The alias inherits the target's per-model properties (context, reasoning, output limit, effort map,
  friendly name, cost class) and is marked in `/v1/models` with `feyntalk` and `alias_of`.
- **Version handshake.** Token mode needs feyntalk-render >= 1.0.0 and an engine reporting >= 1.4.0; otherwise
  the request stays on the text path with one log line.
- **Licence: Apache-2.0** (was MIT), the same as the engine and the renderer.
- **Public-release hygiene.** Vendored third-party code and the chat template used as a test fixture are
  documented with their sources and licences (`tests/vendor/README.md`, `tests/fixtures/README.md`);
  `SECURITY.md` states the token-mode trust boundary and how to report a vulnerability.
