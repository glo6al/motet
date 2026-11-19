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

| Component | Terms and upstream notice |
| --- | --- |
| NumPy and bundled components | [BSD-3-Clause and bundled notices](https://github.com/numpy/numpy/blob/main/LICENSE.txt), including MIT, 0BSD, Zlib and CC0 components in applicable wheels |
| python-sounddevice | [MIT](https://github.com/spatialaudio/python-sounddevice/blob/master/LICENSE) |
| PortAudio in applicable sounddevice wheels | [MIT-style licence](https://github.com/PortAudio/portaudio/blob/master/LICENSE.txt) |
| Requests | [Apache-2.0](https://github.com/psf/requests/blob/main/LICENSE) |
| tqdm | [MPL-2.0 and MIT](https://github.com/tqdm/tqdm/blob/master/LICENCE) |
| filelock | [MIT](https://github.com/tox-dev/filelock/blob/main/LICENSE), checked 2026-11-19; verify the notice bundled with the version installed |
| platformdirs | [MIT](https://github.com/tox-dev/platformdirs/blob/main/LICENSE) |
| google-crc32c | [Apache-2.0](https://github.com/googleapis/python-crc32c/blob/main/LICENSE) |
| CFFI | [MIT-0](https://github.com/python-cffi/cffi/blob/main/LICENSE) |
| libffi in applicable CFFI wheels | [MIT](https://github.com/libffi/libffi/blob/master/LICENSE) |
| pycparser | [BSD-3-Clause](https://github.com/eliben/pycparser/blob/main/LICENSE) |
| charset-normalizer | [MIT](https://github.com/jawah/charset_normalizer/blob/master/LICENSE) |
| idna | [BSD-3-Clause](https://github.com/kjd/idna/blob/master/LICENSE.md) |
| urllib3 | [MIT](https://github.com/urllib3/urllib3/blob/main/LICENSE.txt) |
| certifi | [MPL-2.0](https://github.com/certifi/python-certifi/blob/master/LICENSE) |
| ONNX Runtime in the Moonshine native runtime | [MIT and third-party notices](https://github.com/microsoft/onnxruntime/blob/main/LICENSE) |

The integration never imports sounddevice or opens a soundcard. The optional
package can still bring it as a declared dependency. No inference account or
paid recognition service is required for the selected local model.
