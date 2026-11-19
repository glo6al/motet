# Optional local dictation helper

The helper is a separate, optional component. Motet runs as usual without it.
The page adds a microphone beside Send. The helper and its server proxy are
separately enabled; normal typing works without either one. Page checks use generated audio plus an earlier real-microphone proof; the native engine has been verified only on Apple Silicon macOS.

Recognition uses Moonshine Small Streaming English on this computer, without an
account or inference service. The helper accepts audio supplied by an authorized
local client. It never opens a microphone. The helper keeps audio and transcript
events only in memory and creates no recordings or transcript files. Words in
the page follow the same draft and message-history rules as typed text; nothing
is sent until Send.

## Inspect before setup

The supervisor needs only Python's standard library, Python 3.9 or later.
The separate native recognition environment needs Python **3.10 or later** and
`moonshine-voice==0.1.6`. Although that package advertises an earlier Python
minimum, its eager type annotations need Python 3.10. The native path has been
checked on Apple Silicon macOS. Check the selected wheel's platform requirements
before installation; other native platforms have not been qualified here.
The private-state CLI currently requires POSIX filesystem protections.

No command installs Python or pip packages automatically. `serve`, `status` and
imports never download models. The optional runtime has its own environment;
Motet's server keeps its standard-library-only installation.

From the repository root, inspect the exact model files, source URLs and hashes:

```sh
python3 integrations/dictation/helper.py model-info
```

The eight model files total **142,465,862 bytes** (142.5 MB). Runtime packages
require additional space and download bytes depending on Python and platform.
The model is the English Small Streaming `quantized_26_11_02` variant matched to
the pinned runtime. Do not substitute a newer model snapshot solely because its
architecture name matches. Code/model terms and dependency references are in
[THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md).

## Empty-home preview without installation

This preview creates only a dedicated private credential and serves status. It
does not install a model, use a microphone or connect to Motet or agents.

```sh
DICTATION_PREVIEW_HOME="$(mktemp -d)"
python3 integrations/dictation/helper.py serve --home "$DICTATION_PREVIEW_HOME" --port 18879
```

In another terminal, use the same preview-home path:

```sh
python3 integrations/dictation/helper.py status --home "$DICTATION_PREVIEW_HOME" --port 18879
```

The expected state is `not_installed`. Stop the server with Ctrl+C. The next
status probe reports `not_running`; a home with no helper credential reports
`not_installed`. The probe creates no files. Port 8793 is reserved for Motet and
refused by this helper. `--port 0` chooses a temporary server port and prints it.

`--demo` reports `disabled` without state creation, runtime loading or downloads.
Motet's demo displays an inactive microphone and never requests microphone access.

## Deliberate installation

Set `DICTATION_PYTHON` to an already installed Python 3.10+ executable; for example, use its absolute path after checking `--version`. This guide does not install Python.

Choose an existing, permanent private home and start/stop the helper once to create its
private state folder. The default folder is `.live-room/dictation` in that home.
For installation use `DICTATION_INSTALL_HOME`, never the temporary preview home.
For your normal Motet installation set `DICTATION_INSTALL_HOME="$HOME"`: Motet's
proxy reads helper state from the home of the user running its server. A different
home is only for an explicitly isolated preview whose server selects that home.
Existing helper-state directories must be owned by the current user and private;
the helper refuses unsafe directories rather than changing their permissions.

```sh
DICTATION_INSTALL_HOME="$HOME"
python3 integrations/dictation/helper.py serve --home "$DICTATION_INSTALL_HOME"
```

After the helper starts, stop it with Ctrl+C. Continue in that same terminal.

When you choose to install recognition, create a separate virtual environment
inside that folder with an existing Python 3.10+ interpreter. Then deliberately
install the pinned package into that environment:

```sh
DICTATION_INSTALL_HOME="$HOME"
DICTATION_STATE="$DICTATION_INSTALL_HOME/.live-room/dictation"
: "${DICTATION_PYTHON:?Set this to an existing Python 3.10+ executable}"
"$DICTATION_PYTHON" -m venv "$DICTATION_STATE/.venv"
"$DICTATION_STATE/.venv/bin/python" -m pip install 'moonshine-voice==0.1.6'
```

Here `DICTATION_STATE` is your chosen home's `.live-room/dictation` directory.
This pip command is an explicit runtime download; it is never run by the helper.
Inference does not require the package's training/finetuning extras.

After reading `model-info`, request the separate model download explicitly:

```sh
python3 integrations/dictation/helper.py download-model --home "$DICTATION_INSTALL_HOME" --accept-bytes 142465862
```

No transfer starts without that exact size confirmation. Setup prints progress
and verifies size and SHA256 before activating a complete model folder. A valid
installed bundle is reused without a transfer. Failed transfers leave the old
folder intact. An interrupted process may leave private staging files; a later
setup does not trust them. No inference or microphone activity occurs in setup.

Run `serve` again to load the installed runtime/model. `--python` and `--model`
can explicitly select an existing runtime environment and a private verified
model directory. Model files must be private regular files without links.
Use a canonical parent directory for an externally selected model location.

## Authentication and protocol

The helper binds only `127.0.0.1`. Every recognition API route requires proof of
the dedicated secret stored in the private `header` file under helper state.
This is separate from Motet's owner key. The secret never travels in an HTTP
request: an HMAC challenge proves the responder's knowledge of that secret,
and a separate proof authenticates each recognition request. Proofs bind the protocol,
role, helper authority, fresh nonces, method, route, body digest and relevant
framing/session/sequence headers. Challenges expire and a request proof is
single-use. Raw key headers are refused. Keep the private file out of URLs,
process arguments, logs and browser assets. A process that can read the owner's
private state can also use that capability; this is not isolation between
processes with access to the same private files.

This is request authentication over unencrypted loopback HTTP, not isolation
from every other local process. The challenge and recognition request use
separate connections: if the helper stops and another process takes its port
between them, that process can receive the next request body and return a forged response, including text that could enter the draft; responses have no MAC. It cannot obtain
the secret or replay the proof against a fresh helper instance. Audio stays on
this computer; connection-bound confidentiality would require a different
transport.

The wire headers are `X-Dictation-Client-Nonce`, `X-Dictation-Server-Nonce`,
`X-Dictation-Content-SHA256` and `X-Dictation-Proof`. Use the shared
`src/dictation_auth.py` client rather than copying the file secret into a header.
The server nonce lifetime is ten seconds and the bounded replay fence holds
at most 1,024 used challenges; a full fence refuses work rather than forgetting
live entries. Header admission has an absolute two-second deadline and separate
bounded capacity from authenticated recognition work.

Host, Origin and authentication headers are checked for duplicates. Foreign
origins, cross-site fetches, redirects in the status probe, ambiguous framing
and noncanonical routes are refused. No CORS access is enabled. Motet's
adapter proxies through its own authenticated server and keeps this helper
credential out of the page.

| Route | Request | Result |
| --- | --- | --- |
| `GET /v1/challenge` | No body; one fresh 64-hex `X-Dictation-Client-Nonce` | HMAC authentication bootstrap only: `authentication`, echoed client nonce, fresh server nonce and server proof; no readiness or session access |
| `GET /v1/status` | No body | Readiness, safe code, audio format and limits |
| `POST /v1/start` | JSON `{"intervalSeconds":0.25}` or `0.5` | `sessionId`, `epoch` |
| `POST /v1/audio` | Binary float32 little-endian, mono 16 kHz; singleton `X-Dictation-Session` and `X-Dictation-Sequence` headers | Accepted sequence, epoch, RMS/peak input level |
| `POST /v1/events` | JSON `{"sessionId":"…","after":0}` | Ordered events, epoch, ended flag; short bounded wait |
| `POST /v1/stop` | JSON `{"sessionId":"…"}` | Finalize and end; obtain final line events through events |
| `POST /v1/cancel` | Same | Discard stream without final decoding |
| `POST /v1/reset` | Same | Retire the active phrase and increment epoch |

JSON routes accept `application/json`; audio uses `application/octet-stream`.
Audio packets are at most 64,000 bytes, with finite samples in [-1,1]. Start at
sequence zero and serialize packets and controls. The helper rejects concurrent
audio commands rather than accumulating a queue. The client should bound its
own capture queue and stop on a growing backlog.

Command admission waits at most 100 ms for the helper's command lock. This
short wait is not a deadline for recognition or finalization. A `busy` refusal
does not end the take and does not mean the browser microphone is still open.

Line events contain `id`, `type`, `epoch`, `lineId`, `text` and `complete`, with
timing fields when available. An active line can be revised as a whole; a dark
prefix or grey tail in the later editor cannot imply word-level finality.
After reset, keep the person's edits and reject events from older epochs.
Resetting during speech can lose or repeat boundary words and needs editor
testing. Cursor gaps produce `events_expired`; keep the visible draft rather
than silently treating a truncated event history as complete.

One take is active at a time. Takes are bounded to three minutes, inactive takes
expire after 20 seconds without audio/event requests, and completed histories
expire after a minute. At most four session histories and 512 events per session
are retained. Server concurrency and worker frames/deadlines are bounded.
The helper sends no agent message; the page retains responsibility for its
explicit Send action.

## Fixed readiness and event contract

| State or condition | Owner and remedy |
| --- | --- |
| `not_installed` | Supervisor: runtime/model/state missing or unsafe, or unsupported package/Python version; deliberate setup |
| `not_running` | Client probe: helper unavailable; start the separate helper and retry |
| `loading` | Supervisor: validating assets/loading the model; keep the draft |
| `ready` | Helper: ready for a take |
| `listening` | Helper: accepting supplied audio, not evidence of browser mic access |
| `finishing` | Helper: Stop is finalizing; await final events or a safe error |
| `failed` | Helper: recoverable worker/load/recognition failure; keep the draft and retry |
| `disabled` | Explicit demo mode; no state, model or helper access |
| `busy` | Client probe only: transient request capacity; retry without changing credentials |
| Microphone blocked/disconnected | Browser capture state; the helper cannot infer browser permission |

Readiness uses `schemaVersion: 1`, one of the fixed state strings above, and a
`code` string or `null`. Normal `loading`, `ready`, `listening` and `finishing`
have a null code. Setup reasons are `dependency_missing`, `runtime_version`,
`model_missing_or_invalid` and, for a probe without safe state,
`helper_state_missing_or_unsafe`. Worker failures use `worker_failed`,
`worker_timeout`, `model_error`, `protocol_error`, `recognition_failed` or
`invalid_request`.
A client probe reports `busy/busy` for temporary admission or worker capacity. The model service itself keeps its existing readiness state. The stopped client probe uses `connection_unavailable`; an authentication or
response failure uses `authentication_failed` or `invalid_response`. Demo uses
`demo`. Motet's disabled proxy adds `not_configured`; a missing private helper
credential adds `not_installed`. Those are codes, not additional state names.

The events response has `sessionId`, `epoch`, `ended` and an ordered `events`
array. Each event has a monotonic `id`; the complete event type list is:

| Type | Additional fields | Meaning |
| --- | --- | --- |
| `line` | `epoch`, `lineId`, full `text`, boolean `complete`; optional `audioSeconds`, `decodeMs` | Replace that epoch/line only; `complete` prevents later revision |
| `error` | fixed `code`, safe `message` | Keep visible text; the take cannot continue |
| `ended` | `epoch` | No more text will arrive for that take |

Protocol errors use `schemaVersion: 1, error: {code, message}`. Clients branch
on the code and never parse the message. The route-specific vocabulary is:

| Operation | Error codes in addition to common transport errors |
| --- | --- |
| Start | `invalid_interval`, `busy`, `loading`, `not_installed`, and the readiness setup/worker codes above |
| Audio | `invalid_sequence`, `invalid_audio`, `out_of_order`, `take_limit`, `unknown_session`, `ended`, `busy`, `worker_failed` |
| Stop, Cancel, Reset | `invalid_control`, `unknown_session`, `ended`, `busy`, `worker_failed` |
| Events | `invalid_cursor`, `events_expired`, `unknown_session` |
| Error event | `worker_failed`, `take_limit`, `disconnected` |

Common helper errors are `forbidden`, `invalid_request`, `body_too_large`,
`unknown_route`, `not_running`, `busy` and `unavailable`. The owner proxy additionally
uses `not_configured`, `demo`, `not_installed`, `invalid_response` and
`helper_authentication_failed`; upstream messages are never copied to the page.
Unknown codes fail closed with a kept draft rather than inventing a readiness
state. A retry after a worker failure requests a fresh load first, without
opening the microphone; press the microphone again when readiness is `ready`.

The browser's sound wave uses an independent audio meter. The
helper's RMS/peak describes the supplied packet and is not recognition confidence.
The supervisor permits no external audio destination. Its child loads local
assets and invokes no download API; a Python audit guard rejects Python network
operations. That guard is not an operating-system firewall for native code.
The model remains in the worker's memory while the helper runs, including while
idle. Stop the helper to release it; this version has no idle model unload.

## Checks

Normal checks need neither Moonshine nor a model:

```sh
python3 tests/test_dictation_assets.py
python3 tests/test_dictation_runtime.py
python3 tests/test_dictation_helper.py
```

With an already installed runtime and model, explicitly run
`tests/check_dictation_native.py --python … --model …`. It copies model assets
into a temporary private home and feeds the package's existing speech sample
through the real authenticated helper. It uses no microphone, downloads or new
audio files. Its timing is backend-only; it does not establish browser latency,
accuracy for a person's voice, Undo or comfort while editing.

Stop the helper before removing its dedicated state folder. No core rollback is
needed to stop using this optional component.

## Connecting through Motet's owner proxy

Press the microphone after setup and allow it for the page. Words appear in the
editable message box in italics while listening. The active phrase is grey until
the engine finalizes it; completed phrases use normal ink. Stop keeps the draft
and restores upright text. Cancel removes untouched dictated phrases from the
current take while retaining manually edited phrases. Send stops capture and
sends exactly the visible draft through the existing message path. Late text
cannot extend a sent message. The microphone stops on recipient/draft changes, locking, hidden tabs and errors, with a reason when it ends automatically. Opening or closing the stage conversation and changing its model do not stop a take.

The native textarea remains the editor. A pointer-inert, screen-reader-hidden
display layer colors provisional text and follows its measured wrapping and
scrolling. Selection, composition and forced-color mode use the native text
display. Editing an active phrase resets recognition; speech at that boundary
may need correction. Dictation is currently for the local desktop connection;
phone capture needs its separately reviewed secure connection setup.

Stop and Cancel release microphone capture immediately. Stop keeps the visible
words and waits for final events; Cancel immediately removes untouched dictated
phrases and keeps manual edits. For a known session, the page retries only an
explicit `busy` refusal for the same Stop or cleanup Cancel. It never replays
audio, Reset or Start, and does not retry an ambiguous timeout or network error.
The action has one 20-second budget, including queued work, retry pauses and
final-event waiting. Each request is limited to the remaining budget or twelve
seconds, whichever is shorter. Cleanup after a failed Stop shares its original
budget. A completed event stream can finish Stop before its HTTP reply arrives.

If closure cannot be confirmed, the page says that the microphone is off and
the visible draft has been kept. A late cleanup warning does not replace a
newer status or alter a subsequently edited draft. The next take waits for
cleanup and checks readiness before asking for the microphone. This is a
bounded recovery attempt, not a guarantee that the helper is idle at the
deadline: already admitted native work can outlive an HTTP request. A lost
Start reply can also leave the page without a session ID to cancel. Readiness
checks prevent another take while the helper reports occupied; the helper's
existing inactivity expiry provides recovery after requests stop. An
unconfirmed Stop may lose final words that were not yet visible.

Use Cmd+Z or Ctrl+Z to undo manual corrections, and Cmd+Shift+Z, Ctrl+Shift+Z or
Ctrl+Y to redo them. Recognition updates do not become manual Undo entries, so
undoing a correction keeps unrelated words that arrived afterward. The bounded
history is kept only in memory and retired when the draft's destination changes
or its text is replaced. Browser Edit-menu Undo availability after dictation is
not qualified; the supported path is the keyboard command. Native composition
keeps its own Undo keys until the composition is committed.

The page connects to Motet's own `/api/dictation/` routes. Motet forwards only the
documented recognition operations to the helper on `127.0.0.1`; it keeps the
dedicated helper capability on the server. The page continues to authenticate
with Motet's existing owner credential. Neither credential is included in a URL,
and the Motet owner credential is never sent to the helper.

The proxy is off by default. After deliberately setting up and starting the
helper in the same selected home, opt in when starting Motet:

```sh
WATCH_DICTATION_ENABLED=1 WATCH_DICTATION_PORT=18879 python3 src/server.py
```

`WATCH_DICTATION_ENABLED` must be exactly `1`. The optional helper port defaults
to `18879`, must be between 1 and 65535, and must differ from Motet's current port
and the reserved port 8793. An invalid setting leaves the proxy off. A page
request cannot select a different host, port, file or remote service. The proxy
never starts the helper, installs a runtime, downloads a model or changes its
private state. Status reports distinguish missing/unsafe helper state
(`not_installed`) from a configured helper that gives no answer (`not_running`).

The helper's private `header` is read from `.live-room/dictation` under the home
selected for Motet, with no fallback to another home. The existing POSIX owner
and private-file protections apply. Disabled and demo requests do not inspect
helper state or open a connection. Browser microphone access remains a separate
browser permission; the proxy does not request or infer it.

Every proxy route requires Motet's owner header, a local Host, and an allowed
Origin/fetch-site context. Canonical routes only are accepted: status is GET;
start, audio, events, stop, cancel and reset are POST. Their bodies and the audio
session/sequence headers follow the `/v1/` protocol above. The proxy validates
bounded responses and forwards only documented readiness, session, timing,
level and transcript fields. Native diagnostics and arbitrary helper fields
are discarded. Requests have bounded deadlines and no redirect or environment
proxy fallback. Errors keep a safe fixed code so the editor can retain its
visible draft and offer the appropriate remedy.

The proxy's ordinary tests require no helper installation, model or network
service beyond their own ephemeral loopback fixture:

```sh
python3 tests/test_dictation_proxy.py
```

## Manifest provenance

The pinned model catalogue at
[Moonshine v0.1.6](https://github.com/moonshine-ai/moonshine/blob/v0.1.6/core/moonshine-model-file-metadata.generated.cpp)
supplies file URLs, expected sizes and CRC32C values. The manifest's SHA-256
values are maintainer-computed integrity pins, recorded from the downloaded,
runtime-matched files on 2026-11-19; they are not claimed as upstream-published
SHA-256 values. The manifest records that distinction and verifies every required
file before loading. No model or native binary is shipped in this repository.
