"""The yard's own nudge cards: published from the operator's queue, their outcomes told on the card.

When a harness stops on an error, a core that offers it files a `session nudge`
run ask to the operator as `yard`. Each refresh reads the operator's UserAsks,
sends each such ask once as an ordinary native run card through RunAsks.create,
and, once the card is approved, follows the ask's bead for the daemon's
session_nudged to reply on the card with its outcome. Approve, Decline, stale
and take-back are the run card's own paths. A core without the row files no
such ask, so nothing is sent.
"""
import json

import run_asks as n

TABLE = 'nudge_cards'
ASKER = 'yard'
FOLLOW_SECONDS = 600
OUTCOMES = {'typed': '✓', 'refused': '✕', 'expired': '✕', 'unknown': '?'}
SILENT = ('? Nudge outcome unknown: the yard logged none within 10 minutes of the approval; '
          "look at the agent's pane. Nothing is retried.")
TRANSIENT = (n.RPCUnavailable, n.RPCAmbiguous)


def names(value, seq):
    return type(value) is int and value == seq


def nudge_entry(entry):
    """Whether a queue entry is a nudge card the yard filed itself."""
    return entry['asker'] == ASKER and (entry['argv'] or [])[:2] == ['session', 'nudge'] and '@' not in entry['bead']


def outcome_text(event):
    """The card's reply for one session_nudged event; refuses a shape it does not know."""
    data = event['data']
    if (event['actor'] != ASKER or data.get('outcome') not in OUTCOMES
            or not set(data) <= {'ask', 'by', 'outcome', 'reason', 'held', 'id', 'line', 'crew', 'user', 'session', 'via', 'witness'}):
        raise n.NativeError('invalid native session_nudged event')
    reason = n.text(data.get('reason', ''), 'session_nudged.reason')
    line = f"{OUTCOMES[data['outcome']]} Nudge {data['outcome']}"
    return line + (': ' + reason if reason else '')


class NudgeCards:
    def __init__(self, bridge):
        self.bridge, self.store, self.config = bridge, bridge.store, bridge.config
        self.logged = None
        if self.store is not None:
            with self.store.connect() as db:
                db.execute(f'CREATE TABLE IF NOT EXISTS {TABLE} (id TEXT PRIMARY KEY, data TEXT NOT NULL)')

    @property
    def run_asks(self):
        return self.bridge.run_asks

    def get(self, ident):
        with self.store.connect() as db:
            row = db.execute(f'SELECT data FROM {TABLE} WHERE id=?', (ident,)).fetchone()
        return json.loads(row[0]) if row else None

    def save(self, ident, value):
        with self.store.connect() as db:
            db.execute(f'INSERT INTO {TABLE} VALUES (?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data',
                       (ident, n.canonical(value)))

    def tracked(self, binding):
        with self.store.connect() as db:
            rows = [(r[0], json.loads(r[1])) for r in db.execute(f'SELECT id, data FROM {TABLE}')]
        return [(ident, value) for ident, value in rows
                if value['binding'] == binding and value['state'] in ('open', 'following', 'telling')]

    def log(self, words):
        # Said once while it stands, not on every poll.
        if words != self.logged:
            self.bridge._log(words)
        self.logged = words

    def refresh(self):
        ra = self.run_asks
        if ra.client is None or not self.config.get('yardr_operator') or self.store is None:
            return
        operator = self.config['yardr_operator']
        try:
            binding = n.digest(ra.binding())
            n.verify_operator(ra.client, self.config)
        except n.NativeError as error:
            self.log(f'nudge cards: operator queue unread: {error}')
            return
        trouble = []
        try:
            entries = [entry for entry in n.queue(ra.client.call('UserAsks', dict(name=operator, viewer=operator)))
                       if nudge_entry(entry)]
        except n.NativeError as error:
            # Cards already tracked are still followed: discovery and following stand apart.
            entries = None
            trouble.append(f'nudge cards: operator queue unread: {error}')
        trouble += [self.publish(entry, binding) for entry in entries or ()]
        open_asks = None if entries is None else {(entry['bead'], entry['ask']) for entry in entries}
        with ra.lock():
            for ident, value in self.tracked(binding):
                trouble.append(self.follow(ident, value, open_asks))
        trouble = [words for words in trouble if words]
        if trouble:
            self.log(trouble[0])
        else:
            self.logged = None

    def publish(self, entry, binding):
        """Send the card of one open nudge ask once; the words of a trouble, or None."""
        ra = self.run_asks
        ident = ra.ident(entry['bead'], entry['ask'])
        value = self.get(ident)
        if value is None:
            value = dict(binding=binding, bead=entry['bead'], ask=entry['ask'], state='open')
            self.save(ident, value)
        if value['state'] == 'refused':
            return None
        key = 'nudge:' + ident
        row = ra.get(ident)
        if row is not None:
            with self.store.connect() as db:
                ours = db.execute('SELECT 1 FROM run_ask_keys WHERE key=?', (key,)).fetchone()
            saved = self.store.saved_delivery(ra.send_key(row))
            # A card /decide or run_ask sent already is that card. Ours goes through create again only while its
            # send is known unsent (sent again) or confirmed but unbound (bound from the ledger, not sent again).
            if (not ours or row['message_id'] is not None
                    or saved and not self.bridge.resendable(saved[1]) and saved[1].get('delivered') is not True):
                return None
        try:
            receipt = ra.create(entry['bead'], entry['ask'], key, self.bridge.conversation['conversation_id'])
        except n.NativeError as error:
            if isinstance(error, TRANSIENT):
                return f"nudge cards: {entry['bead']}#{entry['ask']} not sent yet: {error}"
            value.update(state='refused', reason=str(error))
            self.save(ident, value)
            self.bridge._log(f"nudge cards: {entry['bead']}#{entry['ask']} refused: {error}")
            return None
        if receipt.get('delivered') is not True:
            reason = (receipt.get('metadata') or {}).get('reason') or 'delivery not confirmed'
            if self.bridge.resendable(receipt):
                return f"nudge cards: {entry['bead']}#{entry['ask']} not sent yet: {reason}"
            self.bridge._log(f"nudge cards: {entry['bead']}#{entry['ask']} card not confirmed sent: {reason}")
        return None

    def follow(self, ident, value, open_asks):
        """Advance one tracked card: to following once approved, then its outcome told once; a trouble, or None.

        open_asks is None while the queue is unread: a card is then never closed for being absent from it."""
        ra = self.run_asks
        if value['state'] == 'open':
            row = ra.get(ident)
            if row is None or row['message_id'] is None:
                if open_asks is not None and (value['bead'], value['ask']) not in open_asks:
                    value['state'] = 'done'
                    self.save(ident, value)
                return None
            if row['state'] == 'open':
                return None
            if row['state'] not in ('approved', 'carrying'):
                value['state'] = 'done'
                self.save(ident, value)
                return None
            value.update(state='following', approved_at=ra.now(), card=[row['message_id'], row['conversation']])
            self.save(ident, value)
        if value['state'] == 'following':
            try:
                told = self.outcome(value['bead'], value['ask'])
            except n.NativeError as error:
                if isinstance(error, TRANSIENT):
                    # Inconclusive: read again next poll, never said as unknown.
                    return f"nudge cards: {value['bead']}#{value['ask']} outcome unread: {error}"
                told = f"? Nudge outcome unreadable ({error}); look at the agent's pane."
            if told is None:
                if ra.now() <= value['approved_at'] + FOLLOW_SECONDS:
                    return None
                told = SILENT
            # The chosen reply is kept until its delivery is final, so a resend is the same request.
            value.update(state='telling', told=told)
            self.save(ident, value)
        receipts = ra.decide_notice(f'nudge:{ident}:outcome', value['told'], None, tuple(value['card']))
        if any(self.bridge.resendable(receipt) for receipt in receipts):
            return f"nudge cards: {value['bead']}#{value['ask']} outcome not sent yet"
        value['state'] = 'done'
        self.save(ident, value)
        return None

    def outcome(self, bead, seq):
        """The reply for the session_nudged of ask seq on bead, or None while the yard has logged none."""
        ra, limit = self.run_asks, 64
        while True:
            events = ra._validate_events(ra.client.call('Events', [bead, limit]), bead, limit, 'nudge outcome')
            found = [e for e in events if e['kind'] == 'session_nudged' and names(e['data'].get('ask'), seq)]
            if len(found) > 1:
                raise n.NativeError('invalid native duplicate session_nudged')
            if found:
                return outcome_text(found[0])
            # The request the approval wrote, or the ask itself, bounds where its outcome can be; else the journal's start.
            asked = any((e['kind'] == 'session_nudge_requested' and isinstance(e['data'].get('approved'), dict)
                         and names(e['data']['approved'].get('ask'), seq))
                        or (e['kind'] == 'noted' and names(e['data'].get('note'), seq)) for e in events)
            if asked or len(events) < limit:
                return None
            limit *= 2
