# Speech models and licenses

The project ships no speech model weights. The `ggml` registry fetches the selected GGUF variant from its `handy-computer/*-gguf` Hugging Face repository on first use. Check the license on the actual model repository and upstream checkpoint before downloading or deploying it; conversion does not change a weight license.

| Model | License for weights | Languages | Decoder | Profile | Runtime and device |
| --- | --- | --- | --- | --- | --- |
| Parakeet TDT 0.6B v2, F16 | CC BY 4.0 | English | TDT | `english` default | ggml: CPU, Vulkan, native HIP or CUDA |
| Qwen3-ASR 1.7B, Q8_0 | Apache 2.0 | 30 languages incl. English and Hindi (no Marathi) | Attention (LLM decoder) | Optional `english-qwen3`; ensemble pivot | ggml: CPU, Vulkan, native HIP or CUDA. No word times or confidence |
| Whisper large-v3, Q8_0 | MIT | Multilingual | Attention | Optional `english-whisper`; ensemble member | ggml: CPU, Vulkan, native HIP or CUDA |
| Whisper large-v3-turbo, Q8_0 | MIT | Multilingual, including English, Hindi, Marathi; mixed-script accuracy varies | Attention | `multilingual` built-in | ggml: CPU, Vulkan, native HIP or CUDA |
| Nemotron 3.5 ASR streaming 0.6B | Check its upstream model card before use | English live partials | TDT/RNNT family | Optional partials model | ggml: CPU, Vulkan, native HIP or CUDA |
| Bodhan AI indic-transcribe-flex | Indic Open Model License v1.0, gated | English, Hindi, Marathi; mixed mode candidate | Attention, Canary-based | Optional `hf` profile | PyTorch: CPU, ROCm or CUDA with a matching torch build |
| Trelis/whisper-hinglish-preview | Apache 2.0 | Hindi and English, no Marathi | Attention | Optional `hf` profile | PyTorch: CPU, ROCm or CUDA |
| AI4Bharat IndicConformer 600M | MIT | Monolingual Hindi or Marathi per call | CTC | Optional Indic engine candidate, not a built-in profile | PyTorch with a suitable integration; not a shipped ggml profile |

The standard `transcribe-cpp` wheel includes CPU and Vulkan ggml backends. Vulkan works with supported AMD, NVIDIA, and Intel drivers without compiling another library. A native HIP or CUDA build can be selected with `TRANSCRIBE_LIBRARY`. The optional `hf` extra installs Transformers and PyTorch; choose the `torch-rocm`, `torch-cuda`, or `torch-cpu` extra to get the matching torch wheels. `openai_compat` is an external transcription engine, so its model license and runtime depend on your provider.

No open model has yet shown convincing utterance-internal English/Hindi/Marathi code-switching with faithful scripts across all three languages. Flex is the candidate to test, not a verified default. Its gated license permits local self-hosting but requires accepting terms and a Hugging Face token; offering its weights as a hosted API to third parties requires written approval, and share-alike terms apply. Read the full license before use. Whisper-turbo is the available multilingual default, with weaker mixed-script reliability. Trelis is limited to English/Hindi. The guard checks cleanup edits against raw ASR words; it cannot detect words the model itself missed.

## Ensemble profile

`engine = "ensemble"` runs several local profiles on each take and votes word by word (ROVER,
`server/transcriptd/asr/ensemble.py`; example in `config.example.toml`). With Qwen3-ASR 1.7B as
the pivot plus Parakeet TDT v2 and Whisper large-v3 it made fewer errors than any of the three
alone on our phone dictation tests and on public Indian-English phone audio. A region holding a
number or non-Latin script is taken whole from one model, so the vote never builds a number
nobody said or votes a Hindi aside away. A model that writes nothing abstains. Each word's
confidence is the share of models that agreed on it, so disputed words look unsure to the vocab
matcher. The members' own texts are kept on each result (`members`) for tracing. The cost is
running all three: about three times the VRAM of one model and the sum of their run times, and
takes over 30 s lose their silent tail (the VAD step that keeps Whisper from inventing text) for
every member.

Silero VAD downloads automatically for segmentation. Optional pyannote community-1 diarization requires accepting its Hugging Face terms and setting `HF_TOKEN`; its license is separate from the ASR weights.
