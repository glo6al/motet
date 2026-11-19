# Isolated dictation page review

This fixture serves the actual Motet page, dictation controller, editor view,
AudioWorklet and server proxy. It starts no observers and connects no real agents
or provider. Ordinary Send and action requests receive a refusal; attempted-send
counts are kept in memory without their bodies.

## Simulated audio, with no microphone

From the repository root, create a dedicated private preview home and start:

```sh
DICTATION_FIXTURE_HOME="$(mktemp -d)"
python3 docs/fixtures/dictation/serve.py --home "$DICTATION_FIXTURE_HOME" --port 18881 --synthetic
```

Open `http://127.0.0.1:18881/review`. A visible label says **Simulated audio ·
microphone not used**. Press the usual microphone button. That explicit gesture
starts a generated sine-wave MediaStream, sent through the actual AudioWorklet
and authenticated proxy to the real helper service with a fake recognizer. No
real `getUserMedia` is invoked, no sound is sent to speakers, and no audio is
saved. The helper gets an ephemeral port and a separate private capability in the
chosen preview home; neither capability nor helper address reaches the page.

The words are a fixed script based on received PCM packet counts. They are
**visual test data**, not recognition or accuracy evidence. After a phrase
finishes, the next phrase remains provisional so normal ink, grey text, italics
and the waveform can be reviewed together. Stop finalizes the current partial.
The first phrase deliberately changes its earlier word “agent” to “colleague”
once five words are visible, on the same recognition line. This tests a native
caret inside revised text; it is an intentional scripted correction.
Cancel drops the take through the normal controller. Editing the active phrase
exercises the real epoch-reset path; old-epoch fixture events are discarded by
the real service. Typing, selection and caret behavior use the real textarea.

Stopping tracks, hiding/leaving the page or closing the fixture releases the
generated audio sources and helper resources. Stop the fixture with Ctrl+C.

`http://127.0.0.1:18881/fixture-status` reports counts only: received dictation
requests, audio packets, takes, resets and refused sends. It retains no entered
message bodies. This is useful for checking that the worklet delivered PCM and
that no real message was sent.

## Fictional desks and attachment tray

The same `/review` page receives a fixed fictional fleet through the real event
parser. Choose any desk from the stage, or open these bounded variants:

| Query after `/review` | State |
| --- | --- |
| `?desk=claude` | Claude sample, direct-message availability shown |
| `?desk=codex` | Codex sample, direct-message availability shown |
| `?desk=cursor` | Cursor sample, direct messaging unavailable |
| `?desk=antigravity` | Antigravity sample, direct messaging unavailable |
| `?desk=subagent` | Claude subagent, direct messaging unavailable |
| `?desk=ended` | Ended Codex turn, direct messaging unavailable |
| `?desk=unavailable` | Explicitly unavailable connection |
| `?desk=motet` | Motet's own desk |
| `?desk=claude&attachments=a03-one` | Real attachment controller and one fictional file |
| `?desk=claude&attachments=a03-five` | Real attachment controller and five fictional files |
| `?attachments=a03-room-one` | Stage composer and one fictional file |
| `?attachments=a03-room-five` | Stage composer and five fictional files |
| `?desk=motet&attachments=a01-officer` | Motet desk with a fictional file |

The generated-audio microphone works on these review desks through the same
controller and worklet. Capability notices describe the fictional desk; even the
two desks showing available messaging refuse Send on the fixture server. All
product POST actions, provider changes and unknown routes are refused. A sample
attachment Send is intercepted before the canonical fixture can simulate a
delivery, and a body-free refusal is counted instead. No real files are opened,
uploaded or saved. All attachment contents are authored samples already in the
repository's attachment fixture.

Only an explicitly requested attachment variant changes the trusted `/review`
response's demo-only installer gate. The rewrite requires exactly one known
source anchor and a fixture bootstrap flag tied to `/review`; no flag is added
to product files. Without that flag the original gate still rejects a live page.
`?demo` exits before the flag, authentication, storage or generated audio setup.
The real textarea selector remains `#ofIn`; desks use the visible labels above,
and the generated-audio label remains `#dictationSyntheticLabel`.

For optional motion diagnostics, open `/review?motionProbe=1`. The hidden,
unannounced `#dictationMotionProbe` contains JSON for the last 12 actual WAAPI
animations on `#officer`, `#ofSuggestions`, `#workspaceOfficerPrompts` and `#ofLog`. Each record includes the
browser animation's duration/easing, target, bounded supplied height/opacity
keyframes, finish/cancel state and up to 24 samples of actual bounding height,
computed opacity and animation time. A separate `settled` sample observes the
next frame after finish/cancel, including geometry after the effect is removed. The
fixture observes these calls without starting animations or changing their
options. Use the normal arrow or an action that automatically opens the history,
then read the output's `textContent` or `data-last-*` attributes through the DOM.
Reload to clear records; omit `motionProbe=1` to remove the diagnostic entirely.
It is never installed in `?demo` or a product page.

## Strict demo

`http://127.0.0.1:18881/?demo` remains the normal demo. Its controls do not start
capture, worklets, helper requests or storage, even when the fixture process has
`--synthetic`. `/review?demo` also skips fixture authentication and generated
capture before either is installed. The simulated override exists only in the
trusted `/review` bootstrap; it is not a product API or feature flag.

## Existing local helper, explicitly selected

To review a separately prepared helper instead, omit `--synthetic` and supply its
port. It must use the same dedicated preview home:

```sh
python3 docs/fixtures/dictation/serve.py --home "$DICTATION_FIXTURE_HOME" --port 18881 --helper-port 18879
```

In this mode `/review` uses the browser's real microphone permission flow when
the microphone is pressed. The fixture does not start, install or download the
native helper or model. `--helper-port` and `--synthetic` are mutually exclusive.
Without either option, `/review` can exercise the not-configured state.

Private-state integration checks run on POSIX; Windows native behavior is not
verified by this fixture. No command here changes the running Motet instance.
Preview port 8793 is refused.
No phone or HTTPS setup is included. There are no package dependencies or model
downloads for synthetic mode.

Run its finite checks with:

```sh
python3 tests/test_dictation_fixture.py
```
