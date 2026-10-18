#!/bin/sh
# Set up the transcriber's host side for one city's settings, idempotently:
#   transcriber/setup.sh --settings <file> [--dry-run] [--whisper-ref <ref>]
# Builds whisper.cpp for the settings' method, then downloads the chosen
# model and the VAD model and checks them against transcriber/models.sha256,
# so install.sh can say ok without guessing. The whisper.cpp build and the
# models are shared by every city on the host (never under a city). It
# installs nothing system-wide, uses no sudo and starts nothing; a missing
# prerequisite is reported, never installed. It prints one ok/FAIL/would
# line per step like install.sh, and never prints a settings value other
# than the paths it acts on; it never reads the token or secret files.
# WHISPER_REF below is the known-good whisper.cpp commit; --whisper-ref
# takes a tag or a commit instead. The model list comes from the file
# named by TELEGRAM_PACK_MODELS_SHA256 when that is set (for tests only),
# else from transcriber/models.sha256 beside this script's pack.
set -u
tmpfile=
_cleanup_tmp() { if [ -n "$tmpfile" ]; then rm -f "$tmpfile"; fi; }
trap _cleanup_tmp EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
# v1.8.3-120-g764482c3 (2026-02-09), the build the host transcribes with.
WHISPER_REF=764482c3
pack=$(CDPATH= cd -- "$(dirname "$0")/.." && pwd -P)
bridge="$pack/bridge/bridge.py"
sums_file=${TELEGRAM_PACK_MODELS_SHA256:-"$pack/transcriber/models.sha256"}
settings= dry_run=0 whisper_ref=$WHISPER_REF
while [ $# -gt 0 ]; do
  case $1 in
    --settings) [ $# -ge 2 ] || { echo "setup.sh: --settings needs a file (usage: transcriber/setup.sh --settings <file> [--dry-run] [--whisper-ref <ref>])" >&2; exit 64; }; settings=$2; shift 2 ;;
    --settings=*) settings=${1#*=}; shift ;;
    --dry-run) dry_run=1; shift ;;
    --whisper-ref) [ $# -ge 2 ] || { echo "setup.sh: --whisper-ref needs a ref (usage: transcriber/setup.sh --settings <file> [--dry-run] [--whisper-ref <ref>])" >&2; exit 64; }; whisper_ref=$2; shift 2 ;;
    --whisper-ref=*) whisper_ref=${1#*=}; shift ;;
    *) echo "setup.sh: unknown argument $1 (usage: transcriber/setup.sh --settings <file> [--dry-run] [--whisper-ref <ref>])" >&2; exit 64 ;;
  esac
done
[ -n "$settings" ] || { echo "setup.sh: --settings <file> is required (usage: transcriber/setup.sh --settings <file> [--dry-run] [--whisper-ref <ref>])" >&2; exit 64; }
[ -n "$whisper_ref" ] || { echo "setup.sh: --whisper-ref needs a ref (usage: transcriber/setup.sh --settings <file> [--dry-run] [--whisper-ref <ref>])" >&2; exit 64; }

# Read the transcribe paths and method the way install.sh does, through
# the bridge's own settings loader. It validates the file and reads no
# credential file's contents.
settings_out=$(python3 -c '
import runpy, shlex, sys
try:
    s = runpy.run_path(sys.argv[1])["load_settings"](sys.argv[2])
except Exception as exc:
    print("FAIL  " + str(exc), file=sys.stderr)
    sys.exit(1)
enabled = "1" if s.transcribe is not None else "0"
if s.transcribe is not None:
    method = s.transcribe.get("method") or ("gpu" if s.transcribe.get("gpu") else "cpu")
else:
    method = "cpu"
print("enabled=" + shlex.quote(enabled))
print("whisper_cli=" + shlex.quote(str(s.whisper_cli)))
print("models_dir=" + shlex.quote(str(s.models_dir)))
print("model=" + shlex.quote(str(s.model)))
print("vad_model=" + shlex.quote(str(s.vad_model)))
print("method=" + shlex.quote(str(method)))
' "$bridge" "$settings") || exit 1
eval "$settings_out"

if [ "$enabled" != 1 ]; then
  printf 'skip  transcription is off in %s\n' "$settings"
  exit 0
fi

# Prerequisites: report every missing tool, then fail before any change.
missing=0
if command -v git >/dev/null 2>&1; then printf 'ok    git\n'; else printf 'FAIL  git not found (install it with your package manager)\n'; missing=$((missing + 1)); fi
if command -v cmake >/dev/null 2>&1; then printf 'ok    cmake\n'; else printf 'FAIL  cmake not found (install it with your package manager)\n'; missing=$((missing + 1)); fi
cmake_generator=
if command -v make >/dev/null 2>&1; then printf 'ok    make\n'; elif command -v ninja >/dev/null 2>&1; then printf 'ok    ninja\n'; cmake_generator=Ninja; else printf 'FAIL  make not found (install it with your package manager)\n'; missing=$((missing + 1)); fi
if command -v c++ >/dev/null 2>&1; then printf 'ok    c++\n'; elif command -v g++ >/dev/null 2>&1; then printf 'ok    g++\n'; elif command -v clang++ >/dev/null 2>&1; then printf 'ok    clang++\n'; else printf 'FAIL  c++ not found (install it with your package manager)\n'; missing=$((missing + 1)); fi
if command -v ffmpeg >/dev/null 2>&1; then printf 'ok    ffmpeg\n'; else printf 'FAIL  ffmpeg not found (install it with your package manager)\n'; missing=$((missing + 1)); fi
if command -v curl >/dev/null 2>&1; then printf 'ok    curl\n'; else printf 'FAIL  curl not found (install it with your package manager)\n'; missing=$((missing + 1)); fi
if command -v sha256sum >/dev/null 2>&1; then printf 'ok    sha256sum\n'; else printf 'FAIL  sha256sum not found (install it with your package manager)\n'; missing=$((missing + 1)); fi
# nvcc is no prerequisite: without it a gpu/auto build is a CPU build.
if [ "$missing" -ne 0 ]; then exit 1; fi

# The checkout is the directory the whisper_cli layout names:
# <checkout>/build/bin/whisper-cli.
case $whisper_cli in
  */build/bin/*) checkout=${whisper_cli%/build/bin/*}; [ -n "$checkout" ] || checkout=/ ;;
  *) checkout=$(dirname "$(dirname "$whisper_cli")") ;;
esac

if [ -d "$checkout" ]; then
  describe=$(git -C "$checkout" describe --tags --always 2>/dev/null) || describe=unknown
  [ -n "$describe" ] || describe=unknown
  printf 'ok    whisper.cpp at %s (%s)\n' "$checkout" "$describe"
elif [ "$dry_run" = 1 ]; then
  printf 'would clone whisper.cpp to %s at %s\n' "$checkout" "$whisper_ref"
else
  mkdir -p "$(dirname "$checkout")"
  if ! git clone https://github.com/ggml-org/whisper.cpp "$checkout"; then
    printf 'FAIL  whisper.cpp clone to %s failed\n' "$checkout"
    exit 1
  fi
  if ! git -C "$checkout" checkout --detach "$whisper_ref"; then
    rm -rf "$checkout"
    printf 'FAIL  whisper.cpp checkout %s at %s failed\n' "$whisper_ref" "$checkout"
    exit 1
  fi
  describe=$(git -C "$checkout" describe --tags --always 2>/dev/null) || describe=$whisper_ref
  [ -n "$describe" ] || describe=$whisper_ref
  printf 'ok    whisper.cpp at %s (%s)\n' "$checkout" "$describe"
fi

# Build only when whisper_cli is missing. CUDA only for gpu/auto with
# nvcc on PATH; without nvcc the build is for the CPU.
want_cuda=0
if [ "$method" = gpu ] || [ "$method" = auto ]; then
  if command -v nvcc >/dev/null 2>&1; then want_cuda=1; fi
fi
if [ -f "$whisper_cli" ] && [ -x "$whisper_cli" ]; then
  printf 'ok    whisper-cli at %s\n' "$whisper_cli"
elif [ "$dry_run" = 1 ]; then
  if [ "$want_cuda" = 1 ]; then
    printf 'would build whisper-cli at %s with CUDA (-DGGML_CUDA=1)\n' "$whisper_cli"
  elif [ "$method" = gpu ] || [ "$method" = auto ]; then
    printf 'would build whisper-cli at %s for the CPU (no nvcc on PATH; the GPU is tried and falls back to the CPU)\n' "$whisper_cli"
  else
    printf 'would build whisper-cli at %s\n' "$whisper_cli"
  fi
else
  if [ "$want_cuda" = 1 ] && [ -n "$cmake_generator" ]; then
    if ! (cd "$checkout" && cmake -B build -DGGML_CUDA=1 -G "$cmake_generator"); then
      printf 'FAIL  whisper-cli configure in %s failed\n' "$checkout"
      exit 1
    fi
  elif [ "$want_cuda" = 1 ]; then
    if ! (cd "$checkout" && cmake -B build -DGGML_CUDA=1); then
      printf 'FAIL  whisper-cli configure in %s failed\n' "$checkout"
      exit 1
    fi
  elif [ -n "$cmake_generator" ]; then
    if ! (cd "$checkout" && cmake -B build -G "$cmake_generator"); then
      printf 'FAIL  whisper-cli configure in %s failed\n' "$checkout"
      exit 1
    fi
  else
    if ! (cd "$checkout" && cmake -B build); then
      printf 'FAIL  whisper-cli configure in %s failed\n' "$checkout"
      exit 1
    fi
  fi
  if ! (cd "$checkout" && cmake --build build -j --config Release --target whisper-cli); then
    printf 'FAIL  whisper-cli build in %s failed\n' "$checkout"
    exit 1
  fi
  if [ ! -f "$whisper_cli" ] || [ ! -x "$whisper_cli" ]; then
    printf 'FAIL  whisper-cli build did not produce %s\n' "$whisper_cli"
    exit 1
  fi
  if [ "$want_cuda" = 1 ]; then
    printf 'ok    whisper-cli built with CUDA at %s\n' "$whisper_cli"
  elif [ "$method" = gpu ] || [ "$method" = auto ]; then
    printf 'ok    whisper-cli built for the CPU (no nvcc on PATH; the GPU is tried and falls back to the CPU)\n'
  else
    printf 'ok    whisper-cli built at %s\n' "$whisper_cli"
  fi
fi

# Models: check the recorded SHA-256, download only what is missing,
# never overwrite a file that is already there.
recorded_hash() {
  awk -v target="$1" '!/^#/ && NF >= 2 { f = $2; sub(/^\*/, "", f); if (f == target) { print tolower($1); exit } }' "$sums_file"
}
file_hash() {
  sha256sum "$1" | cut -d' ' -f1
}
setup_model() {
  _name=$1
  _url=$2
  _file="ggml-$_name.bin"
  _dest="$models_dir/$_file"
  _expected=$(recorded_hash "$_file")
  if [ -z "$_expected" ]; then
    printf 'FAIL  model %s has no recorded SHA-256 in %s\n' "$_dest" "$sums_file"
    return 1
  fi
  if [ -f "$_dest" ]; then
    _actual=$(file_hash "$_dest")
    if [ "$_actual" = "$_expected" ]; then
      printf 'ok    model %s SHA-256\n' "$_dest"
      return 0
    fi
    printf 'FAIL  model %s SHA-256 mismatch\n' "$_dest"
    return 1
  fi
  if [ "$dry_run" = 1 ]; then
    printf 'would download %s from %s\n' "$_dest" "$_url"
    return 0
  fi
  if [ ! -d "$models_dir" ]; then
    mkdir -p "$models_dir" || { printf 'FAIL  models directory %s could not be created\n' "$models_dir"; return 1; }
  fi
  _tmp=$(mktemp "$models_dir/.ggml-XXXXXXXX") || { printf 'FAIL  model %s download failed\n' "$_dest"; return 1; }
  tmpfile=$_tmp
  if ! curl -fL --proto =https -o "$_tmp" "$_url"; then
    rm -f "$_tmp"
    tmpfile=
    printf 'FAIL  model %s download failed\n' "$_dest"
    return 1
  fi
  _actual=$(file_hash "$_tmp")
  if [ "$_actual" != "$_expected" ]; then
    rm -f "$_tmp"
    tmpfile=
    printf 'FAIL  model %s SHA-256 mismatch\n' "$_dest"
    return 1
  fi
  if ! mv "$_tmp" "$_dest"; then
    rm -f "$_tmp"
    tmpfile=
    printf 'FAIL  model %s could not be moved into place\n' "$_dest"
    return 1
  fi
  tmpfile=
  printf 'ok    model %s downloaded (SHA-256)\n' "$_dest"
  return 0
}
setup_model "$model" "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-$model.bin" || exit 1
setup_model "$vad_model" "https://huggingface.co/ggml-org/whisper-vad/resolve/main/ggml-$vad_model.bin" || exit 1

printf 'next: %s/install.sh --settings %s\n' "$pack" "$settings"
