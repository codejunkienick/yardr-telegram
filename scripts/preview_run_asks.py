#!/usr/bin/env python3
"""Print an offline HTML preview of synthetic native cards; no Telegram access."""
from html import escape
import importlib.util
from pathlib import Path


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
    print('''<!doctype html><meta charset="utf-8"><title>Synthetic native card preview</title>
<style>
*{box-sizing:border-box}body{margin:0;padding:28px;background:#e9f0f5;color:#183044;font:15px/1.45 sans-serif}
h1{margin:0;font-size:25px}.note{margin:4px 0 22px;color:#496175}.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:22px}
h2{font-size:16px;margin:0 0 10px}article{background:white;border:1px solid #cfdee8;border-radius:14px;padding:18px;box-shadow:0 3px 12px #142d4310}
.body{white-space:pre-wrap;overflow-wrap:anywhere}pre{white-space:pre-wrap;overflow-wrap:anywhere;font:13px/1.45 monospace;background:#eef4f8;border-radius:6px;padding:9px;margin:0}
b{font-weight:700}.footer{white-space:pre-wrap;border-top:1px solid #d9e5ed;padding-top:12px;margin-bottom:0}
.buttons{display:flex;gap:8px;margin-top:16px}button{flex:1;border:0;border-radius:7px;padding:11px 5px;background:#e2eff9;color:#226295;font-weight:600}
</style><h1>Native operator run cards</h1><p class="note">Offline HTML preview · synthetic canonical fixtures · no Telegram or macOS runtime claim</p><div class="grid">'''
          + ''.join(cards) + '</div>')


if __name__ == '__main__':
    main()
