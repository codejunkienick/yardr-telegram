#!/usr/bin/env python3
"""Render real isolated native siding states with fake Telegram as offline HTML."""
from html import escape
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch


def main():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bridge'))
    from test_siding_cards import SidingTests, FLOW
    case = SidingTests(methodName='test_real_fold_release_and_replay')
    case.setUp()
    cards = []

    def capture(label, group='actions', card=None, receipt=None):
        messages, sent = {}, 76
        for url, body in case.calls:
            if url.endswith('/sendMessage'):
                sent += 1
                messages[sent] = body
            elif url.endswith('/editMessageText'):
                messages[body['message_id']] = body
        body = messages[card['message_id']] if card else next(
            body for url, body in reversed(case.calls)
            if url.endswith(('/sendMessage', '/editMessageText')) and 'reply_markup' in body)
        controls = ''.join('<div class="buttons">' + ''.join('<button>' + escape(item['text']) + '</button>'
                           for item in row) + '</div>' for row in body['reply_markup']['inline_keyboard'])
        result = ('<p class="receipt">Close receipt: ' + escape(str(receipt)) + '</p>') if receipt else ''
        cards.append('<section class="' + group + '"><h2>' + escape(label) + '</h2>' + result + '<article><div class="body">'
                     + body['text'] + '</div>' + controls + '</article></section>')

    try:
        case.part('shared-a')
        case.part('shared-b')
        first, second = case.card(key='shared-first'), case.card(key='shared-second')
        case.assertEqual(first['canonical'], second['canonical'])
        capture('Two cards share the exact proposal · neither has attempted a write', 'shared', second)
        case.press(first)
        capture('Human approved the first card · one canonical carrier', 'shared', first)
        case.restart()
        receipt = case.bridge.api_ask_close(dict(ask_key='shared-second', decision='Canonical departure only'))
        case.assertTrue(receipt['delivered'], receipt)
        retired = case.bridge.siding_cards.get(second['id'])
        case.assertFalse(retired['attempted'])
        case.assertIsNone(retired['outcome'])
        case.assertEqual(case.bridge.siding_cards.state(retired), 'stale')
        case.assertEqual(len(case.client.call('EventsOfKind', ['folded', 100])), 1)
        capture('Other card retired after restart · confirmed own receipt, no adopted outcome or second effect',
                'shared', second, receipt)
        one, two, three = [case.part(str(i)) for i in range(1, 4)]
        row = case.card()
        capture('Store-backed fold · complete canonical selection')
        case.reply(row, f'fold {two} {one}')
        selected = next(r for r in case.bridge.siding_cards.rows() if r['canonical']['ask']['members'] == [two, one])
        capture('Explicit ordered subset · new human approval required')
        case.press(selected, uid=42)
        case.assertEqual(case.bridge.siding_cards.state(case.bridge.siding_cards.get(selected['id'])), 'settled')
        capture('Canonical carrier · native event and current stage')
        remaining = case.card(key='remaining')
        case.reply(remaining, 'release ' + three, uid=43)
        release = next(r for r in case.bridge.siding_cards.rows() if r['canonical']['ask']['action'] == 'release')
        capture('Store-backed single release · exact bead and target')
        case.press(release, uid=44)
        capture('Canonical released bead · no remaining controls')
        case.assertIsNone(case.card(key='empty')['canonical'])
        capture('Empty siding · explicit status only', 'status')
        case.part('4', 'large')
        case.part('5', 'large')
        case.card(key='ceiling')
        capture('Ceiling warning · complete status with no approval', 'status')
        repo = case.root / 'depot' / '.yardr' / 'flows' / 'default.flow'
        repo.parent.mkdir(parents=True)
        repo.write_text(FLOW)
        case.cli('depot', 'flow', 'sample', 'default')
        case.card(key='repository')
        capture('Repository flow · status-only limitation and named source', 'status')
        case.cli('depot', 'flow', 'sample', 'rotation.f')
        case.cli('group', 'set', 'keepers', 'fold_max=10')
        waiting = case.card(key='leave')
        case.press(waiting, action='leave', uid=45)
        capture('Withdrawn · leave waiting makes no native write', 'status')
        uncertain = case.card(key='pruned')
        from test_siding_cards import n
        native = case.bridge.siding_cards.client.call
        committed = []
        def lost_outcome(method, args):
            if method == 'FoldSidingExpected' and args:
                committed.append(native(method, args))
                raise n.RPCAmbiguous('synthetic lost response after commit')
            if method == 'SidingOutcome' and args and committed:
                raise n.RPCAmbiguous('synthetic unavailable outcome')
            return native(method, args)
        with patch.object(case.bridge.siding_cards.client, 'call', side_effect=lost_outcome):
            case.press(uncertain, uid=46)
        carrier = committed[0]['carrier']['id']
        case.cli('bead', 'note', carrier, 'Synthetic retention noise')
        case.client.call('Prune', [dict(events=1, hook_payloads=1, inbox=1), 'pat'])
        case.bridge.siding_cards.refresh()
        capture('Pruned outcome · uncertainty, no automatic retry', 'status')
        flow = case.root / 'rotation.flow'
        flow.write_text(FLOW.replace('repair -> harbor | launch', 'repair -> launch | harbor'))
        case.cli('flow', 'set', 'rotation.f', '-f', str(flow))
        case.cli('group', 'add', 'menders', '--runner', 'manual', '--members', 'pat', '--set', 'fold_after=24h')
        case.cli('route', 'add', '--stage', 'repair', '--group', 'menders', '--depot', 'sample')
        case.part('6')
        case.part('7')
        for name in ('8', '9'):
            case.cli('bead', 'create', 'Synthetic repair ' + name, '--id', 'sample-' + name,
                     '--depot', 'sample', '--stage', 'repair', '--label', 'size:small')
        batch = case.card(key='batch')
        first, sibling = [case.bridge.siding_cards.get(ident) for ident in batch['batch']]
        case.press(first, uid=47)
        receipt = case.bridge.api_ask_close(dict(ask_key='batch', decision='Caller text is not authority'))
        case.assertFalse(receipt['delivered'], receipt)
        capture('Batch close delivered=false · first siding settled', 'batch', first)
        capture('Open sibling · still awaiting the paired human', 'batch', sibling)
        case.press(sibling, uid=48)
        receipt = case.bridge.api_ask_close(dict(ask_key='batch', decision='Canonical closure only'))
        case.assertTrue(receipt['delivered'], receipt)
        capture('Batch close delivered=true · both sidings settled', 'batch', sibling)
        for route in case.client.call('Routes', []):
            if route['stage'] == 'harbor':
                case.cli('route', 'rm', route['id'])
        case.cli('group', 'add', 'sample-keepers', '--runner', 'manual', '--members', 'pat', '--set', 'fold_after=24h')
        case.cli('route', 'add', '--stage', 'harbor', '--group', '{depot}-keepers')
        case.part('10')
        case.part('11')
        templated = case.card(key='templated', stage='harbor', group='sample-keepers')
        capture('Depot template {depot}-keepers → sample-keepers', 'review', templated)
        value = templated['canonical']
        case.client.call('FoldSidingExpected', dict(ask=value['ask'], digest=value['digest'], op=templated['op'], actor='pat'))
        def reject_edit(url, body, *args, **kwargs):
            if url.endswith('/editMessageText'):
                return 400, dict(ok=False, error_code=400, description='synthetic rejected edit')
            return case.telegram(url, body, *args, **kwargs)
        with patch.object(case.bridge, 'request', side_effect=reject_edit):
            receipt = case.bridge.api_ask_close(dict(ask_key='templated', decision='Canonical state only'))
            case.assertFalse(receipt['delivered'], receipt)
        capture('Fold recorded · close delivered=false while Telegram retains old controls', 'review', templated)
        case.restart()
        receipt = case.bridge.api_ask_close(dict(ask_key='templated', decision='Retry canonical presentation'))
        case.assertTrue(receipt['delivered'], receipt)
        capture('After restart · delivered=true only after confirmed retirement', 'review', templated)
        case.part('12')
        case.part('13')
        stale = case.card(key='stale-edit', stage='harbor', group='sample-keepers')
        case.cli('group', 'set', 'sample-keepers', 'fold_after=12h')
        with patch.object(case.bridge, 'request', side_effect=reject_edit):
            receipt = case.bridge.api_ask_close(dict(ask_key='stale-edit', decision='Stale proposal'))
            case.assertFalse(receipt['delivered'], receipt)
        capture('Stale proposal · close delivered=false after rejected retirement', 'review', stale)
        case.restart()
        receipt = case.bridge.api_ask_close(dict(ask_key='stale-edit', decision='Retry stale presentation'))
        case.assertTrue(receipt['delivered'], receipt)
        capture('Stale retirement retried after restart · controls removed', 'review', stale)
        open_card = case.card(key='closure-open', stage='harbor', group='sample-keepers')
        receipt = case.bridge.api_ask_close(dict(ask_key='closure-open', decision='No authority'))
        case.assertFalse(receipt['delivered'], receipt)
        capture('Open store card · only the human can decide', 'closure', open_card, receipt)
        pending = case.card(key='closure-uncertain', stage='harbor', group='sample-keepers')
        def in_flight(method, args):
            if method == 'FoldSidingExpected' and args:
                raise n.RPCAmbiguous('synthetic in-flight response lost')
            return native(method, args)
        with patch.object(case.bridge.siding_cards.client, 'call', side_effect=in_flight):
            case.press(pending, uid=60)
        receipt = case.bridge.api_ask_close(dict(ask_key='closure-uncertain', decision='No authority'))
        case.assertFalse(receipt['delivered'], receipt)
        capture('Uncertain operation · refuses closure even without controls', 'closure', pending, receipt)
        empty = case.card(key='closure-empty', stage='complete')
        receipt = case.bridge.api_ask_close(dict(ask_key='closure-empty', decision='No action'))
        case.assertTrue(receipt['delivered'], receipt)
        capture('No-siding status · confirmed publish receipt', 'closure', empty, receipt)
        expired = case.card(key='closure-expired', stage='harbor', group='sample-keepers')
        expired['expires'] = 0
        case.bridge.siding_cards.save(expired)
        receipt = case.bridge.api_ask_close(dict(ask_key='closure-expired', decision='Expired review'))
        case.assertTrue(receipt['delivered'], receipt)
        capture('Expired · confirmed control-free presentation', 'closure', expired, receipt)
        receipt = case.bridge.api_ask_close(dict(ask_key='templated', decision='Canonical settlement'))
        case.assertTrue(receipt['delivered'], receipt)
        capture('Settled · native fact plus confirmed Telegram edit', 'closure', templated, receipt)
        wrong = case.card(key='closure-pairing', stage='harbor', group='sample-keepers')
        value = wrong['canonical']
        case.client.call('FoldSidingExpected', dict(ask=value['ask'], digest=value['digest'], op=wrong['op'], actor='pat'))
        with patch.object(case.bridge, 'request', side_effect=reject_edit):
            case.assertFalse(case.bridge.api_ask_close(dict(ask_key='closure-pairing', decision='Retire'))['delivered'])
        case.cli('bead', 'create', 'Synthetic previous chat run card', '--id', 'sample-old',
                 '--depot', 'sample', '--stage', 'intake')
        case.cli('user', 'add', 'proposer')
        asked = subprocess.run(case.command[:-1] + ['proposer', 'bead', 'ask', 'sample-old',
            'Change the synthetic default?', '--to', 'pat', '--context', 'Synthetic chat collision',
            '--run', 'settings set default_kind claude'],
            env=case.env, cwd=case.root, capture_output=True, text=True, timeout=20)
        case.assertEqual(asked.returncode, 0, asked.stderr)
        number = case.client.call('Bead', dict(bead='sample-old', viewer='pat'))['asks'][-1]['ask']
        sent = case.bridge.api_run_ask(dict(bead='sample-old', ask=number, key='closure-old-run'))
        case.assertTrue(sent['delivered'], sent)
        old_message = int(sent['message_id'])
        case.cli('user', 'identity', 'pat', '--source', 'tgbridge', '--id', '456')
        case.config.update(user_id=456, chat_id=456)
        case.restart()
        receipt = case.bridge.api_ask_close(dict(ask_key='closure-pairing', decision='New pairing'))
        case.assertEqual(receipt['reason'], 'siding_wrong_yard_or_pairing')
        capture('Changed pairing · old visible controls cannot be acknowledged or edited into the new chat',
                'closure', wrong, receipt)
        case.part('14')
        case.part('15')
        case.counter = old_message - 1
        current = case.card(key='closure-collision', stage='harbor', group='sample-keepers')
        case.assertEqual(current['message_id'], old_message)
        value = current['canonical']
        case.client.call('FoldSidingExpected', dict(ask=value['ask'], digest=value['digest'], op=current['op'], actor='pat'))
        case.restart()
        receipt = case.bridge.api_ask_close(dict(message_id=old_message, decision='Current chat closure'))
        case.assertTrue(receipt['delivered'], receipt)
        case.assertEqual(int(receipt['message_id']), old_message)
        capture(f'Chat 456 siding closes · old chat 123 run card also has message {old_message}', 'closure', receipt=receipt)
        receipt = case.bridge.api_ask_close(dict(ask_key='closure-old-run', decision='Old chat remains bound'))
        case.assertEqual(receipt['reason'], 'run_ask_wrong_yard_or_pairing')
        held, asked = case.part('held'), case.part('asked')
        case.cli('bead', 'hold', held)
        case.cli('bead', 'ask', asked, 'Synthetic waiting question', '--to', 'pat', '--context', 'Synthetic status review')
        stuck = case.card(key='closure-stuck', stage='harbor', group='sample-keepers')
        case.assertEqual(stuck['canonical']['ask']['members'], [])
        case.restart()
        receipt = case.bridge.api_ask_close(dict(message_id=stuck['message_id'], decision='Status receipt only'))
        case.assertTrue(receipt['delivered'], receipt)
        capture('All waiting beads held or asked · confirmed status receipt after restart', 'closure', receipt=receipt)
    finally:
        case.doCleanups()
        case.tearDown()
    print('''<!doctype html><meta charset="utf-8"><title>Synthetic native siding cards</title>
<style>
*{box-sizing:border-box}body{margin:0;padding:24px;background:#e9f0f5;color:#183044;font:14px/1.4 sans-serif}
h1{font-size:25px;margin:0}.note{margin:6px 0 22px;color:#496175}.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:20px}
h2{font-size:16px;margin:0 0 10px}section{min-width:0}article{background:#fff;border:1px solid #cfdee8;border-radius:12px;padding:15px}
.receipt{overflow-wrap:anywhere;background:#fff3d6;padding:10px;border-radius:7px}
.body{white-space:pre-wrap;overflow-wrap:anywhere;font:12px/1.4 monospace}.buttons{display:flex;gap:8px;margin-top:14px}
button{flex:1;border:0;border-radius:7px;padding:10px;background:#e2eff9;color:#226295;font-weight:600}
</style><h1>Native siding cards · store-backed first slice</h1>
<p class="note">Synthetic data · real disposable installed core/store/RPC · actual bridge text and controls · fake Telegram.
No Telegram runtime, installation or live activation claim. Repository-backed mutations remain unsupported.</p><div class="grid">'''
          + ''.join(cards) + '''</div><script>
const view=new URLSearchParams(location.search).get('view');
if(view)document.querySelectorAll('section').forEach(s=>s.hidden=!s.classList.contains(view));
</script>''')


if __name__ == '__main__':
    main()
