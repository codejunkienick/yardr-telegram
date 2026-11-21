"""Whole yardr mail to the operator: on the yard's tell, each unread mail of
the window read over the native socket and sent whole, in parts that fit
Telegram, through the ledger (OPERATIONS.md, "Whole mail")."""
from collections import deque
import datetime
from html import escape, unescape
import importlib.util
from pathlib import Path
import re
import threading

import run_asks as n

_markdown_spec = importlib.util.spec_from_file_location('mail_markdown', Path(__file__).resolve().with_name('markdown.py'))
markdown = importlib.util.module_from_spec(_markdown_spec)
_markdown_spec.loader.exec_module(markdown)

KEY_PREFIX = 'yardmail:'
PART_UNITS = 4096
SOURCE_UNITS = 16384  # bridge.HTML_SOURCE_MAX_UNITS
MAX_PARTS = 99
UNCONFIRMED = ' · an earlier part is unconfirmed'
FILES_UNITS = 512
AMBIGUOUS = 'ambiguous_delivery_requires_manual_reconciliation'
# Core's PushMaxAge: an older push is never told, so its mail is not expanded.
WINDOW = datetime.timedelta(hours=6)
READ_SECONDS, START_SECONDS, CLIENT_TIMEOUT = 3, 7, 2
PAGE = 200
# Core's push wording (internal/dispatch/pushes.go pushMessage, pushLine).
_SINGLE = re.compile(r'(mail|timer) from (\S+)\Z')
_BATCH = re.compile(r'([1-9][0-9]*) new in your yardr inbox\Z')
_HEAD = re.compile(r'(mail|timer) from (\S+?)(?: \(([1-9][0-9]*)\))?\Z')
_PRE = re.compile(r'<pre><code((?: class="[^"<>]*")?)>(.*?)</code></pre>', re.S)


class MailError(Exception):
    """The window's mail was not read; the message is a fixed phrase."""


class Unfinished(Exception):
    """A part or the notice was not sent: the tell answers 503 and core's
    retry resumes from the ledger."""


class Unrenderable(Exception):
    pass


def units(text):
    return len(text.encode('utf-16-le')) // 2


def letter_kind(letter):
    return 'timer' if letter.get('timer') else 'mail'


def letter_at(value):
    if not isinstance(value, str):
        raise MailError('yard answer not understood')
    try:
        at = datetime.datetime.fromisoformat(value)
    except ValueError:
        raise MailError('yard answer not understood') from None
    if at.tzinfo is None:
        raise MailError('yard answer not understood')
    return at


def check_letter(letter):
    """The letter as read, or MailError naming no part of its text."""
    files = letter.get('files', []) if isinstance(letter, dict) else None
    if (not isinstance(letter, dict) or type(letter.get('seq')) is not int or letter['seq'] <= 0
            or not all(isinstance(letter.get(k), str) for k in ('from', 'to', 'text'))
            or not letter['from'] or type(letter.get('read')) is not bool
            or not isinstance(letter.get('timer', ''), str) or not isinstance(files, list)
            or not all(isinstance(f, dict) and isinstance(f.get('hash'), str) and isinstance(f.get('type', ''), str)
                       and isinstance(f.get('name', ''), str) for f in files)):
        raise MailError('yard answer not understood')
    letter_at(letter.get('at'))
    return letter


def files_line(files):
    """Part 1's second line for a mail with files: their names, at most
    FILES_UNITS visible units, the rest counted."""
    names = [f.get('name') or f"{f.get('type') or 'file'} {f['hash'][:12]}" for f in files]
    shown = []
    for name in names:
        rest = len(names) - len(shown) - 1
        line = 'files: ' + ', '.join(shown + [name]) + (f' and {rest} more' if rest else '') + ' (in the yard)'
        if units(line) > FILES_UNITS:
            break
        shown.append(name)
    rest = len(names) - len(shown)
    listed = ', '.join(escape(name, quote=False) for name in shown)
    if not shown:
        return f'files: {rest} (in the yard)'
    return 'files: ' + listed + (f' and {rest} more' if rest else '') + ' (in the yard)'


def header(letter, tz, index, count, unconfirmed=False):
    """A part's header, HTML: who and when, i/n, the unconfirmed suffix,
    and part 1's files line."""
    line = (f"<b>{letter_kind(letter)} from {escape(letter['from'], quote=False)}</b> · "
            + letter_at(letter['at']).astimezone(tz).strftime('%H:%M'))
    if count > 1:
        line += f' · {index + 1}/{count}'
    if unconfirmed:
        line += UNCONFIRMED
    if index == 0 and letter.get('files'):
        line += '\n' + files_line(letter['files'])
    return line


def body_blocks(text, validate):
    """The mail's text as top-level blocks of one tier, chosen once: Markdown
    with links, else with links as text, else the text escaped."""
    for links in (True, False):
        try:
            rendered = markdown.render_blocks(text, SOURCE_UNITS, links=links)
        except markdown.MarkdownError:
            continue
        blocks = []
        for block in rendered:
            visible, error = validate(block['html'])
            if error:
                break
            if block['fallback'] != block['html']:
                source, error = validate(block['fallback'])
                if error:
                    break
                block = dict(block, source=source)
            blocks.append(dict(block, visible=units(visible),
                               pieces=pieces(dict(block, html=block['fallback']), validate)))
        else:
            return blocks
    return [dict(html=escape(text, quote=False), visible=units(text), code=None, source=text)] if text.strip() else []


def pieces(block, validate):
    """A list or quote with code in it as blocks to cut it at: the text
    between its code blocks, plain, and each code block whole. None when it
    has none, or a piece is not as render writes it."""
    html = block['html']
    if block['code'] or '<pre>' not in html:
        return None
    out, at = [], 0

    def text(segment):
        segment = segment.strip()
        if not segment:
            return True
        visible, error = validate(segment)
        if error:
            return False
        out.append(dict(html=segment, visible=units(visible), code=None, source=visible))
        return True
    for match in _PRE.finditer(html):
        content = unescape(match.group(2))
        visible, error = validate(match.group(0))
        if error or markdown.pre(content, match.group(1)) != match.group(0) or not text(html[at:match.start()]):
            return None
        out.append(dict(html=match.group(0), visible=units(visible), code=(content, match.group(1)), source=content))
        at = match.end()
    return out if text(html[at:]) and out else None


def _smaller(atom, code):
    """An atom too long for a part alone, cut finer: a text line at its
    spaces, else (and a code line always) at code points."""
    words = re.findall(r'\S+\s*|\s+', atom) if not code else []
    return words if len(words) > 1 else list(atom)


def pack(blocks, room):
    """Part bodies from whole blocks, a blank line between; room(i) is part
    i's (visible, source) units. A block that alone does not fit is cut: a
    list or quote with code at its pieces, a code block at lines into
    consecutive exact <pre> slices, any other at lines of its source as
    escaped text with resolved link destinations. Nothing is dropped or reordered."""
    parts = [[]]
    sizes = [[0, 0]]

    def fits(visible, source):
        rv, rs = room(len(parts) - 1)
        gap = 2 if parts[-1] else 0
        return sizes[-1][0] + gap + visible <= rv and sizes[-1][1] + gap + source <= rs

    def add(html, visible, source):
        gap = 2 if parts[-1] else 0
        parts[-1].append(html)
        sizes[-1][0] += gap + visible
        sizes[-1][1] += gap + source

    def close():
        parts.append([])
        sizes.append([0, 0])

    def place(block):
        whole = (block['html'], block['visible'], units(block['html']))
        if fits(*whole[1:]):
            return add(*whole)
        if parts[-1]:
            close()
            if fits(*whole[1:]):
                return add(*whole)
        if block.get('pieces'):
            for piece in block['pieces']:
                place(piece)
            return
        code = block['code']
        if code:
            content, cls = code
            wrap, overhead = (lambda s, c=cls: markdown.pre(s, c)), units(markdown.pre('', cls))
            atoms = deque(content.splitlines(keepends=True))
        else:
            wrap, overhead = (lambda s: escape(s, quote=False)), 0
            atoms = deque(block['source'].splitlines(keepends=True))
        buf, visible, source = '', 0, overhead
        while atoms:
            atom = atoms.popleft()
            av, asrc = units(atom), units(escape(atom, quote=False))
            if fits(visible + av, source + asrc):
                buf, visible, source = buf + atom, visible + av, source + asrc
                continue
            atoms.appendleft(atom)
            if buf:
                add(wrap(buf), visible, source)
                close()
                buf, visible, source = '', 0, overhead
            elif parts[-1]:
                close()
            else:
                atoms.popleft()
                atoms.extendleft(reversed(_smaller(atom, code)))
        if buf:
            add(wrap(buf), visible, source)

    for block in blocks:
        place(block)
    return ['\n\n'.join(part) for part in parts if part] or ['']


def render(letter, tz, validate):
    """The mail's part bodies. Their count and boundaries depend on the
    mail and settings alone, never on the ledger: each part's room is the
    limit less its worst-case header (i/n at 99/99, the unconfirmed suffix,
    part 1's files line), and the header sent is never longer."""
    worst = []
    for index in (0, 1):
        visible, error = validate(header(letter, tz, index, MAX_PARTS, True))
        if error:
            raise Unrenderable(error)
        source = units(header(letter, tz, index, MAX_PARTS, True))
        worst.append((PART_UNITS - units(visible) - 1, SOURCE_UNITS - source - 1))
    bodies = pack(body_blocks(letter['text'], validate), lambda i: worst[min(i, 1)])
    if len(bodies) > MAX_PARTS:
        raise Unrenderable('mail_parts_over_99')
    return bodies


def part_text(letter, tz, index, count, body, unconfirmed=False):
    head = header(letter, tz, index, count, unconfirmed)
    return head + '\n' + body if body else head


def part_request(conversation, letter, index, text):
    """One part's publish request: its whole identity, so its fingerprint."""
    return dict(conversation=conversation, idempotency_key=f"{KEY_PREFIX}{letter['seq']}:{index}", text=text,
                format='html', text_options=True)


def notice_heads(title, text):
    """Core's notice as [(kind, sender, count)] when it tells of mail alone,
    exactly in core's wording; else None."""
    single = _SINGLE.fullmatch(title)
    if single:
        return [(single.group(1), single.group(2), 1)]
    batch = _BATCH.fullmatch(title)
    if not batch:
        return None
    heads = []
    for item in text.split(', '):
        head = _HEAD.fullmatch(item)
        if not head:
            return None
        heads.append((head.group(1), head.group(2), int(head.group(3) or 1)))
    return heads if sum(count for _, _, count in heads) == int(batch.group(1)) else None


class YardMail:
    def __init__(self, bridge, validate, resendable):
        """validate and resendable are bridge.py's html_visible_text and
        resendable."""
        self.bridge, self.config, self.store = bridge, bridge.config, bridge.store
        self.validate, self.resendable = validate, resendable
        self.enabled = bool(self.config.get('yardr_mail')) and self.store is not None
        self.lock = threading.Lock()
        self.client = self.since = None
        if self.enabled:
            self.client = n.YardSocketClient(self.config['yardr_home'], self.config.get('yardr_source', 'tgbridge'),
                                             timeout=CLIENT_TIMEOUT)
            # The first start with [yardr.mail]: mail before it is never expanded.
            with self.store.connect() as db:
                db.execute('CREATE TABLE IF NOT EXISTS yard_mail_since (id INTEGER PRIMARY KEY CHECK (id = 1), at TEXT NOT NULL)')
                # An unread sender's scan: letters low..top were scanned and their unread mail went final;
                # the next tell goes on below low (0: down to the window's start), then above top. checked:
                # the inbox's newest unread seq when the scan last reached both ends, so all mail to it is final.
                db.execute('CREATE TABLE IF NOT EXISTS yard_mail_scan (sender TEXT PRIMARY KEY, top INTEGER NOT NULL, '
                           'low INTEGER NOT NULL, checked INTEGER NOT NULL)')
                # The window's mail found and gone final, whole or not: what the notice is weighed against.
                db.execute('CREATE TABLE IF NOT EXISTS yard_mail_found (seq INTEGER PRIMARY KEY, sender TEXT NOT NULL, '
                           'kind TEXT NOT NULL, at REAL NOT NULL, whole INTEGER NOT NULL)')
                db.execute('INSERT OR IGNORE INTO yard_mail_since VALUES (1, ?)',
                           (datetime.datetime.now(datetime.timezone.utc).isoformat(),))
                self.since = letter_at(db.execute('SELECT at FROM yard_mail_since').fetchone()[0])

    def read(self, started, now=None):
        """(the operator's unread mail of the window by seq, each unread
        sender's scan {sender: (top, low, checked) or None} as _scan reads it
        within READ_SECONDS, the inbox's newest unread seq, the window's
        start). None when the operator is not told through this source.
        MailError when the read was refused or not understood; Unfinished
        when the yard did not answer it."""
        op, source = self.config['yardr_operator'], self.config.get('yardr_source', 'tgbridge')
        try:
            if n.verify_operator(self.client, self.config).get('notify') != source:
                return None
            box = self.client.call('UserInbox', {'name': op, 'limit': 1, 'actor': op})
            senders = box.get('from') if isinstance(box, dict) else None
            last = box.get('last') if isinstance(box, dict) else None
            if (not isinstance(senders, list) or not all(isinstance(s, str) and s for s in senders)
                    or type(last) is not int or last < 0):
                raise MailError('yard answer not understood')
            with self.store.connect() as db:
                saved = {sender: row for sender, *row in db.execute('SELECT sender, top, low, checked FROM yard_mail_scan')}
            floor = max(self.since, (now or datetime.datetime.now(datetime.timezone.utc)) - WINDOW)
            found, scans = {}, {}
            # Senders least read first, so a tell out of time goes on where the last one stopped.
            for sender in sorted(dict.fromkeys(senders), key=lambda s: saved[s][2] if s in saved else -1):
                scans[sender] = self._scan(op, sender, saved.get(sender), floor, found, started, last)
        except n.RPCUnavailable:
            self.bridge._log('yardmail not read: yard unavailable; retry')
            raise Unfinished() from None
        except n.RPCAmbiguous:
            self.bridge._log('yardmail not read: yard did not answer; retry')
            raise Unfinished() from None
        except n.RPCRefused:
            raise MailError('yard refused the read') from None
        except n.NativeError:
            raise MailError('operator not verified') from None
        return [found[seq] for seq in sorted(found)], scans, last, floor

    def _scan(self, op, sender, saved, floor, found, started, last):
        """One sender's scan as read describes it: none when its mail is
        final through the inbox's newest unread (last); else below its saved
        range to the window's start first (low 0: already there), then newer
        than it, so each page read is progress. Its unread window mail goes
        into found."""
        def pages(after, before):
            """Pages from before (0: the newest end) down to after or the
            window's start: (newest seq, oldest seq, reached)."""
            newest = oldest = None
            while self.bridge.monotonic() - started < READ_SECONDS:
                letters = self.client.call('Conversation', {'a': op, 'b': sender, 'after': after, 'limit': PAGE,
                                                            'before': before})
                if not isinstance(letters, list):
                    raise MailError('yard answer not understood')
                for letter in map(check_letter, letters):
                    if letter['to'] == op and not letter['read'] and letter_at(letter['at']) >= floor:
                        found[letter['seq']] = letter
                if letters:
                    newest, oldest = newest or letters[-1]['seq'], letters[0]['seq']
                if len(letters) < PAGE or letter_at(letters[0]['at']) < floor:
                    return newest, oldest, True
                before = letters[0]['seq']
            self.bridge._log(f'yardmail sender {sender} not read to the window start yet; it resumes')
            return newest, oldest, False

        if saved and not saved[1] and saved[2] >= last:
            return saved
        if saved and saved[1]:
            _, oldest, reached = pages(0, saved[1])
            if not reached:
                return saved[0], oldest or saved[1], 0
        top = saved[0] if saved else 0
        newest, oldest, reached = pages(top, 0)
        if reached:
            return newest or top, 0, last
        if newest is None:
            return (top, 0, saved[2]) if saved else None
        # What is between top and oldest is read on the next tell, below oldest.
        return newest, oldest, 0

    def deliver(self, conversation, letter, started):
        """Send the mail's parts not yet final, in order. (delivered whole,
        message id of the last part sent now or None). Unfinished when a
        part was not sent or the time to start one has passed."""
        tz, seq = self.bridge.local_tz, letter['seq']
        try:
            bodies = render(letter, tz, self.validate)
        except Unrenderable as error:
            # Not sent whole, so the tell is not done: never answered 200 while it is in the window.
            self.bridge._log(f'yardmail_unrenderable {seq}: {error}')
            raise Unfinished() from None
        whole, unconfirmed, last = True, False, None
        for index, body in enumerate(bodies):
            request = part_request(conversation, letter, index,
                                   part_text(letter, tz, index, len(bodies), body, unconfirmed))
            key = request['idempotency_key']
            saved = self.store.saved_delivery(key)
            if saved is not None and saved[0] != self.bridge.delivery_fingerprint(request):
                self.bridge._log(f'yardmail {key} ended: idempotency_key_conflict')
                return False, last
            if saved is not None and not self.resendable(saved[1]):
                # Final: read, not published again, so it takes no ledger write or start time.
                receipt = saved[1]
            else:
                if self.bridge.monotonic() - started >= START_SECONDS:
                    raise Unfinished()
                receipt = self.bridge.publish(request, resend_on_429=True, start_by=started + START_SECONDS)
                if receipt.get('delivered') is True:
                    last = receipt.get('message_id')
            if receipt.get('delivered') is True:
                continue
            if self.resendable(receipt):
                raise Unfinished()
            reason = (receipt.get('metadata') or {}).get('reason')
            if reason != AMBIGUOUS:
                self.bridge._log(f'yardmail {key} ended: {reason}')
                return False, last
            whole, unconfirmed = False, True
        return whole, last

    def tell(self, conversation, title, text, notify):
        """Expand one tell: each unread window mail whole, then core's
        notice unless the mails sent cover it. notify(line) sends the notice
        (line: a fixed reason the mail was not read, or None) and returns its
        receipt. The answer's receipt, or Unfinished: a part not sent, the
        yard not answering, or a sender's mail not yet read through the
        inbox's newest unread."""
        started = self.bridge.monotonic()
        try:
            read = self.read(started)
        except MailError as error:
            self.bridge._log(f'yardmail not read: {error}')
            return notify(f'(whole mail not read: {error})')
        if read is None:
            return notify(None)
        mails, scans, last, floor = read
        if not self.lock.acquire(timeout=max(0, START_SECONDS - (self.bridge.monotonic() - started))):
            raise Unfinished()
        try:
            whole, sent_last = {}, None
            for letter in mails:
                whole[letter['seq']], sent = self.deliver(conversation, letter, started)
                sent_last = sent or sent_last
            # Every mail found went final: the ranges read are kept, and a scan not done goes on from them.
            self.save(scans, mails, whole, floor)
            if not all(scan and not scan[1] and scan[2] >= last for scan in scans.values()):
                raise Unfinished()
            heads = notice_heads(title, text)
            if heads is not None and all(self.covered(*head) for head in heads):
                return dict(delivered=True, conversation=conversation, message_id=sent_last)
            if self.bridge.monotonic() - started >= START_SECONDS:
                raise Unfinished()
            return notify(None)
        finally:
            self.lock.release()

    def save(self, scans, mails, whole, floor):
        """Keep each unread sender's scan and the window's mail found, now
        final; drop what is of no unread sender or before the window."""
        with self.store.connect() as db:
            known = ','.join('?' * len(scans))
            db.execute(f'DELETE FROM yard_mail_scan WHERE sender NOT IN ({known})', list(scans))
            for sender, scan in scans.items():
                if scan is not None:
                    db.execute('INSERT OR REPLACE INTO yard_mail_scan VALUES (?, ?, ?, ?)', (sender, *scan))
            for m in mails:
                db.execute('INSERT OR REPLACE INTO yard_mail_found VALUES (?, ?, ?, ?, ?)',
                           (m['seq'], m['from'], letter_kind(m), letter_at(m['at']).timestamp(), whole[m['seq']]))
            db.execute(f'DELETE FROM yard_mail_found WHERE sender NOT IN ({known}) OR at < ?',
                       [*scans, floor.timestamp()])

    def covered(self, kind, sender, count):
        """A head of core's notice is covered when every window mail of that
        kind and sender found by this tell or an earlier one went whole, at
        least as many as it counts."""
        with self.store.connect() as db:
            whole = [w for w, in db.execute('SELECT whole FROM yard_mail_found WHERE kind = ? AND sender = ?', (kind, sender))]
        return len(whole) >= count and all(whole)
