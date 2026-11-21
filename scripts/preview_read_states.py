#!/usr/bin/env python3
"""Render a synthetic chat showing landed (🕊), read (👀), done (👌)
message reactions and an unread-message Nudge offer from the fake core with
fake Telegram as offline HTML."""
from html import escape
from pathlib import Path
import sys


def main():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bridge'))
    from test_read_states import DOVE, EYES, OK_HAND, ReadStateTests, hook_event, nudged, replied_event  # noqa: E402

    case = ReadStateTests(methodName='test_full_lifecycle_replace_not_stack_and_idle_call_counts')
    case.setUp()
    messages = [
        (10, 'Deploy the staging pack after the check passes.'),
        (11, 'Check the latest run log and ack once seen.'),
        (12, 'Update the example settings comment.'),
    ]
    try:
        case._enable_offers(nudge_after=300, status='idle')
        for uid, text in messages:
            case.native.hook_events[uid] = hook_event(uid)
            u = case.update(uid=uid)
            u['message']['text'] = text
            case.drain(u)
        # Msg 10 -> done (replied), Msg 11 -> read (cursor >= 11), Msg 12 -> landed (cursor 11 < 12).
        case.native.crew['yardmaster']['cursor'] = 11
        case.assertTrue(case.bridge.publish(case.publish(key='preview-reply'))['delivered'])
        case.native.journal_events['yardmaster-journal'].append(replied_event(501, 10))
        case.wall[0] += 300
        case.poll()
        states = {mid: case.row(mid)['state'] for mid, _ in messages}
        assert states == {10: 'done', 11: 'read', 12: 'landed'}, states
        last_emoji = {}
        for url, body in case.calls:
            if url.endswith('/setMessageReaction'):
                last_emoji[body['message_id']] = body['reaction'][0]['emoji']
        offer_card = case._sends()[-1]
        token = case.row(12)['offer_token']
        assert token is not None
        case.poll(case._tap(token, uid=40))
        tapped_edit = case._edits()[-1]
        ident = case.native.nudges['crew:yardmaster']['id']
        case.native.journal_events['yardmaster-journal'].append(nudged(502, ident))
        case.poll()
        outcome_reply = case._sends()[-1]
    finally:
        case.doCleanups()
        case.tearDown()

    labels = {
        10: ('done', 'yardr reply logged on journal · replaced 👀 → 👌'),
        11: ('read', 'inbox ack cursor ≥ seq · replaced 🕊 → 👀'),
        12: ('landed', 'accepted by yard hook · cursor below seq · unread 5m'),
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
    offer_buttons = ''.join(
        '<div class="buttons">' + ''.join('<button>' + escape(btn['text']) + '</button>' for btn in row) + '</div>'
        for row in offer_card.get('reply_markup', {}).get('inline_keyboard', [])
    )
    offer_html = (
        f'<div class="row left"><div class="bot">'
        f'<blockquote>{escape(messages[2][1])}</blockquote>'
        f'<div class="card-body">{offer_card["text"]}</div>'
        f'{offer_buttons}'
        f'<div class="meta"><span>silent reply to msg 12 · crew running · idle</span></div>'
        f'</div></div>'
    )
    tapped_html = (
        f'<div class="row left"><div class="bot">'
        f'<blockquote>{escape(messages[2][1])}</blockquote>'
        f'<div class="card-body">{tapped_edit["text"]}</div>'
        f'<div class="meta"><span>verified operator tap · 1 NudgeSession write</span></div>'
        f'</div></div>'
        f'<div class="row left"><div class="bot reply">'
        f'<blockquote>Nudge crew member yardmaster?</blockquote>'
        f'<div class="card-body">{escape(outcome_reply["text"])}</div>'
        f'<div class="meta"><span>daemon session_nudged outcome · message stays {DOVE} until inbox ack</span></div>'
        f'</div></div>'
    )
    print('''<!doctype html><meta charset="utf-8"><title>Synthetic read states and nudge offer</title>
<style>
*{box-sizing:border-box}
body{margin:0;padding:18px;background:#e9f0f5;color:#183044;font:14px/1.45 system-ui,sans-serif}
h1{font-size:19px;margin:0 0 4px}
h2{font-size:15px;margin:18px 0 10px;color:#183044}
.sub{margin:0 0 14px;color:#496175;font-size:12px}
.chat{display:flex;flex-direction:column;gap:12px}
.row{display:flex;justify-content:flex-end}
.row.left{justify-content:flex-start}
.bubble{max-width:92%;background:#effdde;border:1px solid #cce6b8;border-radius:14px 14px 4px 14px;padding:11px 13px;box-shadow:0 1px 2px rgba(24,48,68,.06)}
.bot{max-width:92%;background:#fff;border:1px solid #cfdee8;border-radius:14px 14px 14px 4px;padding:11px 13px;box-shadow:0 1px 2px rgba(24,48,68,.06)}
.bot.reply{margin-left:18px}
blockquote{margin:0 0 8px;padding:4px 8px;border-left:3px solid #6aa6d6;color:#496175;font-size:12px}
.card-body{white-space:pre-wrap;overflow-wrap:anywhere;font-size:13px}
.card-body pre{margin:6px 0 0;padding:7px 9px;background:#f4f8fb;border-radius:6px;white-space:pre-wrap;overflow-wrap:anywhere;font:12px/1.4 monospace}
.buttons{display:flex;gap:8px;margin-top:10px}
button{flex:1;border:0;border-radius:7px;padding:8px 10px;background:#e2eff9;color:#226295;font-weight:600}
.meta{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-top:8px;font-size:11px;color:#4d6b47}
.bot .meta{color:#496175}
.rx{display:inline-flex;align-items:center;padding:2px 8px;border-radius:999px;background:#fff;border:1px solid #c6ddb8;font-size:16px;line-height:1.2}
.legend{margin-top:16px;padding:10px 12px;background:#fff;border:1px solid #cfdee8;border-radius:10px;font-size:12px;color:#395166}
</style>
<h1>Operator message read states &amp; unread nudge offer</h1>
<p class="sub">Synthetic chat · fake core · bridge ledger, setMessageReaction &amp; NudgeSession · fake Telegram</p>
<div class="chat">''' + ''.join(bubbles) + offer_html + '''</div>
<h2>After verified Nudge tap &amp; daemon outcome</h2>
<div class="chat">''' + tapped_html + f'''</div>
<div class="legend"><b>States:</b> {DOVE} landed (hook accepted) · {EYES} read (crew inbox ack ≥ seq) · {OK_HAND} done (reply or react done)</div>''')


if __name__ == '__main__':
    main()
