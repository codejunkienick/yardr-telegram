"""Siding cards on the plain bead listing and fold, on the shared native card ledger and transport."""
from html import escape
from pathlib import Path
import re

import run_asks as n


TABLE = 'siding_fold_cards'
LEGACY = 'siding_cards'
RELEASE_KIND = 'siding_release'  # Not 'siding': the plain-fold release reads those rows of TABLE as folds.
RETIRED = 'Card from an older bridge release; nothing runs from it. Request a new card.'
STUCK = ('held', 'claimed', 'peer', 'train', 'blocked', 'asked')
BY_HAND = ('held', 'asked')  # FoldBeads takes these too; the card leaves them to a person's terminal fold
FOLD_MAX = 6  # upstream store.SidingMax, for a siding whose group leaves fold_max unstated
BEAD = r'[A-Za-z0-9][A-Za-z0-9._-]{0,199}'
EXPIRY = 900
ROUTE = {'id', 'stage', 'type', 'depot', 'label', 'and_labels', 'bead_priority', 'originator', 'group', 'note', 'when', 'else'}
REPLY = 'Reply “fold <id> <id> …” for a card for exactly those beads, or “release <id>” for one; a reply never folds or releases.'
RELEASE_REPLY = 'Reply “release <id>” with one bead of this siding; nothing released.'
RELEASE_CARD = 'A reply never releases; tap Release on this card.'
CHANGED = 'Changed since the card; nothing folded. Request a new card.'
RELEASE_CHANGED = 'Changed since the card; nothing released. Request a new card.'
STATUS = {'next': 'no single stage for the carrier to start in',
          'few': 'fewer than two beads a fold would take',
          'ceiling': 'over fold_max'}
CLOSED = {'stale': 'Changed since the card; nothing folded', 'withdrawn': 'Card withdrawn; nothing folded',
          'expired': 'Card expired; nothing folded', 'status': 'Status only; nothing folded'}
RELEASE_CLOSED = {'stale': 'Changed since the card; nothing released', 'withdrawn': 'Card withdrawn; nothing released',
                  'expired': 'Card expired; nothing released'}


class InvalidSiding(n.NativeError):
    pass


def invalid(field):
    raise InvalidSiding('invalid native siding ' + field)


def line(value, field):
    # A title is shown on one line, so it cannot pose as another row of the card.
    return ' '.join(n.text(value, 'siding ' + field).split())


def optional(value, key, field):
    if key in value and value[key] is not None:
        return n.text(value[key], 'siding ' + field)
    return None


def fold_max(config):
    # As upstream wholeCount reads it: a whole JSON number from 2; anything else is unstated.
    value = config.get('fold_max')
    if type(value) in (int, float) and value == int(value) and 2 <= value <= 2 ** 31 - 1:
        return int(value)
    return FOLD_MAX


def strings(value, field):
    if value is None:
        return []
    if not isinstance(value, list):
        invalid(field)
    return [n.text(item, 'siding ' + field) for item in value]


def bead(value, ident=None):
    if not isinstance(value, dict) or (ident is not None and value.get('id') != ident):
        invalid('bead')
    if not re.fullmatch(BEAD, n.text(value.get('id'), 'siding bead id')):
        invalid('bead id')
    if type(value.get('priority')) is not int:
        invalid('bead priority')
    return dict(id=value['id'], title=line(value.get('title'), 'bead title'), revision=n.text(value.get('revision'), 'siding bead revision'),
                stage=n.text(value.get('stage'), 'siding bead stage'), status=n.text(value.get('status'), 'siding bead status'),
                depot=optional(value, 'depot', 'bead depot'), created=n.text(value.get('created_at'), 'siding bead created_at'),
                reason=optional(value, 'close_reason', 'bead close_reason'), type=n.text(value.get('type'), 'siding bead type'),
                labels=strings(value.get('labels'), 'bead labels'), priority=value['priority'],
                originator=optional(value, 'originator', 'bead originator') or '')


def route(value):
    if not isinstance(value, dict) or set(value) - ROUTE:
        invalid('route')
    further = value.get('else') or []
    if not isinstance(further, list) or any(not isinstance(g, dict) or set(g) - {'name', 'when'} for g in further):
        invalid('route')
    priority = value.get('bead_priority')
    if priority is not None and type(priority) is not int:
        invalid('route bead_priority')
    return dict(id=n.text(value.get('id'), 'siding route id'), stage=n.text(value.get('stage'), 'siding route stage'),
                type=optional(value, 'type', 'route type'), depot=optional(value, 'depot', 'route depot'),
                labels=strings([value['label']] if value.get('label') is not None else [], 'route label')
                + strings(value.get('and_labels'), 'route and_labels'),
                priority=priority, originator=optional(value, 'originator', 'route originator'),
                groups=[n.text(value.get('group'), 'siding route group')] + [n.text(g.get('name'), 'siding route group') for g in further])


# Routing as upstream store.MatchRoute: the depot's effective routes, ranked as SortRoutes ranks them, first match wins.

def route_key(r, operator):
    return (r['stage'], r['type'], tuple(sorted(set(r['labels']))), r['priority'],
            operator if r['originator'] is None else r['originator'])


def effective_routes(routes, depot, operator, groups):
    own = {route_key(r, operator) for r in routes if r['depot'] == depot}
    out = []
    for r in routes:
        if r['depot'] != depot and (r['depot'] is not None or route_key(r, operator) in own):
            continue
        names = [g.replace('{depot}', depot) for g in r['groups']]
        # A default whose resolved group is missing does not serve the depot.
        if names != r['groups'] and any(g != 'wait' and g not in groups for g in names):
            continue
        out.append(dict(r, groups=names))
    by_name = [sum(not l.startswith(('difficulty:', 'size:')) for l in r['labels']) for r in out]
    return [r for _, r in sorted(zip(by_name, out), key=lambda pair: (
        pair[1]['priority'] is None, pair[1]['priority'] or 0, -pair[0], -(len(pair[1]['labels']) - pair[0]),
        -((pair[1]['type'] is not None) + (pair[1]['depot'] is not None) + (pair[1]['originator'] not in (None, 'any'))),
        pair[1]['depot'] is None, pair[1]['id']))]


def matches(r, b, operator):
    originator = b['originator'] or operator
    return (r['stage'] == b['stage'] and r['type'] in (None, b['type']) and r['depot'] in (None, b['depot'])
            and all(label in b['labels'] for label in r['labels'])
            and (r['priority'] is None or b['priority'] <= r['priority'])
            and (originator == operator if r['originator'] is None else r['originator'] in ('any', originator)))


def worker(routes, b, operator):
    """The group b's route sends it to in its stage, or None; routes are its depot's effective routes."""
    return next((r['groups'][0] for r in routes if matches(r, b, operator)), None)


def carrier_of(member):
    folded = re.fullmatch('folded into (' + BEAD + ')', member['reason'] or '') if member['status'] == 'closed' else None
    return folded[1] if folded else None


def render(config, selection, value):
    rows = [f"{config['yard']} · siding · {selection['depot']}"]
    if value is None:
        rows += ['No siding matches this selection: ' + n.canonical(selection), 'Status only; nothing folds from this card.']
    else:
        flow = (value['flow'] or '(no flow)') + (f" ({value['path']})" if value['path'] else '')
        rows += [f"{flow} / {value['stage']} / {value['group']} → {value['to'] or '(no next stage)'}",
                 f"Waiting ({len(value['waiting'])}):"]
        rows += [f"{w['bead']} · {w['title']} · {w['points']}pt · {w['stuck'] or 'eligible'}" for w in value['waiting']]
        limit = 'explicit subset, no max' if value['explicit'] else f"max {value['fold_max']}"
        count = len(value['members'])
        rows += [f"Proposal: fold {count} bead{'' if count == 1 else 's'}, {value['points']} point{'' if value['points'] == 1 else 's'} ({limit}) "
                 f"into one carrier in {value['to'] or '(no next stage)'}:"]
        titles = {w['bead']: w for w in value['waiting']}
        rows += [f"{i}. {ident} · {titles[ident]['title']} · {titles[ident]['points']}pt"
                 for i, ident in enumerate(value['members'], 1)]
        for reason in value['reasons']:
            rows.append('Status only: ' + STATUS[reason] + (f" ({value['points']} > {value['fold_max']})" if reason == 'ceiling' else '') + '.')
        if not value['reasons']:
            rows.append(f"Fold makes one carrier in {value['to']} of exactly these beads and closes each “folded into” it; "
                        'undo with yardr bead unfold.')
        rows.append(REPLY)
    plain = '\n'.join(rows)
    if n.units(plain) > n.CAP:
        raise n.NativeError('siding_card_too_large; no controls published; narrow the selection or use the native client')
    return plain, escape(plain)


def render_release(config, selection, value):
    flow = (value['flow'] or '(no flow)') + (f" ({value['path']})" if value['path'] else '')
    rows = [f"{config['yard']} · siding · {selection['depot']}",
            f"{flow} / {value['stage']} / {value['group']} → {value['to']}",
            f"Release {value['bead']} · {value['title']}: from {value['stage']} into {value['to']}.",
            'One advance moves this bead alone; nothing else moves. A tap refuses if it changed since this card.']
    plain = '\n'.join(rows)
    if n.units(plain) > n.CAP:
        raise n.NativeError('siding_card_too_large; no controls published; narrow the selection or use the native client')
    return plain, escape(plain)


def release_lines(bead, found, note=''):
    """The bead as it stands, for a refused release card; note says why a release would leave it."""
    if found is None:
        return 'Now:\n' + bead + ' · gone'
    fields = [found['stage'], found['status']]
    if found['reason']:
        fields.append(line(found['reason'], 'close_reason'))
    if note:
        fields.append(note)
    return 'Now:\n' + bead + ' · ' + ' · '.join(fields)


class Retired(n.RunAsks):
    """The previous release's siding rows: retired once, never acted on."""
    table = LEGACY

    def owns(self, row):
        return row.get('kind') == 'siding'


class SidingCards(n.RunAsks):
    table = TABLE

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.store is not None:
            with self.store.connect() as db:
                db.execute(f'CREATE TABLE IF NOT EXISTS {TABLE} (id TEXT PRIMARY KEY, data TEXT NOT NULL)')
                db.execute(f'CREATE TABLE IF NOT EXISTS {LEGACY} (id TEXT PRIMARY KEY, data TEXT NOT NULL)')
        self.retired = Retired(self.bridge, self.client, self.now) if self.store is not None else None

    def owns(self, row):
        return row.get('kind') in ('siding', RELEASE_KIND)

    def binding(self):
        binding = super().binding()
        home = Path(self.config['yardr_home'])
        if n.validate_home(home) != home:
            raise n.NativeError('siding_home_changed')
        info = home.stat()
        return dict(binding, home_identity=[info.st_dev, info.st_ino])

    # ---- plain reads ----

    def get_bead(self, ident):
        try:
            return bead(self.client.call('GetBead', [ident]), ident)
        except n.RPCRefused as error:
            if error.code == 'not-found':
                return None
            raise

    def siding_beads(self, ids):
        value = self.client.call('SidingBeads', [ids]) if ids else []
        if not isinstance(value, list) or len(value) != len(ids):
            invalid('beads')
        out = {}
        for ident, row in zip(ids, value):
            if (not isinstance(row, dict) or set(row) - {'bead', 'entered', 'points', 'stuck'} or row.get('bead') != ident
                    or type(row.get('points')) is not int or row['points'] < 0 or row.get('stuck', '') not in ('', *STUCK)):
                invalid('bead eligibility')
            out[ident] = dict(points=row['points'], stuck=row.get('stuck', ''), entered=n.text(row.get('entered'), 'siding entered'))
        return out

    def next_stage(self, ident):
        value = self.client.call('NextStages', [ident])
        if not isinstance(value, dict) or value.get('bead') != ident:
            invalid('next stages')
        stages = value.get('stages') or []
        if not isinstance(stages, list):
            invalid('next stages')
        flow = value.get('flow')
        if flow is not None and (not isinstance(flow, dict) or not isinstance(flow.get('name'), str)):
            invalid('flow')
        source = value.get('source') or {}
        if not isinstance(source, dict):
            invalid('flow source')
        return dict(flow=n.text(flow['name'], 'siding flow') if flow else None,
                    path=n.text(source.get('path', ''), 'siding flow path'),
                    to=n.text(stages[0], 'siding next stage') if stages else None)

    def routing(self, depot):
        """The siding groups with their fold_max, and depot's effective routes ranked, with the operator they read."""
        groups, routes = self.client.call('Groups', []), self.client.call('Routes', [])
        settings = self.client.call('Settings', [])
        if not isinstance(groups, list) or not isinstance(routes, list) or not isinstance(settings, dict):
            invalid('discovery')
        sidings, names = {}, set()
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get('config') or {}, dict):
                invalid('discovery group')
            name = n.text(group.get('name'), 'siding discovery group')
            names.add(name)
            config = group.get('config') or {}
            # As upstream store.SidingOf: a manual group stating any of these is a siding.
            if n.text(group.get('runner'), 'siding discovery runner') == 'manual' and {'fold', 'fold_after', 'fold_max'} & set(config):
                sidings[name] = fold_max(config)
        operator = n.text(settings.get('operator', ''), 'siding operator setting')
        return dict(sidings=sidings, operator=operator,
                    routes=effective_routes([route(value) for value in routes], depot, operator, names))

    def discover(self, selection):
        depot = selection['depot']
        found = self.client.call('GetDepot', [depot])
        if not isinstance(found, dict) or found.get('name') != depot:
            invalid('depot')
        routing = self.routing(depot)
        sidings = routing['sidings']
        candidates = {(r['stage'], r['groups'][0]) for r in routing['routes'] if r['groups'][0] in sidings
                      and r['stage'] == selection.get('stage', r['stage']) and r['groups'][0] == selection.get('group', r['groups'][0])}
        values = []
        for stage, group in sorted(candidates):
            values += [value for value in self.read_siding(depot, stage, group, sidings[group], routing)
                       if selection.get('flow') in (None, value['flow'])]
        return values

    def read_siding(self, depot, stage, group, limit, routing=None):
        """One value per flow of the beads the routes send to group in the stage, as upstream keys a siding."""
        routing = routing or self.routing(depot)
        listed = self.client.call('ListBeads', [{'Depot': depot, 'Stage': stage}])
        if not isinstance(listed, list):
            invalid('listing')
        beads = [bead(value) for value in listed]
        if any(b['status'] != 'open' or b['stage'] != stage or b['depot'] != depot for b in beads) \
                or len({b['id'] for b in beads}) != len(beads):
            invalid('listing')
        # A bead a more specific route sends to another group waits in no siding of this group.
        beads = [b for b in beads if worker(routing['routes'], b, routing['operator']) == group]
        if not beads:
            return [self.value(depot, stage, group, limit, None, '', [], {}, {})]
        ids = [b['id'] for b in beads]
        stuck = self.siding_beads(ids)
        nexts = {ident: self.next_stage(ident) for ident in ids}
        flows = {}
        for b in beads:
            flows.setdefault((nexts[b['id']]['flow'] or '', nexts[b['id']]['path']), []).append(b)
        return [self.value(depot, stage, group, limit, flow or None, path, members, stuck, nexts)
                for (flow, path), members in sorted(flows.items())]

    def value(self, depot, stage, group, limit, flow, path, beads, stuck, nexts):
        beads = sorted(beads, key=lambda b: (stuck[b['id']]['entered'], b['created'], b['id']))
        waiting = [dict(bead=b['id'], title=b['title'], points=stuck[b['id']]['points'], stuck=stuck[b['id']]['stuck'])
                   for b in beads]
        members = [w['bead'] for w in waiting if not w['stuck']]
        tos = {nexts[ident]['to'] for ident in members} or {nexts[w['bead']]['to'] for w in waiting[:1]}
        to = next(iter(tos)) if len(tos) == 1 else None
        points = sum(w['points'] for w in waiting if not w['stuck'])
        reasons = [reason for reason, refused in (('next', to is None and bool(members)), ('few', len(members) < 2),
                                                  ('ceiling', points > limit)) if refused]
        return dict(depot=depot, stage=stage, group=group, flow=flow, path=path, to=to, fold_max=limit, waiting=waiting,
                    members=members, points=points, reasons=reasons, explicit=False)

    def subset(self, value, ids):
        """A value for exactly ids of value's siding, re-read, or a refusal saying why not."""
        current = [v for v in self.read_siding(value['depot'], value['stage'], value['group'], value['fold_max'])
                   if (v['flow'], v['path']) == (value['flow'], value['path'])]
        waiting = {w['bead']: w for v in current[:1] for w in v['waiting']}
        absent = [ident for ident in ids if ident not in waiting]
        if absent:
            raise n.NativeError('Not open in this siding: ' + ', '.join(absent) + '. Nothing folded.')
        stuck = [ident for ident in ids if waiting[ident]['stuck']]
        if stuck:
            by_hand = [i for i in stuck if waiting[i]['stuck'] in BY_HAND]
            raise n.NativeError('; '.join(f"{i} is {waiting[i]['stuck']}" for i in stuck) + '. '
                                + ('Fold a held or asked bead with yardr bead fold in a terminal. ' if by_hand else '')
                                + 'Nothing folded.')
        tos = {self.next_stage(ident)['to'] for ident in ids}
        if len(tos) != 1 or None in tos:
            raise n.NativeError('These beads have no single stage for the carrier; fold them with yardr bead fold in a terminal.')
        # A hand fold has no points max.
        return dict(current[0], to=tos.pop(), members=list(ids), points=sum(waiting[i]['points'] for i in ids),
                    reasons=[], explicit=True)

    def release_value(self, siding, ident):
        """A release card for one bead the siding's fold would take, read now, or a refusal saying why not."""
        current = [v for v in self.read_siding(siding['depot'], siding['stage'], siding['group'], siding['fold_max'])
                   if (v['flow'], v['path']) == (siding['flow'], siding['path'])]
        waiting = {w['bead']: w for v in current[:1] for w in v['waiting']}
        if ident not in waiting:
            raise n.NativeError('Not open in this siding: ' + ident + '. Nothing released.')
        if waiting[ident]['stuck']:
            raise n.NativeError(f"{ident} is {waiting[ident]['stuck']}. Nothing released.")
        found, to = self.get_bead(ident), self.next_stage(ident)['to']
        if found is None or to is None:
            raise n.NativeError(f"{ident} has no next stage. Nothing released.")
        return dict(depot=siding['depot'], stage=siding['stage'], group=siding['group'], flow=siding['flow'], path=siding['path'],
                    bead=ident, title=found['title'], to=to, bead_revision=found['revision'])

    def release_now(self, value):
        """The bead as a release card shows it, read again: its record, why a release would leave it (if so), and whether the card still holds."""
        found = self.get_bead(value['bead'])
        if found is None or found['status'] != 'open' or found['stage'] != value['stage'] or found['depot'] != value['depot']:
            return found, '', False
        routing = self.routing(value['depot'])
        routed = worker(routing['routes'], found, routing['operator'])
        stuck = self.siding_beads([value['bead']])[value['bead']]['stuck']
        nxt = self.next_stage(value['bead'])
        note = stuck or ('' if routed == value['group'] else 'routed to ' + (routed or 'no group'))
        ok = (not note and found['revision'] == value['bead_revision']
              and (nxt['flow'], nxt['path'], nxt['to']) == (value['flow'], value['path'], value['to']))
        return found, note, ok

    def release_unchanged(self, row):
        value = row['release']
        found, note, ok = self.release_now(value)
        if ok:
            return True
        self.refuse(row, RELEASE_CHANGED + '\n' + release_lines(value['bead'], found, note))
        return False

    def release_reconcile(self, row):
        """Settle an attempted release from the bead as it stands, whatever Advance answered."""
        value = row['release']
        found = self.get_bead(value['bead'])
        if found is not None and found['stage'] == value['to']:
            row.update(attempted=False, outcome=dict(to=value['to'], stage=found['stage'], footer=(
                f"Released {value['bead']} from {value['stage']} into {found['stage']}.")))
            self.save(row)
        elif (found is not None and found['status'] == 'open' and found['stage'] == value['stage']
              and found['revision'] == value['bead_revision']):
            # Nothing moved: the card stays open.
            row['attempted'] = False
            self.save(row)
        else:
            self.refuse(row, 'Not released as shown; nothing more runs from this card.\n' + release_lines(value['bead'], found))

    def release(self, row):
        """The tap on a release card: one plain advance, from the stage and revision the card read."""
        self.check_binding(row)
        n.verify_operator(self.client, self.config)
        if row['attempted']:
            self.reconcile(row)
        state = self.state(row)
        if state == 'settled':
            self.display(row)
            return 'Already released into ' + row['outcome']['to']
        if state != 'open':
            self.display(row)
            return RELEASE_CLOSED[state]
        if not self.unchanged(row):
            self.display(row)
            return RELEASE_CLOSED['stale']
        row['attempted'] = True
        self.save(row)
        value = row['release']
        refusal = None
        try:
            self.client.call('Advance', [value['bead'], value['to'], dict(stage=value['stage'], revision=value['bead_revision']),
                                         self.config['yardr_operator']])
        except n.NativeError as error:
            refusal = error  # Any answer may follow a commit; only the bead settles it.
        try:
            self.reconcile(row)
        finally:
            self.display(row)
        state = self.state(row)
        if state == 'settled':
            return 'Released into ' + row['outcome']['to']
        if state == 'stale':
            return 'Not released as shown; see the card'
        if isinstance(refusal, n.RPCRefused):
            return 'The yard refused the release; nothing released'
        return 'Nothing released; the card stays open'

    # ---- rows and presentation ----

    def keyboard(self, row):
        if self.state(row) != 'open':
            return []
        if row.get('action') == 'release':
            value = row['release']
            labels, action = [f"Release {value['bead']} → {value['to']}", f"Release the bead shown → {value['to']}",
                              'Release the bead shown above'], 'release'
        else:
            value = row['siding']
            labels, action = [f"Fold {', '.join(value['members'])} → {value['to']}",
                              f"Fold the {len(value['members'])} beads shown → {value['to']}",
                              'Fold the beads shown above'], 'fold'
        label = next((text for text in labels if n.units(text) <= 64), labels[-1])
        return [[dict(text=label, callback_data=f"siding:{row['id']}:{action}")],
                [dict(text='Leave waiting', callback_data=f"siding:{row['id']}:leave")]]

    def state(self, row):
        if row['outcome'] is not None:
            return 'settled'
        if row['refused'] is not None:
            return 'stale'
        if row['attempted']:
            return 'attempted'
        if row['withdrawn'] is not None:
            return 'withdrawn'
        if self.now() > row['expires']:
            return 'expired'
        return 'open' if row['actionable'] else 'status'

    def presentation(self, row):
        state = self.state(row)
        if state == 'settled':
            return row['outcome']['footer'], []
        if state == 'stale':
            return row['refused'], []
        if state == 'attempted':
            if row.get('action') == 'release':
                return 'Release sent; the card reads its bead again before anything else.', []
            return 'Fold sent; the card reads its beads again before anything else.', []
        if state == 'withdrawn':
            return row['withdrawn'], []
        if state == 'expired':
            return 'Card expired after 15 minutes; nothing folded. Request a new card.', []
        return '', self.keyboard(row)

    def check_binding(self, row):
        if row['binding'] != self.binding():
            raise n.NativeError('siding_wrong_yard_or_pairing')

    def display(self, row):
        self.check_binding(row)
        footer, keyboard = self.presentation(row)
        return super().display(row, footer, keyboard)

    def bound_messages(self, message_id):
        rows = [row for row in self.rows() if row['message_id'] == message_id]
        legacy = [row for row in self.retired.rows() if row.get('message_id') == message_id]
        if not rows + legacy or self.client is None or not self.config.get('yardr_operator'):
            return []
        binding = self.binding()
        return [(row, None) for row in rows + legacy if row['binding'] == binding]

    def send_request(self, row, reply=None):
        key, identity = super().send_request(row, reply)
        identity['reply_markup'] = {'inline_keyboard': row['publish_keyboard']}
        return key, identity

    def confirmed(self, row, receipt):
        mid = receipt.get('message_id') if isinstance(receipt, dict) else None
        return (isinstance(receipt, dict) and receipt.get('delivered') is True
                and type(mid) in (str, int) and str(mid).isdecimal() and int(mid) > 0
                and int(mid) == row['message_id'])

    def saved_receipt(self, key, identity):
        saved = self.store.saved_delivery(key)
        return saved[1] if saved and saved[0] == self.bridge.delivery_fingerprint(identity) else None

    def final_receipt(self, row):
        state = self.state(row)
        if state in ('open', 'attempted'):
            return None
        if state == 'status':
            key, identity = self.send_request(row)
            return self.saved_receipt(key, identity)
        footer, keyboard = self.presentation(row)
        if row.get('display') != dict(footer=footer, keyboard=keyboard):
            return None
        # Match the shared display ledger, including any separately delivered footer parts.
        key = f"runask:{row['id']}:revision:{row['revision']}"
        separate = n.units(row['plain'] + '\n' + footer) > 4096
        suffix = 'Canonical outcome follows below.' if separate else footer
        identity = dict(text=row['html'] + '\n' + escape(suffix), message_id=row['message_id'],
                        reply_markup={'inline_keyboard': keyboard})
        receipt = self.saved_receipt(key + ':edit', identity)
        if separate:
            for index, part in enumerate(n.chunks(footer)):
                identity = dict(type='send', text=part)
                if self.thread(row) is not None:
                    identity['message_thread_id'] = self.thread(row)
                sent = self.saved_receipt(key + f':outcome:{index}', identity)
                mid = sent.get('message_id') if sent else None
                if (not sent or sent.get('delivered') is not True or type(mid) not in (str, int)
                        or not str(mid).isdecimal() or int(mid) <= 0):
                    return None
        return receipt

    def recover_generation(self, row):
        if row['message_id'] is None:
            key, identity = self.send_request(row)
            saved = self.store.saved_delivery(key)
            receipt = saved[1] if saved else None
            if (saved and saved[0] == self.bridge.delivery_fingerprint(identity) and receipt.get('delivered') is True
                    and str(receipt.get('message_id', '')).isdecimal() and int(receipt['message_id']) > 0):
                row['message_id'] = int(receipt['message_id'])
                self.save(row)
        return row

    def message_bindings(self, row):
        return [(row['message_id'], None)] if row['message_id'] is not None else []

    def new_row(self, selection, value, key, conversation, silent):
        plain, html = render(self.config, selection, value)
        ident = n.digest(dict(binding=self.binding(), key=key, conversation=conversation, siding=value))[:24]
        row = dict(id=ident, kind='siding', action='fold', binding=self.binding(), selection=selection, siding=value,
                   conversation=conversation, plain=plain, html=html, message_id=None, silent=silent, revision=0,
                   expires=self.now() + EXPIRY, attempted=False, outcome=None, refused=None, withdrawn=None,
                   actionable=bool(value and not value['reasons']))
        row['publish_keyboard'] = self.keyboard(row)
        return row

    def new_release(self, selection, release, key, conversation, silent):
        plain, html = render_release(self.config, selection, release)
        ident = n.digest(dict(binding=self.binding(), key=key, conversation=conversation, release=release))[:24]
        row = dict(id=ident, kind=RELEASE_KIND, action='release', binding=self.binding(), selection=selection, siding=None,
                   release=release, conversation=conversation, plain=plain, html=html, message_id=None, silent=silent, revision=0,
                   expires=self.now() + EXPIRY, attempted=False, outcome=None, refused=None, withdrawn=None, actionable=True,
                   batch=[ident])
        row['publish_keyboard'] = self.keyboard(row)
        return row

    def create(self, selection, key, conversation, silent=False):
        with self.lock():
            binding = self.binding()
            request = n.canonical(dict(kind='siding', selection=selection, binding=binding, conversation=conversation, silent=silent))
            with self.store.connect() as db:
                saved = db.execute('SELECT id,request FROM run_ask_keys WHERE key=?', (key,)).fetchone()
                if saved and saved[1] != request:
                    raise n.NativeError('idempotency_key_conflict')
                if not saved and (db.execute('SELECT 1 FROM deliveries WHERE key=?', (key,)).fetchone()
                                  or db.execute('SELECT 1 FROM asks WHERE ask_id=?', (self.bridge._ask_id_for_key(key),)).fetchone()):
                    raise n.NativeError('idempotency_key_conflict')
            resumed = bool(saved)
            if saved:
                first = self.get(saved[0])
                if not first:
                    raise n.NativeError('idempotency_key_conflict')
                batch = [self.get(ident) for ident in first['batch']]
                if all(row['message_id'] is not None for row in batch):
                    return self.closure(batch)
            else:
                n.verify_operator(self.client, self.config)
                values = self.discover(selection)
                batch = [self.new_row(selection, value, key + ':' + str(index), conversation, silent)
                         for index, value in enumerate(values or [None])]
                for row in batch:
                    row['batch'] = [r['id'] for r in batch]
                    self.save(row)
                with self.store.connect() as db:
                    db.execute('INSERT INTO run_ask_keys VALUES (?,?,?)', (key, batch[0]['id'], request))
            receipt = None
            for row in batch:
                if row['message_id'] is not None:
                    continue
                n.verify_operator(self.client, self.config)
                # A resumed send re-reads its beads; a card that can no longer stand is never sent.
                if self.state(row) not in ('open', 'status') or (resumed and self.state(row) == 'open' and not self.unchanged(row)):
                    raise n.NativeError('siding_stale_proposal')
                receipt = self.send_card(row)
                if not receipt.get('delivered'):
                    return receipt
            return receipt

    # ---- lifecycle ----

    def refuse(self, row, footer):
        if row['outcome'] is None and row['refused'] is None:
            row.update(refused=footer, attempted=False)
            self.save(row)

    def state_lines(self, row, beads, stuck=None, routed=None):
        lines, carriers = ['Now:'], []
        for ident, member in zip(row['siding']['members'], beads):
            if member is None:
                lines.append(ident + ' · gone')
                continue
            fields = [member['stage'], member['status']]
            if member['reason']:
                fields.append(line(member['reason'], 'close_reason'))
            elif routed and routed.get(ident, row['siding']['group']) != row['siding']['group']:
                fields.append('routed to ' + (routed[ident] or 'no group'))
            elif stuck and stuck.get(ident, {}).get('stuck'):
                fields.append(stuck[ident]['stuck'])
            lines.append(ident + ' · ' + ' · '.join(fields))
            carrier = carrier_of(member)
            if carrier and carrier not in carriers:
                carriers.append(carrier)
                found = self.get_bead(carrier)
                lines.append(f"Carrier {carrier}: " + (f"{found['stage']} · {found['status']}" if found else 'gone'))
        return '\n'.join(lines)

    def unchanged(self, row):
        """Re-read the shown beads; refuse the card when any is no longer open in the siding, eligible and bound for the shown stage."""
        if row.get('action') == 'release':
            return self.release_unchanged(row)
        value = row['siding']
        beads = [self.get_bead(ident) for ident in value['members']]
        routing = self.routing(value['depot'])
        routed = {b['id']: worker(routing['routes'], b, routing['operator']) for b in beads
                  if b and b['status'] == 'open' and b['stage'] == value['stage']}
        waiting = [b['id'] for b in beads if b and b['status'] == 'open' and b['stage'] == value['stage'] and b['depot'] == value['depot']
                   and routed.get(b['id']) == value['group']]
        stuck = self.siding_beads(waiting)
        nexts = {ident: self.next_stage(ident) for ident in waiting if not stuck[ident]['stuck']}
        ready = [ident for ident in waiting if ident in nexts
                 and (nexts[ident]['flow'], nexts[ident]['path'], nexts[ident]['to']) == (value['flow'], value['path'], value['to'])]
        if ready == value['members']:
            return True
        self.refuse(row, CHANGED + '\n' + self.state_lines(row, beads, stuck, routed))
        return False

    def reconcile(self, row):
        """Settle an attempted fold from the members as they stand, whatever FoldBeads answered."""
        if row.get('action') == 'release':
            return self.release_reconcile(row)
        value = row['siding']
        beads = [self.get_bead(ident) for ident in value['members']]
        carriers = {carrier_of(b) if b else None for b in beads}
        if len(carriers) == 1 and None not in carriers:
            carrier = carriers.pop()
            found = self.get_bead(carrier)
            stage = found['stage'] if found else 'gone'
            row.update(attempted=False, outcome=dict(carrier=carrier, stage=stage, footer=(
                f"Folded into {carrier} ({stage}): {', '.join(value['members'])}.\n"
                f"Undo one part: yardr bead unfold {carrier} <member> --reason <why>")))
            self.save(row)
        elif all(b and b['status'] == 'open' and b['stage'] == value['stage'] for b in beads):
            # A fold is all or nothing: none of them closed, so nothing folded.
            row['attempted'] = False
            self.save(row)
        else:
            self.refuse(row, 'Not folded as shown; nothing more runs from this card.\n' + self.state_lines(row, beads))

    def decide(self, row):
        self.check_binding(row)
        n.verify_operator(self.client, self.config)
        if row['attempted']:
            self.reconcile(row)
        state = self.state(row)
        if state == 'settled':
            self.display(row)
            return 'Already folded into ' + row['outcome']['carrier']
        if state != 'open':
            self.display(row)
            return CLOSED[state]
        if not self.unchanged(row):
            self.display(row)
            return CLOSED['stale']
        row['attempted'] = True
        self.save(row)
        value = row['siding']
        refusal = None
        try:
            self.client.call('FoldBeads', [value['members'], {'To': value['to'], 'Title': ''}, self.config['yardr_operator']])
        except n.NativeError as error:
            refusal = error  # Any answer may follow a commit; only the members settle it.
        try:
            self.reconcile(row)
        finally:
            self.display(row)
        state = self.state(row)
        if state == 'settled':
            return 'Folded into ' + row['outcome']['carrier']
        if state == 'stale':
            return 'Not folded as shown; see the card'
        if isinstance(refusal, n.RPCRefused):
            return 'The yard refused the fold; nothing folded'
        return 'Nothing folded; the card stays open'

    def callback(self, update_id, query):
        match = re.fullmatch(r'siding:([0-9a-f]{24}):(fold|release|approve|leave)', query.get('data', ''))
        notice = 'Siding card not authorized'
        try:
            with self.lock():
                row = self.get(match[1]) if match else None
                legacy = None if row or not match else self.retired.get(match[1])
                sender = query.get('from', {})
                if (not (row or legacy) or type(sender.get('id')) is not int or sender['id'] != self.config['user_id']
                        or sender.get('is_bot') is not False or not self.authorized_message(row or legacy, query.get('message'))):
                    return
                try:
                    binding = self.binding()
                except n.NativeError as error:
                    self.bridge._log(f'siding callback not bound (update {update_id}): {error}')
                    raise
                if ((row or legacy)['binding'] != binding or (row and match[2] == 'approve')
                        or (row and match[2] in ('fold', 'release') and (match[2] == 'release') != (row.get('action') == 'release'))):
                    return
                n.verify_operator(self.client, self.config)
                if legacy:
                    self.retire(legacy)
                    notice = RETIRED
                elif not self.claim_update(update_id):
                    notice = 'Already handled'
                elif match[2] == 'leave':
                    if row['attempted']:
                        self.reconcile(row)
                    if self.state(row) in ('open', 'status', 'expired'):
                        row['withdrawn'] = 'Card withdrawn; nothing folds from this card.'
                        self.save(row)
                    self.display(row)
                    notice = 'Left waiting; nothing folded' if self.state(row) == 'withdrawn' else 'Card closed; see it'
                elif match[2] == 'fold':
                    notice = self.decide(row)
                elif match[2] == 'release':
                    notice = self.release(row)
        except n.NativeError as error:
            notice = str(error)
        finally:
            self.bridge._answer_callback(update_id, query['id'], notice)

    def reply(self, update_id, message):
        reply = message.get('reply_to_message')
        if not isinstance(reply, dict) or type(reply.get('message_id')) is not int:
            return False
        with self.lock():
            bindings = self.bound_messages(reply['message_id'])
            if not bindings:
                return False
            if len(bindings) != 1:
                return True
            row = bindings[0][0]
            notice = REPLY
            try:
                if (not self.authorized_message(row, message, reply=True)
                        or any(k in reply for k in self.bridge.FORWARD_KEYS)
                        or ('from' in reply and (not isinstance(reply['from'], dict)
                            or reply['from'].get('id') != self.config['bot_id'] or reply['from'].get('is_bot') is not True))
                        or ('chat' in reply and (not isinstance(reply['chat'], dict) or reply['chat'].get('id') != self.config['chat_id']))
                        or self.bridge._extract_attachment(message) is not None):
                    return True
                n.verify_operator(self.client, self.config)
                if not self.claim_update(update_id):
                    return True
                if 'siding' not in row:
                    self.retire(row)
                    raise n.NativeError(RETIRED)
                words = n.text(message.get('text'), 'siding reply').split()
                if row.get('action') == 'release':
                    raise n.NativeError(RELEASE_CARD)
                verb = 'released' if words[:1] == ['release'] else 'folded'
                if not row['siding']:
                    raise n.NativeError(f'No siding on this card; nothing {verb}.')
                if self.state(row) not in ('open', 'status'):
                    raise n.NativeError(f'This card is closed; request a new card. Nothing {verb}.')
                if verb == 'released':
                    # The card for one bead is made and sent; the bead moves only on its card's tap.
                    if len(words) != 2 or not re.fullmatch(BEAD, words[1]):
                        raise n.NativeError(RELEASE_REPLY)
                    value = self.release_value(row['siding'], words[1])
                    fresh = self.new_release(row['selection'], value, f"release:{row['id']}:{update_id}", row['conversation'], row['silent'])
                    self.save(fresh)
                    receipt = self.send_card(fresh)
                    if receipt.get('delivered') is not True:
                        raise n.NativeError('siding_card_delivery_unconfirmed; request a new card')
                    notice = 'Review the release card; the reply released nothing.'
                else:
                    if (len(words) < 3 or words[0] != 'fold' or len(set(words[1:])) != len(words) - 1
                            or any(not re.fullmatch(BEAD, word) for word in words[1:])):
                        raise n.NativeError(REPLY)
                    value = self.subset(row['siding'], words[1:])
                    fresh = self.new_row(row['selection'], value, f"reply:{row['id']}:{update_id}", row['conversation'], row['silent'])
                    fresh['batch'] = [fresh['id']]
                    self.save(fresh)
                    row['withdrawn'] = 'Replaced by a card for the subset; nothing folds from this card.'
                    self.save(row)
                    self.display(row)
                    receipt = self.send_card(fresh)
                    if receipt.get('delivered') is not True:
                        raise n.NativeError('siding_card_delivery_unconfirmed; request a new card')
                    notice = 'Review the new card; the reply folded nothing.'
            except n.NativeError as error:
                notice = str(error)
            self.bridge._command_message(f'siding:{update_id}:notice', dict(type='send', text=notice, plain=None), self.thread(row))
            return True

    def retire(self, row):
        """Withdraw a previous release's row once and take its buttons off its message; the edit's receipt."""
        if row.get('withdrawn') is None and row.get('outcome') is None and row.get('refused') is None:
            row['withdrawn'] = RETIRED
            self.retired.save(row)
        if row.get('withdrawn') != RETIRED or row.get('message_id') is None:
            return self.bridge.receipt(reason='siding_card_from_older_release')
        return self.retired.display(row, RETIRED, [])

    def close(self, message_id=None, ask_key=None):
        with self.lock():
            if ask_key is not None:
                with self.store.connect() as db:
                    saved = db.execute('SELECT id FROM run_ask_keys WHERE key=?', (ask_key,)).fetchone()
                first = self.get(saved[0]) if saved else None
                rows = [self.get(ident) for ident in first['batch']] if first else []
            else:
                rows = [row for row, _ in self.bound_messages(message_id)]
                if len(rows) == 1 and 'siding' not in rows[0]:
                    return self.retire(rows[0])
            if not rows or any(row is None for row in rows):
                return None
            return self.closure(rows)

    def closure(self, rows):
        refusal, first = None, None
        for row in rows:
            try:
                receipt = self.refresh_row(row)
                state = self.state(row)
                if state in ('open', 'attempted'):
                    reason = 'siding_still_awaiting_human' if state == 'open' else 'siding_fold_unreconciled'
                    receipt = self.bridge.receipt(reason=reason)
                if not self.confirmed(row, receipt):
                    if not isinstance(receipt, dict) or receipt.get('delivered') is True:
                        receipt = self.bridge.receipt(reason='siding_presentation_unconfirmed')
                    refusal = refusal or receipt
                first = first or receipt
            except n.NativeError as error:
                refusal = refusal or self.bridge.receipt(reason=str(error))
        return refusal or first

    def refresh_row(self, row):
        self.check_binding(row)
        receipt = self.final_receipt(row)
        if self.confirmed(row, receipt):
            return receipt
        if row['message_id'] is None:
            return None
        n.verify_operator(self.client, self.config)
        if self.state(row) == 'attempted':
            self.reconcile(row)
        if self.state(row) == 'open':
            self.unchanged(row)
        if self.state(row) == 'status':
            return self.final_receipt(row)
        if self.state(row) == 'open':
            return None
        return self.display(row)

    def refresh(self):
        if self.client is None:
            return
        with self.lock():
            for row in self.rows():
                try:
                    self.refresh_row(row)
                except n.NativeError:
                    pass  # A failed read supplies no new lifecycle fact or presentation.
            try:
                binding = self.binding()
            except n.NativeError:
                return
            for row in self.retired.rows():
                if row['binding'] == binding and row.get('withdrawn') in (None, RETIRED):
                    try:
                        self.retire(row)
                    except n.NativeError:
                        pass
