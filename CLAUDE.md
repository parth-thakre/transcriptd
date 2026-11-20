# transcriptd

Self-hosted push-to-talk dictation + meeting transcription. Four parts, one repo:
`server/` (Python, FastAPI), `agent/` (Rust sidecar), `client/` (Electron), `android/` (Kotlin). Wire formats
in `protocol/`. Design rationale in `docs/architecture.md`.

## The rule that governs every change

Raw ASR words are the source of truth. Nothing downstream may add or replace a content
word, or delete one except as a verified ramble cut (`_cut_ok`: a set-off verbal tic, a repeat
or restart, a self-correction, a trailed-off half-sentence (with the "no, I don't want X" that
takes it back, if X says nothing more), thinking aloud from a fixed phrase list ("what am I
saying", "buckle up"), an aside: a whole clause between Whisper's punctuation with no request,
commitment or name in it, at most half the take; `[cleanup] cuts`). With cuts the LLM may also make lists (an item drops only the "and"/"then"/
"first" joining it; half a list falls back to sentences), write contractions and spoken forms
properly ("dont wanna" -> "don't want to"), fix agreement ("it get" -> "it gets", never a
tense) and write times as a clock. Negation, time
and condition words and numbers are never cut; negation/temporal words are protected
unconditionally. `server/transcriptd/guard.py` enforces this and `tests/test_guard.py` is the spec. If a feature needs to change text,
it must produce an edit the guard can verify (add an allowlisted edit kind + tests), never
bypass it.

The one exception is the opt-in **Ramble** style (`rewrite = true`): the LLM rewrites the take
into what the speaker meant, with new wording, and `server/transcriptd/ramble.py` checks facts
instead of words (no number, name or time word that wasn't said; no negation, time word, number
or name dropped; no word flipped to an opposite that wasn't said, e.g. "no" -> "yes"; at least
40% of the take left). A failed
check falls back to the guarded cleanup. `tests/test_ramble.py` is its spec; the raw words are
always in the result.

## Server (`server/`)

```
uv sync                                   # core; add --extra hf --extra torch-rocm|torch-cuda|torch-cpu for the HF engine
export TRANSCRIBE_LIBRARY=$PWD/.native/transcribe.cpp/build/src/libtranscribe.so   # optional HIP/CUDA build (scripts/build-transcribe-cpp.sh)
uv run transcriptd serve                  # 127.0.0.1:7411, token in data/token (`uv run transcriptd token`)
uv run pytest -q                          # ~1-5 s; downloads Silero VAD to models/ on first run
uv run transcriptd mobile                 # pairing links: Android app (transcriptd://pair) and web app (/m/)
uv run transcriptd telemetry              # per-take phone reports + audio stats; --export DIR for `bench`
uv run transcriptd transcribe file.wav --profile english
uv run transcriptd bench DIR --profiles english multilingual
uv run transcriptd models pull            # pre-download profile models
```

- Python 3.12 pinned (3.14 lacks ML wheels). Config: `config.toml` (gitignored; template
  `config.example.toml`).
- ASR engines live in `transcriptd/asr/`; `ggml.py` (transcribe.cpp bindings) is primary.
  One run in flight per model (binding limitation) — `GgmlEngine` holds a lock.
- Pipeline order is fixed: ASR -> vocab matcher -> cleanup -> guard (`pipeline.py`).
- Do not use `pkill -f "transcriptd serve"` from a tool shell: it matches the tool's own
  wrapper. Kill by port owner: `ss -ltnpH 'sport = :7411'`.
- LLM passes (`[llm]`, optional `[llm_long]` for takes of `cleanup.llm_long_words`+ words) go to
  any OpenAI-compatible endpoint; a small model drops facts from long rambles, hence `[llm_long]`.
- Security-relevant defaults (see `SECURITY.md`): loopback bind, token file 0600, WS tickets for
  the web app, proxy headers ignored (`trust_proxy_headers`), tailnet-identity requests need one of
  our own host names (`allowed_hosts`, DNS rebinding) and a matching `Origin`, upload/queue limits.
- Phone app (`transcriptd/web/`): plain ES modules, no build step, served at `/m/`. Designed for
  the Z Flip cover screen: Flip 7 = 361x399 CSS px (542x599 small display size), Flip 5/6 = 374x360;
  cameras bottom RIGHT (Flip 7: x from ~43%, bottom ~22%), kept clear via `--band` in `app.css`.
  STT profiles only, Whisper first; LLM pass on (switchable); styles are deterministic layouts in
  `cleanup.py`. Needs HTTPS for the mic (e.g. `tailscale serve --bg --https=7443
  http://<server>:7411`). Test headless with Chromium's `--use-file-for-fake-audio-capture=<wav>`,
  viewport AND screen 361x399 (the query checks both).

## Android (`android/`)

```
cd android && ./gradlew testDebugUnitTest assembleDebug   # JDK 21, Android SDK 36; APK in app/build/outputs/apk/debug/
```

- A Wispr Flow-style floating bubble: `FlowService` (accessibility service) shows `Bubble`
  (TYPE_ACCESSIBILITY_OVERLAY) on every text box (keyboard up or a field focused), on whichever display is on
  (folded, the Flip's cover is display 1: `windowsOnAllDisplays`, overlay via a display context); inserts via
  the accessibility input connection (API 33+), then ACTION_SET_TEXT, then paste.
- Privacy contract (keep `docs/android.md` and the service description in sync): caret context
  is bounded on-device (`CaretContext.kt`); edit learning is opt-in (`learnEdits`) and sends a
  window of at most 4000 chars around the dictation (`EditWatch.window`; all of a short field),
  nothing when unchanged or when the dictation can't be located.
- Plain `http://` only to loopback/private/link-local/Tailscale-IP hosts, no redirects (`ServerUrl.kt`, `Api.kt`).
  With `tailnet_auth = true` (`transcriptd/tailnet.py`) the server lets in devices logged in to
  Tailscale as its own user without the token (`tailscale whois`); build with
  `-PtranscriptdServer=http://<tailscale-ip>:7411` and the app needs no pairing.
  Defaults to a Whisper profile and the LLM pass (`llm` setting); never offers chat_audio profiles.
  Every take posts a report to `/v1/telemetry` (`Telemetry.kt`, `ClientReport` table); the playground
  adds the sentence read out (`expected`), so `transcriptd telemetry --export` yields bench fixtures.
- Debug builds read `files/test.wav` (16 kHz mono) instead of the mic: the headless emulator
  recipe is in `docs/android.md` (`flip7-cover` AVD: 948x1048 @ 420 with Samsung's Flip 7
  cover-display skin; its camera mask is x 457-907, y 843-1017).

## Agent (`agent/`)

```
cargo build --release && cargo test
./target/debug/transcriptd-agent run --no-inject --server http://127.0.0.1:7411 --token $(cd ../server && uv run transcriptd token)
```

- Linux audio is `pw-record`/`parec` child processes (no ALSA/PipeWire build deps; cpal is
  Windows-only). Sink monitors are captured with `-P '{ stream.capture.sink = true }'`.
- Hotkeys need read access to keyboards under `/dev/input/event*`, injection needs `/dev/uinput`:
  `sudo cp packaging/70-transcriptd.rules /etc/udev/rules.d/ && sudo udevadm control --reload && sudo udevadm trigger`.
  Without it the agent still runs; it emits two `error` events at startup.
- TLS via rustls (ring provider installed in `main.rs`); `https://` maps to `wss://`.
- Headless test recipe (no mic): `pactl load-module module-null-sink sink_name=tdtest`, set
  `mic_device` to `tdtest.monitor`, play audio with `pw-play --target tdtest file.wav`.

## Client (`client/`)

```
bun install && bun run typecheck && bun run build && bun run test && bun run dev
```

- Never use Electron `globalShortcut` (broken on Wayland); the agent owns hotkeys.
- Agent path: `TRANSCRIPTD_AGENT` env, packaged `resources/bin/`, or `../agent/target/debug/`.

## Conventions

- MIT code; model weights are never committed or redistributed (`docs/models.md`).
- Windows is built in CI; Windows-only code can't be compiled on Linux, so check it by reading
  and in CI.
- Never commit real IP addresses, hostnames, tokens or real dictations: use RFC 6598
  (`100.64.0.x`) or documentation addresses and invented text in tests and docs.
