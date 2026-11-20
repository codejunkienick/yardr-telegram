#!/usr/bin/env python3
"""Render the typed /nudge confirm card and its outcome replies from the fake core with fake Telegram as offline HTML (synthetic data)."""
from html import escape
from pathlib import Path
import sys


def main():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bridge'))
    from test_typed_nudge import TypedCommandTests, nudged, tn  # noqa: E402
    case = TypedCommandTests(methodName='test_typed_words_open_confirm_then_tap_writes_once_and_relays_the_outcome')
    case.setUp()
    cards = []

    def capture(label, body, reply=None):
        controls = ''.join('<div class="buttons">' + ''.join('<button>' + escape(item['text']) + '</button>'
                           for item in row) + '</div>' for row in body.get('reply_markup', {}).get('inline_keyboard', []))
        quoted = '<blockquote>' + escape(reply) + '</blockquote>' if reply else ''
        cards.append('<section><h2>' + escape(label) + '</h2><article>' + quoted + '<div class="body">'
                     + body['text'] + '</div>' + controls + '</article></section>')

    try:
        case.command('/nudge yardmaster go on \u2014 where you stopped? \u2713')
        card = case.sends()[-1]
        capture('Confirm card \u00b7 the target, its status, the exact line', card)
        row = case.row()
        case.poll(case.tap(row['token'], uid=40))
        edit = [body for url, body in case.calls if url.endswith('/editMessageText')][-1]
        capture('Tapped \u00b7 requested, the buttons are off', edit)
        ident = case.native.nudges['crew:yardmaster']['id']
        case.native.raw_events.append(nudged(33, ident))
        case.poll()
        capture("The daemon's outcome \u00b7 a reply on the card", case.sends()[-1], 'Nudge crew member yardmaster?')
        capture('Refused \u00b7 its reason', dict(text=escape(tn.nc.outcome_text(nudged(34, ident, outcome='refused',
                                                                                  reason='the input holds text')))),
                'Nudge crew member yardmaster?')
        capture('No outcome in ten minutes \u00b7 said once, never retried', dict(text=escape(tn.SILENT)),
                'Nudge crew member yardmaster?')
    finally:
        case.doCleanups()
        case.tearDown()
    print('''<!doctype html><meta charset="utf-8"><title>Synthetic typed nudge</title>
<style>
*{box-sizing:border-box}body{margin:0;padding:24px;background:#e9f0f5;color:#183044;font:14px/1.4 sans-serif}
h1{font-size:25px;margin:0}.note{margin:6px 0 22px;color:#496175}.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:20px}
h2{font-size:16px;margin:0 0 10px}section{min-width:0}article{background:#fff;border:1px solid #cfdee8;border-radius:12px;padding:15px}
.body,pre{white-space:pre-wrap;overflow-wrap:anywhere;font:12px/1.4 monospace}.buttons{display:flex;gap:8px;margin-top:14px}
blockquote{margin:0 0 8px;padding:4px 8px;border-left:3px solid #6aa6d6;color:#496175;font-size:12px}
button{flex:1;border:0;border-radius:7px;padding:10px;background:#e2eff9;color:#226295;font-weight:600}
</style><h1>Typed /nudge \u00b7 nudge an agent with your own words</h1>
<p class="note">Synthetic data \u00b7 a fake core \u00b7 the bridge's own text and controls \u00b7 fake Telegram.
No Telegram runtime, installation or live activation claim.</p><div class="grid">'''
          + ''.join(cards) + '</div>')


if __name__ == '__main__':
    main()
