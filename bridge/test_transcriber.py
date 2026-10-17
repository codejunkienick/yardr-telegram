"""The whisper.cpp transcriber and its bridge adapter, with a fake whisper-cli,
a fake ffmpeg, a fake nvidia-smi and a fake ComfyUI on loopback: English is
forced, VAD is on, loops are retried, non-English text is refused, the adapter
speaks the bridge's JSON-line contract, every caller on the host waits its turn
in one FIFO queue, and the GPU is used only when ComfyUI leaves it free."""
import http.server
import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

PACK = Path(__file__).resolve().parents[1]
TRANSCRIBER = PACK / 'transcriber'
spec = importlib.util.spec_from_file_location('voice_transcribe', TRANSCRIBER / 'voice_transcribe.py')
vt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vt)

# A fake whisper-cli: records its argv as one JSON line, then prints the next
# queued answer (one per call; the last repeats), or exits 1 on "FAIL".
FAKE_WHISPER = """#!{python}
import json, os, sys
log = os.environ['FAKE_WHISPER_LOG']
fence = dict(cpus=sorted(os.sched_getaffinity(0)), nice=os.getpriority(os.PRIO_PROCESS, 0),
             ionice=__import__('subprocess').run(['ionice', '-p', str(os.getpid())], capture_output=True, text=True).stdout.strip())
with open(os.environ['FAKE_WHISPER_LOG'] + '.fence', 'w') as stream:
    json.dump(fence, stream)
with open(log, 'a') as stream:
    stream.write(json.dumps(sys.argv[1:]) + '\\n')
with open(log + '.cuda', 'a') as stream:
    stream.write(json.dumps(os.environ.get('CUDA_VISIBLE_DEVICES')) + '\\n')
started = __import__('time').time()
__import__('time').sleep(float(os.environ.get('FAKE_WHISPER_SLEEP', '0')))
if os.environ.get('FAKE_WHISPER_TIMES'):
    with open(os.environ['FAKE_WHISPER_TIMES'], 'a') as stream:
        stream.write(json.dumps(dict(tag=os.environ['FAKE_WHISPER_TAG'], start=started,
                                     end=__import__('time').time())) + '\\n')
answers = json.loads(os.environ['FAKE_WHISPER_ANSWERS'])
calls = sum(1 for _ in open(log))
answer = answers[min(calls, len(answers)) - 1]
if answer == 'FAIL':
    print('whisper: model load failed', file=sys.stderr)
    sys.exit(1)
print(answer)
"""
# A fake ffmpeg: writes the output file (its last argument), or fails on "bad" input.
FAKE_FFMPEG = """#!/bin/sh
for last; do :; done
case "$*" in *bad.ogg*) echo 'decode error' >&2; exit 1 ;; esac
printf 'RIFF' >"$last"
"""
# A fake nvidia-smi: counts its calls, prints FAKE_NVIDIA_SMI and exits with
# FAKE_NVIDIA_SMI_EXIT.
FAKE_NVIDIA_SMI = """#!/bin/sh
echo "$*" >>"$FAKE_WHISPER_LOG.nvidia-smi"
printf '%s' "$FAKE_NVIDIA_SMI"
exit "${FAKE_NVIDIA_SMI_EXIT:-0}"
"""


class FakeWhisperCase(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        bin_dir = self.root / 'bin'
        bin_dir.mkdir()
        self.cli = bin_dir / 'whisper-cli'
        self.cli.write_text(FAKE_WHISPER.format(python=sys.executable))
        self.cli.chmod(0o755)
        (bin_dir / 'ffmpeg').write_text(FAKE_FFMPEG)
        (bin_dir / 'ffmpeg').chmod(0o755)
        (bin_dir / 'nvidia-smi').write_text(FAKE_NVIDIA_SMI)
        (bin_dir / 'nvidia-smi').chmod(0o755)
        self.models = self.root / 'models'
        self.models.mkdir()
        for name in ('large-v3-turbo-q5_0', 'silero-v6.2.0', 'small'):
            (self.models / f'ggml-{name}.bin').write_bytes(b'model')
        self.audio = self.root / '70-voice.ogg'
        self.audio.write_bytes(b'OggS')
        self.log = self.root / 'whisper-calls.jsonl'
        # The queue is always ~/.local/state/voice-transcribe: a fake home
        # keeps the tests off the real one.
        self.home = self.root / 'home'
        self.home.mkdir()
        self.queue = self.home / vt.QUEUE_SUBDIR
        # Nothing from the caller's environment picks the device or ComfyUI:
        # the real ComfyUI is never contacted (the default address is a
        # closed loopback port here; GPU tests start a fake one).
        inherited = {k: v for k, v in os.environ.items()
                     if not k.startswith('VOICE_TRANSCRIBE_') and k != 'CUDA_VISIBLE_DEVICES'}
        self.env = dict(inherited, PATH=f'{bin_dir}{os.pathsep}{os.environ.get("PATH", "")}', HOME=str(self.home),
                        VOICE_TRANSCRIBE_WHISPER_CLI=str(self.cli), VOICE_TRANSCRIBE_MODEL_DIR=str(self.models),
                        VOICE_TRANSCRIBE_THREADS='3', FAKE_WHISPER_LOG=str(self.log),
                        VOICE_TRANSCRIBE_COMFYUI_URL=f'http://127.0.0.1:{closed_port()}')

    def adapter(self, *answers, audio=None, model='large-v3-turbo-q5_0', device='cpu', extra_env=None):
        env = dict(self.env, FAKE_WHISPER_ANSWERS=json.dumps(list(answers)), **(extra_env or {}))
        return subprocess.run([sys.executable, str(TRANSCRIBER / 'adapter.py'), str(audio or self.audio), model, device],
                              env=env, capture_output=True, text=True, timeout=60)

    def whisper_calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def whisper_cuda(self):
        """CUDA_VISIBLE_DEVICES each whisper run saw (None: not set)."""
        path = Path(str(self.log) + '.cuda')
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def nvidia_smi_calls(self):
        path = Path(str(self.log) + '.nvidia-smi')
        return path.read_text().splitlines() if path.exists() else []

    def result(self, *answers, **kwargs):
        """The adapter's result without its device line, which goes to
        self.device (None when whisper never ran)."""
        done = self.adapter(*answers, **kwargs)
        self.assertEqual(done.returncode, 0, done.stderr)
        lines = done.stdout.splitlines()
        # At most one "running" line (the turn started), then exactly one result.
        self.assertIn(lines[:-1], ([], [vt.RUNNING_LINE]), done.stdout)
        result = json.loads(lines[-1])
        self.device = result.pop('device', None)
        return result


def closed_port():
    """A loopback port nothing listens on (connection refused)."""
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


class TranscriberTests(FakeWhisperCase):
    def test_english_is_forced_with_vad_on_the_cpu(self):
        self.assertEqual(self.result(' Ship it\n  today. '), {'text': 'Ship it today.'})
        self.assertEqual(self.device, 'cpu: gpu-off')
        self.assertEqual(self.whisper_cuda(), [''])  # the CPU run never sees a GPU
        self.assertEqual(self.nvidia_smi_calls(), [])
        [argv] = self.whisper_calls()
        self.assertEqual(argv[argv.index('-l') + 1], 'en')
        self.assertNotIn('auto', argv)
        self.assertNotIn('-dl', argv)
        self.assertEqual(argv.count('-l'), 1)
        self.assertIn('-ng', argv)
        self.assertEqual(argv[argv.index('-t') + 1], '3')
        self.assertEqual(argv[argv.index('-m') + 1], str(self.models / 'ggml-large-v3-turbo-q5_0.bin'))
        self.assertIn('--vad', argv)
        self.assertEqual(argv[argv.index('-vm') + 1], str(self.models / 'ggml-silero-v6.2.0.bin'))
        self.assertNotIn('-mc', argv)

    def test_whisper_runs_fenced_on_its_cpus_at_the_agents_priority(self):
        mine = sorted(os.sched_getaffinity(0))
        cpus = mine[-2:] if len(mine) > 1 else mine
        self.env['VOICE_TRANSCRIBE_CPUS'] = ','.join(str(c) for c in cpus)
        self.result('Fine.')
        fence = json.loads(Path(str(self.log) + '.fence').read_text())
        self.assertEqual(fence['cpus'], cpus)
        self.assertEqual(fence['nice'], max(10, os.getpriority(os.PRIO_PROCESS, 0)))
        self.assertEqual(fence['ionice'], 'best-effort: prio 7')

    def test_more_than_four_threads_is_capped(self):
        self.env['VOICE_TRANSCRIBE_THREADS'] = '12'
        self.result('Fine.')
        argv = self.whisper_calls()[0]
        self.assertEqual(argv[argv.index('-t') + 1], '4')

    def test_a_fence_that_cannot_be_set_stops_the_transcription(self):
        for cpus in ('99999', '3-1', 'all'):
            with self.subTest(cpus=cpus):
                self.env['VOICE_TRANSCRIBE_CPUS'] = cpus
                self.assertEqual(self.result('unused'), {'error': 'fence_failed'})
        self.assertEqual(self.whisper_calls(), [])

    def test_the_model_argument_picks_the_model_file(self):
        self.result('fine', model='small')
        self.assertEqual(self.whisper_calls()[0][self.whisper_calls()[0].index('-m') + 1],
                         str(self.models / 'ggml-small.bin'))

    def test_no_speech_after_vad_is_an_empty_text(self):
        self.assertEqual(self.result(''), {'text': ''})

    def test_a_loop_is_retried_once_without_context_and_the_better_text_kept(self):
        looped = 'what is it? what is it? what is it? what is it?'
        self.assertEqual(self.result(looped, 'what is it, he asked'), {'text': 'what is it, he asked'})
        first, retry = self.whisper_calls()
        self.assertNotIn('-mc', first)
        self.assertEqual(retry[retry.index('-mc') + 1], '0')
        self.assertEqual(retry[retry.index('-l') + 1], 'en')

    def test_a_retry_that_still_loops_as_much_keeps_the_first_text(self):
        looped = 'go on go on go on'
        self.assertEqual(self.result(looped, 'yes yes, go on go on go on'), {'text': looped})
        self.assertEqual(len(self.whisper_calls()), 2)

    def test_non_english_text_is_refused(self):
        self.assertEqual(self.result('Что такое? Мы обновим это до пятницы.'), {'error': 'not_english'})

    def test_known_failures_are_codes_never_child_text(self):
        self.assertEqual(self.result('FAIL'), {'error': 'whisper_failed'})
        self.assertEqual(self.device, 'cpu: gpu-off')
        bad = self.root / 'bad.ogg'
        bad.write_bytes(b'x')
        self.assertEqual(self.result('unused', audio=bad), {'error': 'decode_failed'})
        self.assertIsNone(self.device)  # whisper never ran: no device line
        (self.models / 'ggml-silero-v6.2.0.bin').unlink()
        self.assertEqual(self.result('unused'), {'error': 'model_missing'})
        self.cli.chmod(0o644)
        self.assertEqual(self.result('unused'), {'error': 'whisper_missing'})

    def test_usage_errors_exit_2(self):
        for device in ('cuda', 'auto2', ''):
            with self.subTest(device=device):
                done = self.adapter('x', device=device)
                self.assertEqual((done.returncode, done.stdout), (2, ''))

    def test_the_command_writes_the_text_next_to_the_audio(self):
        env = dict(self.env, FAKE_WHISPER_ANSWERS='["Hello there."]')
        done = subprocess.run([sys.executable, str(TRANSCRIBER / 'voice_transcribe.py'), '--quiet', str(self.root)],
                              env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual((done.returncode, done.stdout), (0, 'Hello there.\n'), done.stderr)
        self.assertEqual(done.stderr, f'voice-transcribe: {self.audio}: device cpu: gpu-off\n')
        self.assertEqual((self.root / '70-voice.txt').read_text(), 'Hello there.\n')
        again = subprocess.run([sys.executable, str(TRANSCRIBER / 'voice_transcribe.py'), '--quiet', str(self.root)],
                               env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(again.stdout, '')  # a directory skips audio that already has its .txt
        self.assertEqual(len(self.whisper_calls()), 1)

    def test_the_adapter_writes_nothing_beside_the_audio(self):
        done = self.adapter('Fine.')
        self.assertEqual(done.stdout.splitlines(), [vt.RUNNING_LINE, '{"text": "Fine.", "device": "cpu: gpu-off"}'])
        self.assertEqual(sorted(p.name for p in self.root.iterdir()),
                         ['70-voice.ogg', 'bin', 'home', 'models', 'whisper-calls.jsonl', 'whisper-calls.jsonl.cuda',
                          'whisper-calls.jsonl.fence'])
        self.assertEqual(os.listdir(self.queue), ['queue.lock'])  # its ticket is gone


class GpuCheckMethodTests(FakeWhisperCase):
    """method auto / gpu / cpu through the adapter: the check runs once with
    a fixed environment, its contract decides the device, method gpu runs
    no check at all, and a failed GPU run retries once on the CPU."""

    def setUp(self):
        super().setUp()
        self.check_dir = self.root / 'gpu-check'
        self.check_dir.mkdir()
        self.check = self.check_dir / 'check.py'
        self.check_log = self.root / 'check-calls.jsonl'

    def write_check(self, body):
        self.check.write_text(
            f'#!{sys.executable}\nimport json, os, sys, time\n'
            f'with open({str(self.check_log)!r}, "a") as stream:\n'
            '    stream.write(json.dumps(dict(env=dict(os.environ), cwd=os.getcwd(), argv=sys.argv[1:])) + "\\n")\n'
            + body)
        self.check.chmod(0o755)
        return str(self.check)

    def check_calls(self):
        if not self.check_log.exists():
            return []
        return [json.loads(line) for line in self.check_log.read_text().splitlines()]

    def checked(self, *answers, **kwargs):
        kwargs.setdefault('device', 'check')
        kwargs.setdefault('extra_env', {})['VOICE_TRANSCRIBE_GPU_CHECK'] = str(self.check)
        return self.result(*answers, **kwargs)

    def test_a_true_check_runs_whisper_on_the_gpu_with_a_fixed_environment(self):
        self.write_check('print(\'{"gpu": true}\')\n')
        self.assertEqual(self.checked('Ship it.'), {'text': 'Ship it.'})
        self.assertEqual(self.device, 'gpu')
        [argv] = self.whisper_calls()
        self.assertNotIn('-ng', argv)
        self.assertEqual(self.whisper_cuda(), [None])
        [call] = self.check_calls()
        self.assertEqual(call['argv'], [])
        self.assertEqual(call['cwd'], str(self.check_dir))
        self.assertEqual(set(call['env']), {'PATH', 'HOME', 'LANG', 'GPU_MIN_FREE_MB'})
        self.assertEqual(call['env']['PATH'], '/usr/local/bin:/usr/bin:/bin')
        self.assertEqual(call['env']['GPU_MIN_FREE_MB'], '2048')

    def test_a_true_check_with_a_reason_labels_the_gpu_with_it(self):
        self.write_check('print(\'{"gpu": true, "reason": "unknown"}\')\n')
        self.assertEqual(self.checked('Fine.'), {'text': 'Fine.'})
        self.assertEqual(self.device, 'gpu: unknown')
        self.assertNotIn('-ng', self.whisper_calls()[0])

    def test_a_false_check_runs_on_the_cpu(self):
        for body, label in (('print(\'{"gpu": false, "reason": "operator-busy"}\')\n', 'cpu: operator-busy'),
                            ('print(\'{"gpu": false}\')\n', 'cpu: busy')):
            with self.subTest(label=label):
                self.log.unlink(missing_ok=True)
                Path(str(self.log) + '.cuda').unlink(missing_ok=True)
                self.check_log.unlink(missing_ok=True)
                self.write_check(body)
                self.assertEqual(self.checked('Fine.'), {'text': 'Fine.'})
                self.assertEqual(self.device, label)
                self.assertIn('-ng', self.whisper_calls()[0])
                self.assertEqual(self.whisper_cuda(), [''])
                self.assertEqual(len(self.check_calls()), 1)

    def test_anything_outside_the_check_contract_means_check_error_on_the_cpu(self):
        for body in ('print("not json")\n',
                     'print(\'{"gpu": true, "reason": null}\')\n',
                     'print(\'{"gpu": false, "reason": null}\')\n',
                     'print(\'{"gpu": true, "reason": "gpu-failed"}\')\n',
                     'print(\'{"gpu": true, "reason": "Bad Reason"}\')\n',
                     'print(\'{"gpu": "yes"}\')\n',
                     'print(\'{"gpu": true, "extra": 1}\')\n',
                     'print("x" * 5000)\n',
                     'print(\'{"gpu": true}\')\nsys.exit(3)\n'):
            with self.subTest(body=body):
                self.log.unlink(missing_ok=True)
                self.write_check(body)
                self.assertEqual(self.checked('Fine.'), {'text': 'Fine.'})
                self.assertEqual(self.device, 'cpu: check-error')
                self.assertIn('-ng', self.whisper_calls()[0])

    def test_a_check_that_times_out_means_check_error(self):
        self.write_check('time.sleep(30)\n')
        options = vt.Options(environ=dict(self.env, VOICE_TRANSCRIBE_GPU_CHECK=str(self.check)), method='auto')
        started = time.monotonic()
        with _patched_timeout():
            self.assertEqual(vt.choose_device(options), ('cpu', 'check-error'))
        self.assertLess(time.monotonic() - started, 10)

    def test_method_auto_with_the_default_check_and_no_nvidia_smi_tries_the_gpu(self):
        # The default check (transcriber/gpu_check.py) run through
        # run_gpu_check's fixed environment, with GPU_CHECK_PATH narrowed to
        # a directory holding only a python3 link: no nvidia-smi is found, so
        # the check answers unknown and whisper runs on the GPU.
        check_bin = self.root / 'default-check-bin'
        check_bin.mkdir()
        (check_bin / 'python3').symlink_to(sys.executable)
        env = dict(self.env, FAKE_WHISPER_ANSWERS='["Ship it."]')
        labels = []
        with patch.dict(os.environ, env):
            os.environ.pop('CUDA_VISIBLE_DEVICES', None)
            options = vt.Options(environ=env, method='auto')
            self.assertEqual(options.gpu_check, str(TRANSCRIBER / 'gpu_check.py'))
            with patch.object(vt, 'GPU_CHECK_PATH', str(check_bin)), \
                    patch.object(vt, 'fence', lambda options: None):
                text = vt.transcribe(str(self.audio), options, on_device=labels.append)
        self.assertEqual(text, 'Ship it.')
        self.assertEqual(labels, ['gpu: unknown'])
        [argv] = self.whisper_calls()
        self.assertNotIn('-ng', argv)
        self.assertEqual(self.whisper_cuda(), [None])
        self.assertEqual(self.nvidia_smi_calls(), [])

    def test_method_gpu_runs_no_check_and_retries_a_failure_on_the_cpu(self):
        self.write_check('print(\'{"gpu": false}\')\n')
        env = {'VOICE_TRANSCRIBE_GPU_CHECK': str(self.check)}
        self.assertEqual(self.result('Ship it.', device='gpu', extra_env=env), {'text': 'Ship it.'})
        self.assertEqual(self.device, 'gpu')
        self.assertNotIn('-ng', self.whisper_calls()[0])
        self.assertEqual(self.check_calls(), [])
        self.log.unlink()
        self.assertEqual(self.result('FAIL', 'Still here.', device='gpu', extra_env=env),
                         {'text': 'Still here.'})
        self.assertEqual(self.device, 'cpu: gpu-failed')
        gpu_run, cpu_run = self.whisper_calls()
        self.assertNotIn('-ng', gpu_run)
        self.assertIn('-ng', cpu_run)
        self.assertEqual(self.check_calls(), [])

    def test_method_cpu_never_runs_a_check_or_the_gpu(self):
        self.write_check('print(\'{"gpu": true}\')\n')
        env = {'VOICE_TRANSCRIBE_GPU_CHECK': str(self.check)}
        self.assertEqual(self.result('Fine.', device='cpu', extra_env=env), {'text': 'Fine.'})
        self.assertEqual(self.device, 'cpu: gpu-off')
        self.assertIn('-ng', self.whisper_calls()[0])
        self.assertEqual(self.check_calls(), [])


class _patched_timeout:
    """Shorten the GPU check timeout for one in-process choose_device call."""

    def __enter__(self):
        self.old = vt.GPU_CHECK_TIMEOUT
        vt.GPU_CHECK_TIMEOUT = 0.2
        return self

    def __exit__(self, *exc):
        vt.GPU_CHECK_TIMEOUT = self.old
        return False


class GpuCheckScriptTests(unittest.TestCase):
    """transcriber/gpu_check.py against a fake nvidia-smi (or none)."""

    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.bin = self.root / 'bin'
        self.bin.mkdir()

    def run_check(self, smi=None, exit_code='0', path=None, min_free='2048'):
        if smi is not None:
            fake = self.bin / 'nvidia-smi'
            fake.write_text(FAKE_NVIDIA_SMI)
            fake.chmod(0o755)
        env = dict(PATH=str(self.bin) if path is None else path, HOME=str(self.root),
                   GPU_MIN_FREE_MB=min_free, FAKE_WHISPER_LOG=str(self.root / 'log'),
                   FAKE_NVIDIA_SMI=smi or '', FAKE_NVIDIA_SMI_EXIT=exit_code)
        done = subprocess.run([sys.executable, str(TRANSCRIBER / 'gpu_check.py')],
                              env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(done.stdout)

    def test_enough_free_memory_means_the_gpu(self):
        self.assertEqual(self.run_check('926, 16311\n'), {'gpu': True})
        self.assertEqual(self.run_check('926, 16311\n', min_free='20000'),
                         {'gpu': False, 'reason': 'vram-low'})

    def test_too_little_free_memory_means_vram_low(self):
        self.assertEqual(self.run_check('15000, 16311\n'), {'gpu': False, 'reason': 'vram-low'})

    def test_a_missing_nvidia_smi_means_try_the_gpu(self):
        empty = self.root / 'empty'
        empty.mkdir()
        self.assertEqual(self.run_check(path=str(empty)), {'gpu': True, 'reason': 'unknown'})

    def test_a_malformed_or_failing_nvidia_smi_means_try_the_gpu(self):
        for smi, code in (('N/A, 16311\n', '0'), ('', '0'), ('926, 16311\n926, 16311\n', '0'),
                          ('926, 16311\n', '9')):
            with self.subTest(smi=smi, code=code):
                self.assertEqual(self.run_check(smi, exit_code=code), {'gpu': True, 'reason': 'unknown'})

    def test_a_flooding_nvidia_smi_is_capped_and_its_group_is_killed(self):
        marker = self.root / 'descendant-ran'
        fake = self.bin / 'nvidia-smi'
        fake.write_text(
            f'#!{sys.executable}\nimport subprocess, sys, time\n'
            f'subprocess.Popen([sys.executable, "-c", "import time; time.sleep(0.6); '
            f'open({str(marker)!r}, \'w\').write(\'x\')"])\n'
            'while True:\n'
            '    sys.stdout.write("x" * 8192)\n'
            '    sys.stdout.flush()\n')
        fake.chmod(0o755)
        env = dict(PATH=str(self.bin), HOME=str(self.root), GPU_MIN_FREE_MB='2048')
        started = time.monotonic()
        done = subprocess.run([sys.executable, str(TRANSCRIBER / 'gpu_check.py')],
                              env=env, capture_output=True, text=True, timeout=30)
        self.assertLess(time.monotonic() - started, 4)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(json.loads(done.stdout), {'gpu': True, 'reason': 'unknown'})
        time.sleep(1)
        self.assertFalse(marker.exists())  # the descendant died with its group


class QueueTests(FakeWhisperCase):
    """One host-wide FIFO: requests from two cities (separate settings, one
    queue directory) never run whisper at the same time and run in arrival
    order, with no give-up while waiting."""

    def setUp(self):
        super().setUp()
        self.times = self.root / 'times.jsonl'
        self.cities = {}
        for city in ('city-a', 'city-b'):
            home = self.root / city
            home.mkdir()
            audio = home / 'note.ogg'
            audio.write_bytes(b'OggS')
            self.cities[city] = audio

    def start(self, city, tag, sleep='0.6', answer='Fine.', **env):
        env = dict(self.env, FAKE_WHISPER_ANSWERS=json.dumps([answer]), FAKE_WHISPER_SLEEP=sleep,
                   FAKE_WHISPER_TIMES=str(self.times), FAKE_WHISPER_TAG=tag, **env)
        before = self.tickets()
        proc = subprocess.Popen([sys.executable, str(TRANSCRIBER / 'adapter.py'), str(self.cities[city]), 'small', 'cpu'],
                                env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        deadline = time.monotonic() + 20
        while len(self.tickets()) <= len(before) and proc.poll() is None:  # it took its ticket: arrival fixed
            self.assertLess(time.monotonic(), deadline, 'no ticket taken')
            time.sleep(0.02)
        return proc

    def tickets(self):
        return sorted(p for p in os.listdir(self.queue) if p.endswith('.ticket')) if self.queue.exists() else []

    def finish(self, proc):
        out, err = proc.communicate(timeout=60)
        self.assertEqual(proc.returncode, 0, err)
        result = json.loads(out.splitlines()[-1])
        self.assertIn(result.pop('device', 'cpu: gpu-off'), ('cpu: gpu-off',))
        return result

    def runs(self):
        return [json.loads(line) for line in self.times.read_text().splitlines()]

    def assert_one_at_a_time_in_order(self, tags):
        runs = self.runs()
        self.assertEqual([r['tag'] for r in runs], tags)
        for earlier, later in zip(runs, runs[1:]):
            self.assertLessEqual(earlier['end'], later['start'])

    def test_two_cities_never_run_whisper_at_once_and_go_in_arrival_order(self):
        first = self.start('city-a', 'a1')
        second = self.start('city-b', 'b1')
        self.assertEqual([self.finish(p) for p in (first, second)], [{'text': 'Fine.'}] * 2)
        self.assert_one_at_a_time_in_order(['a1', 'b1'])

    def test_no_environment_moves_a_city_out_of_the_shared_queue(self):
        # The old per-city queue variable is gone: a caller still setting it
        # (each city a different directory) waits in the one queue.
        first = self.start('city-a', 'a1', VOICE_TRANSCRIBE_QUEUE_DIR=str(self.root / 'city-a' / 'q'))
        second = self.start('city-b', 'b1', VOICE_TRANSCRIBE_QUEUE_DIR=str(self.root / 'city-b' / 'q'))
        self.assertEqual([self.finish(p) for p in (first, second)], [{'text': 'Fine.'}] * 2)
        self.assert_one_at_a_time_in_order(['a1', 'b1'])
        self.assertFalse((self.root / 'city-a' / 'q').exists())
        self.assertFalse((self.root / 'city-b' / 'q').exists())

    def test_three_requests_from_two_cities_run_one_by_one_in_arrival_order(self):
        procs = [self.start('city-b', 'b1'), self.start('city-a', 'a1'), self.start('city-b', 'b2')]
        self.assertEqual([self.finish(p) for p in procs], [{'text': 'Fine.'}] * 3)
        self.assert_one_at_a_time_in_order(['b1', 'a1', 'b2'])
        self.assertEqual(self.tickets(), [])

    def test_a_waiting_note_has_no_fixed_give_up(self):
        # The first run takes longer than any old lock wait would allow the
        # second (its run limit does not tick while it waits).
        first = self.start('city-a', 'a1', sleep='3', VOICE_TRANSCRIBE_TIMEOUT='10')
        second = self.start('city-b', 'b1', sleep='0', VOICE_TRANSCRIBE_TIMEOUT='1', VOICE_TRANSCRIBE_TIMEOUT_PER_MINUTE='1')
        self.assertEqual([self.finish(p) for p in (first, second)], [{'text': 'Fine.'}] * 2)
        self.assert_one_at_a_time_in_order(['a1', 'b1'])

    def test_a_hung_whisper_is_killed_at_its_limit_and_the_next_note_runs(self):
        hung = self.start('city-a', 'hung', sleep='60', VOICE_TRANSCRIBE_TIMEOUT='1', VOICE_TRANSCRIBE_TIMEOUT_PER_MINUTE='1')
        nxt = self.start('city-b', 'next', sleep='0')
        started = time.monotonic()
        self.assertEqual(self.finish(hung), {'error': 'timeout'})
        self.assertEqual(self.finish(nxt), {'text': 'Fine.'})
        self.assertLess(time.monotonic() - started, 30)
        self.assertEqual([r['tag'] for r in self.runs()], ['next'])  # the hung run never finished

    def test_a_ticket_left_by_a_dead_process_is_cleared(self):
        self.queue.mkdir(mode=0o700, parents=True)
        (self.queue / '000000000001-99999.ticket').write_text('')  # nobody holds it
        proc = self.start('city-a', 'a1', sleep='0')
        self.assertEqual(self.finish(proc), {'text': 'Fine.'})
        self.assertEqual(self.tickets(), [])

    def test_a_queue_others_can_open_is_refused(self):
        self.queue.mkdir(mode=0o700, parents=True)
        self.queue.chmod(0o755)
        self.assertEqual(self.result('unused'), {'error': 'queue_failed'})
        self.assertEqual(self.whisper_calls(), [])


class FakeComfyUI(http.server.ThreadingHTTPServer):
    """ComfyUI's API on a loopback port. Every request is recorded, whatever
    its method or path, and every POST's Content-Type and body too; GET
    /queue and GET /system_stats answer from per-path scripts (one answer per
    call, the last repeats). An answer is a JSON value, ('raw', status,
    bytes), or ('sleep', seconds) before an idle answer. POST /free answers
    200 with an empty body only when free_status is set to 200."""

    daemon_threads = True
    block_on_close = False

    def __init__(self):
        self.requests, self.scripts, self.lock = [], {}, threading.Lock()
        self.posts, self.free_status = [], None
        super().__init__(('127.0.0.1', 0), self.Handler)

    def handle_error(self, request, client_address):
        pass  # a client that gave up at its timeout closed the connection

    @property
    def url(self):
        return f'http://127.0.0.1:{self.server_address[1]}'

    def answer(self, method, path, content_type=None, body=b''):
        with self.lock:
            self.requests.append((method, path))
            calls = sum(1 for m, p in self.requests if (m, p) == (method, path))
            if method == 'POST':
                self.posts.append((path, content_type, body))
        if (method, path) == ('POST', '/free') and self.free_status is not None:
            return self.free_status, b''
        script = self.scripts.get(path) if method == 'GET' else None
        if not script:
            return 405 if method != 'GET' else 404, b'{}'
        answer = script[min(calls, len(script)) - 1]
        if isinstance(answer, tuple) and answer[0] == 'raw':
            return answer[1], answer[2]
        if isinstance(answer, tuple) and answer[0] == 'sleep':
            time.sleep(answer[1])
            answer = IDLE
        return 200, json.dumps(answer).encode()

    class Handler(http.server.BaseHTTPRequestHandler):
        def respond(self):
            sent = self.rfile.read(int(self.headers.get('Content-Length') or 0))
            status, body = self.server.answer(self.command, self.path, self.headers.get('Content-Type'), sent)
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            if self.command != 'HEAD':
                self.wfile.write(body)

        do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = do_OPTIONS = respond

        def log_message(self, *args):
            pass


IDLE = {'queue_running': [], 'queue_pending': []}
BUSY_RUNNING = {'queue_running': [[1, 'prompt-id', {}, {}, []]], 'queue_pending': []}
BUSY_PENDING = {'queue_running': [], 'queue_pending': [[2, 'prompt-id', {}, {}, []]]}
GIB = 1024 ** 3


def stats(free, held=0, devices=None):
    """A /system_stats answer: one CUDA device with free bytes, held of them
    in ComfyUI's own allocator."""
    return {'system': {'comfyui_version': '0.38.0'}, 'devices': devices if devices is not None else [
        {'name': 'cuda:0 Fake GPU', 'type': 'cuda', 'index': 0, 'vram_total': 16 * GIB, 'vram_free': free,
         'torch_vram_total': held, 'torch_vram_free': held}]}


class GpuChoiceTests(FakeWhisperCase):
    """Device choice once the note's turn starts: the GPU only when ComfyUI is
    idle (or not listening) and the card has room, /queue again right before
    the GPU run, a failed GPU run retried on the CPU, and every doubt the CPU.
    Across every test ComfyUI sees only GET /queue and GET /system_stats."""

    ALLOWED = {('GET', '/queue'), ('GET', '/system_stats')}

    def setUp(self):
        super().setUp()
        self.comfy = FakeComfyUI()
        threading.Thread(target=self.comfy.serve_forever, daemon=True).start()
        self.addCleanup(self.comfy.server_close)
        self.addCleanup(self.comfy.shutdown)
        self.env.update(VOICE_TRANSCRIBE_COMFYUI_URL=self.comfy.url, FAKE_NVIDIA_SMI='926, 16311\n')
        self.cpu_argv = None

    def tearDown(self):
        # Only the comfyui_free tests may also send their one POST /free.
        allowed = self.ALLOWED | ({('POST', '/free')} if self.env.get('VOICE_TRANSCRIBE_COMFYUI_FREE') == '1' else set())
        self.assertLessEqual(set(self.comfy.requests), allowed, self.comfy.requests)
        super().tearDown()

    def comfy_answers(self, queue, system_stats=None):
        self.comfy.scripts = {'/queue': queue, '/system_stats': [system_stats] if system_stats is not None else []}

    def gpu(self, *answers, **kwargs):
        return self.result(*answers, device='auto', **kwargs)

    def assert_cpu_run(self, argv):
        self.assertIn('-ng', argv)

    def assert_gpu_run(self, argv):
        self.assertNotIn('-ng', argv)
        # The GPU call is the CPU call without -ng, nothing else (the log
        # leaves out the binary).
        cpu = vt.whisper_argv(vt.Options(environ=self.env), Path(argv[argv.index('-f') + 1]), no_context='-mc' in argv)
        self.assertEqual(['-ng', *argv], cpu[1:])

    def test_idle_with_room_runs_on_the_gpu(self):
        self.comfy_answers([IDLE], stats(free=12 * GIB, held=GIB))
        self.assertEqual(self.gpu('Ship it.'), {'text': 'Ship it.'})
        self.assertEqual(self.device, 'gpu')
        [argv] = self.whisper_calls()
        self.assert_gpu_run(argv)
        self.assertEqual(self.whisper_cuda(), [None])  # the GPU run inherits the device list
        # /queue, /system_stats, then /queue again right before the GPU run.
        self.assertEqual(self.comfy.requests, [('GET', '/queue'), ('GET', '/system_stats'), ('GET', '/queue')])
        self.assertEqual(self.nvidia_smi_calls(), [])

    def test_a_busy_comfyui_means_the_cpu(self):
        for name, queue in (('running', BUSY_RUNNING), ('pending', BUSY_PENDING)):
            with self.subTest(name):
                self.log.unlink(missing_ok=True)
                self.comfy.requests.clear()
                self.comfy_answers([queue], stats(free=15 * GIB))
                self.assertEqual(self.gpu('Fine.'), {'text': 'Fine.'})
                self.assertEqual(self.device, 'cpu: comfy-busy')
                [argv] = self.whisper_calls()
                self.assert_cpu_run(argv)
                self.assertEqual(self.comfy.requests, [('GET', '/queue')])

    def test_idle_but_full_means_the_cpu(self):
        # 2048 MiB needed: 1.5 GiB free is too little, and so is 3 GiB of
        # which ComfyUI's own allocator still holds 2 GiB.
        for free, held in ((3 * GIB // 2, 0), (3 * GIB, 2 * GIB)):
            with self.subTest(free=free, held=held):
                self.log.unlink(missing_ok=True)
                self.comfy_answers([IDLE], stats(free=free, held=held))
                self.assertEqual(self.gpu('Fine.'), {'text': 'Fine.'})
                self.assertEqual(self.device, 'cpu: vram-low')
                self.assert_cpu_run(self.whisper_calls()[0])
        self.env['VOICE_TRANSCRIBE_GPU_MIN_FREE_MB'] = '1024'
        self.log.unlink()
        self.assertEqual(self.gpu('Fine.'), {'text': 'Fine.'})
        self.assertEqual(self.device, 'gpu')  # the margin is a setting
        self.assertEqual(self.comfy.posts, [])  # comfyui_free off: never asked to unload

    def test_an_idle_comfyui_asked_to_unload_frees_the_gpu(self):
        # comfyui_free on, idle but full: one POST /free with exactly
        # {"unload_models": true} as JSON, the memory read again, the GPU.
        self.env['VOICE_TRANSCRIBE_COMFYUI_FREE'] = '1'
        self.comfy.free_status = 200
        self.comfy.scripts = {'/queue': [IDLE], '/system_stats': [stats(free=GIB), stats(free=12 * GIB)]}
        self.assertEqual(self.gpu('Ship it.'), {'text': 'Ship it.'})
        self.assertEqual(self.device, 'gpu: comfy-freed')
        [argv] = self.whisper_calls()
        self.assert_gpu_run(argv)
        self.assertEqual(self.comfy.posts, [('/free', 'application/json', b'{"unload_models": true}')])
        # The POST follows an empty /queue read at once.
        self.assertEqual(self.comfy.requests, [('GET', '/queue'), ('GET', '/system_stats'), ('GET', '/queue'),
                                               ('POST', '/free'), ('GET', '/system_stats'), ('GET', '/queue')])

    def test_a_job_queued_after_the_free_sends_the_note_to_the_cpu(self):
        self.env['VOICE_TRANSCRIBE_COMFYUI_FREE'] = '1'
        self.comfy.free_status = 200
        self.comfy.scripts = {'/queue': [IDLE, IDLE, BUSY_PENDING],
                              '/system_stats': [stats(free=GIB), stats(free=12 * GIB)]}
        self.assertEqual(self.gpu('Fine.'), {'text': 'Fine.'})
        self.assertEqual(self.device, 'cpu: comfy-busy')
        self.assert_cpu_run(self.whisper_calls()[0])
        self.assertEqual(len(self.comfy.posts), 1)
        self.assertEqual(self.comfy.requests[-1], ('GET', '/queue'))

    def test_a_refused_free_means_the_cpu(self):
        self.env['VOICE_TRANSCRIBE_COMFYUI_FREE'] = '1'
        for status in (None, 500):  # None: the fake's 405 for an unscripted POST
            with self.subTest(status=status):
                self.log.unlink(missing_ok=True)
                self.comfy.requests.clear()
                self.comfy.posts.clear()
                self.comfy.free_status = status
                self.comfy_answers([IDLE], stats(free=GIB))
                self.assertEqual(self.gpu('Fine.'), {'text': 'Fine.'})
                self.assertEqual(self.device, 'cpu: api-error')
                self.assert_cpu_run(self.whisper_calls()[0])
                self.assertEqual(self.comfy.requests,
                                 [('GET', '/queue'), ('GET', '/system_stats'), ('GET', '/queue'), ('POST', '/free')])

    def test_comfyui_free_never_posts_to_a_busy_or_absent_comfyui(self):
        self.env['VOICE_TRANSCRIBE_COMFYUI_FREE'] = '1'
        self.comfy.free_status = 200
        self.comfy_answers([BUSY_RUNNING], stats(free=GIB))
        self.assertEqual(self.gpu('Fine.'), {'text': 'Fine.'})
        self.assertEqual(self.device, 'cpu: comfy-busy')
        # Not listening: nvidia-smi decides as before and nothing is sent.
        self.env.update(VOICE_TRANSCRIBE_COMFYUI_URL=f'http://127.0.0.1:{closed_port()}', FAKE_NVIDIA_SMI='15000, 16311\n')
        self.log.unlink()
        self.assertEqual(self.gpu('Fine.'), {'text': 'Fine.'})
        self.assertEqual(self.device, 'cpu: vram-low')
        self.assertEqual(self.comfy.posts, [])

    def test_comfyui_not_listening_asks_nvidia_smi(self):
        self.env['VOICE_TRANSCRIBE_COMFYUI_URL'] = f'http://127.0.0.1:{closed_port()}'
        for smi, device in (('926, 16311\n', 'gpu'), ('15000, 16311\n', 'cpu: vram-low')):
            with self.subTest(smi=smi):
                self.log.unlink(missing_ok=True)
                self.env['FAKE_NVIDIA_SMI'] = smi
                self.assertEqual(self.gpu('Fine.'), {'text': 'Fine.'})
                self.assertEqual(self.device, device)
                [argv] = self.whisper_calls()
                (self.assert_gpu_run if device == 'gpu' else self.assert_cpu_run)(argv)
        self.assertEqual(self.nvidia_smi_calls(), ['--query-gpu=memory.used,memory.total --format=csv,noheader,nounits'] * 2)
        self.assertEqual(self.comfy.requests, [])

    def test_an_nvidia_smi_that_fails_or_says_something_else_means_the_cpu(self):
        self.env['VOICE_TRANSCRIBE_COMFYUI_URL'] = f'http://127.0.0.1:{closed_port()}'
        for smi, code in (('926, 16311\n', '9'), ('', '0'), ('N/A, 16311\n', '0'), ('926, 16311\n926, 16311\n', '0'),
                          ('17000, 16311\n', '0'), ('926 16311\n', '0')):
            with self.subTest(smi=smi, code=code):
                self.log.unlink(missing_ok=True)
                self.env.update(FAKE_NVIDIA_SMI=smi, FAKE_NVIDIA_SMI_EXIT=code)
                self.assertEqual(self.gpu('Fine.'), {'text': 'Fine.'})
                self.assertEqual(self.device, 'cpu: api-error')
                self.assert_cpu_run(self.whisper_calls()[0])

        def missing(*args, **kwargs):
            raise FileNotFoundError('nvidia-smi')

        self.assertEqual(vt.choose_device(vt.Options(environ=self.env, gpu=True), run=missing), ('cpu', 'api-error'))

    def test_malformed_answers_and_errors_mean_the_cpu(self):
        broken = (
            ([('raw', 200, b'not json')], None),
            ([('raw', 500, b'{}')], None),
            ([('raw', 200, b'[]')], None),
            ([{'queue_running': None, 'queue_pending': []}], None),
            ([{'queue_running': []}], None),
            ([IDLE], ('raw', 200, b'{"devices": "x"}')),
            ([IDLE], stats(0, devices=[])),
            ([IDLE], stats(0, devices=[{'type': 'cpu', 'vram_free': 15 * GIB, 'torch_vram_free': 0}])),
            ([IDLE], stats(0, devices=[{'type': 'cuda', 'vram_free': 15 * GIB, 'torch_vram_free': 0}] * 2)),
            ([IDLE], stats(0, devices=[{'type': 'cuda', 'vram_free': '15000000000', 'torch_vram_free': 0}])),
            ([IDLE], stats(0, devices=[{'type': 'cuda', 'vram_free': True, 'torch_vram_free': 0}])),
            ([IDLE], stats(0, devices=[{'type': 'cuda', 'vram_free': 15 * GIB}])),
            ([IDLE], stats(0, devices=[{'type': 'cuda', 'vram_free': GIB, 'torch_vram_free': 2 * GIB}])),
            ([IDLE], ('raw', 200, b'{' + b' ' * vt.COMFYUI_MAX_BYTES + b'}')),
        )
        for queue, system_stats in broken:
            with self.subTest(queue=queue, system_stats=str(system_stats)[:80]):
                self.log.unlink(missing_ok=True)
                self.comfy_answers(queue, system_stats)
                self.assertEqual(self.gpu('Fine.'), {'text': 'Fine.'})
                self.assertEqual(self.device, 'cpu: api-error')
                self.assert_cpu_run(self.whisper_calls()[0])
        self.assertEqual(self.nvidia_smi_calls(), [])  # an answering ComfyUI is never second-guessed

    def test_a_comfyui_that_does_not_answer_in_time_means_the_cpu(self):
        self.comfy_answers([('sleep', vt.COMFYUI_TIMEOUT + 1)], stats(free=15 * GIB))
        started = time.monotonic()
        self.assertEqual(self.gpu('Fine.'), {'text': 'Fine.'})
        self.assertEqual(self.device, 'cpu: api-error')
        self.assertLess(time.monotonic() - started, vt.COMFYUI_TIMEOUT + 10)
        self.assert_cpu_run(self.whisper_calls()[0])
        self.assertEqual(self.nvidia_smi_calls(), [])

    def test_comfyui_turning_busy_before_the_gpu_run_means_the_cpu(self):
        self.comfy_answers([IDLE, BUSY_PENDING], stats(free=15 * GIB))
        self.assertEqual(self.gpu('Fine.'), {'text': 'Fine.'})
        self.assertEqual(self.device, 'cpu: comfy-busy')
        [argv] = self.whisper_calls()
        self.assert_cpu_run(argv)
        self.assertEqual(self.comfy.requests, [('GET', '/queue'), ('GET', '/system_stats'), ('GET', '/queue')])

    def test_a_recheck_that_errors_means_the_cpu(self):
        self.comfy_answers([IDLE, ('raw', 200, b'garbage')], stats(free=15 * GIB))
        self.assertEqual(self.gpu('Fine.'), {'text': 'Fine.'})
        self.assertEqual(self.device, 'cpu: api-error')
        self.assert_cpu_run(self.whisper_calls()[0])

    def test_a_failed_gpu_run_is_retried_once_on_the_cpu(self):
        self.comfy_answers([IDLE], stats(free=15 * GIB))
        self.assertEqual(self.gpu('FAIL', 'Still here.'), {'text': 'Still here.'})
        self.assertEqual(self.device, 'cpu: gpu-failed')
        gpu_run, cpu_run = self.whisper_calls()
        self.assert_gpu_run(gpu_run)
        self.assert_cpu_run(cpu_run)
        self.assertEqual(self.whisper_cuda(), [None, ''])
        self.assertEqual(self.gpu('FAIL', 'FAIL'), {'error': 'whisper_failed'})  # the CPU retry is the last word
        self.assertEqual(len(self.whisper_calls()), 4)

    def test_a_loop_retry_on_the_gpu_checks_comfyui_again(self):
        looped = 'what is it? what is it? what is it? what is it?'
        self.comfy_answers([IDLE, IDLE, BUSY_RUNNING], stats(free=15 * GIB))
        self.assertEqual(self.gpu(looped, 'what is it, he asked'), {'text': 'what is it, he asked'})
        self.assertEqual(self.device, 'cpu: comfy-busy')
        first, retry = self.whisper_calls()
        self.assert_gpu_run(first)
        self.assert_cpu_run(retry)
        self.assertEqual(retry[retry.index('-mc') + 1], '0')
        self.assertEqual(self.comfy.requests.count(('GET', '/queue')), 3)

    def test_gpu_off_never_contacts_comfyui_or_nvidia_smi(self):
        self.comfy_answers([IDLE], stats(free=15 * GIB))
        for _ in range(2):
            self.assertEqual(self.result('Fine.'), {'text': 'Fine.'})
            self.assertEqual(self.device, 'cpu: gpu-off')
        self.assertTrue(all('-ng' in argv for argv in self.whisper_calls()))
        self.assertEqual(self.whisper_cuda(), ['', ''])
        self.assertEqual((self.comfy.requests, self.nvidia_smi_calls()), ([], []))
        # The command by hand: the GPU only with VOICE_TRANSCRIBE_GPU=1.
        for value, device in (('0', 'cpu: gpu-off'), ('true', 'cpu: gpu-off'), ('1', 'gpu')):
            with self.subTest(value=value):
                env = dict(self.env, VOICE_TRANSCRIBE_GPU=value, FAKE_WHISPER_ANSWERS='["Hello."]')
                done = subprocess.run([sys.executable, str(TRANSCRIBER / 'voice_transcribe.py'), '--quiet', '--force',
                                       str(self.audio)], env=env, capture_output=True, text=True, timeout=60)
                self.assertEqual((done.returncode, done.stdout), (0, 'Hello.\n'), done.stderr)
                self.assertEqual(done.stderr, f'voice-transcribe: {self.audio}: device {device}\n')

    def test_a_comfyui_address_off_loopback_is_never_contacted(self):
        for url in ('http://192.0.2.1:8188', 'http://localhost:8188', f'{self.comfy.url}/api'):
            with self.subTest(url=url):
                self.log.unlink(missing_ok=True)
                self.env['VOICE_TRANSCRIBE_COMFYUI_URL'] = url
                self.assertEqual(self.gpu('Fine.'), {'text': 'Fine.'})
                self.assertEqual(self.device, 'cpu: api-error')
                self.assert_cpu_run(self.whisper_calls()[0])
        self.assertEqual((self.comfy.requests, self.nvidia_smi_calls()), ([], []))


class FakeComfyApi:
    """ComfyUI's API as the injected get and post seams of choose_device, with
    a fake clock: every call is recorded; /system_stats answers free bytes
    from a script and /queue its answer, or one of a list per read (the last
    repeats); post raises `fails`."""

    def __init__(self, queue=None, free=(), fails=None):
        self.queue = queue if isinstance(queue, list) else [queue or IDLE]
        self.free, self.fails = list(free), fails
        self.calls, self.sleeps, self.now = [], [], 1000.0

    def get(self, address, path):
        self.calls.append(('GET', path))
        script = self.queue if path == '/queue' else self.free
        answer = script[min(sum(1 for c in self.calls if c == ('GET', path)), len(script)) - 1]
        if isinstance(answer, Exception):
            raise answer
        return answer if path == '/queue' else stats(free=answer)

    def post(self, address, path, payload):
        self.calls.append(('POST', path, payload))
        if self.fails is not None:
            raise self.fails

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds

    def reads_after_post(self):
        posted = [i for i, c in enumerate(self.calls) if c[0] == 'POST']
        return sum(1 for c in self.calls[posted[0]:] if c == ('GET', '/system_stats')) if posted else 0


class ComfyFreeTests(unittest.TestCase):
    """comfyui_free: an idle ComfyUI short of the margin is asked once to
    unload its models, the memory is read every 0.5 s for at most 10 s, and
    every doubt means the CPU; in process, with fakes and a fake clock."""

    FREE = ('POST', '/free', {'unload_models': True})

    def setUp(self):
        # A closed port: nothing real could be reached even without the fakes.
        self.env = {'VOICE_TRANSCRIBE_COMFYUI_FREE': '1', 'VOICE_TRANSCRIBE_COMFYUI_URL': f'http://127.0.0.1:{closed_port()}'}

    def choose(self, api, comfyui_free=True, deadline=None):
        def no_nvidia_smi(*args, **kwargs):
            raise AssertionError('nvidia-smi must not run while ComfyUI answers')

        env = self.env if comfyui_free else {'VOICE_TRANSCRIBE_COMFYUI_URL': self.env['VOICE_TRANSCRIBE_COMFYUI_URL']}
        return vt.choose_device(vt.Options(environ=env, gpu=True), run=no_nvidia_smi, get=api.get, post=api.post,
                                clock=api.clock, sleep=api.sleep, deadline=deadline)

    def test_the_memory_freed_on_the_third_poll_means_the_gpu(self):
        api = FakeComfyApi(free=[GIB, GIB, GIB, 12 * GIB])
        self.assertEqual(self.choose(api), ('gpu', 'comfy-freed'))
        self.assertEqual(api.calls, [('GET', '/queue'), ('GET', '/system_stats'), ('GET', '/queue'), self.FREE]
                         + [('GET', '/system_stats')] * 3)
        self.assertEqual(api.sleeps, [vt.COMFYUI_FREE_POLL] * 2)
        self.assertEqual(vt.Device('gpu', 'comfy-freed').label, 'gpu: comfy-freed')

    def test_memory_that_never_frees_means_the_cpu_after_10_s(self):
        api = FakeComfyApi(free=[GIB])
        self.assertEqual(self.choose(api), ('cpu', 'vram-low'))
        self.assertEqual([c for c in api.calls if c[0] == 'POST'], [self.FREE])
        self.assertLessEqual(api.reads_after_post(), 21)
        self.assertEqual(api.now - 1000.0, vt.COMFYUI_FREE_WAIT)
        self.assertEqual(vt.COMFYUI_FREE_WAIT, 10)

    def test_the_wait_ends_at_the_notes_deadline(self):
        api = FakeComfyApi(free=[GIB])
        self.assertEqual(self.choose(api, deadline=1003.2), ('cpu', 'vram-low'))
        self.assertEqual(api.now, 1003.2)  # never past the run limit
        self.assertEqual(api.reads_after_post(), 8)

    def test_a_busy_comfyui_is_never_asked(self):
        for queue in (BUSY_RUNNING, BUSY_PENDING):
            with self.subTest(queue=queue):
                api = FakeComfyApi(queue=queue, free=[GIB])
                self.assertEqual(self.choose(api), ('cpu', 'comfy-busy'))
                self.assertEqual(api.calls, [('GET', '/queue')])

    def test_a_job_queued_before_the_post_means_no_post(self):
        # /queue is read again right before the POST: a job that arrived
        # since the first read keeps ComfyUI's models loaded.
        for queue in (BUSY_RUNNING, BUSY_PENDING):
            with self.subTest(queue=queue):
                api = FakeComfyApi(queue=[IDLE, queue], free=[GIB])
                self.assertEqual(self.choose(api), ('cpu', 'comfy-busy'))
                self.assertEqual(api.calls, [('GET', '/queue'), ('GET', '/system_stats'), ('GET', '/queue')])
        for error in (vt.ComfyError(), vt.ComfyDown()):
            with self.subTest(read=type(error).__name__):
                api = FakeComfyApi(queue=[IDLE, error], free=[GIB])
                self.assertEqual(self.choose(api), ('cpu', 'api-error'))
                self.assertEqual(api.calls, [('GET', '/queue'), ('GET', '/system_stats'), ('GET', '/queue')])

    def test_comfyui_free_off_never_asks(self):
        api = FakeComfyApi(free=[GIB])
        self.assertEqual(self.choose(api, comfyui_free=False), ('cpu', 'vram-low'))
        self.assertEqual(api.calls, [('GET', '/queue'), ('GET', '/system_stats')])
        self.assertEqual(api.sleeps, [])

    def test_enough_memory_already_never_asks(self):
        api = FakeComfyApi(free=[12 * GIB])
        self.assertEqual(self.choose(api), ('gpu', 'gpu'))
        self.assertEqual(api.calls, [('GET', '/queue'), ('GET', '/system_stats')])

    def test_a_failed_post_or_read_means_the_cpu_with_no_second_post(self):
        for fails in (vt.ComfyError(), vt.ComfyDown()):
            with self.subTest(fails=type(fails).__name__):
                api = FakeComfyApi(free=[GIB], fails=fails)
                self.assertEqual(self.choose(api), ('cpu', 'api-error'))
                self.assertEqual(api.calls, [('GET', '/queue'), ('GET', '/system_stats'), ('GET', '/queue'), self.FREE])
        for error in (vt.ComfyError(), vt.ComfyDown()):
            with self.subTest(read=type(error).__name__):
                api = FakeComfyApi(free=[GIB, GIB, error])
                self.assertEqual(self.choose(api), ('cpu', 'api-error'))
                self.assertEqual([c for c in api.calls if c[0] == 'POST'], [self.FREE])

    def test_the_post_goes_out_as_one_json_request_on_the_get_transport(self):
        class Response:
            def __init__(self, status, body):
                self.status, self.body = status, body

            def read(self, limit):
                return self.body[:limit]

        class Connection:
            made = []

            def __init__(self, host, port, timeout):
                self.args, self.sent = (host, port, timeout), None
                Connection.made.append(self)

            def request(self, method, path, body=None, headers=None):
                self.sent = (method, path, body, headers)
                if Connection.refuse:
                    raise ConnectionRefusedError()

            def getresponse(self):
                return Response(*Connection.answer)

            def close(self):
                pass

        for answer, refuse, outcome in (((200, b''), False, None), ((200, b'{"anything": 1}'), False, None),
                                        ((500, b''), False, vt.ComfyError),
                                        ((200, b'x' * (vt.COMFYUI_MAX_BYTES + 1)), False, vt.ComfyError),
                                        ((200, b''), True, vt.ComfyDown)):
            with self.subTest(answer=answer[0], size=len(answer[1]), refuse=refuse):
                Connection.answer, Connection.refuse, Connection.made = answer, refuse, []
                if outcome is None:
                    self.assertIsNone(vt.comfyui_post(('127.0.0.1', 8999), '/free', {'unload_models': True},
                                                      connection=Connection))
                else:
                    with self.assertRaises(outcome):
                        vt.comfyui_post(('127.0.0.1', 8999), '/free', {'unload_models': True}, connection=Connection)
                [conn] = Connection.made
                self.assertEqual(conn.args, ('127.0.0.1', 8999, vt.COMFYUI_TIMEOUT))
                self.assertEqual(conn.sent, ('POST', '/free', b'{"unload_models": true}',
                                             {'Content-Type': 'application/json'}))


class GpuRunLimitTests(unittest.TestCase):
    """A GPU run that hangs is stopped at its own limit, so the CPU retry
    still has its whole run limit; in process, with fake runs."""

    def test_a_hung_gpu_run_hands_over_to_the_cpu_with_time_left(self):
        options = vt.Options(environ={'VOICE_TRANSCRIBE_TIMEOUT': '120'}, gpu=True)
        calls = []

        def run(argv, timeout, env, **kwargs):
            calls.append(('-ng' in argv, round(timeout), env and env.get('CUDA_VISIBLE_DEVICES')))
            if '-ng' not in argv:
                raise subprocess.TimeoutExpired(argv, timeout)
            return subprocess.CompletedProcess(argv, 0, stdout='Done.', stderr='')

        device = vt.Device('gpu', 'gpu')
        gpu_limit = options.gpu_run_limit(60)
        deadline = time.monotonic() + options.run_limit(60) + gpu_limit
        text = vt._whisper(options, Path('/x.wav'), run, device, deadline, gpu_limit, get=lambda *a: IDLE)
        self.assertEqual((text, device.label), ('Done.', 'cpu: gpu-failed'))
        (gpu, gpu_timeout, gpu_cuda), (cpu, cpu_timeout, cpu_cuda) = calls
        self.assertEqual((gpu, gpu_timeout, gpu_cuda), (False, gpu_limit, None))
        self.assertEqual((cpu, cpu_cuda), (True, ''))
        self.assertGreaterEqual(cpu_timeout, options.run_limit(60) - 1)  # the CPU keeps its whole limit

    def test_the_gpu_limit_is_a_quarter_of_the_run_limit_at_least_30_s(self):
        options = vt.Options(environ={'VOICE_TRANSCRIBE_TIMEOUT': '120', 'VOICE_TRANSCRIBE_TIMEOUT_PER_MINUTE': '120'})
        self.assertEqual([options.gpu_run_limit(s) for s in (0, 72, 600)], [30, 66, 330])
        self.assertIs(options.gpu, False)


class LoopAndScriptTests(unittest.TestCase):
    def test_longest_repeat_counts_back_to_back_phrases_of_two_words_or_more(self):
        self.assertEqual(vt.longest_repeat('Что такое? Что такое? Что такое? Что такое?'), 4)
        self.assertEqual(vt.longest_repeat('very very very good'), 1)  # one word is speech, not a loop
        self.assertEqual(vt.longest_repeat('ship the pack, ship the pack, then test'), 2)
        self.assertEqual(vt.longest_repeat(''), 1)

    def test_is_english_wants_mostly_latin_letters(self):
        self.assertTrue(vt.is_english('Deploy the shared library on Friday.'))
        self.assertTrue(vt.is_english('42 — 17'))
        self.assertTrue(vt.is_english('Café naïve résumé'))
        self.assertFalse(vt.is_english('Мы обновим shared library до пятницы'))


if __name__ == '__main__':
    unittest.main()
