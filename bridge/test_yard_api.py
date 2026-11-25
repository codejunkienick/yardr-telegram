"""The yardr side of the bridge (YARDR.md): the inbound body posted to the
yard's hook, the message log behind /v1/read, the yard API on the loopback
port, auto_format and valid_hook_url. Fake transports only, except one test
driving a real isolated core; the API runs on a real loopback server picked
by testports."""
import datetime
import http.client
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bridge as b  # noqa: E402
import testyardr  # noqa: E402
from test_bridge import BridgeTestCase  # noqa: E402

HOOK_URL = 'http://127.0.0.1:8797/hooks/tgbridge'
OPS = 12


class InboundBodyTests(BridgeTestCase):
    """One flat JSON body per relayed message, to [yardr] hook_url, with the
    hook secret in X-Tgbridge-Secret."""

    def setUp(self):
        super().setUp()
        self.posts = []

    def hook(self, answer=(200, dict(seq=5))):
        def transport(url, body, *args, **kwargs):
            if url == self.bridge.hook_url:
                self.posts.append(dict(url=url, body=body, args=args, kwargs=kwargs))
                return answer
            return self.transport(url, body, *args, **kwargs)
        self.bridge.request = transport

    def test_a_message_posts_the_flat_body_with_the_secret_header(self):
        self.hook()
        update = self.update(uid=10)
        update['message']['reply_to_message'] = dict(message_id=7)
        before = datetime.datetime.now(datetime.timezone.utc)
        self.bridge.drain([update])
        [post] = self.posts
        self.assertEqual(post['url'], HOOK_URL)
        self.assertEqual(post['kwargs']['headers'], {'X-Tgbridge-Secret': 'HOOKSECRET'})
        self.assertEqual(post['kwargs']['timeout'], b.YARD_HOOK_TIMEOUT)
        self.assertNotIn('method', post['kwargs'])  # a POST, http_json's default
        body = dict(post['body'])
        received_at = datetime.datetime.fromisoformat(body.pop('received_at'))
        self.assertEqual(received_at.utcoffset(), datetime.timedelta(0))
        self.assertLessEqual(before, received_at)
        self.assertEqual(body, {
            'id': '999:123:10', 'kind': 'message', 'chat': '123', 'conversation': '123', 'topic': 'general',
            'thread_id': None, 'message_id': '10', 'reply_to': '7', 'from': {'id': '123', 'name': 'Pat'},
            'text': 'hello', 'forwarded': False,
        })
        self.assertEqual(self.store.offset(), 11)

    def test_no_reply_is_null_and_a_forward_says_so(self):
        self.hook()
        update = self.update(uid=10)
        update['message']['forward_origin'] = dict(type='hidden_user', sender_user_name='Sam', date=1792677900)
        self.bridge.drain([update])
        [post] = self.posts
        self.assertIsNone(post['body']['reply_to'])
        self.assertIs(post['body']['forwarded'], True)
        self.assertTrue(post['body']['text'].startswith('[forwarded message | from: Sam | '))

    def test_a_configured_topic_names_its_topic_thread_and_conversation(self):
        self.hook()
        self.bridge.topic_settings = (dict(name='ops', agent='yardmaster'),)
        self.store.record_topic('ops', OPS, 'created')
        update = self.update(uid=10)
        update['message'].update(is_topic_message=True, message_thread_id=OPS)
        self.bridge.drain([update])
        [post] = self.posts
        body = post['body']
        self.assertEqual((body['id'], body['conversation'], body['topic'], body['thread_id'], body['chat']),
                         ('999:123:topic:12:10', '123:topic:12', 'ops', OPS, '123'))
        self.assertEqual(body['text'], '[topic: ops]\nhello')

    def test_without_a_hook_url_or_secret_nothing_is_posted_and_the_cursor_stays(self):
        for missing in ('hook_url', 'hook_secret'):
            with self.subTest(missing=missing):
                config = {k: v for k, v in self.config.items() if k != missing}
                bridge = b.Bridge(config, self.store, 'SECRET', 'CAPABILITY', request=self.transport)
                with self.assertRaisesRegex(b.TransportError, r'\[yardr\] hook_url and hook_secret_file'):
                    bridge.drain([self.update(uid=10)])
                self.assertEqual(self.calls, [])
                self.assertEqual(self.store.offset(), 10)

    def test_a_yard_answer_the_bridge_does_not_accept_logs_nothing(self):
        self.hook(answer=(200, dict(dropped=True)))
        with self.assertRaisesRegex(b.TransportError, 'dropped'):
            self.bridge.drain([self.update(uid=10)])
        self.assertEqual(self.store.read_messages('123', 50), [])

    def test_the_whole_text_is_logged_once_the_yard_has_it_and_a_retry_logs_it_once(self):
        long_text = 'x' * 3000  # the yard keeps 500 characters; the log keeps them all
        update = self.update(uid=10)
        update['message']['text'] = long_text
        answers = [b.RequestUnconfirmed('lost'), (200, dict(seq=9)), (200, dict(seq=9, duplicate=True))]

        def transport(url, body, *args, **kwargs):
            self.posts.append(body)
            answer = answers.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return answer
        self.bridge.request = transport
        with self.assertRaises(b.TransportError):
            self.bridge.drain([update])
        self.assertEqual(self.store.read_messages('123', 50), [])
        self.bridge.drain([update])
        with self.store.connect() as db:  # replayed after the accept, as from an older cursor
            db.execute('UPDATE cursor SET offset=10 WHERE id=1')
        self.bridge.drain([update])
        self.assertEqual(len(self.posts), 3)
        self.assertEqual(len({body['id'] for body in self.posts}), 1)
        [logged] = self.store.read_messages('123', 50)
        self.assertEqual(logged, dict(id='10', direction='in', **{'from': 'Pat'}, text=long_text,
                                      at=self.posts[1]['received_at']))


class MessageLogTests(BridgeTestCase):

    def test_one_row_per_message_read_oldest_first_and_per_conversation(self):
        log = self.store.log_message
        log('123', 'in', '1', 'Pat', 'first', '2026-11-06T10:00:00+00:00')
        log('123:topic:12', 'in', '2', 'Pat', 'in ops', '2026-11-06T10:00:01+00:00')
        log('123', 'out', '3', 'test_bot', 'second', '2026-11-06T10:00:02+00:00')
        log('123', 'in', '1', 'Pat', 'replayed', '2026-11-06T10:00:03+00:00')  # ignored: already logged
        log('123', 'out', '1', 'test_bot', 'same id, other direction', '2026-11-06T10:00:04+00:00')
        self.assertEqual([(m['id'], m['direction'], m['text']) for m in self.store.read_messages('123', 50)],
                         [('1', 'in', 'first'), ('3', 'out', 'second'), ('1', 'out', 'same id, other direction')])
        self.assertEqual([m['text'] for m in self.store.read_messages('123', 2)],
                         ['second', 'same id, other direction'])
        self.assertEqual(self.store.read_messages('123:topic:12', 50),
                         [dict(id='2', direction='in', **{'from': 'Pat'}, text='in ops', at='2026-11-06T10:00:01+00:00')])
        self.assertEqual(self.store.read_messages('999', 50), [])

    def test_the_log_keeps_the_newest_messages_of_each_conversation(self):
        with patch.object(b, 'MESSAGE_LOG_KEEP', 3):
            for index in range(5):
                self.store.log_message('123', 'in', str(index), 'Pat', f'm{index}', 'at')
                self.store.log_message('456', 'in', str(index), 'Pat', f'o{index}', 'at')
        self.assertEqual([m['text'] for m in self.store.read_messages('123', 50)], ['m2', 'm3', 'm4'])
        self.assertEqual([m['text'] for m in self.store.read_messages('456', 50)], ['o2', 'o3', 'o4'])

    def test_publish_logs_a_confirmed_send_once_with_its_visible_text(self):
        ids = iter(range(70, 80))

        def transport(url, body, *args, **kwargs):
            self.calls.append((url, body))
            return 200, dict(ok=True, result=dict(message_id=next(ids), chat=dict(id=123)))
        self.bridge.request = transport
        self.assertTrue(self.bridge.publish(self.publish(key='p-1', text='plain words'))['delivered'])
        self.assertTrue(self.bridge.publish(self.publish(key='p-1', text='plain words'))['delivered'])  # replay
        html = dict(self.publish(key='p-2', text='<b>bold</b> &amp; more'), format='html')
        self.assertTrue(self.bridge.publish(html)['delivered'])
        photo = dict(self.publish(key='p-3', text='caption'), attachment=dict(path=self.attachment_file(), kind='photo'))
        self.assertTrue(self.bridge.publish(photo)['delivered'])
        logged = self.store.read_messages('123', 50)
        self.assertEqual([(m['id'], m['direction'], m['from'], m['text']) for m in logged],
                         [('70', 'out', 'test_bot', 'plain words'), ('71', 'out', 'test_bot', 'bold & more'),
                          ('88', 'out', 'test_bot', '[photo.png]\ncaption')])
        self.assertEqual(len(self.calls), 2)  # the replay sent nothing

    def test_publish_logs_nothing_it_did_not_deliver(self):
        self.bridge.request = lambda *args, **kwargs: (400, dict(ok=False, error_code=400, description='Bad Request'))
        self.assertFalse(self.bridge.publish(self.publish(key='p-4', text='refused'))['delivered'])
        self.assertFalse(self.bridge.publish(self.publish(key='', text='no key'))['delivered'])
        self.assertEqual(self.store.read_messages('123', 50), [])


class AutoFormatTests(unittest.TestCase):

    def test_valid_markup_with_a_tag_or_an_entity_is_html(self):
        for text in ('<b>bold</b>', 'a &lt; b', '&amp;', '<a href="https://x.org">x</a>', '&#128512;',
                     '<tg-spoiler>s</tg-spoiler>', '<pre><code class="language-py">x</code></pre>'):
            with self.subTest(text=text):
                self.assertEqual(b.auto_format(text), 'html')

    def test_anything_else_is_plain(self):
        for text in ('hello', '', None, 5, 'a < b', 'AT&T', 'x<y>z', '1 &lt 2', '&nbsp;',
                     '<div>x</div>', '<b>unclosed', '<i>a</b>', '<B>x</B>'):
            with self.subTest(text=text):
                self.assertEqual(b.auto_format(text), 'plain')


class ValidHookUrlTests(unittest.TestCase):

    def test_plain_http_on_a_loopback_address_at_a_hooks_path(self):
        for url in (HOOK_URL, 'http://[::1]:8797/hooks/tgbridge', 'http://localhost:8797/hooks/tg-bridge_1.x',
                    'http://127.0.0.1:8797/hooks/' + 'a' * 64):
            with self.subTest(url=url):
                self.assertTrue(b.valid_hook_url(url))

    def test_another_scheme_host_path_or_part_is_refused(self):
        for url in ('https://127.0.0.1:8797/hooks/x', 'http://127.0.0.1/hooks/x', 'http://127.0.0.1:/hooks/x',
                    'http://127.0.0.1:99999/hooks/x', 'http://10.0.0.1:8797/hooks/x', 'http://0.0.0.0:8797/hooks/x',
                    'http://127.0.0.2:8797/hooks/x', 'http://example.org:8797/hooks/x',
                    'http://127.0.0.1:8797/hooks/', 'http://127.0.0.1:8797/hooks/x/', 'http://127.0.0.1:8797/hooks/a/b',
                    'http://127.0.0.1:8797/hooks/-x', 'http://127.0.0.1:8797/v1/send', 'http://127.0.0.1:8797/',
                    'http://127.0.0.1:8797/hooks/' + 'a' * 65, 'http://u@127.0.0.1:8797/hooks/x',
                    'http://u:p@127.0.0.1:8797/hooks/x', 'http://127.0.0.1:8797/hooks/x?a=1',
                    'http://127.0.0.1:8797/hooks/x#f', 'http://127.0.0.1:8797/hooks/' + 'a' * 300,
                    'http://127.0.0.1:0/hooks/x', 'http://127.0.0.1:8797/hooks/x?', 'http://127.0.0.1:8797/hooks/x#',
                    '', None, 5, b'http://127.0.0.1:8797/hooks/x'):
            with self.subTest(url=url):
                self.assertFalse(b.valid_hook_url(url))


class YardApiTests(BridgeTestCase):
    """The yard API on the bridge's loopback port (YARDR.md, "Outbound")."""

    POST_ROUTES = ('/v1/send', '/v1/tell', '/v1/posted', '/v1/read', '/v1/react', '/v1/pin', '/v1/ask-close', '/v1/run-ask', '/v1/ask-card', '/v1/siding-card')

    def setUp(self):
        super().setUp()
        self.port = self.start_callback_server()

    def call(self, path, body=None, **kwargs):
        return self.api_call(self.port, path, body, **kwargs)

    def telegram_calls(self):
        return [(url.rsplit('/', 1)[1], body) for url, body in self.calls if url.startswith('https://api.telegram.org/')]

    def with_topic(self):
        self.bridge.topic_settings = (dict(name='ops', agent='yardmaster'),)
        self.store.record_topic('ops', OPS, 'created')

    # ---- authorization and malformed requests ------------------------------

    def test_every_route_refuses_a_missing_or_wrong_secret_with_403(self):
        refusals = [dict(secret=False), dict(secret='WRONG'), dict(secret='CAPABILITYX'), dict(secret=''),
                    dict(secret=False, headers={'Authorization': 'CAPABILITY'}),
                    dict(secret=False, headers={'Authorization': 'bearer CAPABILITY'}),
                    dict(secret=False, headers={'Authorization': 'Bearer CAPABILITY '}),
                    dict(secret=False, headers={'Authorization': 'Basic Q0FQQUJJTElUWQ=='}),
                    dict(secret=False, headers={'X-Tgbridge-Secret': 'CAPABILITY'})]
        for path in self.POST_ROUTES + ('/v1/health', '/v1/nowhere', '/CAPABILITY/publish'):
            method = 'GET' if path == '/v1/health' else 'POST'
            for refusal in refusals:
                with self.subTest(path=path, refusal=refusal):
                    status, answer = self.call(path, dict(key='k', text='hi', slot='s', decision='d'),
                                               method=method, **refusal)
                    self.assertEqual((status, answer), (403, dict(error='forbidden')))
        self.assertEqual(self.calls, [])
        self.assertIsNone(self.store.saved_receipt('k'))

    def test_a_bridge_without_a_callback_secret_refuses_everything(self):
        port = self.start_callback_server(bridge=b.Bridge(self.config, self.store, 'SECRET', None,
                                                          request=self.transport))
        status, answer = self.api_call(port, '/v1/health', method='GET', secret='None')
        self.assertEqual((status, answer), (403, dict(error='forbidden')))

    def test_an_unknown_route_or_method_is_404(self):
        for method, path in (('POST', '/v1/nowhere'), ('POST', '/v1/health'), ('GET', '/v1/send'),
                             ('POST', '/v1/send/'), ('POST', '/v1/send?x=1'), ('POST', '/CAPABILITY/publish')):
            with self.subTest(method=method, path=path):
                status, answer = self.call(path, dict(key='k', text='hi'), method=method)
                self.assertEqual((status, answer), (404, dict(error='not found')))
        self.assertEqual(self.calls, [])

    def test_a_malformed_body_answers_400(self):
        too_big = json.dumps(dict(key='big', text='x' * (b.YARD_API_MAX_BODY + 1)))
        for raw in ('not json', '[1, 2]', '"text"', 'null', '', b'\xff\xfe{', too_big):
            with self.subTest(raw=raw[:20]):
                status, answer = self.call('/v1/send', raw=raw)
                self.assertEqual(status, 400)
                self.assertIsInstance(answer.get('error'), str)
        self.assertEqual(self.calls, [])

    def test_bad_send_fields_answer_400_and_send_nothing(self):
        self.with_topic()
        cases = [
            (dict(text='no key'), 'key is required'),
            (dict(text='blank key', key='  '), 'key is required'),
            (dict(text='number key', key=5), 'key is required'),
            (dict(key='k', text=5), 'text must be a string'),
            (dict(key='k', text=None, format='markdown'), 'markdown text must be a string'),
            (dict(key='k', text='x', format='rich'), 'format must be plain, html, markdown or auto'),
            *[(dict(key='k', text='x', format=value), 'format must be') for value in (False, [], {}, 5)],
            (dict(key='k', text='x', topic='nope'), 'topic nope is not configured'),
            (dict(key='k', text='x', conversation='124'), b.UNRESOLVED_CONVERSATION),
            (dict(key='k', text='x', conversation='123:topic:99'), b.UNRESOLVED_CONVERSATION),
            (dict(key='k', text='x', silent='maybe'), 'silent must be true or false'),
            (dict(key='k', text='x', text_options=2), 'text_options must be true or false'),
            (dict(key='k', text='x', keyboard='[[{'), 'keyboard must be JSON rows'),
            (dict(key='k', text='x', attachments=[5]), 'attachments must be a list of file paths'),
            (dict(key='k', text='x', attachments=['/tmp/a.png'], attachment_kind='audio'), 'attachment_kind must be'),
        ]
        for body, error in cases:
            with self.subTest(body=body):
                status, answer = self.call('/v1/send', body)
                self.assertEqual(status, 400)
                self.assertIn(error, answer['error'])
        self.assertEqual(self.calls, [])
        self.assertIsNone(self.store.saved_receipt('k'))

    # ---- /v1/send -----------------------------------------------------------

    def test_send_answers_delivered_with_the_message_id_and_the_conversation(self):
        status, answer = self.call('/v1/send', dict(key='s-1', text='hello'))
        self.assertEqual((status, answer), (200, dict(delivered=True, message_id='77', conversation='123')))
        [(method, body)] = self.telegram_calls()
        self.assertEqual((method, body['chat_id'], body['text']), ('sendMessage', 123, 'hello'))
        self.assertNotIn('parse_mode', body)
        status, answer = self.call('/v1/send', dict(key='s-1', text='hello'))  # a replay sends nothing
        self.assertEqual(answer, dict(delivered=True, message_id='77', conversation='123'))
        self.assertEqual(len(self.telegram_calls()), 1)

    def test_a_refused_send_answers_200_with_delivered_false_and_the_reason(self):
        self.call('/v1/send', dict(key='s-2', text='first'))
        status, answer = self.call('/v1/send', dict(key='s-2', text='another body'))
        self.assertEqual((status, answer), (200, dict(delivered=False, conversation='123',
                                                      reason='idempotency_key_conflict', failure_kind='permanent')))
        status, answer = self.call('/v1/send', dict(key='s-3', text='Pick:\nA) one\nB) two'))
        self.assertEqual(answer['reason'], 'options_need_keyboard')
        self.assertEqual(len(self.telegram_calls()), 1)

    def test_format_auto_is_the_default_and_sends_html_only_for_valid_markup(self):
        cases = [
            (dict(text='<b>done</b> &amp; dusted'), 'HTML', False),
            (dict(text='<b>done</b>', format='auto'), 'HTML', False),
            (dict(text='a < b & c > d'), None, True),
            (dict(text='use <div> here'), None, True),
            (dict(text='plain words'), None, False),
            (dict(text='<b>literal</b>', format='plain'), None, False),
            (dict(text='<i>x</i>', format='html'), 'HTML', False),
            (dict(text='```\necho hello\n```'), None, False),
            (dict(text='```\n<b>x</b>\n```'), 'HTML', False),
            (dict(text='a & b; echo "$x" && ls < input'), None, True),
            (dict(text='<b>done</b>', format=None), 'HTML', False),
            (dict(text='<b>done</b>', format=''), 'HTML', False),
            (dict(text='a < b'), None, True),
            (dict(text='<code>echo ready && echo done</code>', format='plain'), None, False),
            (dict(text='<code>echo ready &amp;&amp; echo done</code>'), 'HTML', False),
        ]
        refused = [
            (dict(text='<code>echo ready && echo done</code>'), 'html_text_not_escaped'),
            (dict(text='<code>echo ready && echo done</code>', format='auto'), 'html_text_not_escaped'),
            (dict(text='<code>echo ready && echo done</code>', format=None), 'html_text_not_escaped'),
            (dict(text='<code>echo ready && echo done</code>', format=''), 'html_text_not_escaped'),
            (dict(text='<b>X:</b> Nudge: <words>'), 'html_tag_not_allowed'),
            (dict(text='<b>X:</b> Nudge: <words>', format='auto'), 'html_tag_not_allowed'),
        ]
        for index, (body, reason) in enumerate(refused):
            with self.subTest(body=body):
                before = len(self.telegram_calls())
                request = dict(body, key=f'refuse-{index}')
                status, answer = self.call('/v1/send', request)
                self.assertEqual(status, 200)
                self.assertEqual(answer, dict(delivered=False, conversation='123', reason=reason,
                                              failure_kind='permanent'))
                self.assertEqual(self.call('/v1/send', request), (200, answer))
                self.assertEqual(len(self.telegram_calls()), before)
        for index, (body, parse_mode, fallback) in enumerate(cases):
            with self.subTest(body=body):
                self.calls.clear()
                request = dict(body, key=f'fmt-{index}')
                status, answer = self.call('/v1/send', request)
                self.assertEqual(status, 200)
                expected = dict(delivered=True, message_id='77', conversation='123')
                if fallback:
                    expected.update(delivered_format='plain', format_fallback_reason='invalid_html')
                self.assertEqual(answer, expected)
                self.assertEqual(self.store.saved_receipt(request['key']).get('format_fallback_reason'),
                                 'invalid_html' if fallback else None)
                [(_, sent)] = self.telegram_calls()
                self.assertEqual(sent['text'], body['text'])
                self.assertEqual(sent.get('parse_mode'), parse_mode)
                self.assertEqual(self.call('/v1/send', request), (200, answer))
                self.assertEqual(len(self.telegram_calls()), 1)

        with self.subTest(first_format='plain'):
            self.calls.clear()
            request = dict(key='format-replay-plain', format='plain',
                           text='<code>echo ready && echo done</code>')
            expected = dict(delivered=True, message_id='77', conversation='123')
            self.assertEqual(self.call('/v1/send', request), (200, expected))
            identity = dict(conversation=self.bridge.conversation, idempotency_key=request['key'],
                            text=request['text'])
            fingerprint = b.Bridge.delivery_fingerprint(identity)
            self.assertEqual(self.store.saved_delivery(request['key'])[0], fingerprint)
            restored = b.Bridge(self.config, b.Store(self.path, 0), 'SECRET', 'CAPABILITY',
                                request=self.transport)
            port = self.start_callback_server(bridge=restored)
            for replay_format in ('plain', 'auto', None, ''):
                replay = dict(request, format=replay_format)
                self.assertEqual(self.api_call(port, '/v1/send', replay), (200, expected))
            self.assertEqual(self.api_call(port, '/v1/send', dict(request, text='another body')),
                             (200, dict(delivered=False, conversation='123',
                                 reason='idempotency_key_conflict', failure_kind='permanent')))
            self.assertEqual(self.store.saved_delivery(request['key'])[0], fingerprint)
            self.assertEqual(len(self.telegram_calls()), 1)

        for label, key, text, saved_receipt, expected_answer, refusal_reason in (
                ('legacy_invalid_html_fallback', 'legacy-auto-tag', '<b>X:</b> Nudge: <words>',
                 dict(delivered=True, message_id='55', format_fallback_reason='invalid_html'),
                 dict(delivered=True, message_id='55', conversation='123',
                      delivered_format='plain', format_fallback_reason='invalid_html'),
                 'html_tag_not_allowed'),
                ('legacy_pre_fallback_metadata', 'legacy-auto-amp', '<code>echo ready && echo done</code>',
                 dict(delivered=True, message_id='56'),
                 dict(delivered=True, message_id='56', conversation='123'),
                 'html_text_not_escaped')):
            with self.subTest(legacy=label):
                self.calls.clear()
                identity = dict(conversation=self.bridge.conversation, idempotency_key=key, text=text)
                fingerprint = b.Bridge.delivery_fingerprint(identity)
                with self.store.connect() as db:
                    db.execute('INSERT INTO deliveries VALUES (?, ?, ?)',
                               (key, fingerprint, json.dumps(saved_receipt)))
                for replay_format in ('auto', None, ''):
                    replay = dict(key=key, text=text, format=replay_format)
                    self.assertEqual(self.call('/v1/send', replay), (200, expected_answer))
                self.assertEqual(self.call('/v1/posted', dict(key=key)),
                                 (200, dict(message_id=saved_receipt['message_id'])))
                self.assertEqual(self.call('/v1/send', dict(key=key, text='<b>X:</b> Nudge: &lt;words&gt;')),
                                 (200, dict(delivered=False, conversation='123',
                                     reason='idempotency_key_conflict', failure_kind='permanent')))
                self.assertEqual(self.call('/v1/send', dict(key=key, text='<b>other</b> <words>')),
                                 (200, dict(delivered=False, conversation='123',
                                     reason='idempotency_key_conflict', failure_kind='permanent')))
                fresh_key = f'fresh-{key}'
                self.assertEqual(self.call('/v1/send', dict(key=fresh_key, text=text)),
                                 (200, dict(delivered=False, conversation='123',
                                     reason=refusal_reason, failure_kind='permanent')))
                self.assertIsNone(self.store.saved_receipt(fresh_key))
                self.assertEqual(len(self.telegram_calls()), 0)

        with self.subTest(legacy='ambiguous'):
            key, text = 'legacy-auto-ambiguous', '<b>X:</b> Nudge: <words>'
            fingerprint = b.Bridge.delivery_fingerprint(
                dict(conversation=self.bridge.conversation, idempotency_key=key, text=text))
            with self.store.connect() as db:
                db.execute('INSERT INTO deliveries VALUES (?, ?, ?)',
                           (key, fingerprint, json.dumps(
                               self.bridge.receipt(reason='ambiguous_delivery_requires_manual_reconciliation'))))
            self.assertEqual(self.call('/v1/send', dict(key=key, text=text)),
                             (200, dict(delivered=False, conversation='123',
                                 reason='ambiguous_delivery_requires_manual_reconciliation',
                                 failure_kind='permanent')))

        invalid = dict(text='<code>echo ready && echo done</code>', key='explicit-invalid', format='html')
        before = len(self.telegram_calls())
        self.assertEqual(self.call('/v1/send', invalid)[1], dict(delivered=False, conversation='123',
            reason='html_text_not_escaped', failure_kind='permanent'))
        self.assertEqual(len(self.telegram_calls()), before)

        def transport(url, body, *args, **kwargs):
            status, answer = self.transport(url, body, *args, **kwargs)
            answer['result']['message_id'] = 98
            return status, answer
        self.bridge.request = transport
        body = dict(text='**Ready** & <b>literal</b> 👩\u200d💻', format='markdown', key='markdown-api',
                    silent=True, reply_to='21')
        status, answer = self.call('/v1/send', body)
        self.assertTrue(answer['delivered'])
        sent = self.telegram_calls()[-1][1]
        self.assertEqual(sent['text'], '<b>Ready</b> &amp; &lt;b&gt;literal&lt;/b&gt; 👩\u200d💻')
        self.assertEqual(sent['parse_mode'], 'HTML')
        self.assertEqual(sent['disable_notification'], True)
        self.assertEqual(sent['reply_parameters'], dict(message_id=21))
        self.assertEqual(self.bridge.api_read({})['messages'][-1]['text'], 'Ready & <b>literal</b> 👩\u200d💻')
        count = len(self.calls)
        self.assertEqual(self.call('/v1/send', body)[1], answer)
        self.assertEqual(len(self.calls), count)
        self.assertEqual(self.call('/v1/send', dict(body, format='plain'))[1]['reason'], 'idempotency_key_conflict')

    def test_the_idempotency_key_header_keys_a_send_whose_body_has_none(self):
        status, answer = self.call('/v1/send', dict(text='by header'), headers={'Idempotency-Key': 'hdr-1'})
        self.assertEqual((status, answer['delivered']), (200, True))
        self.assertEqual(self.store.saved_receipt('hdr-1')['message_id'], '77')
        self.call('/v1/send', dict(text='by header'), headers={'Idempotency-Key': 'hdr-1'})
        self.assertEqual(len(self.telegram_calls()), 1)  # the header's key dedupes the replay
        self.call('/v1/send', dict(text='by body', key='body-1'), headers={'Idempotency-Key': 'hdr-2'})
        self.assertIsNotNone(self.store.saved_receipt('body-1'))  # a key in the body wins
        self.assertIsNone(self.store.saved_receipt('hdr-2'))

    def test_send_by_topic_name_or_conversation_id_goes_into_the_topic(self):
        self.with_topic()
        for index, target in enumerate((dict(topic='ops'), dict(conversation='123:topic:12'))):
            with self.subTest(target=target):
                self.calls.clear()
                status, answer = self.call('/v1/send', dict(target, key=f'topic-{index}', text='in ops'))
                self.assertEqual((status, answer),
                                 (200, dict(delivered=True, message_id='77', conversation='123:topic:12')))
                [(_, sent)] = self.telegram_calls()
                self.assertEqual(sent['message_thread_id'], OPS)
        for index, target in enumerate((dict(topic='general'), dict(conversation='123'), dict(topic=''))):
            with self.subTest(target=target):
                self.calls.clear()
                status, answer = self.call('/v1/send', dict(target, key=f'general-{index}', text='in general'))
                self.assertEqual(answer['conversation'], '123')
                [(_, sent)] = self.telegram_calls()
                self.assertNotIn('message_thread_id', sent)

    def test_send_carries_silent_reply_keyboard_and_a_file(self):
        keyboard = [[dict(text='Yes', callback_data=f'ask:{b.Bridge._ask_id_for_key("opt-1")}:yes')]]
        status, answer = self.call('/v1/send', dict(key='opt-1', text='Go?', silent='true', reply_to=5,
                                                    keyboard=json.dumps(keyboard)))
        self.assertTrue(answer['delivered'])
        [(_, sent)] = self.telegram_calls()
        self.assertIs(sent['disable_notification'], True)
        self.assertEqual(sent['reply_parameters'], {'message_id': 5})
        self.assertEqual(sent['reply_markup'], {'inline_keyboard': keyboard})
        path = self.attachment_file('report.pdf')
        status, answer = self.call('/v1/send', dict(key='file-1', text='the report', attachments=[path]))
        self.assertEqual(answer, dict(delivered=True, message_id='88', conversation='123'))
        [(url, fields, field, sent_path)] = self.multipart_calls
        self.assertTrue(url.endswith('/sendDocument'))
        self.assertEqual((field, sent_path, fields['caption']), ('document', path, 'the report'))

    # ---- /v1/posted ---------------------------------------------------------

    def test_posted_answers_from_the_ledger(self):
        self.assertEqual(self.call('/v1/posted', dict(key='never')), (200, {}))
        self.call('/v1/send', dict(key='p-1', text='hello'))
        self.assertEqual(self.call('/v1/posted', dict(key='p-1')), (200, dict(message_id='77')))
        self.assertEqual(self.call('/v1/posted', {}, headers={'Idempotency-Key': 'p-1'}), (200, dict(message_id='77')))
        self.bridge.request = lambda *args, **kwargs: (400, dict(ok=False, error_code=400, description='Bad Request'))
        self.call('/v1/send', dict(key='p-2', text='refused'))
        self.assertFalse(self.store.saved_receipt('p-2')['delivered'])
        self.assertEqual(self.call('/v1/posted', dict(key='p-2')), (200, {}))
        status, answer = self.call('/v1/posted', {})
        self.assertEqual((status, answer), (400, dict(error='key is required')))

    # ---- /v1/read -----------------------------------------------------------

    def test_read_returns_whole_logged_messages_oldest_first(self):
        long_text = 'y' * 2000
        self.bridge.drain([self.update(uid=10)])
        update = self.update(uid=11)
        update['message']['text'] = long_text
        self.bridge.drain([update])
        self.call('/v1/send', dict(key='r-1', text='<b>answer</b>'))
        status, answer = self.call('/v1/read', dict(conversation='123'))
        self.assertEqual(status, 200)
        messages = answer['messages']
        self.assertEqual([(m['id'], m['direction'], m['from'], m['text']) for m in messages],
                         [('10', 'in', 'Pat', 'hello'), ('11', 'in', 'Pat', long_text), ('77', 'out', 'test_bot', 'answer')])
        self.assertTrue(all(datetime.datetime.fromisoformat(m['at']) for m in messages))
        self.assertEqual(self.call('/v1/read', {})[1], answer)  # the paired chat by default
        self.assertEqual([m['id'] for m in self.call('/v1/read', dict(limit=2))[1]['messages']], ['11', '77'])
        self.assertEqual([m['id'] for m in self.call('/v1/read', dict(limit='1'))[1]['messages']], ['77'])

    def test_read_of_a_topic_holds_only_its_messages(self):
        self.with_topic()
        update = self.update(uid=10)
        update['message'].update(is_topic_message=True, message_thread_id=OPS)
        self.bridge.drain([update, self.update(uid=11)])
        self.assertEqual([m['text'] for m in self.call('/v1/read', dict(topic='ops'))[1]['messages']],
                         ['[topic: ops]\nhello'])
        self.assertEqual([m['id'] for m in self.call('/v1/read', dict(conversation='123'))[1]['messages']], ['11'])

    def test_read_refuses_a_bad_limit_or_an_unknown_conversation(self):
        for body in (dict(limit=0), dict(limit=b.READ_LIMIT_MAX + 1), dict(limit='x'), dict(limit=True),
                     dict(limit=2.0), dict(limit=None), dict(conversation='124'), dict(topic='nope')):
            with self.subTest(body=body):
                status, answer = self.call('/v1/read', body)
                self.assertEqual(status, 400)
                self.assertIn('error', answer)
        self.assertEqual(self.call('/v1/read', dict(limit=b.READ_LIMIT_MAX)), (200, dict(messages=[])))

    # ---- /v1/react, /v1/pin, /v1/ask-close, /v1/health -----------------------

    def test_react_sets_a_named_or_emoji_reaction_on_a_message(self):
        def transport(url, body, *args, **kwargs):
            if url.endswith('/setMessageReaction'):
                self.calls.append((url, body))
                return 200, dict(ok=True, result=True)
            return self.transport(url, body, *args, **kwargs)
        self.bridge.request = transport
        status, answer = self.call('/v1/react', dict(reaction='done', message_id=43))
        self.assertEqual((status, answer), (200, dict(delivered=True, message_id='43')))
        [(method, body)] = self.telegram_calls()
        self.assertEqual((method, body['message_id'], body['reaction']),
                         ('setMessageReaction', 43, [dict(type='emoji', emoji=b.REACTIONS_DEFAULTS['done'])]))
        self.calls.clear()
        self.assertEqual(self.call('/v1/react', dict(reaction='\U0001F525', message_id='44'))[1],
                         dict(delivered=True, message_id='44'))
        self.assertEqual(self.telegram_calls()[0][1]['reaction'], [dict(type='emoji', emoji='\U0001F525')])
        self.bridge.drain([self.update(uid=50)])  # now the last accepted message
        self.calls.clear()
        self.assertEqual(self.call('/v1/react', dict(reaction='none'))[1], dict(delivered=True, message_id='50'))
        self.assertEqual(self.telegram_calls()[0][1]['reaction'], [])
        self.calls.clear()
        self.assertEqual(self.call('/v1/react', dict(reaction='seen', conversation='123'))[1],
                         dict(delivered=True, message_id='50'))

    def test_a_yard_reaction_is_keyed_so_the_seen_guard_sees_it(self):
        # The yard sends its own Idempotency-Key; the ledger key stays
        # reaction:<message id>:…, which Store.agent_reacted looks for, so a
        # queued `seen` never overwrites the agent's `done`.
        def transport(url, body, *args, **kwargs):
            if url.endswith('/setMessageReaction'):
                self.calls.append((url, body))
                return 200, dict(ok=True, result=True)
            return self.transport(url, body, *args, **kwargs)
        self.bridge.request = transport
        status, answer = self.call('/v1/react', dict(reaction='done', message_id=43),
                                   headers={'Idempotency-Key': 'yard-key-1'})
        self.assertEqual((status, answer['delivered']), (200, True))
        self.assertIsNotNone(self.store.saved_receipt('reaction:43:yard-yard-key-1'))
        self.assertIsNone(self.store.saved_receipt('yard-key-1'))
        self.assertTrue(self.store.agent_reacted(43))
        # A caller's key of `accept` is never the seen worker's key.
        self.call('/v1/react', dict(reaction='done', message_id=44, key='accept'))
        self.assertIsNone(self.store.saved_receipt('reaction:44:accept'))
        self.assertIsNotNone(self.store.saved_receipt('reaction:44:yard-accept'))
        # The same call again is the same key: one more Telegram request, not two.
        self.call('/v1/react', dict(reaction='done', message_id=43), headers={'Idempotency-Key': 'yard-key-1'})
        self.assertEqual(len(self.telegram_calls()), 2)

    def test_react_refuses_a_bad_reaction_or_target(self):
        for body in (dict(reaction='maybe', message_id=1), dict(message_id=1), dict(reaction='done', message_id='x'),
                     dict(reaction='done', message_id=0), dict(reaction='done'),
                     dict(reaction='done', conversation='124')):
            with self.subTest(body=body):
                status, answer = self.call('/v1/react', body)
                self.assertEqual(status, 400)
                self.assertIn('error', answer)
        self.assertEqual(self.calls, [])

    def test_pin_keeps_a_slot_pinned_and_unpins_it(self):
        def transport(url, body, *args, **kwargs):
            self.calls.append((url, body))
            if url.endswith('/sendMessage'):
                return 200, dict(ok=True, result=dict(message_id=61, chat=dict(id=123)))
            return 200, dict(ok=True, result=True)
        self.bridge.request = transport
        status, answer = self.call('/v1/pin', dict(slot='waiting', text='<b>2</b> asks wait'))
        self.assertEqual((status, answer), (200, dict(delivered=True, message_id='61', changed=True)))
        self.assertEqual([method for method, _ in self.telegram_calls()], ['sendMessage', 'pinChatMessage'])
        self.assertEqual(self.telegram_calls()[0][1]['parse_mode'], 'HTML')  # auto: valid markup
        self.calls.clear()
        self.assertEqual(self.call('/v1/pin', dict(slot='waiting', text='<b>2</b> asks wait'))[1],
                         dict(delivered=True, message_id='61', changed=False))
        self.assertEqual(self.telegram_calls(), [])
        status, answer = self.call('/v1/pin', dict(slot='waiting', unpin=True))
        self.assertEqual((status, answer['delivered']), (200, True))
        self.assertEqual([method for method, _ in self.telegram_calls()], ['unpinChatMessage'])
        self.calls.clear()
        request = dict(slot='markdown', text='**Waiting** `<raw>`', format='markdown')
        self.assertTrue(self.call('/v1/pin', request)[1]['delivered'])
        self.assertEqual(self.telegram_calls()[0][1]['text'], '<b>Waiting</b> <code>&lt;raw&gt;</code>')
        self.assertEqual(self.telegram_calls()[0][1]['parse_mode'], 'HTML')
        self.calls.clear()
        self.assertFalse(self.call('/v1/pin', request)[1]['changed'])
        self.assertEqual(self.telegram_calls(), [])
        for index, empty in enumerate((None, '')):  # an empty format is auto, as before
            self.calls.clear()
            status, answer = self.call('/v1/pin', dict(slot=f'empty-{index}', text='<b>2</b> wait', format=empty))
            self.assertEqual((status, answer['delivered']), (200, True))
            self.assertEqual(self.telegram_calls()[0][1]['parse_mode'], 'HTML')
        status, answer = self.call('/v1/pin', dict(slot='bad', text='x', format='rich'))
        self.assertEqual(status, 400)
        self.assertIn('format must be plain, html, markdown or auto', answer['error'])

    def test_pin_refuses_a_bad_slot_text_or_format(self):
        for body in (dict(slot='Bad Slot', text='x'), dict(text='x'), dict(slot='s'), dict(slot='s', text='  '),
                     dict(slot='s', text='x', format='rich'), dict(slot='s', unpin='perhaps')):
            with self.subTest(body=body):
                status, answer = self.call('/v1/pin', body)
                self.assertEqual(status, 400)
                self.assertIn('error', answer)
        self.assertEqual(self.calls, [])

    def test_ask_close_needs_a_decision_and_exactly_one_target(self):
        for body in (dict(message_id=1), dict(decision='go'), dict(decision='go', message_id=1, ask_key='k'),
                     dict(decision='  ', ask_key='k'), dict(decision='go', message_id='x'),
                     dict(decision='go', ask_key=5), dict(decision='go', ask_key=['k'])):
            with self.subTest(body=body):
                status, answer = self.call('/v1/ask-close', body)
                self.assertEqual(status, 400)
                self.assertIn('error', answer)
        self.assertEqual(self.call('/v1/ask-close', dict(decision='go', ask_key='never-sent')),
                         (200, dict(delivered=False, reason='ask_not_found', failure_kind='permanent')))

    def test_ask_close_closes_a_recorded_ask(self):
        def transport(url, body, *args, **kwargs):
            self.calls.append((url, body))
            if url.endswith('/editMessageText'):
                return 200, dict(ok=True, result=dict(message_id=body['message_id'], chat=dict(id=123)))
            return self.transport(url, body, *args, **kwargs)
        self.bridge.request = transport
        ask = dict(self.publish(key='ask-1', text='Pick one?'),
                   keyboard=[[dict(text='Yes', callback_data=f'ask:{b.Bridge._ask_id_for_key("ask-1")}:yes')]])
        self.assertTrue(self.bridge.publish(ask)['delivered'])
        status, answer = self.call('/v1/ask-close', dict(decision='Typed: yes', ask_key='ask-1'))
        self.assertEqual((status, answer), (200, dict(delivered=True, message_id='77')))
        [edit] = [body for method, body in self.telegram_calls() if method == 'editMessageText']
        self.assertTrue(edit['text'].endswith('Typed: yes'))

    def test_health_answers_the_bot_and_the_last_good_poll(self):
        self.assertEqual(self.call('/v1/health', method='GET'),
                         (200, dict(ok=True, bot='test_bot', last_poll_ok=None)))
        self.bridge.record_poll_ok(now=datetime.datetime(2026, 11, 6, 9, 30, tzinfo=datetime.timezone.utc))
        self.assertEqual(self.call('/v1/health', method='GET'),
                         (200, dict(ok=True, bot='test_bot', last_poll_ok='2026-11-06T09:30:00Z')))

    def test_an_unexpected_failure_answers_500_without_its_detail(self):
        with patch.object(self.bridge, 'api_send', side_effect=RuntimeError('SECRET detail')):
            status, answer = self.call('/v1/send', dict(key='k', text='x'))
        self.assertEqual((status, answer), (500, dict(error='delivery unconfirmed')))


if __name__ == '__main__':
    unittest.main()


class TellTests(BridgeTestCase):
    """POST /v1/tell, the yard's notify: an ask's own tell carries its key; plain text."""

    def setUp(self):
        super().setUp()
        self.port = self.start_callback_server()

    call = YardApiTests.call
    telegram_calls = YardApiTests.telegram_calls
    with_topic = YardApiTests.with_topic

    def test_tells_title_text_and_link_as_plain_text_in_the_named_conversation(self):
        status, answer = self.call('/v1/tell', dict(conversation='123', title='mail from <ops>',
                                                    text='The NAS is back & fine.', link='https://x.test/1'))
        self.assertEqual(status, 200)
        self.assertEqual((answer['delivered'], answer['message_id'], answer['conversation']), (True, '77', '123'))
        [(method, body)] = self.telegram_calls()
        self.assertEqual(method, 'sendMessage')
        self.assertEqual(body['text'], 'mail from <ops>\nThe NAS is back & fine.\nhttps://x.test/1')
        self.assertNotIn('parse_mode', body)

    def test_the_same_tell_within_the_minute_is_sent_once_and_a_minute_later_again(self):
        tell = dict(conversation='123', title='done', text='web-1 landed')
        with patch.object(b.time, 'time', return_value=6000.0):
            first = self.call('/v1/tell', tell)
            again = self.call('/v1/tell', tell)
        with patch.object(b.time, 'time', return_value=6060.0):
            later = self.call('/v1/tell', tell)
        self.assertEqual([r[0] for r in (first, again, later)], [200, 200, 200])
        self.assertTrue(all(r[1]['delivered'] for r in (first, again, later)))
        self.assertEqual(len(self.telegram_calls()), 2)

    def test_tells_into_a_topic_by_its_conversation(self):
        self.with_topic()
        status, answer = self.call('/v1/tell', dict(conversation=f'123:topic:{OPS}', title='t', text='x'))
        self.assertEqual((status, answer['conversation']), (200, f'123:topic:{OPS}'))
        [(_, body)] = self.telegram_calls()
        self.assertEqual(body['message_thread_id'], OPS)

    def test_refuses_an_empty_tell_a_bad_field_or_an_unknown_conversation(self):
        for body in (dict(conversation='123'), dict(conversation='123', title=' ', text=''),
                     dict(conversation='123', title=5, text='x'), dict(conversation='123', title='t' * 251),
                     dict(conversation='123', text='x' * 3501), dict(conversation='999', title='t')):
            with self.subTest(body=body):
                status, answer = self.call('/v1/tell', body)
                self.assertEqual(status, 400)
                self.assertIn('error', answer)
        self.assertEqual(self.telegram_calls(), [])

    def with_owner(self, cards=True, operator='pat'):
        """Rebuild the bridge as the yard's card owner (or not), and serve it."""
        home = Path(self.tmp.name) / 'yard'
        home.mkdir(exist_ok=True)
        self.config.update(yardr_home=str(home), yardr_operator=operator, yardr_cards=cards)
        self.bridge = b.Bridge(self.config, self.store, 'SECRET', 'CAPABILITY', request=self.transport,
                               request_multipart=self.multipart_transport,
                               request_download=self.download_transport)
        self.port = self.start_callback_server()

    def test_an_ask_tell_on_the_card_owner_points_at_decide(self):
        self.with_owner()
        status, answer = self.call('/v1/tell', dict(conversation='123', title='yardmaster asks',
                                                    text='Lower the test limit?'))
        self.assertEqual(status, 200)
        self.assertTrue(answer['delivered'])
        [(method, body)] = self.telegram_calls()
        self.assertEqual(method, 'sendMessage')
        self.assertEqual(body['text'], 'yardmaster asks\nLower the test limit?\nTap /decide to answer.')
        self.assertNotIn('parse_mode', body)

    def test_a_batch_tell_with_an_ask_points_at_decide_and_mail_only_does_not(self):
        self.with_owner()
        for title, text, pointed in (('3 new in yardr', 'sam asks (2), mail from kim', True),
                                     ('3 new in your yardr inbox', 'mail from sam (2), mail from kim', False),
                                     ('mail from kim', 'hello', False)):
            with self.subTest(title=title):
                before = len(self.telegram_calls())
                status, answer = self.call('/v1/tell', dict(conversation='123', title=title, text=text))
                self.assertEqual(status, 200)
                self.assertTrue(answer['delivered'])
                [(method, body)] = self.telegram_calls()[before:]
                expected = f'{title}\n{text}' + ('\nTap /decide to answer.' if pointed else '')
                self.assertEqual(body['text'], expected)

    def test_an_ask_tell_off_the_card_owner_is_sent_unchanged(self):
        cases = (dict(cards=False, operator='pat', text='first question?'),
                 dict(cards=True, operator=None, text='second question?'))
        for case in cases:
            with self.subTest(cards=case['cards'], operator=case['operator']):
                self.with_owner(cards=case['cards'], operator=case['operator'])
                status, answer = self.call('/v1/tell', dict(conversation='123', title='yardmaster asks',
                                                            text=case['text']))
                self.assertEqual(status, 200)
                self.assertTrue(answer['delivered'])
                self.assertEqual(self.telegram_calls()[-1][1]['text'], 'yardmaster asks\n' + case['text'])

    def test_a_lost_reply_replays_a_keyed_ask_tell_once_across_minutes(self):
        self.with_owner()
        tell = dict(conversation='123', title='yardmaster asks', text='Lower the test limit?',
                    key='ask:' + 'cd34' * 8)
        with patch.object(b.time, 'time', return_value=6000.0):
            first = self.call('/v1/tell', tell)
            again = self.call('/v1/tell', tell)
        with patch.object(b.time, 'time', return_value=6060.0):
            later = self.call('/v1/tell', tell)
        self.assertEqual([r[0] for r in (first, again, later)], [200, 200, 200])
        self.assertTrue(all(r[1]['delivered'] for r in (first, again, later)))
        [(method, body)] = self.telegram_calls()
        self.assertEqual(body['text'], 'yardmaster asks\nLower the test limit?\nTap /decide to answer.')

    def test_a_keyed_ask_tell_is_keyed_by_the_scope_and_the_key(self):
        self.with_owner()
        key = 'ask:' + '78ab' * 8
        tell = dict(conversation='123', title='yardmaster asks', text='Lower the test limit?', key=key)
        status, answer = self.call('/v1/tell', tell)
        self.assertEqual(status, 200)
        self.assertTrue(answer['delivered'])
        ledger = 'tell:%s:%s' % (self.bridge._tell_scope_digest('123'), key)
        self.assertTrue(self.store.saved_receipt(ledger)['delivered'])
        self.assertIsNone(self.store.saved_receipt(key))
        # Another recipient (the same ledger, a new scope) neither conflicts nor skips.
        self.with_owner(operator='kim')
        status, answer = self.call('/v1/tell', tell)
        self.assertEqual(status, 200)
        self.assertTrue(answer['delivered'])
        self.assertEqual(len(self.telegram_calls()), 2)
        # Back on the old scope the retry replays.
        self.with_owner(operator='pat')
        status, answer = self.call('/v1/tell', tell)
        self.assertEqual(status, 200)
        self.assertTrue(answer['delivered'])
        self.assertEqual(len(self.telegram_calls()), 2)

    def test_a_source_rebinding_replays_a_keyed_ask_tell(self):
        self.with_owner()
        key = 'ask:' + '90cd' * 8
        tell = dict(conversation='123', title='yardmaster asks', text='Lower the test limit?', key=key)
        status, answer = self.call('/v1/tell', tell)
        self.assertEqual(status, 200)
        self.assertTrue(answer['delivered'])
        ledger = 'tell:%s:%s' % (self.bridge._tell_scope_digest('123'), key)
        # A restart with only the source name changed replays the saved delivery.
        self.config.update(yardr_source='tgbridge2')
        self.with_owner()
        self.assertEqual(self.bridge._tell_scope_digest('123'), ledger.split(':')[1])
        status, answer = self.call('/v1/tell', tell)
        self.assertEqual(status, 200)
        self.assertTrue(answer['delivered'])
        self.assertEqual(len(self.telegram_calls()), 1)

    def test_the_same_ask_key_in_another_served_conversation_sends_again(self):
        self.with_owner()
        self.with_topic()
        key = 'ask:' + '12ef' * 8
        tell = dict(title='yardmaster asks', text='Lower the test limit?', key=key)
        status, answer = self.call('/v1/tell', dict(tell, conversation='123'))
        self.assertEqual(status, 200)
        self.assertTrue(answer['delivered'])
        status, answer = self.call('/v1/tell', dict(tell, conversation=f'123:topic:{OPS}'))
        self.assertEqual(status, 200)
        self.assertTrue(answer['delivered'])
        sent = self.telegram_calls()
        self.assertEqual(len(sent), 2)
        self.assertEqual(sent[0][1]['text'], sent[1][1]['text'])
        self.assertEqual(sent[1][1]['message_thread_id'], OPS)

    def test_a_multiline_ask_question_still_gets_its_pointer(self):
        self.with_owner()
        question = 'Which window?\nA: Morning\nB: Afternoon'
        status, answer = self.call('/v1/tell', dict(conversation='123', title='yardmaster asks', text=question,
                                                    key='ask:' + 'ef56' * 8))
        self.assertEqual(status, 200)
        self.assertTrue(answer['delivered'])
        [(method, body)] = self.telegram_calls()
        self.assertEqual(body['text'], 'yardmaster asks\n' + question + '\nTap /decide to answer.')
        # The keyboard guard stays for every other tell.
        self.with_owner(cards=False)
        status, answer = self.call('/v1/tell', dict(conversation='123', title='yardmaster asks', text=question))
        self.assertEqual(status, 200)
        self.assertEqual(answer['reason'], 'options_need_keyboard')
        self.assertEqual(len(self.telegram_calls()), 1)

    def test_a_padded_ask_title_points_at_decide_like_the_bare_one(self):
        self.with_owner()
        status, answer = self.call('/v1/tell', dict(conversation='123', title=' yardmaster asks ',
                                                    text='Lower the test limit?'))
        self.assertEqual(status, 200)
        self.assertTrue(answer['delivered'])
        [(method, body)] = self.telegram_calls()
        self.assertEqual(body['text'], 'yardmaster asks\nLower the test limit?\nTap /decide to answer.')

    def test_a_keyed_multiword_ask_tell_points_at_decide(self):
        self.with_owner()
        key = 'ask:' + 'beef' * 8
        tell = dict(conversation='123', title='Pat Example asks', text='Lower the test limit?', key=key)
        with patch.object(b.time, 'time', return_value=6000.0):
            status, answer = self.call('/v1/tell', tell)
        self.assertEqual(status, 200)
        self.assertTrue(answer['delivered'])
        [(method, body)] = self.telegram_calls()
        self.assertEqual(body['text'], 'Pat Example asks\nLower the test limit?\nTap /decide to answer.')
        ledger = 'tell:%s:%s' % (self.bridge._tell_scope_digest('123'), key)
        self.assertTrue(self.store.saved_receipt(ledger)['delivered'])
        # A retry after a lost response, in the next minute, sends nothing more.
        with patch.object(b.time, 'time', return_value=6060.0):
            status, answer = self.call('/v1/tell', tell)
        self.assertEqual(status, 200)
        self.assertTrue(answer['delivered'])
        self.assertEqual(len(self.telegram_calls()), 1)
        # Without the key the same words go the ordinary minute-keyed way.
        status, answer = self.call('/v1/tell', dict(conversation='123', title='Pat Example asks',
                                                    text='Raise the test limit?'))
        self.assertEqual(status, 200)
        self.assertTrue(answer['delivered'])
        self.assertEqual(self.telegram_calls()[-1][1]['text'], 'Pat Example asks\nRaise the test limit?')

    def test_a_keyed_multiword_ask_option_preview_still_gets_its_pointer(self):
        self.with_owner()
        question = 'Which window?\nA: Morning\nB: Afternoon'
        for title, stub in (('Pat Example asks', 'cafe'), ('Pat\nExample asks', 'caff')):
            with self.subTest(title=title):
                before = len(self.telegram_calls())
                status, answer = self.call('/v1/tell', dict(conversation='123', title=title, text=question,
                                                            key='ask:' + stub * 8))
                self.assertEqual(status, 200)
                self.assertTrue(answer['delivered'])
                [(method, body)] = self.telegram_calls()[before:]
                self.assertEqual(body['text'], title + '\n' + question + '\nTap /decide to answer.')

    def test_a_keyed_multiword_pointer_proving_not_sent_answers_503_and_its_retry_sends(self):
        self.with_owner()
        self.cut_telegram()
        key = 'ask:' + 'dec0' * 8
        tell = dict(conversation='123', title='Pat Example asks', text='Lower the test limit?', key=key)
        status, answer = self.call('/v1/tell', tell)
        self.assertEqual((status, answer), (503, dict(error='tell unfinished; retry')))
        self.assertEqual(self.telegram_calls(), [])
        self.bridge.request = self.transport
        status, answer = self.call('/v1/tell', tell)
        self.assertEqual(status, 200)
        self.assertTrue(answer['delivered'])
        [(method, body)] = self.telegram_calls()
        self.assertEqual(body['text'], 'Pat Example asks\nLower the test limit?\nTap /decide to answer.')

    def test_an_ask_tell_key_is_validated_before_scoping(self):
        self.with_owner()
        # A blank key is refused, never sent under a scope hiding it.
        for title in ('yardmaster asks', 'Pat Example asks'):
            with self.subTest(title=title):
                status, answer = self.call('/v1/tell', dict(conversation='123', title=title,
                                                            text='Lower the test limit?', key='   '))
                self.assertEqual(status, 200)
                self.assertEqual(answer, dict(delivered=False, conversation='123',
                                              reason='stable_idempotency_key_required',
                                              failure_kind='permanent'))
        # Past the native cap of 200 an ask's key is refused too.
        for length in (201, 256):
            with self.subTest(length=length):
                status, answer = self.call('/v1/tell', dict(conversation='123', title='yardmaster asks',
                                                            text='Lower the test limit?', key='k' * length))
                self.assertEqual(status, 200)
                self.assertEqual(answer, dict(delivered=False, conversation='123',
                                              reason='stable_idempotency_key_required',
                                              failure_kind='permanent'))
        self.assertEqual(self.telegram_calls(), [])
        # An explicit key up to the native cap still goes under its scoped key.
        key = 'k' * 200
        tell = dict(conversation='123', title='yardmaster asks', text='Lower the test limit?', key=key)
        status, answer = self.call('/v1/tell', tell)
        self.assertEqual(status, 200)
        self.assertTrue(answer['delivered'])
        ledger = 'tell:%s:%s' % (self.bridge._tell_scope_digest('123'), key)
        self.assertTrue(self.store.saved_receipt(ledger)['delivered'])
        status, answer = self.call('/v1/tell', tell)
        self.assertEqual(status, 200)
        self.assertTrue(answer['delivered'])
        self.assertEqual(len(self.telegram_calls()), 1)

    def test_a_crash_between_reserve_and_finish_leaves_an_ask_tell_ambiguous(self):
        self.with_owner()
        key = 'ask:' + 'ab12' * 8
        said = 'yardmaster asks\nLower the test limit?\nTap /decide to answer.'
        ledger = 'tell:%s:%s' % (self.bridge._tell_scope_digest('123'), key)
        request = dict(conversation=self.bridge.conversation, idempotency_key=ledger, text=said, text_options=True)
        self.assertIsNone(self.store.reserve(ledger, b.Bridge.delivery_fingerprint(request),
                                             self.bridge.receipt(reason=b.yard_mail.AMBIGUOUS)))
        status, answer = self.call('/v1/tell', dict(conversation='123', title='yardmaster asks',
                                                    text='Lower the test limit?', key=key))
        self.assertEqual(status, 200)
        self.assertEqual(answer, dict(delivered=False, conversation='123',
                                      reason='ambiguous_delivery_requires_manual_reconciliation',
                                      failure_kind='permanent'))
        self.assertEqual(self.telegram_calls(), [])

    def cut_telegram(self):
        """Fail every Telegram attempt before a byte leaves, without waiting."""
        def cut(*args, **kwargs):
            raise b.RequestNotSent('network request not sent', 7, 'connect')
        self.bridge.request, self.bridge.sleep = cut, lambda seconds: None

    def test_a_keyed_pointer_proving_not_sent_answers_503_and_its_retry_sends(self):
        self.with_owner()
        self.cut_telegram()
        key = 'ask:' + '34cd' * 8
        tell = dict(conversation='123', title='yardmaster asks', text='Lower the test limit?', key=key)
        status, answer = self.call('/v1/tell', tell)
        self.assertEqual((status, answer), (503, dict(error='tell unfinished; retry')))
        self.assertEqual(self.telegram_calls(), [])
        ledger = 'tell:%s:%s' % (self.bridge._tell_scope_digest('123'), key)
        saved = self.store.saved_receipt(ledger)
        self.assertTrue(b.resendable(saved))
        # The yard's retry under the same ask key sends the pointer once.
        self.bridge.request = self.transport
        status, answer = self.call('/v1/tell', tell)
        self.assertEqual(status, 200)
        self.assertTrue(answer['delivered'])
        [(method, body)] = self.telegram_calls()
        self.assertEqual(body['text'], 'yardmaster asks\nLower the test limit?\nTap /decide to answer.')

    def post_tell(self, tell):
        """One /v1/tell POST, returning (status, answer, retry_after header or None)."""
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=5)
        try:
            connection.request('POST', '/v1/tell', json.dumps(tell),
                               {'Authorization': 'Bearer CAPABILITY', 'Content-Type': 'application/json'})
            response = connection.getresponse()
            return response.status, json.loads(response.read()), response.getheader('Retry-After')
        finally:
            connection.close()

    def test_a_keyed_pointer_rate_limited_answers_503_and_its_retry_sends_once(self):
        self.with_owner()
        key = 'ask:' + 'ab34' * 8
        tell = dict(conversation='123', title='yardmaster asks', text='Lower the test limit?', key=key)
        def limited(url, body, *args, **kwargs):
            return 429, dict(ok=False, error_code=429, description='Too Many Requests: retry after 3',
                             parameters=dict(retry_after=3))
        self.bridge.request = limited
        status, answer, retry_after = self.post_tell(tell)
        self.assertEqual((status, answer), (503, dict(error='tell unfinished; retry')))
        self.assertEqual(retry_after, '3')
        self.assertEqual(self.telegram_calls(), [])
        ledger = 'tell:%s:%s' % (self.bridge._tell_scope_digest('123'), key)
        saved = self.store.saved_receipt(ledger)
        self.assertTrue(b.resendable(saved))
        # The yard's retry under the same ask key sends the pointer once.
        self.bridge.request = self.transport
        status, answer = self.call('/v1/tell', tell)
        self.assertEqual(status, 200)
        self.assertTrue(answer['delivered'])
        [(method, body)] = self.telegram_calls()
        self.assertEqual(body['text'], 'yardmaster asks\nLower the test limit?\nTap /decide to answer.')
        # Without Telegram's delay the 503 carries no Retry-After.
        key2 = 'ask:' + 'cd56' * 8
        tell2 = dict(conversation='123', title='yardmaster asks', text='Raise the test limit?', key=key2)
        self.bridge.request = lambda *args, **kwargs: (429, dict(ok=False, error_code=429))
        status, answer, retry_after = self.post_tell(tell2)
        self.assertEqual(status, 503)
        self.assertIsNone(retry_after)
        self.bridge.request = self.transport
        status, answer = self.call('/v1/tell', tell2)
        self.assertTrue(answer['delivered'])
        self.assertEqual(len(self.telegram_calls()), 2)
        # Any other tell keeps the 429 as its final answer.
        self.bridge.request = limited
        status, answer = self.call('/v1/tell', dict(conversation='123', title='mail from kim', text='hello'))
        self.assertEqual(status, 200)
        self.assertEqual(answer['reason'], 'telegram_rejected_not_sent_429')
        self.assertEqual(len(self.telegram_calls()), 2)

    def test_an_inconsistent_telegram_429_stays_ambiguous_and_retries_nothing(self):
        self.with_owner()
        key = 'ask:' + 'f00d' * 8
        tell = dict(conversation='123', title='yardmaster asks', text='Lower the test limit?', key=key)
        attempts = []

        def inconsistent(url, body, *args, **kwargs):
            attempts.append(body)
            return 200, dict(ok=False, error_code=429, parameters=dict(retry_after=3))

        self.bridge.request = inconsistent
        status, answer = self.call('/v1/tell', tell)
        self.assertEqual(status, 200)
        self.assertEqual(answer, dict(delivered=False, conversation='123',
                                      reason='ambiguous_delivery_requires_manual_reconciliation',
                                      failure_kind='permanent'))
        self.assertEqual(len(attempts), 1)
        ledger = 'tell:%s:%s' % (self.bridge._tell_scope_digest('123'), key)
        self.assertFalse(b.resendable(self.store.saved_receipt(ledger)))
        # The retry replays the ambiguity instead of sending again.
        status, answer = self.call('/v1/tell', tell)
        self.assertEqual(status, 200)
        self.assertFalse(answer['delivered'])
        self.assertEqual(len(attempts), 1)

    def test_a_retry_with_changed_words_after_a_proven_not_sent_delivers(self):
        self.with_owner()
        self.cut_telegram()
        key = 'ask:' + '56ef' * 8
        prefixed = ('The 20th message this hour, and the last: what comes next waits in yardr inbox.\n'
                    'Lower the test limit?')
        status, answer = self.call('/v1/tell', dict(conversation='123', title='yardmaster asks', text=prefixed,
                                                    key=key))
        self.assertEqual(status, 503)
        # The hour rolled off and the retry lost the prefix: the same ask still delivers.
        self.bridge.request = self.transport
        status, answer = self.call('/v1/tell', dict(conversation='123', title='yardmaster asks',
                                                    text='Lower the test limit?', key=key))
        self.assertEqual(status, 200)
        self.assertTrue(answer['delivered'])
        [(method, body)] = self.telegram_calls()
        self.assertEqual(body['text'], 'yardmaster asks\nLower the test limit?\nTap /decide to answer.')

    def test_a_retry_with_changed_words_after_an_ambiguous_ask_tell_conflicts(self):
        self.with_owner()
        key = 'ask:' + '7890' * 8
        ledger = 'tell:%s:%s' % (self.bridge._tell_scope_digest('123'), key)
        said = 'yardmaster asks\nLower the test limit?\nTap /decide to answer.'
        request = dict(conversation=self.bridge.conversation, idempotency_key=ledger, text=said, text_options=True)
        self.assertIsNone(self.store.reserve(ledger, b.Bridge.delivery_fingerprint(request),
                                             self.bridge.receipt(reason=b.yard_mail.AMBIGUOUS)))
        status, answer = self.call('/v1/tell', dict(conversation='123', title='yardmaster asks',
                                                    text='Raise the test limit?', key=key))
        self.assertEqual(status, 200)
        self.assertEqual(answer, dict(delivered=False, conversation='123', reason='idempotency_key_conflict',
                                      failure_kind='permanent'))
        self.assertEqual(self.telegram_calls(), [])

    def test_an_ordinary_keyed_tell_replays_its_pre_upgrade_receipt(self):
        key = 'm-1'
        said = 'mail from kim\nhello'
        request = dict(conversation=self.bridge.conversation, idempotency_key=key, text=said)
        self.assertIsNone(self.store.reserve(key, b.Bridge.delivery_fingerprint(request),
                                             self.bridge.receipt(reason=b.yard_mail.AMBIGUOUS)))
        self.store.finish(key, self.bridge.receipt(True, message_id='71'))
        status, answer = self.call('/v1/tell', dict(conversation='123', title='mail from kim', text='hello',
                                                    key=key))
        self.assertEqual(status, 200)
        self.assertEqual(answer, dict(delivered=True, message_id='71', conversation='123'))
        self.assertEqual(self.telegram_calls(), [])

    def test_an_asks_shaped_ordinary_keyed_tell_stays_on_its_raw_key(self):
        self.with_owner()
        for title, key in (('mail from Pat asks', 'release-note-7'),
                           ('Pat Example asks', 'release-note-8')):
            with self.subTest(title=title):
                before = len(self.telegram_calls())
                tell = dict(conversation='123', title=title, text='hello', key=key)
                status, answer = self.call('/v1/tell', tell)
                self.assertEqual(status, 200)
                self.assertTrue(answer['delivered'])
                [(method, body)] = self.telegram_calls()[before:]
                self.assertEqual(body['text'], f'{title}\nhello')
                self.assertTrue(self.store.saved_receipt(key)['delivered'])
                scoped = 'tell:%s:%s' % (self.bridge._tell_scope_digest('123'), key)
                self.assertIsNone(self.store.saved_receipt(scoped))
                status, answer = self.call('/v1/tell', tell)
                self.assertEqual(status, 200)
                self.assertTrue(answer['delivered'])
                self.assertEqual(len(self.telegram_calls()), before + 1)

    def test_a_native_ask_replays_its_pre_upgrade_raw_receipt(self):
        self.with_owner()
        key = 'ask:' + 'a1b2' * 8
        said = 'Pat Example asks\nLower the test limit?'
        request = dict(conversation=self.bridge.conversation, idempotency_key=key, text=said)
        self.assertIsNone(self.store.reserve(key, b.Bridge.delivery_fingerprint(request),
                                             self.bridge.receipt(reason=b.yard_mail.AMBIGUOUS)))
        self.store.finish(key, self.bridge.receipt(True, message_id='72'))
        status, answer = self.call('/v1/tell', dict(conversation='123', title='Pat Example asks',
                                                    text='Lower the test limit?', key=key))
        self.assertEqual(status, 200)
        self.assertEqual(answer, dict(delivered=True, message_id='72', conversation='123'))
        self.assertEqual(self.telegram_calls(), [])
        scoped = 'tell:%s:%s' % (self.bridge._tell_scope_digest('123'), key)
        self.assertIsNone(self.store.saved_receipt(scoped))

    def test_a_256_char_explicit_key_on_an_ordinary_tell_still_sends(self):
        key = 'm' * 256
        tell = dict(conversation='123', title='mail from kim', text='hello', key=key)
        status, answer = self.call('/v1/tell', tell)
        self.assertEqual(status, 200)
        self.assertTrue(answer['delivered'])
        self.assertTrue(self.store.saved_receipt(key)['delivered'])
        status, answer = self.call('/v1/tell', tell)
        self.assertEqual(status, 200)
        self.assertTrue(answer['delivered'])
        self.assertEqual(len(self.telegram_calls()), 1)

    def test_a_keyed_ask_tell_off_the_card_owner_proving_not_sent_answers_503(self):
        self.with_owner(cards=False)
        self.cut_telegram()
        key = 'ask:' + '90ab' * 8
        tell = dict(conversation='123', title='yardmaster asks', text='Lower the test limit?', key=key)
        status, answer = self.call('/v1/tell', tell)
        self.assertEqual((status, answer), (503, dict(error='tell unfinished; retry')))
        self.bridge.request = self.transport
        status, answer = self.call('/v1/tell', tell)
        self.assertEqual(status, 200)
        self.assertTrue(answer['delivered'])
        [(method, body)] = self.telegram_calls()
        self.assertEqual(body['text'], 'yardmaster asks\nLower the test limit?')

    def test_an_ask_tell_to_an_unserved_topic_is_refused_and_sends_nothing(self):
        self.with_owner()
        status, answer = self.call('/v1/tell', dict(conversation='123:topic:99', title='yardmaster asks',
                                                    text='Lower the test limit?'))
        self.assertEqual(status, 400)
        self.assertIn('error', answer)
        self.assertEqual(self.telegram_calls(), [])


class RealCoreAskTellTests(BridgeTestCase):
    """Two asks of a real isolated core, plus one from a multi-word actor:
    each its own keyed tell through the real declaration, one pointer each,
    and a retry after a lost response, a minute later or past a bridge
    restart, sends nothing more."""

    call = YardApiTests.call
    telegram_calls = YardApiTests.telegram_calls

    def test_real_core_asks_tell_once_each_and_replay_after_restart_sends_nothing(self):
        binary = testyardr.binary()
        if binary is None:
            self.skipTest('yardr required for the isolated ask tells')
        with tempfile.TemporaryDirectory(prefix='yat-', dir='/tmp') as tmp:
            root = Path(tmp)
            server, home, command, env = testyardr.start(root, binary)

            def cli(*args, stdin=None, actor='pat'):
                run = command if actor == 'pat' else [binary, '--home', str(home), '--actor', actor]
                result = subprocess.run(run + list(args), env=env, cwd=root, input=stdin,
                                        text=True, capture_output=True, timeout=60)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                return result.stdout

            daemon = None
            try:
                cli('init', '--harness', 'claude')
                cli('settings', 'set', 'operator', 'pat')
                self.config.update(yardr_home=str(home), yardr_operator='pat', yardr_cards=True)
                self.bridge = b.Bridge(self.config, self.store, 'SECRET', 'CAPABILITY', request=self.transport,
                                       request_multipart=self.multipart_transport,
                                       request_download=self.download_transport)
                self.port = self.start_callback_server()
                tells = []
                inner = self.bridge.api_tell

                def record(request, key_header=None):
                    tells.append(dict(request))
                    return inner(request, key_header)

                self.bridge.api_tell = record
                pack = Path(__file__).resolve().parent.parent / 'yardr'
                declaration = (pack / 'tgbridge.yaml').read_text().replace(
                    'http://127.0.0.1:8941', f'http://127.0.0.1:{self.port}')
                (root / 'tgbridge.yaml').write_text(declaration)
                shutil.copytree(pack / 'tgbridge', root / 'tgbridge')
                added = subprocess.run(command + ['source', 'add', str(root / 'tgbridge.yaml')], env=env, cwd=root,
                                       text=True, capture_output=True, timeout=60)
                if added.returncode != 0 and 'notify.key' in added.stderr and 'unknown key' in added.stderr:
                    self.skipTest('yardr without per-ask tell keys cannot drive this test')
                self.assertEqual(added.returncode, 0, added.stdout + added.stderr)
                cli('source', 'key', 'tgbridge', stdin='TGBRIDGE_SECRET=CAPABILITY\n')
                cli('user', 'key', 'pat', 'tgbridge', stdin='TGBRIDGE_CONVERSATION=123\n')
                cli('user', 'set', 'pat', 'notify=tgbridge', 'tell=asks')
                bead = cli('bead', 'create', 'two decisions').strip().split()[-1]
                asks = (('pat', 'Lower the test limit?'), ('pat', 'Raise the test limit?'),
                        ('Pat Example', 'Which window?'))
                for actor, question in asks:
                    cli('bead', 'ask', bead, question, '--to', 'pat',
                        '--context', 'first line', '--context', 'second line', actor=actor)
                log = (root / 'daemon.log').open('w')
                daemon = subprocess.Popen(command + ['daemon'], env=env, cwd=root,
                                          stdout=log, stderr=subprocess.STDOUT)
                deadline = time.monotonic() + 90
                while len(tells) < 3 and time.monotonic() < deadline and daemon.poll() is None:
                    time.sleep(0.2)
                log.close()
                tail = '\n'.join((root / 'daemon.log').read_text().splitlines()[-15:])
                self.assertIsNone(daemon.poll(), 'daemon exited before telling:\n' + tail)
                self.assertEqual(len(tells), 3, 'three keyed tells within 90 s:\n' + tail)
                keys = []
                for tell, (actor, question) in zip(tells, asks):
                    self.assertEqual(tell['conversation'], '123')
                    self.assertEqual(tell['title'], f'{actor} asks')
                    self.assertEqual(tell['text'], question)
                    self.assertRegex(tell['key'], r'\Aask:[0-9a-f]{32}\Z')
                    keys.append(tell['key'])
                self.assertEqual(len(set(keys)), 3)
                sent = self.telegram_calls()
                self.assertEqual(len(sent), 3)
                for (_, body), (actor, question) in zip(sent, asks):
                    self.assertEqual(body['text'], f'{actor} asks\n{question}\nTap /decide to answer.')
                    self.assertNotIn('parse_mode', body)
                print('Real core', Path(binary).name, 'ask tells: one pointer per ask, keys',
                      *[key[:12] + '…' for key in keys])
                # A snapshot: the recorder keeps appending on every replay below.
                told = list(tells)
                # A retry after a lost response, in the next minute, sends nothing more.
                with patch.object(b.time, 'time', return_value=6000.0):
                    for tell in told:
                        status, answer = self.call('/v1/tell', tell)
                        self.assertEqual(status, 200)
                        self.assertTrue(answer['delivered'])
                with patch.object(b.time, 'time', return_value=6060.0):
                    for tell in told:
                        status, answer = self.call('/v1/tell', tell)
                        self.assertEqual(status, 200)
                        self.assertTrue(answer['delivered'])
                self.assertEqual(len(self.telegram_calls()), 3)
                # A restart between the send and the retry: the same ledger, a new bridge.
                self.store = b.Store(self.path, 10)
                self.bridge = b.Bridge(self.config, self.store, 'SECRET', 'CAPABILITY', request=self.transport,
                                       request_multipart=self.multipart_transport,
                                       request_download=self.download_transport)
                self.port = self.start_callback_server()
                for tell in told:
                    status, answer = self.call('/v1/tell', tell)
                    self.assertEqual(status, 200)
                    self.assertTrue(answer['delivered'])
                self.assertEqual(len(self.telegram_calls()), 3)
            finally:
                if daemon is not None and daemon.poll() is None:
                    daemon.terminate()
                    try:
                        daemon.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        daemon.kill()
                        daemon.wait(timeout=10)
                testyardr.stop(server)


class RemovedCliCompatTests(unittest.TestCase):
    def test_reply_current_and_session_flag_are_refused(self):
        parser = b._build_parser()
        for argv in (['--settings', '/x.toml', 'reply-current', '--body-file', '/b', '--idempotency-key', 'k'],
                     ['--settings', '/x.toml', 'send', '--session', 'mayor', '--body-file', '/b', '--idempotency-key', 'k']):
            with self.subTest(argv=argv), patch('sys.stderr', new_callable=io.StringIO), self.assertRaises(SystemExit) as raised:
                parser.parse_args(argv)
            self.assertEqual(raised.exception.code, 2)
