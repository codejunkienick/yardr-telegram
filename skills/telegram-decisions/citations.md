# telegram-decisions citations

The source file and symbol behind the claims in SKILL.md, one row per claim.
The rows live only here; SKILL.md holds none.

```text
<!-- citations -->
| kind | path | symbol | note |
| pack | yardr/tgbridge.yaml | keyboard:     { type: string, max: 8000, optional: true } | keyboard= adds tap buttons |
| pack | bridge/bridge.py | text_offers_lettered_options | lettered text lines offering a choice |
| pack | bridge/bridge.py | reason='options_need_keyboard' | text options without buttons refused |
| pack | bridge/OPERATIONS.md | options_need_keyboard | the refusal and its override |
| pack | yardr/tgbridge.yaml | text_options: { type: bool, optional: true } | text_options=true for a deliberate text-only list |
| pack | bridge/OPERATIONS.md | do not delete records to force retry | never delete the ledger |
| pack | bridge/OPERATIONS.md | the `[topic: <name>]` line comes before the `[ask answer \| ...]` | a topic ask's answer keeps its line |
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
| pack | yardr/tgbridge.yaml | run_ask: | the native run and supported bundle call |
| pack | yardr/tgbridge.yaml | ask_card: | the canonical ordinary decision call |
| pack | yardr/tgbridge.yaml | siding_card: | the siding status and fold card call |
| pack | bridge/bridge.py | def _api_native_card(self, request, key_header, kind): | native call inputs exclude approval content |
| pack | bridge/bridge.py | def _api_answer(receipt, conversation=None): | delivery receipts report presentation, not a native outcome |
| pack | bridge/OPERATIONS.md | ## Native run cards | canonical content, paired human authority, same-card Approve then Confirm within 60 seconds, no typed-number approval, ledger and refusal fallbacks |
| pack | bridge/OPERATIONS.md | ### Captured single-file skill review | captured skill bounds, supported metadata and complete review delivery |
| pack | bridge/OPERATIONS.md | ### Native group add and set review | supported group runners, settings, prompts and refusals |
| pack | bridge/OPERATIONS.md | ### Source-add declaration and command review | validated declaration and relative command file bundles |
| pack | bridge/OPERATIONS.md | ### Source pack bundles | source-kind directory and embedded pack proposals; terminal fallback |
| pack | bridge/OPERATIONS.md | ## Ordinary native ask cards | canonical ordinary decisions, recipient, refusals, input and recovery |
| pack | bridge/OPERATIONS.md | ## Delivery recovery | definitely unsent same-key retry and ambiguous-delivery reconciliation |
| pack | bridge/OPERATIONS.md | ## Siding status and the plain fold | filters, the exact proposal, status-only cards, subset replies and the undo |
| pack | bridge/bridge.py | def api_siding_card(self, request, key_header=None): | siding call selection, routing, stable key and receipt |
| pack | bridge/OPERATIONS.md | Native selectors ignore decision and refuse open/put-aside asks | native close reads canonical state rather than using caller decision text |
| pack | yardr/tgbridge.yaml | decision:   { type: string, max: 200 } | native ask_close still requires the decision field; use decision=refresh |
| pack | bridge/OPERATIONS.md | tap the action they intend to take. That tap both reconciles | tapping the original card reconciles delivery and may perform the chosen action |
| pack | bridge/OPERATIONS.md | A reply alone cannot recover an unknown message id. | a reply alone cannot reconcile an unbound card |
| pack | bridge/OPERATIONS.md | ask_card_delivery_unconfirmed | native refresh before recovery; tap the original card or decide in the yard and delete it manually |
| pack | bridge/OPERATIONS.md | Do not delete ledger rows or | keep the ledger and original key; never publish under another key to work around ambiguity |
| pack | bridge/OPERATIONS.md | ## /decide: the operator's open decisions | list open decisions and reopen by button or /decide <bead>#<ask> using the same delivery key |
| pack | bridge/OPERATIONS.md | send is reported as unconfirmed and never retried by | /decide reports an unconfirmed send without retrying it |
| pack | yardr/SETUP.md | ## 12. Native run cards (operator, optional) | native bootstrap and explicit per-call grants belong to the operator |
| pack | yardr/SETUP.md | ## 13. Ordinary native decisions (operator; optional) | ask-card setup and supported canonical asks |
| pack | yardr/SETUP.md | ## 14. Optional native siding cards | siding operator bootstrap, activation and rollback |
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
set review”; `yardr/SETUP.md`, “Group add and set”. The `bridge/fixtures/group_*.json`
fixtures were captured from isolated aligned and upstream yardr Bead responses;
`RealNativeTests` writes the groups through them.

Document review bundles: `bridge/run_asks.py` (`review_bundle`, `source_plan`,
`pack_document`, `RunAsks.document_file`, `RunAsks.send_card`);
`bridge/OPERATIONS.md`, “Captured single-file skill review” and “Source pack
bundles”. All new skill/source/pack/group generations deliver one complete
UTF-8 Markdown (.md) document before the compact card; its hash/size and
bundle digest are named on the card. Existing ledger rows retain their
original delivery shape. `bridge/test_run_asks.py` checks real native-store capture, complete
document bytes, confirmation order, drift, replay and legacy resumption;
`scripts/preview_run_asks.py --pack --dense-bundle` is synthetic presentation.
