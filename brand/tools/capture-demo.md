# Workspace demo captures

Run this recipe after the demo data has passed review. It captures the built-in fictional demo from the current checkout and never runs the application server. An existing Playwright package and an installed Chromium browser are required. Set `NODE_PATH` when Playwright is provided by a separate local runtime.

```sh
node brand/tools/capture-demo.js \
  --browser /path/to/installed/chromium \
  --output /path/to/private/capture-folder \
  --phone /path/to/private/capture-folder/workspace-desk-phone.png \
  --metadata /path/to/private/capture-folder/capture.json
```

The output folder receives `workspace-room.png`, `workspace-desk.png` and `workspace-motet.png`, each at 1600 × 900. The phone capture is Ada's conversation at 390 × 844. Review every picture before copying the three desktop files into `docs/media/`. The phone image and metadata belong in a private scratch folder outside the checkout.

The recipe binds its own static server to `127.0.0.1` on an automatically assigned port. An optional `--port` selects a spare port. It refuses 8793, 18879 and 18959. The browser binary can also be supplied through `MOTET_CAPTURE_BROWSER`.

Each run uses a fresh browser context, scale factor 1, English labels, UTC, light theme and reduced motion. Chromium runs with software rendering and an sRGB color profile. A script injected before page startup fixes the clock at 20 November 2026, 15:00 UTC and holds the demo at its first synthetic tick. The product's source, styles, camera settings and interactions are used as written. The script waits for fonts, image decoding and settled stage transforms. It moves the pointer above the stage and hides the text caret while capturing.

The sequence is the stage, Ada's Changes panel, Ada's phone conversation, then Motet's desk answering “Who is waiting on me?” from the same fictional moment. Network requests are restricted to static files on the temporary server. Dynamic fetches, connections, storage writes, microphone access, notification requests and other permission calls fail the capture audit. Before every picture, the recipe checks that the cast, parent relationships, turns, commands, permissions and recorded stories still match the first picture. The JSON metadata records the commit, source hash, actual browser and Playwright versions, operating system, computed font families, clock, cast, request paths, audit and image hashes. Review the audit before publication.

Repeat captures using the same commit, browser build, Playwright version and operating-system fonts. Runtime metadata identifies those conditions. Matching output across different browser or font environments is not guaranteed. The six Classic images in `docs/TOUR.md` have a separately approved later recapture step.

## Check inertness and compare the baseline

With an existing Playwright runtime available, the companion check compares the current page with a private export of the seven-agent baseline.

```sh
node brand/tools/check-demo.cjs \
  --baseline /path/to/private/baseline/src/web \
  --output /path/to/private/demo-check.json
```

It observes the demo's guarded operations and profiles the real scripted tick plus render, and navigation across two animation frames. The automatic 3500 ms tick is suppressed and its real callback is invoked for each timing sample. Two paired runs reverse the baseline/candidate order and omit four warmups per run. The report records CPU and navigation latency samples, median and p95. Those measurements are not a frames-per-second benchmark.
