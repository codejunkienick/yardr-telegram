"""Per-yard settings: the loader, the lookup, the unit template and the
install checks. Temp files only; nothing reads the host's real settings."""
import hashlib
import importlib.util
import json
import os
import plistlib
from unittest.mock import patch
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bridge as b  # noqa: E402
from test_bridge import MENU, write_settings  # noqa: E402

vt_spec = importlib.util.spec_from_file_location(
    'voice_transcribe', Path(__file__).resolve().parents[1] / 'transcriber' / 'voice_transcribe.py')
vt = importlib.util.module_from_spec(vt_spec)
vt_spec.loader.exec_module(vt)

PACK = Path(__file__).resolve().parents[1]


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def refused(self, pattern, **replace):
        with self.assertRaisesRegex(b.SettingsError, pattern):
            b.load_settings(write_settings(self.root, **replace))

    def with_yardr(self, table):
        """The default settings with their [yardr] table's body replaced by
        `table`; None drops the table."""
        path = write_settings(self.root)
        text = path.read_text()
        start, end = text.index('\n[yardr]\n'), text.index('\n[bot]\n')
        path.write_text(text[:start] + ('' if table is None else '\n[yardr]\n' + table) + text[end:])
        return path

    def test_valid_settings_give_the_bridge_config_and_paths(self):
        settings = b.load_settings(write_settings(self.root))
        self.assertEqual(settings.yard, 'test-city')
        self.assertEqual(settings.hook_secret_file, self.root / 'private' / 'hook-secret')
        self.assertEqual(settings.state_dir, self.root / 'state')
        self.assertEqual(settings.port, 18930)
        self.assertEqual(settings.lock_dir, self.root / 'private')  # the token file's directory
        self.assertEqual(settings.unit, 'test-city-telegram.service')
        self.assertEqual(settings.pairing_update_id, 5)
        self.assertIsNone(settings.transcribe)  # off unless enabled
        # The hook secret itself is read only by the polling process (main).
        self.assertEqual(settings.config, dict(
            yard='test-city', account_id='test_bot', hook_url='http://127.0.0.1:8797/hooks/tgbridge',
            yardr_source='tgbridge', agent='yardmaster', user_display_name='Pat',
            timezone='Asia/Tokyo', menu=MENU, unit='test-city-telegram.service',
            protected_paths=(str(self.root / 'private' / 'telegram-bot-token'),
                             str(self.root / 'private' / 'telegram-callback-secret'),
                             str(self.root / 'private' / 'hook-secret'),
                             str(self.root / 'telegram-bridge.toml')),
            bot_id=999, user_id=123, chat_id=123))
        bridge = b.Bridge(settings.config, None, 'SECRET', None, state_dir=settings.state_dir)
        self.assertEqual(bridge.conversation, dict(scope_id='test-city', provider='telegram', account_id='test_bot',
                                                   conversation_id='123', kind='dm'))

    def test_the_health_file_and_the_hook_secret_file_reach_the_config(self):
        path = write_settings(self.root, health_file='"/srv/yard/health.json"')
        path.write_text(path.read_text().replace(str(self.root / 'private' / 'hook-secret'),
                                                 '~/.config/test-city/hook-secret'))
        settings = b.load_settings(path)
        self.assertEqual(settings.config['health_file'], '/srv/yard/health.json')
        self.assertEqual(settings.hook_secret_file, Path.home() / '.config/test-city/hook-secret')
        self.assertEqual(settings.config['protected_paths'], (str(self.root / 'private' / 'telegram-bot-token'),
                                                              str(self.root / 'private' / 'telegram-callback-secret'),
                                                              str(Path.home() / '.config/test-city/hook-secret'),
                                                              str(path)))

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
            ('not valid TOML', dict(body='yard = \n')),
            ('unknown key language', dict(language='"ru"')),
            ('yard is missing or malformed', dict(yard=None)),
            ('yard is missing or malformed', dict(yard='"two words"')),
            ('crew is missing or malformed', dict(crew='"two words"')),
            ('state_dir must be a path', dict(state_dir=None)),
            ('state_dir must be an absolute', dict(state_dir='"relative/state"')),
            ('state_dir must be an absolute', dict(state_dir='"/srv/a b"')),
            ('state_dir must be an absolute', dict(state_dir='"/srv/%h/state"')),
            ('state_dir must be an absolute', dict(state_dir='"/srv/../etc"')),
            ('port must be an integer from 1024 to 65535', dict(port='80')),
            ('port must be an integer', dict(port='"8930"')),
            ('token_file must be a path', dict(token_file=None)),
            ('timezone is not a known time zone', dict(timezone='"Mars/Olympus"')),
            ('unit is missing or malformed', dict(unit='"bridge; rm"')),
            ('pairing_update_id must be an integer', dict(pairing_update_id='-2')),
        ]
        for pattern, replace in cases:
            with self.subTest(pattern=pattern, replace=replace):
                self.refused(pattern, **replace)

    def test_the_gc_packs_keys_are_refused_by_name(self):
        for key, value, replacement in (('city', '"test-city"', 'yard'), ('agent', '"mayor"', 'crew'),
                                        ('gc_base_url', '"http://127.0.0.1:8372"', '[yardr] hook_url'),
                                        ('gc_api_token_file', '"/srv/gc-token"', '[yardr] hook_secret_file')):
            with self.subTest(key=key):
                with self.assertRaises(b.SettingsError) as caught:
                    b.load_settings(write_settings(self.root, **{key: value}))
                self.assertEqual(str(caught.exception),
                                 f"settings: {key} is the gc pack's key; this bridge reads {replacement} (YARDR.md)")
        # Named even in a gc pack file that has no yard key at all.
        with self.assertRaisesRegex(b.SettingsError, "^settings: city is the gc pack's key"):
            b.load_settings(write_settings(self.root, yard=None, city='"test-city"'))

    def test_the_yardr_table_is_required_and_names_its_keys(self):
        secret = self.root / 'private' / 'hook-secret'
        url = 'hook_url = "http://127.0.0.1:8797/hooks/tgbridge"\n'
        file = f'hook_secret_file = "{secret}"\n'
        for table, line in ((None, 'settings: [yardr] is required'),
                            (file, 'settings: yardr.hook_url is missing or malformed'),
                            ('hook_url = ""\n' + file, 'settings: yardr.hook_url is missing or malformed'),
                            (f'hook_url = "http://127.0.0.1:8797/hooks/{"x" * 250}"\n' + file,
                             'settings: yardr.hook_url is missing or malformed'),
                            (url, 'settings: yardr.hook_secret_file must be a path'),
                            (url + 'hook_secret_file = "relative/hook-secret"\n',
                             'settings: yardr.hook_secret_file must be an absolute, normalized path'),
                            (url + file + 'secret = "x"\n', 'settings: unknown key yardr.secret'),
                            (url + file + 'source = "two words"\n', 'settings: yardr.source is missing or malformed')):
            with self.subTest(table=table):
                with self.assertRaises(b.SettingsError) as caught:
                    b.load_settings(self.with_yardr(table))
                self.assertTrue(str(caught.exception).startswith(line), str(caught.exception))
        self.assertEqual(b.load_settings(self.with_yardr(url + file)).config['yardr_source'], 'tgbridge')
        settings = b.load_settings(self.with_yardr(url + file + 'source = "telegram-ops"\n'))
        self.assertEqual(settings.config['yardr_source'], 'telegram-ops')
        self.assertEqual(settings.config['hook_url'], 'http://127.0.0.1:8797/hooks/tgbridge')

    def test_the_hook_url_must_be_plain_http_on_loopback_at_a_hooks_path(self):
        line = ('settings: yardr.hook_url must be http://<loopback address>:<port>/hooks/<name>, '
                'with no user, query or fragment')
        secret = self.root / 'private' / 'hook-secret'
        for url in ('https://127.0.0.1:8797/hooks/tgbridge', 'http://10.0.0.1:8797/hooks/tgbridge',
                    'http://example.com:8797/hooks/tgbridge', 'http://127.0.0.1:8797',
                    'http://127.0.0.1:8797/', 'http://127.0.0.1:8797/api', 'http://127.0.0.1:8797/hooks/',
                    'http://127.0.0.1:8797/hooks/a/b', 'http://127.0.0.1:8797/hooks/-x',
                    'http://u@127.0.0.1:8797/hooks/tgbridge', 'http://u:p@127.0.0.1:8797/hooks/tgbridge',
                    'http://127.0.0.1:8797/hooks/tgbridge?x=1', 'http://127.0.0.1:8797/hooks/tgbridge#top',
                    'http://127.0.0.1/hooks/tgbridge', 'http://127.0.0.1:99999/hooks/tgbridge'):
            with self.subTest(url=url):
                with self.assertRaises(b.SettingsError) as caught:
                    b.load_settings(self.with_yardr(f'hook_url = "{url}"\nhook_secret_file = "{secret}"\n'))
                self.assertEqual(str(caught.exception), line)
        for url in ('http://127.0.0.1:8797/hooks/tgbridge', 'http://[::1]:8797/hooks/tg.bridge_2',
                    'http://localhost:8797/hooks/tgbridge'):
            with self.subTest(url=url):
                settings = b.load_settings(self.with_yardr(f'hook_url = "{url}"\nhook_secret_file = "{secret}"\n'))
                self.assertEqual(settings.config['hook_url'], url)

    def test_the_hook_secret_must_be_a_file_of_its_own(self):
        token = self.root / 'private' / 'telegram-bot-token'
        callback = self.root / 'private' / 'telegram-callback-secret'
        hook = self.root / 'private' / 'hook-secret'
        url = 'hook_url = "http://127.0.0.1:8797/hooks/tgbridge"\n'
        line = 'settings: yardr.hook_secret_file must differ from token_file and callback_secret_file'
        for same in (token, callback):
            with self.subTest(same=same.name):
                with self.assertRaises(b.SettingsError) as caught:
                    b.load_settings(self.with_yardr(url + f'hook_secret_file = "{same}"\n'))
                self.assertEqual(str(caught.exception), line)
        hook.symlink_to(token)  # an alias of the token file
        with self.assertRaises(b.SettingsError) as caught:
            b.load_settings(write_settings(self.root))
        self.assertEqual(str(caught.exception), line)
        hook.unlink()
        callback.write_text('CALLBACK\n')
        os.link(callback, hook)  # a hard link to the callback secret
        with self.assertRaises(b.SettingsError) as caught:
            b.load_settings(write_settings(self.root))
        self.assertEqual(str(caught.exception), line)
        hook.unlink()
        hook.write_text('HOOK\n')
        self.assertEqual(b.load_settings(write_settings(self.root)).hook_secret_file, hook)

    def test_bot_id_above_the_signed_64_bit_bound_is_refused(self):
        base = write_settings(self.root).read_text()
        path = write_settings(self.root, body=base.replace('bot_id = 999', 'bot_id = 9223372036854775808'))
        with self.assertRaises(b.SettingsError) as caught:
            b.load_settings(path)
        self.assertEqual(str(caught.exception),
                         'settings: bot.bot_id must be an integer from 1 to 9223372036854775807')

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
        calls = []
        def fake(url, body, *args, **kwargs):
            calls.append((url.rsplit('/', 1)[1], body))
            if url.endswith('setMyCommands'):
                return 200, dict(ok=True, result=True)
            return 200, dict(ok=True, result=[])
        bridge = b.Bridge(settings.config, None, 'SECRET', None, request=fake)
        self.assertEqual(bridge.set_commands(), [])
        self.assertEqual(calls, [('setMyCommands', dict(commands=[], scope=dict(type='chat', chat_id=123))),
                                 ('getMyCommands', dict(scope=dict(type='chat', chat_id=123)))])

    def test_a_second_yard_on_the_same_host_gets_its_own_everything(self):
        first = b.load_settings(write_settings(self.root))
        other_root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        second = b.load_settings(write_settings(other_root, yard='"other-yard"', port='18940',
                                                user_display_name='"Sam"', timezone='"UTC"'))
        self.assertEqual(second.unit, 'other-yard-telegram.service')
        for field in ('yard', 'state_dir', 'port', 'token_file', 'callback_secret_file', 'hook_secret_file',
                      'lock_dir', 'unit'):
            self.assertNotEqual(getattr(first, field), getattr(second, field), field)
        bridges = [b.Bridge(s.config, None, 'SECRET', None) for s in (first, second)]
        self.assertEqual([x.conversation['scope_id'] for x in bridges], ['test-city', 'other-yard'])
        self.assertEqual([x.display_name for x in bridges], ['Pat', 'Sam'])


class AutoTopicsSettingsTests(unittest.TestCase):
    """[auto_topics]: one bool key, enabled, default false."""

    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def load(self, table):
        path = write_settings(self.root)
        path.write_text(path.read_text() + table)
        return b.load_settings(path).config

    def test_a_file_without_the_table_loads_as_before(self):
        config = self.load('')
        self.assertNotIn('auto_topics', config)
        self.assertEqual(config, b.load_settings(write_settings(self.root)).config)
        self.assertFalse(b.Bridge(config, None, 'SECRET', None).auto_topics)

    def test_enabled_true_false_and_the_default_load(self):
        without = self.load('')
        for table, enabled in (('\n[auto_topics]\nenabled = true\n', True),
                               ('\n[auto_topics]\nenabled = false\n', False), ('\n[auto_topics]\n', False)):
            with self.subTest(table=table):
                config = self.load(table)
                self.assertEqual(config['auto_topics'], {'enabled': enabled})
                self.assertEqual({k: v for k, v in config.items() if k != 'auto_topics'}, without)
                self.assertIs(b.Bridge(config, None, 'SECRET', None).auto_topics, enabled)

    def test_an_unknown_key_or_a_non_bool_refuses_with_one_line(self):
        for table, message in (('\n[auto_topics]\nenabled = true\nagent = "helper"\n', 'unknown key auto_topics.agent'),
                               ('\n[auto_topics]\nname = "ops"\n', 'unknown key auto_topics.name'),
                               ('\n[auto_topics]\nenabled = 1\n', 'auto_topics.enabled must be true or false'),
                               ('\n[auto_topics]\nenabled = "true"\n', 'auto_topics.enabled must be true or false'),
                               ('\nauto_topics = true\n', 'auto_topics must be a table')):
            with self.subTest(table=table):
                with self.assertRaises(b.SettingsError) as raised:
                    if table.startswith('\nauto_topics'):
                        path = write_settings(self.root)
                        path.write_text(table + path.read_text())
                        b.load_settings(path)
                    else:
                        self.load(table)
                self.assertEqual(str(raised.exception), 'settings: ' + message)


class ExampleSettingsTests(unittest.TestCase):
    """Pin settings.example.toml against load_settings: every key the
    example sets, commented or not, is a key the loader accepts."""

    EXPECTED = {
        '': {'yard', 'state_dir', 'port', 'token_file', 'callback_secret_file', 'lock_dir', 'unit', 'python',
             'user_display_name', 'timezone', 'health_file', 'pairing_update_id', 'crew', 'handler_write_paths'},
        'yardr': {'hook_url', 'hook_secret_file', 'source', 'home', 'operator'},
        'bot': {'bot_id', 'user_id', 'chat_id', 'account_id'},
        'menu': {'command', 'description'},
        'command': {'name', 'description', 'handler', 'menu', 'callback_prefix', 'timeout'},
        'transcribe': {'enabled', 'whisper_cli', 'models_dir', 'model', 'language', 'vad_model', 'cpus',
                       'threads', 'timeout', 'timeout_per_minute', 'method', 'gpu_check', 'gpu',
                       'gpu_min_free_mb'},
        'reactions': {'on_accept', 'seen', 'done'},
        'typing': {'seconds'},
        'topic': {'name', 'crew'},
        'auto_topics': {'enabled'},
    }

    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))

    @staticmethod
    def _example_keys(text):
        keys, section = {}, ''
        for line in text.splitlines():
            stripped = line.strip()
            content = stripped.lstrip('#').strip() if stripped.startswith('#') else stripped
            header = re.fullmatch(r'\[\[(menu|command|topic)\]\]|\[(yardr|bot|transcribe|reactions|typing|auto_topics)\]',
                                  content)
            if header:
                section = header.group(1) or header.group(2)
                keys.setdefault(section, set())
                continue
            key = re.match(r'([a-z_]+)\s*=', content)
            if key:
                keys.setdefault(section, set()).add(key.group(1))
        return keys

    def _write_example(self, command='handler', transcribe='method'):
        (self.root / 'yard').mkdir(mode=0o700, exist_ok=True)
        private = self.root / 'private'
        private.mkdir(mode=0o700, exist_ok=True)
        for name, mode in (('telegram-bot-token', 0o600), ('callback-secret', 0o600), ('hook-secret', 0o600),
                           ('telegram-deploy', 0o700), ('gpu-check', 0o700)):
            path = private / name
            path.write_text('TEST\n' if mode == 0o600 else '#!/bin/sh\n')
            path.chmod(mode)
        text = (PACK / 'settings.example.toml').read_text()
        for old, new in sorted({
                '/home/alex/.local/state/example-telegram': str(self.root / 'state'),
                '~/.config/example-telegram': str(private),
                '~/.yardr': str(self.root / 'yard'),
                '/home/alex/example/health.json': str(self.root / 'health.json'),
                '/home/alex/example/var/deploy': str(self.root / 'deploy'),
                '/home/alex/example/bin/telegram-deploy': str(private / 'telegram-deploy'),
                '/home/alex/example/bin/gpu-check': str(private / 'gpu-check'),
                '~/work/whisper.cpp/build/bin/whisper-cli': str(self.root / 'whisper-cli'),
                '~/work/whisper.cpp/models': str(self.root / 'models')}.items(), key=lambda item: -len(item[0])):
            text = text.replace(old, new)
        lines, section = [], ''
        for line in text.splitlines():
            stripped = line.strip()
            commented = stripped.startswith('#')
            content = stripped.lstrip('#').strip() if commented else stripped
            header = re.fullmatch(r'\[\[(menu|command|topic)\]\]|\[(yardr|bot|transcribe|reactions|typing|auto_topics)\]',
                                  content)
            if header:
                section = header.group(1) or header.group(2)
                lines.append(content)
                continue
            key = re.match(r'([a-z_]+)\s*=', content) if commented else None
            if key is None:
                lines.append(line)
            elif section == 'command' and key.group(1) == 'handler' and command == 'url':
                lines.append('url = "http://127.0.0.1:8095/deploy"')
            elif section == 'transcribe' and transcribe == 'method' and key.group(1) == 'gpu':
                lines.append(line)
            elif section == 'transcribe' and transcribe == 'method' and key.group(1) == 'method':
                lines.append('method = "auto"')  # gpu_check is only valid with method = "auto"
            elif section == 'transcribe' and transcribe == 'legacy' and key.group(1) in ('method', 'gpu_check'):
                lines.append(line)
            else:
                lines.append(content)
        path = self.root / f'example-{command}-{transcribe}.toml'
        path.write_text('\n'.join(lines) + '\n')
        path.chmod(0o600)
        return path

    def test_example_keys_load_and_an_unknown_key_refuses(self):
        self.assertEqual(self._example_keys((PACK / 'settings.example.toml').read_text()), self.EXPECTED)
        loaded = {section: set() for section in self.EXPECTED}
        for command, transcribe in (('handler', 'method'), ('url', 'legacy')):
            with self.subTest(command=command, transcribe=transcribe):
                path = self._write_example(command, transcribe)
                settings = b.load_settings(path)
                self.assertEqual(settings.yard, 'example')
                self.assertEqual(settings.hook_secret_file, self.root / 'private' / 'hook-secret')
                self.assertEqual(settings.config['commands'][0]['name'], 'deploy')
                self.assertIn('url' if command == 'url' else 'handler', settings.config['commands'][0])
                self.assertIsNotNone(settings.transcribe)
                data = tomllib.loads(path.read_text())
                loaded[''] |= set(data) - {'yardr', 'bot', 'menu', 'command', 'transcribe', 'reactions', 'typing',
                                           'topic', 'auto_topics'}
                loaded['yardr'] |= set(data['yardr'])
                loaded['bot'] |= set(data['bot'])
                loaded['menu'] |= set(data['menu'][0])
                loaded['command'] |= set(data['command'][0])
                loaded['transcribe'] |= set(data['transcribe'])
                loaded['reactions'] |= set(data['reactions'])
                loaded['typing'] |= set(data['typing'])
                loaded['topic'] |= set(data['topic'][0])
                loaded['auto_topics'] |= set(data['auto_topics'])
                self.assertEqual(settings.config['auto_topics'], {'enabled': True})
                self.assertEqual(settings.config['topics'],
                                 (dict(name='ops', agent='secretary'), dict(name='web', agent='yardmaster')))
        self.assertEqual(loaded, dict(self.EXPECTED, command=self.EXPECTED['command'] | {'url'}))
        path = self._write_example()
        path.write_text('unknown_example_key = 1\n' + path.read_text())
        with self.assertRaisesRegex(b.SettingsError, 'unknown key unknown_example_key'):
            b.load_settings(path)


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
        self.assertEqual(settings.language, 'en')
        self.assertEqual(settings.transcribe, dict(
            python='/usr/bin/python3', script=str(PACK / 'transcriber' / 'adapter.py'), model='large-v3-turbo-q5_0',
            timeout=120, timeout_per_minute=120, gpu=False, method='cpu', language='en', env={
                'VOICE_TRANSCRIBE_WHISPER_CLI': str(home / 'work/whisper.cpp/build/bin/whisper-cli'),
                'VOICE_TRANSCRIBE_MODEL_DIR': str(home / 'work/whisper.cpp/models'),
                'VOICE_TRANSCRIBE_VAD_MODEL': 'silero-v6.2.0',
                'VOICE_TRANSCRIBE_LANGUAGE': 'en',
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

    def test_every_yard_gets_the_one_host_wide_queue(self):
        # Two yards' settings, each with its own state, port and transcriber
        # paths, still name one queue: there is no per-yard queue setting, and
        # the transcriber derives the same directory on its own.
        home = self.root / 'home'
        second = self.root / 'second'
        second.mkdir()
        yards = []
        for root, table in ((self.root, 'enabled = true\n'),
                            (second, 'enabled = true\nwhisper_cli = "/opt/w/whisper-cli"\nmodel = "small.en"\n')):
            path = write_settings(root, yard='"second-yard"' if root == second else '"test-city"',
                                  port='18940' if root == second else '18930')
            path.write_text(path.read_text() + '\n[transcribe]\n' + table)
            yards.append(b.load_settings(path, home=home))
        self.assertNotEqual(yards[0].state_dir, yards[1].state_dir)
        self.assertEqual({c.queue_dir for c in yards}, {home / '.local/state/voice-transcribe'})
        self.assertEqual(b.QUEUE_SUBDIR, vt.QUEUE_SUBDIR)
        for yard in yards:
            self.assertNotIn('VOICE_TRANSCRIBE_QUEUE_DIR', yard.transcribe['env'])
            self.assertEqual(vt.Options(environ=yard.transcribe['env']).queue_dir,
                             Path.home() / '.local/state/voice-transcribe')

    def test_importing_bridge_does_not_need_voice_transcribe_on_sys_path(self):
        bridge = PACK / 'bridge' / 'bridge.py'
        code = (
            'import importlib.util, sys\n'
            f'spec = importlib.util.spec_from_file_location("bridge_under_test", {str(bridge)!r})\n'
            'mod = importlib.util.module_from_spec(spec)\n'
            'spec.loader.exec_module(mod)\n'
            'assert not any("transcriber" in entry for entry in sys.path), sys.path\n'
            'assert mod.QUEUE_SUBDIR == ".local/state/voice-transcribe"\n'
            'assert mod.Bridge.TRANSCRIBE_RUNNING_LINE == b\'{"state": "running"}\'\n'
            'assert mod.Bridge.TRANSCRIBE_GPU_SHARE == 4\n'
            'assert mod.Bridge.TRANSCRIBE_GPU_MIN == 30\n'
        )
        done = subprocess.run([sys.executable, '-B', '-c', code], cwd=self.root,
                              env={k: v for k, v in os.environ.items() if k != 'PYTHONPATH'},
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)

    def test_paths_and_limits_come_from_the_settings(self):
        settings = self.load('enabled = true\nwhisper_cli = "/opt/w/whisper-cli"\nmodels_dir = "/opt/w/models"\n'
                             'model = "small.en"\nthreads = 2\ncpus = "0-1,4-5"\ntimeout = 60\ntimeout_per_minute = 90\n')
        self.assertEqual(settings.transcribe['model'], 'small.en')
        self.assertEqual(settings.transcribe['timeout'], 60)
        self.assertEqual(settings.transcribe['timeout_per_minute'], 90)
        self.assertEqual(settings.transcribe['env']['VOICE_TRANSCRIBE_CPUS'], '0-1,4-5')
        self.assertEqual(settings.transcribe['env']['VOICE_TRANSCRIBE_WHISPER_CLI'], '/opt/w/whisper-cli')
        self.assertEqual(settings.transcribe['env']['VOICE_TRANSCRIBE_MODEL_DIR'], '/opt/w/models')
        self.assertEqual(settings.transcribe['env']['VOICE_TRANSCRIBE_THREADS'], '2')
        self.assertEqual(settings.transcribe['env']['VOICE_TRANSCRIBE_TIMEOUT'], '60')
        self.assertEqual(settings.transcribe['env']['VOICE_TRANSCRIBE_TIMEOUT_PER_MINUTE'], '90')

    def test_raised_transcribe_limits_are_accepted(self):
        settings = self.load('enabled = true\nthreads = 16\ntimeout = 1200\ntimeout_per_minute = 1200\n')
        self.assertEqual(settings.transcribe['env']['VOICE_TRANSCRIBE_THREADS'], '16')
        self.assertEqual(settings.transcribe['timeout'], 1200)
        self.assertEqual(settings.transcribe['timeout_per_minute'], 1200)
        self.assertEqual(vt.Options(environ=settings.transcribe['env']).threads, 16)

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

    def test_language_setting_accepts_codes_and_auto_and_bad_values_refuse(self):
        for value in ('ru', 'de', 'auto', 'en'):
            with self.subTest(value=value):
                settings = self.load(f'enabled = true\nlanguage = "{value}"\n')
                self.assertEqual(settings.language, value)
                self.assertEqual(settings.transcribe['language'], value)
                self.assertEqual(settings.transcribe['env']['VOICE_TRANSCRIBE_LANGUAGE'], value)
                self.assertEqual(vt.Options(environ=settings.transcribe['env']).language, value)
        # An unlisted short lowercase code is accepted: there is no allowlist.
        self.assertEqual(self.load('enabled = true\nlanguage = "zz"\n').language, 'zz')
        for table in ('enabled = true\nlanguage = ""\n', 'enabled = true\nlanguage = "en; rm"\n',
                      'enabled = true\nlanguage = "EN-US"\n', 'enabled = true\nlanguage = "EN"\n',
                      'enabled = true\nlanguage = 1\n'):
            with self.subTest(table=table):
                with self.assertRaisesRegex(b.SettingsError, 'transcribe.language must be a whisper language code'):
                    self.load(table)

    def test_an_english_only_model_with_a_non_english_language_refuses(self):
        with self.assertRaises(b.SettingsError) as caught:
            self.load('enabled = true\nmodel = "medium.en"\nlanguage = "ru"\n')
        self.assertEqual(str(caught.exception),
                         'settings: transcribe.model "medium.en" is English-only and cannot decode '
                         'transcribe.language "ru"; use a multilingual model '
                         '(such as large-v3-turbo-q5_0) or set language = "en"')
        with self.assertRaises(b.SettingsError) as caught:
            self.load('enabled = true\nmodel = "base.en"\nlanguage = "auto"\n')
        self.assertIn('"base.en"', str(caught.exception))
        self.assertIn('"auto"', str(caught.exception))

    def test_an_english_only_model_loads_with_english_or_when_disabled(self):
        self.assertEqual(self.load('enabled = true\nmodel = "base.en"\nlanguage = "en"\n').model, 'base.en')
        self.assertEqual(self.load('enabled = true\nmodel = "base.en"\n').model, 'base.en')
        disabled = self.load('enabled = false\nmodel = "base.en"\nlanguage = "ru"\n')
        self.assertIsNone(disabled.transcribe)
        self.assertEqual(disabled.model, 'base.en')
        self.assertEqual(self.load('enabled = true\nmodel = "large-v3-turbo-q5_0"\nlanguage = "ru"\n').model,
                         'large-v3-turbo-q5_0')

    def test_check_install_refuses_an_english_only_model_with_one_line_and_no_warn(self):
        path = write_settings(self.root, python=f'"{sys.executable}"')
        path.write_text(path.read_text() + '\n[transcribe]\nenabled = true\nmodel = "medium.en"\nlanguage = "ru"\n')
        result = subprocess.run([sys.executable, str(PACK / 'bridge' / 'bridge.py'), '--settings', str(path),
                                 'check-install'], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, '')
        self.assertEqual(result.stderr.strip().splitlines(), [
            'settings: transcribe.model "medium.en" is English-only and cannot decode '
            'transcribe.language "ru"; use a multilingual model '
            '(such as large-v3-turbo-q5_0) or set language = "en"'])
        self.assertNotIn('WARN', result.stdout + result.stderr)

    def test_bad_transcribe_values_refuse(self):
        for table, pattern in (('device = "cuda"\n', 'unknown key transcribe.device'),
                               ('enabled = "yes"\n', 'transcribe.enabled must be true or false'),
                               ('timeout = 3601\n', 'transcribe.timeout must be an integer from 1 to 3600'),
                               ('timeout = 0\n', 'transcribe.timeout must be an integer from 1 to 3600'),
                               ('threads = 65\n', 'transcribe.threads must be an integer from 1 to 64'),
                               ('timeout_per_minute = 3601\n', 'transcribe.timeout_per_minute must be an integer from 0 to 3600'),
                               ('queue_dir = "/var/tmp/q"\n', 'unknown key transcribe.queue_dir'),
                               ('cpus = "0-3; rm"\n', 'transcribe.cpus must be a CPU list'),
                               ('cpus = [2, 3]\n', 'transcribe.cpus must be a CPU list'),
                               ('timeout_per_minute = -1\n', 'transcribe.timeout_per_minute must be an integer'),
                               ('model = "a b;rm"\n', 'transcribe.model is missing or malformed'),
                               ('whisper_cli = "whisper-cli"\n', 'transcribe.whisper_cli must be an absolute'),
                               ('gpu = "yes"\n', 'transcribe.gpu must be true or false'),
                               ('gpu = 1\n', 'transcribe.gpu must be true or false'),
                               ('gpu_min_free_mb = 100\n', 'transcribe.gpu_min_free_mb must be an integer from 256'),
                               ('gpu_min_free_mb = "2048"\n', 'transcribe.gpu_min_free_mb must be an integer')):
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

    def test_gpu_check_in_a_group_writable_directory_is_refused(self):
        shared = self.root / 'shared-checks'
        shared.mkdir()
        shared.chmod(0o775)
        check = shared / 'gpu-check'
        check.write_text('#!/bin/sh\n')
        check.chmod(0o755)
        with self.assertRaisesRegex(b.SettingsError, r'transcribe\.gpu_check .* must be owned by this user or root, '
                                                     r'in a directory only they can write'):
            self.load(f'enabled = true\nmethod = "auto"\ngpu_check = "{check}"\n')

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

    def test_explicit_then_the_environment(self):
        self.assertEqual(b.settings_path('/x/given.toml', environ={b.SETTINGS_ENV: '/x/env.toml'}),
                         Path('/x/given.toml'))
        self.assertEqual(b.settings_path(None, environ={b.SETTINGS_ENV: '/x/env.toml'}), Path('/x/env.toml'))
        self.assertEqual(b.settings_path(None, environ={b.SETTINGS_ENV: '~/env.toml'}), Path.home() / 'env.toml')
        with patch.dict(os.environ, {b.SETTINGS_ENV: '/x/process.toml'}):
            self.assertEqual(b.settings_path(None), Path('/x/process.toml'))

    def test_nothing_else_finds_settings(self):
        # No city root, no .gc/site.toml, no default under ~/.config.
        (self.root / '.gc').mkdir()
        (self.root / '.gc' / 'site.toml').write_text('workspace_name = "some-city"\n')
        for environ in ({}, {b.SETTINGS_ENV: ''}, {'GC_CITY': str(self.root), 'GC_CITY_PATH': str(self.root)}):
            with self.subTest(environ=environ):
                with self.assertRaises(b.SettingsError) as caught:
                    b.settings_path(None, environ=environ)
                self.assertEqual(str(caught.exception),
                                 f'no settings file: pass --settings or set {b.SETTINGS_ENV}')

    def test_cli_refuses_to_start_without_settings_and_says_why(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith('GC_') and k != b.SETTINGS_ENV}
        env['HOME'] = str(self.root)
        bridge = str(PACK / 'bridge' / 'bridge.py')
        result = subprocess.run([sys.executable, bridge, 'run'],
                                env=env, capture_output=True, text=True, timeout=30, cwd=self.root)
        self.assertEqual((result.returncode, result.stderr.strip()),
                         (1, f'no settings file: pass --settings or set {b.SETTINGS_ENV}'))
        missing = self.root / 'missing.toml'
        result = subprocess.run([sys.executable, bridge, '--settings', str(missing), 'run'],
                                env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual((result.returncode, result.stderr.strip()), (1, f'settings file not found: {missing}'))
        result = subprocess.run([sys.executable, bridge, 'run'], env=dict(env, **{b.SETTINGS_ENV: str(missing)}),
                                capture_output=True, text=True, timeout=30)
        self.assertEqual((result.returncode, result.stderr.strip()), (1, f'settings file not found: {missing}'))
        result = subprocess.run([sys.executable, bridge, f'--city-root={self.root}', 'run'],
                                env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 2)  # the gc pack's flag is gone
        self.assertIn('unrecognized arguments: --city-root', result.stderr)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), [])  # nothing created


class UnitTemplateTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def test_renders_the_yard_unit_with_its_own_paths(self):
        path = write_settings(self.root)
        settings = b.load_settings(path)
        unit = b.render_unit(settings, bridge_path='/srv/pack/bridge/bridge.py')
        state, private = self.root / 'state', self.root / 'private'
        self.assertIn("Description=Private Telegram transport for the test-city yard's yardmaster\n", unit)
        self.assertIn(f'WorkingDirectory={state}\n', unit)
        self.assertIn(f'ExecStart=/usr/bin/python3 /srv/pack/bridge/bridge.py --settings {path} run\n', unit)
        self.assertIn(f'ReadWritePaths={state} {private}\n', unit)  # transcription off: no queue
        self.assertIn(f'ReadWritePaths={state} {private}\n'
                      f'ReadOnlyPaths=-{path} -{private / "telegram-bot-token"} '
                      f'-{private / "telegram-callback-secret"} -{private / "hook-secret"}\n', unit)
        for line in ('Restart=on-failure', 'RestartPreventExitStatus=2', 'UMask=0077', 'NoNewPrivileges=true',
                     'PrivateTmp=true', 'ProtectSystem=strict', 'ProtectHome=read-only', 'StartLimitIntervalSec=0',
                     'RestrictSUIDSGID=true', 'LockPersonality=true', 'RestrictRealtime=true',
                     'RestrictNamespaces=true', 'SystemCallArchitectures=native',
                     'RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6 AF_NETLINK'):
            self.assertIn(line + '\n', unit)
        self.assertNotRegex(unit, r'@[A-Z_]+@')

    def test_a_hook_secret_kept_elsewhere_is_read_only_and_the_crew_named(self):
        hook = self.root / 'yard' / 'hook-secret'
        path = write_settings(self.root, crew='"secretary"')
        path.write_text(path.read_text().replace(str(self.root / 'private' / 'hook-secret'), str(hook)))
        unit = b.render_unit(b.load_settings(path), bridge_path='/srv/pack/bridge/bridge.py')
        private = self.root / 'private'
        self.assertIn(f'ReadWritePaths={self.root / "state"} {private}\n', unit)
        self.assertIn(f'ReadOnlyPaths=-{path} -{private / "telegram-bot-token"} '
                      f'-{private / "telegram-callback-secret"} -{hook}\n', unit)
        self.assertIn("Description=Private Telegram transport for the test-city yard's secretary\n", unit)

    def test_the_sandbox_properties_carry_the_read_only_paths_and_hardening(self):
        settings = b.load_settings(write_settings(self.root))
        properties = b.unit_sandbox_properties(settings, bridge_path='/srv/pack/bridge/bridge.py')
        private = self.root / 'private'
        self.assertIn(f'ReadOnlyPaths=-{settings.path} -{private / "telegram-bot-token"} '
                      f'-{private / "telegram-callback-secret"} -{private / "hook-secret"}', properties)
        for line in ('RestrictSUIDSGID=true', 'LockPersonality=true', 'RestrictRealtime=true',
                     'RestrictNamespaces=true', 'SystemCallArchitectures=native',
                     'RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6 AF_NETLINK'):
            self.assertIn(line, properties)

    def test_with_transcription_the_unit_may_write_the_host_wide_queue(self):
        path = write_settings(self.root)
        path.write_text(path.read_text() + '\n[transcribe]\nenabled = true\n')
        unit = b.render_unit(b.load_settings(path, home='/home/pat'), bridge_path='/srv/bridge.py')
        self.assertIn(f'ReadWritePaths={self.root / "state"} {self.root / "private"} '
                      '/home/pat/.local/state/voice-transcribe\n', unit)

    def test_the_cli_prints_the_same_unit(self):
        path = write_settings(self.root)
        env = dict(os.environ, TELEGRAM_PACK_PLATFORM='linux')
        result = subprocess.run([sys.executable, str(PACK / 'bridge' / 'bridge.py'), '--settings', str(path), 'render-unit'],
                                capture_output=True, text=True, timeout=30, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, b.render_unit(b.load_settings(path), bridge_path=PACK / 'bridge' / 'bridge.py'))

    def test_render_service_on_linux_and_wsl_is_the_systemd_unit(self):
        settings = b.load_settings(write_settings(self.root))
        for plat in ('linux', 'wsl'):
            with self.subTest(platform=plat):
                self.assertEqual(b.render_service(settings, platform=plat, bridge_path='/srv/pack/bridge/bridge.py'),
                                 b.render_unit(settings, bridge_path='/srv/pack/bridge/bridge.py'))

    def test_render_service_on_darwin_is_the_launchd_plist(self):
        home = self.root / 'home'
        for replace, label in ((dict(unit='"my-bridge.service"'), 'my-bridge'), (dict(), 'test-city-telegram')):
            with self.subTest(label=label):
                path = write_settings(self.root, **replace)
                settings = b.load_settings(path)
                text = b.render_service(settings, platform='darwin', home=home,
                                        bridge_path='/srv/pack/bridge/bridge.py')
                log = home / 'Library' / 'Logs' / f'{label}.log'
                self.assertEqual(plistlib.loads(text.encode()), {
                    'Label': label,
                    'ProgramArguments': ['/usr/bin/python3', '/srv/pack/bridge/bridge.py', '--settings', str(path),
                                         'run', '--conflict-exit-status', '0'],
                    'WorkingDirectory': str(self.root / 'state'),
                    'RunAtLoad': True,
                    'KeepAlive': {'SuccessfulExit': False},
                    'ThrottleInterval': 15,
                    'ExitTimeOut': 45,
                    'Umask': 63,
                    'EnvironmentVariables': {'PATH': b.LAUNCHD_PATH},
                    'StandardOutPath': str(log),
                    'StandardErrorPath': str(log),
                })

    def test_service_file_and_next_line_for_each_platform(self):
        home = self.root / 'home'
        xdg = self.root / 'xdg'
        settings = b.load_settings(write_settings(self.root, unit='"my-bridge.service"'))
        launcher = home / '.local/share/yardr-telegram/my-bridge/bin/telegram-bridge'
        with patch.dict(os.environ, {'XDG_CONFIG_HOME': str(xdg)}):
            for plat in ('linux', 'wsl'):
                with self.subTest(platform=plat):
                    self.assertEqual(b.service_file(settings, platform=plat, home=home),
                                     xdg / 'systemd' / 'user' / 'my-bridge.service')
                    self.assertEqual(b.service_next_line(settings, launcher, platform=plat, home=home),
                                     f'next: {launcher} probe; '
                                     'systemctl --user daemon-reload; '
                                     'systemctl --user enable my-bridge.service; '
                                     'systemctl --user restart my-bridge.service; '
                                     f'{launcher} set-commands (yardr/SETUP.md, step 9)')
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(b.service_file(settings, platform='linux', home=home),
                             home / '.config' / 'systemd' / 'user' / 'my-bridge.service')
        plist = home / 'Library' / 'LaunchAgents' / 'my-bridge.plist'
        self.assertEqual(b.service_file(settings, platform='darwin', home=home), plist)
        self.assertEqual(b.launchd_label(settings), 'my-bridge')
        self.assertEqual(b.service_next_line(settings, launcher, platform='darwin', home=home),
                         f'next: {launcher} probe; '
                         'launchctl bootout gui/$(id -u)/my-bridge 2>/dev/null; '
                         f'launchctl bootstrap gui/$(id -u) {plist}; '
                         f'{launcher} set-commands (yardr/SETUP.md, step 9)')

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
        kwargs.setdefault('platform', 'linux')
        kwargs.setdefault('version', lambda python: (3, 14))
        return [line for ok, line in b.install_problems(self.settings, **kwargs) if not ok]

    def model(self, name, data):
        (self.models / f'ggml-{name}.bin').write_bytes(data)
        return {f'ggml-{name}.bin': hashlib.sha256(data).hexdigest()}

    def test_everything_present_and_matching_passes(self):
        sums = {**self.model('large-v3-turbo-q5_0', b'turbo'), **self.model('silero-v6.2.0', b'vad')}
        self.assertEqual(self.failures(sums=sums), [])
        results = b.install_problems(self.settings, which=lambda n: '/usr/bin/' + n, sums=sums,
                                     platform='linux', version=lambda python: (3, 14))
        self.assertEqual(results, [
            (True, f'python {self.settings.python}'),
            (True, 'curl on PATH'),
            (True, 'bot token file is owner-only'),
            (True, 'callback secret file is created by install.sh'),
            (True, 'hook secret file is created by install.sh'),
            (True, f'state directory {self.settings.state_dir}'),
            (True, f'lock directory {self.settings.lock_dir}'),
            (True, 'ffmpeg on PATH'),
            (True, f'transcription queue {self.queue} is created by install.sh'),
            (True, f'whisper-cli {self.cli}'),
            (True, f'model {self.models}/ggml-large-v3-turbo-q5_0.bin SHA-256'),
            (True, f'model {self.models}/ggml-silero-v6.2.0.bin SHA-256'),
        ])

    def test_an_installed_release_checks_its_own_files_and_a_checkout_says_nothing(self):
        sums = {**self.model('large-v3-turbo-q5_0', b'turbo'), **self.model('silero-v6.2.0', b'vad')}
        release = self.root / 'release'
        (release / 'bridge').mkdir(parents=True)
        (release / 'bridge' / 'bridge.py').write_text('print()\n')

        def lines():
            return [(ok, line) for ok, line in b.install_problems(self.settings, sums=sums, pack=release,
                                                                    which=lambda name: '/usr/bin/' + name,
                                                                    platform='linux', version=lambda python: (3, 14))
                    if 'release' in line or 'SHA256SUMS' in line]
        self.assertEqual(lines(), [])  # a development checkout
        (release / 'RELEASE').write_text('commit=0\n')
        self.assertEqual(lines(), [(False, 'release files: SHA256SUMS is missing or unreadable')])
        (release / 'SHA256SUMS').write_text(b.release_sums(release))
        digest = hashlib.sha256(b'print()\n').hexdigest()
        self.assertEqual(b.release_sums(release), f'{digest}  bridge/bridge.py\n')
        self.assertEqual(lines(), [(True, 'release files match SHA256SUMS')])
        (release / 'bridge' / 'bridge.py').write_text('print(1)\n')
        self.assertEqual(lines(), [(False, 'release files: bridge/bridge.py does not match SHA256SUMS')])
        (release / 'bridge' / 'bridge.py').write_text('print()\n')
        (release / 'vendor').mkdir()
        (release / 'vendor' / 'requests.py').write_text('print()\n')
        self.assertEqual(lines(), [(False, 'release files: vendor/requests.py is not listed in SHA256SUMS')])
        (release / 'vendor' / 'requests.py').unlink()
        cache = release / 'bridge' / '__pycache__'
        cache.mkdir()
        (cache / 'bridge.cpython-314.pyc').write_bytes(b'cache')
        self.assertEqual(lines(), [(True, 'release files match SHA256SUMS')])  # beside a listed source
        for name in ('requests.cpython-314.pyc', 'bridge.py'):
            with self.subTest(cache=name):
                (cache / name).write_bytes(b'x')
                self.assertEqual(lines(), [(False, f'release files: bridge/__pycache__/{name} is not listed in SHA256SUMS')])
                (cache / name).unlink()
        for listed in ('0' * 64 + '  ../outside', '0' * 64 + '  /etc/passwd', 'not a sum line'):
            with self.subTest(listed=listed):
                (release / 'SHA256SUMS').write_text(listed + '\n')
                self.assertEqual(lines(), [(False, 'release files: SHA256SUMS line 1 is malformed')])
        self.assertNotIn('release', ' '.join(line for _, line in b.install_problems(
            self.settings, sums=sums, which=lambda name: '/usr/bin/' + name, platform='linux',
            version=lambda python: (3, 14))))  # this checkout itself

    def source_settings(self, top, transcribe='', yardr='', extra='', **replace):
        """Settings under top, with lines swapped as write_settings does, text
        added to its [yardr] table (a hook_secret_file there replaces its own),
        a [transcribe] table and extra tables."""
        top.mkdir(parents=True, exist_ok=True)
        path = write_settings(top, **replace)
        text = path.read_text()
        if 'hook_secret_file' in yardr:
            text = re.sub(r'hook_secret_file = "[^"]*"\n', '', text)
        text = text.replace('[yardr]\n', '[yardr]\n' + yardr)
        path.write_text(text + (f'\n[transcribe]\n{transcribe}' if transcribe else '') + extra)
        return path

    def test_source_dependencies_name_each_inventory_entry_with_its_remedy(self):
        source = Path(os.path.realpath(self.root / 'source'))
        source.mkdir(mode=0o700)
        top = self.root / 'outside'
        copy = 'copy it out of the source, point {} at the copy, then run install.sh again'
        move = 'stop the service, move it out of the source, point {} at the new path, then run install.sh again'
        interpreter = ('point python at an interpreter outside the source, such as /usr/bin/python3 '
                       'or a venv made outside it, then run install.sh again')
        program = source / 'program'
        program.write_text('#!/bin/sh\n')
        program.chmod(0o700)
        (source / 'yard').mkdir(mode=0o700)
        outside_lock = f'"{top / "locks"}"'
        cases = [
            ('python', source / 'python', interpreter, dict(python=f'"{source}/python"')),
            ('token_file', source / 'token', move, dict(token_file=f'"{source}/token"', lock_dir=outside_lock)),
            ('callback_secret_file', source / 'callback', move, dict(callback_secret_file=f'"{source}/callback"')),
            ('yardr.hook_secret_file', source / 'hook', move, dict(yardr=f'hook_secret_file = "{source}/hook"\n')),
            ('lock_dir', source / 'locks', move, dict(lock_dir=f'"{source}/locks"')),
            ('state_dir', source / 'state', move, dict(state_dir=f'"{source}/state"')),
            ('health_file', source / 'health', move, dict(health_file=f'"{source}/health"')),
            ('yardr.home', source / 'yard', move, dict(yardr=f'home = "{source}/yard"\noperator = "pat"\n')),
            ('[[command]] deploy handler', program, copy,
             dict(extra=f'\n[[command]]\nname = "deploy"\ndescription = "Deploy"\nhandler = "{program}"\n')),
            ('handler_write_paths entry 1', source / 'out', move, dict(handler_write_paths=f'["{source}/out"]')),
            ('transcribe.gpu_check', program, copy, dict(transcribe=f'method = "auto"\ngpu_check = "{program}"\n')),
            ('transcribe.whisper_cli', source / 'cli', copy, dict(transcribe=f'whisper_cli = "{source}/cli"\n')),
            ('transcribe.models_dir', source / 'models', copy, dict(transcribe=f'models_dir = "{source}/models"\n')),
        ]
        for name, value, remedy, replace in cases:
            with self.subTest(entry=name):
                path = self.source_settings(top / re.sub(r'[^a-z_.]', '', name), **replace)
                settings = b.load_settings(path)
                self.assertEqual(b.source_dependencies(str(source), settings), [
                    f'settings {name} {value} is inside the source {source}: {remedy.format(name)}'])
        path = self.source_settings(top / 'none')
        self.assertEqual(b.source_dependencies(str(source), b.load_settings(path)), [])
        path = self.source_settings(top / 'token-and-lock', token_file=f'"{source}/private/token"')
        self.assertEqual(b.source_dependencies(str(source), b.load_settings(path)), [
            f'settings token_file {source}/private/token is inside the source {source}: {move.format("token_file")}',
            f'settings lock_dir {source}/private is inside the source {source}: {move.format("lock_dir")}'])
        moved = top / 'elsewhere.toml'
        moved.write_bytes(path.read_bytes())
        inside = source / 'telegram-bridge.toml'
        inside.write_bytes(self.source_settings(top / 'settings-file').read_bytes())
        inside.chmod(0o600)
        self.assertEqual(b.source_dependencies(str(source), b.load_settings(inside)), [
            f'the settings file {inside} is inside the source {source}: copy it out of the source (mode 600), '
            'then run install.sh --settings <the copy>'])
        # The running pack: its built-in gpu_check moves with the release; a configured one is named.
        pack = str(b.PACK_DIR)
        gpu_check = b.PACK_DIR / 'transcriber/gpu_check.py'
        built_in = self.source_settings(top / 'built-in', python='"/usr/bin/python3"',
                                        transcribe='enabled = true\nmethod = "auto"\n')
        self.assertEqual(b.source_dependencies(pack, b.load_settings(built_in)), [])
        named = self.source_settings(top / 'configured', python='"/usr/bin/python3"',
                                     transcribe=f'method = "auto"\ngpu_check = "{gpu_check}"\n')
        self.assertEqual(b.source_dependencies(pack, b.load_settings(named)), [
            f'settings transcribe.gpu_check {gpu_check} is inside the source {pack}: '
            + copy.format('transcribe.gpu_check')])
        self.assertEqual(b.source_dependencies(pack, root=Path(pack) / 'instance'), [
            f'the root {pack}/instance is inside the source {pack}; choose one outside it'])

    def test_source_dependencies_follow_links_at_every_component(self):
        tree = Path(os.path.realpath(self.root / 'tree'))
        source, out = tree / 'source', tree / 'out'
        source.mkdir(parents=True, mode=0o700)
        out.mkdir()
        for venv in (source / '.venv', out / 'venv-ok'):
            made = subprocess.run([sys.executable, '-m', 'venv', '--without-pip', str(venv)],
                                  capture_output=True, text=True, timeout=120)
            if made.returncode != 0:
                self.skipTest(f'python -m venv failed: {made.stderr.strip()[-200:]}')
        python = source / '.venv/bin/python'
        (out / 'py').symlink_to(python)
        (out / 'rel').symlink_to('../source/.venv/bin/python')
        (out / 'venv').symlink_to(source / '.venv')
        (source / 'hop').symlink_to(out / 'venv-ok')
        (out / 'back').symlink_to(source / 'hop')
        (self.root / 'alias').symlink_to(tree)
        chain = out / 'c0'
        chain.write_text('')
        for index in range(1, 42):
            (out / f'c{index}').symlink_to(chain)
            chain = out / f'c{index}'
        remedy = ('point python at an interpreter outside the source, such as /usr/bin/python3 '
                  'or a venv made outside it, then run install.sh again')

        def named(value):
            path = self.source_settings(self.root / 'settings', python=f'"{value}"')
            return b.source_dependencies(str(source), b.load_settings(path))

        through = f'depends on the source {source} through'
        cases = [
            (python, f'settings python {python} is inside the source {source}: {remedy}'),
            (out / 'py', f'settings python {out}/py {through} {out}/py -> {python}: {remedy}'),
            (out / 'rel', f'settings python {out}/rel {through} {out}/rel -> ../source/.venv/bin/python: {remedy}'),
            (out / 'venv/bin/python', f'settings python {out}/venv/bin/python {through} {out}/venv -> '
                                      f'{source}/.venv: {remedy}'),
            (out / 'back/bin/python', f'settings python {out}/back/bin/python {through} {out}/back -> '
                                      f'{source}/hop: {remedy}'),
            (self.root / 'alias/source/.venv/bin/python',
             f'settings python {self.root}/alias/source/.venv/bin/python is inside the source {source}: {remedy}'),
            (out / 'c41', f'settings python {out}/c41 cannot be resolved (more than 40 links)'),
        ]
        for value, line in cases:
            with self.subTest(value=str(value)):
                self.assertEqual(named(value), [line])
        for value in (out / 'venv-ok/bin/python', sys.executable, out / 'missing/bin/python'):
            with self.subTest(value=str(value)):
                self.assertEqual(named(value), [])

    def test_an_installed_release_checks_the_settings_against_its_source(self):
        source = Path(os.path.realpath(self.root / 'source'))
        source.mkdir(mode=0o700)
        release = self.root / 'release'
        (release / 'bridge').mkdir(parents=True)
        (release / 'bridge' / 'bridge.py').write_text('print()\n')
        (release / 'SHA256SUMS').write_text(b.release_sums(release))

        def lines(settings=self.settings):
            return [(ok, line) for ok, line in b.install_problems(settings, sums={}, pack=release,
                                                                    which=lambda name: '/usr/bin/' + name,
                                                                    platform='linux', version=lambda python: (3, 14))
                    if 'the source' in line or 'RELEASE' in line]
        (release / 'RELEASE').write_text(f'commit=0\nsource={source}\ninstalled=2026-11-08T00:00:00Z\n')
        self.assertEqual(lines(), [(True, f'nothing the settings name depends on the source {source}')])
        inside = self.source_settings(self.root / 'inside', state_dir=f'"{source}/state"')
        self.assertEqual(lines(b.load_settings(inside)), [
            (False, f'settings state_dir {source}/state is inside the source {source}: stop the service, move it '
                    'out of the source, point state_dir at the new path, then run install.sh again')])
        (release / 'RELEASE').write_text('commit=0\ninstalled=2026-11-08T00:00:00Z\n')
        self.assertEqual(lines(), [(False, 'RELEASE names no source (a source= line)')])
        self.assertEqual([line for _, line in b.install_problems(
            self.settings, sums={}, which=lambda name: '/usr/bin/' + name, platform='linux',
            version=lambda python: (3, 14)) if 'the source' in line or 'RELEASE' in line], [])  # a checkout

    def test_an_old_or_unreadable_interpreter_fails_on_linux_and_darwin(self):
        sums = {**self.model('large-v3-turbo-q5_0', b'turbo'), **self.model('silero-v6.2.0', b'vad')}
        line = (f'python {self.settings.python} is not Python 3.11 or later '
                '(set python in the settings to a 3.11+ interpreter)')
        for platform in ('linux', 'darwin'):
            for version in (lambda python: (3, 9), lambda python: None):
                with self.subTest(platform=platform, version=version):
                    results = b.install_problems(
                        self.settings, which=lambda n, path=None: '/usr/bin/' + n, sums=sums,
                        platform=platform, version=version, home=self.root / 'home')
                    self.assertIn((False, line), results)
        results = b.install_problems(self.settings, which=lambda n: '/usr/bin/' + n, sums=sums,
                                     platform='linux', version=lambda python: (3, 11))
        self.assertIn((True, f'python {self.settings.python}'), results)

    def test_a_non_executable_interpreter_keeps_the_existing_line(self):
        sums = {**self.model('large-v3-turbo-q5_0', b'turbo'), **self.model('silero-v6.2.0', b'vad')}
        python = self.root / 'python'
        python.write_text('#!/bin/sh\n')
        settings = b.Settings(**dict(self.settings.__dict__, python=python))
        results = b.install_problems(settings, which=lambda n: '/usr/bin/' + n, sums=sums,
                                     platform='linux', version=lambda path: (3, 14))
        self.assertIn((False, f'python {python}'), results)

    def test_darwin_checks(self):
        sums = {**self.model('large-v3-turbo-q5_0', b'turbo'), **self.model('silero-v6.2.0', b'vad')}
        home = self.root / 'home'
        calls = []

        def recording_which(name, path=None):
            calls.append((name, path))
            return '/usr/bin/' + name

        handler = self.root / 'handler'
        handler.write_text('#!/bin/sh\n')
        handler.chmod(0o755)
        out = self.root / 'out'
        out.mkdir()
        state = home / 'Documents' / 'state'
        state.mkdir(parents=True)
        transcribe = dict(self.settings.transcribe,
                          env=dict(self.settings.transcribe['env'], VOICE_TRANSCRIBE_CPUS='0-3'))
        settings = b.Settings(**dict(
            self.settings.__dict__, transcribe=transcribe, state_dir=state,
            config=dict(self.settings.config, commands=(dict(name='deploy', handler=str(handler)),)),
            handler_write_paths=(out,)))
        results = b.install_problems(settings, which=recording_which, sums=sums, platform='darwin',
                                     version=lambda python: (3, 14), home=home)
        self.assertIn(('curl', b.LAUNCHD_PATH), calls)
        self.assertIn(('ffmpeg', b.LAUNCHD_PATH), calls)
        self.assertIn((False, 'transcribe.cpus is not supported on macOS (no CPU affinity); remove it'), results)
        self.assertIn((True, f"command /deploy handler {handler} "
                             "(runs with your user's full rights: no sandbox on macOS)"), results)
        self.assertIn((True, f'handler write path {out}'), results)
        self.assertIn((True, f'WARN  handler write path {out} is not enforced on macOS (no sandbox)'), results)
        self.assertIn((True, f'WARN  state directory {state} is in a folder macOS privacy protection '
                             'blocks for a launchd agent; move it'), results)

    def test_wsl_checks(self):
        import getpass
        sums = {**self.model('large-v3-turbo-q5_0', b'turbo'), **self.model('silero-v6.2.0', b'vad')}
        user = getpass.getuser()
        systemd = self.root / 'systemd'
        systemd.mkdir()
        linger = self.root / 'linger'
        linger.mkdir()
        (linger / user).write_text('')
        release = self.root / 'osrelease'
        release.write_text('microsoft-standard-WSL2\n')
        base = dict(which=lambda n: '/usr/bin/' + n, sums=sums, platform='wsl',
                    version=lambda python: (3, 14), osrelease=str(release),
                    systemd_dir=str(systemd), linger_dir=str(linger), home=self.root / 'home')
        results = b.install_problems(self.settings, **base)
        self.assertEqual(results[0], (True, 'WSL 2 with systemd'))
        results = b.install_problems(self.settings, **dict(base, systemd_dir=str(self.root / 'missing-systemd')))
        self.assertIn((False, 'systemd is not running in this WSL distribution: add [boot] systemd=true '
                              'to /etc/wsl.conf, run wsl --shutdown in Windows, reopen the distribution'), results)
        release.write_text('4.4.0-19041-Microsoft\n')
        results = b.install_problems(self.settings, **base)
        self.assertIn((False, 'WSL 1 is not supported: convert the distribution with '
                              'wsl --set-version <distro> 2'), results)
        release.write_text('microsoft-standard-WSL2\n')
        settings = b.Settings(**dict(self.settings.__dict__, token_file=Path('/mnt/c/token')))
        results = b.install_problems(settings, **base)
        self.assertIn((False, 'bot token file /mnt/c/token is on a Windows drive (/mnt/...): '
                              'keep it in the Linux file system'), results)
        empty_linger = self.root / 'empty-linger'
        empty_linger.mkdir()
        results = b.install_problems(self.settings, **dict(base, linger_dir=str(empty_linger)))
        self.assertIn((True, f'WARN  no linger for {user}: the bridge stops when your last WSL session ends '
                             f'(loginctl enable-linger {user})'), results)

    def test_check_install_prints_the_warn_line_and_exits_0(self):
        import io
        from unittest.mock import patch
        from test_bridge import run_main
        path = write_settings(self.root, python=f'"{sys.executable}"')
        path.write_text(path.read_text() + '\n[transcribe]\nenabled = true\nmodel = "small.en"\n')
        warn = (True, 'WARN  transcribe model small.en is English-only (*.en) but transcribe.language is "ru"')
        with patch.object(b, 'install_problems', return_value=[(True, 'python'), warn]), \
                patch('sys.stdout', new_callable=io.StringIO) as out:
            run_main(self, 'bridge.py', '--settings', str(path), 'check-install')
        self.assertIn('WARN  transcribe model small.en', out.getvalue())
        self.assertNotIn('ok    WARN', out.getvalue())

    def test_the_queue_directory_is_created_by_install_or_must_be_owner_only(self):
        sums = {**self.model('large-v3-turbo-q5_0', b'turbo'), **self.model('silero-v6.2.0', b'vad')}
        lines = [line for ok, line in b.install_problems(self.settings, which=lambda n: '/usr/bin/' + n, sums=sums,
                                                         platform='linux')]
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
        self.assertIn('transcribe model small is not one of the supported models: large-v3-turbo-q5_0',
                      [line for ok, line in b.install_problems(
                          b.Settings(**dict(self.settings.__dict__, model='small')), which=lambda n: n, sums=sums,
                          platform='linux')
                       if not ok])

    def test_a_world_readable_token_fails_and_is_never_printed(self):
        token = self.settings.token_file
        token.chmod(0o644)
        sums = {**self.model('large-v3-turbo-q5_0', b'turbo'), **self.model('silero-v6.2.0', b'vad')}
        results = b.install_problems(self.settings, which=lambda n: n, sums=sums, platform='linux')
        self.assertIn((False, 'bot token file must be an owner-only regular file'), results)
        self.assertNotIn('TESTTOKEN', repr(results))

    def test_the_hook_secret_must_be_owner_only_and_is_never_printed(self):
        sums = {**self.model('large-v3-turbo-q5_0', b'turbo'), **self.model('silero-v6.2.0', b'vad')}
        hook = self.settings.hook_secret_file
        hook.write_text('HOOKSECRETVALUE\n')
        hook.chmod(0o600)
        results = b.install_problems(self.settings, which=lambda n: n, sums=sums, platform='linux')
        self.assertIn((True, 'hook secret file is owner-only'), results)
        hook.chmod(0o644)
        results = b.install_problems(self.settings, which=lambda n: n, sums=sums, platform='linux')
        self.assertIn((False, 'hook secret file must be an owner-only regular file'), results)
        self.assertNotIn('HOOKSECRETVALUE', repr(results))

    def test_a_model_without_a_recorded_sum_fails_naming_it(self):
        sums = self.model('large-v3-turbo-q5_0', b'turbo')
        self.assertEqual(self.failures(sums=sums),
                         ['model ggml-silero-v6.2.0.bin: no recorded SHA-256 in the pack'])

    def test_install_problems_uses_the_injected_sums_for_supported_models(self):
        sums = {**self.model('small.en', b'small'), **self.model('silero-v6.2.0', b'vad')}
        self.assertEqual(self.failures(sums=sums),
                         ['transcribe model large-v3-turbo-q5_0 is not one of the supported models: small.en'])

    def test_an_unlisted_model_fails_naming_the_supported_models(self):
        sums = {**self.model('large-v3-turbo-q5_0', b'turbo'), **self.model('silero-v6.2.0', b'vad')}
        settings = b.Settings(**dict(self.settings.__dict__, model='tiny'))
        self.assertEqual(b.supported_models(),
                         ['large-v3-turbo-q5_0', 'small.en', 'small.en-q5_1', 'base.en', 'small'])
        self.assertEqual([line for ok, line in b.install_problems(settings, which=lambda n: '/usr/bin/' + n,
                                                                  sums=sums, platform='linux') if not ok],
                         ['transcribe model tiny is not one of the supported models: large-v3-turbo-q5_0'])

    def test_an_unlisted_model_present_on_disk_warns_and_passes(self):
        sums = {**self.model('large-v3-turbo-q5_0', b'turbo'), **self.model('silero-v6.2.0', b'vad')}
        (self.models / 'ggml-tiny.bin').write_bytes(b'tiny-model')
        settings = b.Settings(**dict(self.settings.__dict__, model='tiny'))
        results = b.install_problems(settings, which=lambda n: '/usr/bin/' + n, sums=sums, platform='linux')
        self.assertEqual([line for ok, line in results if not ok], [])
        warns = [line for ok, line in results if line.startswith('WARN')]
        self.assertEqual(warns, ['WARN  model ggml-tiny.bin is not in transcriber/models.sha256; skipping hash check'])
        # An empty unlisted file does not pass.
        (self.models / 'ggml-tiny.bin').write_bytes(b'')
        results = b.install_problems(settings, which=lambda n: '/usr/bin/' + n, sums=sums, platform='linux')
        self.assertEqual([line for ok, line in results if not ok],
                         ['transcribe model tiny is not one of the supported models: large-v3-turbo-q5_0'])

    def test_a_listed_vad_name_as_model_fails_as_unsupported(self):
        sums = {**self.model('large-v3-turbo-q5_0', b'turbo'), **self.model('silero-v6.2.0', b'vad')}
        settings = b.Settings(**dict(self.settings.__dict__, model='silero-v6.2.0'))
        results = b.install_problems(settings, which=lambda n: '/usr/bin/' + n, sums=sums, platform='linux')
        self.assertEqual([line for ok, line in results if not ok],
                         ['transcribe model silero-v6.2.0 is not one of the supported models: '
                          'large-v3-turbo-q5_0'])
        self.assertFalse(any(line.startswith('WARN') for ok, line in results))

    def test_a_listed_model_with_a_mismatched_hash_still_fails(self):
        sums = {**self.model('large-v3-turbo-q5_0', b'turbo'), 'ggml-silero-v6.2.0.bin': '0' * 64}
        self.model('silero-v6.2.0', b'vad')
        # The main model is listed, so its hash is checked even though it passes;
        # tamper with it and the check must fail, not warn.
        (self.models / 'ggml-large-v3-turbo-q5_0.bin').write_bytes(b'tampered')
        self.assertIn(f'model {self.models}/ggml-large-v3-turbo-q5_0.bin SHA-256', self.failures(sums=sums))

    def test_the_pack_records_the_handed_over_model_sums(self):
        sums = b.recorded_sums()
        self.assertEqual(sums['ggml-large-v3-turbo-q5_0.bin'],
                         '394221709cd5ad1f40c46e6031ca61bce88931e6e088c188294c6d5a55ffa7e2')
        self.assertEqual(sums['ggml-silero-v6.2.0.bin'],
                         '2aa269b785eeb53a82983a20501ddf7c1d9c48e33ab63a41391ac6c9f7fb6987')
        self.assertEqual(sums['ggml-small.bin'], '1be3a9b2063867b937e64e2ec7483364a79917e157fa98c5d94b5c1fffea987b')


def git_env(home=None):
    """The environment for a fixture's git: no inherited GIT_* variable or
    system config, and the fixture's own home when it has one."""
    env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
    env['GIT_CONFIG_NOSYSTEM'] = '1'
    if home is not None:
        env['HOME'] = str(home)
    return env


def commit_source(source, home, message):
    """Commit every change in the fixture source; return the commit."""
    env = git_env(home)
    for argv in ((['init', '-q'] if not (source / '.git').exists() else []), ['add', '-A'],
                 ['-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', message]):
        if argv:
            subprocess.run(['git', '-C', str(source), *argv], env=env, check=True, capture_output=True)
    return subprocess.run(['git', '-C', str(source), 'rev-parse', 'HEAD'], env=env, check=True,
                          capture_output=True, text=True).stdout.strip()


def make_source(case, source, home):
    """A git checkout of this pack's tracked files as they are in the working
    tree, committed once; the test is skipped without git or a checkout."""
    if not shutil.which('git'):
        case.skipTest('git not on PATH')
    listed = subprocess.run(['git', '-C', str(PACK), 'ls-files', '-z'], env=git_env(), capture_output=True)
    if listed.returncode != 0:
        case.skipTest('the pack is not a git checkout')
    for name in filter(None, listed.stdout.decode().split('\0')):
        if (PACK / name).is_file():
            (source / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(PACK / name, source / name)
    return commit_source(source, home, 'fixture')


def tree_digest(*tops):
    """{path: SHA-256 and mode, or link target} of every file under the tops,
    following links (current, previous) into the releases they name."""
    found = {}
    for top in map(Path, tops):
        if top.is_symlink():
            found[str(top)] = 'link ' + os.readlink(top)
        if top.is_file():
            found[str(top)] = hashlib.sha256(top.read_bytes()).hexdigest() + oct(top.stat().st_mode)
        for directory, dirs, files in os.walk(top, followlinks=True):
            for name in dirs + files:
                path = Path(directory) / name
                if path.is_symlink():
                    found[str(path)] = 'link ' + os.readlink(path)
                elif path.is_file():
                    found[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest() + oct(path.stat().st_mode)
                elif path.is_dir():
                    found[str(path)] = 'dir ' + oct(path.stat().st_mode)
    return found


class InstallScriptTests(unittest.TestCase):
    """install.sh from a fixture checkout of this pack, against a fake home:
    the unit may write only its state and lock directories (and the queue),
    so the script itself mints a missing callback or hook secret kept
    anywhere else before the unit's first start, and the service runs the
    instance's own release copy."""

    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.home = self.root / 'home'
        self.home.mkdir()
        self.source = self.root / 'source'
        self.commit = make_source(self, self.source, self.home)
        self.secret = self.root / 'secrets' / 'telegram-callback-secret'  # outside state_dir and lock_dir
        self.path = write_settings(self.root, python=f'"{sys.executable}"', callback_secret_file=f'"{self.secret}"')
        self.instance = self.home / '.local/share/yardr-telegram/test-city-telegram'

    def install(self, *args, platform='linux', extra_env=None, settings=None):
        env = {k: v for k, v in git_env(self.home).items() if not k.startswith('GC_') and k != b.SETTINGS_ENV}
        env.update(XDG_CONFIG_HOME=str(self.home / '.config'), TELEGRAM_PACK_PLATFORM=platform)
        if extra_env:
            env.update(extra_env)
        return subprocess.run(['sh', str(self.source / 'install.sh'), '--settings', str(settings or self.path), *args],
                              env=env, capture_output=True, text=True, timeout=120)

    def next_line(self, instance=None, unit='test-city-telegram.service'):
        launcher = (instance or self.instance) / 'bin/telegram-bridge'
        return (f'next: {launcher} probe; systemctl --user daemon-reload; systemctl --user enable {unit}; '
                f'systemctl --user restart {unit}; {launcher} set-commands (yardr/SETUP.md, step 9)')

    def test_install_creates_a_missing_callback_secret_the_unit_could_not(self):
        if not shutil.which('curl'):
            self.skipTest('curl not on PATH')
        dry = self.install('--dry-run')
        self.assertEqual(dry.returncode, 0, dry.stderr)
        self.assertEqual(dry.stdout.strip().splitlines()[-1], self.next_line())
        self.assertIn(f'would stage {self.commit} as {self.instance}/releases/{self.commit}\n', dry.stdout)
        self.assertIn(f'would create callback secret {self.secret} (mode 600)', dry.stdout)
        self.assertIn('ok    callback secret file is created by install.sh', dry.stdout)
        self.assertFalse(self.secret.parent.exists())  # a dry run writes nothing
        self.assertEqual(sorted(p.name for p in self.home.iterdir()), [])

        done = self.install()
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn(f'created callback secret {self.secret} (mode 600)', done.stdout)
        self.assertIn('ok    callback secret file is owner-only', done.stdout)
        self.assertIn('ok    release files match SHA256SUMS', done.stdout)  # the staged copy's own check
        secret = self.secret.read_text()
        self.assertGreaterEqual(len(secret), 32)
        self.assertEqual(self.secret.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.secret.parent.stat().st_mode & 0o777, 0o700)
        self.assertNotIn(secret, done.stdout + done.stderr)
        unit = (self.home / '.config/systemd/user/test-city-telegram.service').read_text()
        self.assertIn(f'ReadWritePaths={self.root / "state"} {self.root / "private"}\n', unit)
        self.assertIn(f'ExecStart={sys.executable} {self.instance}/current/bridge/bridge.py --settings {self.path} run\n',
                      unit)
        self.assertEqual(os.readlink(self.instance / 'current'), f'releases/{self.commit}')
        self.assertFalse((self.instance / 'previous').exists())
        self.assertEqual((self.instance / 'bin/telegram-bridge').read_text(),
                         f'#!/bin/sh\nexec {sys.executable} {self.instance}/current/bridge/bridge.py '
                         f'--settings {self.path} "$@"\n')
        self.assertEqual((self.instance / 'bin/voice-transcribe').read_text(),
                         f'#!/bin/sh\nexec {sys.executable} -B {self.instance}/current/transcriber/voice_transcribe.py "$@"\n')
        release = self.instance / 'releases' / self.commit
        self.assertEqual((release / 'RELEASE').read_text().splitlines()[:2],
                         [f'commit={self.commit}', f'source={self.source}'])
        self.assertEqual(list(release.rglob('__pycache__')), [])
        self.assertNotIn('__pycache__/', (release / 'SHA256SUMS').read_text())
        self.assertIsNone(b.release_mismatch(release))
        self.assertEqual([p.name for p in (self.instance / 'releases').iterdir()], [self.commit])  # no stage left
        self.assertEqual(done.stdout.strip().splitlines()[-1], self.next_line())

        caches = ['__pycache__/module.cpython-314.pyc', 'bridge/__pycache__/bridge.cpython-314.pyc',
                  'transcriber/__pycache__/adapter.cpython-314.pyc']
        for name in caches:
            path = self.source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'cache')
        ignored = subprocess.run(['git', '-C', str(self.source), 'check-ignore', '--stdin'],
                                 input='\n'.join(caches) + '\n', env=git_env(self.home),
                                 capture_output=True, text=True, timeout=30)
        self.assertEqual(ignored.returncode, 0, ignored.stderr)
        self.assertEqual(ignored.stdout.splitlines(), caches)

        again = self.install()  # idempotent: the secret and the release are kept, nothing is rewritten
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertNotIn('created callback secret', again.stdout)
        self.assertIn(f'release {self.commit} reused from {release}', again.stdout)
        self.assertIn(f'release releases/{self.commit} is current', again.stdout)
        self.assertNotIn('wrote', again.stdout)
        self.assertEqual(self.secret.read_text(), secret)

    def test_install_mints_a_missing_hook_secret_beside_the_callback_secret(self):
        if not shutil.which('curl'):
            self.skipTest('curl not on PATH')
        hook = self.root / 'yard-secrets' / 'hook-secret'  # outside state_dir and lock_dir too
        self.path.write_text(self.path.read_text().replace(str(self.root / 'private' / 'hook-secret'), str(hook)))
        dry = self.install('--dry-run')
        self.assertEqual(dry.returncode, 0, dry.stderr)
        self.assertIn(f'would create callback secret {self.secret} (mode 600)\n'
                      f'would create hook secret {hook} (mode 600)\n', dry.stdout)
        self.assertIn('ok    hook secret file is created by install.sh', dry.stdout)
        self.assertFalse(hook.parent.exists())  # a dry run writes nothing
        self.assertFalse(self.secret.parent.exists())

        done = self.install()
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn(f'created callback secret {self.secret} (mode 600)', done.stdout)
        self.assertIn(f'created hook secret {hook} (mode 600)', done.stdout)
        self.assertIn('ok    hook secret file is owner-only', done.stdout)
        secret = hook.read_text()
        self.assertGreaterEqual(len(secret), 32)
        self.assertNotEqual(secret, self.secret.read_text())
        self.assertEqual(hook.stat().st_mode & 0o777, 0o600)
        self.assertEqual(hook.parent.stat().st_mode & 0o777, 0o700)
        self.assertNotIn(secret, done.stdout + done.stderr)
        unit = (self.home / '.config/systemd/user/test-city-telegram.service').read_text()
        self.assertIn(f' -{hook}\n', unit)  # read-only inside the unit

        again = self.install()  # idempotent: the secret is kept
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertNotIn('created hook secret', again.stdout)
        self.assertEqual(hook.read_text(), secret)

    def test_install_keeps_a_hook_secret_the_yard_already_has(self):
        if not shutil.which('curl'):
            self.skipTest('curl not on PATH')
        hook = self.root / 'private' / 'hook-secret'
        hook.write_text('the-secret-yardr-hook-add-was-given\n')
        hook.chmod(0o600)
        for args in (('--dry-run',), ()):
            with self.subTest(args=args):
                result = self.install(*args)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertNotIn('hook secret ' + str(hook), result.stdout)
                self.assertIn('ok    hook secret file is owner-only', result.stdout)
                self.assertNotIn('the-secret-yardr-hook-add-was-given', result.stdout + result.stderr)
                self.assertEqual(hook.read_text(), 'the-secret-yardr-hook-add-was-given\n')

    def test_install_on_darwin_writes_the_launchagent_plist(self):
        if not shutil.which('curl', path=b.LAUNCHD_PATH):
            self.skipTest('curl not on the launchd PATH')
        plist = self.home / 'Library/LaunchAgents/test-city-telegram.plist'
        launcher = self.instance / 'bin/telegram-bridge'
        next_line = (f'next: {launcher} probe; '
                     'launchctl bootout gui/$(id -u)/test-city-telegram 2>/dev/null; '
                     f'launchctl bootstrap gui/$(id -u) {plist}; '
                     f'{launcher} set-commands (yardr/SETUP.md, step 9)')
        dry = self.install('--dry-run', platform='darwin')
        self.assertEqual(dry.returncode, 0, dry.stderr)
        self.assertIn(f'would write {plist}:', dry.stdout)
        self.assertEqual(dry.stdout.strip().splitlines()[-1], next_line)
        self.assertFalse(plist.exists())
        self.assertFalse(self.secret.parent.exists())
        self.assertEqual(sorted(p.name for p in self.home.iterdir()), [])

        done = self.install(platform='darwin')
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn(f'wrote {plist}', done.stdout)
        self.assertEqual(done.stdout.strip().splitlines()[-1], next_line)
        self.assertEqual(plist.stat().st_mode & 0o777, 0o644)
        settings = b.load_settings(self.path)
        expected = b.render_service(settings, platform='darwin', home=self.home,
                                    bridge_path=self.instance / 'current/bridge/bridge.py')
        self.assertEqual(plistlib.loads(plist.read_bytes()), plistlib.loads(expected.encode()))
        self.assertFalse((self.home / '.config/systemd').exists())

        again = self.install(platform='darwin')
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertIn(f'unit {plist} is current', again.stdout)

    def test_install_refuses_python3_older_than_311_before_creating_anything(self):
        fake_bin = self.root / 'fake-bin'
        fake_bin.mkdir()
        fake = fake_bin / 'python3'
        fake.write_text('#!/bin/sh\n'
                        'case "$*" in *version_info*) exit 1;; esac\n'
                        f'exec "{sys.executable}" "$@"\n')
        fake.chmod(0o755)
        result = self.install(extra_env={'PATH': f'{fake_bin}:{os.environ.get("PATH", "")}'})
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(result.stderr.strip(),
                         'install.sh: python3 on PATH must be Python 3.11 or later (python3 --version)')
        self.assertEqual(sorted(p.name for p in self.home.iterdir()), [])
        self.assertFalse(self.secret.exists())

    def test_install_on_wsl_matches_linux(self):
        # check-install's WSL host probes are covered in InstallCheckTests
        # with injected paths; this host is not WSL, so stub that one step
        # and compare install.sh's own unit and next-line output.
        fake_bin = self.root / 'fake-bin'
        fake_bin.mkdir()
        fake = fake_bin / 'python3'
        fake.write_text('#!/bin/sh\n'
                        'for a in "$@"; do [ "$a" = check-install ] && exit 0; done\n'
                        f'exec "{sys.executable}" "$@"\n')
        fake.chmod(0o755)
        extra = {'PATH': f'{fake_bin}:{os.environ.get("PATH", "")}'}
        self.path.write_text(self.path.read_text().replace(f'python = "{sys.executable}"', f'python = "{fake}"'))
        linux = self.install('--dry-run', platform='linux', extra_env=extra)
        self.assertEqual(linux.returncode, 0, linux.stderr)
        self.assertEqual(linux.stdout.strip().splitlines()[-1], self.next_line())
        wsl = self.install('--dry-run', platform='wsl', extra_env=extra)
        self.assertEqual(wsl.returncode, 0, wsl.stderr)
        self.assertEqual(wsl.stdout, linux.stdout)
        self.assertEqual(wsl.stderr, linux.stderr)

    def test_install_with_unloadable_settings_creates_nothing(self):
        path = write_settings(self.root, python=f'"{sys.executable}"',
                              callback_secret_file=f'"{self.secret}"', language='"ru"')
        path.write_text(path.read_text() + '\n[transcribe]\nenabled = true\n')
        result = self.install()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stderr.strip(), 'settings: unknown key language')
        self.assertFalse(self.secret.exists())
        self.assertFalse((self.root / 'private' / 'hook-secret').exists())
        self.assertFalse((self.home / '.local/state/voice-transcribe').exists())
        self.assertFalse((self.home / '.config/systemd/user/test-city-telegram.service').exists())
        self.assertFalse((self.home / '.local/bin/voice-transcribe').exists())
        self.assertEqual(sorted(p.name for p in self.home.iterdir()), [])


class InstanceTests(unittest.TestCase):
    """Instances installed from one fixture checkout under one fake home,
    each with its own settings, bot, port, secrets, state, root, unit and
    native yard home: nothing done to one changes another."""

    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.home = self.root / 'home'
        self.home.mkdir()
        self.source = self.root / 'source'
        self.first = make_source(self, self.source, self.home)
        self.bin = self.root / 'bin'
        from test_bridge import write_fake_curl
        write_fake_curl(self.bin, f'#!{sys.executable}\nimport json,sys\nfrom pathlib import Path\n'
                        'with Path(__file__).with_name("requests.jsonl").open("a") as stream:\n'
                        '    stream.write(json.dumps(sys.stdin.read()) + "\\n")\n'
                        'print(json.dumps({"ok":True,"result":{"message_id":90,"chat":{"id":123}}}))\n'
                        'print("200 0.01 0.02 0", end="")\n')
        self.env = {k: v for k, v in git_env(self.home).items() if not k.startswith('GC_') and k != b.SETTINGS_ENV}
        self.env.update(XDG_CONFIG_HOME=str(self.home / '.config'), TELEGRAM_PACK_PLATFORM='linux',
                        PATH=f'{self.bin}:/usr/local/bin:/usr/bin:/bin')

    def instance(self, yard, bot_id, port):
        """One yard's settings under <tmp>/<yard>, with the operator pat on its
        own fake native socket; returns (settings path, root, unit file)."""
        from test_run_asks import NativeSocket
        top = self.root / yard
        top.mkdir()
        yard_home = top / 'yard'
        yard_home.mkdir(mode=0o700)
        native = NativeSocket(yard_home)
        self.addCleanup(native.close)
        path = write_settings(top, yard=f'"{yard}"', port=str(port), python=f'"{sys.executable}"')
        text = path.read_text().replace('[yardr]', f'[yardr]\nhome = "{yard_home}"\noperator = "pat"')
        text = text.replace('bot_id = 999', f'bot_id = {bot_id}').replace('"test_bot"', f'"{yard}_bot"')
        path.write_text(text.replace(':8797/', f':{port + 1000}/'))
        return (path, self.home / f'.local/share/yardr-telegram/{yard}-telegram',
                self.home / f'.config/systemd/user/{yard}-telegram.service')

    def run_install(self, settings, *args, source=None):
        return subprocess.run(['sh', str((source or self.source) / 'install.sh'), '--settings', str(settings), *args],
                              env=self.env, capture_output=True, text=True, timeout=120)

    def ok(self, result):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout

    def launch(self, root, *args):
        return subprocess.run([str(root / 'bin/telegram-bridge'), *args], env=self.env, cwd=self.root,
                              capture_output=True, text=True, timeout=60)

    def test_two_instances_stay_independent_through_upgrade_rollback_and_removal(self):
        a_settings, a_root, a_unit = self.instance('alder', 998, 18941)
        b_settings, b_root, b_unit = self.instance('birch', 997, 18951)
        self.ok(self.run_install(a_settings))
        out = self.ok(self.run_install(b_settings))
        self.assertIn('ok    native yard socket and current operator identity', out)
        self.assertNotEqual(a_root, b_root)
        self.assertIn(f'wrote pack {b_root}/pack (base http://127.0.0.1:18951)', out)
        self.assert_pack(a_root, 18941)
        self.assert_pack(b_root, 18951)
        b_tops = (b_root, b_settings, self.root / 'birch' / 'private', self.root / 'birch' / 'state', b_unit)
        b_before = tree_digest(*b_tops)
        self.assertIn(str(b_root / 'bin/telegram-bridge'), b_before)

        def unchanged(step):
            with self.subTest(step=step):
                self.assertEqual(tree_digest(*b_tops), b_before)

        a_before = tree_digest(a_root, a_unit, self.root / 'alder')
        dry = self.ok(self.run_install(a_settings, '--dry-run'))
        self.assertIn(f'release releases/{self.first} is current', dry)
        self.assertIn(f'pack {a_root}/pack is current', dry)
        self.assertEqual(tree_digest(a_root, a_unit, self.root / 'alder'), a_before)
        unchanged('dry-run')

        (self.source / 'vendor').mkdir()
        (self.source / 'vendor' / 'yardr_fixture_vendored.py').write_text('VALUE = "alder"\n')
        with (self.source / 'yardr/SETUP.md').open('a') as stream:
            stream.write('\nA line of the second release.\n')
        second = commit_source(self.source, self.home, 'vendor a module')
        first_pack = os.readlink(a_root / 'pack')
        dry = self.ok(self.run_install(a_settings, '--dry-run'))
        self.assertIn(f'would write pack {a_root}/pack (base http://127.0.0.1:18941)', dry)
        self.assertEqual(tree_digest(a_root, a_unit, self.root / 'alder'), a_before)
        # A pack generation that cannot be written keeps the release, the link and the pack as they were.
        (a_root / 'packs').chmod(0o500)
        try:
            failed = self.run_install(a_settings)
        finally:
            (a_root / 'packs').chmod(0o700)
        self.assertEqual(failed.returncode, 1, failed.stdout)
        self.assertIn('not activated', failed.stderr)
        self.assertEqual(tree_digest(a_root, a_unit, self.root / 'alder'), a_before)
        out = self.ok(self.run_install(a_settings))
        self.assertIn(f'linked {a_root}/previous -> releases/{self.first}', out)
        self.assertIn(f'linked {a_root}/current -> releases/{second}', out)
        # the unit and the launchers name current: unchanged; the pack is the new release's
        self.assertEqual([line for line in out.splitlines() if line.startswith('wrote')],
                         [f'wrote pack {a_root}/pack (base http://127.0.0.1:18941)'])
        second_pack = os.readlink(a_root / 'pack')
        self.assertNotEqual(second_pack, first_pack)
        self.assertTrue((a_root / first_pack).is_dir())
        self.assert_pack(a_root, 18941)
        unchanged('upgrade')

        # The vendor slot: A's release resolves the module inside itself; B's commit has none.
        probe = ('import runpy, sys; runpy.run_path(sys.argv[1]); import yardr_fixture_vendored as v; '
                 'print(v.__file__)')
        found = subprocess.run([sys.executable, '-B', '-c', probe, str(a_root / 'current/bridge/bridge.py')],
                               env=self.env, cwd=self.root, capture_output=True, text=True, timeout=30)
        self.assertEqual(found.stdout.strip(), str((a_root / 'releases' / second / 'vendor/yardr_fixture_vendored.py')
                                                   .resolve()), found.stderr)
        missing = subprocess.run([sys.executable, '-B', '-c', probe, str(b_root / 'current/bridge/bridge.py')],
                                 env=self.env, cwd=self.root, capture_output=True, text=True, timeout=30)
        self.assertNotEqual(missing.returncode, 0)
        self.assertIn("No module named 'yardr_fixture_vendored'", missing.stderr)
        # An earlier PYTHONPATH entry does not shadow the release's vendor/.
        shadow = self.root / 'shadow'
        shadow.mkdir()
        (shadow / 'yardr_fixture_vendored.py').write_text('VALUE = "shadow"\n')
        vendor = a_root / 'current/vendor'
        shadowed = subprocess.run([sys.executable, '-B', '-c', probe, str(a_root / 'current/bridge/bridge.py')],
                                  env={**self.env, 'PYTHONPATH': f'{shadow}{os.pathsep}{vendor.resolve()}'},
                                  cwd=self.root, capture_output=True, text=True, timeout=30)
        self.assertEqual(shadowed.stdout.strip(), found.stdout.strip(), shadowed.stderr)

        out = self.ok(self.run_install(a_settings, '--rollback', source=a_root / 'current'))
        self.assertEqual((os.readlink(a_root / 'current'), os.readlink(a_root / 'previous')),
                         (f'releases/{self.first}', f'releases/{second}'))
        self.assertIn('ok    release files match SHA256SUMS', out)
        self.assertEqual(out.strip().splitlines()[-1], self.next_line(a_root, 'alder-telegram.service'))
        self.assertIn(f'wrote pack {a_root}/pack (base http://127.0.0.1:18941)', out)
        self.assertEqual(os.readlink(a_root / 'pack'), first_pack)  # the prior release's own generation
        self.assert_pack(a_root, 18941)
        unchanged('rollback')

        out = self.ok(self.run_install(a_settings))
        self.assertIn(f'release {second} reused from {a_root}/releases/{second}', out)
        self.assertEqual(os.readlink(a_root / 'current'), f'releases/{second}')
        self.assertEqual(os.readlink(a_root / 'pack'), second_pack)
        out = self.ok(self.run_install(a_settings))
        self.assertIn(f'pack {a_root}/pack is current', out)
        unchanged('reinstall')

        # A launcher with the right bytes but the wrong mode is written again.
        (a_root / 'bin/telegram-bridge').chmod(0o644)
        out = self.ok(self.run_install(a_settings))
        self.assertIn(f'wrote {a_root}/bin/telegram-bridge', out)
        self.assertEqual((a_root / 'bin/telegram-bridge').stat().st_mode & 0o7777, 0o755)
        self.assertEqual(self.launch(a_root, '--help').returncode, 0)
        unchanged('launcher mode repair')

        # A launcher that is a link to a file with the right bytes and mode is replaced by a file of its own.
        launcher = a_root / 'bin/telegram-bridge'
        linked = self.source / 'telegram-bridge-copy'
        shutil.copy2(launcher, linked)
        launcher.unlink()
        launcher.symlink_to(linked)
        out = self.ok(self.run_install(a_settings))
        self.assertIn(f'wrote {launcher}', out)
        self.assertNotIn(f'launcher {launcher} is current', out)
        self.assertFalse(launcher.is_symlink())
        self.assertEqual(launcher.read_bytes(), linked.read_bytes())
        self.assertEqual(launcher.stat().st_mode & 0o7777, 0o755)
        linked.unlink()
        self.assertEqual(self.launch(a_root, '--help').returncode, 0)
        unchanged('launcher link repair')

        a_unit.unlink()
        shutil.rmtree(a_root)
        unchanged('removal of A')
        shutil.rmtree(self.source)
        unchanged('removal of the source')
        self.assertFalse((self.home / '.local/bin/voice-transcribe').exists())  # never created

        # B restarted from its own copy, with the source and A gone.
        self.assertEqual(self.launch(b_root, '--help').returncode, 0)
        unit = self.launch(b_root, 'render-unit')
        self.assertEqual(unit.stdout, b_unit.read_text(), unit.stderr)
        check = self.launch(b_root, 'check-install')
        self.assertEqual(check.returncode, 0, check.stdout + check.stderr)
        self.assertIn('ok    release files match SHA256SUMS', check.stdout)
        self.assertIn('ok    native yard socket and current operator identity', check.stdout)
        sent = self.launch(b_root, 'run-ask', '--bead', 'sample-1', '--ask', '7', '--idempotency-key', 'restarted')
        self.assertEqual(sent.returncode, 0, sent.stderr)
        self.assertTrue(json.loads(sent.stdout)['delivered'])
        requests = (self.bin / 'requests.jsonl').read_text().splitlines()
        self.assertEqual(len(requests), 1)
        self.assertIn('/bot' + 'TESTTOKEN/sendMessage', json.loads(requests[0]))
        with b.Store(self.root / 'birch/state/telegram-bridge.sqlite3', 0).connect() as db:
            rows = [json.loads(row[0]) for row in db.execute('SELECT data FROM run_asks')]
        self.assertEqual([(row['conversation'], row['message_id']) for row in rows], [('123', 90)])

    def assert_pack(self, root, port):
        """root/pack is a generation under packs/ holding the current
        release's yardr/ with only its base line naming port."""
        self.assertRegex(os.readlink(root / 'pack'), r'^packs/[0-9a-f]{16}$')
        release, pack = root / 'current/yardr', root / 'pack'

        def files(top):
            return {p.relative_to(top).as_posix(): p for p in sorted(top.rglob('*'))
                    if p.is_file() and '__pycache__' not in p.parts}

        released, packed = files(release), files(pack)
        self.assertEqual(sorted(packed), sorted(released))
        for name, path in released.items():
            self.assertEqual(packed[name].stat().st_mode & 0o111, path.stat().st_mode & 0o111, name)
            if name != 'tgbridge.yaml':
                self.assertEqual(packed[name].read_bytes(), path.read_bytes(), name)
        old, new = released['tgbridge.yaml'].read_text(), packed['tgbridge.yaml'].read_text()
        self.assertEqual(len(old.splitlines(keepends=True)), len(new.splitlines(keepends=True)))
        self.assertEqual([(a, b) for a, b in zip(old.splitlines(keepends=True), new.splitlines(keepends=True)) if a != b],
                         [('base: http://127.0.0.1:8941\n', f'base: http://127.0.0.1:{port}\n')])
        bases = 'bases:\n  odyssey: http://127.0.0.1:8941\n  imd: http://127.0.0.1:8942\n'
        self.assertIn(bases, old)
        self.assertIn(bases, new)

    def next_line(self, root, unit):
        launcher = root / 'bin/telegram-bridge'
        return (f'next: {launcher} probe; systemctl --user daemon-reload; systemctl --user enable {unit}; '
                f'systemctl --user restart {unit}; {launcher} set-commands (yardr/SETUP.md, step 9)')

    def test_a_legacy_unit_is_kept_and_only_its_execstart_changes(self):
        settings, root, unit_file = self.instance('cedar', 996, 18961)
        loaded = b.load_settings(settings)
        legacy = b.render_unit(loaded, bridge_path=self.source / 'bridge/bridge.py')
        unit_file.parent.mkdir(parents=True)
        unit_file.write_text(legacy)
        link = self.home / '.local/bin/voice-transcribe'
        link.parent.mkdir(parents=True)
        link.symlink_to(self.source / 'transcriber/voice_transcribe.py')
        out = self.ok(self.run_install(settings))
        self.assertIn(f'replacing {unit_file}: bridge {self.source}/bridge/bridge.py -> '
                      f'{root}/current/bridge/bridge.py; the old file is kept in {root}/service.previous', out)
        self.assertEqual((root / 'service.previous').read_text(), legacy)
        self.assertEqual((root / 'service.previous').stat().st_mode & 0o777, 0o600)
        self.assertEqual(sorted(p.name for p in unit_file.parent.iterdir()), ['cedar-telegram.service'])
        new = unit_file.read_text()
        changed = [(old, line) for old, line in zip(legacy.splitlines(), new.splitlines()) if old != line]
        self.assertEqual(len(legacy.splitlines()), len(new.splitlines()))
        self.assertEqual(changed, [(f'ExecStart={sys.executable} {self.source}/bridge/bridge.py --settings {settings} run',
                                    f'ExecStart={sys.executable} {root}/current/bridge/bridge.py --settings {settings} run')])
        self.assertEqual(os.readlink(link), str(self.source / 'transcriber/voice_transcribe.py'))
        self.assertIn(f'note: {link} is left as it is; this instance\'s own is {root}/bin/voice-transcribe', out)
        refused = self.run_install(settings, '--rollback')
        self.assertEqual((refused.returncode, refused.stderr.strip()),
                         (1, f'install.sh: {root} has no previous release; to undo the first install, '
                             f'restore {root}/service.previous to {unit_file}'))

    def test_refusals_write_nothing(self):
        settings, root, unit_file = self.instance('dogwood', 995, 18971)
        self.ok(self.run_install(settings))
        tops = (self.home, self.root / 'dogwood')

        def refused(line, *args, settings=settings):
            before = tree_digest(*tops)
            result = self.run_install(settings, *args)
            self.assertEqual((result.returncode, result.stderr.strip()), (1, line), result.stdout)
            self.assertEqual(tree_digest(*tops), before)

        readme = self.source / 'README.md'
        original = readme.read_text()
        readme.write_text(original + 'local edit\n')
        refused(f'install.sh: the source {self.source} has tracked changes; commit or restore them first')
        readme.write_text(original)
        (self.source / 'untracked-note').write_text('not part of the release\n')  # untracked: installable

        refused(f'install.sh: the root {self.source}/instance is inside the source {self.source}; choose one outside it',
                '--root', str(self.source / 'instance'))
        self.assertFalse((self.source / 'instance').exists())

        gpu = self.root / 'dogwood' / 'gpu.toml'
        gpu.write_text(settings.read_text() + f'\n[transcribe]\nmethod = "auto"\n'
                       f'gpu_check = "{self.source}/transcriber/gpu_check.py"\n')
        gpu.chmod(0o600)
        refused(f'install.sh: settings transcribe.gpu_check {self.source}/transcriber/gpu_check.py is inside the '
                f'source {self.source}: copy it out of the source, point transcribe.gpu_check at the copy, '
                'then run install.sh again', settings=gpu)
        gpu.unlink()

        # A release of this commit reached through a link, such as another root's, is not reused.
        release = root / 'releases' / self.first
        elsewhere = self.root / 'elsewhere'
        elsewhere.mkdir()
        release.rename(elsewhere / self.first)
        release.symlink_to(elsewhere / self.first)
        refused(f'install.sh: {release} does not match HEAD (it is a link, not a release of its own); '
                'move it aside, then run install.sh again')
        release.unlink()
        (elsewhere / self.first).rename(release)
        (root / 'releases').rename(elsewhere / 'releases')
        (root / 'releases').symlink_to(elsewhere / 'releases')
        for args in ((), ('--dry-run',)):
            refused(f'install.sh: {root}/releases is a link; this root keeps its releases in a directory of its own',
                    *args)
        (root / 'releases').unlink()
        (elsewhere / 'releases').rename(root / 'releases')

        tampered = root / 'releases' / self.first / 'README.md'
        tampered.write_text(original + 'tampered\n')
        refused(f'install.sh: {root}/releases/{self.first} does not match HEAD (README.md does not match SHA256SUMS); '
                'move it aside, then run install.sh again')
        check = self.launch(root, 'check-install')
        self.assertNotEqual(check.returncode, 0)
        self.assertIn('FAIL  release files: README.md does not match SHA256SUMS', check.stdout)

        refused(f'install.sh: {root} has no previous release to roll back to', '--rollback')
        self.assertEqual(os.readlink(root / 'current'), f'releases/{self.first}')

        # RELEASE is line-based: a source path with a newline cannot be recorded.
        odd = self.root / 'new\nline'
        shutil.copytree(self.source, odd, symlinks=True)
        before = tree_digest(*tops)
        result = self.run_install(settings, source=odd)
        self.assertEqual((result.returncode, result.stderr.strip()),
                         (1, 'install.sh: the source path has a newline in it; install from a checkout whose path '
                             'has none'), result.stdout)
        self.assertEqual(tree_digest(*tops), before)
        shutil.rmtree(odd)

        # The pack's generation and link are the root's own, as they were rendered.
        generation = root / os.readlink(root / 'pack')
        setup = generation / 'SETUP.md'
        setup.chmod(0o600)
        kept = setup.read_bytes()
        setup.write_bytes(kept + b'tampered\n')
        for args in ((), ('--dry-run',)):
            refused(f'install.sh: {generation} does not match the pack rendered for it; move it aside, then run '
                    'install.sh again', *args)
        setup.write_bytes(kept)
        setup.chmod(0o644)
        pointer = os.readlink(root / 'pack')
        (root / 'pack').unlink()
        (root / 'pack').mkdir()
        refused(f'install.sh: {root}/pack is not a link to a generation under packs/; move it aside, then run '
                'install.sh again')
        (root / 'pack').rmdir()
        (root / 'pack').symlink_to(pointer)

        # A declaration without exactly one top-level base line is refused before anything is written.
        declaration = self.source / 'yardr/tgbridge.yaml'
        text = declaration.read_text()
        line = 'base: http://127.0.0.1:8941\n'
        self.assertEqual(text.count(line), 1)
        for broken, has in ((text.replace(line, ''), 'it has 0 top-level base lines'),
                            (text.replace(line, line + line), 'it has 2 top-level base lines'),
                            (text.replace(line, 'base: http://localhost:8941\n'), 'its one is another form')):
            declaration.write_text(broken)
            commit_source(self.source, self.home, 'a broken base line')
            for args in ((), ('--dry-run',)):
                with self.subTest(has=has, args=args):
                    refused(f'install.sh: {declaration} must have exactly one top-level line '
                            f'base: http://127.0.0.1:<port>; {has}', *args)
        declaration.write_text(text)
        commit_source(self.source, self.home, 'the base line again')

    def venv(self, path):
        """A venv at path whose python links to this interpreter; skipped when one cannot be made."""
        made = subprocess.run([sys.executable, '-m', 'venv', '--without-pip', str(path)],
                              capture_output=True, text=True, timeout=120)
        if made.returncode != 0:
            self.skipTest(f'python -m venv failed: {made.stderr.strip()[-200:]}')
        return path / 'bin/python'

    def with_python(self, settings, python):
        text = re.sub(r'(?m)^python = .*$', f'python = "{python}"', settings.read_text())
        settings.write_text(text)

    def test_an_interpreter_in_the_source_is_refused_and_one_outside_it_survives_the_source(self):
        settings, root, unit_file = self.instance('fir', 993, 18991)
        source = Path(os.path.realpath(self.source))
        venv_python = self.venv(source / '.venv')
        remedy = ('point python at an interpreter outside the source, such as /usr/bin/python3 '
                  'or a venv made outside it, then run install.sh again')
        line = f'install.sh: settings python {venv_python} is inside the source {source}: {remedy}'
        tops = (self.home, self.root / 'fir')

        # (a) A first install: the dry-run and the install refuse alike and write nothing.
        self.with_python(settings, venv_python)
        before = tree_digest(*tops)
        for args in (('--dry-run',), ()):
            with self.subTest(args=args):
                refused = self.run_install(settings, *args)
                self.assertEqual((refused.returncode, refused.stderr.strip()), (1, line), refused.stdout)
                self.assertEqual(tree_digest(*tops), before)
        self.assertFalse(root.exists())

        # (b) An installed instance whose settings then name the source's venv.
        self.with_python(settings, sys.executable)
        self.ok(self.run_install(settings))
        installed = settings.read_text()
        self.with_python(settings, venv_python)
        before = tree_digest(*tops)
        refused = self.run_install(settings)
        self.assertEqual((refused.returncode, refused.stderr.strip()), (1, line), refused.stdout)
        self.assertEqual(tree_digest(*tops), before)
        self.assertEqual(list((root / 'releases').glob('.stage-*')), [])
        self.assertEqual(self.launch(root, '--help').returncode, 0)
        check = self.launch(root, 'check-install')
        self.assertNotEqual(check.returncode, 0)
        self.assertIn('FAIL  ' + line.removeprefix('install.sh: '), check.stdout)
        settings.write_text(installed)  # render-unit renders the settings' python: the old settings render the unit
        self.assertEqual(self.launch(root, 'render-unit').stdout, unit_file.read_text())

        # (e) An outside link to the source's venv is refused in the through form.
        link = self.root / 'fir' / 'py'
        link.symlink_to(venv_python)
        self.with_python(settings, link)
        before = tree_digest(*tops)
        refused = self.run_install(settings)
        self.assertEqual((refused.returncode, refused.stderr.strip()),
                         (1, f'install.sh: settings python {link} depends on the source {source} through '
                             f'{link} -> {venv_python}: {remedy}'), refused.stdout)
        self.assertEqual(tree_digest(*tops), before)

        # (d) The remedy: a venv made outside the source keeps working once the source is gone.
        outside = self.venv(self.root / 'fir' / 'venv')
        self.with_python(settings, outside)
        out = self.ok(self.run_install(settings))
        self.assertIn(f'ok    nothing the settings name depends on the source {source}', out)
        self.assertIn(f'exec {outside} ', (root / 'bin/telegram-bridge').read_text())
        shutil.rmtree(self.source)
        self.assertEqual(self.launch(root, '--help').returncode, 0)
        check = self.launch(root, 'check-install')
        self.assertEqual(check.returncode, 0, check.stdout + check.stderr)
        self.assertIn(f'ok    nothing the settings name depends on the source {source}', check.stdout)

    def test_a_reinstall_from_another_checkout_keeps_the_release_s_recorded_source(self):
        settings, root, unit_file = self.instance('ivy', 990, 19021)
        other_settings, other_root, other_unit = self.instance('juniper', 989, 19031)
        self.ok(self.run_install(settings))
        self.ok(self.run_install(other_settings))
        other_tops = (other_root, other_settings, self.root / 'juniper' / 'private', other_unit)
        other_before = tree_digest(*other_tops)
        source = Path(os.path.realpath(self.source))
        second = self.root / 'second'
        shutil.copytree(self.source, second, symlinks=True)  # the same HEAD at another path
        venv_python = self.venv(source / '.venv')
        release = root / 'releases' / self.first
        self.assertEqual(b.release_source(release), str(source))

        # The reused release records the first checkout, so its venv is refused from the second.
        self.with_python(settings, venv_python)
        tops = (self.home, self.root / 'ivy')
        before = tree_digest(*tops)
        line = (f'install.sh: settings python {venv_python} is inside the source {source}: point python at an '
                'interpreter outside the source, such as /usr/bin/python3 or a venv made outside it, '
                'then run install.sh again')
        for args in (('--dry-run',), ()):
            with self.subTest(args=args):
                refused = self.run_install(settings, *args, source=second)
                self.assertEqual((refused.returncode, refused.stderr.strip()), (1, line), refused.stdout)
                self.assertEqual(tree_digest(*tops), before)
        self.assertEqual(list((root / 'releases').glob('.stage-*')), [])
        self.assertEqual(os.readlink(root / 'current'), f'releases/{self.first}')
        self.assertFalse((root / 'previous').exists())
        self.assertEqual(b.release_source(release), str(source))
        self.assertEqual(tree_digest(*other_tops), other_before)

        # With the host interpreter the release is reused as it is and outlives the first checkout.
        self.with_python(settings, sys.executable)
        out = self.ok(self.run_install(settings, source=second))
        self.assertIn(f'release {self.first} reused from {release}', out)
        self.assertIn(f'ok    nothing the settings name depends on the source {source}', out)
        self.assertEqual(b.release_source(release), str(source))
        shutil.rmtree(self.source)
        self.assertEqual(self.launch(root, '--help').returncode, 0)
        check = self.launch(root, 'check-install')
        self.assertEqual(check.returncode, 0, check.stdout + check.stderr)
        self.assertIn(f'ok    nothing the settings name depends on the source {source}', check.stdout)
        self.assertEqual(tree_digest(*other_tops), other_before)

    def test_a_rollback_to_a_release_whose_settings_depend_on_the_source_is_refused(self):
        settings, root, unit_file = self.instance('gum', 992, 19001)
        self.ok(self.run_install(settings))
        (self.source / 'CHANGELOG.md').write_text('a newer release\n')
        second = commit_source(self.source, self.home, 'a newer release')
        self.ok(self.run_install(settings))
        source = Path(os.path.realpath(self.source))
        venv_python = self.venv(source / '.venv')
        self.with_python(settings, venv_python)
        before = tree_digest(root, unit_file)
        refused = self.run_install(settings, '--rollback', source=root / 'current')
        self.assertNotEqual(refused.returncode, 0, refused.stdout)
        self.assertIn(f'FAIL  settings python {venv_python} is inside the source {source}', refused.stdout)
        self.assertEqual(tree_digest(root, unit_file), before)
        self.assertEqual((os.readlink(root / 'current'), os.readlink(root / 'previous')),
                         (f'releases/{second}', f'releases/{self.first}'))

    def test_several_entries_in_the_source_are_named_in_order_and_nothing_is_written(self):
        settings, root, unit_file = self.instance('hazel', 991, 19011)
        source = Path(os.path.realpath(self.source))
        handler = source / 'handlers' / 'deploy'
        handler.parent.mkdir()
        handler.write_text('#!/bin/sh\n')
        handler.chmod(0o700)
        original = settings.read_text()
        text = re.sub(r'(?m)^state_dir = .*$', f'state_dir = "{source}/state"', original)
        settings.write_text(text + f'\n[[command]]\nname = "deploy"\ndescription = "Deploy"\nhandler = "{handler}"\n')
        tops = (self.home, self.root / 'hazel', self.source)
        before = tree_digest(*tops)
        refused = self.run_install(settings)
        self.assertEqual((refused.returncode, refused.stderr.strip().splitlines()), (1, [
            f'install.sh: settings state_dir {source}/state is inside the source {source}: stop the service, move it '
            'out of the source, point state_dir at the new path, then run install.sh again',
            f'install.sh: settings [[command]] deploy handler {handler} is inside the source {source}: copy it out '
            'of the source, point [[command]] deploy handler at the copy, then run install.sh again']), refused.stdout)
        self.assertEqual(tree_digest(*tops), before)

        settings.write_text(original)
        inside = source / 'private.toml'
        inside.write_text(original)
        inside.chmod(0o600)
        before = tree_digest(*tops)
        refused = self.run_install(inside)
        self.assertEqual((refused.returncode, refused.stderr.strip()),
                         (1, f'install.sh: the settings file {inside} is inside the source {source}: copy it out of '
                             'the source (mode 600), then run install.sh --settings <the copy>'), refused.stdout)
        self.assertEqual(tree_digest(*tops), before)
        self.assertFalse(root.exists())

    def test_a_failed_check_or_publication_keeps_the_active_instance(self):
        settings, root, unit_file = self.instance('elm', 994, 18981)
        self.ok(self.run_install(settings))
        active = (root, unit_file)
        before = tree_digest(*active)

        token = self.root / 'elm' / 'private' / 'telegram-bot-token'
        token.chmod(0o644)
        (self.source / 'CHANGELOG.md').write_text('a newer release\n')
        commit_source(self.source, self.home, 'a newer release')
        failed = self.run_install(settings)
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn('FAIL  bot token file must be an owner-only regular file', failed.stdout)
        self.assertEqual(tree_digest(*active), before)  # no stage, release, link, unit or launcher change
        token.chmod(0o600)

        if os.geteuid() == 0:
            self.skipTest('root writes a read-only directory')
        template = self.source / 'systemd/telegram-bridge.service.in'
        template.write_text(template.read_text() + '# a template change\n')
        third = commit_source(self.source, self.home, 'a new unit template')
        unit_file.parent.chmod(0o555)
        try:
            failed = self.run_install(settings)
        finally:
            unit_file.parent.chmod(0o755)
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn(f'install.sh: cannot write beside {unit_file.parent}', failed.stderr)
        self.assertIn('not activated', failed.stderr)
        self.assertEqual(tree_digest(*active), before)
        self.assertEqual(self.launch(root, 'render-unit').stdout, unit_file.read_text())

        # The settings' own interpreter checks the release, and a rename that
        # fails once activation has begun puts back the release, links, unit and
        # launchers already replaced: os.replace fails on the last launcher.
        python, calls = self.root / 'elm-python', self.root / 'elm-python.log'
        python.write_text(f'#!/bin/sh\necho "$*" >>{calls}\nexec {sys.executable} "$@"\n')
        python.chmod(0o755)
        old_settings = settings.read_text()
        settings.write_text(old_settings.replace(f'python = "{sys.executable}"', f'python = "{python}"'))
        faults = self.root / 'faults'
        faults.mkdir()
        (faults / 'sitecustomize.py').write_text(
            'import os\n_replace = os.replace\n'
            'def replace(src, dst, *a, **k):\n'
            '    if str(dst).endswith(os.environ["FAIL_REPLACE"]):\n'
            '        raise OSError(5, "Input/output error", str(dst))\n'
            '    return _replace(src, dst, *a, **k)\n'
            'os.replace = replace\n')
        self.env.update(PYTHONPATH=str(faults), FAIL_REPLACE='bin/voice-transcribe')
        try:
            failed = self.run_install(settings)
        finally:
            del self.env['PYTHONPATH'], self.env['FAIL_REPLACE']
        self.assertNotEqual(failed.returncode, 0, failed.stdout)
        self.assertIn(f'install.sh: activating {root / "bin/voice-transcribe"} failed', failed.stderr)
        self.assertIn('back in place', failed.stderr)
        self.assertIn(f'releases/.stage-', calls.read_text())
        self.assertIn('check-install', calls.read_text())
        self.assertEqual(tree_digest(*active), before)  # no release, stage, previous, service.previous
        new_settings = settings.read_text()
        settings.write_text(old_settings)
        self.assertEqual(self.launch(root, 'render-unit').stdout, unit_file.read_text())
        settings.write_text(new_settings)

        out = self.ok(self.run_install(settings))
        self.assertIn(f'wrote {unit_file}', out)
        self.assertEqual(os.readlink(root / 'current'), f'releases/{third}')
        self.assertEqual(os.readlink(root / 'previous'), f'releases/{self.first}')
        self.assertTrue(unit_file.read_text().endswith('# a template change\n'))
        self.assertIn(f'exec {python} ', (root / 'bin/telegram-bridge').read_text())


    def test_an_interrupted_or_concurrent_activation_keeps_the_pack_with_its_release(self):
        settings, root, _ = self.instance('fir', 993, 18991)
        self.ok(self.run_install(settings))
        first = os.readlink(root / 'current')
        faults = self.root / 'faults'
        faults.mkdir()
        (faults / 'sitecustomize.py').write_text(
            'import os, signal, time\n'
            '_dir, _fault = os.environ.get("FAULT_DIR"), os.environ.get("FAULT")\n'
            'if _fault == "term":\n'
            '    _replace = os.replace\n'
            '    def replace(src, dst, *a, **k):\n'
            '        _replace(src, dst, *a, **k)\n'
            '        if os.path.basename(str(dst)) == "pack":\n'
            '            os.kill(os.getpid(), signal.SIGTERM)\n'
            '    os.replace = replace\n'
            'elif _fault == "pause":\n'
            '    _rename = os.rename\n'
            '    def rename(src, dst, *a, **k):\n'
            '        if os.path.basename(os.path.dirname(str(dst))) == "packs":\n'
            '            open(os.path.join(_dir, "paused"), "w").close()\n'
            '            deadline = time.monotonic() + 120\n'
            '            while not os.path.exists(os.path.join(_dir, "go")) and time.monotonic() < deadline:\n'
            '                time.sleep(0.05)\n'
            '            raise OSError(5, "Input/output error", str(dst))\n'
            '        return _rename(src, dst, *a, **k)\n'
            '    os.rename = rename\n'
            'elif _fault == "wait":\n'
            '    import fcntl\n'
            '    _flock = fcntl.flock\n'
            '    def flock(fd, op):\n'
            '        try:\n'
            '            return _flock(fd, op | fcntl.LOCK_NB)\n'
            '        except BlockingIOError:\n'
            '            open(os.path.join(_dir, "blocked"), "w").close()\n'
            '            return _flock(fd, op)\n'
            '    fcntl.flock = flock\n')

        def faulted(fault):
            return {**self.env, 'PYTHONPATH': str(faults), 'FAULT_DIR': str(faults), 'FAULT': fault}

        def command():
            return ['sh', str(self.source / 'install.sh'), '--settings', str(settings)]

        # SIGTERM as the pack link is replaced: the pack moved last, after the release it belongs to.
        with (self.source / 'yardr/SETUP.md').open('a') as stream:
            stream.write('\nA line of the second release.\n')
        second = commit_source(self.source, self.home, 'a second release')
        stopped = subprocess.run(command(), env=faulted('term'), capture_output=True, text=True, timeout=120)
        self.assertNotEqual(stopped.returncode, 0, stopped.stdout)
        self.assertEqual(os.readlink(root / 'current'), f'releases/{second}', stopped.stderr)
        self.assertEqual(os.readlink(root / 'previous'), first)
        self.assert_pack(root, 18991)

        # A pauses before its pack generation, with its release already in
        # place; B, installing the same commit, waits for A's activation, so
        # A's failure cannot take back a release B adopted.
        with (self.source / 'yardr/SETUP.md').open('a') as stream:
            stream.write('\nA line of the third release.\n')
        third = commit_source(self.source, self.home, 'a third release')
        a = subprocess.Popen(command(), env=faulted('pause'), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        b_proc = None
        try:
            deadline = time.monotonic() + 120
            while not (faults / 'paused').exists() and a.poll() is None and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue((faults / 'paused').exists(), a.poll())
            self.assertTrue((root / f'releases/{third}').is_dir())
            b_proc = subprocess.Popen(command(), env=faulted('wait'), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                      text=True)
            while not (faults / 'blocked').exists() and b_proc.poll() is None and time.monotonic() < deadline:
                time.sleep(0.05)
        finally:
            (faults / 'go').touch()
            a_out, a_err = a.communicate(timeout=120)
            b_out, b_err = b_proc.communicate(timeout=120) if b_proc else ('', '')
        self.assertNotEqual(a.returncode, 0, a_out)
        self.assertIn('back in place', a_err)
        self.assertEqual(b_proc.returncode, 0, b_out + b_err)
        self.assertEqual(os.readlink(root / 'current'), f'releases/{third}')
        self.assertTrue((root / 'current').is_dir(), b_out)
        self.assertEqual(os.readlink(root / 'previous'), f'releases/{second}')
        self.assert_pack(root, 18991)


if __name__ == '__main__':
    unittest.main()
