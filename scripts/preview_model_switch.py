#!/usr/bin/env python3
"""Render synthetic model cards through isolated native RPC and production polling.

TGBRIDGE_TEST_YARDR (or yardr on PATH) must name a core with crew_handoff_switch v1.
No live Telegram or crew.
"""
import copy
import datetime
from html import escape
from pathlib import Path
import secrets
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bridge'))
from test_model_switch import NativeSwitchTests, m, y  # noqa: E402


def _capture_lifecycle(found):
    case = NativeSwitchTests(methodName='test_complete_real_native_poll_slice')
    try:
        case.setUp()
        selector = case.selector()
        found.append(('1. Initial picks', 'initial', copy.deepcopy(selector[0])))
        case.rpc('SetContextFor', name='mayor', user='', pct=42, window=200000)
        case.poll(case.pick(selector))
        found.append(('2. Waiting to deliver · working', 'waiting', copy.deepcopy(case.edit())))
        op = case.last()['observed']
        case.rpc('StepCrewSwitch', name='mayor', id=op['id'], generation=op['generation'],
                 session=case.old, step='nudged')
        case.rpc('CrewStatusFor', name='mayor', user='', status='blocked')
        case.poll()
        found.append(('3. Waiting for written handoff · blocked', 'blocked', copy.deepcopy(case.edit())))
        case.rpc('CrewStatusFor', name='mayor', user='', status='idle')
        case.handoff()
        case.poll()
        found.append(('4. Handoff written · awaiting restart', 'handoff', copy.deepcopy(case.edit())))
        case.rpc('CrewEndedFor', name='mayor', user='', generation=case.old,
                 reason='restart: ' + case.crew().get('restart_requested', 'handoff'), gone=False)
        case.poll()
        found.append(('5. Restarting · old session ending', 'restarting', copy.deepcopy(case.edit())))
        case.rpc('CrewLaunchedFor', name='mayor', user='', previous=case.old,
                 launch=op['target'], handle={'crew_generation': secrets.token_hex(12)})
        case.poll()
        found.append(('6. Running on target · completed trail', 'running', copy.deepcopy(case.edit())))
    finally:
        case.doCleanups()


def _capture_skipped_polls_and_force(found):
    # Skip intermediate polls between admission and new launch; trail retains every milestone.
    case = NativeSwitchTests(methodName='test_complete_real_native_poll_slice')
    try:
        case.setUp()
        selector = case.selector()
        case.rpc('SetContextFor', name='mayor', user='', pct=68, window=200000)
        case.poll(case.pick(selector))
        op = case.last()['observed']
        case.rpc('StepCrewSwitch', name='mayor', id=op['id'], generation=op['generation'],
                 session=case.old, step='nudged')
        case.handoff()
        case.launch()
        case.poll()
        found.append(('7. Running · skipped polls keep trail', 'trail', copy.deepcopy(case.edit())))
    finally:
        case.doCleanups()

    # Timeout + explicit Force pending idle + natural exit without handoff.
    case = NativeSwitchTests(methodName='test_timeout_card_preserves_target_no_automatic_force_and_explicit_force')
    try:
        case.setUp()
        case.rpc('SetContextFor', name='mayor', user='', pct=55, window=200000)
        case.poll(case.pick(case.selector()))
        later = m.timestamp(case.last()['deadline']) + datetime.timedelta(seconds=1)
        with patch.object(m, 'now', return_value=later):
            case.poll()
            found.append(('8. Timeout (600s) · Force apply kept', 'timeout', copy.deepcopy(case.edit())))
        case.poll(case.force_tap())
        found.append(('9. Explicit Force · waits for idle', 'force', copy.deepcopy(case.edit())))
    finally:
        case.doCleanups()

    case = NativeSwitchTests(methodName='test_natural_exit_external_supersession_and_failed_start')
    try:
        case.setUp()
        case.poll(case.pick(case.selector()))
        row = case.last()
        case.rpc('CrewEndedFor', name='mayor', user='', generation=case.old, reason='the session is gone', gone=True)
        case.rpc('CrewLaunchedFor', name='mayor', user='', previous=case.old,
                 launch=row['observed']['target'], handle={'crew_generation': secrets.token_hex(12)})
        case.poll()
        found.append(('10. Natural exit without handoff · running', 'natural', copy.deepcopy(case.edit())))
    finally:
        case.doCleanups()


def _capture_errors_and_edge_cases(found):
    # Refused admission (command did not start).
    case = NativeSwitchTests(methodName='test_bad_capability_status_and_output_never_reserve_or_disclose')
    try:
        case.setUp()
        selector = case.selector()
        original = y.run_yardr
        def not_started(argv, *args, **kwargs):
            if '--handoff' in argv:
                return None, b'', b'', 'not_started'
            return original(argv, *args, **kwargs)
        with patch.object(y, 'run_yardr', not_started):
            case.bridge.drain([case.pick(selector)])
        found.append(('11. Refused · native command did not start', 'refused', copy.deepcopy(case.edit())))
    finally:
        case.doCleanups()

    # Unknown / unmatched request on reopen (competing request took ID).
    case = NativeSwitchTests(methodName='test_native_request_id_and_edit_ledger_key_collisions')
    try:
        case.setUp()
        selector = case.selector()
        native = y.run_yardr
        def interleave(argv, *args, **kwargs):
            if '--handoff' in argv:
                request = copy.deepcopy(case.last()['request'])
                request['target']['args'] = ['--effort', 'low']
                case.rpc('RequestCrewSwitch', req=request, actor='pat')
            return native(argv, *args, **kwargs)
        with patch.object(y, 'run_yardr', interleave):
            case.bridge.drain([case.pick(selector)])
        case.bridge = case.reopen()
        case.poll()
        found.append(('12. Unknown · unmatched request on reopen', 'unknown', copy.deepcopy(case.edit())))
    finally:
        case.doCleanups()

    # Unavailable / stale observation with missing metadata.
    case = NativeSwitchTests(methodName='test_missing_and_stale_telemetry_sanitized_and_payload_limits')
    try:
        case.setUp()
        selector = case.selector()
        case.poll(case.pick(selector))
        row = case.last()
        row['telemetry_at'] = '2020-01-01T00:00:00Z'
        case.bridge.model_switch.save(row)
        with patch.object(y, 'run_yardr', return_value=(1, b'', b'PRIVATE', None)):
            case.poll()
        found.append(('13. Unavailable & stale · missing context', 'unavailable', copy.deepcopy(case.edit())))
    finally:
        case.doCleanups()

    # Long labels, HTML escaping, and non-leaking env_file.
    case = NativeSwitchTests(methodName='test_missing_and_stale_telemetry_sanitized_and_payload_limits')
    try:
        case.setUp()
        config = copy.deepcopy(case.config['yardr_switch_model'])
        config['picks'][1]['id'] = 'x' * 32
        config['picks'][1]['label'] = '<Ops & Prod> 32-char label-pad!!'[:32]
        config['picks'][1]['model'] = 'claude-opus-4-synthetic-long-model-name'
        config['picks'][1]['env_file'] = '/synthetic/private/crew-secret.env'
        case.bridge = case.reopen(yardr_switch_model=config)
        selector = case.selector()
        case.poll(case.pick(selector, 'x' * 32))
        rendered = copy.deepcopy(case.edit())
        assert 'crew-secret.env' not in rendered['text']
        assert '&lt;Ops &amp; Prod&gt;' in rendered['text']
        found.append(('14. Escaped long label · env_file omitted', 'escaping', rendered))
    finally:
        case.doCleanups()

    case = NativeSwitchTests(methodName='test_native_refusal_stale_revision_and_unknown_receipt_collision')
    try:
        case.setUp()
        selector = case.selector()
        native = y.run_yardr
        def stale(argv, *args, **kwargs):
            if '--handoff' in argv:
                case.rpc('CrewStatusFor', name='mayor', user='', status='idle')
            return native(argv, *args, **kwargs)
        with patch.object(y, 'run_yardr', stale):
            case.poll(case.pick(selector))
        found.append(('15. Refused · native admission rejected', 'admission-refused', copy.deepcopy(case.edit())))
    finally:
        case.doCleanups()

    case = NativeSwitchTests(methodName='test_exact_actual_launch_noop_and_same_kind_model_effort_change')
    try:
        case.setUp()
        case.native_launch_pick(case.picks[1])
        case.poll(case.pick(case.selector()))
        found.append(('16. Already running · no switch requested', 'already-running', copy.deepcopy(case.edit())))
    finally:
        case.doCleanups()


def cards_with_groups():
    found = []
    _capture_lifecycle(found)
    _capture_skipped_polls_and_force(found)
    _capture_errors_and_edge_cases(found)
    return found


def cards():
    return [(title, body) for title, _group, body in cards_with_groups()]


def html():
    sections = []
    for title, group, body in cards_with_groups():
        controls = ''.join('<div class="buttons">' + ''.join('<button>' + escape(item['text']) + '</button>'
                           for item in row) + '</div>' for row in body['reply_markup']['inline_keyboard'])
        sections.append('<section class="' + escape(group) + '"><h2>' + escape(title)
                        + '</h2><article>' + body['text'] + controls + '</article></section>')
    return '''<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Model handoff cards</title>
<style>
*{box-sizing:border-box}body{margin:0;padding:24px;background:#e9f0f5;color:#183044;font:14px/1.4 sans-serif}
h1{font-size:24px;margin:0}p{color:#496175;margin:6px 0 20px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,380px),1fr));gap:18px}
h2{font-size:15px;margin:0 0 10px}section{min-width:0}
article{background:#fff;border:1px solid #cfdee8;border-radius:12px;padding:14px}
pre{white-space:pre-wrap;overflow-wrap:anywhere;font:12.5px/1.45 monospace;margin:0}
.buttons{display:flex;gap:8px;margin-top:12px}
button{flex:1;border:0;border-radius:7px;padding:10px;background:#e2eff9;color:#226295;font-weight:600}
@media (max-width:600px){body{padding:12px}.grid{grid-template-columns:1fr;gap:14px}h1{font-size:20px}}
</style><h1>Switch models through a handoff</h1>
<p>Synthetic data · isolated native core and SQLite · production poll/drain · fake Telegram · one message per switch</p>
<div class="grid">''' + ''.join(sections) + '''</div><script>
const view=new URLSearchParams(location.search).get('view');
if(view){const wanted=new Set(view.split(','));document.querySelectorAll('section').forEach(s=>s.hidden=!wanted.has(s.className));}
</script>'''


def main():
    print(html())


if __name__ == '__main__':
    main()
