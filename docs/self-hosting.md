# Self-hosting

The server runs on Python 3.12 and listens on port 7411. Install the agent on the machine where you speak and type; it does not belong in the server container. A GPU is optional. Speech model weights are not included in the repository or images.

## Bare metal

```sh
cd server
uv sync
cp config.example.toml config.toml  # review the settings before exposing the port
uv run transcriptd token             # prints the generated bearer token
uv run transcriptd serve
```

`pip install transcribe-cpp` is already covered by `uv sync`. Its wheel includes CPU and Vulkan backends. The selected GGUF model downloads from Hugging Face into `server/models/` on first use. Silero VAD also downloads automatically. The first request may take time and needs network access. To select the multilingual profile, set `default_profile = "multilingual"` in `config.toml` or choose it in the agent.

For a faster native backend, run `scripts/build-transcribe-cpp.sh hip gfx1201`, `scripts/build-transcribe-cpp.sh cuda`, `scripts/build-transcribe-cpp.sh vulkan`, or `scripts/build-transcribe-cpp.sh cpu` from `server/`. Export `TRANSCRIBE_LIBRARY="$PWD/.native/transcribe.cpp/build/src/libtranscribe.so"` before starting the server. This native build needs CMake, a C++ compiler, git, and the matching GPU toolkit.

## Containers

From the repository root:

```sh
podman build -f server/Containerfile.cpu -t transcriptd-cpu server/
cd server
podman-compose up --build
podman-compose exec cpu transcriptd token
```

The compose example binds port 7411 to localhost and persists `server/data/` and `server/models/`. Copy `config.example.toml` to `config.toml` and uncomment the read-only mount if you need custom settings. To build a GPU image instead, use `podman build -f server/Containerfile.rocm --build-arg GPU_TARGETS=gfx1201 -t transcriptd-rocm server/` or `podman build -f server/Containerfile.cuda -t transcriptd-cuda server/`, then enable only that service in `podman-compose.yml`. HIP/CUDA images compile transcribe.cpp and set `TRANSCRIBE_LIBRARY`. The images do not include gated model weights or a vendor-specific PyTorch installation. Extend the image with the matching torch extra and optional engine dependencies before using `hf` profiles or `diarize` inside a container.

AMD GPUs work via Vulkan with the standard Python wheel and a working Vulkan driver. On an RDNA 4 card (e.g. RX 9070 XT), the optional HIP build targets `gfx1201` with ROCm 7.x; ROCm containers need `/dev/kfd` and `/dev/dri` access. NVIDIA cards also work via Vulkan without compiling; CUDA needs a CUDA toolkit for the native build and the NVIDIA container runtime for containers. Intel graphics can use Vulkan. CPU works everywhere but large models can be slow. Confirm the chosen backend in `/v1/health` after loading a model.

## Configuration

See [`server/config.example.toml`](../server/config.example.toml) for every key and its default. The server reads `config.toml` from its working directory unless `TRANSCRIPTD_CONFIG` points elsewhere. `host`, `port`, `token`, `data_dir`, and `models_dir` also accept `TRANSCRIPTD_*` environment overrides. The `profiles` table maps names to ASR engines. `english` uses Parakeet F16 and is the default; `multilingual` uses Whisper-turbo Q8_0. Install Transformers with `uv sync --extra hf --extra torch-rocm` on ROCm 7.1, `--extra torch-cuda` on CUDA 12.8, or `--extra torch-cpu` on CPU. Use the same torch choice with `--extra diarize` for pyannote. Pick only one torch backend. `uv sync --extra indic` adds Indic transliteration support. Add an `hf` profile after installing its dependencies. Diarization needs `HF_TOKEN` and acceptance of pyannote community-1 terms. For meeting summaries set `[llm] base_url`, `api_key` if needed, and `model` for an OpenAI-compatible endpoint. Leave them unset to keep summaries off.

With no explicit token the server generates one in `data/token` (mode 0600), readable with `uv run transcriptd token` from `server/` or `transcriptd token` inside the container. Keep it private.

The server listens on `127.0.0.1` and speaks plain HTTP. To reach it from other machines, pick one:

- **Tailscale (simplest).** Set `host` to the server's Tailscale address. WireGuard encrypts the traffic, so plain HTTP is fine on the tailnet. With `tailnet_auth = true`, your own devices (logged in to Tailscale as the same user as the server) need no token; requests admitted that way must not carry a foreign `Origin`.
- **A TLS reverse proxy** (Caddy, nginx, `tailscale serve`) in front of `127.0.0.1:7411`. Behind a proxy every request comes from the proxy's address, so tailnet login doesn't apply and clients use the token, unless you set `trust_proxy_headers = true` and the proxy sets `X-Forwarded-For` (and `X-Forwarded-Proto` for HTTPS; check that yours does). Only do that when nothing untrusted can reach port 7411 directly. Proxy WebSocket upgrades as well as HTTP. The agent accepts `https://` server URLs and connects with `wss://`, trusting the operating system's certificate store.
- **`host = "0.0.0.0"`** only on a network you trust.

Do not publish port 7411 directly to the internet. The container images set `TRANSCRIPTD_HOST=0.0.0.0` inside the container; the compose file publishes the port on localhost only.

Request limits are top-level keys: `max_upload_mb` (25) for request bodies, `max_meeting_upload_mb` (500) for meeting audio, `max_dictation_seconds` (600) per streamed take, and `max_queued_meetings` (8) meetings waiting for processing. See [SECURITY.md](../SECURITY.md) for what the server stores.

## Phone (Galaxy Z Flip cover screen)

The Android app floats a dictation bubble over any app you're typing in. Build it, install it, and pair it with the link `uv run transcriptd mobile` prints; [Android](android.md) has the steps. It reaches the server at its Tailscale address over plain HTTP; the app refuses `http://` to anything but loopback, private, link-local and Tailscale addresses, so use `https://` for any other server. The server must listen on that address (`host` in `config.toml`), not the default `127.0.0.1`.

The full-screen web app (dictate, restyle, history) is served by the server itself; nothing to build. It needs HTTPS for the microphone. On a Tailscale network:

```sh
tailscale serve --bg --https=7443 http://<server host>:7411   # tailnet only, not the internet
cd server && uv run transcriptd mobile                        # prints the pairing link
```

Open the link on the phone in Chrome, then menu > Add to home screen > Install. To run it on the Flip cover screen, allow the installed app under Settings > Advanced features > Labs > Apps allowed on cover screen (Good Lock's MultiStar lists apps that Samsung's own list leaves out). The app stores the token in the browser; Settings > Unpair removes it. Any other HTTPS reverse proxy works too if it forwards WebSocket upgrades; pass its address with `transcriptd mobile --url https://...`.

## Agent and maintenance

Build the agent with `cd agent && cargo build --release`. Linux needs `pw-record` or `parec` from `pipewire-utils` or `pulseaudio-utils`, and the [udev rule](../packaging/70-transcriptd.rules) to access `/dev/input/event*` and `/dev/uinput` without joining the `input` group:

```sh
sudo cp packaging/70-transcriptd.rules /etc/udev/rules.d/
sudo udevadm control --reload
sudo udevadm trigger
```

Run `transcriptd-agent run --no-stdin --hotkey ctrl+alt+space --mode hold`, passing `--server` and `--token` or saving them in the agent config. `toggle` works with compositor keybindings; `inject <text>`, `meeting start|stop`, `devices`, and `config` are also available. The agent config lives at `~/.config/transcriptd/agent.json` on Linux or `%APPDATA%\transcriptd\agent.json` on Windows. Windows capture uses WASAPI. The agent accepts `http://` and `https://` server URLs. For the Electron client, see [client/README.md](../client/README.md). Before installing the udev rule, read [SECURITY.md](../SECURITY.md): it lets any program running as you read your keyboards and inject input.

Back up `data/` including the SQLite database, token, and any saved audio. Back up `config.toml` separately if used. `models/` can be redownloaded but retaining it avoids the transfer; gated models may need renewed authorization. Stop the server before copying SQLite files. To upgrade, stop the service, back up data and config, pull new code, run `uv sync` or rebuild the container, and restart. Keep the persistent mounts in place and check the health endpoint and logs after startup. Unsigned Windows releases can trigger SmartScreen warnings.
