#!/usr/bin/env python3
"""voice-transcribe: English speech to text for voice notes and audio files,
with whisper.cpp, on the GPU when it is free for it and on the CPU otherwise.
Writes <name>.txt next to each audio file and prints the text.

    voice-transcribe [--model NAME|PATH] [--force] [--quiet] <file|dir>...

English only: whisper decodes with the language forced to en (it never
detects one, and never defaults to another, which would translate English
speech). Silero VAD runs first, so a note without speech gives no text
instead of a hallucinated subtitle line. A phrase repeated three or more
times in a row is retried once without text context and the result with
fewer repeats is kept. Text that is mostly not Latin script is refused as
not English.

One transcription at a time on the whole host, whoever calls, in arrival
order: a FIFO queue in one directory every caller on the host shares (every
city's bridge and this command). A waiting note keeps its place until its
turn, however long; each run has a hard time limit scaled to the audio
length, so a hung whisper cannot hold the queue. The process fences itself
before it decodes anything: only the CPUs it is given (sched_setaffinity),
the agents' priority (nice 10, best-effort I/O level 7), and at most 4
whisper threads. Idle priority is not used: under a busy host it starves.

The GPU is used only with VOICE_TRANSCRIBE_GPU=1 (the bridge's gpu = true),
and only when the card is free for it, decided per note once its turn in the
queue starts. ComfyUI shares the card, so its local API is read first
(GET /queue and GET /system_stats, loopback only): anything rendering or
queued, or less free video memory than the margin, and the note runs on the
CPU. Only with VOICE_TRANSCRIBE_COMFYUI_FREE=1 (the bridge's comfyui_free =
true), an idle ComfyUI short of the margin, its /queue read empty again
right before, is asked once to unload its models (POST /free
{"unload_models": true}, the one request ever sent that is not a read) and
its free memory read again every 0.5 s for at most 10 s;
nothing is reloaded or restored afterwards. With ComfyUI not listening,
nvidia-smi gives the free memory instead and nothing is sent. /queue is read
again right before whisper starts on the GPU. Any error, timeout or
unexpected answer means the CPU. A GPU run that fails is retried once on the
CPU in the same turn. One "device" line per note says which ran and why:
gpu, gpu: comfy-freed, or cpu: comfy-busy | vram-low | api-error | gpu-failed
| gpu-off.

Settings come from the environment (the Telegram bridge passes its city's
settings this way):
  VOICE_TRANSCRIBE_WHISPER_CLI  whisper-cli binary (~/work/whisper.cpp/build/bin/whisper-cli)
  VOICE_TRANSCRIBE_MODEL_DIR    ggml models directory (~/work/whisper.cpp/models)
  VOICE_TRANSCRIBE_MODEL        model name or path (large-v3-turbo-q5_0)
  VOICE_TRANSCRIBE_VAD_MODEL    VAD model name or path (silero-v6.2.0)
  VOICE_TRANSCRIBE_THREADS      CPU threads, at most 4 (4)
  VOICE_TRANSCRIBE_CPUS         CPU list such as 2-7,10-15 (unset: every CPU the caller has)
  VOICE_TRANSCRIBE_TIMEOUT      run limit in seconds, before the per-minute allowance (120)
  VOICE_TRANSCRIBE_TIMEOUT_PER_MINUTE  seconds added per minute of audio (120)
  VOICE_TRANSCRIBE_GPU          1 = GPU when free, anything else = CPU only (unset)
  VOICE_TRANSCRIBE_COMFYUI_URL  ComfyUI on a loopback address (http://127.0.0.1:8188)
  VOICE_TRANSCRIBE_GPU_MIN_FREE_MB  free video memory needed for the GPU, MiB (2048)
  VOICE_TRANSCRIBE_COMFYUI_FREE  1 = ask an idle ComfyUI to unload its models for the GPU (unset)
The queue is always ~/.local/state/voice-transcribe, never a setting, so every
caller running as this user waits in the same one.
"""
import argparse
import fcntl
import http.client
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time

AUDIO_RE = re.compile(r'\.(ogg|oga|opus|mp3|m4a|wav|flac|webm|mp4)$', re.IGNORECASE)
LANGUAGE = 'en'
LOOP_MIN_REPEATS = 3
LOOP_MAX_PHRASE_WORDS = 16
# Share of letters that must be Latin script for a transcript to count as
# English; forcing en on other speech gives either other scripts or Latin
# gibberish, and only the first can be told apart here.
ENGLISH_MIN_LATIN = 0.7
MAX_THREADS = 4
NICE = 10
IONICE = ['-c', '2', '-n', '7']
# The queue: one file per waiting or running transcription, named by its
# arrival number, each held under flock by its owner for as long as it lives.
TICKET_RE = re.compile(r'([0-9]{12})-([0-9]{1,10})\.ticket')
QUEUE_SUBDIR = '.local/state/voice-transcribe'
QUEUE_LOCK = 'queue.lock'
QUEUE_POLL = 0.25
# The line the adapter prints when its turn starts: the bridge's own run
# limit starts there, not while the note waits in the queue.
RUNNING_LINE = '{"state": "running"}'
CPU_LIST_RE = re.compile(r'[0-9]{1,4}(?:-[0-9]{1,4})?(?:,[0-9]{1,4}(?:-[0-9]{1,4})?)*')
# ComfyUI's local API: a literal loopback address only (no name to resolve),
# plain HTTP, no path. Every read has a 2 s limit and a size cap.
COMFYUI_URL = 'http://127.0.0.1:8188'
COMFYUI_URL_RE = re.compile(r'http://(\[[0-9A-Fa-f:.]{2,45}\]|[0-9.]{7,15})(?::([0-9]{1,5}))?/?')
COMFYUI_TIMEOUT = 2
COMFYUI_MAX_BYTES = 1024 * 1024
GPU_MIN_FREE_MB = 2048
# POST /free with only unload_models: ComfyUI unloads its models from the
# card and keeps its cached node outputs (free_memory would drop those too),
# so its next run only loads the model again. The memory is then read every
# COMFYUI_FREE_POLL seconds for at most COMFYUI_FREE_WAIT.
COMFYUI_FREE_REQUEST = {'unload_models': True}
COMFYUI_FREE_WAIT = 10
COMFYUI_FREE_POLL = 0.5
MIB = 1024 * 1024
NVIDIA_SMI = ['nvidia-smi', '--query-gpu=memory.used,memory.total', '--format=csv,noheader,nounits']
NVIDIA_SMI_TIMEOUT = 5
# A GPU run gets a quarter of the CPU run limit (at least 30 s) before it
# counts as failed and the CPU takes over; the bridge allows the same extra.
GPU_LIMIT_SHARE = 4
GPU_MIN_LIMIT = 30


class TranscribeError(Exception):
    """A failure with a fixed code the bridge maps to one relay phrase."""

    def __init__(self, code, detail=''):
        super().__init__(detail or code)
        self.code = code


class Options:
    def __init__(self, environ=None, model=None, gpu=None):
        env = os.environ if environ is None else environ
        home = Path.home()
        self.whisper_cli = Path(env.get('VOICE_TRANSCRIBE_WHISPER_CLI')
                                or home / 'work/whisper.cpp/build/bin/whisper-cli').expanduser()
        self.model_dir = Path(env.get('VOICE_TRANSCRIBE_MODEL_DIR') or home / 'work/whisper.cpp/models').expanduser()
        self.model = model or env.get('VOICE_TRANSCRIBE_MODEL') or 'large-v3-turbo-q5_0'
        self.vad_model = env.get('VOICE_TRANSCRIBE_VAD_MODEL') or 'silero-v6.2.0'
        self.threads = min(_positive(env.get('VOICE_TRANSCRIBE_THREADS'), MAX_THREADS), MAX_THREADS)
        self.cpus = env.get('VOICE_TRANSCRIBE_CPUS') or None
        self.queue_dir = home / QUEUE_SUBDIR
        self.timeout = _positive(env.get('VOICE_TRANSCRIBE_TIMEOUT'), 120)
        self.timeout_per_minute = _positive(env.get('VOICE_TRANSCRIBE_TIMEOUT_PER_MINUTE'), 120)
        self.gpu = env.get('VOICE_TRANSCRIBE_GPU') == '1' if gpu is None else bool(gpu)
        # None when the URL is not a literal loopback one: the note then runs
        # on the CPU (api-error) and nothing is contacted.
        self.comfyui = comfyui_address(env.get('VOICE_TRANSCRIBE_COMFYUI_URL') or COMFYUI_URL)
        self.gpu_min_free_mb = _positive(env.get('VOICE_TRANSCRIBE_GPU_MIN_FREE_MB'), GPU_MIN_FREE_MB)
        self.comfyui_free = env.get('VOICE_TRANSCRIBE_COMFYUI_FREE') == '1'

    def run_limit(self, audio_seconds):
        """Seconds one run may take: the base plus the allowance per minute."""
        return self.timeout + math.ceil(self.timeout_per_minute * audio_seconds / 60)

    def gpu_run_limit(self, audio_seconds):
        """Seconds a GPU run may take before the CPU takes over."""
        return max(GPU_MIN_LIMIT, math.ceil(self.run_limit(audio_seconds) / GPU_LIMIT_SHARE))

    def model_file(self, name):
        if '/' in name or name.endswith('.bin'):
            return Path(name).expanduser()
        return self.model_dir / f'ggml-{name}.bin'


def _positive(value, default):
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return number if number > 0 else default


def comfyui_address(url):
    """(host, port) of a literal loopback http URL with no path, else None."""
    match = COMFYUI_URL_RE.fullmatch(url) if isinstance(url, str) else None
    if match is None:
        return None
    host, port = match.group(1).strip('[]'), int(match.group(2) or 80)
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        return None
    return (host, port) if loopback and 0 < port < 65536 else None


def parse_cpus(text):
    """{2, 3, ...} from a list such as '2-7,10-15'; ValueError when malformed."""
    if not CPU_LIST_RE.fullmatch(text):
        raise ValueError(f'bad CPU list {text!r}')
    cpus = set()
    for part in text.split(','):
        low, _, high = part.partition('-')
        low, high = int(low), int(high or low)
        if high < low:
            raise ValueError(f'bad CPU range {part!r}')
        cpus.update(range(low, high + 1))
    return cpus


def fence(options, run=subprocess.run):
    """Confine this process, and so ffmpeg and whisper-cli, to its CPUs at
    nice 10 and best-effort I/O level 7. A fence that cannot be set is an
    error: the transcription must not run unfenced. A caller already nicer
    than 10 keeps its niceness."""
    try:
        if options.cpus:
            os.sched_setaffinity(0, parse_cpus(options.cpus))
    except (OSError, ValueError) as error:
        raise TranscribeError('fence_failed', f'CPU affinity {options.cpus}: {error}') from None
    try:
        if os.getpriority(os.PRIO_PROCESS, 0) < NICE:
            os.setpriority(os.PRIO_PROCESS, 0, NICE)
    except OSError as error:
        raise TranscribeError('fence_failed', f'nice {NICE}: {error}') from None
    try:
        done = run(['ionice', *IONICE, '-p', str(os.getpid())], stdin=subprocess.DEVNULL,
                   capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        raise TranscribeError('fence_failed', 'ionice is not available') from None
    if done.returncode != 0:
        raise TranscribeError('fence_failed', 'ionice refused the best-effort class')


def whisper_argv(options, wav, no_context=False, gpu=False):
    """The whisper-cli call: plain text, English forced, Silero VAD; on the
    CPU (-ng) unless gpu, which is the same call without -ng."""
    argv = [str(options.whisper_cli), '-ng', '-t', str(options.threads), '-nt', '-np',
            '-l', LANGUAGE, '-m', str(options.model_file(options.model)), '-f', str(wav),
            '--vad', '-vm', str(options.model_file(options.vad_model))]
    if gpu:
        argv.remove('-ng')
    if no_context:
        argv += ['-mc', '0']
    return argv


class ComfyDown(Exception):
    """Nothing listens on ComfyUI's address (connection refused)."""


class ComfyError(Exception):
    """ComfyUI's answer is missing, late, refused or not the expected shape."""


def _comfyui_request(address, method, path, body, headers, connection):
    """The body of one 200 answer from ComfyUI's local API. http.client, not
    urllib: no proxy from the environment and no redirect is ever followed."""
    host, port = address
    conn = connection(host, port, timeout=COMFYUI_TIMEOUT)
    try:
        conn.request(method, path, body=body, headers=headers)
        response = conn.getresponse()
        answer = response.read(COMFYUI_MAX_BYTES + 1)
    except ConnectionRefusedError:
        raise ComfyDown() from None
    except (OSError, http.client.HTTPException):
        raise ComfyError() from None
    finally:
        conn.close()
    if response.status != 200 or len(answer) > COMFYUI_MAX_BYTES:
        raise ComfyError()
    return answer


def comfyui_get(address, path, connection=http.client.HTTPConnection):
    """The JSON of one GET to ComfyUI's local API."""
    answer = _comfyui_request(address, 'GET', path, None, {'Accept': 'application/json'}, connection)
    try:
        return json.loads(answer)
    except ValueError:
        raise ComfyError() from None


def comfyui_post(address, path, payload, connection=http.client.HTTPConnection):
    """One POST of a JSON payload to ComfyUI's local API. Any 200 answer is
    accepted whatever its body; anything else raises like comfyui_get."""
    _comfyui_request(address, 'POST', path, json.dumps(payload).encode(),
                     {'Content-Type': 'application/json'}, connection)


def _comfy_busy(answer):
    """True when GET /queue shows anything running or waiting."""
    running = answer.get('queue_running') if isinstance(answer, dict) else None
    pending = answer.get('queue_pending') if isinstance(answer, dict) else None
    if not isinstance(running, list) or not isinstance(pending, list):
        raise ComfyError()
    return bool(running or pending)


def _whole(value):
    return type(value) is int and value >= 0


def _comfy_free_bytes(answer):
    """Video memory another process can take, from GET /system_stats: its one
    CUDA device's vram_free less torch_vram_free, the part ComfyUI counts as
    free but still holds in its own allocator."""
    devices = answer.get('devices') if isinstance(answer, dict) else None
    if not isinstance(devices, list) or not all(isinstance(d, dict) for d in devices):
        raise ComfyError()
    cuda = [d for d in devices if d.get('type') == 'cuda']
    if len(cuda) != 1:
        raise ComfyError()
    free, held = cuda[0].get('vram_free'), cuda[0].get('torch_vram_free')
    if not _whole(free) or not _whole(held) or held > free:
        raise ComfyError()
    return free - held


def nvidia_free_bytes(run=subprocess.run):
    """Free video memory of the host's one GPU from nvidia-smi."""
    try:
        done = run(NVIDIA_SMI, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=NVIDIA_SMI_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        raise ComfyError() from None
    lines = (done.stdout or '').strip().splitlines() if done.returncode == 0 else []
    match = re.fullmatch(r'([0-9]{1,9}), ([0-9]{1,9})', lines[0].strip()) if len(lines) == 1 else None
    if match is None or int(match.group(1)) > int(match.group(2)):
        raise ComfyError()
    return (int(match.group(2)) - int(match.group(1))) * MIB


def choose_device(options, run=subprocess.run, get=comfyui_get, post=comfyui_post, clock=time.monotonic,
                  sleep=time.sleep, deadline=None):
    """('gpu', 'gpu') when the card is free for whisper, ('gpu',
    'comfy-freed') when it became free once an idle ComfyUI unloaded its
    models (options.comfyui_free only), else ('cpu', reason). deadline (a
    clock() value) ends the wait for the memory early: it never extends the
    note's run limit."""
    if not options.gpu:
        return 'cpu', 'gpu-off'
    if options.comfyui is None:
        return 'cpu', 'api-error'
    try:
        if _comfy_busy(get(options.comfyui, '/queue')):
            return 'cpu', 'comfy-busy'
        free = _comfy_free_bytes(get(options.comfyui, '/system_stats'))
    except ComfyDown:
        # Not listening: nvidia-smi decides, and there is nobody to ask.
        try:
            free = nvidia_free_bytes(run)
        except ComfyError:
            return 'cpu', 'api-error'
        return ('gpu', 'gpu') if free >= options.gpu_min_free_mb * MIB else ('cpu', 'vram-low')
    except ComfyError:
        return 'cpu', 'api-error'
    if free >= options.gpu_min_free_mb * MIB:
        return 'gpu', 'gpu'
    if not options.comfyui_free:
        return 'cpu', 'vram-low'
    return _free_comfyui(options, get, post, clock, sleep, deadline)


def _free_comfyui(options, get, post, clock, sleep, deadline):
    """Ask an idle ComfyUI to unload its models, then wait for the memory: at
    most one POST, whatever happens next. /queue is read again right before
    the POST so that nothing but that one round trip separates an empty queue
    from the request: ComfyUI has no conditional /free. Its prompt worker
    takes the flag at once when its queue is empty, and otherwise only after
    the item it takes next has run (server.py "/free", main.py
    prompt_worker), so a job queued in that gap still runs with its models
    loaded. The freed memory shows in /system_stats."""
    try:
        if _comfy_busy(get(options.comfyui, '/queue')):
            return 'cpu', 'comfy-busy'
        post(options.comfyui, '/free', COMFYUI_FREE_REQUEST)
        end = clock() + COMFYUI_FREE_WAIT
        if deadline is not None:
            end = min(end, deadline)
        while True:
            if _comfy_free_bytes(get(options.comfyui, '/system_stats')) >= options.gpu_min_free_mb * MIB:
                return 'gpu', 'comfy-freed'
            left = end - clock()
            if left <= 0:
                return 'cpu', 'vram-low'
            sleep(min(COMFYUI_FREE_POLL, left))
    except (ComfyDown, ComfyError):
        return 'cpu', 'api-error'


def gpu_still_free(options, get=comfyui_get):
    """None when a GPU run may start now (ComfyUI idle, or not listening),
    else the CPU reason: the check right before whisper starts."""
    try:
        return 'comfy-busy' if _comfy_busy(get(options.comfyui, '/queue')) else None
    except ComfyDown:
        return None
    except ComfyError:
        return 'api-error'


class Device:
    """The device of one note: starts as choose_device() decided and only
    ever moves from the GPU to the CPU. label is its one log line."""

    def __init__(self, name, reason):
        self.name, self.reason = name, reason

    def to_cpu(self, reason):
        self.name, self.reason = 'cpu', reason

    @property
    def label(self):
        if self.name == 'gpu':
            return 'gpu' if self.reason == 'gpu' else f'gpu: {self.reason}'
        return f'cpu: {self.reason}'


def clean(text):
    return ' '.join(text.split())


def longest_repeat(text):
    """The most times one phrase of 2..16 words repeats back to back."""
    words = re.findall(r"[\w']+", text.lower())
    best = 1
    for size in range(2, min(LOOP_MAX_PHRASE_WORDS, len(words) // 2) + 1):
        for start in range(0, len(words) - size * 2 + 1):
            phrase, count, at = words[start:start + size], 1, start + size
            while words[at:at + size] == phrase:
                count, at = count + 1, at + size
            best = max(best, count)
    return best


def is_english(text):
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return True
    latin = sum(1 for c in letters if c < 'ɐ')
    return latin / len(letters) >= ENGLISH_MIN_LATIN


def _run_whisper(argv, run, deadline, env=None):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TranscribeError('timeout', 'the run limit was reached')
    try:
        result = run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=remaining, env=env)
    except subprocess.TimeoutExpired:
        raise TranscribeError('timeout', 'whisper-cli was killed at the run limit') from None
    except (OSError, subprocess.SubprocessError):
        raise TranscribeError('whisper_failed') from None
    if result.returncode != 0:
        lines = (result.stderr or '').strip().splitlines()
        raise TranscribeError('whisper_failed', lines[-1][:200] if lines else '')
    return clean(result.stdout)


class Queue:
    """The host-wide FIFO of transcriptions, in one directory every caller
    shares. Taking a ticket and scanning the queue happen under a short flock
    on queue.lock, so arrival numbers are strictly ordered. Each owner holds
    an flock on its own ticket while it waits and runs: a ticket nobody holds
    belongs to a process that died, and the next scan removes it. The head
    ticket runs; everyone else waits with no time limit."""

    def __init__(self, path, sleep=time.sleep, poll=QUEUE_POLL):
        self.path, self.sleep, self.poll = Path(path), sleep, poll
        self.dir_fd = self.lock_fd = self.ticket_fd = None
        self.ticket = None

    def __enter__(self):
        try:
            os.makedirs(self.path, mode=0o700, exist_ok=True)
            self.dir_fd = os.open(self.path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
            st = os.fstat(self.dir_fd)
            if st.st_uid != os.getuid() or st.st_mode & 0o077:
                raise TranscribeError('queue_failed', f'queue directory must be owner-only: {self.path}')
            self.lock_fd = os.open(QUEUE_LOCK, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600,
                                   dir_fd=self.dir_fd)
            with self._locked():
                number = max((n for n, _ in self._tickets()), default=0) + 1
                self.ticket = f'{number:012d}-{os.getpid()}.ticket'
                self.ticket_fd = os.open(self.ticket, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                                         0o600, dir_fd=self.dir_fd)
                fcntl.flock(self.ticket_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            while not self._my_turn():
                self.sleep(self.poll)
        except OSError as error:
            self.__exit__(None, None, None)
            raise TranscribeError('queue_failed', f'queue {self.path}: {error.strerror}') from None
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, *exc):
        try:
            if self.ticket_fd is not None and self.lock_fd is not None:
                with self._locked():
                    try:
                        os.unlink(self.ticket, dir_fd=self.dir_fd)
                    except FileNotFoundError:
                        pass
        finally:
            for fd in (self.ticket_fd, self.lock_fd, self.dir_fd):
                if fd is not None:
                    os.close(fd)
            self.dir_fd = self.lock_fd = self.ticket_fd = None
        return False

    class _Held:
        def __init__(self, fd):
            self.fd = fd

        def __enter__(self):
            fcntl.flock(self.fd, fcntl.LOCK_EX)

        def __exit__(self, *exc):
            fcntl.flock(self.fd, fcntl.LOCK_UN)
            return False

    def _locked(self):
        return self._Held(self.lock_fd)

    def _tickets(self):
        found = []
        for name in os.listdir(self.dir_fd):
            match = TICKET_RE.fullmatch(name)
            if match:
                found.append((int(match.group(1)), name))
        return sorted(found)

    def _my_turn(self):
        """True when every ticket ahead of ours is gone; removes dead ones."""
        with self._locked():
            for _, name in self._tickets():
                if name == self.ticket:
                    return True
                try:
                    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=self.dir_fd)
                except FileNotFoundError:
                    continue
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return False  # alive: waiting or running ahead of us
                finally:
                    os.close(fd)
                os.unlink(name, dir_fd=self.dir_fd)  # its owner died
        raise TranscribeError('queue_failed', 'our ticket left the queue')


def _whisper(options, wav, run, device, deadline, gpu_limit, get, no_context=False):
    """One whisper pass on the note's device. On the GPU: /queue again right
    before it starts, at most gpu_limit seconds, and any failure moves the
    note to the CPU for this pass and the rest of it. The CPU pass never sees
    a GPU (CUDA_VISIBLE_DEVICES empty)."""
    if device.name == 'gpu':
        late = gpu_still_free(options, get)
        if late is not None:
            device.to_cpu(late)
    if device.name == 'gpu':
        try:
            return _run_whisper(whisper_argv(options, wav, no_context, gpu=True), run,
                                min(deadline, time.monotonic() + gpu_limit))
        except TranscribeError:
            device.to_cpu('gpu-failed')
    return _run_whisper(whisper_argv(options, wav, no_context), run, deadline,
                        env=dict(os.environ, CUDA_VISIBLE_DEVICES=''))


def _wav_seconds(path):
    """Seconds of a 16 kHz mono 16-bit WAV, from its size."""
    return max(os.path.getsize(path) - 44, 0) / 32000


def transcribe(audio, options, run=subprocess.run, on_turn=None, on_device=None, get=comfyui_get):
    """English text of one audio file ('' when it holds no speech), or
    TranscribeError. Waits for its turn in the host-wide queue, calls
    on_turn() when it starts, picks the device, and runs within
    options.run_limit() (plus the GPU allowance with options.gpu).
    on_device(label) gets the note's final device line once whisper ran."""
    if not (options.whisper_cli.is_file() and os.access(options.whisper_cli, os.X_OK)):
        raise TranscribeError('whisper_missing', f'whisper-cli not found: {options.whisper_cli}')
    for name in (options.model, options.vad_model):
        if not options.model_file(name).is_file():
            raise TranscribeError('model_missing', f'model not found: {options.model_file(name)}')
    fence(options)
    with Queue(options.queue_dir), tempfile.TemporaryDirectory(prefix='voice-transcribe.') as tmp:
        if on_turn is not None:
            on_turn()
        wav = Path(tmp) / 'in.wav'
        try:
            converted = run(['ffmpeg', '-nostdin', '-loglevel', 'error', '-y', '-i', str(audio),
                             '-ar', '16000', '-ac', '1', '-c:a', 'pcm_s16le', str(wav)],
                            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=options.timeout)
        except subprocess.TimeoutExpired:
            raise TranscribeError('timeout', 'ffmpeg was killed at the run limit') from None
        except (OSError, subprocess.SubprocessError):
            raise TranscribeError('decode_failed', 'ffmpeg could not run') from None
        if converted.returncode != 0:
            raise TranscribeError('decode_failed', 'ffmpeg could not decode it')
        seconds = _wav_seconds(wav)
        gpu_limit = options.gpu_run_limit(seconds)
        deadline = time.monotonic() + options.run_limit(seconds) + (gpu_limit if options.gpu else 0)
        device = Device(*choose_device(options, run, get, deadline=deadline))
        try:
            text = _whisper(options, wav, run, device, deadline, gpu_limit, get)
            if longest_repeat(text) >= LOOP_MIN_REPEATS:
                # A decoding loop: retry with no text context, keep the better one.
                try:
                    retry = _whisper(options, wav, run, device, deadline, gpu_limit, get, no_context=True)
                except TranscribeError as error:
                    if error.code != 'timeout':
                        raise
                    retry = text  # out of time: keep the first text
                if longest_repeat(retry) < longest_repeat(text):
                    text = retry
        finally:
            if on_device is not None:
                on_device(device.label)
    if text and not is_english(text):
        raise TranscribeError('not_english', 'the transcript is not English')
    return text


def _audio_files(paths, force):
    todo = []
    for given in paths:
        path = Path(given)
        if path.is_dir():
            for found in sorted(path.rglob('*')):
                if found.is_file() and AUDIO_RE.search(found.name) and (force or not found.with_suffix('.txt').exists()):
                    todo.append(found)
        elif path.is_file():
            todo.append(path)
        else:
            raise SystemExit(f'voice-transcribe: no such file or directory: {given}')
    return todo


def _write_text(path, text):
    part = path.with_name(path.name + '.part')
    part.write_text(text + '\n')
    os.replace(part, path)


def main(argv=None):
    parser = argparse.ArgumentParser(prog='voice-transcribe', description=__doc__.split('\n\n')[0],
                                     formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    parser.add_argument('--model', help='model name (ggml-NAME.bin in the models directory) or a path')
    parser.add_argument('--force', action='store_true', help='transcribe again even if the .txt exists')
    parser.add_argument('-q', '--quiet', action='store_true', help='print only the text')
    parser.add_argument('paths', nargs='+', metavar='file|dir')
    args = parser.parse_args(argv)
    options = Options(model=args.model)
    todo = _audio_files(args.paths, args.force)
    status = 0
    for audio in todo:
        started = time.monotonic()
        try:
            text = transcribe(audio, options,
                              on_device=lambda label, audio=audio: print(f'voice-transcribe: {audio}: device {label}',
                                                                         file=sys.stderr, flush=True))
        except TranscribeError as error:
            print(f'voice-transcribe: {audio}: {error}', file=sys.stderr)
            status = 1
            continue
        out = audio.with_suffix('.txt')
        _write_text(out, text)
        if args.quiet:
            print(text)
        else:
            seconds = time.monotonic() - started
            print(f'== {audio} (en, {options.model}, {seconds:.1f} s) -> {out}\n{text or "(no speech)"}')
    return status


if __name__ == '__main__':
    sys.exit(main())
