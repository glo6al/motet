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
Cancel drops the take through the normal controller. Editing the active phrase
exercises the real epoch-reset path; old-epoch fixture events are discarded by
the real service. Typing, selection and caret behavior use the real textarea.

Stopping tracks, hiding/leaving the page or closing the fixture releases the
generated audio sources and helper resources. Stop the fixture with Ctrl+C.

`http://127.0.0.1:18881/fixture-status` reports counts only: received dictation
requests, audio packets, takes, resets and refused sends. It retains no entered
message bodies. This is useful for checking that the worklet delivered PCM and
that no real message was sent.

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

The authenticated helper fixture currently requires a POSIX host for private-file
permissions, matching the helper itself. No command here changes the running Motet instance. Preview port 8793 is refused.
No phone or HTTPS setup is included. There are no package dependencies or model
downloads for synthetic mode.

Run its finite checks with:

```sh
python3 tests/test_dictation_fixture.py
```
