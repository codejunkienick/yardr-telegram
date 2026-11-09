"""The yardr side of the bridge (YARDR.md): the inbound body posted to the
yard's hook, the message log behind /v1/read, the yard API on the loopback
port, auto_format and valid_hook_url. Fake transports only; the API runs on
a real loopback server picked by testports."""
import datetime
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bridge as b  # noqa: E402
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

    POST_ROUTES = ('/v1/send', '/v1/tell', '/v1/posted', '/v1/read', '/v1/react', '/v1/pin', '/v1/ask-close', '/v1/run-ask')

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
            *[(dict(key='k', text='x', format=value), 'format must be') for value in (None, '', False, [], {}, 5)],
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
            (dict(text='<b>done</b> &amp; dusted'), 'HTML'),
            (dict(text='<b>done</b>', format='auto'), 'HTML'),
            (dict(text='a < b & c > d'), None),
            (dict(text='use <div> here'), None),
            (dict(text='plain words'), None),
            (dict(text='<b>literal</b>', format='plain'), None),
            (dict(text='<i>x</i>', format='html'), 'HTML'),
            (dict(text='```\necho hello\n```'), None),
            (dict(text='```\n<b>x</b>\n```'), 'HTML'),
            (dict(text='a & b; echo "$x" && ls < input'), None),
        ]
        for index, (body, parse_mode) in enumerate(cases):
            with self.subTest(body=body):
                self.calls.clear()
                status, answer = self.call('/v1/send', dict(body, key=f'fmt-{index}'))
                self.assertEqual(status, 200)
                self.assertTrue(answer['delivered'])
                [(_, sent)] = self.telegram_calls()
                self.assertEqual(sent['text'], body['text'])
                self.assertEqual(sent.get('parse_mode'), parse_mode)

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
    """POST /v1/tell, the yard's notify: no key from the yard, plain text."""

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


class SessionFlagTests(unittest.TestCase):
    def test_the_gc_session_flag_is_accepted_and_ignored(self):
        # The sysmon watch and pricewatch add-ons still pass --session.
        args = b._build_parser().parse_args(['--settings', '/x.toml', 'send', '--session', 'mayor',
                                             '--body-file', '/b', '--idempotency-key', 'k'])
        self.assertEqual((args.command, args.session), ('send', 'mayor'))
        self.assertNotIn('--session', b._build_parser().format_help())
