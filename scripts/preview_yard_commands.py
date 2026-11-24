#!/usr/bin/env python3
"""Print quota fixtures and model-switch cards from the isolated native test build.

No live Telegram or crew. TGBRIDGE_TEST_YARDR names the new native build.
"""
import datetime
import re
import sys
import tempfile
from pathlib import Path

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


def switch_previews(root):
    from html import unescape
    from preview_model_switch import cards
    for title, body in cards():
        lines = unescape(body['text'].removeprefix('<pre>').removesuffix('</pre>')).splitlines()
        lines += [f'[{button["text"]}]' for row in body['reply_markup']['inline_keyboard'] for button in row]
        show(title, lines)


def main():
    tz = y.zone('Asia/Tokyo')
    now = datetime.datetime.now(datetime.timezone.utc)
    plain = y.parse_limits_answer((FIXTURES / 'limits_plain.json').read_bytes())
    grouped = y.parse_limits_answer((FIXTURES / 'limits_group.json').read_bytes())
    show('plain', y.fit(y.render_quota(plain, [], True, False, tz, now)))
    show('plain with machine', y.fit(y.render_quota(plain, [], True, True, tz, now)))
    show('groups', y.fit(y.render_quota(plain, [('quota-builders', grouped)], True, False, tz, now)))
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
        text = text[:start] + marker + 'home = "%s"\noperator = "pat"\ncards = true\n' % (root / 'yard') \
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
        switch_previews(root)


if __name__ == '__main__':
    sys.exit(main())
