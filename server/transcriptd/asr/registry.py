"""Builds engines from profiles and caches them by name. Resolves HF repo ids to files."""

from __future__ import annotations

import logging
import re
import threading
from pathlib import Path

from ..config import ProfileConfig, Settings
from .base import Engine

log = logging.getLogger(__name__)


def resolve_ggml_model(model: str, quant: str, models_dir: Path) -> Path:
    """`model` is a local path to a .gguf, or an HF repo id like handy-computer/x-gguf."""
    p = Path(model)
    if p.suffix == ".gguf" and p.exists():
        return p
    if "/" not in model:
        raise FileNotFoundError(f"model {model!r} is neither a .gguf path nor an HF repo id")
    slug = model.split("/")[-1].removesuffix("-gguf")
    local = models_dir / slug
    hits = _quant_files(local, quant)
    if hits:
        return hits[0]
    from huggingface_hub import snapshot_download

    log.info("downloading %s (%s) to %s", model, quant, local)
    # The quant is a whole name part: "F16" must not pull in (or pick) "BF16".
    snapshot_download(model, allow_patterns=[f"*[-_.]{quant}.gguf", f"*[-_.]{quant}[-_.]*.gguf", "*.json"],
                      local_dir=local)
    hits = _quant_files(local, quant)
    if not hits:
        raise FileNotFoundError(f"no *{quant}*.gguf in {model}")
    return hits[0]


def _quant_files(local: Path, quant: str) -> list[Path]:
    """The .gguf files in `local` whose name has `quant` as a whole part, between - _ . or the ends."""
    if not local.exists():
        return []
    part = re.compile(rf"(?:^|[-_.]){re.escape(quant)}(?:$|[-_.])")
    return sorted(p for p in local.glob("*.gguf") if part.search(p.stem))


class EngineRegistry:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._engines: dict[str, Engine] = {}
        self._lock = threading.Lock()  # guards the two dicts; never held while a model loads
        self._building: dict[str, threading.Lock] = {}  # one lock per profile being built

    def profile(self, name: str | None) -> tuple[str, ProfileConfig]:
        name = name or self.settings.default_profile
        try:
            return name, self.settings.profiles[name]
        except KeyError:
            raise KeyError(f"unknown profile {name!r}; have {sorted(self.settings.profiles)}") from None

    def get(self, name: str | None = None) -> Engine:
        """The profile's engine, built on first use. Two threads asking for the same profile
        build it once; different profiles build in parallel, and an ensemble builds its members
        through here (so a member shared with its own profile loads once)."""
        name, prof = self.profile(name)
        with self._lock:
            if name in self._engines:
                return self._engines[name]
            building = self._building.setdefault(name, threading.Lock())
        with building:
            with self._lock:
                if name in self._engines:  # built while we waited
                    return self._engines[name]
            engine = self._build(prof)
            with self._lock:
                self._engines[name] = engine
                self._building.pop(name, None)
            return engine

    def _build(self, prof: ProfileConfig) -> Engine:
        if prof.engine == "ensemble":
            from .ensemble import EnsembleEngine

            # Config validation keeps members to local, non-ensemble profiles, so this recursion
            # is one level deep and takes other profiles' build locks only.
            return EnsembleEngine([(m, self.get(m)) for m in prof.members])
        if prof.engine == "ggml":
            from .ggml import GgmlEngine

            path = resolve_ggml_model(prof.model, prof.quant, self.settings.models_dir)
            return GgmlEngine(path, backend=prof.backend, whisper_prompt_bias=prof.whisper_prompt_bias,
                              pnc=prof.pnc, itn=prof.itn)
        if prof.engine == "hf":
            from .hf import HfEngine

            return HfEngine(prof.model, language=prof.language, mixedcode=prof.hf_mixedcode, dtype=prof.hf_dtype)
        if prof.engine == "openai_compat":
            from .openai_compat import OpenAICompatEngine

            return OpenAICompatEngine(prof.base_url or "", prof.api_key or "", prof.model)
        if prof.engine == "chat_audio":
            from .chat_audio import ChatAudioEngine

            return ChatAudioEngine(prof.base_url or "", prof.api_key or "", prof.model, prompt=prof.prompt)
        raise ValueError(prof.engine)

    def loaded(self) -> dict[str, dict]:
        with self._lock:
            engines = dict(self._engines)
        return {
            n: {"engine": e.name, "model": e.model_name, "device": getattr(e, "device", None)}
            for n, e in engines.items()
        }

    def close(self) -> None:
        with self._lock:
            engines = list(self._engines.values())
            self._engines.clear()
        for e in engines:
            e.close()
