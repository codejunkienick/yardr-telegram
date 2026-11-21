"""Whole yardr mail in Telegram: rendering and packing, the tell over a fake
or a real isolated core, the ledger across retries, and the timing."""
import datetime
import html
import http.client
import io
import json
from pathlib import Path
import re
import shutil
import socketserver
import http.server
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import zoneinfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bridge as b  # noqa: E402
import testports  # noqa: E402
import testyardr  # noqa: E402
from test_bridge import BridgeTestCase, run_main, write_settings  # noqa: E402

ym = b.yard_mail
TZ = zoneinfo.ZoneInfo('Asia/Tokyo')
OLD = datetime.datetime(2000, 1, 1, tzinfo=datetime.timezone.utc)
CMD1 = 'cd ~/src/yardr-telegram && ./install.sh --settings ~/.config/odyssey-telegram/telegram-bridge.toml\n'
CMD2 = 'systemctl --user daemon-reload &&  systemctl --user restart odyssey-telegram.service\t# <&> "q" \'s\'\n'


def now():
    return datetime.datetime.now(datetime.timezone.utc)


def stamp(when):
    return when.isoformat().replace('+00:00', 'Z')


def four_kb_mail():
    """A 4096-byte mail with two fenced commands, words numbered in order."""
    head = 'Install the whole-mail bridge, then restart it.\n\n```sh\n' + CMD1 + '```\n\n'
    tail = '\n\n```\n' + CMD2 + '```\n\nThat is all.'
    words, room = [], 4096 - len((head + tail).encode())
    while len(' '.join(words + [f'w{len(words):04d}'])) <= room:
        words.append(f'w{len(words):04d}')
    filler = ' '.join(words)
    filler += 'x' * (room - len(filler))
    text = head + filler + tail
    assert len(text.encode()) == 4096
    return text


def pres(text):
    return [html.unescape(code) for code in re.findall(r'<pre><code[^>]*>(.*?)</code></pre>', text, re.S)]


def visible(text):
    shown, error = b.html_visible_text(text)
    assert error is None, error
    return shown


def letter(seq, text, sender='sam', at=None, **extra):
    return dict(dict(seq=seq, at=stamp(at or now()), to='pat', text=text, read=False, **{'from': sender}), **extra)


class RenderTests(unittest.TestCase):
    def test_four_kb_mail_splits_at_the_limit_whole_in_order_commands_exact(self):
        text = four_kb_mail()
        mail = letter(42, text)
        bodies = ym.render(mail, TZ, b.html_visible_text)
        self.assertEqual(len(bodies), 2)
        parts = [ym.part_text(mail, TZ, i, 2, body) for i, body in enumerate(bodies)]
        for i, part in enumerate(parts):
            self.assertLessEqual(b.utf16_units(visible(part)), 4096)
            self.assertRegex(visible(part).split('\n')[0], rf'^mail from sam · \d\d:\d\d · {i + 1}/2$')
        # Split at the limit: the first part could not take the next block.
        self.assertGreater(b.utf16_units(visible(parts[0])) + 2 + b.utf16_units(visible(bodies[1].split('\n\n')[0])),
                           4096 - len(' · an earlier part is unconfirmed') - 3)
        whole = visible('\n\n'.join(block['html'] for block in ym.body_blocks(text, b.html_visible_text)))
        self.assertEqual(' '.join('\n'.join(visible(body) for body in bodies).split()), ' '.join(whole.split()))
        self.assertEqual([p for part in parts for p in pres(part)], [CMD1, CMD2])
        self.assertIn('w0000', visible(parts[0]))
        self.assertIn('That is all.', visible(parts[1]))

    def test_tiers_relative_link_then_plain_and_nothing_lost(self):
        text = 'Read [the docs](docs/x.md) and <https://example.invalid/a>.\n\n```sh\nls -la\n```'
        [body] = ym.render(letter(1, text), TZ, b.html_visible_text)
        self.assertIn('the docs (docs/x.md)', visible(body))
        self.assertNotIn('<a href', body)
        self.assertEqual(pres(body), ['ls -la\n'])
        # Markdown the renderer refuses (NUL, nesting past its depth): the text escaped, whole.
        for refused in ('a\0b <i> & `x`', '> ' * 900 + 'deep'):
            with self.subTest(refused=refused[:8]):
                bodies = ym.render(letter(2, refused), TZ, b.html_visible_text)
                self.assertEqual(''.join(visible(body) for body in bodies), refused)
                self.assertNotIn('<pre>', ''.join(bodies))

    def test_code_block_larger_than_a_part_splits_into_exact_slices(self):
        files = [dict(hash='ab' * 32, type='text/plain', name='n' * 40 + str(i)) for i in range(30)]
        code = ''.join(f'echo line {i:04d} && true\n' for i in range(170))
        astral = '𝟘' * 2100 + '\n'
        for content in (code, astral):
            with self.subTest(lines=content.count('\n')):
                mail = letter(3, '```bash\n' + content + '```', files=files)
                bodies = ym.render(mail, TZ, b.html_visible_text)
                self.assertGreaterEqual(len(bodies), 2)
                parts = [ym.part_text(mail, TZ, i, len(bodies), body, True) for i, body in enumerate(bodies)]
                self.assertTrue(all(b.utf16_units(visible(part)) <= 4096 for part in parts))
                self.assertEqual(''.join(p for part in parts for p in pres(part)), content)
                self.assertTrue(all('class="language-bash"' in part for part in parts))

    def test_header_escapes_and_shows_files_and_plain_form(self):
        files = [dict(hash='0123456789abcdef' * 4, type='image/png'), dict(hash='f' * 64, type='', name='<x>&.txt')]
        mail = letter(4, 'hi', sender='a<&>b', files=files, timer='t1')
        head = ym.header(mail, TZ, 0, 2, True)
        self.assertIn('<b>timer from a&lt;&amp;&gt;b</b>', head)
        self.assertEqual(visible(head).split('\n')[1], 'files: image/png 0123456789ab, <x>&.txt (in the yard)')
        self.assertRegex(visible(head).split('\n')[0], r'^timer from a<&>b · \d\d:\d\d · 1/2 · an earlier part is unconfirmed$')
        many = [dict(hash='c' * 64, type='text/plain', name='file-' + 'n' * 60 + f'{i}') for i in range(20)]
        line = ym.files_line(many)
        self.assertLessEqual(b.utf16_units(line), 512)
        self.assertRegex(line, r' and \d+ more \(in the yard\)$')
        self.assertEqual(ym.files_line([dict(hash='d' * 64, type='', name='z' * 600)]), 'files: 1 (in the yard)')

    def test_notice_heads_in_core_wording_only(self):
        self.assertEqual(ym.notice_heads('mail from yardmaster', 'whatever'), [('mail', 'yardmaster', 1)])
        self.assertEqual(ym.notice_heads('timer from sam', ''), [('timer', 'sam', 1)])
        self.assertEqual(ym.notice_heads('3 new in your yardr inbox', 'mail from a (2), timer from b'),
                         [('mail', 'a', 2), ('timer', 'b', 1)])
        for title, text in (('3 new in your yardr inbox', 'mail from a (2)'), ('2 new in yardr', 'mail from a, sam asks'),
                            ('2 new in your yardr inbox', 'mail from a, bead-1 closed'), ('sam asks', 'q'),
                            ('2 new in your yardr inbox', 'The 20th message this hour, and the last: x\nmail from a (2)')):
            self.assertIsNone(ym.notice_heads(title, text), title)


class FakeYard:
    """The native socket's mail reads over an in-memory log, paged as core pages."""

    def __init__(self, home):
        self.user = dict(name='pat', role='operator', notify='tgbridge', identities={'tgbridge': {'id': '123'}})
        self.letters, self.calls, self.seq = [], [], 100
        self.release = threading.Event()
        self.hang = False
        # Methods answered 503 (the yard unavailable) or refused.
        self.unavailable, self.refused = set(), set()
        self.on_call = lambda method: None
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                args = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                method = self.path.removeprefix('/call/')
                owner.calls.append((method, args))
                owner.on_call(method)
                if owner.hang:
                    owner.release.wait(10)
                    return
                if method in owner.unavailable:
                    self.send_response(503)
                    self.send_header('Content-Length', '0')
                    self.end_headers()
                    return
                if method in owner.refused:
                    self.reply({'error': {'code': 'error', 'message': 'not granted'}})
                    return
                if method == 'GetUser':
                    result = owner.user
                elif method == 'UserInbox':
                    unread = [m for m in owner.letters if m['to'] == args['name'] and not m['read']]
                    result = dict(mail=unread[:args['limit']], unread=len(unread), last=unread[-1]['seq'] if unread else 0,
                                  **{'from': list(dict.fromkeys(m['from'] for m in unread))})
                elif method == 'Conversation':
                    pair = {args['a'], args['b']}
                    rows = [m for m in owner.letters if {m['from'], m['to']} == pair and m['seq'] > args['after']
                            and (not args['before'] or m['seq'] < args['before'])]
                    result = rows[-args['limit']:] if args['limit'] else rows
                else:
                    self.reply({'error': {'code': 'not-found', 'message': 'unknown method'}})
                    return
                self.reply({'result': result})

            def reply(self, value):
                raw = json.dumps(value).encode()
                self.send_response(200)
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
            daemon_threads = True

        self.server = Server(str(home / 'yard.sock'), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def mail(self, text, sender='sam', at=None, **extra):
        self.seq += 1
        self.letters.append(letter(self.seq, text, sender, at, **extra))
        return self.letters[-1]

    def close(self):
        self.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


class MailCase(BridgeTestCase):
    def setUp(self):
        super().setUp()
        self.home = Path(tempfile.mkdtemp(prefix='ym-', dir='/tmp'))
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        self.home.chmod(0o700)
        self.yard = FakeYard(self.home)
        self.addCleanup(self.yard.close)
        self.config.update(yardr_home=str(self.home), yardr_operator='pat', yardr_source='tgbridge', yardr_mail=True)
        self.clock, self.tick, self.plan, self.sent, self.starts = [0.0], 0, [], [], []
        self.logs = []
        self.make_bridge()

    def make_bridge(self, monotonic=None):
        self.bridge = b.Bridge(self.config, self.store, 'TOKEN', 'CAPABILITY', request=self.telegram,
                               monotonic=monotonic or (lambda: self.clock[0]), sleep=lambda _s: None)
        self.bridge.hook_secret = None
        self.bridge.yard_mail.since = OLD
        self.bridge._log = self.logs.append

    def telegram(self, url, body, timeout=20):
        if not url.endswith('/sendMessage'):
            return self.transport(url, body, timeout)
        self.starts.append(self.clock[0])
        self.clock[0] += self.tick
        action = self.plan.pop(0) if self.plan else None
        if action is not None:
            return action(body)
        self.sent.append(body)
        return 200, dict(ok=True, result=dict(message_id=1000 + len(self.sent), chat=dict(id=123)))

    def tell(self, title, text=''):
        return self.bridge.api_tell(dict(title=title, text=text))

    def texts(self):
        return [visible(body['text']) if body.get('parse_mode') else body['text'] for body in self.sent]

    def seed(self, mail, index, receipt):
        """A ledger row for one part, under the fingerprint the mail path builds."""
        bodies = ym.render(mail, TZ, b.html_visible_text)
        request = ym.part_request(self.bridge.conversation, mail, index,
                                  ym.part_text(mail, TZ, index, len(bodies), bodies[index]))
        fingerprint = self.bridge.delivery_fingerprint(request)
        self.assertIsNone(self.store.reserve(request['idempotency_key'], fingerprint, receipt))
        self.store.finish(request['idempotency_key'], receipt)
        return bodies

    def two_part_mail(self, sender='sam'):
        return self.yard.mail(four_kb_mail(), sender)


def rejected(code, description='Bad Request: chat not found'):
    return lambda body: (code, dict(ok=False, error_code=code, description=description))


def not_sent(body):
    raise b.RequestNotSent('synthetic offline', 7, 'connect')


def unconfirmed(body):
    raise b.RequestUnconfirmed('synthetic lost answer', 28, 'transfer')


class MailTests(MailCase):
    def test_batch_mails_go_apart_with_sender_and_time_and_cover_the_notice(self):
        for text, sender in (('first from a', 'a'), ('second from a', 'a'), ('only from b', 'b')):
            self.yard.mail(text, sender)
        answer = self.tell('3 new in your yardr inbox', 'mail from a (2), mail from b')
        self.assertTrue(answer['delivered'])
        self.assertEqual(answer['message_id'], '1003')
        texts = self.texts()
        self.assertEqual(len(texts), 3)
        for text, (sender, words) in zip(texts, (('a', 'first from a'), ('a', 'second from a'), ('b', 'only from b'))):
            self.assertRegex(text, rf'^mail from {sender} · \d\d:\d\d\n{words}$')
        self.assertTrue(all(body['parse_mode'] == 'HTML' and 'message_thread_id' not in body for body in self.sent))
        # With an ask among them (core's "N new in yardr"): mails first, then the notice.
        self.yard.mail('third from a', 'a')
        self.tell('2 new in yardr', 'mail from a, sam asks')
        self.assertEqual(self.texts()[3:], [self.texts()[3], '2 new in yardr\nmail from a, sam asks'])
        self.assertTrue(self.texts()[3].endswith('third from a'))

    def test_a_failed_mail_keeps_the_notice_though_an_older_one_went(self):
        self.yard.mail('m1', 'a')
        self.tell('mail from a', 'm1')
        self.assertEqual(len(self.sent), 1)
        for name, action in (('rejected', rejected(400)), ('ambiguous', unconfirmed)):
            with self.subTest(name):
                mail = self.yard.mail('m2 ' + name, 'a')
                self.plan = [action]
                self.tell('mail from a', mail['text'])
                self.assertEqual(self.sent[-1]['text'], 'mail from a\n' + mail['text'])
                count = len(self.sent)
                # Neither is sent again; the notice goes again while it is not whole.
                self.tell('mail from a', 'again ' + name)
                self.assertEqual(len(self.sent), count + 1)
                self.assertEqual(self.sent[-1]['text'], 'mail from a\nagain ' + name)
        self.assertTrue(any('ended: telegram_rejected_not_sent_400' in line for line in self.logs))

    def test_read_old_and_pre_watermark_mail_is_not_sent(self):
        self.yard.mail('read already', read=True)
        self.yard.mail('seven hours old', at=now() - datetime.timedelta(hours=7))
        before = self.yard.mail('before the watermark', at=now() - datetime.timedelta(minutes=5))
        self.bridge.yard_mail.since = ym.letter_at(before['at']) + datetime.timedelta(seconds=1)
        self.tell('mail from sam', 'x')
        self.assertEqual(self.texts(), ['mail from sam\nx'])
        # The watermark is kept across restarts: the first start's.
        first = self.bridge.yard_mail.since
        self.make_bridge()
        with self.store.connect() as db:
            saved = ym.letter_at(db.execute('SELECT at FROM yard_mail_since').fetchone()[0])
        self.assertLess(saved, now())
        self.assertNotEqual(saved, first)

    def test_lettered_options_arrive_whole_without_a_keyboard(self):
        self.yard.mail('Pick one:\n\nA: install now\nB: later')
        self.assertTrue(self.tell('mail from sam', 'Pick')['delivered'])
        self.assertTrue(self.texts()[0].endswith('Pick one:\n\nA: install now\nB: later'))
        self.assertNotIn('reply_markup', self.sent[0])

    def scans(self):
        with self.store.connect() as db:
            return {sender: (top, low) for sender, top, low in db.execute('SELECT sender, top, low FROM yard_mail_scan')}

    def slow_pages(self, seconds):
        """Each Conversation page costs seconds of the tell's read time."""
        def on_call(method):
            if method == 'Conversation':
                self.clock[0] += seconds
        self.yard.on_call = on_call

    def test_pages_to_the_window_start_and_a_scan_out_of_time_resumes_below_until_all_went_whole(self):
        mails = [self.yard.mail(f'note {i}') for i in range(7)]
        to_sam = self.yard.mail('to sam', 'pat', to='sam')
        with patch.object(ym, 'PAGE', 3):
            self.tell('7 new in your yardr inbox', 'mail from sam (7)')
        self.assertEqual([t.split('\n')[1] for t in self.texts()], [m['text'] for m in mails])
        self.assertEqual([a['before'] for m, a in self.yard.calls if m == 'Conversation'],
                         [0, mails[5]['seq'], mails[2]['seq']])
        self.assertEqual(self.scans(), {'sam': (to_sam['seq'], 0)})
        # Two pages a tell: one answers 503 until the scan reaches the start, and goes on below what it read.
        self.sent.clear()
        with self.store.connect() as db:
            for table in ('deliveries', 'yard_mail_scan', 'yard_mail_found'):
                db.execute(f'DELETE FROM {table}')
            db.execute("INSERT INTO yard_mail_scan VALUES ('gone', 9, 8, 0)")
        self.slow_pages(2)
        tell = ('8 new in your yardr inbox', 'mail from sam (8)')
        with patch.object(ym, 'PAGE', 3):
            with self.assertRaises(b.ApiUnavailable):
                self.tell(*tell)
            self.assertEqual(self.scans(), {'sam': (to_sam['seq'], mails[2]['seq'])})
            late = self.yard.mail('note 7')
            # Out of read time: nothing read, nothing sent, the scan kept.
            with patch.object(ym, 'READ_SECONDS', 0), self.assertRaises(b.ApiUnavailable):
                self.tell(*tell)
            self.assertEqual(self.scans(), {'sam': (to_sam['seq'], mails[2]['seq'])})
            self.yard.calls.clear()
            self.assertTrue(self.tell(*tell)['delivered'])
            self.assertEqual([(a['after'], a['before']) for m, a in self.yard.calls if m == 'Conversation'],
                             [(0, mails[2]['seq']), (to_sam['seq'], 0)])
        # Every mail went whole over the three tells: the notice is left out.
        self.assertEqual([t.split('\n', 1)[1] for t in self.texts()],
                         ['note 2', 'note 3', 'note 4', 'note 5', 'note 6', 'note 0', 'note 1', 'note 7'])
        self.assertEqual(self.scans(), {'sam': (late['seq'], 0)})
        self.assertTrue(any('sam not read to the window start yet' in line for line in self.logs))
        # Senders the read time does not reach are read first on the next tell; one read through the
        # inbox's newest unread is not read again until newer mail comes, and the notice is left out.
        self.sent.clear()
        self.slow_pages(1.1)
        for sender in 'abcd':
            self.yard.mail('body ' + sender, sender)
        tell = ('4 new in your yardr inbox', 'mail from a, mail from b, mail from c, mail from d')
        answers = []
        for _ in range(3):
            self.yard.calls.clear()
            try:
                answers.append((self.tell(*tell)['delivered'], [a['b'] for m, a in self.yard.calls if m == 'Conversation']))
            except b.ApiUnavailable:
                answers.append((503, [a['b'] for m, a in self.yard.calls if m == 'Conversation']))
        self.assertEqual(answers, [(503, ['a', 'b', 'c']), (True, ['d', 'sam']), (True, [])])
        self.assertEqual([t.split('\n', 1)[1] for t in self.texts()], ['body a', 'body b', 'body c', 'body d'])
        # New mail from b: every sender is read again for newer mail, least lately read first.
        self.yard.mail('more from b', 'b')
        self.yard.calls.clear()
        with self.assertRaises(b.ApiUnavailable):
            self.tell('mail from b', 'more')
        self.assertEqual([a['b'] for m, a in self.yard.calls if m == 'Conversation'], ['sam', 'a', 'b'])
        self.yard.calls.clear()
        self.assertTrue(self.tell('mail from b', 'more')['delivered'])
        self.assertEqual([(a['b'], a['after']) for m, a in self.yard.calls if m == 'Conversation'],
                         [('c', self.yard.seq - 2), ('d', self.yard.seq - 1)])
        self.assertEqual(self.texts()[4:], [self.texts()[4]])
        self.assertTrue(self.texts()[4].endswith('more from b'))

    def test_a_scan_not_done_keeps_what_it_read_only_once_its_mail_is_final(self):
        mails = [self.yard.mail(f'note {i}') for i in range(5)]
        seqs = [m['seq'] for m in mails]
        # One page a tell, an empty one too: each still gets further.
        self.slow_pages(3)
        self.plan = [not_sent, not_sent, not_sent]
        with patch.object(ym, 'PAGE', 2):
            with self.assertRaises(b.ApiUnavailable):
                self.tell('mail from sam', 'x')
            self.assertEqual((self.sent, self.scans()), ([], {}))
            # note 3 now ends on a refusal: final, so the range is kept and the scan goes on below it.
            self.plan = [rejected(400)]
            scans = []
            for _ in range(3):
                with self.assertRaises(b.ApiUnavailable):
                    self.tell('mail from sam', 'x')
                scans.append(self.scans()['sam'])
            self.assertEqual(scans, [(seqs[4], seqs[3]), (seqs[4], seqs[1]), (seqs[4], 0)])
            # The last tell reads only the new note 5, whole, but note 3 never arrived: the notice goes.
            self.yard.mail('note 5')
            self.assertTrue(self.tell('mail from sam', 'x')['delivered'])
        self.assertEqual([t.split('\n', 1)[1] for t in self.texts()],
                         ['note 4', 'note 1', 'note 2', 'note 0', 'note 5', 'x'])
        self.assertIn(f"yardmail yardmail:{seqs[3]}:0 ended: telegram_rejected_not_sent_400", self.logs)
        self.assertEqual(self.scans(), {'sam': (seqs[4] + 1, 0)})

    def test_the_same_tell_twice_sends_nothing_twice(self):
        self.two_part_mail()
        self.assertEqual(self.tell('mail from sam', 'start')['message_id'], '1002')
        self.assertEqual(len(self.sent), 2)
        # Final parts are read from the ledger, not published again: the answer names no part sent now.
        self.assertEqual(self.tell('mail from sam', 'start'), dict(delivered=True, conversation='123'))
        self.assertEqual(len(self.sent), 2)
        with self.store.connect() as db:
            logged = db.execute('SELECT count(*) FROM messages').fetchone()[0]
        self.assertEqual(logged, 2)

    def test_a_delivered_first_part_is_not_sent_again(self):
        mail = self.two_part_mail()
        bodies = self.seed(mail, 0, self.bridge.receipt(True, message_id='5'))
        self.tell('mail from sam', 'start')
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0]['text'], ym.part_text(mail, TZ, 1, 2, bodies[1]))

    def test_an_ambiguous_full_first_part_is_never_resent_and_the_next_says_so(self):
        head = visible(ym.header(letter(101, ''), TZ, 0, ym.MAX_PARTS, True))
        room = 4096 - b.utf16_units(head) - 1
        first = ('word ' * 1000)[:room - 1] + 'z'
        mail = self.yard.mail(first + '\n\nsecond part')
        clean = ym.render(mail, TZ, b.html_visible_text)
        self.assertEqual(clean, [first, 'second part'])
        self.assertEqual(b.utf16_units(head) + 1 + b.utf16_units(clean[0]), 4096)
        self.seed(mail, 0, self.bridge.receipt(reason=ym.AMBIGUOUS))
        self.assertEqual(self.tell('mail from sam', 'x')['delivered'], True)
        [part, notice] = self.sent
        self.assertRegex(visible(part['text']), r'^mail from sam · \d\d:\d\d · 2/2 · an earlier part is unconfirmed\nsecond part$')
        self.assertEqual(notice['text'], 'mail from sam\nx')
        self.assertLessEqual(b.utf16_units(visible(part['text'])), 4096)

    def test_a_part_not_sent_answers_503_and_the_retry_sends_only_it(self):
        mail = self.two_part_mail()
        self.plan = [None, not_sent, not_sent, not_sent]
        server = testports.bind_test_server(b._callback_handler(self.bridge))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

        def post():
            connection = http.client.HTTPConnection('127.0.0.1', server.server_address[1], timeout=10)
            connection.request('POST', '/v1/tell', json.dumps(dict(title='mail from sam', text='x')),
                               {'Authorization': 'Bearer CAPABILITY', 'Content-Type': 'application/json'})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        self.assertEqual(post(), (503, dict(error='tell unfinished; retry')))
        self.assertEqual(len(self.sent), 1)
        self.assertTrue(b.resendable(self.store.saved_receipt(f"yardmail:{mail['seq']}:1")))
        status, answer = post()
        self.assertEqual((status, answer['delivered']), (200, True))
        self.assertEqual(len(self.sent), 2)
        self.assertEqual(self.sent[1]['text'], ym.part_text(mail, TZ, 1, 2, ym.render(mail, TZ, b.html_visible_text)[1]))

    def test_telegram_429_on_a_part_is_resent_on_the_retry_only_for_mail(self):
        mail = self.two_part_mail()
        self.plan = [None, rejected(429, 'Too Many Requests: retry after 3')]
        with self.assertRaises(b.ApiUnavailable):
            self.tell('mail from sam', 'x')
        self.assertTrue(b.resendable(self.store.saved_receipt(f"yardmail:{mail['seq']}:1")))
        self.assertIn('yardmail part rate-limited', self.logs)
        self.tell('mail from sam', 'x')
        self.assertEqual(len(self.sent), 2)
        self.assertEqual(sum(1 for line in self.logs if 'rate-limited' in line), 1)
        # Every other send keeps the 429 as its final answer.
        self.plan = [rejected(429, 'Too Many Requests: retry after 3')]
        answer = self.bridge.api_send(dict(text='plain', key='other'))
        self.assertEqual((answer['delivered'], answer['reason']), (False, 'telegram_rejected_not_sent_429'))
        self.assertFalse(b.resendable(self.store.saved_receipt('other')))

    def test_mail_keys_are_the_bridges_own(self):
        for call in (lambda: self.bridge.api_send(dict(text='x', key='yardmail:1:0')),
                     lambda: self.bridge.api_send(dict(text='x'), key_header='yardmail:1:0'),
                     lambda: self.bridge.api_tell(dict(title='t', key='yardmail:1:0'))):
            with self.assertRaisesRegex(b.ApiError, 'yardmail:'):
                call()
        root = Path(self.tmp.name) / 'cli'
        root.mkdir()
        settings = write_settings(root)
        (root / 'body.txt').write_text('x')
        with self.assertRaises(SystemExit), patch('sys.stderr', new_callable=io.StringIO) as err:
            run_main(self, 'bridge.py', '--settings', str(settings), 'send', '--body-file', str(root / 'body.txt'),
                     '--idempotency-key', 'yardmail:9:0')
        self.assertIn('yardmail:', err.getvalue())
        # A row already under a mail key with another request: that mail ends, nothing under another key.
        mail = self.yard.mail('held')
        key = f"yardmail:{mail['seq']}:0"
        self.store.reserve(key, '0' * 64, self.bridge.receipt(True, message_id='9'))
        self.tell('mail from sam', 'held')
        self.assertEqual(self.texts(), ['mail from sam\nheld'])
        self.assertIn(f'yardmail {key} ended: idempotency_key_conflict', self.logs)

    def test_two_tells_at_once_send_each_part_once_in_order(self):
        self.make_bridge(time.monotonic)
        mails = [self.yard.mail(f'mail {i}') for i in range(4)] + [self.two_part_mail()]
        slow = threading.Event()

        def telegram(url, body, timeout=20):
            slow.wait(.02)
            return self.telegram(url, body, timeout)
        self.bridge.request = telegram
        threads = [threading.Thread(target=self.tell, args=('5 new in your yardr inbox', 'mail from sam (5)'))
                   for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(self.sent), 6)
        self.assertEqual([t.split('\n')[1] for t in self.texts()[:4]], [m['text'] for m in mails[:4]])
        self.assertEqual([re.search(r' · (\d)/2\n', t)[1] for t in self.texts()[4:]], ['1', '2'])

    def test_no_part_starts_after_seven_seconds_and_the_retry_continues(self):
        mails = [self.yard.mail(f'm{i}') for i in range(30)]
        self.tick = 1
        with self.assertRaises(b.ApiUnavailable):
            self.tell('30 new in your yardr inbox', 'mail from sam (30)')
        self.assertEqual(self.starts, [0, 1, 2, 3, 4, 5, 6])
        self.assertEqual([t.split('\n')[1] for t in self.texts()], [m['text'] for m in mails[:7]])
        with self.assertRaises(b.ApiUnavailable):
            self.tell('30 new in your yardr inbox', 'mail from sam (30)')
        self.assertEqual([t.split('\n')[1] for t in self.texts()], [m['text'] for m in mails[:14]])
        # A part that waits on another send past the time is checked again once it holds the lock: not sent.
        self.tick, started = 0, self.clock[0]
        publish = self.bridge.publish
        waiting = threading.Event()

        def queued(request, **options):
            waiting.set()
            return publish(request, **options)
        errors = []

        def tell():
            try:
                self.tell('30 new in your yardr inbox', 'mail from sam (30)')
            except b.ApiUnavailable as error:
                errors.append(error)
        worker = threading.Thread(target=tell)
        with patch.object(self.bridge, 'publish', side_effect=queued):
            with self.bridge.publish_lock:
                worker.start()
                self.assertTrue(waiting.wait(5))
                self.clock[0] = started + 11
            worker.join(5)
        self.assertEqual((len(errors), len(self.sent)), (1, 14))
        self.assertTrue(b.resendable(self.store.saved_receipt(f"yardmail:{mails[14]['seq']}:0")))
        self.assertIn('yardmail part not started: its time had passed', self.logs)

    def test_a_mail_that_cannot_be_rendered_is_not_answered_as_told(self):
        self.two_part_mail()
        with patch.object(ym, 'MAX_PARTS', 1), self.assertRaises(b.ApiUnavailable):
            self.tell('mail from sam', 'start')
        self.assertEqual(self.sent, [])
        self.assertTrue(any('yardmail_unrenderable' in line for line in self.logs))
        self.assertTrue(self.tell('mail from sam', 'start')['delivered'])
        self.assertEqual(len(self.sent), 2)

    def test_an_answer_the_caller_left_is_logged_once_and_the_tell_resumes(self):
        mail = self.two_part_mail()
        answers, errors = [], []

        class Gone(b._callback_handler(self.bridge)):
            def _answer(handler, status, value):
                answers.append(status)
                super()._answer(status, value)

            def do_POST(handler):
                class Closed(io.BufferedIOBase):
                    def write(self, data):
                        raise BrokenPipeError(32, 'Broken pipe')
                handler.wfile = Closed()
                super().do_POST()
        server = testports.bind_test_server(Gone)
        server.handle_error = lambda *args: errors.append(args)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.plan = [None, not_sent, not_sent, not_sent]
        connection = http.client.HTTPConnection('127.0.0.1', server.server_address[1], timeout=10)
        connection.request('POST', '/v1/tell', json.dumps(dict(title='mail from sam', text='x')),
                           {'Authorization': 'Bearer CAPABILITY', 'Content-Type': 'application/json'})
        with self.assertRaises((http.client.RemoteDisconnected, ConnectionError)):
            connection.getresponse()
        self.assertEqual((answers, errors), ([503], []))
        self.assertEqual(self.logs.count('yard api answer not written'), 1)
        self.assertTrue(self.store.saved_receipt(f"yardmail:{mail['seq']}:0")['delivered'])
        self.tell('mail from sam', 'x')
        self.assertEqual(len(self.sent), 2)

    def test_parse_rejection_sends_the_plain_form_with_the_header_once(self):
        self.yard.mail('**bold** <tag> & `code`')
        self.plan = [rejected(400, "Bad Request: can't parse entities: unsupported start tag")]
        self.tell('mail from sam', 'x')
        [plain] = self.sent
        self.assertNotIn('parse_mode', plain)
        self.assertRegex(plain['text'], r'^mail from sam · \d\d:\d\d\nbold <tag> & code$')

    def test_guards_fall_back_to_the_notice(self):
        self.yard.mail('hello')
        self.yard.user['notify'] = 'mailer'
        self.tell('mail from sam', 'hello')
        self.assertEqual(self.texts(), ['mail from sam\nhello'])
        self.assertEqual([m for m, _ in self.yard.calls], ['GetUser'])
        self.yard.user.update(notify='tgbridge', role='member')
        self.tell('mail from sam', 'hello again')
        self.assertEqual(self.texts()[1], 'mail from sam\nhello again\n(whole mail not read: operator not verified)')
        # Without [yardr.mail] nothing is read.
        del self.config['yardr_mail']
        self.make_bridge()
        self.yard.calls.clear()
        self.tell('mail from sam', 'plain')
        self.assertEqual((self.texts()[2], self.yard.calls), ('mail from sam\nplain', []))

    def test_a_yard_that_does_not_answer_answers_503_and_the_retry_sends_the_mail_whole(self):
        mail = self.two_part_mail()
        self.yard.unavailable.add('Conversation')
        with self.assertRaises(b.ApiUnavailable):
            self.tell('mail from sam', 'start')
        self.assertEqual(self.sent, [])
        self.assertIn('yardmail not read: yard unavailable; retry', self.logs)
        # Core's retry of the same tell, once the yard answers: the mail whole, no other mail needed.
        self.yard.unavailable.clear()
        self.assertTrue(self.tell('mail from sam', 'start')['delivered'])
        self.assertEqual([p for t in self.sent for p in pres(t['text'])], [CMD1, CMD2])
        self.assertEqual(len(self.sent), 2)
        self.assertEqual(sum(b.utf16_units(visible(t['text']).split('\n', 1)[1]) for t in self.sent) + 2,
                         b.utf16_units(visible('\n\n'.join(ym.render(mail, TZ, b.html_visible_text)))))
        # A refusal is not retried: the notice goes, saying why.
        self.yard.refused.add('Conversation')
        self.yard.mail('refused')
        self.tell('mail from sam', 'refused')
        self.assertEqual(self.sent[-1]['text'], 'mail from sam\nrefused\n(whole mail not read: yard refused the read)')

    def test_a_yard_that_never_answers_answers_503_in_about_three_seconds(self):
        self.make_bridge(time.monotonic)
        self.yard.mail('hello')
        self.yard.hang = True
        started = time.monotonic()
        with self.assertRaises(b.ApiUnavailable):
            self.tell('mail from sam', 'hello')
        self.assertLess(time.monotonic() - started, 3.5)
        self.assertEqual(self.sent, [])
        self.yard.hang = False
        self.tell('mail from sam', 'hello')
        self.assertEqual(self.texts()[0].split('\n', 1)[1], 'hello')

    def test_a_command_nested_in_a_list_or_a_quote_arrives_exact(self):
        heredoc = "cat <<'EOF'\nvalue\n\tEOF2\nEOF\n"
        self.yard.mail('1. Run:\n\n   ```sh\n' + '\n'.join('   ' + line for line in heredoc.split('\n')[:-1])
                       + '\n   ```\n2. done\n\n> ```\n' + ''.join('> ' + line + '\n' for line in heredoc.split('\n')[:-1])
                       + '> ```')
        self.assertTrue(self.tell('mail from sam', 'Run')['delivered'])
        self.assertEqual(pres(self.sent[0]['text']), [heredoc, heredoc])
        # A list or a quote larger than a part is cut around its code: the code stays one exact <pre>.
        code = "cat <<'EOF'\nvalue\nEOF\n"
        for name, first, rest in (('list', '1. ', '   '), ('quote', '> ', '> ')):
            with self.subTest(name):
                self.sent.clear()
                head = (first + 'Run:\n' + rest + '\n' + rest + '```sh\n' + ''.join(rest + line + '\n' for line in code.splitlines())
                        + rest + '```\n' + rest + '\n' + rest)
                words = [f'w{i:04d}' for i in range((4096 - len(head.encode())) // 6)]
                text = head + ' '.join(words)
                self.assertLessEqual(len(text.encode()), 4096)
                self.yard.mail(text)
                self.assertTrue(self.tell('mail from sam', 'Run')['delivered'])
                self.assertGreaterEqual(len(self.sent), 2)
                parts = [t['text'] for t in self.sent]
                self.assertEqual([p for part in parts for p in pres(part)], [code])
                self.assertFalse(any('```' in part or '   EOF' in part for part in parts))
                shown = ' '.join(visible(part) for part in parts)
                self.assertEqual(re.findall(r'w\d{4}', shown), words)
                self.assertLess(shown.index('Run:'), shown.index('value'))
                self.assertTrue(all(b.utf16_units(visible(part)) <= 4096 for part in parts))
        # The text piece of an oversized quote also needs its reference context when cut again.
        destination = 'https://example.invalid/' + 'q' * 90
        text = ('> ' + ' '.join(['[label][ref]'] * 120) + ' ' + 'x' * 110
                + '\n>\n> ```sh\n> echo exact\n> ```\n\n[ref]: ' + destination)
        self.assertLessEqual(len(text.encode()), 4096)
        self.sent.clear()
        self.yard.mail(text)
        self.assertTrue(self.tell('mail from sam', 'Run')['delivered'])
        parts = [t['text'] for t in self.sent]
        shown = ''.join(visible(part).split('\n', 1)[1] for part in parts)
        self.assertEqual(shown.count('label (' + destination + ')'), 120)
        self.assertIn('x' * 110, shown)
        self.assertEqual([p for part in parts for p in pres(part)], ['echo exact\n'])
        self.assertTrue(all(b.utf16_units(visible(part)) <= 4096 and b.utf16_units(part) <= 16384 for part in parts))

class MailSettingsTests(unittest.TestCase):
    def test_mail_table_needs_native_and_takes_no_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / 'yard'
            home.mkdir(mode=0o700)
            path = write_settings(root)
            base = path.read_text()
            native = base.replace('[yardr]\n', f'[yardr]\nhome = "{home}"\noperator = "pat"\n')
            for text, error in ((base + '\n[yardr.mail]\n', 'needs \\[yardr\\] home and operator'),
                                (native + '\n[yardr.mail]\nseq_floor = 1\n', 'unknown key yardr.mail.seq_floor'),
                                (native.replace('[yardr]\n', '[yardr]\nmail = 1\n'), 'must be a table')):
                path.write_text(text)
                with self.assertRaisesRegex(b.SettingsError, error):
                    b.load_settings(path)
            path.write_text(native + '\n[yardr.mail]\n')
            self.assertIs(b.load_settings(path).config['yardr_mail'], True)
            path.write_text(native)
            self.assertNotIn('yardr_mail', b.load_settings(path).config)


DECLARATION = '''name: tgbridge
base: http://127.0.0.1:9
calls:
  health:
    kind: read
    request: {method: GET, path: /v1/health}
  tell:
    kind: update
    params:
      conversation: { type: string }
      title: { type: string, max: 250 }
      text: { type: string, max: 3500 }
    request:
      method: POST
      path: /v1/tell
      body: { conversation: "{conversation}", title: "{title}", text: "{text}" }
check: health
notify:
  call: tell
  address: { param: conversation, env: TGBRIDGE_CONVERSATION }
  title: title
  text: text
hook:
  bodies: unique
  verify:
    scheme: secret
    signature: {header: X-Secret}
  normalize: [/bin/false]
'''


class RealCoreMailTests(BridgeTestCase):
    def test_real_core_mail_whole_batched_options_acked_and_paged(self):
        binary = testyardr.binary()
        if binary is None:
            self.skipTest('yardr required for the isolated mail reads')
        with tempfile.TemporaryDirectory(prefix='ymr-', dir='/tmp') as tmp:
            root = Path(tmp)
            server, home, command, env = testyardr.start(root, binary)

            def cli(*args, stdin=None):
                result = subprocess.run(command + list(args), env=env, cwd=root, input=stdin,
                                        text=True, capture_output=True, timeout=60)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                return result.stdout

            def mail(sender, text):
                cli('--actor', sender, 'mail', 'pat', '-', stdin=text)

            try:
                cli('init', '--harness', 'claude')
                cli('settings', 'set', 'operator', 'pat')
                (root / 'tgbridge.yaml').write_text(DECLARATION)
                cli('source', 'add', str(root / 'tgbridge.yaml'))
                cli('user', 'identity', 'pat', '--source', 'tgbridge', '--id', '123')
                cli('user', 'set', 'pat', 'notify=tgbridge')
                cli('user', 'add', 'sam')
                cli('user', 'add', 'kim')
                self.config.update(yardr_home=str(home), yardr_operator='pat', yardr_source='tgbridge', yardr_mail=True)
                sent, plan = [], []

                def telegram(url, body, timeout=20):
                    if url.endswith('/sendMessage'):
                        action = plan.pop(0) if plan else None
                        if action is not None:
                            return action(body)
                        sent.append(body)
                        return 200, dict(ok=True, result=dict(message_id=500 + len(sent), chat=dict(id=123)))
                    return self.transport(url, body, timeout)
                self.bridge = b.Bridge(self.config, self.store, 'TOKEN', 'CAPABILITY', request=telegram)
                self.bridge.yard_mail.since = OLD

                # N1: one 4 KB mail, two fenced commands: two parts, whole, in order, exact.
                text = four_kb_mail()
                mail('sam', text)
                self.assertTrue(self.bridge.api_tell(dict(title='mail from sam', text=text[:199] + '…'))['delivered'])
                self.assertEqual(len(sent), 2)
                self.assertTrue(all('message_thread_id' not in body and body['parse_mode'] == 'HTML' for body in sent))
                self.assertTrue(all(b.utf16_units(visible(body['text'])) <= 4096 for body in sent))
                self.assertEqual([p for body in sent for p in pres(body['text'])], [CMD1, CMD2])
                words = re.findall(r'w\d{4}', ' '.join(visible(body['text']) for body in sent))
                self.assertEqual(words, [f'w{i:04d}' for i in range(len(words))])
                print('N1 parts:', *(json.dumps(body['text'][:160]) + ' … ' + json.dumps(body['text'][-120:])
                                    for body in sent), sep='\n  ')

                # N2: three mails from two senders, batched by core: three messages, no notice.
                for sender, words in (('sam', 'one'), ('kim', 'two'), ('sam', 'three')):
                    mail(sender, words)
                self.bridge.api_tell(dict(title='3 new in your yardr inbox', text='mail from sam (2), mail from kim'))
                self.assertEqual([visible(body['text']).split('\n')[1] for body in sent[2:]], ['one', 'two', 'three'])
                self.assertRegex(visible(sent[3]['text']), r'^mail from kim · \d\d:\d\d\ntwo$')

                # N4: lettered options; L1: the same tell again sends nothing.
                mail('kim', 'Choose:\nA: install now\nB: later')
                self.bridge.api_tell(dict(title='mail from kim', text='Choose:'))
                self.bridge.api_tell(dict(title='mail from kim', text='Choose:'))
                self.assertEqual(len(sent), 6)
                self.assertTrue(visible(sent[5]['text']).endswith('A: install now\nB: later'))

                # N3: acknowledged mail and mail before the watermark are not sent.
                mail('sam', 'acked')
                seq = self.bridge.yard_mail.client.call('UserInbox', {'name': 'pat', 'limit': 1, 'actor': 'pat'})['last']
                cli('--actor', 'pat', 'inbox', '--mail', '--ack', str(seq))
                mail('kim', 'before the watermark')
                self.bridge.yard_mail.since = now()
                self.bridge.api_tell(dict(title='mail from kim', text='before'))
                self.assertEqual(sent[6]['text'], 'mail from kim\nbefore')

                # N5: paged from the newest end, three to a page.
                self.bridge.yard_mail.since = now()
                for i in range(7):
                    mail('sam', f'page note {i}')
                with patch.object(ym, 'PAGE', 3):
                    self.bridge.api_tell(dict(title='7 new in your yardr inbox', text='mail from sam (7)'))
                self.assertEqual([visible(body['text']).split('\n')[1] for body in sent[7:]],
                                 [f'page note {i}' for i in range(7)])

                # A small source can render over the part budget: every resolved reference must survive fallback.
                destination = 'https://example.invalid/' + 'q' * 90
                text = ' '.join(['[label][ref]'] * 121) + '\n\n[ref]: ' + destination
                self.assertEqual(len(text.encode()), 1695)
                mail('sam', text)
                inbox = self.bridge.yard_mail.client.call('UserInbox', {'name': 'pat', 'limit': 200, 'actor': 'pat'})
                seq = inbox['last']
                self.assertEqual(next(row['text'] for row in inbox['mail'] if row['seq'] == seq), text)
                start = len(sent)
                plan[:] = [None, not_sent, not_sent, not_sent]
                with self.assertRaises(b.ApiUnavailable):
                    self.bridge.api_tell(dict(title='mail from sam', text=text[:199] + '…'))
                self.assertEqual(len(sent) - start, 1)
                self.assertTrue(self.store.saved_receipt(f'yardmail:{seq}:0')['delivered'])
                self.assertTrue(b.resendable(self.store.saved_receipt(f'yardmail:{seq}:1')))
                with self.store.connect() as db:
                    self.assertIsNone(db.execute('SELECT whole FROM yard_mail_found WHERE seq=?', (seq,)).fetchone())
                self.assertTrue(self.bridge.api_tell(dict(title='mail from sam', text=text[:199] + '…'))['delivered'])
                parts = sent[start:]
                self.assertGreater(len(parts), 1)
                bodies = [visible(body['text']).split('\n', 1)[1] for body in parts]
                expected = ' '.join(['label (' + destination + ')'] * 121)
                self.assertEqual(' '.join(''.join(bodies).split()), expected)
                for index, body in enumerate(parts):
                    self.assertEqual(body['parse_mode'], 'HTML')
                    self.assertLessEqual(b.utf16_units(visible(body['text'])), 4096)
                    self.assertLessEqual(b.utf16_units(body['text']), 16384)
                    self.assertTrue(self.store.saved_receipt(f'yardmail:{seq}:{index}')['delivered'])
                with self.store.connect() as db:
                    self.assertEqual(db.execute('SELECT whole FROM yard_mail_found WHERE seq=?', (seq,)).fetchone()[0], 1)
                count = len(sent)
                self.bridge.api_tell(dict(title='mail from sam', text=text[:199] + '…'))
                self.assertEqual(len(sent), count)
                print('Real core', Path(binary).name, 'mail reads: whole, batched, options, acked, watermark, paged, references')
            finally:
                testyardr.stop(server)


if __name__ == '__main__':
    unittest.main()
