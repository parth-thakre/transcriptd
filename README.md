# transcriptd

Self-hosted push-to-talk dictation and meeting transcription for Windows and Linux.
A Wispr Flow / Typeless replacement that you run on your own GPU.

## The one rule

**The words the speech model heard are the source of truth. Nothing downstream may
delete or replace a content word.**

Cleanup (punctuation, casing, filler removal, and with the LLM pass cutting rambles:
verbal tics, repeats, false starts, self-corrections like "at 7, actually 3", half-sentences taken back, thinking aloud,
asides off the point) runs as a
separate pass and every edit it makes is checked by a guard that word-aligns the cleaned
text against the raw transcript. Any edit that substitutes a content word, or removes one
outside those verified cuts, is rejected and the verbatim words are used instead. Negation and temporal words ("not", "never", "before",
"नाही", "नहीं", "कधीच") are protected unconditionally. The client underlines every change
so you can see at a glance what was touched.

Numbers are compared by value, sign and unit: "1.5" never becomes "15", "-5" never "5".

**The one exception is opt-in.** The **Ramble** style (`rewrite = true` on a style, off by
default) lets the LLM rewrite a take into what you meant, in new words. It is not held to the
word-for-word guard. Instead `server/transcriptd/ramble.py` checks facts heuristically: no
number, name or time that wasn't said; no negation, number or name dropped; no word flipped to
an opposite that wasn't said ("demand" to "refuse", "no" to "yes"); at least 40% of the take
kept. These checks catch common slips, not every change of meaning a free rewrite can make. A
rewrite that fails falls back to the guarded cleanup, and the raw words are always kept with
the result. Don't pick Ramble if you need verbatim text.

## What it does

- Push-to-talk dictation into any application: evdev/uinput on Linux (Wayland and X11),
  a keyboard hook and SendInput on Windows. No browser extensions. Linux needs a udev rule
  that lets your user read keyboards and inject input; read [SECURITY.md](SECURITY.md) first.
  After a paste the agent reads the focused field back through AT-SPI (Linux) or UI
  Automation (Windows) to learn the words you correct; that text stays on your machine.
- Meeting transcription from your microphone plus system audio. No bots join your calls.
  Your side and the remote side are captured as two channels; remote speakers are
  diarized.
- Android: a floating bubble over any app you're typing in, like Wispr Flow's. It is an
  accessibility service. Hold it and talk; the cleaned text goes in at the cursor. Built for
  the Galaxy Z Flip cover screen too. What it reads and sends: [docs/android.md](docs/android.md).
  The server also hosts a full-screen web app at `/m/`.
- English, Hindi and Marathi in one transcript, each language in its own script.
- Custom vocabulary with a correction loop: fix a word once and transcriptd offers to
  remember it.
- Optional meeting summaries through any OpenAI-compatible endpoint (local llama.cpp,
  Ollama, vLLM, or a hosted API). Transcripts are saved whether or not you use this.

## Layout

| Directory   | What                                                                 |
|-------------|----------------------------------------------------------------------|
| `server/`   | Python FastAPI server: ASR engines, vocabulary, guard, diarization, storage |
| `agent/`    | Rust sidecar: hotkeys, audio capture, text injection. Also a CLI      |
| `client/`   | Electron app: tray, overlay, vocab popup, meetings UI, settings       |
| `android/`  | Android app: the floating dictation bubble ([docs/android.md](docs/android.md)) |
| `server/transcriptd/web/` | Full-screen phone web app (PWA, no build step), served at `/m/` |
| `protocol/` | Wire formats shared by the parts                                     |
| `docs/`     | Architecture, model licenses, self-hosting                           |

## Status

Early, and built for one person's use first. Meeting and client work is ongoing. See
[architecture](docs/architecture.md), [models and licenses](docs/models.md),
[self-hosting](docs/self-hosting.md), [security](SECURITY.md), the
[desktop client](client/README.md) and the [Android app](docs/android.md).

## Quick start

```sh
cd server && uv sync
cp config.example.toml config.toml
uv run transcriptd token
uv run transcriptd serve
# In another terminal, from the repository root:
cd agent && cargo build --release
./target/release/transcriptd-agent --server http://127.0.0.1:7411 --token YOUR_TOKEN run
# Desktop client (tray, overlay, settings): cd client && bun install && bun run dev
```

The server listens on `127.0.0.1:7411` by default. To use it from other machines, set
`host` in `config.toml` to your Tailscale address (or `0.0.0.0` on a trusted network), or put
it behind a TLS reverse proxy; the agent accepts `https://` server URLs.

Linux agent access needs the [udev rule](packaging/70-transcriptd.rules) and
`pw-record` or `parec`. See [self-hosting](docs/self-hosting.md) for setup, GPU
choices, and containers, [client/README.md](client/README.md) for the desktop app and
[docs/android.md](docs/android.md) for the phone.

## Known limitations

- The focus check before typing only works where the accessibility layer can identify the
  field; apps that expose nothing (many terminals, games) get the text without the check.
- The correction hotkey copies the selection with Ctrl+C; in a terminal that interrupts the
  running program instead of copying.
- Paste-based injection restores files, text (with HTML) and images on the clipboard; other
  formats (RTF, app-specific data) are lost, and images are re-encoded.
- Meeting recordings don't sync the microphone and system-audio clocks: system audio is padded
  with silence when it lags and trimmed when it runs more than 200 ms ahead, so expect small
  gaps and some skew over long meetings.
- The capture device opens after the hotkey press, so a word spoken in the same instant can be
  clipped.
- TLS in the agent trusts the operating system's certificate store only (add a private CA
  there); client certificates aren't supported.
- Windows builds are unsigned and can trigger SmartScreen.

## License

MIT for the code in this repository. Speech model weights are not bundled; each model's
license is listed in `docs/models.md`.
