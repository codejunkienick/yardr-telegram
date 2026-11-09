"""The local publication gate uses committed synthetic repos, never private state."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[2] / 'scripts/public_readiness.py'


class PublicReadinessTests(unittest.TestCase):
    def setUp(self):
        if not shutil.which('git'):
            self.skipTest('git is required')
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        self.env = dict(HOME=str(self.root), PATH=str(self.root) + ':/usr/local/bin:/usr/bin:/bin',
                        XDG_CONFIG_HOME=str(self.root / 'config'), XDG_STATE_HOME=str(self.root / 'state'))
        self.git('init', '-q')
        self.git('config', 'core.hooksPath', '/dev/null')

    def git(self, *args):
        result = subprocess.run(['git', '-C', str(self.repo), *args], env=self.env, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        return result.stdout.decode().strip()

    def commit(self):
        self.git('add', '.')
        self.git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
                 'commit', '-qm', 'Synthetic release', '--no-gpg-sign')

    def scan(self, *args):
        return subprocess.run(['python3', '-B', str(SCRIPT), *map(str, args)], cwd=self.repo,
                              env=self.env, capture_output=True, text=True, timeout=30)

    def test_export_matches_every_committed_byte_mode_and_provenance(self):
        (self.repo / 'LICENSE').write_text('Copyright the authors\n')
        # Generic paths, compatibility names and attribution are allowed.
        (self.repo / 'README.md').write_text('/home/alex /Users/example odyssey imd city mayor\n'
                                           'GC_RECEIPT gc_base_url gc_api_token_file\n'
                                           '_print_gc_receipt GC_SETTINGS mayor_quota_episode\n')
        (self.repo / 'bridge').mkdir()
        (self.repo / 'bridge/test_bridge.py').write_text('import g' + 'c\ng' + 'c.collect()\n')
        (self.repo / 'bridge/test_settings.py').write_text("legacy = '.g" + "c'\n"
                                                        "names = ['G" + "C_CITY', 'G" + "C_CITY_PATH', 'G" + "C_']\n")
        executable = self.repo / 'helper'
        executable.write_bytes(b'#!/bin/sh\nprintf usable\\n\n')
        executable.chmod(0o755)
        self.commit()
        target = self.root / 'review'
        result = self.scan('--export', target)
        self.assertEqual(result.returncode, 0, result.stderr)
        summary = json.loads(result.stdout)
        self.assertEqual(summary['findings'], 0)
        manifest = json.loads((target / 'manifest.json').read_text())
        self.assertEqual(manifest['commit'], self.git('rev-parse', 'HEAD'))
        self.assertEqual(manifest['tree'], self.git('rev-parse', 'HEAD^{tree}'))
        self.assertEqual(summary['files'], 5)
        archive = target / manifest['archive']
        self.assertEqual(hashlib.sha256(archive.read_bytes()).hexdigest(), manifest['archive_sha256'])
        with tarfile.open(archive) as tar:
            self.assertEqual(sorted(tar.getnames()), ['LICENSE', 'README.md', 'bridge', 'bridge/test_bridge.py',
                                                     'bridge/test_settings.py', 'helper'])
            for entry in manifest['files']:
                content = (self.repo / entry['path']).read_bytes()
                self.assertEqual(tar.extractfile(entry['path']).read(), content)
                self.assertEqual(entry['size'], len(content))
                self.assertEqual(entry['sha256'], hashlib.sha256(content).hexdigest())
                self.assertEqual(entry['blob'], self.git('rev-parse', 'HEAD:' + entry['path']))
                self.assertEqual(entry['mode'], '100755' if entry['path'] == 'helper' else '100644')
        before = archive.read_bytes()
        self.assertEqual(self.scan('--export', target).returncode, 1)
        self.assertEqual(archive.read_bytes(), before)
        self.assertEqual(self.scan('--export', self.repo / 'export').returncode, 1)
        self.assertFalse((self.repo / 'export').exists())

    def test_sensitive_values_are_counted_by_location_and_never_exported_or_echoed(self):
        token = '123456789:' + 'Z' * 35
        private = '-----BEGIN ' + 'OPENSSH PRIVATE KEY-----'
        operator_path = '/home/' + 'nick/private'
        (self.repo / 'README.md').write_text(token + '\n' + private + '\n' + operator_path + '\n')
        (self.repo / 'ledger.sqlite').write_bytes(b'SQLite\0fixture')
        (self.repo / 'photo.bin').write_bytes(b'human attachment\0kept')
        (self.repo / '.env').write_text('DO_NOT_ECHO=fixture\n')
        (self.repo / 'test_migration.py').write_text(operator_path)
        self.commit()
        target = self.root / 'refused'
        result = self.scan('--export', target)
        self.assertEqual(result.returncode, 1)
        self.assertFalse(target.exists())
        for value in (token, private, operator_path, 'DO_NOT_ECHO=fixture'):
            self.assertNotIn(value, result.stdout + result.stderr)
        records = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual({r['category'] for r in records[:-1]},
                         {'bot-token', 'private-key', 'operator-path', 'runtime-file', 'binary-needs-review'})
        self.assertEqual(records[-1]['findings'], 7)
        self.assertEqual([(r['line'], r['count']) for r in records if r.get('path') == 'README.md'], [(1, 1), (2, 1), (3, 1)])
        self.assertEqual((self.repo / 'photo.bin').read_bytes(), b'human attachment\0kept')

        for name in ('README.md', 'ledger.sqlite', 'photo.bin', '.env'):
            (self.repo / name).unlink()
        cases = (
            ('README.md', 'Fork of Gas' + ' City'),
            ('YARDR.md', 'Gas' + '\nCITY dependency'),
            ('settings.example.toml', '# gas' + '-city default'),
            ('skills/example/SKILL.md', 'gas' + '_city setup'),
            ('bridge/bridge.py', '# G' + 'C accepted the relay'),
            ('bridge/test_settings.py', '# g' + "c's configuration"),
            ('bridge/test_bridge.py', 'import g' + 'c  # Gas' + ' City dependency'),
            ('scripts/public_readiness.py', '# g' + 'c integration'),
            ('CHANGELOG.md', 'forked at 1997' + '3ca'),
            ('docs/example.md', 'uses ext' + 'msg'),
            ('yardr/SETUP.md', 'load .g' + 'c/site.toml'),
            ('example.py', 'g' + 'c_new_api = None'),
            ('example.py', '_new_g' + 'c_api = None'),
            ('README.md', 'GC_RECEIPT means Gas' + ' City delivery'),
        )
        for index, (name, text) in enumerate(cases):
            with self.subTest(path=name, case=index):
                path = self.repo / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('GC_RECEIPT gc_base_url\n' + text + '\n')
                self.commit()
                destination = self.root / ('relationship-' + str(index))
                result = self.scan('--export', destination)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertFalse(destination.exists())
                records = [json.loads(line) for line in result.stdout.splitlines()]
                count = 2 if index == 6 else 1
                self.assertEqual(records[:-1], [dict(path=name, line=2, category='relationship-reference', count=count)])
                self.assertEqual(records[-1]['findings'], count)
                self.assertNotIn(text, result.stdout + result.stderr)
                path.unlink()

    def test_dirty_tree_unknown_entries_and_archive_omissions_refuse_before_export(self):
        (self.repo / 'README.md').write_text('fixture\n')
        self.commit()
        target = self.root / 'refused'
        for untracked in (True, False):
            path = self.repo / ('untracked' if untracked else 'README.md')
            path.write_text('private dirty fixture')
            result = self.scan('--export', target)
            self.assertEqual(result.returncode, 1)
            self.assertNotIn('private dirty fixture', result.stdout + result.stderr)
            self.assertFalse(target.exists())
            if untracked:
                path.unlink()
        self.commit()
        (self.repo / '.gitattributes').write_text('README.md export-ignore\n')
        self.commit()
        self.assertEqual(self.scan('--export', target).returncode, 1)
        self.assertFalse(target.exists())
        (self.repo / '.gitattributes').write_text('README.md export-subst\n')
        (self.repo / 'README.md').write_text('$Format:%H$\n')
        self.commit()
        self.assertEqual(self.scan('--export', target).returncode, 1)
        self.assertFalse(target.exists())
        (self.repo / '.gitattributes').write_text('')
        (self.repo / 'link').symlink_to('README.md')
        self.commit()
        self.assertEqual(self.scan('--export', target).returncode, 1)
        self.assertFalse(target.exists())
