# Agent IPC protocol

The Electron client spawns `transcriptd-agent run` and talks JSON lines: one object per
line on the agent's stdin (commands) and stdout (events). Logs go to stderr.

## Control socket

`transcriptd-agent toggle`, `inject` and `meeting` reach a running agent (e.g. from a
compositor keybinding) over a single-instance socket: `$XDG_RUNTIME_DIR/transcriptd-agent.sock`
on Linux (without `XDG_RUNTIME_DIR`: `~/.cache/transcriptd/run/`, created 0700), mode 0600;
`\\.\pipe\transcriptd-agent` on Windows, local clients only. The socket is not
authenticated beyond file permissions, so it accepts only `ptt.start`, `ptt.stop`,
`ptt.toggle`, `ptt.cancel`, `inject`, `meeting.start` and `meeting.stop`; everything else
(notably `configure`, `clipboard.read`, `selection.read`, `quit`) is refused and only works
on stdin. Events always go to stdout, never to the socket.

At startup the agent takes an exclusive `flock` on `transcriptd-agent.lock` (0600, next
to the socket) and holds it until it exits; if another agent holds it, the new one exits
after a fatal `error`. Only the lock holder probes an existing socket: a live one (an
older agent) is the same fatal error, a dead one left by a crash is removed. On Windows the
pipe must be created as its first instance, with the same result.

## Commands (client -> agent)

| type              | fields                               | effect |
|-------------------|--------------------------------------|--------|
| `configure`       | `config: AgentConfig`                | replace config, persist to `~/.config/transcriptd/agent.json` (0600: it holds the token) |
| `ptt.start`       |                                      | start recording (as if hotkey down) |
| `ptt.stop`        |                                      | stop, run final pass, inject if `auto_inject` (see below) |
| `ptt.toggle`      |                                      | start if idle, stop if recording |
| `ptt.cancel`      |                                      | drop the recording |
| `inject`          | `text`                               | paste `text` into the focused app |
| `clipboard.read`  |                                      | -> `clipboard` |
| `selection.read`  |                                      | copy the current selection -> `selection` |
| `devices.list`    |                                      | -> `devices` |
| `meeting.start`   | `meeting_id?`, `title?`              | record mic + system audio to a stereo WAV |
| `meeting.stop`    |                                      | finish the WAV |
| `ping`            |                                      | -> `pong` |
| `quit`            |                                      | exit |

`AgentConfig` fields: `server_url` (`http://host:port`, or `https://` behind a TLS proxy;
WebSockets then use `wss://`, verified against the OS trust store), `token`, `profile`,
`language`, `hotkey` (e.g. `ctrl+alt+space`, `rightalt`, `f13`), `correction_hotkey`,
`hotkey_mode` (`hold`|`toggle`), `auto_inject`,
`inject.{method,paste_chord,restore_clipboard,restore_delay_ms,trailing_space}`,
`mic_device`, `loopback_device`, `partials`, `meetings_dir`, `final_timeout_secs`
(default 60: how long after the key goes up the take may take to finish, sending the
remaining audio included).

A press shorter than 150 ms, measured from key down to key up as the hotkey thread saw
them (not from when the server connection was ready), is taken as accidental and dropped.

### Injection safety

At hotkey press the agent notes which field has focus (AT-SPI object on Linux, UIA
runtime id on Windows) and which app owns it (bus name / process id). With `auto_inject`,
it checks again when the transcript arrives. If another field or app has focus, or the
original field can no longer be seen, it does not type:
it puts `text` on the clipboard, sends `final` with `injected: false`, and an `error` with
`code: "focus_changed"`. When no field could be identified at press time (no
accessibility backend, or an app that exposes none), it injects as before. A Windows
control without a runtime id is only checked by process: another app counts as moved,
another field in the same app cannot be detected and the text is typed. The explicit
`inject` command is not checked.

If the take has not finished within `final_timeout_secs` after the key goes up (the
connection stopped taking audio, or the transcript never came), or the server closes the
connection first, the take is dropped with a non-fatal `error` and the agent is idle again.

## Events (agent -> client)

| type               | fields |
|--------------------|--------|
| `hello`            | `version`, `platform`, `hotkey_backend` (`evdev`/`win32-ll-hook`/`none`), `inject_backend` (`uinput`/`sendinput`/`none`), `readback_backend` (`atspi`/`uia`/`none`) |
| `configured`       | `warnings[]` |
| `ptt.down` / `ptt.up` | `source` |
| `level`            | `db` (mic RMS dBFS, ~10 Hz while recording) |
| `partial`          | `text` (display only, never injected) |
| `final`            | `result` (the server's DictationResult: `text`, `raw_text`, `words[]`, `edits[]`, `replacements[]`, `low_confidence[]`, `dictation_id`), `injected` |
| `injected`         | `chars`, `method` |
| `corrections`      | `dictation_id?`, `app`, `corrections[]` of `{heard, meant}` (edits the user made after a paste, read back through accessibility) |
| `clipboard` / `selection` | `text?` |
| `devices`          | `inputs[]`, `outputs[]` of `{name, default}` |
| `meeting.started`  | `meeting_id?`, `wav_path` |
| `meeting.level`    | `me_db`, `them_db` |
| `meeting.segment`  | `meeting_id?`, `segment` (server live segment: `id`, `channel`, `speaker`, `start_ms`, `end_ms`, `text`, `raw_text`, `live`) |
| `meeting.stopped`  | `meeting_id?`, `wav_path`, `duration_ms` |
| `error`            | `message`, `fatal`, `code?` (`focus_changed`: text copied instead of typed) |

A `final` with an empty `text` means the utterance was too short or silent.
