# ODD ARCHIVE build status

## Included

- Groq chat-completions (`openai/gpt-oss-20b`), JSON mode, `include_reasoning=false`, low reasoning effort.
- Token budget auto-raised when a response is truncated (`finish_reason == "length"`).
- Server-directed 429 retry delays and a conservative interval between requests.
- Separate outline and per-scene generation with per-scene JSON checkpoints under `work/`.
- GitHub Actions cache restore at start and save with `if: always()` (JSON checkpoints only), so failed runs resume.
- Pixabay search that skips empty video URLs, prefers up to 1080p, and retries with progressively shorter queries.
- Topic rotation from `topics.txt` for scheduled runs.
- Edge TTS narration, FFmpeg 1080p rendering, thumbnail, metadata, script, downloadable artifact.
- Optional YouTube OAuth upload, disabled unless `AUTO_UPLOAD=true`.

## Checked in the build environment

- Python compilation, YAML parsing, `main.py --self-test`.
- 9 unit tests (JSON parsing, Groq settings, 429 retry, truncation handling, scene resume, Pixabay selection, topic rotation).
- Synthetic 10-scene render with mocked Groq/Pixabay/TTS: valid 1920x1080 H.264/AAC MP4 plus thumbnail, metadata and script.

## Not verified

- Live Groq, Pixabay, Edge TTS and YouTube calls (they need your secrets and network access).
