# Optional local dictation helper

The helper is a separate, optional component. Motet runs as usual without it.
This release adds the helper and its protocol only; the page has no dictation
controls or connection to it yet.

Recognition uses Moonshine Small Streaming English on this computer, without an
account or inference service. The helper accepts audio supplied by an authorized
local client. It never opens a microphone. Audio and transcript events remain in
memory; this integration creates no audio files or persistent transcripts.

## Inspect before setup

The supervisor needs only Python's standard library, Python 3.9 or later.
The separate native recognition environment needs Python **3.10 or later** and
`moonshine-voice==0.1.6`. Although that package advertises an earlier Python
minimum, its eager type annotations need Python 3.10. The native path has been
checked on Apple Silicon macOS; the published wheel requires macOS 15 or later.
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
Motet's existing demo page is unchanged and gains no microphone request.

## Deliberate installation

Choose an existing private home and start/stop the helper once to create its
private state folder. The default folder is `.live-room/dictation` in that home.
Existing helper-state directories must be owned by the current user and private;
the helper refuses unsafe directories rather than changing their permissions.

When you choose to install recognition, create a separate virtual environment
inside that folder with an existing Python 3.10+ interpreter. Then deliberately
install the pinned package into that environment:

```sh
: "${DICTATION_PREVIEW_HOME:?Set this to the private home chosen above}"
DICTATION_STATE="$DICTATION_PREVIEW_HOME/.live-room/dictation"
python3 -m venv "$DICTATION_STATE/.venv"
"$DICTATION_STATE/.venv/bin/python" -m pip install 'moonshine-voice==0.1.6'
```

Here `DICTATION_STATE` is your chosen home's `.live-room/dictation` directory.
This pip command is an explicit runtime download; it is never run by the helper.
Inference does not require the package's training/finetuning extras.

After reading `model-info`, request the separate model download explicitly:

```sh
python3 integrations/dictation/helper.py download-model --home "$DICTATION_PREVIEW_HOME" --accept-bytes 142465862
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

The helper binds only `127.0.0.1`. Every API route requires the dedicated
`X-Dictation-Key` capability, stored in the private `header` file under helper
state. This is separate from Motet's owner key. Keep the header out of URLs,
process arguments, logs and browser assets. A process that can read the owner's
private state can also use that capability; this is not isolation between
processes with access to the same private files.

Host, Origin and authentication headers are checked for duplicates. Foreign
origins, cross-site fetches, redirects in the status probe, ambiguous framing
and noncanonical routes are refused. No CORS access is enabled. A future Motet
adapter should proxy through its own authenticated server and keep this helper
credential out of the page.

| Route | Request | Result |
| --- | --- | --- |
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
Nothing sends an agent message; a future editor retains responsibility for its
explicit Send action.

## Readiness and future design states

| State or condition | Owner and remedy |
| --- | --- |
| `not_installed` | Supervisor: runtime/model/state missing or unsafe; deliberate setup |
| `not_running` | Client probe: helper unavailable; start the separate helper and retry |
| `loading` | Supervisor: validating assets/loading the model; keep the draft |
| `ready` | Helper: ready for a take |
| `listening` | Helper: accepting supplied audio, not evidence of browser mic access |
| `finishing` | Helper: Stop is finalizing; await final events or a safe error |
| `failed` | Helper: runtime/version/model failure; keep draft, fix setup or restart |
| Microphone blocked/disconnected | Future browser capture adapter; helper cannot infer browser permission |

The browser's future sound wave should use an independent audio meter. The
helper's RMS/peak describes the supplied packet and is not recognition confidence.
The supervisor permits no external audio destination. Its child loads local
assets and invokes no download API; a Python audit guard rejects Python network
operations. That guard is not an operating-system firewall for native code.

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
