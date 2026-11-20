"""`transcriptd` command line: serve, transcribe a file, benchmark a fixture set."""

from __future__ import annotations

import argparse
import copy
import json
import logging
import re
import sys
import time
from pathlib import Path

_SECRET_QUERY = re.compile(r"([?&](?:token|ticket)=)[^&\s\"']*", re.IGNORECASE)


def redact_secrets(text: str) -> str:
    """`?token=...` / `&ticket=...` query values replaced with `***`."""
    return _SECRET_QUERY.sub(r"\1***", text)


class RedactSecrets(logging.Filter):
    """Keeps tokens and WebSocket tickets in request URLs out of Uvicorn's access and
    WebSocket ("uvicorn.error") log lines."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact_secrets(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(redact_secrets(a) if isinstance(a, str) else a for a in record.args)
        elif isinstance(record.args, dict):
            record.args = {k: redact_secrets(v) if isinstance(v, str) else v for k, v in record.args.items()}
        return True


def uvicorn_log_config() -> dict:
    """Uvicorn's default logging, with RedactSecrets on its handlers."""
    from uvicorn.config import LOGGING_CONFIG

    cfg = copy.deepcopy(LOGGING_CONFIG)
    cfg.setdefault("filters", {})["redact_secrets"] = {"()": RedactSecrets}
    for handler in cfg["handlers"].values():
        handler.setdefault("filters", []).append("redact_secrets")
    return cfg


def uvicorn_options(settings, host: str | None = None, port: int | None = None) -> dict:
    """Keyword arguments for uvicorn.run. Proxy headers are off unless `trust_proxy_headers`:
    Uvicorn's default trusts X-Forwarded-For from 127.0.0.1, so any local process (or anything
    a local proxy forwards) could name its own client address, and with `tailnet_auth` pass as
    one of your Tailscale devices."""
    return {"host": host or settings.host, "port": port or settings.port, "log_level": "info",
            "log_config": uvicorn_log_config(), "proxy_headers": settings.trust_proxy_headers}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="transcriptd")
    ap.add_argument("--config", default=None, help="config.toml (default: ./config.toml or $TRANSCRIPTD_CONFIG)")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="run the HTTP/WebSocket server")
    s.add_argument("--host")
    s.add_argument("--port", type=int)

    t = sub.add_parser("transcribe", help="transcribe audio files through the full pipeline")
    t.add_argument("files", nargs="+")
    t.add_argument("--profile")
    t.add_argument("--language")
    t.add_argument("--json", action="store_true")
    t.add_argument("--raw", action="store_true", help="print verbatim text instead of guarded-cleaned")

    b = sub.add_parser("bench", help="score profiles on a fixture dir of <name>.wav + <name>.txt")
    b.add_argument("fixtures")
    b.add_argument("--profiles", nargs="+")

    sub.add_parser("token", help="print the API token")

    mo = sub.add_parser("mobile", help="print the link that pairs a phone with this server")
    mo.add_argument("--url", help="HTTPS base URL the phone reaches the server at (default: found via `tailscale serve`)")

    te = sub.add_parser("telemetry", help="what clients reported about recent takes, with the audio stats")
    te.add_argument("--limit", type=int, default=40)
    te.add_argument("--json", action="store_true")
    te.add_argument("--export", metavar="DIR", help="write takes read out in the playground as <id>.wav + <id>.txt for `bench`")

    m = sub.add_parser("models", help="manage model files")
    msub = m.add_subparsers(dest="models_cmd", required=True)
    mp = msub.add_parser("pull", help="download the models for one or more profiles (default: all ggml profiles) plus the VAD")
    mp.add_argument("profiles", nargs="*")
    msub.add_parser("list", help="show which profile models are present locally")

    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not args.verbose:
        logging.getLogger("transcriptd.asr.ggml.native").setLevel(logging.WARNING)

    from .config import Settings

    settings = Settings.load(args.config)

    if args.cmd == "token":
        print(settings.token)
        return 0
    if args.cmd == "models":
        return _models(settings, args)
    if args.cmd == "mobile":
        return _mobile(settings, args.url)
    if args.cmd == "telemetry":
        return _telemetry(settings, args)
    if args.cmd == "serve":
        import uvicorn

        from .app import create_app

        uvicorn.run(create_app(settings), **uvicorn_options(settings, args.host, args.port))
        return 0

    from .asr.registry import EngineRegistry
    from .audio import load_audio
    from .db import Database
    from .pipeline import Pipeline

    db = Database(settings.data_dir / "transcriptd.db")
    pipeline = Pipeline(settings, EngineRegistry(settings), db)
    pipeline.reload_vocab()

    if args.cmd == "transcribe":
        for f in args.files:
            pcm, _ = load_audio(f)
            t0 = time.perf_counter()
            r = pipeline.process(pcm, profile=args.profile, language=args.language)
            wall = (time.perf_counter() - t0) * 1000
            if args.json:
                print(json.dumps(r.to_dict(), ensure_ascii=False))
            else:
                print(f"# {f}  [{r.profile}/{r.model} lang={r.language} {r.duration_ms} ms audio, {wall:.0f} ms wall]")
                print(r.raw_text if args.raw else r.text)
                for e in r.edits:
                    mark = "ok " if e.accepted else "REV"
                    print(f"   {mark} {e.kind:7} {e.raw!r} -> {e.cleaned!r} ({e.reason})", file=sys.stderr)
                if r.low_confidence:
                    print("   low confidence:", ", ".join(f"{w['text']}({w['conf']})" for w in r.low_confidence), file=sys.stderr)
        return 0

    if args.cmd == "bench":
        from .bench import run_bench

        return run_bench(pipeline, Path(args.fixtures), args.profiles or [settings.default_profile])
    return 1


def _telemetry(settings, args) -> int:
    import shutil

    from .api.routes import reports
    from .bench import wer
    from .db import Database
    from .text import tokenize

    rows = reports(Database(settings.data_dir / "transcriptd.db"), args.limit)
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=1))
        return 0
    if args.export:
        out = Path(args.export)
        out.mkdir(parents=True, exist_ok=True)
        n = 0
        for r in rows:
            d, exp = r["dictation"], r["report"].get("expected")
            if not (exp and d and d["audio_path"] and Path(d["audio_path"]).exists()):
                continue
            mic = r["report"].get("mic", {}).get("source_name", "mic")
            stem = out / f"{r['dictation_id']}-{mic}"
            shutil.copyfile(d["audio_path"], stem.with_suffix(".wav"))
            stem.with_suffix(".txt").write_text(exp + "\n")
            n += 1
        print(f"{n} takes in {out}  (score them: transcriptd bench {out} --profiles ...)")
        return 0

    def words(text: str) -> list[str]:
        return [t.key for t in tokenize(text or "") if t.key]

    from datetime import UTC, datetime

    def when(r: dict) -> str:  # the phone's time of the take, else arrival; local time
        at = r["report"].get("at")
        t = datetime.fromtimestamp(at / 1000, UTC) if isinstance(at, int) else datetime.fromisoformat(r["created_at"]).replace(tzinfo=UTC)
        return t.astimezone().strftime("%m-%d %H:%M")

    print(f"{'take':>5} {'when':11} {'mic':6} {'scr':5} {'outcome':20} {'ms':>5} {'speech':>6} {'snr':>4} {'rumb':>4} {'wer':>4}  heard")
    def num(v) -> str:
        return "" if v is None else f"{v:.0f}"

    for r in sorted(rows, key=when):
        rep, d = r["report"], r["dictation"] or {}
        a = d.get("audio") or {}
        exp = rep.get("expected")
        score = f"{wer(words(exp), words(d.get('raw_text', ''))):.0%}" if exp and d else ""
        print(f"{r['dictation_id'] or '-':>5} {when(r):11} "
              f"{rep.get('mic', {}).get('source_name', '?')[:6]:6} {str(rep.get('screen', ''))[:5]:5} "
              f"{str(rep.get('outcome', ''))[:20]:20} {num(rep.get('release_to_text_ms')):>5} "
              f"{num(a.get('speech_db')):>6} {num(a.get('snr_db')):>4} {num(a.get('rumble_pct')):>4} {score:>4}  "
              f"{d.get('raw_text') or rep.get('error') or rep.get('target_app') or ''}")
        if exp:
            print(f"{'':>65}  said: {exp}")
    return 0


MOBILE_PORT = 7443


def _mobile(settings, url: str | None) -> int:
    """Pairing links for the phone. The Android app (android/, the floating bubble) talks to the
    server directly; over Tailscale plain HTTP is fine, the tunnel is encrypted. The web app
    (/m/) runs in a browser, which only allows the microphone on HTTPS, so it needs `tailscale
    serve` (or another TLS proxy) in front."""
    import shutil
    import subprocess
    from urllib.parse import quote

    loopback = settings.host in ("127.0.0.1", "localhost", "::1")
    target_host = "127.0.0.1" if settings.host in ("0.0.0.0", "::") else settings.host
    target = f"http://{target_host}:{settings.port}"
    status, serve = {}, {}
    if shutil.which("tailscale"):
        try:
            status = json.loads(subprocess.run(["tailscale", "status", "--json"], capture_output=True, text=True,
                                               check=False, timeout=5).stdout or "{}")
            serve = json.loads(subprocess.run(["tailscale", "serve", "status", "--json"], capture_output=True,
                                              check=False, text=True, timeout=5).stdout or "{}")
        except (OSError, ValueError, subprocess.TimeoutExpired):
            pass
    ts_ip = next((ip for ip in status.get("Self", {}).get("TailscaleIPs", []) if "." in ip), None)
    app_server = url or (f"http://{ts_ip if target_host == '127.0.0.1' and ts_ip else target_host}:{settings.port}")
    app_link = f"transcriptd://pair?u={quote(app_server, safe='')}&t={settings.token}"

    print("Android app (the floating bubble). Copy this on the phone, then tap Paste pairing link:\n")
    print(f"  {app_link}\n")
    if loopback and not url:
        print(f"  (the server listens on {settings.host} only, which the phone can't reach: set host to this\n"
              "   machine's Tailscale IP, or \"0.0.0.0\" on a trusted network, in config.toml; or pass --url)\n")
    elif target_host == "127.0.0.1" and not url and not ts_ip:
        print(f"  (the server listens on {settings.host}; pass --url with an address the phone can reach)\n")

    web = url if url and url.startswith("https://") else None
    for hostport, w in (serve.get("Web") or {}).items():
        proxies = {h.get("Proxy", "").rstrip("/") for h in (w.get("Handlers") or {}).values()}
        if not web and (target in proxies or f"http://localhost:{settings.port}" in proxies):
            web = "https://" + hostport.removesuffix(":443")
    if web:
        link = f"{web.rstrip('/')}/m/#t={settings.token}"
        print("Web app (full screen, history), in Chrome on the phone:\n")
        print(f"  {link}\n")
    else:
        print("Web app (optional): needs HTTPS. On the tailnet only:")
        print(f"  tailscale serve --bg --https={MOBILE_PORT} {target}\n")
    print("Both links carry the token; don't share them.")
    if shutil.which("qrencode"):
        subprocess.run(["qrencode", "-t", "ansiutf8", app_link], check=False)
    return 0


def _models(settings, args) -> int:
    from .asr.registry import resolve_ggml_model
    from .vad import ensure_model

    names = getattr(args, "profiles", None) or [n for n, p in settings.profiles.items() if p.engine == "ggml"]
    if args.models_cmd == "pull":
        vad = ensure_model(settings.models_dir)
        print(f"vad      {vad}")
        # An ensemble pulls its members' models.
        names = list(dict.fromkeys(m for n in names for m in (settings.profiles[n].members or [n])))
        for n in names:
            p = settings.profiles[n]
            if p.engine != "ggml":
                print(f"{n:8} ({p.engine}) downloads on first use via transformers; skipped")
                continue
            path = resolve_ggml_model(p.model, p.quant, settings.models_dir)
            print(f"{n:8} {path}")
            if p.partials_model:
                print(f"{n:8} partials {resolve_ggml_model(p.partials_model, p.quant, settings.models_dir)}")
        return 0
    for n, p in settings.profiles.items():
        if p.engine == "ensemble":
            print(f"{n:12} {p.engine:6} {' + '.join(p.members)}")
            continue
        if p.engine != "ggml":
            print(f"{n:12} {p.engine:6} {p.model}")
            continue
        slug = p.model.split("/")[-1].removesuffix("-gguf")
        local = settings.models_dir / slug
        have = sorted(local.glob(f"*{p.quant}*.gguf")) if local.exists() else []
        print(f"{n:12} {p.engine:6} {p.model} [{p.quant}] {'present' if have else 'missing'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
