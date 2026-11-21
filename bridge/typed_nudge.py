"""Nudge an agent with the operator's own words.

`/nudge <target> [words]` in the operator's private chat, where the target
is a session id, a crew member's name or a bead with exactly one live
session, opens a confirm card drawn from canonical state: the target and
its current status (the GetCrew read or the ListSessions list over the
socket), the exact line shown whole, and Nudge and Cancel buttons. A reply
with words to a nudge card (the yard's own, from `nudge_cards.py`) does the
same, with that card's target.

A verified tap (`verify_operator`, sender, chat, topic, message binding,
claimed once) calls core `NudgeSession` as the operator, one write. The
outcome is relayed as in the card child: the daemon's `session_nudged`
matched on the request id, told once as a reply on the card, unknown after
ten minutes. No words: the core default line. Text is passed as given, never
interpolated into a shell. A chat message alone never nudges: only the tap
on the confirm card does.

A core without the method answers no such method, classified like
`RPCUnsupported`: `/nudge` then answers that this yard's core has no
session nudge, the command stays out of the menu (it is typed-only on every
core), and nothing is written. Grants: none new.
"""
import json
import re
import secrets
import time
import unicodedata
from html import escape

import run_asks as n
import nudge_cards as nc
import yard_commands as y

TABLE = 'typed_nudges'
CALLBACK = re.compile(r'nudge:([A-Za-z0-9_-]{16}):(nudge|cancel)')
TARGET_MAX = 128
WORDS_MAX = 1000
FOLLOW_SECONDS = nc.FOLLOW_SECONDS
TRANSIENT = (n.RPCUnavailable, n.RPCAmbiguous)
USAGE = 'Usage: /nudge <session|crew member|bead> [words]. A reply with words to a nudge card does the same.'
# The line a nudge with no words types, as the core defaults it
# (store.DefaultNudgeLine in the yardr source): shown whole on the card.
DEFAULT_LINE = 'yardr: the operator asks you to go on where you stopped.'
NO_SUPPORT = "This yard's core has no session nudge; nudge from a terminal instead. Nothing written."
UNAVAILABLE = "The yard is unavailable; nothing nudged."
UNCERTAIN = "Nudge uncertain; reconcile in the yard. Nothing retried."
STANDING = 'Another nudge stands already; nothing nudged.'
REFUSED = 'The yard refused this nudge; nothing nudged.'
CANCELLED = 'Cancelled; nothing nudged.'
REQUESTED = 'Nudge requested; waiting for the daemon.'
ALREADY = 'That tap is already recorded; nothing retried.'
DONE_STATES = ('done', 'cancelled', 'refused', 'unknown')
SILENT = ('? Nudge outcome unknown: the yard logged none within 10 minutes of the tap; '
          "look at the agent's pane. Nothing is retried.")


def _controls(text):
    return any(unicodedata.category(char) in ('Cc', 'Zl', 'Zp') for char in text)


def split_args(args):
    """(target, words) of the command's raw argument string. Words keep
    their punctuation and non-ASCII; only leading whitespace goes. NativeError
    with a fixed reason otherwise, before any call."""
    text = (args or '').lstrip()
    if not text:
        raise n.NativeError(USAGE)
    target, _, words = text.partition(' ')
    if not (1 <= len(target) <= TARGET_MAX) or _controls(target):
        raise n.NativeError(USAGE)
    return target, words.lstrip()


def check_words(words):
    """Words as the nudge types them: '' for the core default, else one line
    of at most WORDS_MAX characters with no control characters."""
    if len(words) > WORDS_MAX:
        raise n.NativeError(f'Words are {len(words)} characters; {WORDS_MAX} at most. Nothing nudged.')
    if _controls(words):
        raise n.NativeError('Words are one line with no control characters. Nothing nudged.')
    return words


def read_crew(client, target, user=''):
    """(status line, journal) of the crew member, or None when no crew member
    reads under the target. With user, the conversation's own session reads
    where the core names conversations; where it names none, the card says
    status unknown rather than show the line's. NativeError otherwise."""
    try:
        member = client.call('GetCrew', {'name': target})
    except n.RPCRefused as error:
        if error.code == 'not-found':
            return None
        raise n.NativeError('crew read unavailable') from None
    if not isinstance(member, dict) or member.get('name') != target:
        raise n.NativeError('invalid native crew')
    unknown = False
    if user:
        conversations = member.get('conversations')
        conversation = conversations.get(user) if isinstance(conversations, dict) else None
        if conversation is None:
            unknown = True
            state, status = '', ''
        else:
            if not isinstance(conversation, dict):
                raise n.NativeError('invalid native crew')
            state, status = conversation.get('state', ''), conversation.get('status', '')
            if not isinstance(state, str) or not isinstance(status, str):
                raise n.NativeError('invalid native crew')
    else:
        state, status = member.get('state', ''), member.get('status', '')
    journal = member.get('journal')
    kind, model = member.get('kind', ''), member.get('model', '')
    if (not isinstance(journal, str) or not journal
            or not isinstance(kind, str) or not isinstance(model, str)
            or not isinstance(state, str) or not isinstance(status, str)):
        raise n.NativeError('invalid native crew')
    runtime = ['status unknown'] if unknown else [y.clean(state, 32), y.clean(status, 32)]
    shown = ' · '.join(part for part in [y.clean(kind, 32), y.clean(model, 128), *runtime] if part)
    return ('crew member ' + target + (': ' + shown if shown else ''), journal)


def read_session(client, target):
    """(status line, journal) of the live session the target names: its id,
    or its bead when exactly one live session reads under it. None when
    neither reads. NativeError with a fixed reason otherwise."""
    try:
        sessions = client.call('ListSessions', [{}])
    except n.NativeError:
        raise n.NativeError('session list unavailable') from None
    if not isinstance(sessions, list):
        raise n.NativeError('invalid native session list')
    found = []
    for item in sessions:
        if (not isinstance(item, dict) or not isinstance(item.get('id'), str) or not item['id']
                or not isinstance(item.get('bead'), str) or not item['bead']
                or not isinstance(item.get('state'), str)
                or not isinstance(item.get('kind', ''), str) or not isinstance(item.get('model', ''), str)
                or (item.get('handle') is not None and not isinstance(item.get('handle'), dict))):
            raise n.NativeError('invalid native session list')
        if item['id'] == target or item['bead'] == target:
            found.append(item)
    if not found:
        return None
    if len(found) > 1:
        raise n.NativeError(f'Bead {target} has several live sessions; name one. Nothing nudged.')
    item = found[0]
    shown = ' · '.join(part for part in (y.clean(item.get('kind', ''), 32), y.clean(item.get('model', ''), 128),
                                         y.clean(item['state'], 32)) if part)
    return ('session ' + item['id'] + ' on ' + item['bead'] + (': ' + shown if shown else ''), item['bead'])


def _target_word(word):
    return isinstance(word, str) and 1 <= len(word) <= TARGET_MAX and not _controls(word)


def card_target(client, bead, number, operator):
    """(target, user) of the run ask (bead, number): the yard's own
    `session nudge` card names them in its argv, with `--for user` on a
    crew conversation's card. NativeError otherwise."""
    asked = client.call('Bead', {'bead': bead, 'viewer': operator})
    if not isinstance(asked, dict) or asked.get('id') != bead or not isinstance(asked.get('asks'), list):
        raise n.NativeError('invalid native Bead response')
    found = [ask for ask in asked['asks'] if isinstance(ask, dict) and ask.get('ask') == number]
    if len(found) != 1:
        raise n.NativeError('that card is answered or gone; ask again')
    ask = found[0]
    argv = (ask.get('run') or {}).get('argv') if isinstance(ask.get('run'), dict) else None
    if (ask.get('asker') != 'yard' or not isinstance(argv, list) or argv[:2] != ['session', 'nudge']
            or not all(isinstance(word, str) for word in argv)
            or not _target_word(argv[2] if len(argv) > 2 else '')):
        raise n.NativeError('that card names no nudge target')
    if len(argv) == 3:
        return argv[2], ''
    if len(argv) == 5 and argv[3] == '--for' and _target_word(argv[4]):
        return argv[2], argv[4]
    raise n.NativeError('that card names no nudge target')


def outcome_text(event):
    """The card's reply for one session_nudged event. The core sets working
    and queued on a line typed into a working agent; they read here in words,
    and the shared whitelist in nudge_cards stays as it is."""
    data = event.get('data') if isinstance(event, dict) else None
    if not isinstance(data, dict):
        raise n.NativeError('invalid native session_nudged event')
    working, queued = data.get('working', False), data.get('queued', False)
    for flag in (working, queued):
        if flag is not False and flag is not True:
            raise n.NativeError('invalid native session_nudged event')
    told = nc.outcome_text(dict(event, data={key: value for key, value in data.items()
                                             if key not in ('working', 'queued')}))
    extra = []
    if working is True:
        extra.append('the agent was working')
    if queued is True:
        extra.append('the line is queued')
    return told + (' (' + '; '.join(extra) + ')' if extra else '')


class TypedNudge:
    def __init__(self, bridge):
        self.bridge, self.store, self.config = bridge, bridge.store, bridge.config
        self.logged = None
        if self.store is not None:
            with self.store.connect() as db:
                db.execute(f'CREATE TABLE IF NOT EXISTS {TABLE} (token TEXT PRIMARY KEY, value TEXT NOT NULL)')

    @property
    def ra(self):
        return self.bridge.run_asks

    @property
    def client(self):
        return self.ra.client

    def log(self, words):
        # Said once while it stands, not on every poll.
        if words != self.logged:
            self.bridge._log(words)
        self.logged = words

    def get(self, token):
        with self.store.connect() as db:
            row = db.execute(f'SELECT value FROM {TABLE} WHERE token=?', (token,)).fetchone()
        return json.loads(row[0]) if row else None

    def save(self, row):
        with self.store.connect() as db:
            db.execute(f'INSERT INTO {TABLE} VALUES (?,?) ON CONFLICT(token) DO UPDATE SET value=excluded.value',
                       (row['token'], n.canonical(row)))

    def rows(self):
        with self.store.connect() as db:
            rows = [json.loads(row[0]) for row in db.execute(f'SELECT value FROM {TABLE}')]
        return [row for row in rows if row['binding'] == self.binding_digest()]

    def binding_digest(self):
        return n.digest(self.ra.binding())

    def verify(self):
        n.verify_operator(self.client, self.config)

    # --- the confirm card ------------------------------------------------

    def render(self, row):
        """(html, plain, keyboard) of the confirm card in the row's state."""
        line = row['words'] or DEFAULT_LINE
        if row['state'] == 'open':
            text = [f"<b>Nudge {escape(row['title'])}?</b>", escape(row['status']),
                    '<pre>' + escape(line) + '</pre>']
            keyboard = [[dict(text='Nudge', callback_data=f"nudge:{row['token']}:nudge"),
                         dict(text='Cancel', callback_data=f"nudge:{row['token']}:cancel")]]
        elif row['state'] == 'following':
            text = [f"<b>Nudge {escape(row['title'])}?</b>", escape(row['status']),
                    '<pre>' + escape(line) + '</pre>', escape(REQUESTED)]
            keyboard = []
        else:
            text = [f"<b>Nudge {escape(row['title'])}?</b>", escape(row['status']),
                    '<pre>' + escape(line) + '</pre>', escape(row.get('notice', ''))]
            keyboard = []
        body = '\n'.join(text)
        return body, row['title'] + '\n' + row['status'] + '\n' + line, keyboard

    def display(self, row):
        """Edit the card to the row's state; best effort, logged on failure:
        the outcome reply below is the durable signal, under its own key."""
        body, plain, keyboard = self.render(row)
        receipt = self.bridge._command_message(f"typed-nudge:{row['token']}:card",
                                               dict(type='edit', text=body, plain=plain, format='html',
                                                    message_id=row['message'], keyboard=keyboard),
                                               row['thread'])
        if receipt.get('delivered') is not True:
            self.bridge._log(f"typed nudge {row['token']}: card edit unconfirmed: "
                             f"{(receipt.get('metadata') or {}).get('reason')}")
        return receipt

    def open_confirm(self, uid, target, words, thread, user=''):
        """Send the confirm card for target and words; the row, saved with
        its card message. With user, the crew conversation's card. NativeError
        with a fixed reason otherwise."""
        conversation = self.bridge.decide_conversation(thread)
        if conversation is None:
            raise n.NativeError('Nudge is answered in the paired private chat.')
        self.verify()
        try:
            self.client.call('NudgeSession', {})
        except n.RPCUnsupported:
            raise n.NativeError(NO_SUPPORT) from None
        except n.RPCUnavailable:
            raise n.NativeError(UNAVAILABLE) from None
        except n.RPCRefused:
            pass  # the method reads; only its validation refused, writing nothing
        except n.NativeError:
            raise n.NativeError(UNCERTAIN) from None
        found = read_crew(self.client, target, user)
        kind = 'crew'
        if found is None:
            if user:
                raise n.NativeError(f"--for names a crew member's conversation; {target} is no crew member. Nothing nudged.")
            found = read_session(self.client, target)
            kind = 'session'
        if found is None:
            raise n.NativeError(f'No session, crew member or bead {target}; nothing nudged.')
        status, journal = found
        if kind == 'crew' and user:
            title = f'crew member {target} for {user}'
        else:
            title = ('crew member ' if kind == 'crew' else 'session ') + target
        row = dict(token=secrets.token_urlsafe(12), binding=self.binding_digest(),
                   conversation=conversation['conversation_id'], thread=thread,
                   title=title,
                   target=target, user=user, kind=kind, status=status, words=words, journal=journal,
                   request=None, state='open', message=None, approved_at=0, told=None, notice=None)
        self.save(row)
        body, plain, keyboard = self.render(row)
        receipt = self.bridge._command_message(f"typed-nudge:{row['token']}:send",
                                               dict(type='send', text=body, plain=plain, format='html',
                                                    keyboard=self.bridge._command_keyboard(keyboard)), thread)
        if receipt.get('delivered') is not True:
            reason = (receipt.get('metadata') or {}).get('reason') or 'delivery not confirmed'
            raise n.NativeError(f'Nudge card not sent: {reason}. Nothing nudged.')
        row['message'] = int(receipt['message_id'])
        self.save(row)
        return row

    def command(self, uid, payload, query_id, thread):
        if payload.get('kind') != 'command':
            self.bridge._answer_callback(uid, query_id)
            return
        try:
            target, words = split_args(payload.get('args'))
            self.open_confirm(uid, target, check_words(words), thread)
        except n.NativeError as error:
            self.bridge._command_message(f'typed-nudge:{uid}:notice',
                                         dict(type='send', text=str(error), plain=None), thread)
        except Exception:
            self.bridge._log('typed nudge command failed (update %s): internal error' % (uid,))
            self.bridge._command_message(f'typed-nudge:{uid}:notice',
                                         dict(type='send', text='Nudge unavailable; nothing nudged.',
                                              plain=None), thread)

    # --- the tap ----------------------------------------------------------

    def answer(self, uid, query_id, text, alert=False):
        return self.bridge._answer_callback(uid, query_id, text, alert, key=f'nudge:{uid}:answer')

    def callback(self, uid, query):
        match = CALLBACK.fullmatch(query.get('data') or '')
        row = self.get(match[1]) if match else None
        query_id = query.get('id')
        try:
            if row is None:
                self.answer(uid, query_id, 'That nudge button is out of date.', True)
                return
            sender, message = query.get('from', {}), query.get('message', {})
            chat = message.get('chat', {}) if isinstance(message, dict) else {}
            if (not isinstance(sender, dict) or sender.get('id') != self.config['user_id']
                    or sender.get('is_bot') is not False
                    or not isinstance(chat, dict) or chat.get('id') != self.config['chat_id']):
                self.answer(uid, query_id, 'Native nudge card not authorized.')
                return
            thread = self.bridge._topic_thread(message) if isinstance(message, dict) else None
            if (not isinstance(message, dict) or any(key in message for key in self.bridge.FORWARD_KEYS)
                    or message.get('from', {}).get('id') != self.config['bot_id']
                    or message.get('from', {}).get('is_bot') is not True
                    or message.get('message_id') != row['message']
                    or self.bridge.decide_conversation(thread) is None
                    or self.bridge.decide_conversation(thread)['conversation_id'] != row['conversation']):
                self.answer(uid, query_id, 'That nudge button is out of date.', True)
                return
            if row['binding'] != self.binding_digest():
                notice = 'That nudge card is from another yard or pairing; nothing nudged.'
                row.update(state='refused', notice=notice)
                self.save(row)
                self.display(row)
                self.answer(uid, query_id, notice, True)
                return
            self.verify()
            if not self.ra.claim_update(uid):
                self.answer(uid, query_id, ALREADY)
                return
            if match[2] == 'cancel':
                if row['state'] != 'open':
                    self.answer(uid, query_id, ALREADY)
                    return
                row.update(state='cancelled', notice=CANCELLED)
                self.save(row)
                self.display(row)
                self.answer(uid, query_id, CANCELLED)
                return
            if row['state'] != 'open':
                self.answer(uid, query_id, ALREADY)
                return
            row.update(state='following', approved_at=self.ra.now())
            self.save(row)
            try:
                result = self.client.call('NudgeSession', dict(
                    target=row['target'], user=row.get('user', ''), text=row['words'], clear=False,
                    actor=self.config['yardr_operator']))
            except n.RPCUnsupported:
                row.update(state='refused', notice=NO_SUPPORT)
                self.save(row)
                self.display(row)
                self.answer(uid, query_id, NO_SUPPORT, True)
                return
            except n.RPCRefused as error:
                if error.code == 'conflict':
                    notice = STANDING
                elif error.code == 'not-found':
                    notice = f"No such {row['kind']} now; nothing nudged."
                else:
                    notice = REFUSED
                row.update(state='refused', notice=notice)
                self.save(row)
                self.display(row)
                self.answer(uid, query_id, notice, True)
                return
            except TRANSIENT as error:
                row.update(state='unknown', notice=f'{UNCERTAIN} ({error})')
                self.save(row)
                self.display(row)
                self.answer(uid, query_id, UNCERTAIN, True)
                return
            request, journal = self.adopt(row, result)
            if request is None:
                self.answer(uid, query_id, UNCERTAIN, True)
                return
            row.update(request=request, journal=journal)
            self.save(row)
            self.display(row)
            self.answer(uid, query_id, REQUESTED)
        except n.NativeError as error:
            self.answer(uid, query_id, str(error), True)
        except Exception:
            self.bridge._log(f'typed nudge tap failed (update {uid}): internal error')
            self.answer(uid, query_id, UNCERTAIN, True)

    def adopt(self, row, result):
        """(request id, journal) of a NudgeSession result for the row; the
        row marked unknown otherwise. Nothing is retried either way."""
        if (not isinstance(result, dict) or not isinstance(result.get('id'), str) or not result['id']
                or not isinstance(result.get('text'), str) or result.get('state') != 'asked'):
            row.update(state='unknown', notice=UNCERTAIN + ' (invalid native NudgeSession response)')
            self.save(row)
            self.display(row)
            return None, None
        if row['kind'] == 'crew':
            if result.get('crew') != row['target'] or result.get('user', '') != row.get('user', ''):
                row.update(state='unknown', notice=UNCERTAIN + ' (native nudge is for another target)')
                self.save(row)
                self.display(row)
                return None, None
            return result['id'], row['journal']
        if result.get('session') != row['target'] and result.get('bead') != row['target']:
            row.update(state='unknown', notice=UNCERTAIN + ' (native nudge is for another target)')
            self.save(row)
            self.display(row)
            return None, None
        if not isinstance(result.get('bead'), str) or not result['bead'] or result['bead'] != row['journal']:
            row.update(state='unknown', notice=UNCERTAIN + ' (native nudge journal changed)')
            self.save(row)
            self.display(row)
            return None, None
        return result['id'], row['journal']

    # --- a reply with words to a nudge card ---------------------------------

    def reply(self, update_id, message):
        if not self.bridge.native_nudge():
            return False
        reply = message.get('reply_to_message')
        if not isinstance(reply, dict) or type(reply.get('message_id')) is not int:
            return False
        candidates = [row for row, _ in self.ra.bound_messages(reply['message_id'])
                      if row.get('kind') != 'decision' and self.bridge.nudge_cards.get(row['id']) is not None]
        if not candidates:
            return False
        try:
            if len(candidates) != 1:
                raise n.NativeError('Native nudge card message binding ambiguous')
            run = candidates[0]
            if not self.ra.authorized_message(run, message, reply=True):
                raise n.NativeError('Native nudge card not authorized')
            if any(key in message for key in self.bridge.FORWARD_KEYS):
                raise n.NativeError('Native nudge card not authorized')
            if self.bridge._extract_attachment(message) is not None:
                raise n.NativeError('Nudge card replies must be text only; attachments are not supported')
            if not isinstance(message.get('text'), str) or not message['text'].strip():
                raise n.NativeError('Reply with the words to nudge with.')
            # The length check runs before any native RPC: the supplied reply
            # is measured before stripping, so padding cannot smuggle an
            # over-long line past it; an over-long reply is refused with an
            # empty native call list.
            if len(message['text']) > WORDS_MAX:
                raise n.NativeError(f'Words are {len(message["text"])} characters; {WORDS_MAX} at most. Nothing nudged.')
            words = check_words(message['text'].lstrip())
            self.verify()
            if not self.ra.claim_update(update_id):
                return True
            target, user = card_target(self.client, run['bead'], run['ask'], self.config['yardr_operator'])
            self.open_confirm(update_id, target, words, self.ra.thread(run), user)
        except n.NativeError as error:
            self.bridge._command_message(f'typed-nudge:{update_id}:notice',
                                         dict(type='send', text=str(error), plain=None),
                                         self.bridge._topic_thread(message))
        return True

    # --- following the request to its outcome -------------------------------

    def outcome(self, journal, ident):
        """The reply for the session_nudged of request ident on journal, or
        None while the yard has logged none. Bound like the card child's:
        back to the request's own session_nudge_requested, or the start."""
        ra, limit = self.ra, 64
        while True:
            events = ra._validate_events(ra.client.call('Events', [journal, limit]), journal, limit, 'nudge outcome')
            found = [e for e in events if e['kind'] == 'session_nudged' and e['data'].get('id') == ident]
            if len(found) > 1:
                raise n.NativeError('invalid native duplicate session_nudged')
            if found:
                return outcome_text(found[0])
            asked = any(e['kind'] == 'session_nudge_requested' and e['data'].get('id') == ident for e in events)
            if asked or len(events) < limit:
                return None
            limit *= 2

    def follow(self, row):
        """Advance one requested row: to told once its outcome reads, and
        its reply sent once under its own key. A trouble, or None."""
        ra = self.ra
        if row['request'] is None:
            if ra.now() <= row['approved_at'] + FOLLOW_SECONDS:
                return None
            row.update(state='unknown', notice='? Nudge was not recorded; ask again.')
            self.save(row)
            self.display(row)
            return None
        try:
            told = self.outcome(row['journal'], row['request'])
        except n.NativeError as error:
            if isinstance(error, TRANSIENT):
                # Inconclusive: read again next poll, never said as unknown.
                return f"typed nudge {row['token']}: outcome unread: {error}"
            told = f"? Nudge outcome unreadable ({error}); look at the agent's pane."
        if told is None:
            if ra.now() <= row['approved_at'] + FOLLOW_SECONDS:
                return None
            told = SILENT
        # The chosen reply is kept until its delivery is final, so a resend is the same request.
        row.update(state='telling', told=told)
        self.save(row)
        receipts = ra.decide_notice(f"typed-nudge:{row['request']}:outcome", row['told'], None,
                                    (row['message'], row['conversation']))
        if any(self.bridge.resendable(receipt) for receipt in receipts):
            return f"typed nudge {row['token']}: outcome not sent yet"
        row['state'] = 'done'
        self.save(row)
        return None

    def refresh(self):
        if not self.bridge.native_nudge():
            return
        ra = self.ra
        if ra.client is None or not self.config.get('yardr_operator'):
            return
        with ra.lock():
            rows = [row for row in self.rows() if row['state'] in ('following', 'telling')]
        trouble = []
        with ra.lock():
            for row in rows:
                if row['state'] == 'telling':
                    receipts = ra.decide_notice(f"typed-nudge:{row['request']}:outcome", row['told'], None,
                                                (row['message'], row['conversation']))
                    if any(self.bridge.resendable(receipt) for receipt in receipts):
                        trouble.append(f"typed nudge {row['token']}: outcome not sent yet")
                        continue
                    row['state'] = 'done'
                    self.save(row)
                    continue
                trouble.append(self.follow(row))
        trouble = [words for words in trouble if words]
        if trouble:
            self.log(trouble[0])
        else:
            self.logged = None
