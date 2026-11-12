#!/usr/bin/env python3
"""Render siding cards from a real isolated yardr with fake Telegram as offline HTML (synthetic data).

The build is the one the native tests use: TGBRIDGE_TEST_YARDR, else yardr on PATH.
"""
from html import escape
from pathlib import Path
import sys


def main():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bridge'))
    from test_siding_cards import SidingTests
    case = SidingTests(methodName='test_card_folds_exactly_the_shown_beads_and_unfold_returns_them')
    case.setUp()
    cards = []

    def capture(label, group, card):
        body = None
        for url, sent in case.calls:
            if url.endswith('/sendMessage') and sent.get('reply_markup') is not None and sent['text'] == card['html']:
                body = sent
            elif url.endswith('/editMessageText') and sent['message_id'] == card['message_id']:
                body = sent
        controls = ''.join('<div class="buttons">' + ''.join('<button>' + escape(item['text']) + '</button>'
                           for item in row) + '</div>' for row in body['reply_markup']['inline_keyboard'])
        cards.append('<section class="' + group + '"><h2>' + escape(label) + '</h2><article><div class="body">'
                     + body['text'] + '</div>' + controls + '</article></section>')

    try:
        a, held, c = case.part('a'), case.part('b'), case.part('c')
        case.cli('bead', 'hold', held)
        row = case.card(key='open')
        capture('Open · every waiting bead, the held one stuck; the exact proposal and its carrier stage', 'open', row)
        case.press(row)
        capture('Settled · one fold of exactly the shown beads; carrier, its stage and the undo', 'settled', case.row(row))
        large = [case.part('d', 'large'), case.part('e', 'large'), case.part('f')]
        status = case.card(key='status')
        capture('Status only · over fold_max; a reply asks for a card for an explicit subset', 'status', status)
        case.reply(status, f'fold {large[0]} {large[1]}', uid=41)
        explicit, = case.explicit()
        capture('Explicit subset · a new card, no points max; the reply folded nothing', 'open', explicit)
        case.cli('bead', 'hold', large[1])
        case.bridge.inbound(case.press_update(explicit, uid=42))
        capture('Refused · a member was held after the card; nothing folded', 'refused', case.row(explicit))
    finally:
        case.doCleanups()
        case.tearDown()
    print('''<!doctype html><meta charset="utf-8"><title>Synthetic siding cards</title>
<style>
*{box-sizing:border-box}body{margin:0;padding:24px;background:#e9f0f5;color:#183044;font:14px/1.4 sans-serif}
h1{font-size:25px;margin:0}.note{margin:6px 0 22px;color:#496175}.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:20px}
h2{font-size:16px;margin:0 0 10px}section{min-width:0}article{background:#fff;border:1px solid #cfdee8;border-radius:12px;padding:15px}
.body{white-space:pre-wrap;overflow-wrap:anywhere;font:12px/1.4 monospace}.buttons{display:flex;gap:8px;margin-top:14px}
button{flex:1;border:0;border-radius:7px;padding:10px;background:#e2eff9;color:#226295;font-weight:600}
</style><h1>Siding cards · plain bead listing and fold</h1>
<p class="note">Synthetic data · a real disposable yardr store over its socket · the bridge's own text and controls ·
fake Telegram. No Telegram runtime, installation or live activation claim.</p><div class="grid">'''
          + ''.join(cards) + '''</div><script>
const view=new URLSearchParams(location.search).get('view');
if(view)document.querySelectorAll('section').forEach(s=>s.hidden=!s.classList.contains(view));
</script>''')


if __name__ == '__main__':
    main()
