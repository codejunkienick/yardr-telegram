"""check-install --gpu-probe: a fake systemd-run on PATH stands in for the
transient unit and prints what the probe helper would; the helper itself runs
with a fake transcriber. Nothing here runs whisper, systemd or the GPU."""
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('bridge', Path(__file__).with_name('bridge.py'))
b = importlib.util.module_from_spec(spec)
spec.loader.exec_module(b)
probe_spec = importlib.util.spec_from_file_location(
    'gpu_probe', Path(__file__).resolve().parents[1] / 'transcriber' / 'gpu_probe.py')
helper = importlib.util.module_from_spec(probe_spec)
probe_spec.loader.exec_module(helper)
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_bridge import write_settings  # noqa: E402

RUNNING = '{"state": "running"}'
CLIP = b'RIFF\x24\x00\x00\x00WAVEfmt '


class ProbeFixture(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        checkout = self.root / 'whisper.cpp'
        (checkout / 'build' / 'bin').mkdir(parents=True)
        (checkout / 'samples').mkdir()
        self.cli = checkout / 'build' / 'bin' / 'whisper-cli'
        self.cli.write_text('#!/bin/sh\n')
        self.cli.chmod(0o755)
        self.jfk = checkout / 'samples' / 'jfk.wav'
        self.jfk.write_bytes(CLIP)
        self.home = self.root / 'home'
        (self.home / b.QUEUE_SUBDIR).mkdir(parents=True, mode=0o700)
        (self.root / 'state').mkdir()
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        self.calls = self.root / 'systemd-run-calls'
        fake = self.bin / 'systemd-run'
        fake.write_text(f'#!{sys.executable}\nimport json, os, sys\n'
                        f'with open({str(self.calls)!r}, "a") as log:\n'
                        '    log.write(json.dumps(dict(argv=sys.argv[1:], stdin=sys.stdin.buffer.read().hex())) + "\\n")\n'
                        'sys.stdout.write(os.environ.get("FAKE_PROBE_STDOUT", ""))\n'
                        'sys.exit(int(os.environ.get("FAKE_PROBE_EXIT", "0")))\n')
        fake.chmod(0o755)
        # HOME is the temp home too: main() loads its settings with the real
        # Path.home(), so the transcription queue the probe checks is ours.
        self.enterContext(patch.dict(os.environ, PATH=f'{self.bin}:{os.environ["PATH"]}', HOME=str(self.home)))

    def settings(self, transcribe='enabled = true\ngpu = true\n', **replace):
        path = write_settings(self.root, python=f'"{sys.executable}"', **replace)
        path.write_text(path.read_text() + f'\n[transcribe]\n{transcribe}whisper_cli = "{self.cli}"\n')
        return b.load_settings(path, home=self.home)

    def probe(self, stdout='', exit_code=0, settings=None, **kwargs):
        with patch.dict(os.environ, FAKE_PROBE_STDOUT=stdout, FAKE_PROBE_EXIT=str(exit_code)):
            return b.gpu_probe(settings or self.settings(), **kwargs)

    def recorded(self):
        if not self.calls.exists():
            return []
        return [json.loads(line) for line in self.calls.read_text().splitlines()]


class GpuProbeTests(ProbeFixture):
    def test_device_gpu_is_ok_with_the_run_time(self):
        self.assertEqual(self.probe(f'{RUNNING}\n{{"device": "gpu", "seconds": 2.4}}\n'),
                         ('ok', 'gpu probe: device gpu (2.4 s)'))

    def test_a_gpu_with_a_check_reason_is_ok(self):
        self.assertEqual(self.probe(f'{RUNNING}\n{{"device": "gpu: unknown", "seconds": 2.4}}\n'),
                         ('ok', 'gpu probe: device gpu: unknown (2.4 s)'))

    def test_a_gpu_freed_by_comfyui_is_ok_too(self):
        # The probe decides its device like a note: with comfyui_free on it
        # may ask an idle ComfyUI to unload its models first.
        self.assertEqual(self.probe(f'{RUNNING}\n{{"device": "gpu: comfy-freed", "seconds": 3.1}}\n'),
                         ('ok', 'gpu probe: device gpu: comfy-freed (3.1 s)'))

    def test_gpu_failed_is_a_failure(self):
        self.assertEqual(self.probe(f'{RUNNING}\n{{"device": "cpu: gpu-failed", "seconds": 40.1}}\n'),
                         ('FAIL', 'gpu probe: device cpu: gpu-failed'))

    def test_a_busy_or_full_card_is_a_skip_to_try_later(self):
        for reason in ('comfy-busy', 'vram-low', 'api-error'):
            self.assertEqual(self.probe(f'{RUNNING}\n{{"device": "cpu: {reason}", "seconds": 30}}\n'),
                             ('skip', f'gpu probe: ComfyUI busy or card full ({reason}), try later'))

    def test_transcriber_errors_map_to_their_fixed_phrases(self):
        self.assertEqual(self.probe('{"error": "whisper_missing"}\n'),
                         ('FAIL', 'gpu probe: transcriber missing (whisper-cli not found)'))
        self.assertEqual(self.probe('{"error": "clip_unreadable"}\n'),
                         ('FAIL', 'gpu probe: the clip is empty or over 25 MiB'))
        self.assertEqual(self.probe('{"error": "something new"}\n'), ('FAIL', 'gpu probe: invalid output'))
        self.assertEqual(self.probe(f'{RUNNING}\n{{"error": "not_english", "device": "gpu", "seconds": 3}}\n'),
                         ('ok', 'gpu probe: device gpu (3 s)'))  # the GPU ran; the clip's language is not the question

    def test_output_is_parsed_strictly_and_never_echoed(self):
        for stdout in ('', 'garbage\n', '[]\n', '{}\n', f'{RUNNING}\n{{"device": "gpu"}}\n',
                       '{"device": "gpu", "seconds": 1, "text": "ask not what your country"}\n',
                       'ask not what your country\n{"device": "gpu", "seconds": 1}\n',
                       '{"device": "gpu: FAST!", "seconds": 1}\n', '{"device": "gpu", "seconds": -1}\n',
                       '{"device": "gpu", "seconds": true}\n', 'x' * (b.GPU_PROBE_OUTPUT_MAX + 1)):
            self.assertEqual(self.probe(stdout), ('FAIL', 'gpu probe: invalid output'), stdout[:80])
        self.assertEqual(self.probe('{"device": "gpu", "seconds": 1}\n', exit_code=3),
                         ('FAIL', 'gpu probe: the probe unit failed (exit 3)'))

    def test_the_default_clip_is_jfk_of_the_whisper_checkout_and_goes_in_on_stdin(self):
        settings = self.settings()
        self.assertEqual(b.default_probe_clip(settings), self.jfk)
        self.probe('{"device": "gpu", "seconds": 1}\n', settings=settings)
        self.assertEqual(bytes.fromhex(self.recorded()[0]['stdin']), CLIP)
        other = self.root / 'mine.ogg'
        other.write_bytes(b'OggS-mine')
        self.probe('{"device": "gpu", "seconds": 1}\n', settings=settings, clip=other)
        self.assertEqual(bytes.fromhex(self.recorded()[1]['stdin']), b'OggS-mine')

    def test_no_clip_is_one_failure_line_and_runs_nothing(self):
        self.jfk.unlink()
        self.assertEqual(self.probe(), ('FAIL', f'gpu probe: no clip: {self.jfk} is missing; pass --clip'))
        self.assertEqual(self.probe(clip=self.root / 'absent.wav'),
                         ('FAIL', f'gpu probe: clip {self.root / "absent.wav"} is not a file'))
        self.assertEqual(self.recorded(), [])

    def test_transcription_or_the_gpu_off_is_one_skip_line(self):
        self.assertEqual(self.probe(settings=self.settings('enabled = false\ngpu = true\n')),
                         ('skip', 'gpu probe: transcription off ([transcribe] enabled = false)'))
        self.assertEqual(self.probe(settings=self.settings('enabled = true\ngpu = false\n')),
                         ('skip', 'gpu probe: gpu = false'))
        self.assertEqual(self.recorded(), [])

    def test_method_cpu_skips_and_method_auto_and_gpu_probe(self):
        self.assertEqual(self.probe(settings=self.settings('enabled = true\nmethod = "cpu"\n')),
                         ('skip', 'gpu probe: method = cpu'))
        self.assertEqual(self.recorded(), [])
        for method in ('auto', 'gpu'):
            with self.subTest(method=method):
                settings = self.settings(f'enabled = true\nmethod = "{method}"\n')
                self.assertEqual(self.probe('{"device": "gpu", "seconds": 1}\n', settings=settings),
                                 ('ok', 'gpu probe: device gpu (1 s)'))
        self.assertEqual(len(self.recorded()), 2)
        env = [a[len('--setenv='):] for a in self.recorded()[0]['argv'] if a.startswith('--setenv=')]
        self.assertIn('VOICE_TRANSCRIBE_METHOD=auto', env)
        self.assertIn(f'VOICE_TRANSCRIBE_GPU_CHECK={b.GPU_CHECK}', env)

    def test_a_method_auto_check_that_says_no_is_a_skip_to_try_later(self):
        settings = self.settings('enabled = true\nmethod = "auto"\n')
        for device in ('cpu: busy', 'cpu: vram-low', 'cpu: operator-busy'):
            with self.subTest(device=device):
                self.assertEqual(self.probe(f'{RUNNING}\n{{"device": "{device}", "seconds": 30}}\n',
                                            settings=settings),
                                 ('skip', f'gpu probe: GPU not free now ({device}), try later'))
        for device in ('cpu: gpu-failed', 'cpu: check-error'):
            with self.subTest(device=device):
                self.assertEqual(self.probe(f'{RUNNING}\n{{"device": "{device}", "seconds": 30}}\n',
                                            settings=settings),
                                 ('FAIL', f'gpu probe: device {device}'))

    def test_a_missing_writable_path_of_the_unit_fails_before_running(self):
        (self.root / 'state').rmdir()
        self.assertEqual(self.probe(), ('FAIL', f'gpu probe: {self.root / "state"} is missing '
                                                 '(the bridge unit writes it; run install.sh)'))
        self.assertEqual(self.recorded(), [])

    def test_the_transient_unit_runs_the_pack_helper_with_the_gpu_on(self):
        settings = self.settings()
        self.probe('{"device": "gpu", "seconds": 1}\n', settings=settings)
        argv = self.recorded()[0]['argv']
        self.assertEqual(argv[:5], ['--user', '--wait', '--pipe', '--collect', '--quiet'])
        self.assertEqual(argv[argv.index('--'):], ['--', sys.executable, str(b.GPU_PROBE), 'large-v3-turbo-q5_0'])
        env = [a[len('--setenv='):] for a in argv if a.startswith('--setenv=')]
        self.assertIn('VOICE_TRANSCRIBE_GPU=1', env)
        self.assertIn('TMPDIR=/tmp', env)
        self.assertIn(f'VOICE_TRANSCRIBE_WHISPER_CLI={self.cli}', env)
        self.assertEqual(sorted(e.split('=', 1)[0] for e in env),
                         sorted(['VOICE_TRANSCRIBE_GPU', 'TMPDIR', *settings.transcribe['env']]))
        self.assertNotIn('VOICE_TRANSCRIBE_COMFYUI_FREE=1', env)

    def test_the_probe_follows_comfyui_free_like_a_note(self):
        settings = self.settings(transcribe='enabled = true\ngpu = true\ncomfyui_free = true\n')
        self.probe('{"device": "gpu", "seconds": 1}\n', settings=settings)
        env = [a[len('--setenv='):] for a in self.recorded()[0]['argv'] if a.startswith('--setenv=')]
        self.assertIn('VOICE_TRANSCRIBE_COMFYUI_FREE=1', env)

    def test_the_probe_carries_exactly_the_rendered_units_sandbox(self):
        settings = self.settings()
        self.probe('{"device": "gpu", "seconds": 1}\n', settings=settings)
        argv = self.recorded()[0]['argv']
        given = [a[len('--property='):] for a in argv if a.startswith('--property=')]
        service = b.render_unit(settings).split('[Service]\n', 1)[1].split('\n[', 1)[0]
        rendered = [line for line in service.splitlines()
                    if line and line.split('=', 1)[0] not in b.UNIT_PROCESS_KEYS]
        self.assertEqual(given, rendered)
        for line in ('PrivateTmp=true', 'ProtectSystem=strict', 'ProtectHome=read-only', 'NoNewPrivileges=true',
                     'UMask=0077', f'ReadWritePaths={self.root / "state"} {self.root / "private"} '
                                   f'{self.home / b.QUEUE_SUBDIR}'):
            self.assertIn(line, given)

    def test_handler_write_paths_reach_the_probe_as_the_unit_renders_them(self):
        out = self.root / 'out'
        out.mkdir()
        settings = self.settings(handler_write_paths=f'["{out}"]')
        self.probe('{"device": "gpu", "seconds": 1}\n', settings=settings)
        given = [a[len('--property='):] for a in self.recorded()[0]['argv'] if a.startswith('--property=')]
        self.assertIn(f'ReadWritePaths={self.root / "state"} {self.root / "private"} '
                      f'{self.home / b.QUEUE_SUBDIR} {out}', given)
        out.rmdir()
        self.assertEqual(self.probe(settings=settings),
                         ('FAIL', f'gpu probe: {out} is missing (the bridge unit writes it; run install.sh)'))
        self.assertEqual(len(self.recorded()), 1)

    def test_a_key_the_unit_template_gains_reaches_the_probe_and_a_lost_private_tmp_refuses(self):
        settings = self.settings()
        template = self.root / 'unit.in'
        template.write_text(b.UNIT_TEMPLATE.read_text().replace('PrivateTmp=true\n',
                                                                'PrivateTmp=true\nPrivateDevices=false\n'))
        with patch.object(b, 'UNIT_TEMPLATE', template):
            self.assertIn('PrivateDevices=false', b.unit_sandbox_properties(settings))
        template.write_text(b.UNIT_TEMPLATE.read_text().replace('PrivateTmp=true\n', ''))
        with patch.object(b, 'UNIT_TEMPLATE', template):
            self.assertEqual(self.probe(settings=settings), ('FAIL', 'gpu probe: the bridge unit could not be rendered'))
        self.assertEqual(self.recorded(), [])


class CheckInstallFlagTests(ProbeFixture):
    """main(): the probe runs only with --gpu-probe and only a FAIL fails."""

    def run_main(self, *flags, stdout=''):
        settings = self.settings()
        argv = ['bridge.py', '--settings', str(settings.path), 'check-install', *flags]
        umask = os.umask(0o077)
        os.umask(umask)
        self.addCleanup(os.umask, umask)  # main() narrows the process umask
        with patch.object(b, 'install_problems', return_value=[(True, 'python')]), \
             patch.dict(os.environ, FAKE_PROBE_STDOUT=stdout), patch('sys.argv', argv), \
             patch('sys.stdout', new_callable=io.StringIO) as out:
            try:
                b.main()
                return 0, out.getvalue()
            except b.TransportError as error:
                return str(error), out.getvalue()

    def test_without_the_flag_nothing_runs(self):
        self.assertEqual(self.run_main(), (0, 'ok    python\n'))
        self.assertEqual(self.recorded(), [])

    def test_with_the_flag_the_line_is_printed_and_only_a_failure_fails(self):
        self.assertEqual(self.run_main('--gpu-probe', stdout='{"device": "gpu", "seconds": 2.4}\n'),
                         (0, 'ok    python\nok    gpu probe: device gpu (2.4 s)\n'))
        self.assertEqual(self.run_main('--gpu-probe', stdout='{"device": "cpu: comfy-busy", "seconds": 9}\n'),
                         (0, 'ok    python\nskip  gpu probe: ComfyUI busy or card full (comfy-busy), try later\n'))
        self.assertEqual(self.run_main('--gpu-probe', '--clip', str(self.jfk),
                                       stdout='{"device": "cpu: gpu-failed", "seconds": 9}\n'),
                         ('1 install check(s) failed', 'ok    python\nFAIL  gpu probe: device cpu: gpu-failed\n'))
        self.assertEqual(len(self.recorded()), 3)
        rw = [a for a in self.recorded()[0]['argv'] if a.startswith('--property=ReadWritePaths=')]
        self.assertEqual(rw, [f'--property=ReadWritePaths={self.root / "state"} {self.root / "private"} '
                              f'{self.home / b.QUEUE_SUBDIR}'])  # the fixture's queue, never the real home's

    def test_the_flags_belong_to_check_install(self):
        parser = b._build_parser()
        for argv in (['check-install', '--clip', '/x.wav'], ['send', '--gpu-probe'], ['probe', '--gpu-probe']):
            with self.assertRaises(SystemExit), patch('sys.stderr', new_callable=io.StringIO):
                b._validate_attachment_flags(parser, parser.parse_args(argv))
        b._validate_attachment_flags(parser, parser.parse_args(['check-install', '--gpu-probe', '--clip', '/x.wav']))


class ProbeHelperTests(unittest.TestCase):
    """transcriber/gpu_probe.py, the side inside the unit, with a fake transcriber."""

    def setUp(self):
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.object(helper.tempfile, 'tempdir', str(self.tmp)))
        self.enterContext(patch.dict(os.environ, VOICE_TRANSCRIBE_GPU='1'))
        self.seen = {}

    def run_helper(self, transcribe, clip=CLIP):
        ticks = iter([100.0, 102.44])
        with patch('sys.stdout', new_callable=io.StringIO) as out:
            code = helper.main(['gpu_probe.py', 'large-v3-turbo-q5_0'], stdin=io.BytesIO(clip),
                               transcribe=transcribe, clock=lambda: next(ticks))
        return code, out.getvalue().splitlines()

    def fake(self, label, error=None):
        def transcribe(path, options, on_turn=None, on_device=None):
            self.seen.update(path=path, data=Path(path).read_bytes(), gpu=options.gpu, model=options.model)
            on_turn()
            on_device(label)
            if error:
                raise helper.voice_transcribe.TranscribeError(error)
            return 'ask not what your country can do for you'
        return transcribe

    def test_prints_the_device_and_seconds_never_the_text(self):
        code, lines = self.run_helper(self.fake('gpu'))
        self.assertEqual((code, lines), (0, [RUNNING, '{"seconds": 2.4, "device": "gpu"}']))
        self.assertEqual((self.seen['data'], self.seen['gpu'], self.seen['model']), (CLIP, True, 'large-v3-turbo-q5_0'))
        self.assertTrue(self.seen['path'].startswith(str(self.tmp) + '/'))
        self.assertEqual(list(self.tmp.iterdir()), [])  # the clip copy is gone

    def test_a_failure_keeps_its_code_and_device(self):
        code, lines = self.run_helper(self.fake('cpu: gpu-failed', error='whisper_failed'))
        self.assertEqual(json.loads(lines[-1]), {'error': 'whisper_failed', 'device': 'cpu: gpu-failed', 'seconds': 2.4})
        self.assertNotIn('country', ''.join(lines))

    def test_an_empty_or_oversize_clip_is_refused_before_transcribing(self):
        never = lambda *a, **k: self.fail('transcribed')  # noqa: E731
        self.assertEqual(self.run_helper(never, clip=b''), (0, ['{"error": "clip_unreadable"}']))
        self.assertIsNone(helper.read_clip(io.BytesIO(b'x' * 11), limit=10))
        self.assertEqual(helper.read_clip(io.BytesIO(b'x' * 10), limit=10), b'x' * 10)


if __name__ == '__main__':
    unittest.main()
