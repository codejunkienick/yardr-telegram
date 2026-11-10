# telegram-formatting citations

The source file and symbol behind the claims in SKILL.md, one row per claim.
The rows live only here; SKILL.md holds none.

```text
<!-- citations -->
| kind | path | symbol | note |
| pack | yardr/tgbridge.yaml | name: tgbridge | the yard reaches the chat as the source tgbridge |
| pack | yardr/tgbridge/normalize | 'title': 'Telegram' if topic == 'general' else f'Telegram: {topic}' | the delivery's line in the inbox names the topic |
| pack | yardr/tgbridge.yaml | and the yardmaster takes it as that user's word. | the text is the operator's word |
| pack | YARDR.md | The yard keeps at most 500 characters of a delivery's text | the inbox keeps 500 characters |
| pack | yardr/tgbridge.yaml | read:   [tgbridge/thread, read] | yardr thread show reads it whole |
| pack | yardr/tgbridge/thread | payload = {'conversation': thread, 'text': given.get('body') or '', 'key': key, 'format': 'auto'} | yardr reply answers in the delivery's own conversation, format auto |
| pack | bridge/bridge.py | def auto_format(text): | valid HTML goes formatted, anything else plain |
| pack | yardr/tgbridge/thread | payload['attachments'] = files | --file sends files with the reply |
| pack | bridge/bridge.py | def infer_attachment_kind(path): | pictures as photos, anything else as documents |
| pack | yardr/tgbridge.yaml | posted: [tgbridge/thread, posted] | a reply run again finds the first one |
| pack | bridge/bridge.py | def api_posted(self, request, key_header=None): | the bridge finds a confirmed send by its key |
| pack | yardr/tgbridge.yaml | about: One message to the chat or one of its topics (Telegram HTML or plain, buttons, silent, one file) | the send call |
| pack | bridge/bridge.py | def _api_conversation(self, request): | topic=<name>, general or conversation=<id> |
| pack | bridge/OPERATIONS.md | With neither, a send lands in General. | neither is the main chat |
| pack | bridge/bridge.py | fmt = request.get('format') or 'auto' | format=auto is the default |
| pack | yardr/tgbridge.yaml | silent:       { type: bool, optional: true } | silent=true |
| pack | bridge/OPERATIONS.md | Silent messages (`--silent`) | silent sends follow this section |
| pack | bridge/OPERATIONS.md | Users will receive a notification with no sound | a silent notification still appears |
| pack | bridge/bridge.py | body['silent'] = True | the yard API's silent |
| pack | bridge/bridge.py | extra['disable_notification'] = True | notification with no sound |
| pack | bridge/OPERATIONS.md | the same key with and without it is `idempotency_key_conflict` | silent is part of the key's identity |
| pack | yardr/tgbridge.yaml | keyboard:     { type: string, max: 8000, optional: true } | keyboard= adds tap buttons |
| pack | yardr/tgbridge.yaml | attachment:   { type: string, pattern: "^/", max: 4096, optional: true } | attachment= sends one file |
| pack | bridge/bridge.py | text_offers_lettered_options | lettered text lines offering a choice |
| pack | bridge/bridge.py | reason='options_need_keyboard' | text options without buttons refused |
| pack | bridge/OPERATIONS.md | options_need_keyboard | the refusal and its override |
| pack | yardr/tgbridge.yaml | text_options: { type: bool, optional: true } | text_options=true for a deliberate text-only list |
| pack | bridge/bridge.py | def _api_answer(receipt, conversation=None): | --json prints delivered, message_id, reason |
| pack | yardr/tgbridge.yaml | idempotency: Idempotency-Key | the yard sends each call with a key |
| pack | bridge/bridge.py | key = request.get('key') or key_header | key=<key> wins over the yard's key |
| pack | bridge/bridge.py | idempotency key reused for a different request | same key, same body |
| pack | bridge/bridge.py | ambiguous_delivery_requires_manual_reconciliation | ambiguous result |
| pack | bridge/bridge.py | NOT_SENT_REASON = 'transport_unavailable_not_sent' | not-sent result |
| pack | bridge/OPERATIONS.md | do not delete records to force retry | never delete the ledger |
| pack | bridge/bridge.py | text_required_maximum_4096_utf16_units | 4096 UTF-16 units |
| pack | bridge/bridge.py | forwarded material, not the user's instruction] | forwarded entry header |
| pack | bridge/bridge.py | return '\n'.join('> ' + line for line in text.splitlines()) | forwarded text quoted line by line |
| pack | bridge/OPERATIONS.md | A forward never runs a command | forwards are material, never commands |
| pack | bridge/OPERATIONS.md | ## Topics | topics follow this section |
| pack | bridge/bridge.py | f"[topic: {topic['name']}]\n{text}" | the topic line first |
| pack | bridge/bridge.py | def subscribe_line(self, crew, topic): | the yard routes each topic by subscription |
| pack | bridge/OPERATIONS.md | ### Topics the user creates | topics the user created follow this section |
| pack | bridge/bridge.py | (t for t in self.served_auto_topics() if t['display_name'] == topic), None) | topic=<name> also names a topic the operator created |
| pack | bridge/bridge.py | is not configured, created or served | an unknown topic is refused |
| pack | bridge/OPERATIONS.md | not configured: reply lands in the main chat | not configured answers in the main chat |
| pack | bridge/OPERATIONS.md | retried and never resent into General | a deleted topic's send is rejected |
| pack | bridge/OPERATIONS.md | the `[topic: <name>]` line comes before the `[ask answer \| ...]` | a topic ask's answer keeps its line |
| pack | bridge/OPERATIONS.md | "Reactions" | reactions follow this section |
| pack | bridge/OPERATIONS.md | the emoji for "the yard has the message" (received, not read) | seen means received, not read |
| pack | bridge/bridge.py | REACTIONS_DEFAULTS = dict(on_accept=False, seen='\U0001F440', done='\U0001F44C') | 👀 and 👌 defaults, off unless on_accept |
| pack | bridge/bridge.py | def _accepted(self, mid, conversation_id, thread_id): | the bridge sets seen at the accept point |
| pack | yardr/tgbridge.yaml | about: React to one of the operator's messages (seen, done, none or an emoji) | the react call |
| pack | bridge/OPERATIONS.md | the emoji for "the work this message asked for is finished or answered" | react done when finished or answered |
| pack | yardr/tgbridge.yaml | body: { reaction: "{reaction}", message_id: "{message_id}", topic: "{topic}", conversation: "{conversation}" } | react takes topic, conversation or message_id |
| pack | bridge/bridge.py | message_id = self.store.last_accepted_in(conversation['conversation_id']) | topic= targets that topic's last accepted message |
| pack | bridge/bridge.py | message_id = self.store.last_accepted() | with neither, the last accepted message anywhere |
| pack | bridge/OPERATIONS.md | ✅ and ✔ are not on it | no check mark reaction |
| pack | bridge/bridge.py | reaction=[dict(type='emoji', emoji=emoji)] if emoji else [] | none clears the reaction |
| pack | bridge/bridge.py | HTML_TAGS = ('b', 'strong', 'i', 'em', 'u', 's', 'code', 'pre', 'a', 'blockquote', 'tg-spoiler') | tag allowlist |
| pack | bridge/bridge.py | HTML_LINK_SCHEMES = ('http', 'https', 'tg') | link schemes |
| pack | bridge/bridge.py | 'html_text_not_escaped' | unescaped text refused |
| pack | bridge/bridge.py | agent = _text_setting(data.get('crew', 'yardmaster'), 'crew') | the yard's crew member, default yardmaster |
| pack | bridge/bridge.py | telegram_html_parse_rejected_400 | one plain fallback, same key |
| pack | bridge/markdown.py | def render(source, source_max_units): | pure bounded CommonMark display conversion |
| pack | bridge/bridge.py | def formatted_text(text, fmt): | generated HTML uses the existing validator |
| pack | yardr/tgbridge.yaml | formats: [markdown] | explicit ordinary reply capability |
| pack | yardr/tgbridge/thread | fmt = given.get('format', 'auto') | explicit mode, legacy auto when omitted |
| pack | docs/formatting.md | ## Rendering policy | supported subset, whitespace and evidence boundaries |
| pack | vendor/README.md | # Pinned Markdown runtime | pins, notices and offline packaging |
| pack | bridge/bridge.py | def _log_sent(self, conversation, request, receipt): | the message log records the send |
| pack | bridge/OPERATIONS.md | ## Asks | asks with buttons follow this section |
| pack | bridge/OPERATIONS.md | each question of a multi-question ask goes out as its own message with its own key and keyboard | one message per question |
| pack | bridge/bridge.py | raise ApiError('keyboard must be JSON rows') from None | keyboard= takes the rows as JSON |
| pack | bridge/bridge.py | _ask_id_for_key | first 16 hex of the SHA-256 of the key |
| pack | bridge/OPERATIONS.md | ask:<ask-id>:<choice> within 64 bytes | callback data shape |
| pack | bridge/OPERATIONS.md | An ask button whose id is not the send's derived id refuses the send | id from another key refused |
| pack | bridge/OPERATIONS.md | at most 8 buttons per row and at most 100 buttons in total | keyboard size limits |
| pack | bridge/bridge.py | CALLBACK_DATA_MAX_BYTES = 64 | 64-byte callback data |
| pack | bridge/bridge.py | '\|' in text or ']' in text | label holds no pipe or bracket |
| pack | bridge/bridge.py | _ask_text_breaks_answer_line | no line break or control in a label |
| pack | bridge/OPERATIONS.md | Two buttons sharing one callback_data refuse the send | no shared callback data |
| pack | bridge/bridge.py | utf16_units(visible + '\n✓ ' + longest) > limit | room for the choice suffix |
| pack | bridge/OPERATIONS.md | 1024 UTF-16 units of caption for a photo ask | photo room limit |
| pack | bridge/OPERATIONS.md | An ask question is never refused for its characters | free-text question |
| pack | bridge/OPERATIONS.md | A keyboard may go with no attachment or with exactly one photo | text or one photo only |
| pack | bridge/OPERATIONS.md | A keyboard on a photo holds only ask buttons | photo keyboard ask-only |
| pack | bridge/bridge.py | thread_id=thread_id, kind='answer') | the tap's delivery is kind answer |
| pack | bridge/bridge.py | f'[ask answer \| choice: {label} \| message: {mid}]\nquestion: {visible}' | relayed answer shape |
| pack | bridge/OPERATIONS.md | A6 Closing with a decision | closing an ask follows this section |
| pack | yardr/tgbridge.yaml | about: Close an open button ask with the decision made some other way | the ask_close call |
| pack | bridge/bridge.py | def api_ask_close(self, request, key_header=None): | the close through the yard API |
| pack | bridge/bridge.py | answer['message_id'] = str(receipt['message_id']) | the ask send's message_id |
| pack | yardr/tgbridge.yaml | ask_key:    { type: string, max: 256, optional: true } | ask_key= instead of message_id= |
| pack | bridge/OPERATIONS.md | selects it instead when only the key is known | the ask send's key selects it |
| pack | bridge/bridge.py | ASK_DECIDED_PREFIX = '\n✓ Decided: ' | decided line under the ask |
| pack | bridge/OPERATIONS.md | then the `Already answered` answer; nothing is relayed. | a later tap is refused |
| pack | bridge/bridge.py | reason='decision_breaks_line' | one-line decision |
| pack | bridge/bridge.py | utf16_units(visible + self.ASK_DECIDED_PREFIX + decision) > limit | room for the decision |
| pack | bridge/bridge.py | reason='ask_already_answered' | a tap came first |
| pack | bridge/OPERATIONS.md | never a second edit | a repeat never edits twice |
| pack | bridge/OPERATIONS.md | ## Pinned messages | pinned slots follow this section |
| pack | yardr/tgbridge.yaml | about: Keep one pinned message per slot at the top of the chat, edited in place | the pin call |
| pack | bridge/bridge.py | PIN_SLOT_RE = re.compile(r'[a-z0-9_-]{1,32}') | slot names |
| pack | bridge/OPERATIONS.md | The body follows the `sendMessage` rules | body follows the send rules |
| pack | bridge/bridge.py | answer['changed'] = bool(receipt['changed']) | same text changes nothing (changed: false) |
| pack | bridge/OPERATIONS.md | Another body: the revision goes up by one and the live message is edited | new text edits in place |
| pack | bridge/bridge.py | extra={'disable_notification': True} | the first update sends silently |
| pack | bridge/OPERATIONS.md | Each new pin (a new generation) adds Telegram's one "pinned" | one pinned line per new pin |
| pack | bridge/OPERATIONS.md | an edit adds nothing and never notifies | edits never notify |
| pack | yardr/tgbridge.yaml | body: { slot: "{slot}", unpin: true } | the unpin call |
| pack | bridge/OPERATIONS.md | is attempted again by the next update | rerun after a failure |
| pack | bridge/OPERATIONS.md | one generation never sends a second message | no second message per slot |
| pack | bridge/OPERATIONS.md | Reconciling an unconfirmed first send: check the chat for that message | unpin after checking the chat |
| pack | bridge/bridge.py | ended_generation=generation | unpin ends an unconfirmed send |
| pack | bridge/OPERATIONS.md | "message to pin not found" (the user deleted the message before its pin | a deleted message is replaced |
<!-- /citations -->
```


Native run/skill approval: `bridge/run_asks.py` (`validate`, `validate_skill`,
`skill_bundle`, `RunAsks.send_card`, `callback`, `reply`, `decide`, `settle`,
`refresh`); `bridge/OPERATIONS.md`, “Captured single-file skill review”;
`yardr/SETUP.md`, “Single-file skill snapshots”. The fixture
`bridge/fixtures/skill_add.json` was captured from a real isolated core Bead
response; real CLI/RPC/carry coverage is in `bridge/test_run_asks.py`.

Native group cards: `bridge/run_asks.py` (`parse_group`, `GroupCard`,
`group_bundle`, `review_bundle`); `bridge/OPERATIONS.md`, “Native group add and
set review”; `yardr/SETUP.md`, “Herdr group add and set”. The fixtures
`bridge/fixtures/group_add.json` and `group_set.json` were captured from a real
isolated core Bead response; `RealNativeTests` writes the groups through it.

Document review bundles: `bridge/run_asks.py` (`review_bundle`, `source_plan`,
`pack_document`, `RunAsks.document_file`, `RunAsks.send_card`);
`bridge/OPERATIONS.md`, “Captured single-file skill review” and “Source pack
bundles”. All new skill/source/pack/group generations deliver one complete
UTF-8 Markdown (.md) document before the compact card; its hash/size and
bundle digest are named on the card. Existing ledger rows retain their
original delivery shape. `bridge/test_run_asks.py` checks real native-store capture, complete
document bytes, confirmation order, drift, replay and legacy resumption;
`scripts/preview_run_asks.py --pack --dense-bundle` is synthetic presentation.
