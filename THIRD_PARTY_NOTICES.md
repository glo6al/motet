# Third-party notices

## Lucide Settings and Folder Open icons

The workspace Stage controls button uses the Lucide Settings icon, and project marks use the Lucide Folder Open icon.
Source: https://github.com/lucide-icons/lucide

ISC License

Copyright (c) 2026 Lucide Icons and Contributors

Permission to use, copy, modify, and/or distribute this software for any
purpose with or without fee is hereby granted, provided that the above
copyright notice and this permission notice appear in all copies.

THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR DISCLAIMS ALL WARRANTIES
WITH REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES OF
MERCHANTABILITY AND FITNESS. IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR
ANY SPECIAL, DIRECT, INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES
WHATSOEVER RESULTING FROM LOSS OF USE, DATA OR PROFITS, WHETHER IN AN
ACTION OF CONTRACT, NEGLIGENCE OR OTHER TORTIOUS ACTION, ARISING OUT OF
OR IN CONNECTION WITH THE USE OR PERFORMANCE OF THIS SOFTWARE.

## Bravura whole-note glyph

The Motet wordmark's “o” and standalone icon adapt the whole-note glyph from
Bravura by Steinberg Media Technologies GmbH. Bravura is licensed under the
SIL Open Font License, version 1.1. The artwork uses outlines, and the font
files are not distributed with Motet.

[Bravura source and upstream licence](https://github.com/steinbergmedia/bravura/blob/master/LICENSE.txt)

## Optional local dictation

The repository contains integration code and a model download manifest, not
Moonshine wheels, native libraries or model binaries. Recognition is optional
and separately installed. The integration selects `moonshine-voice` 0.1.6 and
English Small Streaming `quantized_26_11_02`; it does not select upstream's legacy
non-English non-streaming models governed by different terms.

### Moonshine code and selected English streaming model

Source and licence: https://github.com/moonshine-ai/moonshine/blob/v0.1.6/LICENSE

Checked 2026-11-19: the opening scope statement at that pinned tag explicitly
includes every English-language and streaming speech-to-text model under MIT,
then lists the legacy non-English non-streaming exceptions. Section 1 supplies
the copyright and MIT terms reproduced below. The selected English Small
Streaming model is within that scope; no Community License model is selected.

MIT License

Copyright (c) 2025 Useful Sensors, Inc. (dba Moonshine AI)

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

### Optional runtime dependencies

The runtime's base dependency declarations are separate from Motet's Python
standard-library server. Installing optional packages retains their own notices;
an executable or wheel redistribution must include the exact installed binary
components' notices as well. Version/platform-specific dependencies may vary.
Training/finetuning extras are not needed by this integration.

The versions below identify the versioned licence references checked here, not
an installation lock. The optional package does not pin every transitive
version. Preserve the notices from the exact package and platform installed.

| Component / reference version | Terms and pinned upstream notice | Checked |
| --- | --- | --- |
| NumPy 2.5.5 and bundled components | [BSD-3-Clause](https://github.com/numpy/numpy/blob/v2.5.5/LICENSE.txt), plus exact wheel notices (including MIT, 0BSD, Zlib and CC0 where applicable) | 2026-11-19 |
| python-sounddevice 0.5.6 | [MIT](https://github.com/spatialaudio/python-sounddevice/blob/0.5.6/LICENSE) | 2026-11-19 |
| PortAudio v19.7.0 in the referenced sounddevice binary recipe | [MIT-style licence](https://github.com/PortAudio/portaudio/blob/v19.7.0/LICENSE.txt) | 2026-11-19 |
| Requests 2.34.2 | [Apache-2.0](https://github.com/psf/requests/blob/v2.34.2/LICENSE); preserve its NOTICE | 2026-11-19 |
| tqdm 4.70.1 | [MPL-2.0 and MIT](https://github.com/tqdm/tqdm/blob/v4.70.1/LICENCE), by component | 2026-11-19 |
| filelock 4.2.12 | [MIT](https://github.com/tox-dev/filelock/blob/4.2.12/LICENSE) | 2026-11-19 |
| platformdirs 4.14.7 | [MIT](https://github.com/tox-dev/platformdirs/blob/4.14.7/LICENSE) | 2026-11-19 |
| google-crc32c 1.9.0 | [Apache-2.0 release metadata](https://pypi.org/project/google-crc32c/1.9.0/), [immutable upstream licence reference](https://github.com/googleapis/python-crc32c/blob/385c745fd13ece0dbf76339854ad8a30709a8b0d/LICENSE) | 2026-11-19 |
| CFFI 2.1.1 | [MIT-0](https://github.com/python-cffi/cffi/blob/v2.1.1/LICENSE) | 2026-11-19 |
| libffi in applicable CFFI builds | MIT: [CFFI's vendored Windows notice](https://github.com/python-cffi/cffi/blob/v2.1.1/src/c/libffi_x86_x64/LICENSE), [Linux recipe's v3.4.6 notice](https://github.com/libffi/libffi/blob/v3.4.6/LICENSE) | 2026-11-19 |
| pycparser 3.12 | [BSD-3-Clause](https://github.com/eliben/pycparser/blob/release_v3.12/LICENSE) | 2026-11-19 |
| charset-normalizer 3.5.2 | [MIT](https://github.com/jawah/charset_normalizer/blob/3.5.2/LICENSE) | 2026-11-19 |
| idna 3.20 | [BSD-3-Clause](https://github.com/kjd/idna/blob/v3.20/LICENSE.md) | 2026-11-19 |
| urllib3 2.8.0 | [MIT](https://github.com/urllib3/urllib3/blob/2.8.0/LICENSE.txt) | 2026-11-19 |
| certifi 2026.10.23 | [MPL-2.0](https://github.com/certifi/python-certifi/blob/2026.10.23/LICENSE) | 2026-11-19 |
| ONNX Runtime in Moonshine 0.1.6 | [MIT notice vendored by the selected runtime](https://github.com/moonshine-ai/moonshine/blob/v0.1.6/core/third-party/onnxruntime/LICENSE.txt) | 2026-11-19 |

The PortAudio version is selected by sounddevice's [pinned binary build
recipe](https://github.com/spatialaudio/portaudio-binaries/blob/bf2681cd2c6c88c2ad83341049b2420d805d5353/.github/workflows/build-libs.yml);
applicable wheel notices can include additional components. CFFI's [versioned
build workflow](https://github.com/python-cffi/cffi/blob/v2.1.1/.github/workflows/ci.yaml)
selects libffi 3.4.6 on Linux; an exact macOS libffi revision is not established
by those references. The google-crc32c licence commit is a licence reference,
not a claimed release build commit. Moonshine's [pinned library
selection](https://github.com/moonshine-ai/moonshine/blob/v0.1.6/core/third-party/onnxruntime/find-ort-library-path.cmake)
references ONNX Runtime 1.23.2 on macOS; the vendored notice is not a reproduced
binary build audit. Exact native notices remain necessary for redistribution.

Motet's integration does not call sounddevice recording or playback APIs.
Microphone capture occurs in the browser; the optional Moonshine package still
declares sounddevice as a dependency and its installed notices apply. Upstream's
[pinned on-device documentation](https://github.com/moonshine-ai/moonshine/blob/v0.1.6/README.md)
describes operation without an account or API key. This integration uses that
local recognition path and requires no paid recognition service (checked
2026-11-19).
