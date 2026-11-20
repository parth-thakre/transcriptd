# transcriptd desktop

Electron tray app for Windows and Linux. The Rust agent owns recording, global hotkeys, clipboard injection, and meeting audio. React runs in isolated renderer windows; all HTTP requests and sidecar commands go through the main process.

## Development

1. In `agent/`, run `cargo build`. On Linux, install the udev rule in `packaging/70-transcriptd.rules` as described by the agent documentation. Without input-device permissions, global hotkeys and injection may be unavailable.
2. Start the server on `http://127.0.0.1:7411`. In `server/`, run `uv run transcriptd token` to get its bearer token.
3. In `client/`, run `bun install`, then `bun run dev` on a machine with a display.
4. Open Settings, enter the server URL and token, and test the connection. Save changes. Restart the agent from Settings after changing either hotkey or its mode.

The default PTT binding is `ctrl+alt+space`, held to record. The correction binding is `ctrl+alt+shift+v`. Select the corrected sentence in your editor before using it. The client compares the selection with the last five dictations and submits substitutions only.

`TRANSCRIPTD_AGENT` overrides the sidecar executable path. Otherwise a packaged app uses `resources/bin/transcriptd-agent`, with `.exe` on Windows. Development uses `../agent/target/debug/transcriptd-agent` relative to the client directory. `ELECTRON_RENDERER_URL` is set by electron-vite in development; it is ignored in packaged builds.

## Checks

```sh
bun run typecheck
bun run build
bun run test
bun run smoke --token-file ../server/data/token
```

The smoke script uses the running local server and agent without opening Electron. It performs read-only server requests and starts the agent with isolated HOME/XDG directories, so it does not replace your agent settings. Use `TRANSCRIPTD_TOKEN` instead of `--token-file` if preferred. `TRANSCRIPTD_SERVER` or `--server` overrides the smoke server URL; `--agent` overrides its executable. `--skip-api` and `--skip-agent` run one check only. No microphone recording or text injection runs during smoke tests.

## Packaging

Build the agent for the target OS with `cargo build --release`, then run `bun run dist:linux` or `bun run dist:win` from this directory. These scripts stage the matching release binary into `client/bin`. Set `TRANSCRIPTD_AGENT` to a target-specific binary when cross-building. Installers do not include speech models or the server.

Linux targets are AppImage, rpm, and deb. The udev rule ships in the application's resources; installing it still requires administrator approval. Windows uses NSIS. Package metadata currently points to `https://github.com/parth-thakre/transcriptd`; update it if the project is published elsewhere. `bun run icons` regenerates the checked-in PNG icons.

## Behavior and limits

- The tray keeps the app running when the main window closes. Quit from the tray to stop it.
- The overlay stays visible while recording and for four seconds after a final result. Hovering or editing pauses dismissal. It never requests focus when a result arrives.
- Raw ASR text and cleanup edits remain intact. Accepted cleanup changes are underlined; low-confidence words have dotted marks. The audit lists rejected edits too. Overlay corrections update the server's final text, not the raw evidence. They do not replace text already pasted into another application. Re-inject from history if needed.
- Re-injection hides the main window and waits 1.5 seconds. Focus the target application during that delay. The client does not try to restore another application's focus on Wayland.
- Meeting audio capture, live streaming, WAV backup, and upload belong to the agent. The client displays its segment events and can request processing again after upload. Summarization requires the server's LLM configuration.
- On Linux, the loopback name may need a PipeWire/PulseAudio monitor source rather than an output-device label. Use the default unless you know the monitor name.
- Windows start-at-login uses Electron's login settings. Linux writes an XDG autostart entry. Tray support, positioning, and always-on-top behavior depend on the desktop/compositor.
- Settings and the bearer token are stored in `settings.json` under Electron's user-data directory. This file is owner-readable/writable on Linux, but the token is not encrypted. The agent also persists its own configuration.

Typechecks, builds, and headless tests cannot verify tray integration, Wayland focus rules, Windows injection, audio hardware, or the appearance of the three windows. Test those on each target desktop before distributing an installer.
