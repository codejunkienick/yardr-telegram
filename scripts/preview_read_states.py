#!/usr/bin/env python3
"""Render a synthetic chat showing landed (🕊), read (👀) and done (👌)
message reactions from the fake core with fake Telegram as offline HTML."""
from html import escape
from pathlib import Path
import sys


def main():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bridge'))
    from test_read_states import DOVE, EYES, OK_HAND, ReadStateTests, hook_event, replied_event  # noqa: E402

    case = ReadStateTests(methodName='test_full_lifecycle_replace_not_stack_and_idle_call_counts')
    case.setUp()
    messages = [
        (10, 'Deploy the staging pack after the check passes.'),
        (11, 'Check the latest run log and ack once seen.'),
        (12, 'Update the example settings comment.'),
    ]
    try:
        for uid, text in messages:
            case.native.hook_events[uid] = hook_event(uid)
            u = case.update(uid=uid)
            u['message']['text'] = text
            case.drain(u)
        # Msg 10 -> done (replied), Msg 11 -> read (cursor >= 11), Msg 12 -> landed (cursor 11 < 12).
        case.native.crew['yardmaster']['cursor'] = 11
        case.assertTrue(case.bridge.publish(case.publish(key='preview-reply'))['delivered'])
        case.native.journal_events['yardmaster-journal'].append(replied_event(501, 10))
        case.poll()
        states = {mid: case.row(mid)['state'] for mid, _ in messages}
        assert states == {10: 'done', 11: 'read', 12: 'landed'}, states
        last_emoji = {}
        for url, body in case.calls:
            if url.endswith('/setMessageReaction'):
                last_emoji[body['message_id']] = body['reaction'][0]['emoji']
    finally:
        case.doCleanups()
        case.tearDown()

    labels = {
        10: ('done', 'yardr reply logged on journal · replaced 👀 → 👌'),
        11: ('read', 'inbox ack cursor ≥ seq · replaced 🕊 → 👀'),
        12: ('landed', 'accepted by yard hook · cursor below seq'),
    }
    bubbles = []
    for mid, text in messages:
        state, note = labels[mid]
        emoji = last_emoji[mid]
        bubbles.append(
            f'<div class="row">'
            f'<div class="bubble">{escape(text)}'
            f'<div class="meta"><span>{escape(state)} · {escape(note)}</span>'
            f'<span class="rx">{escape(emoji)}</span></div></div></div>'
        )
    print('''<!doctype html><meta charset="utf-8"><title>Synthetic read states</title>
<style>
*{box-sizing:border-box}
body{margin:0;padding:18px;background:#e9f0f5;color:#183044;font:14px/1.45 system-ui,sans-serif}
h1{font-size:19px;margin:0 0 4px}
.sub{margin:0 0 16px;color:#496175;font-size:12px}
.chat{display:flex;flex-direction:column;gap:14px}
.row{display:flex;justify-content:flex-end}
.bubble{max-width:92%;background:#effdde;border:1px solid #cce6b8;border-radius:14px 14px 4px 14px;padding:11px 13px;box-shadow:0 1px 2px rgba(24,48,68,.06)}
.meta{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-top:8px;font-size:11px;color:#4d6b47}
.rx{display:inline-flex;align-items:center;padding:2px 8px;border-radius:999px;background:#fff;border:1px solid #c6ddb8;font-size:16px;line-height:1.2}
.legend{margin-top:18px;padding:10px 12px;background:#fff;border:1px solid #cfdee8;border-radius:10px;font-size:12px;color:#395166}
</style>
<h1>Operator message read states</h1>
<p class="sub">Synthetic chat · fake core · bridge ledger &amp; setMessageReaction · fake Telegram</p>
<div class="chat">''' + ''.join(bubbles) + f'''</div>
<div class="legend"><b>States:</b> {DOVE} landed (hook accepted) · {EYES} read (crew inbox ack ≥ seq) · {OK_HAND} done (reply or react done)</div>''')


if __name__ == '__main__':
    main()
