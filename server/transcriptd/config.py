"""Server configuration: config.toml + environment (TRANSCRIPTD_*)."""

from __future__ import annotations

import os
import secrets
import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, model_validator

EngineKind = Literal["ggml", "hf", "openai_compat", "chat_audio", "ensemble"]
ENSEMBLE_MEMBER_ENGINES = ("ggml", "hf")  # local engines only


class ProfileConfig(BaseModel):
    """One ASR configuration the client can pick by name."""

    engine: EngineKind = "ggml"
    # ggml: path or HF repo id (handy-computer/...-gguf); hf: repo id; openai_compat: model name;
    # ensemble: unused
    model: str = ""
    language: str | None = None  # None = auto where supported
    languages: list[str] = Field(default_factory=lambda: ["en"])  # what the profile is meant for
    backend: str = "auto"  # ggml: auto|cpu|vulkan|rocm|cuda
    quant: str = "F16"  # ggml: file variant to pick when `model` is an HF repo id
    whisper_prompt_bias: bool = False
    pnc: str = "default"
    itn: str = "default"
    partials_model: str | None = None  # ggml streaming model for live partials (optional)
    hf_mixedcode: bool = False  # hf: Whisper fine-tune with a <|mixedcode|> token (Hinglish)
    hf_dtype: str = "auto"  # hf: auto|float16|bfloat16|float32
    base_url: str | None = None  # openai_compat / chat_audio
    api_key: str | None = None  # openai_compat / chat_audio
    prompt: str | None = None  # chat_audio: transcription instruction
    # ensemble: names of other (local) profiles whose engines vote word by word (asr/ensemble.py).
    # The first is the pivot; the order breaks ties. The ensemble's `language` goes to every member.
    members: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_engine_fields(self) -> ProfileConfig:
        if self.engine == "ensemble":
            if len(self.members) < 2:
                raise ValueError("an ensemble profile needs at least two members")
            if len(set(self.members)) != len(self.members):
                raise ValueError(f"ensemble members repeat: {self.members}")
        elif not self.model:
            raise ValueError(f"engine {self.engine!r} needs a model")
        elif self.members:
            raise ValueError("members is only for engine = \"ensemble\"")
        return self


class StyleConfig(BaseModel):
    """A post-cleanup style: deterministic layout on top of the ASR model's own punctuation.
    The guard still checks the result, so a style can only change punctuation, case and layout."""

    label: str
    layout: Literal["none", "note", "email"] = "none"  # note: lists become bullets; email: greeting/sign-off lines
    terminal_punctuation: bool | None = None  # overrides cleanup.ensure_terminal_punctuation
    lists: bool | None = None  # may the LLM make lists; None: yes, except in cleanup.chat_apps
    # Ramble: the LLM rewrites the take into what you meant (new wording allowed), checked for
    # invented or flipped facts by ramble.check; on a failed check the usual guarded cleanup.
    rewrite: bool = False


def _default_styles() -> dict[str, StyleConfig]:
    return {
        "auto": StyleConfig(label="Auto"),
        "message": StyleConfig(label="Message", terminal_punctuation=True, lists=False),
        "note": StyleConfig(label="Note", layout="note", terminal_punctuation=True),
        "email": StyleConfig(label="Email", layout="email", terminal_punctuation=True),
        "ramble": StyleConfig(label="Ramble", terminal_punctuation=True, rewrite=True),
    }


class CleanupConfig(BaseModel):
    remove_fillers: bool = True
    collapse_stutters: bool = True
    capitalize_first: bool = True
    ensure_terminal_punctuation: bool = False
    extra_protected: list[str] = Field(default_factory=list)
    # Optional LLM formatting pass (uses [llm]; `llm_model` overrides the model). The guard
    # still reverts any word the model adds or changes, and any drop that isn't a verified cut.
    llm: bool = False
    llm_model: str | None = None
    llm_timeout_s: float = 8.0
    llm_instructions: str = ""  # your own rules, e.g. "Use sentence case. Numbers as digits."
    llm_long_words: int = 40  # takes this long go to [llm_long] when it's configured
    # How long a long take waits for [llm_long]; [llm] runs alongside it and stands in after that.
    llm_long_timeout_s: float = 10.0
    # Apps (substrings of the phone's package or the desktop window's app) where text is a chat
    # message: the LLM makes no lists there unless the style says otherwise.
    chat_apps: list[str] = Field(default_factory=lambda: [
        "instagram", "whatsapp", "telegram", "signal", "securesms", "messenger", "facebook.orca", "discord",
        "snapchat", "messaging", "twitter",
    ])
    # Ramble cleanup: the LLM may also delete false starts, repeats, self-corrections and verbal
    # tics, each checked by the guard (never a negation, time word, condition or number).
    cuts: bool = True
    # Named styles a client can pick per dictation ([cleanup.styles.<name>] replaces the built-ins).
    styles: dict[str, StyleConfig] = Field(default_factory=_default_styles)


class VocabConfig(BaseModel):
    phonetic_threshold: float = 0.86  # loose tier: replace when this close AND the model was unsure
    strong_threshold: float = 0.88  # strong tier: replace when this close even if confident (non-common words)
    max_conf_for_replacement: float = 0.85  # "unsure" means the lowest word confidence in the span is at or below this
    prompt_terms_max: int = 60


class LLMConfig(BaseModel):
    base_url: str | None = None  # e.g. http://localhost:8080/v1
    api_key: str | None = None
    api_key_file: str | None = None  # read at each call instead of api_key, e.g. ~/.config/transcriptd/llm.key
    model: str | None = None
    reasoning_effort: str | None = None  # sent as-is ("low", "none") to models that think first


class Settings(BaseModel):
    # Loopback by default: plain HTTP with a bearer token belongs on a trusted network, a
    # tailnet or behind TLS. Set host = "0.0.0.0" or the machine's Tailscale IP to opt in.
    host: str = "127.0.0.1"
    port: int = 7411
    token: str = ""
    # Let your own Tailscale devices in without the token (tailnet.py). Off by default: only
    # turn it on when the server is reachable over Tailscale alone.
    tailnet_auth: bool = False
    tailnet_users: list[str] = []  # logins allowed that way; empty = the same user as this machine
    # Host names (besides IP literals, localhost and *.ts.net) a request let in by tailnet
    # identity alone may be addressed to. Anything else is refused: a DNS-rebinding page.
    allowed_hosts: list[str] = []
    # Take the client address and scheme from X-Forwarded-For/-Proto when the connection comes
    # from a proxy in uvicorn's FORWARDED_ALLOW_IPS (default 127.0.0.1). Off by default: with
    # tailnet_auth the caller's identity would then come from that header.
    trust_proxy_headers: bool = False
    data_dir: Path = Path("data")
    models_dir: Path = Path("models")
    default_profile: str = "english"
    profiles: dict[str, ProfileConfig] = Field(
        default_factory=lambda: {
            "english": ProfileConfig(
                model="handy-computer/parakeet-tdt-0.6b-v2-gguf", languages=["en"], language="en",
                partials_model="handy-computer/nemotron-3.5-asr-streaming-0.6b-gguf",
            ),
            "multilingual": ProfileConfig(
                model="handy-computer/whisper-large-v3-turbo-gguf", quant="Q8_0",
                languages=["en", "hi", "mr"], language=None,
            ),
        }
    )
    cleanup: CleanupConfig = CleanupConfig()
    vocab: VocabConfig = VocabConfig()
    llm: LLMConfig = LLMConfig()
    # A bigger model for long rambles (cleanup.llm_long_words and up), where a small one drops
    # facts; [llm] if it fails or is slow.
    llm_long: LLMConfig = LLMConfig()
    keep_dictation_audio: bool = False
    # Limits on what one client can make the server hold (413 / 429 past them).
    max_upload_mb: float = 25.0  # request bodies: /v1/transcribe uploads, JSON
    max_meeting_upload_mb: float = 500.0  # POST /v1/meetings/{id}/audio
    max_queued_meetings: int = 8  # meeting final passes waiting or running at once
    max_dictation_seconds: int = 600  # audio per take: WS /v1/dictate and /v1/transcribe uploads

    @model_validator(mode="after")
    def _check_ensembles(self) -> Settings:
        for name, p in self.profiles.items():
            if p.engine != "ensemble":
                continue
            for m in p.members:
                mp = self.profiles.get(m)
                if mp is None:
                    raise ValueError(f"profile {name!r}: unknown member profile {m!r}")
                if mp.engine not in ENSEMBLE_MEMBER_ENGINES:
                    raise ValueError(f"profile {name!r}: member {m!r} is {mp.engine!r}; "
                                     f"members must be {' or '.join(ENSEMBLE_MEMBER_ENGINES)} profiles")
        return self

    @classmethod
    def load(cls, path: str | Path | None = None) -> Settings:
        path = Path(path or os.environ.get("TRANSCRIPTD_CONFIG", "config.toml"))
        raw: dict = {}
        if path.exists():
            raw = tomllib.loads(path.read_text())
        s = cls.model_validate(raw)
        s.host = os.environ.get("TRANSCRIPTD_HOST", s.host)
        s.port = int(os.environ.get("TRANSCRIPTD_PORT", s.port))
        s.token = os.environ.get("TRANSCRIPTD_TOKEN", s.token)
        if d := os.environ.get("TRANSCRIPTD_DATA_DIR"):
            s.data_dir = Path(d)
        if m := os.environ.get("TRANSCRIPTD_MODELS_DIR"):
            s.models_dir = Path(m)
        private_dir(s.data_dir)
        s.models_dir.mkdir(parents=True, exist_ok=True)
        if not s.token:
            s.token = _ensure_token(s.data_dir)
        return s


def private_dir(path: Path) -> Path:
    """`path` as a directory only its owner can enter (0700): the data dir holds dictations,
    meetings and the token. An existing one open to group or others is tightened."""
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.stat().st_mode & 0o077:
        path.chmod(0o700)
    return path


def private_opener(path: str, flags: int) -> int:
    """`open(..., opener=private_opener)`: a new file is created owner-only (0600) whatever the umask."""
    return os.open(path, flags, 0o600)


def _ensure_token(data_dir: Path) -> str:
    """data/token, created owner-only (0600) from the start; an existing file is tightened."""
    f = data_dir / "token"
    if f.exists():
        if f.stat().st_mode & 0o077:
            f.chmod(0o600)
        return f.read_text().strip()
    tok = secrets.token_urlsafe(32)
    try:
        fd = os.open(f, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:  # another process got there first
        return f.read_text().strip()
    with os.fdopen(fd, "w") as fh:
        fh.write(tok)
    return tok
