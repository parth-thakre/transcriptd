# transcriptd server

FastAPI server: speech engines, vocabulary, faithfulness guard, diarization, storage.

```
uv sync                      # core deps
uv sync --extra hf           # + transformers engine (needs a torch build for your GPU)
scripts/build-transcribe-cpp.sh hip gfx1201   # ggml engine shared library
uv run transcriptd serve
```
