"""Native run proposals: socket authority, complete cards and durable interactions."""
import contextlib
import fcntl
import hashlib
from html import escape
import http.client
import json
import os
from pathlib import Path
import re
import socket
import stat
import time


CAP = 3500
PENDING = 'approved, to be carried out'
UNCERTAIN = 'the yard did not answer; read the card again'
FALLBACK = '; use Footplate or the yard cards'
WEIGHTS = {'credential', 'as-user', 'outside', 'code', 'trust', 'reads'}


class NativeError(Exception):
    """A fixed refusal reason, without input or response text."""


class RPCUnavailable(NativeError):
    pass


class RPCAmbiguous(NativeError):
    pass


class RPCRefused(NativeError):
    pass


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def units(value):
    return len(value.encode('utf-16-le')) // 2


def without_weight_mark(value):
    lines = []
    for line in value.split('\n'):
        rest = line.lstrip()
        leading = line[:len(line) - len(rest)]
        while rest.startswith('WEIGHTY · '):
            rest = rest[len('WEIGHTY · '):]
            if rest.lstrip().startswith('WEIGHTY · '):
                rest = rest.lstrip()
        lines.append(leading + rest)
    return '\n'.join(lines)


def validate_home(home):
    path = Path(home).expanduser().resolve()
    try:
        info = path.stat()
    except OSError:
        raise NativeError('yardr.home must be an existing private directory') from None
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
        raise NativeError('yardr.home must be an owner-held directory without group/world writes')
    return path


class YardSocketClient:
    def __init__(self, home, source, timeout=10):
        self.home, self.source, self.timeout = Path(home), source, timeout

    def call(self, method, arguments):
        path = self.home / 'yard.sock'
        try:
            info = path.lstat()
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
                raise RPCUnavailable('native socket must be an owner-held Unix socket')
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(self.timeout)
            try:
                sock.connect(str(path))
            except OSError:
                sock.close()
                raise
        except OSError:
            raise RPCUnavailable(f'native {method} unavailable before send') from None
        with sock:
            data = canonical(arguments).encode()
            request = (f'POST /call/{method} HTTP/1.1\r\nHost: localhost\r\n'
                       f'X-Yardr-Connection: {self.source}\r\nContent-Type: application/json\r\n'
                       f'Content-Length: {len(data)}\r\nConnection: close\r\n\r\n').encode() + data
            try:
                sock.sendall(request)
                response = http.client.HTTPResponse(sock)
                try:
                    response.begin()
                    raw = response.read(8 * 1024 * 1024 + 1)
                finally:
                    response.close()
                if response.status != 200 or len(raw) > 8 * 1024 * 1024:
                    raise ValueError()
                value = json.loads(raw, object_pairs_hook=strict_object)
                if not isinstance(value, dict) or set(value) - {'result', 'error', 'more'}:
                    raise ValueError()
                if 'error' in value:
                    error = value['error']
                    if (not isinstance(error, dict) or set(error) != {'code', 'message'}
                            or not all(isinstance(v, str) for v in error.values()) or 'result' in value):
                        raise ValueError()
                    raise RPCRefused(f'native {method} refused ({error["code"] if error["code"] in {"not-found", "taken", "at-limit", "conflict", "ambiguous", "replacing", "error"} else "unknown"})')
                return value.get('result')  # successful void calls serialize as {}
            except (OSError, ValueError, http.client.HTTPException, UnicodeError):
                raise RPCAmbiguous(f'native {method} response uncertain') from None


def strict_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError()
        result[key] = value
    return result


def verify_operator(client, config):
    user = client.call('GetUser', {'name': config['yardr_operator']})
    identities = user.get('identities') if isinstance(user, dict) else None
    identity = identities.get(config.get('yardr_source', 'tgbridge')) if isinstance(identities, dict) else None
    if (not isinstance(user, dict) or user.get('name') != config['yardr_operator']
            or user.get('role') != 'operator' or not isinstance(identity, dict)
            or identity.get('id') != str(config['user_id'])):
        raise NativeError('operator_not_authorized')


def text(value, field, controls=False):
    if not isinstance(value, str):
        raise NativeError(f'invalid native {field}')
    try:
        units(value)
    except UnicodeError:
        raise NativeError(f'invalid native {field}') from None
    if not controls and any(ord(c) < 32 and c not in '\n\t' for c in value):
        raise NativeError(f'invalid native {field}')
    return value


def lines(value, field):
    if not isinstance(value, list):
        raise NativeError(f'invalid native {field}')
    return [text(item, field) for item in value]


def validate(ask, operator):
    known = {'ask', 'to', 'asker', 'since', 'question', 'context', 'options', 'recommend', 'why', 'about',
             'state', 'option', 'words', 'by', 'return_at', 'picks_before', 'secret', 'concealed', 'given',
             'run', 'ran', 'setup', 'identity', 'depot_push', 'weight'}
    if set(ask) - known:
        raise NativeError('run_ask_unknown_payload' + FALLBACK)
    if ask.get('options') not in (None, []) or ask.get('about') or ask.get('recommend') or ask.get('why'):
        raise NativeError('ask_not_run_proposal' + FALLBACK)
    if ask.get('to') != operator:
        raise NativeError('run_ask_wrong_recipient')
    if (not isinstance(ask.get('run'), dict)
            or any(ask.get(key) is not None for key in ('secret', 'setup', 'identity', 'depot_push'))):
        raise NativeError('ask_not_run_proposal' + FALLBACK)
    run = ask['run']
    if set(run) - {'argv', 'command', 'description', 'does', 'back', 'back_by', 'carried', 'pack', 'ask'}:
        raise NativeError('run_ask_unknown_payload' + FALLBACK)
    for key in ('ask', 'carried'):
        if key in run and not isinstance(run[key], list):
            raise NativeError('invalid native run.' + key)
    if run.get('ask'):
        raise NativeError('run_ask_requires_input_use_terminal' + FALLBACK)
    argv = lines(run.get('argv'), 'run.argv')
    if not argv or not all(argv):
        raise NativeError('invalid native run.argv')
    if run.get('carried') or run.get('pack') is not None or argv[:2] in (['source', 'add'], ['pack', 'apply'], ['crew', 'add']):
        raise NativeError('run_ask_carries_files_use_terminal' + FALLBACK)
    for key in ('question', 'asker', 'to', 'state'):
        text(ask.get(key), key)
    lines(ask.get('context'), 'context')
    for key in ('command', 'back'):
        text(run.get(key), 'run.' + key)
    for key in ('description', 'back_by'):
        text(run.get(key, ''), 'run.' + key)
    lines(run.get('does'), 'run.does')
    weight = ask.get('weight', [])
    if not isinstance(weight, list):
        raise NativeError('invalid native weight')
    for item in weight:
        if (not isinstance(item, dict) or set(item) != {'class', 'line'}
                or not isinstance(item['class'], str) or item['class'] not in WEIGHTS or '\n' in text(item['line'], 'weight.line')):
            raise NativeError('invalid native weight')


def render(config, bead, ask):
    run = ask['run']
    parts = []

    def add(value, tag=None, trusted=False):
        value = value if trusted else without_weight_mark(value)
        escaped = escape(value)
        parts.append((value, f'<{tag}>{escaped}</{tag}>' if tag else escaped))

    add(f"{config['yard']} · {bead['id']} · ask #{ask['ask']}", 'b')
    add(f"approving runs in {config['yard']} as {config['yardr_operator']}")
    for weight in ask.get('weight', []):
        add(f"WEIGHTY · {weight['class']}: {weight['line']}", 'b', trusted=True)
    add(text(bead.get('title'), 'bead.title'), 'b')
    add('Proposed by ' + ask['asker'])
    add(ask['question'])
    if run.get('description'):
        add(run['description'])
    # Canonical command text remains exact; preformatted text cannot be a warning line.
    add(run['command'], 'pre', trusted=True)
    add('Does:')
    for line in run['does']:
        add(line, 'pre')
    add('Takes it back: ' + run['back'])
    if run.get('back_by'):
        add(run['back_by'] + ' can take it back')
    for line in ask['context']:
        add(line)
    plain = '\n'.join(p for p, _ in parts)
    if units(plain) > CAP:
        raise NativeError('run_ask_payload_too_large_for_card' + FALLBACK)
    return plain, '\n'.join(h for _, h in parts)


def chunks(value, limit=3500):
    chunk, size = [], 0
    for char in value:
        width = units(char)
        if size + width > limit:
            yield ''.join(chunk)
            chunk, size = [], 0
        chunk.append(char)
        size += width
    if chunk:
        yield ''.join(chunk)


class RunAsks:
    def __init__(self, bridge, client=None, now=time.time):
        self.bridge, self.store, self.config, self.now = bridge, bridge.store, bridge.config, now
        self.client = client
        if self.config.get('yardr_home') and client is None:
            self.client = YardSocketClient(self.config['yardr_home'], self.config.get('yardr_source', 'tgbridge'))
        if self.store is not None:
            with self.store.connect() as db:
                db.execute('CREATE TABLE IF NOT EXISTS run_asks (id TEXT PRIMARY KEY, data TEXT NOT NULL)')
                db.execute('CREATE TABLE IF NOT EXISTS run_ask_updates (binding TEXT NOT NULL, id INTEGER NOT NULL, PRIMARY KEY(binding,id))')

    @contextlib.contextmanager
    def lock(self):
        fd = os.open(self.store.path + '.run-asks.lock', os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def binding(self):
        if self.client is None or not self.config.get('yardr_operator'):
            raise NativeError('native_run_asks_not_configured')
        return {key: str(self.config.get(key, 'tgbridge' if key == 'yardr_source' else '')) for key in
                ('yard', 'yardr_home', 'yardr_operator', 'yardr_source', 'bot_id', 'user_id', 'chat_id')}

    def rows(self):
        with self.store.connect() as db:
            return [json.loads(row[0]) for row in db.execute('SELECT data FROM run_asks')]

    def get(self, ident):
        with self.store.connect() as db:
            row = db.execute('SELECT data FROM run_asks WHERE id=?', (ident,)).fetchone()
        return json.loads(row[0]) if row else None

    def save(self, row):
        with self.store.connect() as db:
            db.execute('INSERT OR REPLACE INTO run_asks VALUES (?,?)', (row['id'], canonical(row)))

    def fetch(self, bead_id, number):
        bead = self.client.call('Bead', {'bead': bead_id, 'viewer': self.config['yardr_operator']})
        if not isinstance(bead, dict) or bead.get('id') != bead_id or not isinstance(bead.get('asks'), list):
            raise NativeError('invalid native Bead response')
        if any(not isinstance(a, dict) or type(a.get('ask')) is not int or a['ask'] <= 0 for a in bead['asks']):
            raise NativeError('invalid native Bead asks')
        found = [a for a in bead['asks'] if a['ask'] == number]
        if len(found) != 1:
            raise NativeError('run_ask_not_found')
        ask = found[0]
        validate(ask, self.config['yardr_operator'])
        if ask['state'] not in {'open', 'put aside', 'answered', 'declined', 'taken back'}:
            raise NativeError('invalid native ask state')
        return bead, ask

    def proposal(self, bead, ask, conversation):
        return digest(dict(binding=self.binding(), bead=bead['id'], title=bead['title'], ask=ask['ask'],
                           to=ask['to'], asker=ask['asker'], question=ask['question'], context=ask['context'],
                           run=ask['run'], weight=ask.get('weight', []), conversation=conversation))

    def ensure(self, row):
        if row['state'] == 'stale':
            raise NativeError('run_ask_stale_proposal')
        if row['binding'] != self.binding():
            raise NativeError('run_ask_wrong_yard_or_pairing')
        verify_operator(self.client, self.config)
        bead, ask = self.fetch(row['bead'], row['ask'])
        if self.proposal(bead, ask, row['conversation']) != row['proposal']:
            row.update(pending=None, state='stale')
            self.save(row)
            self.display(row, 'Proposal changed; use Footplate or the yard cards', [])
            raise NativeError('run_ask_stale_proposal')
        return ask

    def keyboard(self, row):
        actions = [('Cancel', 'cancel')] if row.get('pending') else [(f"✓ Approve in {self.config['yard']}", 'approve')]
        actions.append(('✕ Decline', 'decline'))
        return [[dict(text=label, callback_data=f"runask:{row['id']}:{action}") for label, action in actions]]

    def create(self, bead_id, number, key, conversation, silent=False):
        binding = self.binding()
        request = canonical(dict(bead=bead_id, ask=number, conversation=conversation, silent=silent, binding=binding))
        with self.lock():
            verify_operator(self.client, self.config)
            bead, ask = self.fetch(bead_id, number)
            with self.store.connect() as db:
                saved = db.execute('SELECT id,request FROM run_ask_keys WHERE key=?', (key,)).fetchone()
            if saved and saved[1] != request:
                raise NativeError('idempotency_key_conflict')
            proposal = self.proposal(bead, ask, conversation)
            ident = digest(dict(binding=binding, bead=bead_id, ask=number))[:24]
            row = self.get(ident)
            if row:
                if row['state'] == 'stale':
                    raise NativeError('run_ask_stale_proposal')
                if row['proposal'] != proposal:
                    raise NativeError('run_ask_stale_proposal')
                if row['message_id'] is None and ask['state'] != 'open':
                    raise NativeError('run_ask_not_open')
                if row['silent'] != silent:
                    raise NativeError('idempotency_key_conflict')
            else:
                if ask['state'] != 'open':
                    raise NativeError('run_ask_not_open')
                plain, card = render(self.config, bead, ask)
                row = dict(id=ident, binding=binding, bead=bead_id, ask=number, conversation=conversation,
                           proposal=proposal, plain=plain, html=card, weighty=bool(ask.get('weight')),
                           state='open', pending=None, message_id=None, silent=silent, revision=0)
            with self.store.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                if (db.execute('SELECT 1 FROM deliveries WHERE key=?', (key,)).fetchone()
                        or db.execute('SELECT 1 FROM asks WHERE ask_id=?', (self.bridge._ask_id_for_key(key),)).fetchone()):
                    raise NativeError('idempotency_key_conflict')
                db.execute('INSERT OR IGNORE INTO run_ask_keys VALUES (?,?,?)', (key, ident, request))
            self.save(row)
            receipt = self.send_card(row)
            if row['message_id'] is not None:
                if ask['state'] != 'open':
                    self.settle(row, ask)
            return receipt

    def thread(self, row):
        return int(row['conversation'].rpartition(':topic:')[2]) if ':topic:' in row['conversation'] else None

    def send_card(self, row):
        b = self.bridge
        extra = dict(reply_markup={'inline_keyboard': self.keyboard(dict(row, pending=None))}, disable_notification=row['silent'])
        if self.thread(row) is not None:
            extra['message_thread_id'] = self.thread(row)
        receipt = b._reserve_and_deliver(f"runask:{row['id']}:send", dict(text=row['html'], **extra),
                    lambda pending: b._deliver_text(row['html'], None, pending, row['plain'], extra=extra))
        if receipt.get('delivered'):
            row['message_id'] = int(receipt['message_id'])
            self.save(row)
        return dict(receipt, conversation=dict(b.conversation, conversation_id=row['conversation']))

    def display(self, row, footer='', keyboard=None):
        if row['message_id'] is None:
            return self.bridge.receipt(reason='ask_message_unknown')
        keyboard = self.keyboard(row) if keyboard is None else keyboard
        # Approval-critical data stays on the card; long canonical outcomes are separate whole parts.
        separate = units(row['plain'] + '\n' + footer) > 4096
        suffix = 'Canonical outcome follows below.' if separate else footer
        desired = dict(footer=footer, keyboard=keyboard)
        if row.get('display') != desired:
            row['revision'] += 1
            row['display'] = desired
            self.save(row)
        key = f"runask:{row['id']}:revision:{row['revision']}"
        extra = dict(message_id=row['message_id'], reply_markup={'inline_keyboard': keyboard})
        b = self.bridge
        receipt = b._reserve_and_deliver(key + ':edit', dict(text=row['html'] + '\n' + escape(suffix), **extra),
                    lambda pending: b._deliver_text(row['html'] + '\n' + escape(suffix), None, pending,
                        row['plain'] + '\n' + suffix, method='editMessageText', extra=extra, not_modified=True), retry_undelivered=True)
        if separate:
            for index, part in enumerate(chunks(footer)):
                sent = b._command_message(key + f':outcome:{index}', dict(type='send', text=part, plain=None), self.thread(row))
                if not sent.get('delivered'):
                    receipt = sent
        return receipt

    def settle(self, row, ask):
        state = ask['state']
        if state in ('open', 'put aside'):
            return False
        if state == 'answered':
            ran = ask.get('ran')
            if (not isinstance(ran, dict) or set(ran) - {'argv', 'result', 'pending_reason', 'failed'}
                    or lines(ran.get('argv'), 'ran.argv') != ask['run']['argv']
                    or type(ran.get('failed', False)) is not bool):
                raise NativeError('invalid native ran response')
            result = text(ran.get('result'), 'ran.result', controls=True)
            pending_reason = text(ran.get('pending_reason', ''), 'ran.pending_reason', controls=True)
            state = 'failed' if ran.get('failed') else 'carrying' if result == PENDING else 'approved'
            label = {'failed': '✕ approved, failed', 'carrying': '⏳ approved, to be carried out',
                     'approved': '✓ approval recorded' if not result else '✓ approved, ran'}[state]
            footer = label + ': ' + ask['run']['command'] + '\n' + result
            if pending_reason:
                footer += '\n' + pending_reason
        else:
            words = text(ask.get('words', ''), 'words')
            footer = '✕ ' + ('Taken back by ' + text(ask.get('by'), 'by') if state == 'taken back' else 'Declined') + ': ' + words
        row.update(state=state, pending=None, uncertain=False)
        self.save(row)
        receipt = self.display(row, footer, [])
        # Revisions include outcome edits and pending_reason changes, not just the first answer.
        relay_key = f"runask-{row['id']}-{row['revision']}"
        if self.bridge.hook_secret and row.get('relayed') != relay_key:
            try:
                self.bridge._relay(row['message_id'], relay_key,
                    f"[native run-ask | bead: {row['bead']} | ask: {row['ask']}]\n{footer}",
                    thread_id=self.thread(row), kind='answer')
            except Exception:  # the native decision is already committed; poll retries this delivery
                self.bridge._log('native decision relay unconfirmed')
            else:
                row['relayed'] = relay_key
                self.save(row)
        return receipt

    def claim_update(self, update_id):
        with self.store.connect() as db:
            return db.execute('INSERT OR IGNORE INTO run_ask_updates VALUES (?,?)',
                              (digest(self.binding()), update_id)).rowcount == 1

    def authorized_message(self, row, message, reply=False):
        if not isinstance(message, dict) or any(key in message for key in self.bridge.FORWARD_KEYS):
            return False
        chat, sender = message.get('chat', {}), message.get('from', {})
        if not isinstance(chat, dict) or not isinstance(sender, dict):
            return False
        expected_id = self.config['user_id'] if reply else self.config['bot_id']
        if (chat.get('id') != self.config['chat_id'] or type(chat.get('id')) is not int or chat.get('type') != 'private'
                or sender.get('id') != expected_id or type(sender.get('id')) is not int
                or sender.get('is_bot') is not (False if reply else True)):
            return False
        thread = self.bridge._topic_thread(message)
        if thread != self.thread(row) or (message.get('message_thread_id') is not None and thread is None):
            return False
        return reply or (type(message.get('message_id')) is int and message['message_id'] == row['message_id'])

    def callback(self, update_id, query):
        match = re.fullmatch(r'runask:([0-9a-f]{24}):(approve|decline|cancel)', query.get('data', ''))
        notice = 'Native card not authorized'
        try:
            with self.lock():
                row = self.get(match[1]) if match else None
                sender = query.get('from', {})
                if (not row or sender.get('id') != self.config['user_id'] or sender.get('is_bot') is not False
                        or not self.authorized_message(row, query.get('message'))):
                    return
                ask = self.ensure(row)
                if not self.claim_update(update_id):
                    notice = 'Already handled'
                elif ask['state'] != 'open':
                    self.settle(row, ask)
                    notice = 'Ask is ' + ask['state']
                elif match[2] == 'cancel':
                    row['pending'] = None
                    self.save(row)
                    self.display(row)
                    notice = 'Cancelled'
                elif match[2] == 'approve' and row['weighty']:
                    row['pending'] = dict(expires=self.now() + 300, update=update_id)
                    self.save(row)
                    self.display(row, f"type the ask's number, {row['ask']}, to approve this weighty card. Reply directly to this card with {row['ask']}.")
                    notice = f"Reply to this card with {row['ask']}"
                else:
                    notice = self.decide(row, match[2], False)
        except NativeError as error:
            notice = str(error)
        finally:
            self.bridge._answer_callback(update_id, query['id'], notice[:180], key=f'runask:{update_id}:answer')

    def reply(self, update_id, message):
        if not isinstance(message.get('text'), str):
            return False
        reply = message.get('reply_to_message')
        if not isinstance(reply, dict) or type(reply.get('message_id')) is not int or reply['message_id'] <= 0:
            return False
        if 'from' in reply and (not isinstance(reply['from'], dict) or reply['from'].get('id') != self.config['bot_id'] or reply['from'].get('is_bot') is not True):
            return False
        if 'chat' in reply and (not isinstance(reply['chat'], dict) or reply['chat'].get('id') != self.config['chat_id']):
            return False
        with self.lock():
            with self.store.connect() as db:
                seen = db.execute('SELECT 1 FROM run_ask_updates WHERE binding=? AND id=?',
                                  (digest(self.binding()), update_id)).fetchone() if self.client else None
            if seen and any(row['message_id'] == reply.get('message_id')
                            and self.authorized_message(row, message, reply=True) for row in self.rows()):
                return True
            candidates = [row for row in self.rows() if row['message_id'] == reply.get('message_id')
                          and row.get('pending') and self.authorized_message(row, message, reply=True)]
            if len(candidates) != 1:
                return False
            row = candidates[0]
            try:
                ask = self.ensure(row)
                if not self.claim_update(update_id):
                    return True
                pending = row['pending']
                row['pending'] = None
                self.save(row)  # consumed durably before the write, including crashes
                if self.settle(row, ask):
                    notice = 'Ask is ' + ask['state']
                elif self.now() > pending['expires']:
                    notice = 'Weighty confirmation expired; tap Approve on the card again'
                    self.display(row)
                elif not isinstance(message.get('text'), str) or message['text'].strip() != str(row['ask']):
                    notice = f"ask {row['ask']}'s number was not typed back, so nothing was done"
                    self.display(row)
                elif ask['state'] != 'open':
                    notice = 'Ask is ' + ask['state']
                else:
                    notice = self.decide(row, 'approve', True)
            except NativeError as error:
                notice = str(error)
            self.bridge._command_message(f'runask:{update_id}:notice', dict(type='send', text=notice, plain=None), self.thread(row))
            return True

    def decide(self, row, action, weighed):
        ask = self.ensure(row)  # last ownership/proposal/state read before approval
        if ask['state'] != 'open':
            self.settle(row, ask)
            return 'Ask is ' + ask['state']
        row.update(pending=None, uncertain=True)
        self.save(row)
        args = dict(bead=row['bead'], ask=row['ask'], actor=self.config['yardr_operator'])
        method = 'ApproveAsk' if action == 'approve' else 'DeclineAsk'
        args.update(dict(weighed=weighed, values={}) if action == 'approve' else dict(words='Declined in Telegram'))
        refusal = None
        try:
            self.client.call(method, args)
        except NativeError as error:
            refusal = error
        # Even a wire error or 5xx can follow a committed operation; only canonical state decides.
        known_open = False
        try:
            ask = self.ensure(row)
            if self.settle(row, ask):
                return 'Canonical decision recorded'
            known_open = ask['state'] == 'open'
        except NativeError:
            if row['state'] == 'stale':
                return 'run_ask_stale_proposal'
        refused = known_open and isinstance(refusal, RPCRefused)
        self.display(row, 'The yard refused this action; read the card again' if refused else UNCERTAIN)
        return str(refusal) if refused else UNCERTAIN

    def close(self, message_id=None, ask_key=None):
        with self.lock():
            if ask_key is not None:
                with self.store.connect() as db:
                    found = db.execute('SELECT id FROM run_ask_keys WHERE key=?', (ask_key,)).fetchone()
                row = self.get(found[0]) if found else None
            else:
                found = [r for r in self.rows() if r['message_id'] == message_id]
                if len(found) > 1:
                    return self.bridge.receipt(reason='run_ask_message_binding_ambiguous')
                row = found[0] if found else None
            if row is None:
                return None
            try:
                ask = self.ensure(row)
                return self.settle(row, ask) or self.bridge.receipt(reason='run_ask_still_open_in_yard')
            except NativeError as error:
                return self.bridge.receipt(reason=str(error))

    def refresh(self):
        if self.client is None:
            return
        with self.lock():
            for row in self.rows():
                if row['binding'] != self.binding() or row['message_id'] is None or row['state'] == 'stale':
                    continue
                try:
                    ask = self.ensure(row)
                    if not self.settle(row, ask) and row.get('pending') and self.now() > row['pending']['expires']:
                        row['pending'] = None
                        self.save(row)
                        self.display(row, 'Weighty confirmation expired; tap Approve again')
                except NativeError:
                    continue
