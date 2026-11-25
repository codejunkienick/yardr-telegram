"""Read-only linked-yard crew mail in the paired operator chat."""
import hashlib
from html import escape, unescape
import json
import re
import secrets
import socket
import sqlite3
import threading
import time

import run_asks as native
import yard_mail

KEY_PREFIX = 'peermail:'
NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9._/-]{0,63}')
WIRE_ID = re.compile(r'[A-Za-z0-9_-]{1,64}')
REFERENCE = re.compile(r'Mirror ref ([0-9a-f]{32})')
RETRY_MIN, RETRY_MAX = 1, 60


class MirrorError(native.NativeError):
    pass


def initialize(db):
    db.execute('CREATE TABLE IF NOT EXISTS peer_mail_cursor (id INTEGER PRIMARY KEY CHECK (id=1), '
               'activation INTEGER NOT NULL, cursor INTEGER NOT NULL, event_id TEXT NOT NULL, binding TEXT NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS peer_mail_crew (name TEXT PRIMARY KEY, floor INTEGER NOT NULL, present INTEGER NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS peer_mail_links (name TEXT PRIMARY KEY)')
    db.execute('CREATE TABLE IF NOT EXISTS peer_mail_outbox (peer TEXT NOT NULL, wire_id TEXT NOT NULL, '
               'crew TEXT, letter TEXT, PRIMARY KEY (peer, wire_id))')
    db.execute('CREATE TABLE IF NOT EXISTS peer_mail_work (ready INTEGER PRIMARY KEY, letter_seq INTEGER NOT NULL UNIQUE, '
               'crew TEXT NOT NULL, letter TEXT NOT NULL, parts TEXT, '
               'part INTEGER NOT NULL DEFAULT 0, incomplete INTEGER NOT NULL DEFAULT 0)')
    db.execute('CREATE TABLE IF NOT EXISTS peer_mail_parts (key TEXT PRIMARY KEY, letter_seq INTEGER NOT NULL, '
               'ref TEXT NOT NULL UNIQUE, text TEXT, proof TEXT NOT NULL, bot INTEGER NOT NULL, chat INTEGER NOT NULL, '
               'attempted INTEGER NOT NULL DEFAULT 0, message_id INTEGER)')
    # A claimed letter's destination thread, NULL for the paired chat: every
    # part of the letter reuses it, so retries, restarts and settings changes
    # never move an unfinished letter to another conversation.
    db.execute('CREATE TABLE IF NOT EXISTS peer_mail_threads (letter_seq INTEGER PRIMARY KEY, thread INTEGER)')
    db.execute('CREATE UNIQUE INDEX IF NOT EXISTS peer_mail_message '
               'ON peer_mail_parts (bot, chat, message_id) WHERE message_id IS NOT NULL')


def names(value, field):
    if not isinstance(value, str) or not NAME.fullmatch(value):
        raise MirrorError(f'invalid native {field}')
    return value


def wire_id(value):
    if not isinstance(value, str) or not WIRE_ID.fullmatch(value):
        raise MirrorError('invalid native peer mail wire ID')
    return value


def seqno(value, field):
    if type(value) is not int or not 0 <= value < 2**63:
        raise MirrorError(f'invalid native {field} sequence')
    return value


def event_id(event):
    value = event.get('id')
    if not isinstance(value, str) or not value:
        raise MirrorError('invalid native event ID')
    # Core's ID is the decimal sequence. Its timestamp and kind distinguish a reused sequence.
    return hashlib.sha256(native.canonical([value, event['at'], event['kind'], event['actor']]).encode()).hexdigest()


def verify_crew(client, crew):
    member = client.call('GetCrew', {'name': crew})
    if not isinstance(member, dict) or member.get('name') != crew:
        raise MirrorError('peer mail allowlist member is not native crew')


def proof(text):
    # Telegram can trim trailing whitespace and normalizes line endings.
    text = text.replace('\r\n', '\n').replace('\r', '\n').rstrip()
    return hashlib.sha256(text.encode()).hexdigest()


def header(letter, index, count, incomplete):
    peer = f"{letter['remote']}@{letter['peer']}"
    if letter['incoming']:
        line = f"Incoming · {peer} → {letter['crew']}"
    else:
        line = f"Outgoing · {letter['crew']} → {peer}"
    line += f' · {index + 1}/{count}'
    if incomplete:
        line += yard_mail.UNCONFIRMED
    if index == 0 and letter.get('files'):
        line += '\n' + unescape(yard_mail.files_line(letter['files']))
    return line


def bodies(letter):
    # Pack the literal text so Markdown syntax and whitespace can travel as written.
    framing = yard_mail.units('\n\nMirror ref ' + '0' * 32)
    other, first = (yard_mail.PART_UNITS - yard_mail.units(header(letter, i, yard_mail.MAX_PARTS, True)) - framing
                    for i in (yard_mail.MAX_PARTS - 1, 0))
    first -= 1  # room for the widest part index in the first-part estimate
    text = letter['text']
    blocks = [dict(html=escape(text, quote=False), visible=yard_mail.units(text), code=None, source=text)]
    parts = yard_mail.pack(blocks, lambda i: (other if i else first, yard_mail.SOURCE_UNITS))
    if len(parts) > yard_mail.MAX_PARTS:
        raise MirrorError('peer mail exceeds the part limit')
    return [unescape(part) for part in parts]


class PeerMail:
    def __init__(self, bridge, resendable):
        self.bridge, self.store, self.config = bridge, bridge.store, bridge.config
        self.crew = frozenset(self.config.get('yardr_peer_mail', ()))
        self.client = None
        if self.crew and self.store is not None:
            self.client = native.YardSocketClient(self.config['yardr_home'], self.config['yardr_source'],
                                                  timeout=2, verify_owner=True)
        self.resendable = resendable
        self.stopped, self.active, self.wake = (threading.Event() for _ in range(3))
        self.sock_lock = threading.Lock()
        self.sock = None
        self.workers = ()

    def start(self):
        if self.client is None:
            return
        self.workers = tuple(threading.Thread(target=target, name=name, daemon=True) for name, target in (
            ('peer-mail-stream', self.stream), ('peer-mail-delivery', self.deliver)))
        for worker in self.workers:
            worker.start()

    def stop(self):
        self.stopped.set()
        self.wake.set()
        with self.sock_lock:
            if self.sock is not None:
                try:
                    self.sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
        for worker in self.workers:
            worker.join(timeout=35)

    def register_socket(self, sock):
        with self.sock_lock:
            self.sock = sock
            if sock is not None and self.stopped.is_set():
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

    def binding(self):
        user = native.verify_operator(self.client, self.config)
        created = user.get('created_at')
        try:
            yard_mail.letter_at(created)
            home = native.validate_home(self.client.home)
            info = home.stat()
        except (yard_mail.MailError, OSError):
            raise MirrorError('invalid native operator or yard home identity') from None
        return native.canonical([str(home), info.st_dev, info.st_ino, self.config['yardr_operator'], created,
                                 self.config['yardr_source'], self.config['user_id'], self.config['bot_id'],
                                 self.bridge.conversation])

    def head(self):
        return seqno(self.client.call('Head', []), 'Head')

    def checkpoint(self, seq):
        if not seq:
            return ''
        rows = self.client.call('EventsSince', [seq - 1, 1])
        if not isinstance(rows, list) or len(rows) != 1 or native.validate_stream_event(rows[0])['seq'] != seq:
            raise MirrorError('native peer mail checkpoint is missing')
        return event_id(rows[0])

    def snapshot_links(self):
        peers = self.client.call('Peers', {})
        if not isinstance(peers, list):
            raise MirrorError('invalid native Peers response')
        links = {}
        for peer in peers:
            if not isinstance(peer, dict):
                raise MirrorError('invalid native Peers row')
            name = names(peer.get('name'), 'peer name')
            if name in links:
                raise MirrorError('duplicate native peer name')
            outbox = self.client.call('PeerOutbox', [name, 0])
            if not isinstance(outbox, list):
                raise MirrorError('invalid native PeerOutbox response')
            old_mail = set()
            for item in outbox:
                if (not isinstance(item, dict) or item.get('peer') != name
                        or not isinstance(item.get('kind'), str) or not item['kind']):
                    raise MirrorError('invalid native PeerOutbox row')
                if item['kind'] == 'mail':
                    old_mail.add(wire_id(item.get('id')))
            links[name] = old_mail
        return links

    def prepare(self):
        binding = self.binding()
        with self.store.connect() as db:
            saved = db.execute('SELECT cursor, event_id, binding FROM peer_mail_cursor').fetchone()
            existing = {row[0] for row in db.execute('SELECT name FROM peer_mail_crew')}
        if saved is not None and saved[2] != binding:
            raise MirrorError('peer mail ledger identity changed')
        head = self.head()
        if saved is not None and (head < saved[0] or self.checkpoint(saved[0]) != saved[1]):
            raise MirrorError('native peer mail checkpoint changed')
        added = self.crew - existing
        removed = existing - self.crew
        if saved is not None and not added and not removed:
            return saved[0]
        for crew in sorted(added):
            verify_crew(self.client, crew)
        links = self.snapshot_links() if saved is None else {}
        anchor = self.checkpoint(head) if saved is None else ''
        if self.head() != head or self.binding() != binding:
            raise MirrorError('native yard changed during peer mail activation')
        with self.store.connect() as db:
            if saved is None:
                db.execute('INSERT INTO peer_mail_cursor VALUES (1, ?, ?, ?, ?)', (head, head, anchor, binding))
                db.executemany('INSERT INTO peer_mail_links VALUES (?)', ((peer,) for peer in links))
                db.executemany('INSERT INTO peer_mail_outbox (peer, wire_id) VALUES (?, ?)',
                               ((peer, wire) for peer, wires in links.items() for wire in wires))
            for crew in sorted(removed):
                db.execute('DELETE FROM peer_mail_crew WHERE name=?', (crew,))
                db.execute('UPDATE peer_mail_outbox SET crew=NULL, letter=NULL WHERE crew=?', (crew,))
                db.execute('UPDATE peer_mail_parts SET text=NULL WHERE letter_seq IN '
                           '(SELECT letter_seq FROM peer_mail_work WHERE crew=?)', (crew,))
                db.execute('DELETE FROM peer_mail_threads WHERE letter_seq IN '
                           '(SELECT letter_seq FROM peer_mail_work WHERE crew=?)', (crew,))
                db.execute('DELETE FROM peer_mail_work WHERE crew=?', (crew,))
            db.executemany('INSERT INTO peer_mail_crew VALUES (?, ?, 1)', ((crew, head) for crew in sorted(added)))
        return head if saved is None else saved[0]

    def authorize(self):
        binding = self.binding()
        with self.store.connect() as db:
            saved = db.execute('SELECT cursor, event_id, binding FROM peer_mail_cursor').fetchone()
        if saved is None or binding != saved[2]:
            raise MirrorError('peer mail ledger identity changed')
        return saved[0], saved[1]

    @staticmethod
    def link(db, peer):
        if db.execute('SELECT 1 FROM peer_mail_links WHERE name=?', (peer,)).fetchone() is None:
            raise MirrorError('unknown native peer mail link')

    @staticmethod
    def queue(db, seq, letter):
        db.execute('INSERT INTO peer_mail_work (ready, letter_seq, crew, letter) VALUES (?, ?, ?, ?)',
                   (seq, letter['seq'], letter['crew'], native.canonical(letter)))

    def mail(self, db, event, data):
        incoming = 'from_peer' in data
        if incoming and 'to_peer' in data:
            raise MirrorError('invalid native peer mail direction')
        peer = names(data.get('from_peer' if incoming else 'to_peer'), 'peer mail link')
        self.link(db, peer)
        ident = wire_id(data.get('id'))
        seq = event['seq']
        crew = names(data.get('to') if incoming else event['actor'], 'peer mail local name')
        to_user = data.get('to_user', False)
        if type(to_user) is not bool:
            raise MirrorError('invalid native peer mail recipient kind')
        allowed = db.execute('SELECT 1 FROM peer_mail_crew WHERE name=? AND present=1 AND floor<?', (crew, seq)).fetchone()
        letter = None
        if allowed and not to_user:
            remote = names(data.get('from' if incoming else 'to'), 'peer mail remote name')
            if incoming and event['actor'] != f'{remote}@{peer}':
                raise MirrorError('invalid native peer mail sender')
            try:
                yard_mail.check_letter(dict(seq=seq, read=False, at=event['at'],
                                            **{'from': remote, 'to': crew, 'text': data.get('text'),
                                               'files': data.get('files', [])}))
            except yard_mail.MailError:
                raise MirrorError('invalid native peer mail letter') from None
            letter = dict(seq=seq, crew=crew, remote=remote, peer=peer, incoming=incoming,
                          text=native.text(data['text'], 'peer mail body', controls=True), files=data.get('files', []))
        if incoming:
            if letter is not None:
                self.queue(db, seq, letter)
            return
        # The native confirmation only names a link and wire ID. A repeated ID in one link is ambiguous.
        db.execute('INSERT INTO peer_mail_outbox VALUES (?, ?, ?, ?) ON CONFLICT (peer, wire_id) '
                   'DO UPDATE SET crew=NULL, letter=NULL',
                   (peer, ident, crew if letter is not None else None, native.canonical(letter) if letter is not None else None))

    def settle(self, db, event, data):
        if event['actor'] != 'yard':
            raise MirrorError('invalid native peer mail confirmation actor')
        peer = names(data.get('peer'), 'peer mail confirmation link')
        ident = wire_id(data.get('id'))
        self.link(db, peer)
        pending = db.execute('SELECT letter FROM peer_mail_outbox WHERE peer=? AND wire_id=?', (peer, ident)).fetchone()
        if pending and pending[0] is not None and event['kind'] == 'peer_message_sent':
            letter = json.loads(pending[0])
            self.queue(db, event['seq'], dict(letter, peer=peer))
        db.execute('INSERT INTO peer_mail_outbox (peer, wire_id) VALUES (?, ?) ON CONFLICT (peer, wire_id) '
                   'DO UPDATE SET crew=NULL, letter=NULL', (peer, ident))

    def project(self, event, after):
        seq = seqno(event['seq'], 'event')
        if seq != after + 1:
            raise MirrorError('native peer mail stream has a sequence gap')
        kind, data = event['kind'], event.get('data', {})
        peer_letter = kind == 'mail' and ('from_peer' in data or 'to_peer' in data)
        if peer_letter:
            self.authorize()
        with self.store.connect() as db:
            if peer_letter:
                self.mail(db, event, data)
            elif kind in ('peer_message_sent', 'peer_message_failed'):
                if not isinstance(data.get('kind'), str) or not data['kind']:
                    raise MirrorError('invalid native peer confirmation kind')
                if data['kind'] == 'mail':
                    self.settle(db, event, data)
            elif kind == 'peer_added':
                db.execute('INSERT INTO peer_mail_links VALUES (?)', (names(data.get('peer'), 'added peer'),))
            elif kind == 'peer_renamed':
                old = names(data.get('from'), 'old peer name')
                new = names(data.get('to'), 'new peer name')
                self.link(db, old)
                db.execute('UPDATE peer_mail_links SET name=? WHERE name=?', (new, old))
                db.execute('UPDATE peer_mail_outbox SET peer=? WHERE peer=?', (new, old))
            elif kind == 'peer_removed':
                peer = names(data.get('peer'), 'removed peer')
                self.link(db, peer)
                db.execute('DELETE FROM peer_mail_outbox WHERE peer=?', (peer,))
                db.execute('DELETE FROM peer_mail_links WHERE name=?', (peer,))
            elif kind in ('crew_defined', 'crew_deleted', 'resident_defined', 'resident_deleted'):
                crew = data.get('crew', data.get('resident'))
                if not isinstance(crew, str) or not crew:
                    raise MirrorError('invalid native crew roster event')
                db.execute('UPDATE peer_mail_crew SET present=? WHERE name=? AND floor<?',
                           (kind.endswith('_defined'), crew, seq))
            changed = db.execute('UPDATE peer_mail_cursor SET cursor=?, event_id=? WHERE id=1 AND cursor=?',
                                 (seq, event_id(event), after)).rowcount
            if changed != 1:
                raise MirrorError('peer mail stream cursor changed while projecting')
        self.wake.set()
        return seq

    def stream(self):
        delay, last_issue = RETRY_MIN, None
        while not self.stopped.is_set():
            try:
                after = self.prepare()

                def opened():
                    seq, anchor = self.authorize()
                    if self.checkpoint(seq) != anchor:
                        raise MirrorError('native peer mail checkpoint changed on reconnect')
                    self.active.set()
                    self.wake.set()

                for event in self.client.stream_events(after, self.stopped, self.register_socket, opened):
                    if self.stopped.is_set():
                        return
                    after = self.project(event, after)
                    delay, last_issue = RETRY_MIN, None
                issue = 'native stream disconnected'
            except (native.NativeError, sqlite3.Error, OSError) as error:
                issue = str(error) if isinstance(error, native.NativeError) else 'native stream or ledger unavailable'
            finally:
                self.active.clear()
            if self.stopped.is_set():
                return
            if issue != last_issue:
                self.bridge._log(f'peer mail paused: {issue}')
                last_issue = issue
            self.stopped.wait(delay)
            delay = min(delay * 2, RETRY_MAX)

    def part(self, letter, index, incomplete, parts):
        key = f'{KEY_PREFIX}{letter["seq"]}:{index}'
        bot, chat = self.config['bot_id'], self.config['chat_id']
        with self.store.connect() as db:
            saved = db.execute('SELECT text, bot, chat FROM peer_mail_parts WHERE key=?', (key,)).fetchone()
            if saved is not None:
                if saved[0] is None or saved[1:] != (bot, chat):
                    raise MirrorError('peer mail part identity conflict')
                return key, saved[0]
            ref = secrets.token_hex(16)
            text = header(letter, index, len(parts), incomplete)
            if parts[index]:
                text += '\n' + parts[index]
            text += f'\nMirror ref {ref}'
            db.execute('INSERT INTO peer_mail_parts (key, letter_seq, ref, text, proof, bot, chat) VALUES (?, ?, ?, ?, ?, ?, ?)',
                       (key, letter['seq'], ref, text, proof(text), bot, chat))
        return key, text

    def before_send(self):
        if self.stopped.is_set() or self.bridge.stop.is_set() or not self.active.is_set():
            raise MirrorError('peer mail stream is offline')
        seq, anchor = self.authorize()
        if self.checkpoint(seq) != anchor:
            raise MirrorError('native peer mail checkpoint changed before send')

    def confirmed(self, status, data, thread=None):
        message = data.get('result')
        if status != 200 or data.get('ok') is not True or not isinstance(message, dict):
            return False
        chat, sender, mid = message.get('chat'), message.get('from'), message.get('message_id')
        return (type(mid) is int and mid > 0 and isinstance(chat, dict) and chat.get('type') == 'private'
                and type(chat.get('id')) is int and chat['id'] == self.config['chat_id'] and isinstance(sender, dict)
                and sender.get('is_bot') is True and type(sender.get('id')) is int
                and sender['id'] == self.config['bot_id']
                and self.bridge._topic_thread(message) == thread)

    def destination(self):
        """The thread newly claimed letters mirror to, None for the paired chat."""
        name = self.config.get('yardr_peer_mail_topic')
        if name is None:
            return None
        topic = self.bridge.topic_by_name(name)
        if topic is None:
            raise MirrorError(f'peer mail topic "{name}" is not configured and recorded')
        thread = topic['thread_id']
        if type(thread) is not int or not 1 <= thread <= 2 ** 31 - 1:
            raise MirrorError(f'peer mail topic "{name}" has invalid thread {thread} (1 to 2147483647)')
        return thread

    def deliver_next(self):
        with self.store.connect() as db:
            work = db.execute('SELECT ready, letter, parts, part, incomplete FROM peer_mail_work ORDER BY ready LIMIT 1').fetchone()
        if work is None:
            return None
        ready, letter, parts, index, incomplete = work
        letter = json.loads(letter)
        if letter['crew'] not in self.crew:
            raise MirrorError('peer mail queued crew scope changed')
        with self.store.connect() as db:
            claimed = db.execute('SELECT thread FROM peer_mail_threads WHERE letter_seq=?', (letter['seq'],)).fetchone()
            started = claimed is None and (parts is not None or index != 0 or incomplete != 0
                                           or db.execute('SELECT 1 FROM peer_mail_parts WHERE letter_seq=?',
                                                         (letter['seq'],)).fetchone() is not None)
        if claimed is None:
            # Claimed before rendering, so a rendered but unclaimed letter is
            # one the General-only release started: it keeps General.
            thread = None if started else self.destination()
            with self.store.connect() as db:
                db.execute('INSERT INTO peer_mail_threads VALUES (?, ?)', (letter['seq'], thread))
        else:
            thread = claimed[0]
            if thread is not None and (type(thread) is not int or not 1 <= thread <= 2 ** 31 - 1):
                raise MirrorError(f'peer mail topic thread {thread} is not a positive int32 (1 to 2147483647)')
        if parts is None:
            parts = bodies(letter)
            with self.store.connect() as db:
                db.execute('UPDATE peer_mail_work SET parts=? WHERE ready=?', (native.canonical(parts), ready))
        else:
            parts = json.loads(parts)
        key, text = self.part(letter, index, incomplete, parts)
        conversation = self.bridge.conversation if thread is None else self.bridge.topic_conversation(thread)
        request = dict(conversation=conversation, idempotency_key=key, text=text, text_options=True)
        saved = self.store.saved_delivery(key)
        if saved is not None:
            fingerprint, receipt = saved
            if fingerprint != self.bridge.delivery_fingerprint(request):
                raise MirrorError('peer mail delivery key or destination conflict')
            if self.resendable(receipt):
                wait = receipt['metadata'].get('retry_at', 0) - time.time()
                if wait > 0:
                    return wait
        receipt = self.bridge.publish(request, mirror=True)
        reason = receipt.get('metadata', {}).get('reason')
        if reason == 'idempotency_key_conflict':
            raise MirrorError('peer mail delivery key or destination conflict')
        if reason == 'recipient_not_authorized' or receipt.get('thread_not_found') is True:
            raise MirrorError(f'peer mail topic thread {thread} is not served')
        if self.resendable(receipt):
            return max(0, receipt['metadata'].get('retry_at', 0) - time.time())
        failed = receipt.get('delivered') is not True
        if failed:
            self.bridge._log(f'peer mail part {key}: {reason or "delivery unconfirmed"}')
        with self.store.connect() as db:
            db.execute('UPDATE peer_mail_parts SET text=NULL WHERE key=?', (key,))
            if index + 1 == len(parts):
                db.execute('DELETE FROM peer_mail_work WHERE ready=?', (ready,))
                db.execute('DELETE FROM peer_mail_threads WHERE letter_seq=?', (letter['seq'],))
            else:
                db.execute('UPDATE peer_mail_work SET part=?, incomplete=? WHERE ready=?',
                           (index + 1, incomplete or failed, ready))
        return True

    def deliver(self):
        delay, last_issue = RETRY_MIN, None
        while not self.stopped.is_set():
            self.wake.clear()
            try:
                result = self.deliver_next() if self.active.is_set() else None
                if result is None:
                    self.wake.wait()
                elif result is True:
                    delay, last_issue = RETRY_MIN, None
                else:
                    self.stopped.wait(max(delay, result))
                    delay = min(delay * 2, RETRY_MAX)
            except (native.NativeError, sqlite3.Error, OSError) as error:
                issue = str(error) if isinstance(error, native.NativeError) else 'delivery ledger unavailable'
                if issue != last_issue:
                    self.bridge._log(f'peer mail delivery paused: {issue}')
                    last_issue = issue
                self.stopped.wait(delay)
                delay = min(delay * 2, RETRY_MAX)

    def is_reply(self, message):
        target = message.get('reply_to_message')
        # No thread check: the mirror may post in a topic, message ids are
        # unique per chat, and only the mirror's own sends carry its ref line.
        if self.store is None or not isinstance(target, dict):
            return False
        bot, chat = self.config['bot_id'], self.config['chat_id']
        mid = target.get('message_id')
        if type(mid) is not int or mid <= 0:
            return False
        target_chat = target.get('chat')
        if target_chat is not None and (not isinstance(target_chat, dict) or target_chat.get('id') != chat):
            return False
        with self.store.connect() as db:
            if db.execute('SELECT 1 FROM peer_mail_parts WHERE bot=? AND chat=? AND message_id=?', (bot, chat, mid)).fetchone():
                return True
            sender, text = target.get('from'), target.get('text')
            if (not isinstance(sender, dict) or sender.get('is_bot') is not True
                    or type(sender.get('id')) is not int or sender['id'] != bot
                    or not isinstance(text, str) or target_chat is None):
                return False
            ref = REFERENCE.fullmatch(text.rstrip().rpartition('\n')[2])
            if ref is None:
                return False
            expected = db.execute('SELECT proof FROM peer_mail_parts WHERE ref=? AND bot=? AND chat=? '
                                  'AND attempted=1 AND message_id IS NULL', (ref[1], bot, chat)).fetchone()
            return expected is not None and proof(text) == expected[0]
