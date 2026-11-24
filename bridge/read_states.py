"""Track operator Telegram message read and done states from the yard.

When [reactions] read is configured (with [yardr] home), each accepted
operator message gets a `read_states` ledger row in state `landed`. While the
bridge runs, a stream worker follows `GET /events?after=<seq>` on the native
socket to transition rows at once on `inbox_shown`, `inbox_read` (line session
`""`) and `replied` (and tracks `crew_status` / `crew_stopped` for stream typing
without changing any reaction), while a fallback round runs at most once every
5 monotonic seconds only while open rows exist:

- EventsSince [seq-1, 1] once per message: the configured crew must appear
  in data.deliveries from our source, else the row goes `off`.
- GetCrew {"name": crew} once per distinct crew with `landed` rows (and
  crews with open rows when a reply check runs): cursor >= seq moves
  `landed` -> `read` with the configured `read` emoji.
- Events [journal, n] (64..1024) only after a non-reaction/non-readstate
  delivery row newer than the stored mark: `replied` matching seq, source
  and crew moves `landed`/`read` -> `done` with `done` emoji. The mark
  advances after two rounds with no newer send.
- `landed` rows older than 24 h move to `expired` without a reaction.

Any legacy ledger row still in `working` settles once to `read` on the first
refresh after upgrade. Every reaction transition holds the reactions flock
from row re-read through Telegram's answer and row update, stepping only
forward.
"""
import collections
import json
import secrets
import socket
import threading
import time

import run_asks as n

TABLE = 'read_states'
META_TABLE = 'read_states_meta'
OVERRIDE_TABLE = 'read_state_overrides'
FIELDS = ('message_id', 'conversation_id', 'thread_id', 'seq', 'crew',
          'state', 'accepted_at', 'read_at', 'done_at', 'reason', 'offer_token', 'turn')
OPEN_STATES = ('landed', 'read')
LEGACY_OPEN_STATES = ('landed', 'read', 'working')
TERMINAL_STATES = ('done', 'manual', 'off', 'expired')
CREW_STATUSES = ('working', 'idle', 'blocked', 'unknown')
ROUND_SECONDS = 5
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
        self._last_round_wall = None
        self._nudge_supported = None
        self._offer_off_logged = set()
        self._state_lock = threading.Lock()
        self._round_lock = threading.Lock()
        self._stream_cv = threading.Condition(self._state_lock)
        self._poll_wake = threading.Event()
        self._worker_stop = threading.Event()
        self._stream_thread = None
        self._poll_thread = None
        self._stream_sock = None
        self._stream_started = False
        self._stream_connected = False
        self._stream_broken = False
        self._stream_start_seq = None
        self._last_seq = None
        self._inbox_read_seen = False
        self._fallback_logged = False
        self._stream_logged = None
        self._stream_inbox_read = {}
        self._stream_replied = set()
        self._stream_replied_order = collections.deque(maxlen=512)
        self._crew_status = {}
        self._crew_turn_seq = {}
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
                        'offer_token TEXT, '
                        'turn INTEGER NOT NULL DEFAULT 0)'
                    )
                    cols = {row[1] for row in db.execute(f'PRAGMA table_info({TABLE})').fetchall()}
                    if 'turn' not in cols:
                        db.execute(f'ALTER TABLE {TABLE} ADD COLUMN turn INTEGER NOT NULL DEFAULT 0')
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
        with self._state_lock:
            if reason in self._offer_off_logged:
                return
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
        with self._state_lock:
            changed = words is not None and words != self.logged
            self.logged = words
        if changed:
            self.bridge._log(words)

    def _log_not_set(self, message_id, target_state, reason):
        key = (int(message_id), target_state, reason)
        with self._state_lock:
            if key in self.not_set_logged:
                return
            self.not_set_logged.add(key)
        self.bridge._log(f'reaction not set (msg {int(message_id)}): {reason}')

    def _log_stream_fallback(self, words):
        with self._state_lock:
            if self._fallback_logged:
                return
            self._fallback_logged = True
        self.bridge._log(words)

    def _log_stream_error(self, words):
        with self._state_lock:
            changed = words is not None and words != self._stream_logged
            self._stream_logged = words
        if changed:
            self.bridge._log(words)

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
                has_open = db.execute(
                    f"SELECT 1 FROM {TABLE} WHERE state IN ('landed', 'read', 'working') LIMIT 1"
                ).fetchone()
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
            has_open = db.execute(
                f"SELECT 1 FROM {TABLE} WHERE state IN ('landed', 'read', 'working') LIMIT 1"
            ).fetchone()
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
                'VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, NULL, NULL, 0)',
                (mid, conversation_id, thread_id, seq, state, accepted_at, read_at, done_at),
            )
        self._poll_wake.set()

    def _open_rows(self):
        with self.store.connect() as db:
            rows = db.execute(
                'SELECT ' + ', '.join(FIELDS)
                + f" FROM {TABLE} WHERE state IN ('landed', 'read', 'working') ORDER BY message_id"
            ).fetchall()
        return [dict(zip(FIELDS, row)) for row in rows]

    def _has_open(self):
        with self.store.connect() as db:
            return db.execute(
                f"SELECT 1 FROM {TABLE} WHERE state IN ('landed', 'read', 'working') LIMIT 1"
            ).fetchone() is not None

    def _pending_done_ids(self):
        with self.store.connect() as db:
            rows = db.execute(
                f"SELECT o.message_id FROM {OVERRIDE_TABLE} o "
                f"JOIN {TABLE} r ON r.message_id = o.message_id "
                f"WHERE o.state = 'pending_done' AND r.state IN ('landed', 'read', 'working') ORDER BY o.message_id"
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
        self.bridge.typing_wake.set()
        return True

    def transition(self, message_id, target_state):
        """Step one message to `read` or `done` under the reactions flock:
        re-read the row, deliver through the ledger under readstate:<mid>:<state>
        (or readstate:<mid>:read:settle for legacy `working` rows), and update the
        row inside the same flock. Transient not-sent leaves the row unchanged."""
        if not self.enabled or self.store is None:
            return None
        if target_state not in ('read', 'done'):
            raise ValueError('read state target must be read or done')
        emoji = self.bridge.reactions.get(target_state)
        if not emoji:
            return None
        mid = int(message_id)
        allowed = ('landed', 'working') if target_state == 'read' else LEGACY_OPEN_STATES
        advanced = False
        with self.bridge._reaction_lock():
            row = self.get(mid)
            if row is None or row['state'] not in allowed:
                if row is not None and row['state'] not in LEGACY_OPEN_STATES:
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
            if target_state == 'done':
                key = f'readstate:{mid}:done'
            elif row['state'] == 'working':
                key = f'readstate:{mid}:read:settle'
            else:
                key = f'readstate:{mid}:read'
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
                db.execute('BEGIN IMMEDIATE')
                if target_state == 'read' and row['state'] == 'landed':
                    db.execute(f'UPDATE {TABLE} SET state=?, read_at=?, reason=? WHERE message_id=?',
                               ('read', now, reason, mid))
                elif target_state == 'read':
                    db.execute(f'UPDATE {TABLE} SET state=?, read_at=coalesce(read_at, ?), reason=? WHERE message_id=?',
                               ('read', now, reason, mid))
                else:
                    db.execute(f'UPDATE {TABLE} SET state=?, done_at=?, reason=? WHERE message_id=?',
                               ('done', now, reason, mid))
                    db.execute(f'DELETE FROM {OVERRIDE_TABLE} WHERE message_id=?', (mid,))
            advanced = True
        if advanced:
            self.close_offer(mid)
            self.bridge.typing_wake.set()
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
                if row[0] not in LEGACY_OPEN_STATES:
                    return
                if target == 'done':
                    db.execute(
                        f"UPDATE {TABLE} SET state='done', done_at=? WHERE message_id=? AND state IN ('landed', 'read', 'working')",
                        (now, mid),
                    )
                    db.execute(f'DELETE FROM {OVERRIDE_TABLE} WHERE message_id=?', (mid,))
                elif target == 'read':
                    if row[0] == 'landed':
                        db.execute(f"UPDATE {TABLE} SET state='read', read_at=? WHERE message_id=? AND state='landed'",
                                   (now, mid))
                else:
                    db.execute(
                        f"UPDATE {TABLE} SET state='manual' WHERE message_id=? AND state IN ('landed', 'read', 'working')",
                        (mid,),
                    )
                    db.execute(f'DELETE FROM {OVERRIDE_TABLE} WHERE message_id=?', (mid,))
            else:
                ov = db.execute(f'SELECT state FROM {OVERRIDE_TABLE} WHERE message_id=?', (mid,)).fetchone()
                if ov is None:
                    db.execute(f'INSERT INTO {OVERRIDE_TABLE} VALUES (?, ?, ?)', (mid, target, now))
                elif ov[0] in ('done', 'manual'):
                    return
                elif target in ('done', 'manual'):
                    db.execute(f'UPDATE {OVERRIDE_TABLE} SET state=?, at=? WHERE message_id=?', (target, now, mid))
                return
        if row is not None:
            self.bridge.typing_wake.set()

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
                    if row[0] not in LEGACY_OPEN_STATES:
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
                self._set_terminal(row['message_id'], LEGACY_OPEN_STATES, 'off', off.reason)
                trouble.append(f'read states off (msg {row["message_id"]}): {off.reason}')
                continue
            with self.store.connect() as db:
                db.execute(
                    f"UPDATE {TABLE} SET crew=? WHERE message_id=? AND crew IS NULL AND state IN ('landed', 'read', 'working')",
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
                    self._detect_missing_inbox_read(row)
                    latest = self.get(mid)
                    if latest is None or latest['state'] != 'landed':
                        continue
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

    def _save_crew(self, message_id, crew):
        with self.store.connect() as db:
            db.execute(
                f"UPDATE {TABLE} SET crew=? WHERE message_id=? AND crew IS NULL AND state IN ('landed', 'read', 'working')",
                (crew, int(message_id)),
            )

    def _row_crew_or_configured(self, row):
        if row['crew'] is not None:
            return row['crew']
        return self.bridge._quota_agent(self.bridge.conversation_for_id(row['conversation_id']))

    def stream_active(self):
        """True when the native /events stream is connected and unbroken."""
        with self._state_lock:
            return self._stream_started and self._stream_connected and not self._stream_broken

    def typing_targets(self):
        """When the event stream is active, return {crew: (thread_id, newest_mid,
        turn_seq)} for each working crew with at least one open `read`/`working`
        row; return None when no live stream is active (timer fallback)."""
        if not self.enabled or self.store is None or not self.stream_active():
            return None
        by_crew = {}
        for row in self._open_rows():
            if row['state'] not in ('read', 'working'):
                continue
            crew = self._row_crew_or_configured(row)
            if crew:
                by_crew[crew] = row  # _open_rows is ordered by message_id ascending
        if not by_crew:
            return {}
        with self._state_lock:
            targets = {}
            for crew, row in by_crew.items():
                st = self._crew_status.get(crew)
                if st is not None and st['status'] == 'working':
                    targets[crew] = (row['thread_id'], row['message_id'], st['turn_seq'])
        return targets

    def _settle_legacy_working(self):
        if not self.enabled or self.store is None:
            return
        pending_done_ids = set(self._pending_done_ids())
        for row in self._open_rows():
            mid = row['message_id']
            if row['state'] == 'working' and mid not in pending_done_ids:
                self.transition(mid, 'read')

    def _resolve_row_crew(self, row, crew):
        try:
            self._check_recipient(row['seq'], crew)
        except TRANSIENT:
            return False
        except _Off as off:
            self._set_terminal(row['message_id'], LEGACY_OPEN_STATES, 'off', off.reason)
            return False
        self._save_crew(row['message_id'], crew)
        return True

    def _detect_missing_inbox_read(self, row):
        client = self.bridge.yard_client()
        if client is None:
            return False
        with self._state_lock:
            if (not self._stream_started or not self._stream_connected
                    or self._inbox_read_seen or self._fallback_logged
                    or self._stream_start_seq is None or row['seq'] <= self._stream_start_seq):
                return False
        try:
            head = client.call('Head', [])
        except (TRANSIENT, n.RPCRefused, n.NativeError):
            return False
        if type(head) is not int or head < 0:
            return False
        with self._stream_cv:
            deadline = time.monotonic() + 0.5
            while (not self._worker_stop.is_set() and not self.bridge.stop.is_set()
                   and self._stream_connected and not self._inbox_read_seen
                   and (self._last_seq is None or self._last_seq < head)):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._stream_cv.wait(remaining)
            caught_up = (self._stream_connected and not self._inbox_read_seen
                         and self._last_seq is not None and self._last_seq >= head)
        if not caught_up:
            return False
        latest = self.get(row['message_id'])
        if latest is not None and latest['state'] == 'landed':
            self._log_stream_fallback(
                f'read states stream fallback: yard core did not emit inbox_read; polling every {ROUND_SECONDS}s'
            )
            return True
        return False

    def _row_crew(self, row, expected_crew):
        if row['crew'] is not None:
            return row['crew']
        configured = self.bridge._quota_agent(self.bridge.conversation_for_id(row['conversation_id']))
        if configured != expected_crew:
            return None
        return expected_crew if self._resolve_row_crew(row, expected_crew) else None

    def _apply_inbox_read(self, reader, through):
        pending_done_ids = set(self._pending_done_ids())
        for row in self._open_rows():
            mid = row['message_id']
            if row['state'] != 'landed' or row['seq'] > through or mid in pending_done_ids:
                continue
            if self._row_crew(row, reader) == reader:
                self.transition(mid, 'read')

    def _apply_replied(self, crew, target_seq):
        for row in self._open_rows():
            if row['seq'] != target_seq:
                continue
            if self._row_crew(row, crew) == crew:
                self.transition(row['message_id'], 'done')

    def _apply_cached_stream_events(self):
        with self._state_lock:
            inbox_read = dict(self._stream_inbox_read)
            replied = list(self._stream_replied_order)
        if not inbox_read and not replied:
            return
        for row in self._open_rows():
            configured = self.bridge._quota_agent(self.bridge.conversation_for_id(row['conversation_id']))
            crew = row['crew'] or configured
            if (crew, row['seq']) in replied:
                if self._row_crew(row, crew) == crew:
                    self.transition(row['message_id'], 'done')
                    continue
            through = inbox_read.get(crew)
            if row['state'] == 'landed' and through is not None and row['seq'] <= through:
                if self._row_crew(row, crew) == crew:
                    self.transition(row['message_id'], 'read')

    def _handle_hook_event(self, seq, data):
        if data.get('source') != self.source:
            return
        deliveries = data.get('deliveries')
        if (not isinstance(deliveries, dict)
                or not all(isinstance(k, str) and isinstance(v, str) for k, v in deliveries.items())):
            raise n.RPCAmbiguous('invalid native hook deliveries')
        for row in self._open_rows():
            if row['seq'] != seq or row['crew'] is not None:
                continue
            crew = self.bridge._quota_agent(self.bridge.conversation_for_id(row['conversation_id']))
            if deliveries.get(crew):
                self._save_crew(row['message_id'], crew)
            else:
                self._set_terminal(row['message_id'], LEGACY_OPEN_STATES, 'off', 'crew_not_in_deliveries')

    def _handle_inbox_event(self, kind, data):
        reader = data.get('reader')
        through = data.get('through')
        session = data.get('session', '')
        if (not isinstance(reader, str) or not reader
                or type(through) is not int or through < 0
                or not isinstance(session, str)):
            raise n.RPCAmbiguous(f'invalid native {kind} event')
        with self._stream_cv:
            if kind == 'inbox_read':
                self._inbox_read_seen = True
            if session == '':
                self._stream_inbox_read[reader] = max(self._stream_inbox_read.get(reader, 0), through)
            self._stream_cv.notify_all()
        if session != '':
            return
        self._apply_inbox_read(reader, through)

    def _handle_replied_event(self, data):
        if data.get('source') != self.source:
            return
        crew = data.get('crew')
        target_seq = data.get('seq')
        if not isinstance(crew, str) or not crew or type(target_seq) is not int or target_seq <= 0:
            raise n.RPCAmbiguous('invalid native replied event')
        pair = (crew, target_seq)
        with self._state_lock:
            if pair not in self._stream_replied:
                if len(self._stream_replied_order) == self._stream_replied_order.maxlen:
                    self._stream_replied.discard(self._stream_replied_order[0])
                self._stream_replied_order.append(pair)
                self._stream_replied.add(pair)
        self._apply_replied(crew, target_seq)

    def _set_crew_idle(self, crew):
        with self._stream_cv:
            turn_seq = self._crew_turn_seq.get(crew, 0)
            self._crew_status[crew] = dict(status='idle', since=None, turn_seq=turn_seq)
            self._stream_cv.notify_all()
        self.bridge.typing_wake.set()

    def _seed_crew_status(self):
        """Seed cached crew status at stream start from each open row crew's
        current GetCrew state, so a crew already in a working turn gets
        stream typing without waiting for its next crew_status event. A crew
        that is not working, is already cached, or cannot be read stays as it
        is. Never raises."""
        try:
            rows = self._open_rows()
        except Exception:  # noqa: BLE001 - best effort; crew_status events still drive status
            return
        seen = set()
        for row in rows:
            crew = self._row_crew_or_configured(row)
            if not crew or crew in seen:
                continue
            seen.add(crew)
            with self._state_lock:
                if crew in self._crew_status:
                    continue
            try:
                member = self._read_crew(crew)
            except Exception:  # noqa: BLE001 - an unreadable crew stays unseeded
                continue
            if member.get('state') != 'running' or member.get('status') != 'working':
                continue
            with self._stream_cv:
                if crew in self._crew_status:
                    continue
                turn_seq = self._crew_turn_seq.get(crew, 0) + 1
                self._crew_turn_seq[crew] = turn_seq
                self._crew_status[crew] = dict(status='working', since=self.bridge.monotonic(),
                                               turn_seq=turn_seq)
                self._stream_cv.notify_all()

    def _handle_crew_status_event(self, data):
        crew = data.get('crew')
        status = data.get('status')
        user = data.get('user', '')
        was = data.get('was', '')
        if (not isinstance(crew, str) or not crew
                or not isinstance(status, str) or status not in CREW_STATUSES
                or not isinstance(user, str) or not isinstance(was, str)):
            raise n.RPCAmbiguous('invalid native crew_status event')
        if user != '':
            return
        if status != 'working':
            self._set_crew_idle(crew)
            return
        now_mono = self.bridge.monotonic()
        with self._stream_cv:
            prev = self._crew_status.get(crew)
            was_working = prev is not None and prev['status'] == 'working'
            turn_seq = self._crew_turn_seq.get(crew, 0) + (0 if was_working else 1)
            self._crew_turn_seq[crew] = turn_seq
            since = prev['since'] if was_working else now_mono
            self._crew_status[crew] = dict(status='working', since=since, turn_seq=turn_seq)
            self._stream_cv.notify_all()
        self.bridge.typing_wake.set()

    def _handle_crew_stopped_event(self, data):
        crew = data.get('crew')
        user = data.get('user', '')
        if not isinstance(crew, str) or not crew or not isinstance(user, str):
            raise n.RPCAmbiguous('invalid native crew_stopped event')
        if user != '':
            return
        self._set_crew_idle(crew)

    def _handle_stream_event(self, event):
        kind = event['kind']
        data = event.get('data', {})
        if kind == 'hook':
            self._handle_hook_event(event['seq'], data)
        elif kind in ('inbox_read', 'inbox_shown'):
            self._handle_inbox_event(kind, data)
        elif kind == 'replied':
            self._handle_replied_event(data)
        elif kind == 'crew_status':
            self._handle_crew_status_event(data)
        elif kind == 'crew_stopped':
            self._handle_crew_stopped_event(data)

    def _stopped(self):
        return self._worker_stop.is_set() or self.bridge.stop.is_set()

    def _register_stream_sock(self, sock):
        with self._state_lock:
            self._stream_sock = sock

    def _mark_stream_broken(self):
        with self._stream_cv:
            self._stream_connected = False
            self._stream_broken = True
            self._crew_status.clear()
            self._stream_cv.notify_all()
        self._poll_wake.set()
        self.bridge.typing_wake.set()

    def _stream_loop(self):
        backoff = 0.1
        while not self._stopped():
            client = self.bridge.yard_client()
            if client is None:
                return
            opened_at = None
            received = False
            try:
                with self._state_lock:
                    after = self._last_seq
                if after is None:
                    head = client.call('Head', [])
                    if type(head) is not int or not 0 <= head < 2**64:
                        raise n.RPCAmbiguous('invalid native Head response')
                    with self._stream_cv:
                        self._last_seq = head
                        self._stream_start_seq = head
                        self._stream_cv.notify_all()
                    after = head
                    if self._has_open():
                        self._poll_wake.set()

                def on_open():
                    nonlocal opened_at
                    opened_at = time.monotonic()
                    self._seed_crew_status()
                    with self._stream_cv:
                        self._stream_connected = True
                        was_broken = self._stream_broken
                        self._stream_broken = False
                        self._stream_cv.notify_all()
                    self._log_stream_error(None)
                    if self._has_open() or was_broken:
                        self._poll_wake.set()
                    self.bridge.typing_wake.set()

                for event in client.stream_events(
                        after, stop_event=self._worker_stop,
                        register_socket=self._register_stream_sock, on_open=on_open):
                    if self._stopped():
                        break
                    with self._stream_cv:
                        if self._last_seq is not None and event['seq'] <= self._last_seq:
                            raise n.RPCAmbiguous('native events stream out of order')
                        self._last_seq = event['seq']
                        self._stream_cv.notify_all()
                    received = True
                    backoff = 0.1
                    self._handle_stream_event(event)
                if self._stopped():
                    break
                self._mark_stream_broken()
                if opened_at is not None and (received or time.monotonic() - opened_at >= 1.0):
                    backoff = 0.1
            except n.RPCRefused as error:
                self._mark_stream_broken()
                self._log_stream_fallback(
                    f'read states stream fallback: native events stream refused ({error}); polling every {ROUND_SECONDS}s'
                )
                self._worker_stop.wait(30.0)
                continue
            except TRANSIENT as error:
                self._mark_stream_broken()
                self._log_stream_error(f'read states stream: {error}')
            except Exception as error:  # noqa: BLE001 - keep stream supervisor alive
                self._mark_stream_broken()
                self._log_stream_error(f'read states stream failed: {type(error).__name__}')
            if self._stopped():
                break
            self._worker_stop.wait(backoff)
            backoff = min(backoff * 2, 5.0)

    def _poll_loop(self):
        while not self._stopped():
            self._poll_wake.clear()
            try:
                if self._has_open():
                    self._apply_cached_stream_events()
                    self._settle_legacy_working()
                    last_wall = self._last_round_wall
                    now_wall = time.monotonic()
                    if last_wall is None or now_wall - last_wall >= ROUND_SECONDS:
                        self.refresh(force=True)
                        if self._stopped():
                            break
                    last_wall = self._last_round_wall
                    if self._has_open():
                        remaining = (ROUND_SECONDS - (time.monotonic() - last_wall)
                                     if last_wall is not None else float(ROUND_SECONDS))
                        self._poll_wake.wait(max(0.05, remaining))
                        continue
                self._poll_wake.wait()
            except Exception as error:  # noqa: BLE001 - never stop poll loop
                self.log(f'read states failed: {type(error).__name__}')
                self._worker_stop.wait(float(ROUND_SECONDS))

    def start(self):
        if not self.enabled or self.bridge.yard_client() is None or not self.config.get('yardr_home'):
            return
        with self._state_lock:
            if self._stream_started:
                return
            self._stream_started = True
            self._worker_stop.clear()
            self._poll_wake.clear()
            self._stream_thread = threading.Thread(target=self._stream_loop, daemon=True, name='read-states-stream')
            self._poll_thread = threading.Thread(target=self._poll_loop, daemon=True, name='read-states-poll')
            self._stream_thread.start()
            self._poll_thread.start()

    def stop(self):
        with self._state_lock:
            if not self._stream_started:
                return
            self._worker_stop.set()
            self._poll_wake.set()
            sock = self._stream_sock
            stream_thread = self._stream_thread
            poll_thread = self._poll_thread
            self._stream_cv.notify_all()
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass
        if stream_thread is not None and stream_thread is not threading.current_thread():
            stream_thread.join(timeout=5)
        if poll_thread is not None and poll_thread is not threading.current_thread():
            poll_thread.join(timeout=5)
        with self._state_lock:
            self._stream_started = False
            self._stream_connected = False
            self._stream_thread = None
            self._poll_thread = None
        self.bridge.typing_wake.set()

    def refresh(self, force=False):
        if not self.enabled or self.bridge.yard_client() is None or not self.config.get('yardr_home'):
            return
        with self._round_lock:
            try:
                if self.nudge_after > 0 and (not self.bridge.native_nudge() or not self.config.get('yardr_operator')):
                    self._log_offer_off('nudge offers off: built-in /nudge or [yardr] operator unavailable')
                self._close_stale_offers()
                self._settle_legacy_working()
                now_mono = self.bridge.monotonic()
                if not force and self.last_round is not None and now_mono - self.last_round < ROUND_SECONDS:
                    return
                if not self._has_open():
                    return
                self.last_round = now_mono
                self._last_round_wall = time.monotonic()
                trouble = self._run_round()
                if trouble:
                    self.log(trouble[0])
                else:
                    self.log(None)
            except Exception as error:  # noqa: BLE001 - never break poll_once
                self.log(f'read states failed: {type(error).__name__}')
