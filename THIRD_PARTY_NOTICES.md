# Third-party notices

The project's [MIT LICENSE](LICENSE), copyright 2026 the telegram-pack
authors, applies to this project's code. It does not relicense downloaded
models, external tools or add-ons. Retain each upstream notice with any
redistributed copy. This repository does not include transcriber binaries
or model weights.

## Included Markdown runtime

`vendor/` includes markdown-it-py 4.2.0 and mdurl 0.1.2 from verified wheels,
with their upstream MIT licenses, copyright notices and metadata intact.
See [the pins and offline reproduction](vendor/README.md),
[markdown-it-py's notices](vendor/markdown_it_py-4.2.0.dist-info/licenses/LICENSE),
[its markdown-it notice](vendor/markdown_it_py-4.2.0.dist-info/licenses/LICENSE.markdown-it)
and [mdurl's notice](vendor/mdurl-0.1.2.dist-info/LICENSE).

## Optional transcription downloads

Checked against primary upstream sources on 2026-11-09:

| Actual artifact selected by `transcriber/setup.sh` | Upstream terms and provenance |
| --- | --- |
| whisper.cpp source, default pin `d1be6fde11ac6e0407606b4e42fe72d34add8037` | [Pinned MIT license](https://github.com/ggml-org/whisper.cpp/blob/d1be6fde11ac6e0407606b4e42fe72d34add8037/LICENSE), copyright 2023–2026 the ggml authors. A supplied `--whisper-ref` or existing checkout requires its own review. |
| `ggml-large-v3-turbo-q5_0.bin` (default); listed alternatives `ggml-small.en.bin`, `ggml-small.en-q5_1.bin`, `ggml-base.en.bin`, `ggml-small.bin` | [The converter's model card](https://huggingface.co/ggerganov/whisper.cpp/blob/5359861c739e955e79d9a303bcbc70fb988958b1/README.md) declares MIT for these ggml Whisper weights. Upstream [Whisper LICENSE](https://github.com/openai/whisper/blob/main/LICENSE), copyright 2022 OpenAI, and [large-v3-turbo model card](https://huggingface.co/openai/whisper-large-v3-turbo) supply the original terms/model context. |
| `ggml-silero-v6.2.0.bin` (default VAD) | [The actual ggml VAD repository's card](https://huggingface.co/ggml-org/whisper-vad/blob/9ffd54a1e1ee413ddf265af9913beaf518d1639b/README.md) declares MIT. [Artifact provenance](https://huggingface.co/ggml-org/whisper-vad/blob/9ffd54a1e1ee413ddf265af9913beaf518d1639b/ggml-silero-v6.2.0.bin). Do not substitute another Silero release's terms by inference. |

The six SHA-256 values in [models.sha256](transcriber/models.sha256) were
compared with those repositories' LFS metadata at the above revisions; all
matched. Setup downloads from their `resolve/main` URLs but checks the
recorded hashes before accepting the bytes. Moving `main` cannot silently
replace a listed artifact. This check fetched metadata/cards, not weights.
Setup can reuse a non-empty, locally supplied unlisted Whisper model without
a checksum, and reuse an existing whisper.cpp binary/build. Those bytes and
licenses are operator-owned review inputs; this notice makes no grant or
provenance claim for them. An unlisted VAD download refuses.

`curl`, Python, git, CMake, the C++ compiler, make/Ninja, ffmpeg, systemd/launchd
and optional CUDA/Metal/GPU drivers are separately installed host tools, not
vendored under the project license. Their redistribution has its own terms.
See [prerequisites](README.md#prerequisites); no cloud transcription service
or automatic host-package installer is included.
