"""Per-city settings: the loader, the default lookup, the unit template and the
install checks. Temp files only; nothing reads the host's real settings."""
import hashlib
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('bridge', Path(__file__).with_name('bridge.py'))
b = importlib.util.module_from_spec(spec)
spec.loader.exec_module(b)
vt_spec = importlib.util.spec_from_file_location(
    'voice_transcribe', Path(__file__).resolve().parents[1] / 'transcriber' / 'voice_transcribe.py')
vt = importlib.util.module_from_spec(vt_spec)
vt_spec.loader.exec_module(vt)
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_bridge import MENU, write_settings  # noqa: E402

PACK = Path(__file__).resolve().parents[1]


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def refused(self, pattern, **replace):
        with self.assertRaisesRegex(b.SettingsError, pattern):
            b.load_settings(write_settings(self.root, **replace))

    def test_valid_settings_give_the_bridge_config_and_paths(self):
        settings = b.load_settings(write_settings(self.root))
        self.assertEqual(settings.city, 'test-city')
        self.assertEqual(settings.state_dir, self.root / 'state')
        self.assertEqual(settings.port, 18931)
        self.assertEqual(settings.lock_dir, self.root / 'private')  # the token file's directory
        self.assertEqual(settings.unit, 'test-city-telegram.service')
        self.assertEqual(settings.pairing_update_id, 5)
        self.assertIsNone(settings.transcribe)  # off unless enabled
        self.assertEqual(settings.config, dict(
            city='test-city', account_id='test_bot', gc_base_url='http://127.0.0.1:8372', user_display_name='Pat',
            timezone='Asia/Karachi', menu=MENU, unit='test-city-telegram.service',
            bot_id=999, user_id=123, chat_id=123))
        bridge = b.Bridge(settings.config, None, 'SECRET', None, state_dir=settings.state_dir)
        self.assertEqual(bridge.conversation, dict(scope_id='test-city', provider='telegram', account_id='test_bot',
                                                   conversation_id='123', kind='dm'))

    def test_optional_files_reach_the_config(self):
        path = write_settings(self.root, health_file='"/srv/city/.runtime/city-health.json"',
                              gc_api_token_file='"~/.config/test-city/gc-token"')
        config = b.load_settings(path).config
        self.assertEqual(config['health_file'], '/srv/city/.runtime/city-health.json')
        self.assertEqual(config['gc_api_token_path'], str(Path.home() / '.config/test-city/gc-token'))

    def test_a_leftover_quota_table_refuses_with_the_migration_line(self):
        line = 'settings: [quota] is no longer read; /quota is a [[command]] add-on (README.md, "Add-ons")'
        for table in ('\n[quota]\nenabled = true\n', '\n[quota]\nenabled = false\n', '\n[quota]\n'):
            with self.subTest(table=table):
                path = write_settings(self.root)
                path.write_text(path.read_text() + table)
                with self.assertRaises(b.SettingsError) as caught:
                    b.load_settings(path)
                self.assertEqual(str(caught.exception), line)

    def test_quota_loads_as_an_ordinary_command(self):
        handler = self.root / 'quota-handler'
        handler.write_text('#!/bin/sh\n')
        handler.chmod(0o700)
        entry = f'\n[[command]]\nname = "quota"\ndescription = "Quota"\nhandler = "{handler}"\n'
        base = write_settings(self.root).read_text().split('\n[[menu]]')[0]
        settings = b.load_settings(write_settings(self.root, body=base + entry))
        self.assertEqual([c['name'] for c in settings.config['commands']], ['quota'])
        menu = '\n[[menu]]\ncommand = "quota"\ndescription = "Quota"\n'
        with self.assertRaisesRegex(b.SettingsError, r'command entry 1 \(quota\) clashes with /quota'):
            b.load_settings(write_settings(self.root, body=base + menu + entry))
        start = entry.replace('name = "quota"', 'name = "start"')
        with self.assertRaisesRegex(b.SettingsError, r'command entry 1 \(start\) clashes with /start'):
            b.load_settings(write_settings(self.root, body=base + start))

    def test_missing_settings_refuse_with_the_path(self):
        with self.assertRaisesRegex(b.SettingsError, 'settings file not found: .*nowhere.toml'):
            b.load_settings(self.root / 'nowhere.toml')

    def test_a_settings_file_others_can_read_is_refused(self):
        with self.assertRaisesRegex(b.SettingsError, 'owner-only regular file'):
            b.load_settings(write_settings(self.root, mode=0o644))

    def test_invalid_settings_name_the_first_problem(self):
        cases = [
            ('not valid TOML', dict(body='city = \n')),
            ('unknown key language', dict(language='"ru"')),
            ('city is missing or malformed', dict(city=None)),
            ('city is missing or malformed', dict(city='"two words"')),
            ('state_dir must be a path', dict(state_dir=None)),
            ('state_dir must be an absolute', dict(state_dir='"relative/state"')),
            ('state_dir must be an absolute', dict(state_dir='"/srv/a b"')),
            ('state_dir must be an absolute', dict(state_dir='"/srv/%h/state"')),
            ('state_dir must be an absolute', dict(state_dir='"/srv/../etc"')),
            ('port must be an integer from 1024 to 65535', dict(port='80')),
            ('port must be an integer', dict(port='"8931"')),
            ('token_file must be a path', dict(token_file=None)),
            ('timezone is not a known time zone', dict(timezone='"Mars/Olympus"')),
            ('unit is missing or malformed', dict(unit='"bridge; rm"')),
            ('pairing_update_id must be an integer', dict(pairing_update_id='-2')),
        ]
        for pattern, replace in cases:
            with self.subTest(pattern=pattern, replace=replace):
                self.refused(pattern, **replace)

    def test_token_and_callback_secret_must_be_different_files(self):
        token = self.root / 'private' / 'telegram-bot-token'
        secret = self.root / 'private' / 'telegram-callback-secret'
        pattern = 'token_file and callback_secret_file must name different files'
        self.refused(pattern, callback_secret_file=f'"{token}"')  # the same path
        write_settings(self.root)  # creates the token file
        secret.symlink_to(token)
        self.refused(pattern)  # an alias of the token file
        secret.unlink()
        os.link(token, secret)
        self.refused(pattern)  # a hard link to it
        secret.unlink()
        secret.write_text('SECRET\n')
        settings = b.load_settings(write_settings(self.root))  # two distinct files
        self.assertNotEqual(settings.token_file, settings.callback_secret_file)

    def test_bot_ids_must_be_a_private_pairing(self):
        base = write_settings(self.root).read_text()
        for old, new, pattern in (('chat_id = 123', 'chat_id = 124', 'chat_id must equal bot.user_id'),
                                  ('bot_id = 999', 'bot_id = 0', 'bot.bot_id must be an integer'),
                                  ('user_id = 123', 'user_id = "123"', 'bot.user_id must be an integer'),
                                  ('account_id = "test_bot"\n', 'account_id = "test_bot"\nextra = 1\n', 'unknown key bot.extra'),
                                  ('[bot]', '[robot]', 'unknown key robot')):
            with self.subTest(new):
                path = write_settings(self.root, body=base.replace(old, new))
                with self.assertRaisesRegex(b.SettingsError, pattern):
                    b.load_settings(path)

    def test_menu_entries_are_checked(self):
        base = write_settings(self.root).read_text()
        for old, new, pattern in (('command = "quota"', 'command = "Quota!"', 'menu entry 1 command'),
                                  ('command = "status"\n', 'command = "status"\nhidden = true\n', 'menu entry 2 needs exactly')):
            with self.subTest(new):
                with self.assertRaisesRegex(b.SettingsError, pattern):
                    b.load_settings(write_settings(self.root, body=base.replace(old, new)))
        settings = b.load_settings(write_settings(self.root, body=base.split('\n[[menu]]')[0]))
        bridge = b.Bridge(settings.config, None, 'SECRET', None)
        with self.assertRaisesRegex(b.TransportError, 'no command menu'):
            bridge.set_commands()

    def test_a_second_city_on_the_same_host_gets_its_own_everything(self):
        first = b.load_settings(write_settings(self.root))
        other_root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        second = b.load_settings(write_settings(other_root, city='"other-city"', port='18932',
                                                user_display_name='"Sam"', timezone='"UTC"'))
        self.assertEqual(second.unit, 'other-city-telegram.service')
        for field in ('city', 'state_dir', 'port', 'token_file', 'callback_secret_file', 'lock_dir', 'unit'):
            self.assertNotEqual(getattr(first, field), getattr(second, field), field)
        bridges = [b.Bridge(s.config, None, 'SECRET', None) for s in (first, second)]
        self.assertEqual([x.conversation['scope_id'] for x in bridges], ['test-city', 'other-city'])
        self.assertEqual([x.display_name for x in bridges], ['Pat', 'Sam'])


class TranscribeSettingsTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def load(self, table):
        path = write_settings(self.root)
        path.write_text(path.read_text() + '\n[transcribe]\n' + table)
        return b.load_settings(path)

    def test_enabled_transcription_uses_the_pack_adapter_with_neutral_defaults(self):
        settings = self.load('enabled = true\n')
        home = Path.home()
        self.assertEqual(settings.transcribe, dict(
            python='/usr/bin/python3', script=str(PACK / 'transcriber' / 'adapter.py'), model='large-v3-turbo-q5_0',
            timeout=120, timeout_per_minute=120, gpu=False, method='cpu', env={
                'VOICE_TRANSCRIBE_WHISPER_CLI': str(home / 'work/whisper.cpp/build/bin/whisper-cli'),
                'VOICE_TRANSCRIBE_MODEL_DIR': str(home / 'work/whisper.cpp/models'),
                'VOICE_TRANSCRIBE_VAD_MODEL': 'silero-v6.2.0',
                'VOICE_TRANSCRIBE_THREADS': '4',
                'VOICE_TRANSCRIBE_CPUS': '',
                'VOICE_TRANSCRIBE_TIMEOUT': '120',
                'VOICE_TRANSCRIBE_TIMEOUT_PER_MINUTE': '120',
                'VOICE_TRANSCRIBE_GPU_MIN_FREE_MB': '2048',
                'VOICE_TRANSCRIBE_METHOD': 'cpu',
            }))
        self.assertTrue(Path(settings.transcribe['script']).is_file())
        self.assertEqual(settings.queue_dir, home / '.local/state/voice-transcribe')
        self.assertEqual(self.load('enabled = true\ngpu = false\n').transcribe['env'],
                         settings.transcribe['env'])

    def test_every_city_gets_the_one_host_wide_queue(self):
        # Two cities' settings, each with its own state, port and transcriber
        # paths, still name one queue: there is no per-city queue setting, and
        # the transcriber derives the same directory on its own.
        home = self.root / 'home'
        second = self.root / 'second'
        second.mkdir()
        cities = []
        for root, table in ((self.root, 'enabled = true\n'),
                            (second, 'enabled = true\nwhisper_cli = "/opt/w/whisper-cli"\nmodel = "small.en"\n')):
            path = write_settings(root, city='"second-city"' if root == second else '"test-city"',
                                  port='18932' if root == second else '18931')
            path.write_text(path.read_text() + '\n[transcribe]\n' + table)
            cities.append(b.load_settings(path, home=home))
        self.assertNotEqual(cities[0].state_dir, cities[1].state_dir)
        self.assertEqual({c.queue_dir for c in cities}, {home / '.local/state/voice-transcribe'})
        self.assertEqual(b.QUEUE_SUBDIR, vt.QUEUE_SUBDIR)
        for city in cities:
            self.assertNotIn('VOICE_TRANSCRIBE_QUEUE_DIR', city.transcribe['env'])
            self.assertEqual(vt.Options(environ=city.transcribe['env']).queue_dir,
                             Path.home() / '.local/state/voice-transcribe')

    def test_paths_and_limits_come_from_the_settings(self):
        settings = self.load('enabled = true\nwhisper_cli = "/opt/w/whisper-cli"\nmodels_dir = "/opt/w/models"\n'
                             'model = "small.en"\nthreads = 2\ncpus = "2-7,10-15"\ntimeout = 60\ntimeout_per_minute = 90\n')
        self.assertEqual(settings.transcribe['model'], 'small.en')
        self.assertEqual(settings.transcribe['timeout'], 60)
        self.assertEqual(settings.transcribe['timeout_per_minute'], 90)
        self.assertEqual(settings.transcribe['env']['VOICE_TRANSCRIBE_CPUS'], '2-7,10-15')
        self.assertEqual(settings.transcribe['env']['VOICE_TRANSCRIBE_WHISPER_CLI'], '/opt/w/whisper-cli')
        self.assertEqual(settings.transcribe['env']['VOICE_TRANSCRIBE_MODEL_DIR'], '/opt/w/models')
        self.assertEqual(settings.transcribe['env']['VOICE_TRANSCRIBE_THREADS'], '2')
        self.assertEqual(settings.transcribe['env']['VOICE_TRANSCRIBE_TIMEOUT'], '60')
        self.assertEqual(settings.transcribe['env']['VOICE_TRANSCRIBE_TIMEOUT_PER_MINUTE'], '90')

    def test_legacy_gpu_true_behaves_as_method_auto(self):
        settings = self.load('enabled = true\ngpu = true\ngpu_min_free_mb = 3072\n')
        default_check = str(PACK / 'transcriber' / 'gpu_check.py')
        self.assertIs(settings.transcribe['gpu'], True)
        self.assertEqual(settings.transcribe['method'], 'auto')
        self.assertEqual(settings.transcribe['gpu_check'], default_check)
        self.assertEqual(settings.transcribe['env']['VOICE_TRANSCRIBE_METHOD'], 'auto')
        self.assertEqual(settings.transcribe['env']['VOICE_TRANSCRIBE_GPU_CHECK'], default_check)
        self.assertEqual(settings.transcribe['env']['VOICE_TRANSCRIBE_GPU_MIN_FREE_MB'], '3072')
        options = vt.Options(environ=settings.transcribe['env'])
        self.assertEqual((options.method, options.gpu, options.gpu_min_free_mb), ('auto', True, 3072))
        # A configured check with legacy gpu = true loads, as method auto does.
        check = self.check_file()
        configured = self.load(f'enabled = true\ngpu = true\ngpu_check = "{check}"\n')
        self.assertEqual(configured.transcribe['env']['VOICE_TRANSCRIBE_GPU_CHECK'], str(check))

    def test_each_removed_key_refuses_with_the_migration_line(self):
        removed_url = 'comf' 'yui_url'
        removed_free = 'comf' 'yui_free'
        line = ('settings: transcribe.' + removed_url + ' and ' + removed_free + ' are no longer read; '
                'a city that shares the GPU sets method = "auto" and its own gpu_check '
                '(OPERATIONS.md, "The GPU check")')
        for table in (f'enabled = true\n{removed_url} = "http://127.0.0.1:8188"\n',
                      f'enabled = true\n{removed_free} = true\n',
                      f'enabled = true\ngpu = true\n{removed_free} = false\n',
                      f'enabled = true\nmethod = "auto"\n{removed_url} = "http://127.0.0.1:8188"\n',
                      f'enabled = false\n{removed_free} = true\n'):
            with self.subTest(table=table):
                with self.assertRaises(b.SettingsError) as caught:
                    self.load(table)
                self.assertEqual(str(caught.exception), line)

    def test_there_is_no_language_setting_and_bad_values_refuse(self):
        for table, pattern in (('language = "ru"\n', 'unknown key transcribe.language'),
                               ('device = "cuda"\n', 'unknown key transcribe.device'),
                               ('enabled = "yes"\n', 'transcribe.enabled must be true or false'),
                               ('timeout = 301\n', 'transcribe.timeout must be an integer from 5 to 300'),
                               ('threads = 5\n', 'transcribe.threads must be an integer from 1 to 4'),
                               ('queue_dir = "/var/tmp/q"\n', 'unknown key transcribe.queue_dir'),
                               ('cpus = "0-3; rm"\n', 'transcribe.cpus must be a CPU list'),
                               ('cpus = [2, 3]\n', 'transcribe.cpus must be a CPU list'),
                               ('timeout_per_minute = -1\n', 'transcribe.timeout_per_minute must be an integer'),
                               ('model = "a b;rm"\n', 'transcribe.model is missing or malformed'),
                               ('whisper_cli = "whisper-cli"\n', 'transcribe.whisper_cli must be an absolute'),
                               ('gpu = "yes"\n', 'transcribe.gpu must be true or false'),
                               ('gpu = 1\n', 'transcribe.gpu must be true or false'),
                               ('gpu_min_free_mb = 100\n', 'transcribe.gpu_min_free_mb must be an integer from 256'),
                               ('gpu_min_free_mb = "2048"\n', 'transcribe.gpu_min_free_mb must be an integer'),
                               ('device = "cuda"\n', 'unknown key transcribe.device')):
            with self.subTest(table):
                with self.assertRaisesRegex(b.SettingsError, pattern):
                    self.load(table)

    def test_the_timeout_grows_with_the_audio_length(self):
        settings = self.load('enabled = true\ntimeout = 60\ntimeout_per_minute = 90\n')
        bridge = b.Bridge(settings.config, None, 'SECRET', None, transcribe=settings.transcribe)
        self.assertEqual(bridge._transcribe_timeout(0), 60)
        self.assertEqual(bridge._transcribe_timeout(20), 90)
        self.assertEqual(bridge._transcribe_timeout(150), 285)  # a 2:30 note
        self.assertEqual(bridge._transcribe_timeout(None), 60 + 900)  # no duration: assume 10 minutes
        self.assertEqual(bridge._transcribe_timeout(3 * 3600), 60 + 90 * 180)  # no cap: it scales
        self.assertEqual([bridge._duration_seconds(d) for d in ('2:30', '0:07', '?:??', '1:75')], [150, 7, None, None])

    def test_gpu_on_adds_the_transcribers_gpu_allowance(self):
        settings = self.load('enabled = true\ntimeout = 60\ntimeout_per_minute = 90\n')
        bridge = b.Bridge(settings.config, None, 'SECRET', None, transcribe=settings.transcribe)
        self.assertEqual(bridge._transcribe_gpu_allowance(150), 0)  # gpu off: nothing extra
        settings = self.load('enabled = true\ntimeout = 60\ntimeout_per_minute = 90\ngpu = true\n')
        bridge = b.Bridge(settings.config, None, 'SECRET', None, transcribe=settings.transcribe)
        options = vt.Options(environ=settings.transcribe['env'], gpu=True)
        for seconds in (0, 20, 150, 3 * 3600):
            with self.subTest(seconds=seconds):
                self.assertEqual(bridge._transcribe_gpu_allowance(seconds), options.gpu_run_limit(seconds))
        self.assertEqual(bridge._transcribe_gpu_allowance(0), 30)  # the floor
        self.assertEqual(bridge._transcribe_gpu_allowance(None), 240)  # (60 + 900) / 4

    def check_file(self, name='gpu-check', mode=0o755):
        path = self.root / name
        path.write_text('#!/bin/sh\n')
        path.chmod(mode)
        return path

    def test_each_method_loads(self):
        cpu = self.load('enabled = true\nmethod = "cpu"\n')
        self.assertEqual(cpu.transcribe['method'], 'cpu')
        self.assertIs(cpu.transcribe['gpu'], False)
        self.assertEqual(cpu.transcribe['env']['VOICE_TRANSCRIBE_METHOD'], 'cpu')
        self.assertNotIn('VOICE_TRANSCRIBE_GPU_CHECK', cpu.transcribe['env'])
        self.assertNotIn('gpu_check', cpu.transcribe)
        gpu = self.load('enabled = true\nmethod = "gpu"\n')
        self.assertEqual(gpu.transcribe['method'], 'gpu')
        self.assertEqual(gpu.transcribe['env']['VOICE_TRANSCRIBE_METHOD'], 'gpu')
        self.assertNotIn('VOICE_TRANSCRIBE_GPU_CHECK', gpu.transcribe['env'])
        auto = self.load('enabled = true\nmethod = "auto"\n')
        default_check = str(PACK / 'transcriber' / 'gpu_check.py')
        self.assertEqual(auto.transcribe['method'], 'auto')
        self.assertEqual(auto.transcribe['gpu_check'], default_check)
        self.assertEqual(auto.transcribe['env']['VOICE_TRANSCRIBE_METHOD'], 'auto')
        self.assertEqual(auto.transcribe['env']['VOICE_TRANSCRIBE_GPU_CHECK'], default_check)
        self.assertTrue(os.access(default_check, os.X_OK))

    def test_method_auto_uses_a_configured_safe_gpu_check(self):
        check = self.check_file()
        settings = self.load(f'enabled = true\nmethod = "auto"\ngpu_check = "{check}"\n')
        self.assertEqual(settings.transcribe['gpu_check'], str(check))
        self.assertEqual(settings.transcribe['env']['VOICE_TRANSCRIBE_GPU_CHECK'], str(check))
        options = vt.Options(environ=settings.transcribe['env'])
        self.assertEqual((options.method, options.gpu, options.gpu_check), ('auto', True, str(check)))

    def test_method_and_legacy_gpu_keys_refuse_together(self):
        for table, pattern in (
                ('enabled = true\nmethod = "cpu"\ngpu = false\n', 'set one of transcribe.method and transcribe.gpu'),
                ('enabled = true\nmethod = "gpu"\ngpu = true\n', 'set one of transcribe.method and transcribe.gpu'),
                ('enabled = true\nmethod = "fast"\n', 'transcribe.method must be one of'),
                ('enabled = true\nmethod = 1\n', 'transcribe.method must be one of')):
            with self.subTest(table=table):
                with self.assertRaisesRegex(b.SettingsError, pattern):
                    self.load(table)

    def test_gpu_check_needs_method_auto_and_a_safe_executable(self):
        check = self.check_file()
        for table in ('enabled = true\ngpu_check = "%s"\n',
                      'enabled = true\nmethod = "cpu"\ngpu_check = "%s"\n',
                      'enabled = true\nmethod = "gpu"\ngpu_check = "%s"\n'):
            with self.subTest(table=table):
                with self.assertRaisesRegex(b.SettingsError, 'transcribe.gpu_check needs method = "auto"'):
                    self.load(table % check)
        for bad, mode in ((self.root / 'absent-check', None), (self.check_file('not-executable', 0o644), None),
                          (self.check_file('group-writable', 0o775), None)):
            with self.subTest(bad=bad):
                with self.assertRaisesRegex(b.SettingsError, 'transcribe.gpu_check'):
                    self.load(f'enabled = true\nmethod = "auto"\ngpu_check = "{bad}"\n')
        with self.assertRaisesRegex(b.SettingsError, 'transcribe.gpu_check'):
            self.load('enabled = true\nmethod = "auto"\ngpu_check = "relative/check"\n')

    def test_method_gpu_and_auto_get_the_gpu_allowance_and_cpu_does_not(self):
        for method, allowance in (('cpu', 0), ('gpu', 30), ('auto', 30)):
            with self.subTest(method=method):
                settings = self.load(f'enabled = true\nmethod = "{method}"\ntimeout = 60\ntimeout_per_minute = 90\n')
                bridge = b.Bridge(settings.config, None, 'SECRET', None, transcribe=settings.transcribe)
                self.assertEqual(bridge._transcribe_gpu_allowance(0), allowance)

    def test_disabled_transcription_still_validates_its_table(self):
        self.assertIsNone(self.load('enabled = false\nmodel = "small"\n').transcribe)


class SettingsLookupTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def test_explicit_then_environment_then_the_city_workspace_name(self):
        city = self.root / 'city'
        (city / '.gc').mkdir(parents=True)
        (city / '.gc' / 'site.toml').write_text('workspace_name = "some-city"\n')
        home = self.root / 'home'
        self.assertEqual(b.settings_path('/x/given.toml', city, environ={b.SETTINGS_ENV: '/x/env.toml'}, home=home),
                         Path('/x/given.toml'))
        self.assertEqual(b.settings_path(None, city, environ={b.SETTINGS_ENV: '/x/env.toml'}, home=home),
                         Path('/x/env.toml'))
        self.assertEqual(b.settings_path(None, city, environ={}, home=home),
                         home / '.config' / 'some-city' / 'telegram-bridge.toml')

    def test_no_way_to_find_settings_refuses(self):
        city = self.root / 'city'
        (city / '.gc').mkdir(parents=True)
        for site in (None, 'workspace_name = "../escape"\n', 'not toml [', 'other = 1\n'):
            with self.subTest(site=site):
                if site is not None:
                    (city / '.gc' / 'site.toml').write_text(site)
                with self.assertRaisesRegex(b.SettingsError, 'no settings file: pass --settings'):
                    b.settings_path(None, city, environ={}, home=self.root)

    def test_cli_refuses_to_start_without_settings_and_says_why(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith('GC_') and k != b.SETTINGS_ENV}
        env['HOME'] = str(self.root)
        result = subprocess.run([sys.executable, str(PACK / 'bridge' / 'bridge.py'), '--city-root', str(self.root), 'run'],
                                env=env, capture_output=True, text=True, timeout=30, cwd=self.root)
        self.assertEqual(result.returncode, 1)
        self.assertIn('no settings file: pass --settings', result.stderr)
        missing = self.root / 'missing.toml'
        result = subprocess.run([sys.executable, str(PACK / 'bridge' / 'bridge.py'), '--settings', str(missing), 'run'],
                                env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual((result.returncode, result.stderr.strip()), (1, f'settings file not found: {missing}'))
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), [])  # nothing created


class UnitTemplateTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def test_renders_the_city_unit_with_its_own_paths(self):
        path = write_settings(self.root)
        settings = b.load_settings(path)
        unit = b.render_unit(settings, bridge_path='/srv/pack/bridge/bridge.py')
        state, private = self.root / 'state', self.root / 'private'
        self.assertIn('Description=Private Telegram transport for the test-city mayor\n', unit)
        self.assertIn(f'WorkingDirectory={state}\n', unit)
        self.assertIn(f'ExecStart=/usr/bin/python3 /srv/pack/bridge/bridge.py --settings {path} run\n', unit)
        self.assertIn(f'ReadWritePaths={state} {private}\n', unit)  # transcription off: no queue
        for line in ('Restart=on-failure', 'RestartPreventExitStatus=2', 'UMask=0077', 'NoNewPrivileges=true',
                     'PrivateTmp=true', 'ProtectSystem=strict', 'ProtectHome=read-only', 'StartLimitIntervalSec=0'):
            self.assertIn(line + '\n', unit)
        self.assertNotRegex(unit, r'@[A-Z_]+@')

    def test_with_transcription_the_unit_may_write_the_host_wide_queue(self):
        path = write_settings(self.root)
        path.write_text(path.read_text() + '\n[transcribe]\nenabled = true\n')
        unit = b.render_unit(b.load_settings(path, home='/home/pat'), bridge_path='/srv/bridge.py')
        self.assertIn(f'ReadWritePaths={self.root / "state"} {self.root / "private"} '
                      '/home/pat/.local/state/voice-transcribe\n', unit)

    def test_the_cli_prints_the_same_unit(self):
        path = write_settings(self.root)
        result = subprocess.run([sys.executable, str(PACK / 'bridge' / 'bridge.py'), '--settings', str(path), 'render-unit'],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, b.render_unit(b.load_settings(path), bridge_path=PACK / 'bridge' / 'bridge.py'))

    def test_an_unsafe_bridge_path_or_placeholder_refuses(self):
        settings = b.load_settings(write_settings(self.root))
        with self.assertRaisesRegex(b.SettingsError, 'bridge path must be an absolute'):
            b.render_unit(settings, bridge_path='/srv/my city/bridge.py')
        template = self.root / 'unit.in'
        template.write_text('ExecStart=@PYTHON@ @UNKNOWN@\n')
        with self.assertRaisesRegex(b.SettingsError, 'unknown placeholder'):
            b.render_unit(settings, template=template, bridge_path='/srv/bridge.py')


class InstallCheckTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.models = self.root / 'models'
        self.models.mkdir()
        self.cli = self.root / 'whisper-cli'
        self.cli.write_text('#!/bin/sh\n')
        self.cli.chmod(0o755)
        path = write_settings(self.root, python=f'"{sys.executable}"')
        home = self.root / 'home'
        self.queue = home / '.local/state/voice-transcribe'
        path.write_text(path.read_text() + f'\n[transcribe]\nenabled = true\nwhisper_cli = "{self.cli}"\n'
                        f'models_dir = "{self.models}"\n')
        (self.root / 'state').mkdir()
        self.settings = b.load_settings(path, home=home)

    def failures(self, **kwargs):
        kwargs.setdefault('which', lambda name: '/usr/bin/' + name)
        return [line for ok, line in b.install_problems(self.settings, **kwargs) if not ok]

    def model(self, name, data):
        (self.models / f'ggml-{name}.bin').write_bytes(data)
        return {f'ggml-{name}.bin': hashlib.sha256(data).hexdigest()}

    def test_everything_present_and_matching_passes(self):
        sums = {**self.model('large-v3-turbo-q5_0', b'turbo'), **self.model('silero-v6.2.0', b'vad')}
        self.assertEqual(self.failures(sums=sums), [])

    def test_the_queue_directory_is_created_by_install_or_must_be_owner_only(self):
        sums = {**self.model('large-v3-turbo-q5_0', b'turbo'), **self.model('silero-v6.2.0', b'vad')}
        lines = [line for ok, line in b.install_problems(self.settings, which=lambda n: '/usr/bin/' + n, sums=sums)]
        self.assertIn(f'transcription queue {self.queue} is created by install.sh', lines)
        self.queue.mkdir(mode=0o755, parents=True)
        self.queue.chmod(0o755)
        self.assertEqual(self.failures(sums=sums), [f'transcription queue {self.queue} is an owner-only directory'])
        self.queue.chmod(0o700)
        self.assertEqual(self.failures(sums=sums), [])

    def test_missing_tools_models_and_mismatched_sums_each_fail(self):
        sums = {**self.model('large-v3-turbo-q5_0', b'turbo'), 'ggml-silero-v6.2.0.bin': '0' * 64}
        self.model('silero-v6.2.0', b'tampered')
        failed = self.failures(sums=sums, which=lambda name: None)
        self.assertEqual(failed, ['curl on PATH', 'ffmpeg on PATH', f'model {self.models}/ggml-silero-v6.2.0.bin SHA-256'])
        (self.models / 'ggml-large-v3-turbo-q5_0.bin').unlink()
        self.cli.chmod(0o644)
        failed = self.failures(sums=sums)
        self.assertIn(f'whisper-cli {self.cli}', failed)
        self.assertIn(f'model {self.models}/ggml-large-v3-turbo-q5_0.bin is missing', failed)
        self.assertIn('model ggml-small.bin: no recorded SHA-256 in the pack',
                      [line for ok, line in b.install_problems(
                          b.Settings(**dict(self.settings.__dict__, model='small')), which=lambda n: n, sums=sums)
                       if not ok])

    def test_a_world_readable_token_fails_and_is_never_printed(self):
        token = self.settings.token_file
        token.chmod(0o644)
        sums = {**self.model('large-v3-turbo-q5_0', b'turbo'), **self.model('silero-v6.2.0', b'vad')}
        results = b.install_problems(self.settings, which=lambda n: n, sums=sums)
        self.assertIn((False, 'bot token file must be an owner-only regular file'), results)
        self.assertNotIn('TESTTOKEN', repr(results))

    def test_an_unlisted_model_fails_naming_the_supported_models(self):
        sums = {**self.model('large-v3-turbo-q5_0', b'turbo'), **self.model('silero-v6.2.0', b'vad')}
        settings = b.Settings(**dict(self.settings.__dict__, model='tiny'))
        self.assertEqual(b.supported_models(settings),
                         ['large-v3-turbo-q5_0', 'small.en', 'small.en-q5_1', 'base.en', 'small'])
        self.assertEqual([line for ok, line in b.install_problems(settings, which=lambda n: '/usr/bin/' + n,
                                                                  sums=sums) if not ok],
                         ['transcribe model tiny is not one of the supported models: '
                          'large-v3-turbo-q5_0, small.en, small.en-q5_1, base.en, small'])

    def test_the_pack_records_the_handed_over_model_sums(self):
        sums = b.recorded_sums()
        self.assertEqual(sums['ggml-large-v3-turbo-q5_0.bin'],
                         '394221709cd5ad1f40c46e6031ca61bce88931e6e088c188294c6d5a55ffa7e2')
        self.assertEqual(sums['ggml-silero-v6.2.0.bin'],
                         '2aa269b785eeb53a82983a20501ddf7c1d9c48e33ab63a41391ac6c9f7fb6987')
        self.assertEqual(sums['ggml-small.bin'], '1be3a9b2063867b937e64e2ec7483364a79917e157fa98c5d94b5c1fffea987b')


class InstallScriptTests(unittest.TestCase):
    """install.sh against a fake home: the unit may write only its state and
    lock directories (and the queue), so the script itself mints a missing
    callback secret kept anywhere else before the unit's first start."""

    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.home = self.root / 'home'
        self.home.mkdir()
        self.secret = self.root / 'secrets' / 'telegram-callback-secret'  # outside state_dir and lock_dir
        self.path = write_settings(self.root, python=f'"{sys.executable}"', callback_secret_file=f'"{self.secret}"')

    def install(self, *args):
        env = {k: v for k, v in os.environ.items() if not k.startswith('GC_') and k != b.SETTINGS_ENV}
        env.update(HOME=str(self.home), XDG_CONFIG_HOME=str(self.home / '.config'))
        return subprocess.run(['sh', str(PACK / 'install.sh'), '--settings', str(self.path), *args],
                              env=env, capture_output=True, text=True, timeout=60)

    def test_install_creates_a_missing_callback_secret_the_unit_could_not(self):
        if not shutil.which('curl'):
            self.skipTest('curl not on PATH')
        dry = self.install('--dry-run')
        self.assertEqual(dry.returncode, 0, dry.stderr)
        self.assertIn(f'would create callback secret {self.secret} (mode 600)', dry.stdout)
        self.assertIn('ok    callback secret file is created by install.sh', dry.stdout)
        self.assertFalse(self.secret.parent.exists())  # a dry run writes nothing
        self.assertEqual(sorted(p.name for p in self.home.iterdir()), [])

        done = self.install()
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn(f'created callback secret {self.secret} (mode 600)', done.stdout)
        self.assertIn('ok    callback secret file is owner-only', done.stdout)
        secret = self.secret.read_text()
        self.assertGreaterEqual(len(secret), 32)
        self.assertEqual(self.secret.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.secret.parent.stat().st_mode & 0o777, 0o700)
        self.assertNotIn(secret, done.stdout + done.stderr)
        unit = (self.home / '.config/systemd/user/test-city-telegram.service').read_text()
        self.assertIn(f'ReadWritePaths={self.root / "state"} {self.root / "private"}\n', unit)

        again = self.install()  # idempotent: the secret is kept, nothing is rewritten
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertNotIn('created callback secret', again.stdout)
        self.assertIn('is current', again.stdout)
        self.assertEqual(self.secret.read_text(), secret)


class PackNeutralityTests(unittest.TestCase):
    def test_nothing_in_the_pack_names_a_particular_city_or_company(self):
        allowed = {PACK / 'README.md'}  # its examples from one real city are marked as such
        hits = []
        for path in sorted(PACK.rglob('*')):
            if not path.is_file() or path in allowed or '__pycache__' in path.parts:
                continue
            text = path.read_text(errors='replace').lower()
            # Spelled in pieces so this file does not match itself.
            for word in ('imd' + '-city', 'front' + '-apps', 'imu' + 'sician', 'nick' + '-city'):
                if word in text:
                    hits.append(f'{path.relative_to(PACK)}: {word}')
        self.assertEqual(hits, [])


if __name__ == '__main__':
    unittest.main()
