#!/usr/bin/env python3
"""Render a synthetic chat showing a message move through landed (🕊), read (👀),
working (✍) and done (👌), active typing, and an unread-message Nudge offer
from the fake core with fake Telegram as offline HTML."""
import argparse
from html import escape
from pathlib import Path
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--instance', choices=('primary', 'secondary'), default='primary')
    args = parser.parse_args()
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bridge'))
    from test_read_states import (  # noqa: E402
        DOVE, EYES, OK_HAND, WORKING, ReadStateTests, SecondaryReadStateTests,
        crew_status_event, hook_event, inbox_shown_event, nudged, replied_event,
    )

    fixture = SecondaryReadStateTests if args.instance == 'secondary' else ReadStateTests
    case = fixture(methodName='test_full_lifecycle_replace_not_stack_and_idle_call_counts')
    case.setUp()
    messages = [
        (11, 'Deploy the staging pack after the check passes.'),
        (12, 'Refactor the hook stream parser and run the full suite.'),
        (13, 'Check the latest run log and ack once seen.'),
        (14, 'Update the example settings comment.'),
    ]
    progression_text = 'Run the migration check and tell me once the release is green.'
    progression_steps = []
    try:
        case._enable_offers(nudge_after=300, status='idle')
        case.restart(
            commands=case.bridge.config.get('commands', ()),
            reactions=dict(case.config['reactions'], nudge_after=300),
            typing=dict(seconds=600),
        )
        case.bridge.read_states.start()
        case._wait_until(lambda: case.bridge.read_states._stream_connected, timeout=3.0)

        # Drive one message (msg 10) through 🕊 landed -> 👀 read -> ✍ working -> 👌 done.
        u10 = case.update(uid=10)
        u10['message']['text'] = progression_text
        case.drain(u10)
        seq10 = case.row(10)['seq']
        assert case.row(10)['state'] == 'landed'
        progression_steps.append(('1. landed', DOVE, f'accepted by yard hook (seq {seq10}) · crew idle · typing off'))

        case.native.push_event(inbox_shown_event(30, reader=case.CREW, through=seq10, session=''))
        case._wait_until(lambda: (case.row(10) or {}).get('state') == 'read', timeout=2.0)
        progression_steps.append(('2. read', EYES, 'inbox_shown received · replaced 🕊 → 👀 · waiting for working turn'))

        case.native.push_event(crew_status_event(31, crew=case.CREW, status='working', was='idle'))
        case._wait_until(lambda: case.bridge.read_states.typing_targets() != {}, timeout=2.0)
        case.bridge._typing_tick()
        assert case.chat_actions, 'expected sendChatAction while working'
        case.mono[0] += 15.0
        case.poll()
        assert case.row(10)['state'] == 'working'
        progression_steps.append(('3. working', WORKING, 'crew_status=working past 15s · replaced 👀 → ✍ · typing… active'))

        case.native.push_event(replied_event(32, seq10, crew=case.CREW, source=case.SOURCE, journal=case.CREW + '-journal'))
        case._wait_until(lambda: (case.row(10) or {}).get('state') == 'done', timeout=2.0)
        case.bridge._typing_tick()
        progression_steps.append(('4. done', OK_HAND, 'replied event received · replaced ✍ → 👌 · typing stopped'))

        # Finish msg 10's turn and set up the 4-message overview chat.
        case.native.push_event(crew_status_event(33, crew=case.CREW, status='idle', was='working'))
        case._wait_until(lambda: (case.bridge.read_states._crew_status.get(case.CREW) or {}).get('status') != 'working',
                         timeout=2.0)
        case.bridge.read_states.stop()
        case.calls.clear()
        case.chat_actions.clear()

        for uid, text in messages:
            u = case.update(uid=uid)
            u['message']['text'] = text
            case.drain(u)
        # Msg 11 -> done (replied), Msg 12 -> working (✍), Msg 13 -> read (cursor >= seq13), Msg 14 -> landed (cursor < seq14).
        case.native.crew[case.CREW]['cursor'] = case.row(13)['seq']
        case.assertTrue(case.bridge.publish(case.publish(key='preview-reply'))['delivered'])
        case.native.journal_events[case.CREW + '-journal'].append(
            replied_event(501, case.row(11)['seq'], crew=case.CREW, source=case.SOURCE, journal=case.CREW + '-journal')
        )
        case.wall[0] += 300
        case.mono[0] += 5.0
        case.poll()
        case.bridge.read_states._stream_started = True
        case.bridge.read_states._stream_connected = True
        case.bridge.read_states._stream_broken = False
        case.bridge.read_states._handle_crew_status_event(dict(crew=case.CREW, status='working', was='idle'))
        case.mono[0] += 15.0
        case.poll()
        # Move msg 13 back to read while keeping msg 12 in working for a 4-state overview.
        with case.store.connect() as db:
            db.execute("UPDATE read_states SET state='read', turn=2 WHERE message_id=13")
        case.bridge.set_reaction(13, EYES, 'reaction:13:preview-read')
        states = {mid: case.row(mid)['state'] for mid, _ in messages}
        assert states == {11: 'done', 12: 'working', 13: 'read', 14: 'landed'}, states
        last_emoji = {}
        for url, body in case.calls:
            if url.endswith('/setMessageReaction'):
                last_emoji[body['message_id']] = body['reaction'][0]['emoji']
        offer_card = [s for s in case._sends() if 'Nudge crew member' in s.get('text', '')][-1]
        token = case.row(14)['offer_token']
        assert token is not None
        case.poll(case._tap(token, uid=40))
        tapped_edit = case._edits()[-1]
        ident = case.native.nudges['crew:' + case.CREW]['id']
        case.native.journal_events[case.CREW + '-journal'].append(
            dict(nudged(502, ident, crew=case.CREW), bead=case.CREW + '-journal'))
        case.poll()
        outcome_reply = case._sends()[-1]
        assert outcome_reply['text'] == '✓ Nudge typed', outcome_reply
        if args.instance == 'secondary':
            assert case.bridge.native_decide() is False
            assert case.bridge.run_asks.client is None
            assert {m for m, _ in case.native.calls} <= {
                'Head', 'GetUser', 'GetCrew', 'EventsSince', 'Events', 'ListSessions', 'NudgeSession'}
    finally:
        case.doCleanups()
        case.tearDown()

    progression_html = ''.join(
        f'<div class="step">'
        f'<div class="step-head"><span class="badge">{escape(stage)}</span><span class="rx">{escape(emoji)}</span></div>'
        f'<div class="step-msg">{escape(progression_text)}</div>'
        f'<div class="step-note">{escape(note)}</div>'
        f'</div>'
        for stage, emoji, note in progression_steps
    )
    labels = {
        11: ('done', 'yardr reply logged on journal · replaced ✍ → 👌'),
        12: ('working', 'crew_status=working ≥ 15s · typing… active · 👀 → ✍'),
        13: ('read', 'inbox shown / ack ≥ seq · crew idle · 🕊 → 👀'),
        14: ('landed', 'accepted by yard hook · cursor below seq · unread 5m'),
    }
    bubbles = []
    for mid, text in messages:
        state, note = labels[mid]
        emoji = last_emoji[mid]
        typing_pill = ' <span class="typing-pill">typing…</span>' if state == 'working' else ''
        bubbles.append(
            f'<div class="row">'
            f'<div class="bubble">{escape(text)}'
            f'<div class="meta"><span>{escape(state)} · {escape(note)}{typing_pill}</span>'
            f'<span class="rx">{escape(emoji)}</span></div></div></div>'
        )
    offer_buttons = ''.join(
        '<div class="buttons">' + ''.join('<button>' + escape(btn['text']) + '</button>' for btn in row) + '</div>'
        for row in offer_card.get('reply_markup', {}).get('inline_keyboard', [])
    )
    offer_html = (
        f'<div class="row left"><div class="bot">'
        f'<blockquote>{escape(messages[3][1])}</blockquote>'
        f'<div class="card-body">{offer_card["text"]}</div>'
        f'{offer_buttons}'
        f'<div class="meta"><span>silent reply to msg 14 · crew running · idle</span></div>'
        f'</div></div>'
    )
    tapped_html = (
        f'<div class="row left"><div class="bot">'
        f'<blockquote>{escape(messages[3][1])}</blockquote>'
        f'<div class="card-body">{tapped_edit["text"]}</div>'
        f'<div class="meta"><span>verified operator tap · 1 NudgeSession write</span></div>'
        f'</div></div>'
        f'<div class="row left"><div class="bot reply">'
        f'<blockquote>Nudge crew member {escape(case.CREW)}?</blockquote>'
        f'<div class="card-body">{escape(outcome_reply["text"])}</div>'
        f'<div class="meta"><span>daemon session_nudged outcome · message stays {DOVE} until inbox ack</span></div>'
        f'</div></div>'
    )
    subtitle = 'Synthetic chat · fake core · bridge ledger, setMessageReaction, sendChatAction &amp; NudgeSession · fake Telegram'
    if args.instance == 'secondary':
        subtitle += ' · secondary tgsecretary · crew secretary · native cards not owned here'
    print('''<!doctype html><meta charset="utf-8"><title>Synthetic read states, working typing &amp; nudge offer</title>
<style>
*{box-sizing:border-box}
body{margin:0;padding:18px;background:#e9f0f5;color:#183044;font:14px/1.45 system-ui,sans-serif}
h1{font-size:19px;margin:0 0 4px}
h2{font-size:15px;margin:18px 0 10px;color:#183044}
.sub{margin:0 0 14px;color:#496175;font-size:12px}
.timeline{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-bottom:16px}
.step{background:#effdde;border:1px solid #cce6b8;border-radius:12px;padding:10px 11px;box-shadow:0 1px 2px rgba(24,48,68,.06)}
.step-head{display:flex;align-items:center;justify-content:space-between;margin-bottom:6px}
.badge{font-size:11px;font-weight:700;color:#2e5a27;text-transform:uppercase;letter-spacing:.03em}
.step-msg{font-size:12px;color:#183044;margin-bottom:6px}
.step-note{font-size:11px;color:#4d6b47}
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
.typing-pill{display:inline-block;margin-left:6px;padding:1px 7px;border-radius:999px;background:#dbeeff;color:#1d598a;font-weight:600}
.rx{display:inline-flex;align-items:center;padding:2px 8px;border-radius:999px;background:#fff;border:1px solid #c6ddb8;font-size:16px;line-height:1.2}
.legend{margin-top:16px;padding:10px 12px;background:#fff;border:1px solid #cfdee8;border-radius:10px;font-size:12px;color:#395166}
</style>
<h1>Operator message read states, working turn &amp; unread nudge offer</h1>
<p class="sub">''' + subtitle + '''</p>
<h2>Single message lifecycle on the event stream</h2>
<div class="timeline">''' + progression_html + '''</div>
<h2>Chat overview &amp; unread nudge offer</h2>
<div class="chat">''' + ''.join(bubbles) + offer_html + '''</div>
<h2>After verified Nudge tap &amp; daemon outcome</h2>
<div class="chat">''' + tapped_html + f'''</div>
<div class="legend"><b>States:</b> {DOVE} landed (hook accepted) · {EYES} read (shown or acked) · {WORKING} working (crew_status working ≥ 15s; returns to {EYES} on idle) · {OK_HAND} done (reply or react done)</div>''')


if __name__ == '__main__':
    main()
