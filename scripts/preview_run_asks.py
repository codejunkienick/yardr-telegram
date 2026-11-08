#!/usr/bin/env python3
"""Print an offline HTML preview of synthetic native cards; no Telegram access."""
from html import escape
import importlib.util
from pathlib import Path
import sys


def skill_cards():
    # Drive the real bridge state machine with the existing offline transports.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bridge'))
    from test_run_asks import NativeTests, skill_proposal
    case = NativeTests()
    case.setUp()
    cards = []
    def capture(label, body, group):
        buttons = [item['text'] for row in body.get('reply_markup', {}).get('inline_keyboard', []) for item in row]
        reply = body.get('reply_parameters', {}).get('message_id')
        navigation = f'Reply to message {reply}' if reply else ''
        controls = ''.join('<button>' + escape(button) + '</button>' for button in buttons)
        cards.append(f'<section class="{group}"><h2>' + escape(label) + '</h2><article>'
                     + '<div class="navigation">' + escape(navigation) + '</div><div class="body">' + body['text']
                     + '</div><div class="buttons">' + controls + '</div></article></section>')
    try:
        case.native.ask = skill_proposal('Generic long line <&> 🙂; file content only. ' * 35 + '\n')
        case.config['yard'] = 'example'
        case.create()
        sends = [body for url, body in case.calls if url.endswith('/sendMessage')]
        for index, body in enumerate(sends[:-1], 1):
            capture(f'Complete review · part {index}/{len(sends)-1}', body, 'bundle')
        capture('Final bound approval card', sends[-1], 'states')
        case.press()
        capture('Direct reply confirmation · first tap installs nothing', case.edits()[-1], 'states')
        case.native.mode = 'carrying'
        case.reply()
        capture('Approval recorded · pending carry', case.edits()[-1], 'states')
        case.native.ask['ran'].update(result='skill snapshot: installed the captured SKILL.md bytes', pending_reason='')
        case.bridge.run_asks.refresh()
        capture('Settled canonical installation', case.edits()[-1], 'states')
        case.native.ask['ran'].update(result='Synthetic failure: target changed; nothing installed', failed=True)
        case.bridge.run_asks.refresh()
        capture('Alternative canonical failure', case.edits()[-1], 'states')
    finally:
        case.doCleanups()
        case.tearDown()
    return cards


def decision_cards():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bridge'))
    from test_run_asks import NativeTests
    import json
    case = NativeTests()
    case.setUp()
    cards = []

    def capture(label, body):
        rows = body.get('reply_markup', {}).get('inline_keyboard', [])
        controls = ''.join('<div class="buttons">' + ''.join('<button>' + escape(item['text']) + '</button>'
                           for item in row) + '</div>' for row in rows)
        cards.append('<section><h2>' + escape(label) + '</h2><article><div class="body">' + body['text']
                     + '</div>' + controls + '</article></section>')

    def create(name, title):
        fixture = Path(__file__).resolve().parents[1] / 'bridge' / 'fixtures' / ('ordinary_' + name + '.json')
        case.reset_native(json.loads(fixture.read_text()))
        case.config['yard'] = 'example'
        case.native.bead = dict(id='stage-1' if name == 'stage' else 'sample-1', title=title)
        result = case.bridge.api_ask_card(dict(bead=case.native.bead['id'], ask=7, key='preview-' + name))
        if not result['delivered']:
            raise RuntimeError('synthetic preview card refused')
        capture(title, case.calls[-1][1])

    try:
        create('option', 'Ordinary option decision')
        case.reply('2 Keep them until Monday')
        capture('Canonical answer with optional words', case.edits()[-1])
        create('words', 'Free-word answer')
        case.reply('Use the new location 🙂')
        capture('Canonical free-word answer', case.edits()[-1])
        create('stage', 'Synthetic stage decision')
        case.press('later')
        capture('Canonical put-aside state', case.edits()[-1])
        case.native.ask.update(state='open')
        case.bridge.run_asks.refresh()
        capture('Retired original generation · no controls', case.edits()[-1])
        reopened = next(body for url, body in reversed(case.calls) if url.endswith('/sendMessage')
                        and body.get('reply_markup', {}).get('inline_keyboard'))
        capture('Due/reopened generation · new message', reopened)
        case.press('decline', uid=41)
        capture('Canonical declined state', case.edits()[-1])
        case.native.ask.update(state='taken back', by='yard', words='Another stage decider moved the bead')
        case.bridge.run_asks.refresh()
        capture('Canonical sibling take-back', case.edits()[-1])
        create('option', 'Before an unavailable re-presentation')
        row = case.row()
        sent = case.bridge.api_send(dict(text='Unrelated synthetic message', key=f"runask:{row['id']}:send:1"))
        if not sent['delivered']:
            raise RuntimeError('synthetic collision message refused')
        case.bridge.run_asks.client.call('DeferAskExpected', dict(bead='sample-1', ask=7, turn=0, actor='pat'))
        case.native.ask['state'] = 'open'
        case.bridge.run_asks.refresh()
        current = case.row()
        if current['message_id'] is not None or len(current['generations']) != 1:
            raise RuntimeError('synthetic collision must leave the new turn unbound')
        capture('Reopened ask unavailable · old card retired, no new card', case.edits()[-1])
    finally:
        case.doCleanups()
        case.tearDown()
    return cards


def main():
    spec = importlib.util.spec_from_file_location('native_cards', Path(__file__).resolve().parents[1] / 'bridge' / 'run_asks.py')
    native = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(native)
    config = dict(yard='example', yardr_operator='operator')
    bead = dict(id='example-1', title='Review a proposed setting')
    routine = dict(ask=7, to='operator', asker='yardmaster', question='Lower the synthetic source limit?',
                   context=['The import now makes fewer calls.', 'This changes the stored limit; no shell is executed.'],
                   state='open', run=dict(argv=['source', 'set', 'example', 'calls_per_hour=60'],
                   command='yardr source set example calls_per_hour=60', description='Set a source setting',
                   does=['The example source may be called 60 times an hour.'],
                   back='yardr source set example calls_per_hour=120', back_by='an operator'))
    weighty = dict(routine, ask=8, question='Use the complete test command?',
                   weight=[{'class': 'code', 'line': 'brings a program the yard will run: the depot test command'}],
                   run=dict(argv=['depot', 'test', 'web'], command='yardr depot test web', description='Set the depot test command',
                   does=['current: make test', "proposed: sh scripts/test.sh && python3 -m compileall -q bridge"],
                   back='propose the old test command', back_by='an operator'))
    cards = []

    def card(label, ask, footer='', buttons=()):
        _, body = native.render(config, bead, ask)
        controls = ''.join('<button>' + escape(button) + '</button>' for button in buttons)
        cards.append('<section><h2>' + escape(label) + '</h2><article><div class="body">' + body + '</div>'
                     + ('<p class="footer">' + escape(footer) + '</p>' if footer else '')
                     + '<div class="buttons">' + controls + '</div></article></section>')

    card('Routine proposal', routine, buttons=('✓ Approve in example', '✕ Decline'))
    card('Complete old + new setting', weighty, buttons=('✓ Approve in example', '✕ Decline'))
    card('Weight confirmation', weighty,
         "type the ask's number, 8, to approve this weighty card. Reply directly to this card with 8.",
         ('Cancel', '✕ Decline'))
    card('Approval recorded; execution pending', routine,
         '⏳ approved, to be carried out: yardr source set example calls_per_hour=60\napproved, to be carried out\nWaiting for the external report')
    card('Settled canonical outcome', routine,
         '✓ approved, ran: yardr source set example calls_per_hour=60\nsource example: calls_per_hour: 60')
    card('Failed canonical outcome', routine,
         '✕ approved, failed: yardr source set example calls_per_hour=60\nThe synthetic external operation failed')
    if '--skill' in sys.argv:
        cards = skill_cards()
    if '--decisions' in sys.argv:
        cards = decision_cards()
    heading = 'Native ordinary decisions' if '--decisions' in sys.argv else 'Native operator run cards'
    print('''<!doctype html><meta charset="utf-8"><title>Synthetic native card preview</title>
<style>
*{box-sizing:border-box}body{margin:0;padding:28px;background:#e9f0f5;color:#183044;font:15px/1.45 sans-serif}
h1{margin:0;font-size:25px}.note{margin:4px 0 22px;color:#496175}.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:22px}
h2{font-size:16px;margin:0 0 10px}article{background:white;border:1px solid #cfdee8;border-radius:14px;padding:18px;box-shadow:0 3px 12px #142d4310}
.body{white-space:pre-wrap;overflow-wrap:anywhere}pre{white-space:pre-wrap;overflow-wrap:anywhere;font:13px/1.45 monospace;background:#eef4f8;border-radius:6px;padding:9px;margin:0}
b{font-weight:700}.footer{white-space:pre-wrap;border-top:1px solid #d9e5ed;padding-top:12px;margin-bottom:0}
.buttons{display:flex;gap:8px;margin-top:16px}button{flex:1;border:0;border-radius:7px;padding:11px 5px;background:#e2eff9;color:#226295;font-weight:600}
.navigation{font-size:12px;color:#496175;margin-bottom:8px}section{min-width:0}
</style><h1>''' + escape(heading) + '''</h1><p class="note">Offline HTML preview · synthetic canonical fixtures · no Telegram or macOS runtime claim</p><div class="grid">'''
          + ''.join(cards) + '''</div><script>
const view = new URLSearchParams(location.search).get('view');
if (view === 'bundle' || view === 'states') {
  document.querySelectorAll('section').forEach(section => section.hidden = !section.classList.contains(view));
}
</script>''')


if __name__ == '__main__':
    main()
