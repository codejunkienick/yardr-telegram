#!/usr/bin/env python3
"""Render synthetic model cards through isolated native RPC and production polling.

TGBRIDGE_TEST_YARDR must name the new core test build. No live Telegram or crew.
"""
import copy
import datetime
from html import escape
from pathlib import Path
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bridge'))
from test_model_switch import NativeSwitchTests, m, y  # noqa: E402


def cards():
    case = NativeSwitchTests(methodName='test_complete_real_native_poll_slice')
    found = []
    try:
        case.setUp()
        selector = case.selector()
        found.append(('Choose a model', copy.deepcopy(selector[0])))
        case.rpc('SetContextFor', name='mayor', user='', pct=42, window=200000)
        case.poll(case.pick(selector))
        case.poll()
        found.append(('Waiting · Force available immediately', copy.deepcopy(case.edit())))
        case.bridge = case.reopen()
        case.poll()
        found.append(('Saved admission · Force survives reopen', copy.deepcopy(case.edit())))
        later = m.timestamp(case.last()['deadline']) + datetime.timedelta(seconds=1)
        with patch.object(m, 'now', return_value=later):
            case.poll()
            found.append(('Deadline elapsed · no automatic Force', copy.deepcopy(case.edit())))
        case.poll(case.force_tap())
        found.append(('Force accepted · ordinary restart', copy.deepcopy(case.edit())))
        case.handoff()
        case.launch()
        case.poll()
        found.append(('Running · completed trail, controls removed', copy.deepcopy(case.edit())))
    finally:
        case.doCleanups()
    case = NativeSwitchTests(methodName='test_native_request_id_and_edit_ledger_key_collisions')
    try:
        case.setUp()
        selector = case.selector()
        native = y.run_yardr
        competing = []
        def interleave(argv, *args, **kwargs):
            if '--handoff' in argv:
                request = copy.deepcopy(case.last()['request'])
                request['target']['args'] = ['--effort', 'low']
                competing.append(case.rpc('RequestCrewSwitch', req=request, actor='pat'))
            return native(argv, *args, **kwargs)
        with patch.object(y, 'run_yardr', interleave):
            case.bridge.drain([case.pick(selector)])
        case.bridge = case.reopen()
        case.poll()
        found.append(('Unmatched request · no Force authority', copy.deepcopy(case.edit())))
        case.handoff()
        case.rpc('CrewEndedFor', name='mayor', user='', generation=case.old, reason='handoff', gone=False)
        case.rpc('CrewLaunchedFor', name='mayor', user='', previous=case.old,
                 launch=competing[0]['operation']['target'], handle={'crew_generation': 'synthetic-new-session'})
        case.poll()
        assert case.last()['active'] and case.last()['receipt'] is None
        assert not case.edit()['reply_markup']['inline_keyboard']
        assert {edit['message_id'] for edit in case.telegram.of('editMessageText')} == {selector[1]}
        assert len(case.telegram.of('sendMessage')) == 1
        found.append(('Competing launch · remains unknown', copy.deepcopy(case.edit())))
    finally:
        case.doCleanups()
    return found


def main():
    sections = []
    for title, body in cards():
        controls = ''.join('<div class="buttons">' + ''.join('<button>' + escape(item['text']) + '</button>'
                           for item in row) + '</div>' for row in body['reply_markup']['inline_keyboard'])
        sections.append('<section><h2>' + escape(title) + '</h2><article>' + body['text'] + controls + '</article></section>')
    print('''<!doctype html><meta charset="utf-8"><title>Model handoff cards</title>
<style>
*{box-sizing:border-box}body{margin:0;padding:28px;background:#e9f0f5;color:#183044;font:14px/1.4 sans-serif}
h1{font-size:26px;margin:0}p{color:#496175}.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:22px}
h2{font-size:16px;margin:0 0 12px}section{min-width:0}article{background:#fff;border:1px solid #cfdee8;border-radius:12px;padding:16px}
pre{white-space:pre-wrap;overflow-wrap:anywhere;font:13px/1.5 monospace;margin:0}.buttons{display:flex;gap:8px;margin-top:14px}
button{flex:1;border:0;border-radius:7px;padding:10px;background:#e2eff9;color:#226295;font-weight:600}
</style><h1>Switch models through a handoff</h1>
<p>Synthetic data · isolated native core and SQLite · production poll/drain · fake Telegram · one message per switch</p>
<div class="grid">''' + ''.join(sections) + '</div>')


if __name__ == '__main__':
    main()
