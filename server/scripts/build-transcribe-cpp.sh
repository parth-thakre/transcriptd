#!/usr/bin/env bash
# Build libtranscribe (transcribe.cpp) as a shared library for the server's ggml engine.
# Usage: build-transcribe-cpp.sh [hip|vulkan|cuda|cpu] [gfx-targets]
# Output: server/.native/transcribe.cpp/build/src/libtranscribe.so
set -euo pipefail
BACKEND="${1:-cpu}"
TARGETS="${2:-gfx1201}"
HERE="$(cd "$(dirname "$0")/.." && pwd)"
SRC="$HERE/.native/transcribe.cpp"
mkdir -p "$HERE/.native"
if [ ! -d "$SRC/.git" ]; then
  git clone --depth 1 https://github.com/handy-computer/transcribe.cpp "$SRC"
fi
FLAGS=(-DTRANSCRIBE_BUILD_SHARED=ON -DCMAKE_BUILD_TYPE=Release)
case "$BACKEND" in
  hip)    FLAGS+=(-DTRANSCRIBE_HIP=ON "-DAMDGPU_TARGETS=$TARGETS") ;;
  vulkan) FLAGS+=(-DTRANSCRIBE_VULKAN=ON) ;;
  cuda)   FLAGS+=(-DTRANSCRIBE_CUDA=ON) ;;
  cpu)    ;;
  *) echo "unknown backend $BACKEND" >&2; exit 2 ;;
esac
cmake -S "$SRC" -B "$SRC/build" "${FLAGS[@]}"
cmake --build "$SRC/build" --target transcribe -j"$(nproc)"
ls -l "$SRC"/build/src/libtranscribe.so* 2>/dev/null || find "$SRC/build" -name 'libtranscribe.so*' -print
