#!/usr/bin/env python3
"""Render the yard's nudge card and its outcome replies from the fake core with fake Telegram as offline HTML (synthetic data)."""
from html import escape
from pathlib import Path
import sys


def main():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bridge'))
    from test_nudge_cards import NudgeCardTests, nudged, event, nc, JOURNAL
    case = NudgeCardTests(methodName='test_approve_relays_the_outcome_once_as_a_reply_on_the_card')
    case.setUp()
    cards = []

    def capture(label, body, reply=None):
        controls = ''.join('<div class="buttons">' + ''.join('<button>' + escape(item['text']) + '</button>'
                           for item in row) + '</div>' for row in body.get('reply_markup', {}).get('inline_keyboard', []))
        quoted = '<blockquote>' + escape(reply) + '</blockquote>' if reply else ''
        cards.append('<section><h2>' + escape(label) + '</h2><article>' + quoted + '<div class="body">'
                     + body['text'] + '</div>' + controls + '</article></section>')

    try:
        case.native.beads.pop('run-1')
        case.poll()
        card = case.sends()[-1]
        capture('Sent on its own · the yard asked it on a harness error', card)
        case.native.raw_events = [event(30, 'noted', dict(note=21))]
        case.poll(case.tap(40))
        # The fake core answers every approval alike; this row's own result, as a real core gives it.
        case.ask()['ran']['result'] = 'nudge requested: yardmaster'
        case.poll()
        capture('Approved · the nudge is requested in the yard', case.edits()[-1])
        case.native.raw_events += [event(31, 'session_nudge_requested', dict(approved=dict(ask=21, bead=JOURNAL)), actor='pat'),
                                   nudged(32, outcome='typed', reason='')]
        case.poll()
        reply = case.sends()[-1]
        capture("The daemon's outcome · a reply on the card", reply, 'Journal of yardmaster · ask #21')
        capture('Refused · its reason', dict(text=escape(nc.outcome_text(nudged(33)))), 'Journal of yardmaster · ask #21')
        capture('No outcome in ten minutes · said once, never retried',
                dict(text=escape(nc.SILENT)), 'Journal of yardmaster · ask #21')
    finally:
        case.doCleanups()
        case.tearDown()
    print('''<!doctype html><meta charset="utf-8"><title>Synthetic nudge cards</title>
<style>
*{box-sizing:border-box}body{margin:0;padding:24px;background:#e9f0f5;color:#183044;font:14px/1.4 sans-serif}
h1{font-size:25px;margin:0}.note{margin:6px 0 22px;color:#496175}.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:20px}
h2{font-size:16px;margin:0 0 10px}section{min-width:0}article{background:#fff;border:1px solid #cfdee8;border-radius:12px;padding:15px}
.body,pre{white-space:pre-wrap;overflow-wrap:anywhere;font:12px/1.4 monospace}.buttons{display:flex;gap:8px;margin-top:14px}
blockquote{margin:0 0 8px;padding:4px 8px;border-left:3px solid #6aa6d6;color:#496175;font-size:12px}
button{flex:1;border:0;border-radius:7px;padding:10px;background:#e2eff9;color:#226295;font-weight:600}
</style><h1>The yard's nudge cards · published on a harness error</h1>
<p class="note">Synthetic data · a fake core · the bridge's own text and controls · fake Telegram.
No Telegram runtime, installation or live activation claim.</p><div class="grid">'''
          + ''.join(cards) + '</div>')


if __name__ == '__main__':
    main()
