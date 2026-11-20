# Architecture

transcriptd has three processes. The Python 3.12 FastAPI server owns ASR, vocabulary, the faithfulness guard, SQLite history, and optional meeting summaries. The Rust agent handles global hotkeys, microphone and system audio, and text injection. The Electron client controls the agent over JSON lines and shows partial results, edits, vocabulary prompts, and meeting transcripts. The agent also runs without the client. See [agent IPC](../protocol/agent-ipc.md) for its commands and events.

```
  keyboard + mic + system audio
              |
              v
    Rust agent <-------- JSON lines --------> Electron client
         |                                     (overlay, corrections)
         | PCM/WebSocket                            |
         v                                          | HTTP
  FastAPI server <----------------------------------+
    VAD -> ASR -> vocab -> cleanup -> guard -> SQLite
             |                 |
          HF models         edits + raw words
```

## Push-to-talk

The agent watches the hotkey and records microphone audio. It streams PCM to `/v1/dictate`; Silero VAD segments speech, and live partials go to the overlay only. On release the server runs the final ASR pass and returns the raw words, guarded text, edits, and low-confidence words. The client can show the diff; the agent pastes the selected output into the focused application using uinput on Linux or SendInput on Windows. Partial text is never injected.

## Phone

On Android, dictation works like Wispr Flow's: the app (`android/`, see [Android](android.md)) is an accessibility service that floats a small bubble over whatever app has a text field focused while the keyboard is up. Hold it to talk, tap it for hands-free. The result goes in at the cursor through the accessibility input connection, and up to 400 characters before the cursor and 100 after it are sent as `context`, so `insert_text` fits the spacing and case. Like the agent, it streams to `/v1/dictate` and keeps the take locally for an upload fallback. It talks to the server's Tailscale address directly.

The server also hosts a full-screen installable web app at `/m/` (`server/transcriptd/web/`, plain ES modules, no build step). It is laid out for the Galaxy Z Flip cover screen first, using measured geometry: the Flip 7's 948×1048 px panel at density 2.625 is 361×399 CSS px (542×599 at a smaller display size), and the Flip 5/6 is 374×360. Held upright, the cameras are to the bottom right; on the Flip 7 they sit inside the screen from about 43% of the width to the right edge, over the bottom fifth. The cover layout is chosen by a media query for a small window on a small, near-square screen, keeps that bottom band free of controls, and puts only the style chip in the band's free left end. The main action is one large button above it. Holding the button is push-to-talk, a quick tap starts hands-free recording, and sliding off the button discards the take. The app opens the microphone and the `/v1/dictate` socket together and buffers audio until the server is ready, so the first words are not lost. Every frame is also kept locally; if the socket fails, the same take is uploaded to `/v1/transcribe`, or kept on screen for a retry.

The phone uses speech-to-text profiles only (Whisper first; never `chat_audio`) and keeps the LLM pass on by default (`"llm"` in the start message, a switch in both apps). The LLM gets plain text and may fix punctuation, capitals and numbers, and with `[cleanup] cuts` (on by default) delete ramble: verbal tics ("so" and "like" mid-sentence too, where they aren't "therefore" or "looks like"), repeats, false starts, self-corrections, trailed-off half-sentences (with a "no, I don't want X" that only takes one back), thinking aloud from a fixed list of phrases ("what am I saying?", "buckle up"; set off by punctuation or a joining "so"/"but"/"and") and asides: a whole clause or sentence between Whisper's punctuation with no negation, time word, condition, number, request, commitment or name in it, and at most half the take. It may also make lists (one item per line, "- " or "1. "; an item may drop only the words that joined it to the one before, and a list the guard can't keep whole is run back into sentences), write contractions and spoken forms properly ("dont wanna" -> "don't want to"), fix verb agreement ("it get" -> "it gets", never a tense) and write times as a clock ("5 30" -> "5:30"). It may never add a word or swap one for a different one. Takes of `llm_long_words` (40) or more go to `[llm_long]` when it's set, a bigger model that keeps the facts in a long ramble that a small one drops, with `[llm]` as the fallback. A Note keeps a list the LLM made; otherwise the style lays the text out. The style's layout is applied after it: the styles (Message, Note, Email) are deterministic layouts in `cleanup.py` built on that punctuation: line breaks, bullets, colons and capitals. So the LLM never sees or has to preserve bullets, and with the LLM off the layouts still work on the ASR model's own punctuation. The guard carries line breaks and bullet markers across as layout, never as words; the only word a style may drop is the "and" that joins the last item of a list, and only right before a bullet. `/v1/dictations/{id}/restyle` re-runs everything after ASR on the stored raw words, so switching style needs no new recognition pass. Tapping a word opens a fix sheet that posts the edit to the correction loop. A single common word ("to" -> "two") is recorded but never learned.

Browsers only allow the microphone on secure origins, so the web app reaches the server over HTTPS: `tailscale serve --bg --https=7443 http://<host>:7411` on the tailnet, then `transcriptd mobile` prints a pairing link that carries the token in the URL fragment.

## Meetings

The agent records the microphone as "me" and system loopback as "them" on separate channels, keeping a local WAV while it streams to the server. The server transcribes speech segments and merges them by timestamp. Remote speaker diarization is optional via pyannote community-1. Stopping a meeting saves the transcript independently of the optional summary step. Summaries use the configured OpenAI-compatible endpoint. Meeting capture, diarization, and the client UI are still under development; this describes the intended flow, not a promise that every step is available yet.

## Faithfulness and corrections

The pipeline is `ASR -> vocabulary -> deterministic cleanup -> guard`. Raw ASR word objects and timestamps stay available. The vocabulary matcher can replace a low-confidence phonetic match with a saved term or apply an explicit learned correction. Cleanup handles casing, punctuation, stutters, and configured fillers. The guard aligns raw and proposed words. It rejects content-word substitutions and any deletion that isn't a verified ramble cut, and restores the verbatim span. A cut is verified piece by piece: a verbal tic opening a sentence or set off by commas, a repeat or restart whose opening is said again right after, a self-correction behind a set-off cue ("at 7, actually 3"), or a half-sentence that trails off with "..." from its start; when only part of a cut checks out, that part goes and the rest is put back. Cuts never take a negation, time word, condition or number (a repeat may, since it's said again). Negations and temporal words in English, Hindi, and Marathi get special protection. The response includes every accepted and rejected edit so the client can underline changes instead of hiding them. Cleanup cannot fix an ASR mistake on its own; users must correct the underlying word.

A user correction in the overlay, a correction hotkey after editing pasted text, or a low-confidence unknown word can prompt a vocabulary suggestion. Accepted terms and "heard as" aliases become available to later dictations. Automatic phonetic replacement still requires sufficient similarity and low ASR confidence.

## Engines and profiles

The registry loads one engine per named profile on demand and caches it. `ggml` uses transcribe.cpp and accepts a local `.gguf` path or a Hugging Face repo ID. For repo IDs it downloads the requested `quant` variant into `models/` on first use. `hf` uses Transformers/PyTorch for models without a ggml port. `openai_compat` sends transcription to an external compatible endpoint. The built-in `english` profile uses Parakeet TDT 0.6B v2 F16; `multilingual` uses Whisper large-v3-turbo Q8_0 for English, Hindi, and Marathi. The default is `english`; choose another profile in the agent or configuration. See [models](models.md) for limitations and licenses.
