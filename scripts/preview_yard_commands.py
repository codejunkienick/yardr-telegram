#!/usr/bin/env python3
"""Print the /quota renderings for the captured fixtures: the plain read,
the configured group read, the unknown entry on its own, the timeout line
and the settings refusal. Then the /mayor_model view and the answers of a
done, refused, unknown and restart-not-confirmed tap, driven through the
real tap code on a scripted yardr. No Telegram access, no yardr run."""
import datetime
import json
import re
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bridge'))
import bridge as b  # noqa: E402
import yard_commands as y  # noqa: E402
from test_bridge import write_settings  # noqa: E402

FIXTURES = Path(__file__).resolve().parents[1] / 'bridge' / 'fixtures'


def show(title, lines):
    print('### ' + title)
    for line in lines:
        print(line)
    print()


class ScriptedYard:
    """crew show/set/restart as yardr answers them, for one scenario: the
    set lands, is refused, outlives its deadline, or lands with a failed
    restart. GetUser maps the operator to the paired user."""

    def __init__(self, scenario):
        self.scenario, self.revision = scenario, 1
        self.crew = dict(kind='codex', model='gpt-6.1-sol',
                         args=['--dangerously-bypass-approvals-and-sandbox', '-c', 'model_reasoning_effort="high"'])

    def call(self, method, arguments):
        return dict(name=arguments['name'], role='operator', identities=dict(tgbridge=dict(id='123')))

    def run(self, argv, home, env, deadline):
        verb = argv[argv.index('crew') + 1]
        if verb == 'show':
            crew = dict(name='yardmaster', config=dict(self.crew), revision='R%d' % self.revision,
                        kind=self.crew['kind'], state='running', status='idle', handle=dict(kind='codex'))
            return 0, json.dumps(dict(crew=crew)).encode(), b'', None
        if verb == 'set':
            if self.scenario == 'refused':
                return 1, b'', b'stale', None
            if self.scenario == 'unknown':
                return None, b'', b'', 'timeout'
            for word in argv[argv.index('set') + 2:argv.index('--revision')]:
                key, _, value = word.partition('=')
                self.crew[key] = json.loads(value) if key == 'args' else value
            self.revision += 1
            return 0, b'', b'', None
        return (1 if self.scenario == 'restart' else 0), b'', b'', None


class PreviewBridge:
    def __init__(self, config, store, yard):
        self.config, self.store = config, store
        self.run_asks = type('RunAsks', (), {'client': yard})()

    @staticmethod
    def _log(line):
        pass


def mayor_previews(root):
    picks = (dict(id='sol', label='GPT-6.1 Sol high', kind='codex', model='gpt-6.1-sol',
                  args=('--dangerously-bypass-approvals-and-sandbox', '-c', 'model_reasoning_effort="high"'),
                  env_file=None, effort='high'),
             dict(id='opus', label='Opus high', kind='claude', model='opus',
                  args=('--permission-mode', 'auto', '--effort', 'high'), env_file=None, effort='high'))
    config = dict(yardr_bin='/usr/local/bin/yardr', yardr_home=str(root / 'yard'), yardr_operator='pat',
                  yardr_source='tgbridge', user_id=123, timezone='Asia/Tokyo',
                  yardr_mayor_model=dict(crew='yardmaster', timeout=60, picks=picks))
    store = b.Store(root / 'preview.sqlite3', 1)

    def shown(title, action, answer=None):
        lines = action['text'].removeprefix('<pre>').removesuffix('</pre>').splitlines()
        if answer is not None:
            lines = [f'[answer: {answer["text"]}{" (alert)" if answer["alert"] else ""}]'] + lines
        lines += [f'[{button["text"]}]' for row in action.get('keyboard', []) for button in row]
        show(title, lines)

    yard = ScriptedYard('done')
    with patch.object(y, 'run_yardr', yard.run):
        (view,) = y.handle_mayor_model(PreviewBridge(config, store, yard), dict(kind='command'))['actions']
    shown('mayor_model view', view)
    for update_id, (scenario, title) in enumerate((('done', 'tap done'), ('refused', 'tap refused'),
                                                   ('unknown', 'tap unknown'),
                                                   ('restart', 'tap done, restart not confirmed')), 1):
        yard = ScriptedYard(scenario)
        with patch.object(y, 'run_yardr', yard.run):
            answer, edit = y.handle_mayor_model(PreviewBridge(config, store, yard), dict(
                kind='callback', callback_data='mayor-model:opus', update_id=update_id,
                callback_message_id=500))['actions']
        shown(title, edit, answer)


def main():
    tz = y.zone('Asia/Tokyo')
    now = datetime.datetime.now(datetime.timezone.utc)
    plain = y.parse_limits_answer((FIXTURES / 'limits_plain.json').read_bytes())
    grouped = y.parse_limits_answer((FIXTURES / 'limits_group.json').read_bytes())
    show('plain', y.fit(y.render_quota(plain, [], True, tz, now)))
    show('groups', y.fit(y.render_quota(plain, [('quota-builders', grouped)], True, tz, now)))
    selfy = [entry for entry in plain[1] if entry['source'] == 'selfy'][0]
    show('unknown entry', y.source_block(selfy, tz, now))
    show('timeout', y.unavailable('timed out')['actions'][0]['text']
         .removeprefix('<pre>').removesuffix('</pre>').splitlines())

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / 'yard').mkdir(mode=0o700)
        path = write_settings(root)
        text = path.read_text()
        text = re.sub(r'\n\[\[menu\]\]\ncommand = "quota"\ndescription = [^\n]*\n', '\n', text)
        marker = '\n[yardr]\n'
        start = text.index(marker)
        text = text[:start] + marker + 'home = "%s"\noperator = "pat"\n' % (root / 'yard') \
            + text[start + len(marker):]
        text += '\n[yardr.quota]\n'
        text += '\n[[command]]\nname = "quota"\ndescription = "Quota via add-on"\n'
        path.write_text(text)
        try:
            b.load_settings(path)
            print('settings refusal: unexpectedly loaded')
        except b.SettingsError as error:
            print('settings refusal: ' + str(error))
        print()
        mayor_previews(root)


if __name__ == '__main__':
    sys.exit(main())
