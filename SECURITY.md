# Security

transcriptd is a self-hosted tool you run for yourself. This page explains what it can reach,
what it stores, and how to report a problem.

## Reporting a vulnerability

Use GitHub's private vulnerability reporting on this repository (Security > Report a
vulnerability). Please don't open a public issue for a security problem. Expect an answer
within a week; this is a one-person project.

## The server

- **Authentication.** Every `/v1` route and WebSocket needs the bearer token (`data/token`,
  created with mode 0600, or `token` in `config.toml`). The phone web app gets a single-use
  WebSocket ticket (`POST /v1/ws-ticket`, valid 60 s) so the token never appears in a URL.
  Uvicorn's access log blanks out any `token=` or `ticket=` query value.
- **Tailnet login (`tailnet_auth = true`, off by default).** The server lets in devices
  logged in to Tailscale as the same user as the server, with no token (`tailscale whois`).
  A request admitted this way must name the server by an IP address, `localhost`, a `*.ts.net`
  name or a host listed in `allowed_hosts` (so a DNS-rebinding page can't reach it under its own
  domain), and is refused if its `Origin` header doesn't match that host and scheme. The client address is
  the TCP peer: `X-Forwarded-For` is ignored unless you set `trust_proxy_headers = true`, and
  then tailnet identity is only as trustworthy as the proxy that sets the header.
- **Binding.** The server listens on `127.0.0.1` by default and speaks plain HTTP. Bind it to
  your Tailscale address or `0.0.0.0` only on a network you trust, or put it behind a TLS
  reverse proxy that forwards WebSocket upgrades. Don't publish port 7411 to the internet.
- **Limits.** Request bodies are capped (`max_upload_mb`, default 25; meeting audio
  `max_meeting_upload_mb`, default 500), a dictation take is capped at
  `max_dictation_seconds` (600, streamed or uploaded), and at most `max_queued_meetings` (8) meetings wait for
  processing at a time.

## What is stored, and where

Everything stays in the server's `data_dir` (default `server/data/`, created 0700 with its database and
audio files 0600), in SQLite and plain files:

| What | When | Removed by |
|---|---|---|
| Dictation history: the raw words, the cleaned text, the app name the client reported | every take | deleting the dictation (phone web app history, or `DELETE /v1/dictations/{id}`) |
| Dictation audio (`data/dictations/<id>.wav`) | only with `keep_dictation_audio = true` | deleting the dictation |
| Edits you made after a dictation went in (up to 4000 characters of the text field around it) | only from the Android app with "Learn from my edits" on, and only when you changed the text | deleting the dictation |
| Corrections (heard → meant word pairs) and vocabulary | when you correct a word | deleting the vocabulary term also deletes the corrections that taught it; corrections stay after a dictation is deleted |
| Per-take telemetry from the Android app (device, mic, timings, how the text went in; no field text) | every Android take | deleting the database rows by hand |
| Meeting audio, transcripts and summaries | when you record a meeting | deleting the meeting (desktop client or `DELETE /v1/meetings/{id}`) |

SQLite runs with `secure_delete`, so deleted rows are overwritten in the database file rather
left in free pages. The server's logs carry counts and kinds of rejected edits, not the words;
the words are only logged at DEBUG level.

The desktop agent also keeps its own copy of each meeting recording (`meetings_dir`, by default
the user data directory's `transcriptd/meetings`, e.g. `~/.local/share/transcriptd/meetings`).
Deleting the meeting on the server doesn't delete that file.

What leaves the server: with the built-in engines (`ggml`, `hf`) speech recognition runs on the
server. A profile using the `openai_compat` or `chat_audio` engine sends the take's audio to
the endpoint it names. With an LLM endpoint configured (`[llm]`, `[llm_long]`) the take's words,
recent edited takes used as examples, and for summaries the meeting transcript go to that
endpoint. Nothing else is sent anywhere.

What clients send with each take: the desktop agent sends up to 2000 characters before the
caret and 500 after it in the focused field (for spacing, capitalization and temporary
vocabulary); the Android app up to 400 before and 100 after. The server doesn't store this
context, but names picked from it can go into the speech-engine and LLM prompts.

## The desktop agent (Linux)

Global push-to-talk on Wayland and X11 reads keyboards directly from `/dev/input/event*`,
and typing the result uses `/dev/uinput`. The [udev rule](packaging/70-transcriptd.rules)
tags keyboards and uinput with `uaccess`, which gives the user logged in at the seat read
access to every keyboard and the ability to inject input.

That is a real widening of what your user account can do: **any program running as you can
then log every keystroke and type into any window**, not just transcriptd. On a single-user
desktop this is usually acceptable (programs running as you can already do a lot); on a shared
or high-security machine, don't install the rule. Without it the agent still runs, but global
hotkeys and typing are unavailable and you can paste the result yourself.

Other agent details:

- `agent.json` (which holds the server token) is written with mode 0600 in a 0700 directory.
- The control socket lives in `$XDG_RUNTIME_DIR` (or a 0700 directory under the user's cache)
  with mode 0600, and accepts only push-to-talk, `inject` and meeting start/stop.
- **Read-back.** After a paste, the agent reads the focused text field through AT-SPI (Linux)
  or UI Automation (Windows) to see what you changed. Turning AT-SPI read-back on enables the
  session's accessibility flags. The field's text stays on your machine; only the corrected
  word pairs (heard → meant) are sent to the server.
- If focus moves to a different field between pressing the hotkey and the result arriving, the
  agent doesn't type: the text goes to the clipboard and you get a notice. This check needs the
  accessibility layer to identify the field at press time; where it can't (many terminals,
  games), the agent types into whatever has focus, as it would without the check.
- `https://` server URLs use TLS (`wss://`) with the operating system's certificate store.

## The Android app

- It is an accessibility service: it sees which app has a text field focused so it can show
  the bubble there. With each take it sends at most 400 characters before the caret and 100
  after it, for spacing, capitalization and language hints.
- "Learn from my edits" (off by default) sends a window of up to 4000 characters around a
  dictation once you're done editing it (all of a short field), so the server can learn the
  words you fix. When it's off nothing is posted. When the text is unchanged, or the dictation
  can't be found in the field any more, no field text is sent (an unchanged take posts only the
  app name and how the watch ended).
- Plain `http://` is allowed only to loopback, private (RFC 1918), link-local and Tailscale
  (100.64.0.0/10) IP addresses; host names, including `*.ts.net`, and anything else must use
  `https://`. The app never follows redirects from the server.
