"""Track operator Telegram message read and done states from the yard.

When [reactions] read is configured (with [yardr] home), each accepted
operator message gets a `read_states` ledger row in state `landed`. A
bounded native round at most once every 60 monotonic seconds checks:

- EventsSince [seq-1, 1] once per message: the configured crew must appear
  in data.deliveries from our source, else the row goes `off`.
- GetCrew {"name": crew} once per distinct crew with `landed` rows (and
  crews with `read` rows when a reply check runs): cursor >= seq moves
  `landed` -> `read` with the configured `read` emoji.
- Events [journal, n] (64..1024) only after a non-reaction/non-readstate
  delivery row newer than the stored mark: `replied` matching seq, source
  and crew moves `landed`/`read` -> `done` with `done` emoji. The mark
  advances after two rounds with no newer send.
- `landed` rows older than 24 h move to `expired` without a reaction.

Every reaction transition holds the reactions flock from row re-read
through Telegram's answer and row update, stepping only forward.
"""
import json
import secrets

import run_asks as n

TABLE = 'read_states'
META_TABLE = 'read_states_meta'
OVERRIDE_TABLE = 'read_state_overrides'
FIELDS = ('message_id', 'conversation_id', 'thread_id', 'seq', 'crew',
          'state', 'accepted_at', 'read_at', 'done_at', 'reason', 'offer_token')
OPEN_STATES = ('landed', 'read')
TERMINAL_STATES = ('done', 'manual', 'off', 'expired')
ROUND_SECONDS = 60
EXPIRE_SECONDS = 24 * 3600
EVENTS_MIN = 64
EVENTS_MAX = 1024
TRANSIENT = (n.RPCUnavailable, n.RPCAmbiguous)
OFFER_LINE = "yardr: Telegram message (event {seq}) waits unread; read with yardr inbox."
OFFER_NOTICES = {
    'read': 'Message read; nothing nudged.',
    'done': 'Message answered; nothing nudged.',
}
OFFER_ENDED_NOTICE = 'Tracking ended; nothing nudged.'
# A delivery row counts for the reply-check send mark only once its receipt
# is no longer retryable: a transient not-sent attempt never left this host,
# and its same-key retry must still trigger reply discovery when it delivers.
# Mirrors bridge.resendable(); the reason string mirrors bridge.NOT_SENT_REASON
# (read_states cannot import bridge, bridge loads this module).
SETTLED_RECEIPT = (
    "NOT (coalesce(json_extract(receipt, '$.delivered'), 1) = 0 "
    "AND coalesce(json_extract(receipt, '$.failure_kind'), '') = 'transient' "
    "AND coalesce(json_extract(receipt, '$.metadata.reason'), '') = 'transport_unavailable_not_sent')"
)


def _format_age(seconds):
    total = max(0, int(seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f'{hours}h {minutes}m' if minutes else f'{hours}h'
    if minutes:
        return f'{minutes}m {secs}s' if secs else f'{minutes}m'
    return f'{secs}s'


class _Off(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


class ReadStates:
    def __init__(self, bridge):
        self.bridge = bridge
        self.store = bridge.store
        self.config = bridge.config
        self.logged = None
        self.not_set_logged = set()
        self.last_round = None
        self._nudge_supported = None
        self._offer_off_logged = set()
        if self.store is not None:
            with self.store.connect() as db:
                if self.enabled:
                    db.execute(
                        f'CREATE TABLE IF NOT EXISTS {TABLE} ('
                        'message_id INTEGER PRIMARY KEY, '
                        'conversation_id TEXT NOT NULL, '
                        'thread_id INTEGER, '
                        'seq INTEGER NOT NULL, '
                        'crew TEXT, '
                        'state TEXT NOT NULL, '
                        'accepted_at REAL NOT NULL, '
                        'read_at REAL, '
                        'done_at REAL, '
                        'reason TEXT, '
                        'offer_token TEXT)'
                    )
                    db.execute(
                        f'CREATE TABLE IF NOT EXISTS {META_TABLE} ('
                        'id INTEGER PRIMARY KEY CHECK (id = 1), '
                        'mark INTEGER NOT NULL DEFAULT 0, '
                        'pending_mark INTEGER NOT NULL DEFAULT 0, '
                        'pending_rounds INTEGER NOT NULL DEFAULT 0, '
                        'accept_mid INTEGER)'
                    )
                    db.execute(
                        f'CREATE TABLE IF NOT EXISTS {OVERRIDE_TABLE} ('
                        'message_id INTEGER PRIMARY KEY, '
                        'state TEXT NOT NULL, '
                        'at REAL NOT NULL)'
                    )

    @property
    def enabled(self):
        return bool(self.bridge.reactions.get('read'))

    @property
    def nudge_after(self):
        return int(self.bridge.reactions.get('nudge_after') or 0)

    def _log_offer_off(self, reason):
        if reason not in self._offer_off_logged:
            self._offer_off_logged.add(reason)
            self.bridge._log(reason)

    @property
    def ra(self):
        return self.bridge.run_asks

    @property
    def source(self):
        return self.config.get('yardr_source', 'tgbridge')

    def now(self):
        return float(self.ra.now())

    def log(self, words):
        # Said once while it stands, not on every poll.
        if words != self.logged:
            self.bridge._log(words)
        self.logged = words

    def _log_not_set(self, message_id, target_state, reason):
        key = (int(message_id), target_state, reason)
        if key not in self.not_set_logged:
            self.not_set_logged.add(key)
            self.bridge._log(f'reaction not set (msg {int(message_id)}): {reason}')

    def get(self, message_id):
        if self.store is None:
            return None
        with self.store.connect() as db:
            if not self.enabled and db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)).fetchone() is None:
                return None
            row = db.execute('SELECT ' + ', '.join(FIELDS) + f' FROM {TABLE} WHERE message_id=?',
                             (int(message_id),)).fetchone()
        return None if row is None else dict(zip(FIELDS, row))

    def past_landed(self, message_id):
        """True when message_id already has a read_states row or override past `landed`."""
        if not self.enabled or self.store is None:
            return False
        mid = int(message_id)
        with self.store.connect() as db:
            row = db.execute(f'SELECT state FROM {TABLE} WHERE message_id=?', (mid,)).fetchone()
            if row is not None:
                return row[0] != 'landed'
            ov = db.execute(f'SELECT state FROM {OVERRIDE_TABLE} WHERE message_id=?', (mid,)).fetchone()
        return ov is not None and ov[0] in ('read', 'done', 'manual')

    def _max_send(self, db):
        return db.execute(
            "SELECT coalesce(max(rowid), 0) FROM deliveries "
            f"WHERE key NOT GLOB 'reaction:*' AND key NOT GLOB 'readstate:*' AND {SETTLED_RECEIPT}"
        ).fetchone()[0]

    def _ensure_meta(self, db):
        db.execute(f'INSERT OR IGNORE INTO {META_TABLE} (id, mark, pending_mark, pending_rounds, accept_mid) '
                   'VALUES (1, 0, 0, 0, NULL)')

    def _save_pending_done(self, db, mid, now):
        ov = db.execute(f'SELECT state FROM {OVERRIDE_TABLE} WHERE message_id=?', (mid,)).fetchone()
        if ov is None:
            db.execute(f"INSERT INTO {OVERRIDE_TABLE} VALUES (?, 'pending_done', ?)", (mid, now))
        elif ov[0] == 'read':
            db.execute(f"UPDATE {OVERRIDE_TABLE} SET state='pending_done', at=? WHERE message_id=?", (now, mid))

    def before_relay(self, message_id):
        """Snapshot the send mark before relaying `message_id` to the yard so
        replies arriving between hook acceptance and record() are not retired."""
        if not self.enabled or self.store is None:
            return
        mid = int(message_id)
        try:
            with self.store.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                self._ensure_meta(db)
                accept_mid = db.execute(f'SELECT accept_mid FROM {META_TABLE} WHERE id=1').fetchone()[0]
                if accept_mid == mid:
                    return
                has_open = db.execute(f"SELECT 1 FROM {TABLE} WHERE state IN ('landed', 'read') LIMIT 1").fetchone()
                if has_open is None:
                    db.execute(
                        f'UPDATE {META_TABLE} SET mark=max(mark, ?), pending_mark=0, pending_rounds=0, accept_mid=? '
                        'WHERE id=1',
                        (self._max_send(db), mid),
                    )
                else:
                    db.execute(f'UPDATE {META_TABLE} SET accept_mid=? WHERE id=1', (mid,))
        except Exception:  # noqa: BLE001 - never block inbound relay
            pass

    def record(self, message_id, conversation_id, thread_id, seq):
        """Insert a `landed` (or pre-overridden) row for an accepted message
        once (INSERT OR IGNORE). When no open rows stand yet and before_relay()
        did not already snapshot for this message, advance the reply-check mark
        past prior deliveries so old sends never trigger an Events read."""
        if not self.enabled or self.store is None or type(seq) is not int or seq <= 0:
            return
        mid = int(message_id)
        accepted_at = self.now()
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            self._ensure_meta(db)
            accept_mid = db.execute(f'SELECT accept_mid FROM {META_TABLE} WHERE id=1').fetchone()[0]
            if db.execute(f'SELECT 1 FROM {TABLE} WHERE message_id=?', (mid,)).fetchone() is not None:
                if accept_mid == mid:
                    db.execute(f'UPDATE {META_TABLE} SET accept_mid=NULL WHERE id=1')
                return
            has_open = db.execute(f"SELECT 1 FROM {TABLE} WHERE state IN ('landed', 'read') LIMIT 1").fetchone()
            if has_open is None and accept_mid != mid:
                db.execute(
                    f'UPDATE {META_TABLE} SET mark=max(mark, ?), pending_mark=0, pending_rounds=0 WHERE id=1',
                    (self._max_send(db),),
                )
            if accept_mid == mid:
                db.execute(f'UPDATE {META_TABLE} SET accept_mid=NULL WHERE id=1')
            ov = db.execute(f'SELECT state, at FROM {OVERRIDE_TABLE} WHERE message_id=?', (mid,)).fetchone()
            state, read_at, done_at = 'landed', None, None
            if ov is not None:
                ov_state, ov_at = ov
                if ov_state == 'done':
                    state, done_at = 'done', ov_at
                    db.execute(f'DELETE FROM {OVERRIDE_TABLE} WHERE message_id=?', (mid,))
                elif ov_state == 'read':
                    state, read_at = 'read', ov_at
                    db.execute(f'DELETE FROM {OVERRIDE_TABLE} WHERE message_id=?', (mid,))
                elif ov_state == 'manual':
                    state = 'manual'
                    db.execute(f'DELETE FROM {OVERRIDE_TABLE} WHERE message_id=?', (mid,))
            db.execute(
                f'INSERT OR IGNORE INTO {TABLE} (' + ', '.join(FIELDS) + ') '
                'VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, NULL, NULL)',
                (mid, conversation_id, thread_id, seq, state, accepted_at, read_at, done_at),
            )

    def _open_rows(self):
        with self.store.connect() as db:
            rows = db.execute(
                'SELECT ' + ', '.join(FIELDS) + f" FROM {TABLE} WHERE state IN ('landed', 'read') ORDER BY message_id"
            ).fetchall()
        return [dict(zip(FIELDS, row)) for row in rows]

    def _has_open(self):
        with self.store.connect() as db:
            return db.execute(f"SELECT 1 FROM {TABLE} WHERE state IN ('landed', 'read') LIMIT 1").fetchone() is not None

    def _pending_done_ids(self):
        with self.store.connect() as db:
            rows = db.execute(
                f"SELECT o.message_id FROM {OVERRIDE_TABLE} o "
                f"JOIN {TABLE} r ON r.message_id = o.message_id "
                f"WHERE o.state = 'pending_done' AND r.state IN ('landed', 'read') ORDER BY o.message_id"
            ).fetchall()
        return [row[0] for row in rows]

    def _set_terminal(self, message_id, expected_states, new_state, reason=None):
        mid = int(message_id)
        with self.bridge._reaction_lock():
            row = self.get(mid)
            if row is None or row['state'] not in expected_states:
                return False
            with self.store.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                db.execute(f'UPDATE {TABLE} SET state=?, reason=? WHERE message_id=? AND state=?',
                           (new_state, reason, mid, row['state']))
                db.execute(f'DELETE FROM {OVERRIDE_TABLE} WHERE message_id=?', (mid,))
        self.close_offer(mid)
        return True

    def transition(self, message_id, target_state):
        """Step one message forward to `read` or `done` under the reactions
        flock: re-read the row, deliver through the ledger under
        readstate:<mid>:<target_state>, and advance the row inside the same
        flock. Transient not-sent leaves the row open (and preserves
        `pending_done` for `done`) for the next round."""
        if not self.enabled or self.store is None:
            return None
        if target_state not in ('read', 'done'):
            raise ValueError('read state target must be read or done')
        emoji = self.bridge.reactions.get(target_state)
        if not emoji:
            return None
        mid = int(message_id)
        key = f'readstate:{mid}:{target_state}'
        allowed = ('landed',) if target_state == 'read' else OPEN_STATES
        advanced = False
        with self.bridge._reaction_lock():
            row = self.get(mid)
            if row is None or row['state'] not in allowed:
                if row is not None and row['state'] not in OPEN_STATES:
                    with self.store.connect() as db:
                        db.execute(f'DELETE FROM {OVERRIDE_TABLE} WHERE message_id=?', (mid,))
                return None
            with self.store.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                if target_state == 'read':
                    ov = db.execute(f'SELECT state FROM {OVERRIDE_TABLE} WHERE message_id=?', (mid,)).fetchone()
                    if ov is not None and ov[0] in ('pending_done', 'done', 'manual'):
                        return None
                else:
                    self._save_pending_done(db, mid, self.now())
            receipt = self.bridge._deliver_reaction(mid, emoji, key, once=False)
            delivered = receipt.get('delivered') is True
            reason = None
            if not delivered:
                reason = (receipt.get('metadata') or {}).get('reason') or 'reaction_failed'
                if self.bridge.resendable(receipt):
                    self._log_not_set(mid, target_state, reason)
                    return receipt
                self.bridge._log(f'reaction not set (msg {mid}): {reason}')
            now = self.now()
            with self.store.connect() as db:
                if target_state == 'read':
                    db.execute(f'UPDATE {TABLE} SET state=?, read_at=?, reason=? WHERE message_id=?',
                               ('read', now, reason, mid))
                else:
                    db.execute('BEGIN IMMEDIATE')
                    db.execute(f'UPDATE {TABLE} SET state=?, done_at=?, reason=? WHERE message_id=?',
                               ('done', now, reason, mid))
                    db.execute(f'DELETE FROM {OVERRIDE_TABLE} WHERE message_id=?', (mid,))
            advanced = True
        if advanced:
            self.close_offer(mid)
        return receipt

    def on_react(self, message_id, emoji):
        """Update an open row (or record a pre-registration override when
        called before record()) inside the caller's reactions flock after a
        delivered non-once reaction."""
        if not self.enabled or self.store is None:
            return
        mid = int(message_id)
        done_emoji = self.bridge.reactions['done']
        read_emoji = self.bridge.reactions.get('read')
        seen_emoji = self.bridge.reactions['seen']
        if emoji == done_emoji:
            target = 'done'
        elif read_emoji and emoji == read_emoji:
            target = 'read'
        elif emoji == seen_emoji:
            return
        else:
            target = 'manual'
        now = self.now()
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute(f'SELECT state FROM {TABLE} WHERE message_id=?', (mid,)).fetchone()
            if row is not None:
                if row[0] not in OPEN_STATES:
                    return
                if target == 'done':
                    db.execute(
                        f"UPDATE {TABLE} SET state='done', done_at=? WHERE message_id=? AND state IN ('landed', 'read')",
                        (now, mid),
                    )
                    db.execute(f'DELETE FROM {OVERRIDE_TABLE} WHERE message_id=?', (mid,))
                elif target == 'read':
                    if row[0] == 'landed':
                        db.execute(f"UPDATE {TABLE} SET state='read', read_at=? WHERE message_id=? AND state='landed'",
                                   (now, mid))
                else:
                    db.execute(
                        f"UPDATE {TABLE} SET state='manual' WHERE message_id=? AND state IN ('landed', 'read')",
                        (mid,),
                    )
                    db.execute(f'DELETE FROM {OVERRIDE_TABLE} WHERE message_id=?', (mid,))
                return
            ov = db.execute(f'SELECT state FROM {OVERRIDE_TABLE} WHERE message_id=?', (mid,)).fetchone()
            if ov is None:
                db.execute(f'INSERT INTO {OVERRIDE_TABLE} VALUES (?, ?, ?)', (mid, target, now))
            elif ov[0] in ('done', 'manual'):
                return
            elif target in ('done', 'manual'):
                db.execute(f'UPDATE {OVERRIDE_TABLE} SET state=?, at=? WHERE message_id=?', (target, now, mid))

    def on_reply(self, reply_to):
        """Move a row to `done` after a delivered /v1/send with reply_to."""
        if not self.enabled or self.store is None or reply_to in (None, ''):
            return
        text = str(reply_to).strip()
        if not text.isdecimal() or int(text) <= 0:
            return
        mid = int(text)
        try:
            with self.store.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                row = db.execute(f'SELECT state FROM {TABLE} WHERE message_id=?', (mid,)).fetchone()
                if row is not None:
                    if row[0] not in OPEN_STATES:
                        return
                    self._save_pending_done(db, mid, self.now())
                else:
                    meta = db.execute(f'SELECT accept_mid FROM {META_TABLE} WHERE id=1').fetchone()
                    if meta is None or meta[0] != mid:
                        return
                    self._save_pending_done(db, mid, self.now())
                    return
            self.transition(mid, 'done')
        except Exception as error:  # noqa: BLE001 - the reply already delivered
            self.bridge._log(f'read state reply update failed (msg {text}): {type(error).__name__}')

    def _check_recipient(self, seq, crew):
        """Verify once via EventsSince [seq-1, 1] that the hook event delivered
        this message from our source to `crew`. Raises TRANSIENT on transient
        RPC errors, or _Off(reason) when permanently ineligible/invalid."""
        try:
            events = self.bridge.yard_client().call('EventsSince', [seq - 1, 1])
        except TRANSIENT:
            raise
        except (n.RPCRefused, n.NativeError) as error:
            raise _Off(getattr(error, 'code', None) or 'events_since_refused') from None
        if not isinstance(events, list) or len(events) != 1:
            raise _Off('hook_event_missing')
        event = events[0]
        if (not isinstance(event, dict)
                or type(event.get('seq')) is not int or event['seq'] != seq
                or event.get('id') != str(seq)
                or event.get('kind') != 'hook'
                or not isinstance(event.get('actor'), str) or not event['actor']
                or not isinstance(event.get('at'), str) or not event['at']):
            raise _Off('invalid_hook_event')
        data = event.get('data')
        if not isinstance(data, dict) or data.get('source') != self.source:
            raise _Off('hook_source_mismatch')
        deliveries = data.get('deliveries')
        if (not isinstance(deliveries, dict)
                or not all(isinstance(k, str) and isinstance(v, str) for k, v in deliveries.items())):
            raise _Off('invalid_hook_deliveries')
        if not deliveries.get(crew):
            raise _Off('crew_not_in_deliveries')

    def _read_crew(self, crew):
        """Call GetCrew {"name": crew} and validate name and non-negative int
        cursor. Raises TRANSIENT on transient errors, _Off on refusal/invalid."""
        try:
            member = self.bridge.yard_client().call('GetCrew', {'name': crew})
        except TRANSIENT:
            raise
        except (n.RPCRefused, n.NativeError) as error:
            raise _Off(getattr(error, 'code', None) or 'crew_refused') from None
        if (not isinstance(member, dict) or member.get('name') != crew
                or type(member.get('cursor')) is not int or member['cursor'] < 0):
            raise _Off('invalid_crew_cursor')
        return member

    def _read_replied_seqs(self, crew, journal, oldest_open_seq):
        """Read Events [journal, n] doubling 64..1024 back to oldest_open_seq
        and return matching replied seqs for (crew, self.source)."""
        limit = EVENTS_MIN
        while True:
            events = self.ra._validate_events(
                self.bridge.yard_client().call('Events', [journal, limit]),
                journal, limit, 'read state reply',
            )
            if not events or len(events) < limit or events[0]['seq'] <= oldest_open_seq or limit >= EVENTS_MAX:
                break
            limit *= 2
        matched = set()
        for event in events:
            if event['kind'] != 'replied':
                continue
            data = event.get('data') or {}
            if (data.get('source') == self.source
                    and data.get('crew') == crew
                    and type(data.get('seq')) is int and data['seq'] > 0):
                matched.add(data['seq'])
        return matched

    def _pending_send(self):
        with self.store.connect() as db:
            meta = db.execute(
                f'SELECT mark, pending_mark, pending_rounds FROM {META_TABLE} WHERE id=1'
            ).fetchone()
            mark, pending_mark, pending_rounds = meta if meta else (0, 0, 0)
            latest = db.execute(
                'SELECT coalesce(max(rowid), 0) FROM deliveries '
                f"WHERE rowid > ? AND key NOT GLOB 'reaction:*' AND key NOT GLOB 'readstate:*' AND {SETTLED_RECEIPT}",
                (mark,),
            ).fetchone()[0]
        return mark, pending_mark, pending_rounds, latest

    def _advance_send_mark(self, pending_mark, pending_rounds, latest):
        with self.store.connect() as db:
            self._ensure_meta(db)
            if latest == pending_mark:
                rounds = pending_rounds + 1
                if rounds >= 2:
                    mark = db.execute(f'SELECT mark FROM {META_TABLE} WHERE id=1').fetchone()[0]
                    waiting = db.execute(
                        "SELECT receipt FROM deliveries WHERE rowid > ? AND rowid <= ? "
                        "AND key NOT GLOB 'reaction:*' AND key NOT GLOB 'readstate:*'",
                        (mark, latest),
                    ).fetchall()
                    if any(self.bridge.resendable(json.loads(row[0])) for row in waiting):
                        db.execute(f'UPDATE {META_TABLE} SET pending_mark=?, pending_rounds=? WHERE id=1',
                                   (latest, rounds))
                        return
                    db.execute(
                        f'UPDATE {META_TABLE} SET mark=max(mark, ?), pending_mark=0, pending_rounds=0 WHERE id=1',
                        (latest,),
                    )
                    return
                db.execute(f'UPDATE {META_TABLE} SET pending_rounds=? WHERE id=1', (rounds,))
                return
            db.execute(
                f'UPDATE {META_TABLE} SET pending_mark=?, pending_rounds=1 WHERE id=1',
                (latest,),
            )

    def _run_round(self):
        trouble = []
        now = self.now()

        pending_done_ids = set(self._pending_done_ids())
        attempted_done = set()
        for mid in sorted(pending_done_ids):
            attempted_done.add(mid)
            receipt = self.transition(mid, 'done')
            if receipt is None or not self.bridge.resendable(receipt):
                pending_done_ids.discard(mid)

        # 4d. Expire `landed` rows older than 24 h before any native calls.
        for row in self._open_rows():
            if (row['state'] == 'landed' and row['message_id'] not in pending_done_ids
                    and now - row['accepted_at'] > EXPIRE_SECONDS):
                self._set_terminal(row['message_id'], ('landed',), 'expired')

        open_rows = self._open_rows()
        if not open_rows:
            return trouble

        # 4a. Check recipient once for rows without a resolved crew.
        recipient_incomplete = False
        for row in open_rows:
            if row['crew'] is not None:
                continue
            crew = self.bridge._quota_agent(self.bridge.conversation_for_id(row['conversation_id']))
            try:
                self._check_recipient(row['seq'], crew)
            except TRANSIENT as error:
                trouble.append(f'read states recipient check (seq {row["seq"]}): {error}')
                recipient_incomplete = True
                continue
            except _Off as off:
                self._set_terminal(row['message_id'], OPEN_STATES, 'off', off.reason)
                trouble.append(f'read states off (msg {row["message_id"]}): {off.reason}')
                continue
            with self.store.connect() as db:
                db.execute(
                    f"UPDATE {TABLE} SET crew=? WHERE message_id=? AND crew IS NULL AND state IN ('landed', 'read')",
                    (crew, row['message_id']),
                )

        all_open = self._open_rows()
        open_rows = [row for row in all_open if row['crew'] is not None]
        if not open_rows:
            return trouble

        mark, pending_mark, pending_rounds, latest_send = self._pending_send()
        want_reply_check = latest_send > 0
        # A row still without a crew cannot be reply-checked this round; keep
        # the send mark so its already-delivered reply is still found later.
        unresolved = recipient_incomplete or len(open_rows) != len(all_open)

        # 4b. One GetCrew per distinct crew with `landed` rows (plus `read`-only crews if reply check runs).
        landed_crews = {row['crew'] for row in open_rows
                        if row['state'] == 'landed' and row['message_id'] not in pending_done_ids}
        crews_to_read = sorted(landed_crews | ({row['crew'] for row in open_rows} if want_reply_check else set()))
        crew_info = {}
        reply_check_incomplete = unresolved and want_reply_check
        for crew in crews_to_read:
            try:
                crew_info[crew] = self._read_crew(crew)
            except TRANSIENT as error:
                trouble.append(f'read states crew {crew}: {error}')
                if want_reply_check:
                    reply_check_incomplete = True
            except _Off as off:
                for row in open_rows:
                    if row['crew'] == crew and row['state'] == 'landed':
                        self._set_terminal(row['message_id'], ('landed',), 'off', off.reason)
                trouble.append(f'read states crew {crew} off: {off.reason}')

        # 4c. Reply check when a non-reaction/non-readstate delivery is newer than mark.
        if want_reply_check:
            open_by_crew = {}
            for row in open_rows:
                open_by_crew.setdefault(row['crew'], []).append(row)
            for crew in sorted(open_by_crew):
                member = crew_info.get(crew)
                if member is None:
                    continue
                journal = member.get('journal')
                if not isinstance(journal, str) or not journal:
                    trouble.append(f'read states crew {crew}: invalid native crew journal')
                    continue
                crew_rows = open_by_crew[crew]
                oldest_open_seq = min(row['seq'] for row in crew_rows)
                try:
                    replied_seqs = self._read_replied_seqs(crew, journal, oldest_open_seq)
                except TRANSIENT as error:
                    reply_check_incomplete = True
                    trouble.append(f'read states events {crew}: {error}')
                    continue
                except (n.RPCRefused, n.NativeError) as error:
                    trouble.append(f'read states events {crew}: {error}')
                    continue
                for row in crew_rows:
                    mid = row['message_id']
                    if row['seq'] in replied_seqs and mid not in attempted_done:
                        attempted_done.add(mid)
                        receipt = self.transition(mid, 'done')
                        if receipt is not None and self.bridge.resendable(receipt):
                            pending_done_ids.add(mid)
            if not reply_check_incomplete:
                self._advance_send_mark(pending_mark, pending_rounds, latest_send)

        # 4b transitions for rows still in `landed`.
        for crew, member in crew_info.items():
            cursor = member['cursor']
            for row in open_rows:
                mid = row['message_id']
                if (row['crew'] == crew and row['state'] == 'landed' and row['seq'] <= cursor
                        and mid not in pending_done_ids and mid not in attempted_done):
                    self.transition(mid, 'read')

        self._close_stale_offers()
        self._offer_step(now, crew_info, trouble)
        return trouble

    def _cancel_offer(self, rs_row):
        token = rs_row['offer_token']
        tn = self.bridge.typed_nudge
        tn_row = tn.get(token)
        if tn_row is None or tn_row['state'] != 'open':
            return
        if tn_row.get('message') is None:
            saved = self.store.saved_receipt(f'typed-nudge:{token}:send')
            if saved is not None and saved.get('delivered') is True and saved.get('message_id') is not None:
                tn_row['message'] = int(saved['message_id'])
        notice = OFFER_NOTICES.get(rs_row['state'], OFFER_ENDED_NOTICE)
        tn_row.update(state='cancelled', notice=notice)
        tn.save(tn_row)
        if tn_row.get('message') is not None:
            tn.display(tn_row)

    def _close_offer_row(self, rs_row):
        token = rs_row['offer_token']
        if not token:
            return
        with self.ra.lock():
            latest = self.get(rs_row['message_id'])
            if latest is None or latest['state'] == 'landed' or latest['offer_token'] != token:
                return
            self._cancel_offer(latest)

    def close_offer(self, message_id):
        if not self.enabled or self.store is None:
            return
        row = self.get(int(message_id))
        if row is None or not row['offer_token'] or row['state'] == 'landed':
            return
        self._close_offer_row(row)

    def sync_offer(self, token):
        """Close an open offer row when called under ra.lock() if its read_states row left landed."""
        if not self.enabled or self.store is None:
            return
        with self.store.connect() as db:
            raw = db.execute(
                'SELECT ' + ', '.join(FIELDS) + f' FROM {TABLE} WHERE offer_token=?', (token,)
            ).fetchone()
        if raw is None:
            return
        rs_row = dict(zip(FIELDS, raw))
        if rs_row['state'] == 'landed':
            return
        self._cancel_offer(rs_row)

    def _close_stale_offers(self):
        if not self.enabled or self.store is None:
            return
        with self.store.connect() as db:
            rows = db.execute(
                'SELECT ' + ', '.join(FIELDS) + f" FROM {TABLE} WHERE offer_token IS NOT NULL AND state != 'landed'"
            ).fetchall()
        for raw in rows:
            rs_row = dict(zip(FIELDS, raw))
            tn_row = self.bridge.typed_nudge.get(rs_row['offer_token'])
            if tn_row is not None and tn_row['state'] == 'open':
                self._close_offer_row(rs_row)

    def _ensure_nudge_supported(self, trouble):
        if self._nudge_supported is not None:
            return self._nudge_supported
        try:
            supported = self.bridge.typed_nudge.probe()
        except n.NativeError as error:
            trouble.append(f'read states offer probe: {error}')
            return False
        if not supported:
            self._nudge_supported = False
            self._log_offer_off('nudge offers off: this yard core has no session nudge')
            return False
        self._nudge_supported = True
        return True

    def _retry_offer_send(self, rs_row, trouble):
        token = rs_row['offer_token']
        tn = self.bridge.typed_nudge
        with self.ra.lock():
            latest = self.get(rs_row['message_id'])
            if latest is None or latest['state'] != 'landed' or latest['offer_token'] != token:
                return
            tn_row = tn.get(token)
            if tn_row is None or tn_row['state'] != 'open' or tn_row.get('message') is not None:
                return
            saved = self.store.saved_receipt(f'typed-nudge:{token}:send')
            is_new_send = saved is None or self.bridge.resendable(saved)
            if is_new_send and not self._ensure_nudge_supported(trouble):
                return
            receipt = tn.send_confirm(tn_row)
            if receipt.get('delivered') is True:
                tn_row['message'] = int(receipt['message_id'])
                tn.save(tn_row)
                return
            if self.bridge.resendable(receipt):
                trouble.append(f'read states offer (msg {rs_row["message_id"]}): send not confirmed yet')
                return
            reason = (receipt.get('metadata') or {}).get('reason') or 'delivery not confirmed'
            tn_row.update(state='unknown', notice=f'Offer not sent ({reason}).')
            tn.save(tn_row)
            self.bridge._log(f'read states offer {token}: nudge card not sent: {reason}')
            trouble.append(f'read states offer {token}: {reason}')

    def _offer_step(self, now, crew_info, trouble):
        if self.nudge_after <= 0:
            return
        if not self.bridge.native_nudge() or not self.config.get('yardr_operator'):
            self._log_offer_off('nudge offers off: built-in /nudge or [yardr] operator unavailable')
            return
        if self._nudge_supported is False:
            return
        landed_rows = [row for row in self._open_rows() if row['state'] == 'landed']
        if not landed_rows:
            return
        for row in landed_rows:
            if row['offer_token']:
                self._retry_offer_send(row, trouble)
        if self._nudge_supported is False:
            return
        with self.store.connect() as db:
            offered = db.execute(
                f'SELECT crew, offer_token FROM {TABLE} WHERE offer_token IS NOT NULL AND crew IS NOT NULL'
            ).fetchall()
        crews_with_open_offer = set()
        tn = self.bridge.typed_nudge
        for crew, token in offered:
            tn_row = tn.get(token)
            if tn_row is not None and tn_row['state'] == 'open':
                crews_with_open_offer.add(crew)

        for row in landed_rows:
            if row['offer_token'] is not None or not row['crew']:
                continue
            if now - row['accepted_at'] < self.nudge_after:
                continue
            if row['crew'] in crews_with_open_offer:
                continue
            member = crew_info.get(row['crew'])
            if not member or member.get('state') != 'running' or member.get('status') not in ('idle', 'blocked'):
                continue
            if self.bridge.decide_conversation(row['thread_id']) is None:
                continue
            if not self._ensure_nudge_supported(trouble):
                return
            token = secrets.token_urlsafe(12)
            header = f"Unread for {_format_age(now - row['accepted_at'])}"
            words = OFFER_LINE.format(seq=int(row['seq']))

            def save_offer(tn_row, message_id=row['message_id']):
                with self.store.connect() as db:
                    db.execute('BEGIN IMMEDIATE')
                    cur = db.execute(
                        f'SELECT state, offer_token FROM {TABLE} WHERE message_id=?', (message_id,)
                    ).fetchone()
                    if cur is None or cur[0] != 'landed' or cur[1] is not None:
                        raise n.NativeError('read state changed before offer')
                    db.execute(
                        f"UPDATE {TABLE} SET offer_token=? WHERE message_id=? AND state='landed' AND offer_token IS NULL",
                        (tn_row['token'], message_id),
                    )
                    tn.save(tn_row, db=db)

            try:
                with self.ra.lock():
                    tn.open_confirm(
                        0, row['crew'], words, row['thread_id'], user='',
                        token=token, reply_to=row['message_id'], header=header, silent=True,
                        before_send=save_offer, probe=False,
                    )
                crews_with_open_offer.add(row['crew'])
            except n.NativeError as error:
                tn_row = tn.get(token)
                if tn_row is None:
                    trouble.append(f'read states offer (msg {row["message_id"]}): {error}')
                    continue
                receipt = self.store.saved_receipt(f'typed-nudge:{token}:send')
                if receipt is not None and self.bridge.resendable(receipt):
                    crews_with_open_offer.add(row['crew'])
                    trouble.append(f'read states offer (msg {row["message_id"]}): send not confirmed yet')
                    continue
                reason = (receipt.get('metadata') or {}).get('reason') if receipt else 'delivery not confirmed'
                tn_row.update(state='unknown', notice=f'Offer not sent ({reason}).')
                tn.save(tn_row)
                self.bridge._log(f'read states offer {token}: nudge card not sent: {reason}')
                trouble.append(f'read states offer {token}: {reason}')

    def refresh(self):
        if not self.enabled or self.bridge.yard_client() is None or not self.config.get('yardr_home'):
            return
        try:
            if self.nudge_after > 0 and (not self.bridge.native_nudge() or not self.config.get('yardr_operator')):
                self._log_offer_off('nudge offers off: built-in /nudge or [yardr] operator unavailable')
            self._close_stale_offers()
            now_mono = self.bridge.monotonic()
            if self.last_round is not None and now_mono - self.last_round < ROUND_SECONDS:
                return
            if not self._has_open():
                return
            self.last_round = now_mono
            trouble = self._run_round()
            if trouble:
                self.log(trouble[0])
            else:
                self.logged = None
        except Exception as error:  # noqa: BLE001 - never break poll_once
            self.log(f'read states failed: {type(error).__name__}')
