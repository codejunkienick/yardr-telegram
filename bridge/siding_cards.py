"""Canonical siding proposals on the shared native card ledger and transport."""
from html import escape
from pathlib import Path
import re

import run_asks as n


METHODS = ('SidingProposal', 'SidingOutcome', 'FoldSidingExpected', 'ReleaseSidingExpected')
UNCERTAIN = 'Outcome uncertain (including possible pruned history). No new operation; review canonical state.'


class InvalidSiding(n.NativeError):
    pass


def invalid(field):
    raise InvalidSiding('invalid native siding ' + field)


def strings(value, field):
    if not isinstance(value, list):
        invalid(field)
    return [n.text(item, 'siding ' + field) for item in value]


def proposal(value):
    fields = {'version', 'digest', 'ask', 'depot', 'stage', 'to', 'flow', 'guard', 'unguarded', 'group',
              'routes', 'members', 'points', 'fold_max', 'waiting', 'ready', 'trigger', 'due', 'refusals'}
    if not isinstance(value, dict) or set(value) - {'held'} != fields or type(value['version']) is not int or value['version'] != 1:
        invalid('proposal schema; guarded core required')
    if value.get('held', []) != []:
        invalid('held inputs; only store or repository guards supported')
    if not isinstance(value['digest'], str) or not re.fullmatch('sp1:[0-9a-f]{64}', value['digest']):
        invalid('digest')
    ask = value['ask']
    if (not isinstance(ask, dict) or set(ask) - {'depot', 'stage', 'flow', 'group', 'action', 'members', 'to'}
            or not {'depot', 'stage', 'flow', 'group', 'action', 'members'} <= set(ask)):
        invalid('resolved ask')
    for key in set(ask) - {'members'}:
        n.text(ask[key], 'siding ask ' + key)
    ids = strings(ask['members'], 'selection')
    if len(set(ids)) != len(ids) or ask['action'] not in ('fold', 'release') or (ask['action'] == 'release' and len(ids) != 1):
        invalid('selection')
    for key in ('depot', 'stage', 'to', 'due', 'trigger'):
        n.text(value[key], 'siding ' + key)
    if (value['depot'] != ask['depot'] or value['stage'] != ask['stage'] or value['to'] != ask.get('to', '')
            or value['guard'] not in ('store', 'repository') or type(value['ready']) is not bool
            or value['trigger'] not in ('', 'count', 'wait')):
        invalid('source class or selection')
    for key in ('points', 'fold_max'):
        if type(value[key]) is not int or value[key] < 0:
            invalid(key)
    flow, group = value['flow'], value['group']
    if (not isinstance(flow, dict) or set(flow) != {'name', 'source', 'digest'} or flow['name'] != ask['flow']
            or not isinstance(flow['source'], dict) or set(flow['source']) - {'kind', 'type', 'path', 'commit'}
            or flow['source'].get('kind') not in ('store', 'pin', 'repo')
            or not isinstance(group, dict) or set(group) != {'name', 'runner', 'members', 'fold', 'fold_after', 'fold_max'}
            or group['name'] != ask['group'] or group['runner'] != 'manual'):
        invalid('flow or group')
    for item in list(flow['source'].values()) + [flow['name'], flow['digest'], group['name'], group['fold_after']]:
        n.text(item, 'siding configuration')
    if not re.fullmatch('[0-9a-f]{64}', flow['digest']):
        invalid('flow digest')
    strings(group['members'], 'group members')
    if any(type(group[k]) is not int or group[k] < 1 for k in ('fold', 'fold_max')) or group['fold_max'] != value['fold_max']:
        invalid('group limits')
    for key in ('members', 'waiting'):
        if not isinstance(value[key], list):
            invalid(key)
        required = {'bead', 'revision', 'points', 'stuck', 'entered', 'title'}
        if key == 'members':
            required |= {'stage', 'route'}
        seen = set()
        for row in value[key]:
            if (not isinstance(row, dict) or set(row) != required
                    or type(row['points']) is not int or row['points'] < 0):
                invalid(key)
            for name in required - {'points'}:
                n.text(row[name], 'siding ' + key + ' ' + name)
            if row['bead'] in seen:
                invalid('duplicate ' + key)
            if row['stuck'] not in ('', 'held', 'claimed', 'peer', 'train', 'blocked', 'asked'):
                invalid('eligibility')
            seen.add(row['bead'])
    if ([row['bead'] for row in value['members']] != ids
            or sum(row['points'] for row in value['members']) != value['points']
            or any(row['stage'] != ask['stage'] for row in value['members'])):
        invalid('member binding')
    waiting = {row['bead']: row for row in value['waiting']}
    if any(row['bead'] not in waiting or any(waiting[row['bead']][k] != row[k] for k in waiting[row['bead']])
           for row in value['members']):
        invalid('waiting binding')
    refusals = strings(value['refusals'], 'refusals')
    if any(r not in ('ceiling', 'next', 'stuck', 'few', 'to', 'source') for r in refusals):
        invalid('refusals')
    if not isinstance(value['unguarded'], list) or not isinstance(value['routes'], list):
        invalid('inputs or routes')
    for item in value['unguarded']:
        if (not isinstance(item, dict) or set(item) - {'input', 'bead', 'blocker', 'depot', 'path', 'commit', 'dir'}
                or not {'input', 'depot', 'path'} <= set(item) or item['input'] not in ('flow', 'until')):
            invalid('unguarded inputs')
        for v in item.values():
            n.text(v, 'siding unguarded input')
    if ((value['guard'] == 'store' and (value['unguarded'] or 'source' in refusals))
            or (value['guard'] == 'repository' and (not value['unguarded'] or 'source' not in refusals))):
        invalid('source class')
    for route in value['routes']:
        if (not isinstance(route, dict) or set(route) - {'id', 'stage', 'group', 'type', 'depot', 'label', 'and_labels',
                                                       'bead_priority', 'originator', 'when', 'else'}
                or not {'id', 'stage', 'group'} <= set(route)):
            invalid('routes')
        for key, item in route.items():
            if key == 'and_labels':
                strings(item, 'route labels')
            elif key == 'bead_priority':
                if type(item) is not int:
                    invalid('route priority')
            elif key == 'else':
                if not isinstance(item, list):
                    invalid('route alternatives')
                for alt in item:
                    if not isinstance(alt, dict) or set(alt) - {'name', 'when'} or 'name' not in alt:
                        invalid('route alternatives')
                    for v in alt.values():
                        n.text(v, 'siding route alternative')
            else:
                n.text(item, 'siding route ' + key)
    return value


def render(config, selection, value):
    rows = [f"{config['yard']} · siding · {selection['depot']}"]
    if value is None:
        rows += ['No waiting siding matches this selection.', 'Selection: ' + n.canonical(selection), 'Status only; no action.']
    else:
        ask, group = value['ask'], value['group']
        rows += [f"{ask['flow']} / {ask['stage']} / {ask['group']} → {value['to'] or '(no next stage)'}",
                 f"Guard: {value['guard']} · source: " + n.canonical(value['flow']['source']),
                 'Group: ' + n.canonical(group), 'Routes: ' + n.canonical(value['routes']),
                 f"Waiting {len(value['waiting'])}; eligible {sum(not r['stuck'] for r in value['waiting'])}; "
                 f"selected {len(value['members'])} / {value['points']} points; limit {value['fold_max']}",
                 f"Automatic: {'ready' if value['ready'] else 'waiting'}; trigger {value['trigger'] or 'none'}; due {value['due']}",
                 'Waiting (complete):']
        rows += [f"{r['bead']} · {r['title']} · {r['points']}pt · {r['stuck'] or 'eligible'}" for r in value['waiting']]
        rows += [f"Exact {ask['action']} proposal → {value['to'] or '(none)'}:"]
        rows += [f"{i}. {r['bead']} · {r['title']} · {r['points']}pt · {r['stuck'] or 'eligible'} · rev {r['revision']}"
                 for i, r in enumerate(value['members'], 1)]
        if value['refusals']:
            rows += ['Status only: ' + ', '.join(value['refusals'])]
        if value['guard'] == 'repository':
            rows += ['Repository inputs cannot be guarded; status only.', 'Unguarded: ' + n.canonical(value['unguarded'])]
        if 'ceiling' in value['refusals']:
            rows += ['Ceiling exceeded; nothing will fold. Reply with an explicit subset to review.']
        rows += ['Fold closes exactly these parts into one carrier; release moves only the selected bead.'
                 if ask['action'] == 'fold' else 'Release moves only this bead; native stage asks and gates still apply.',
                 'Reply “fold id id …” or “release id” for a NEW review card; a reply never executes.',
                 'Proposal: ' + value['digest']]
    plain = '\n'.join(rows)
    if n.units(plain) > n.CAP:
        raise n.NativeError('siding_card_too_large; no controls published; narrow the selection or use the native client')
    return plain, escape(plain)


class SidingCards(n.RunAsks):
    table = 'siding_cards'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.store is not None:
            with self.store.connect() as db:
                db.execute('CREATE TABLE IF NOT EXISTS siding_cards (id TEXT PRIMARY KEY, data TEXT NOT NULL)')

    def owns(self, row):
        return row.get('kind') == 'siding'

    def binding(self):
        binding = super().binding()
        home = Path(self.config['yardr_home'])
        if n.validate_home(home) != home:
            raise n.NativeError('siding_home_changed')
        info = home.stat()
        return dict(binding, home_identity=[info.st_dev, info.st_ino])

    def capability(self):
        # Missing required arguments can never write; old cores reject the method first.
        for method in METHODS[1:]:
            try:
                self.client.call(method, {})
            except n.RPCUnsupported:
                raise
            except n.RPCRefused as error:
                if error.code != 'error':
                    raise n.NativeError('siding_core_capability_unverified') from None
            else:
                raise n.NativeError('siding_core_capability_unverified')

    def fetch_proposal(self, ask):
        value = self.client.call('SidingProposal', {'ask': ask})
        try:
            return proposal(value)
        except n.NativeError as error:
            raise InvalidSiding(str(error)) from None

    def discover(self, selection):
        depot = self.client.call('GetDepot', [selection['depot']])
        if not isinstance(depot, dict) or depot.get('name') != selection['depot']:
            invalid('depot')
        groups = self.client.call('Groups', [])
        routes = self.client.call('Routes', [])
        if not isinstance(groups, list) or not isinstance(routes, list):
            invalid('discovery')
        sidings = set()
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get('config'), dict):
                invalid('discovery group')
            name = n.text(group.get('name'), 'siding discovery group')
            runner = n.text(group.get('runner'), 'siding discovery runner')
            if runner == 'manual' and {'fold', 'fold_after', 'fold_max'} & set(group['config']):
                sidings.add(name)
        candidates = set()
        for route in routes:
            if not isinstance(route, dict) or not isinstance(route.get('stage'), str) or not isinstance(route.get('group'), str):
                invalid('discovery route')
            group = route['group'].replace('{depot}', selection['depot'])
            if (group in sidings and route.get('depot', selection['depot']) == selection['depot']
                    and route['stage'] == selection.get('stage', route['stage'])
                    and group == selection.get('group', group)):
                candidates.add((route['stage'], group))
        result = []
        for stage, group in sorted(candidates):
            ask = dict(selection, stage=stage, group=group, action='fold', members=[])
            try:
                result.append(self.fetch_proposal(ask))
            except n.RPCRefused as error:
                if error.code != 'not-found':
                    raise
        return result

    def keyboard(self, row):
        value = row.get('canonical')
        if self.state(row) != 'open':
            return []
        ids = ', '.join(value['ask']['members'])
        label = f"{value['ask']['action'].capitalize()} {ids} → {value['to']}"
        if n.units(label) > 64:
            label = f"{value['ask']['action'].capitalize()} exact {len(value['members'])} shown → {value['to']}"
        if n.units(label) > 64:
            label = 'Approve exact proposal shown above'
        return [[dict(text=label, callback_data=f"siding:{row['id']}:approve")],
                [dict(text='Leave waiting', callback_data=f"siding:{row['id']}:leave")]]

    def state(self, row):
        if row['outcome'] is not None:
            return 'settled'
        if row['attempted']:
            return 'uncertain'
        if row['refused'] is not None:
            return 'stale'
        if row['withdrawn'] is not None:
            return 'withdrawn'
        if self.now() > row['expires']:
            return 'expired'
        return 'open' if row['actionable'] else 'status'

    def check_binding(self, row):
        if row['binding'] != self.binding():
            raise n.NativeError('siding_wrong_yard_or_pairing')

    def bound_messages(self, message_id):
        rows = [row for row in self.rows() if row['message_id'] == message_id]
        if not rows or self.client is None or not self.config.get('yardr_operator'):
            return []
        binding = self.binding()
        return [(row, None) for row in rows if row['binding'] == binding]

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

    def presentation(self, row):
        state = self.state(row)
        if state == 'settled':
            return row['outcome']['footer'], []
        if state == 'uncertain':
            return UNCERTAIN, []
        if state == 'stale':
            return row['refused'], []
        if state == 'withdrawn':
            return row['withdrawn'], []
        if state == 'expired':
            return 'Review expired; request a fresh proposal.', []
        return '', self.keyboard(row)

    def display(self, row):
        self.check_binding(row)
        footer, keyboard = self.presentation(row)
        return super().display(row, footer, keyboard)

    def final_receipt(self, row):
        state = self.state(row)
        if state in ('open', 'uncertain'):
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
            if saved:
                first = self.get(saved[0])
                if not first:
                    raise n.NativeError('idempotency_key_conflict')
                batch = [self.get(ident) for ident in first['batch']]
                if all(row['message_id'] is not None for row in batch):
                    return self.closure(batch)
            else:
                n.verify_operator(self.client, self.config)
                self.capability()
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
                if row['canonical']:
                    self.ensure(row)
                else:
                    n.verify_operator(self.client, self.config)
                receipt = self.send_card(row)
                if not receipt.get('delivered'):
                    return receipt
            return receipt

    def new_row(self, selection, value, key, conversation, silent):
        plain, html = render(self.config, selection, value)
        ident = n.digest(dict(binding=self.binding(), key=key, conversation=conversation, canonical=value))[:24]
        row = dict(id=ident, kind='siding', binding=self.binding(), selection=selection, canonical=value,
                    conversation=conversation, plain=plain, html=html,
                    pending=None, message_id=None, silent=silent, revision=0, expires=self.now() + 900,
                    op='siding:' + ident, attempted=False, outcome=None, refused=None, withdrawn=None,
                    actionable=bool(value and value['guard'] == 'store' and not value['refusals']))
        row['publish_keyboard'] = self.keyboard(row)
        return row

    def ensure(self, row):
        self.check_binding(row)
        n.verify_operator(self.client, self.config)
        value = row['canonical']
        try:
            current = self.fetch_proposal(value['ask'])
        except (n.RPCUnsupported, InvalidSiding):
            self.refuse(row, 'Core cannot validate the saved proposal; request canonical status again.')
            raise
        except n.RPCRefused as error:
            if error.code == 'error':
                # Core reports departed explicit members as error; prove drift from its default read.
                ask = dict(value['ask'], action='fold', members=[])
                ask.pop('to', None)
                current = self.fetch_proposal(ask)
                waiting = {member['bead'] for member in current['waiting']}
                if not set(value['ask']['members']) <= waiting or value['to'] != current['to']:
                    self.refuse(row, self.external_state(row, current))
                    raise n.NativeError('siding_stale_proposal') from None
            if error.code not in ('not-found', 'conflict'):
                raise
            self.refuse(row, self.external_state(row))
            raise n.NativeError('siding_stale_proposal') from None
        if current['digest'] != value['digest'] or current['ask'] != value['ask'] or current['guard'] != value['guard']:
            self.refuse(row, 'Proposal changed; request a new review card.\n' + self.external_state(row, current))
            raise n.NativeError('siding_stale_proposal')
        return current

    def refuse(self, row, footer):
        self.check_binding(row)
        if not row['attempted'] and row['outcome'] is None and row['withdrawn'] is None and row['refused'] is None:
            row['refused'] = footer
            self.save(row)

    def external_state(self, row, current=None):
        remaining = 'no matching siding'
        if current is None:
            try:
                current = self.fetch_proposal(dict(row['canonical']['ask'], action='fold', members=[]))
            except n.RPCRefused as error:
                if error.code != 'not-found':
                    remaining = 'unavailable: ' + str(error)
        lines = ['Canonical selected state now:']
        carriers = set()
        for ident in row['canonical']['ask']['members']:
            bead = self.client.call('GetBead', [ident])
            if not isinstance(bead, dict) or bead.get('id') != ident:
                invalid('current bead')
            fields = [n.text(bead.get(k, ''), 'siding current ' + k) for k in ('stage', 'status', 'close_reason')]
            lines.append(ident + ' · ' + ' · '.join(fields))
            folded = re.fullmatch(r'folded into ([A-Za-z0-9][A-Za-z0-9._-]{0,199})', fields[2])
            if folded and folded[1] not in carriers:
                carrier = self.client.call('GetBead', [folded[1]])
                if not isinstance(carrier, dict) or carrier.get('id') != folded[1]:
                    invalid('external carrier')
                lines.append('Carrier now: ' + folded[1] + ' · ' + ' · '.join(
                    n.text(carrier.get(k), 'siding carrier ' + k) for k in ('stage', 'status')))
                carriers.add(folded[1])
        lines.append('Current waiting: ' + (', '.join(r['bead'] for r in current['waiting']) if current else remaining))
        return '\n'.join(lines)

    def outcome(self, row):
        value = row['canonical']
        result = self.client.call('SidingOutcome', dict(ask=value['ask'], digest=value['digest'], op=row['op']))
        if result is None:
            return None
        if (not isinstance(result, dict) or set(result) - {'op', 'digest', 'action', 'actor', 'event', 'members', 'left',
                                                         'replayed', 'carrier', 'bead'}
                or result.get('op') != row['op'] or result.get('digest') != value['digest']
                or result.get('action') != value['ask']['action'] or result.get('actor') != self.config['yardr_operator']
                or type(result.get('event')) is not int or result['event'] <= 0 or type(result.get('replayed')) is not bool):
            invalid('outcome identity')
        members, left = strings(result.get('members'), 'outcome members'), strings(result.get('left'), 'outcome left')
        if members != (value['ask']['members'] if value['ask']['action'] == 'fold' else []):
            invalid('outcome selection')
        current = result.get('carrier' if value['ask']['action'] == 'fold' else 'bead')
        if not isinstance(current, dict) or not {'id', 'stage', 'status', 'revision'} <= set(current):
            invalid('outcome current state')
        for k in ('id', 'stage', 'status', 'revision'):
            n.text(current[k], 'siding outcome ' + k)
        if value['ask']['action'] == 'release' and current['id'] != value['ask']['members'][0]:
            invalid('outcome release identity')
        return result, current, left

    def reconcile(self, row):
        if row['outcome'] is not None or not row['canonical']:
            return
        # Status-only publications have no guarded operation to reconcile.
        if not row['actionable'] and not row['attempted']:
            return
        try:
            settled = self.outcome(row)
        except n.NativeError as error:
            if isinstance(error, n.RPCRefused) and error.code == 'conflict' and not row['attempted']:
                # Another operation's outcome is not ours; prove canonical departure instead.
                if self.state(row) != 'open':
                    return
                try:
                    self.ensure(row)
                except n.NativeError:
                    if self.state(row) == 'stale':
                        return
                    raise
                raise
            if (isinstance(error, (n.RPCUnavailable, n.RPCAmbiguous))
                    or isinstance(error, n.RPCRefused) and not isinstance(error, n.RPCUnsupported)):
                raise
            self.refuse(row, 'Core cannot validate the saved proposal; request canonical status again.')
            if self.state(row) != 'stale':
                raise
            return
        if settled:
            result, current, left = settled
            footer = (f"Canonical {result['action']} recorded (event {result['event']}): {current['id']} · "
                      f"{current['stage']} · {current['status']}. Left at commit: {', '.join(left) or 'none'}.")
            row['outcome'] = dict(result=result, footer=footer)
            self.save(row)

    def refresh_row(self, row, reconciled=False):
        self.check_binding(row)
        receipt = self.final_receipt(row)
        if self.confirmed(row, receipt):
            return receipt
        if self.state(row) in ('settled', 'stale', 'withdrawn', 'expired'):
            return self.display(row)
        n.verify_operator(self.client, self.config)
        if not reconciled:
            self.reconcile(row)
        if self.state(row) not in ('open', 'status'):
            return self.display(row)
        if self.state(row) == 'status':
            return self.final_receipt(row)
        try:
            self.ensure(row)
        except n.NativeError:
            if self.state(row) != 'stale':
                raise
            return self.display(row)
        return None

    def decide(self, row):
        self.check_binding(row)
        n.verify_operator(self.client, self.config)
        self.reconcile(row)
        if row['outcome'] is not None:
            self.display(row)
            return 'Canonical outcome recorded'
        if self.state(row) != 'open':
            self.display(row)
            raise n.NativeError('siding_review_closed_or_uncertain')
        try:
            current = self.ensure(row)
        except n.NativeError:
            if row['refused'] is not None:
                self.display(row)
            raise
        if current['guard'] != 'store' or current['refusals']:
            self.refuse(row, 'Status only; canonical action refused.')
            self.display(row)
            raise n.NativeError('siding_status_only')
        try:
            self.capability()
        except n.RPCUnsupported:
            self.refuse(row, 'Core no longer supports guarded siding actions; request canonical status again.')
            self.display(row)
            raise
        row['attempted'] = True
        self.save(row)
        value = row['canonical']
        method = 'FoldSidingExpected' if value['ask']['action'] == 'fold' else 'ReleaseSidingExpected'
        try:
            self.client.call(method, dict(ask=value['ask'], digest=value['digest'], op=row['op'], actor=self.config['yardr_operator']))
        except n.NativeError:
            pass  # Any response may follow a commit; only the canonical outcome settles it.
        try:
            self.reconcile(row)
        except n.NativeError:
            self.display(row)
            raise
        self.display(row)
        if row['outcome'] is not None:
            return 'Canonical outcome recorded'
        return UNCERTAIN

    def callback(self, update_id, query):
        match = re.fullmatch(r'siding:([0-9a-f]{24}):(approve|leave)', query.get('data', ''))
        notice = 'Siding card not authorized'
        try:
            with self.lock():
                row = self.get(match[1]) if match else None
                sender = query.get('from', {})
                if (not row or type(sender.get('id')) is not int or sender['id'] != self.config['user_id']
                        or sender.get('is_bot') is not False or not self.authorized_message(row, query.get('message'))):
                    return
                if row['binding'] != self.binding():
                    return
                n.verify_operator(self.client, self.config)
                if not self.claim_update(update_id):
                    notice = 'Already handled'
                elif match[2] == 'leave':
                    if row['withdrawn'] is None:
                        row['withdrawn'] = 'Card withdrawn; no further native write from this card.'
                        self.save(row)
                    self.reconcile(row)
                    self.display(row)
                    notice = 'Card withdrawn; no native write'
                else:
                    notice = self.decide(row)
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
            notice = 'Reply with “fold id id …” or “release id” to review; nothing executed.'
            try:
                if (not self.authorized_message(row, message, reply=True)
                        or any(k in reply for k in self.bridge.FORWARD_KEYS)
                        or ('from' in reply and (not isinstance(reply['from'], dict)
                            or reply['from'].get('id') != self.config['bot_id'] or reply['from'].get('is_bot') is not True))
                        or ('chat' in reply and (not isinstance(reply['chat'], dict) or reply['chat'].get('id') != self.config['chat_id']))
                        or self.bridge._extract_attachment(message) is not None):
                    return True
                if row['binding'] != self.binding():
                    return True
                n.verify_operator(self.client, self.config)
                if not self.claim_update(update_id):
                    return True
                if self.state(row) not in ('open', 'status') or not row['canonical']:
                    raise n.NativeError('siding_review_closed_or_uncertain')
                value = n.text(message.get('text'), 'siding reply').split()
                if len(value) < 2 or value[0] not in ('fold', 'release') or any(not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,199}', s) for s in value[1:]):
                    raise n.NativeError(notice)
                ask = dict(row['canonical']['ask'], action=value[0], members=value[1:])
                canonical = self.fetch_proposal(ask)
                self.capability()
                fresh = self.new_row(row['selection'], canonical, f"reply:{row['id']}:{update_id}", row['conversation'], row['silent'])
                self.save(fresh)
                row['withdrawn'] = 'Replaced by an explicit review card; this card cannot execute.'
                self.save(row)
                self.display(row)
                receipt = self.send_card(fresh)
                if receipt.get('delivered') is not True:
                    raise n.NativeError('siding_review_delivery_unconfirmed; request canonical status again')
                notice = 'Review the new exact proposal; reply made no mutation.'
            except n.NativeError as error:
                notice = str(error)
            self.bridge._command_message(f'siding:{update_id}:notice', dict(type='send', text=notice, plain=None), self.thread(row))
            return True

    def close(self, message_id=None, ask_key=None):
        with self.lock():
            if ask_key is not None:
                with self.store.connect() as db:
                    saved = db.execute('SELECT id FROM run_ask_keys WHERE key=?', (ask_key,)).fetchone()
                first = self.get(saved[0]) if saved else None
                rows = [self.get(ident) for ident in first['batch']] if first else []
            else:
                rows = [row for row, _ in self.bound_messages(message_id)]
            if not rows or any(row is None for row in rows):
                return None
            return self.closure(rows)

    def closure(self, rows):
        refusal, first = None, None
        for row in rows:
            try:
                self.check_binding(row)
                self.reconcile(row)
                receipt = self.refresh_row(row, reconciled=True)
                state = self.state(row)
                if state in ('open', 'uncertain'):
                    reason = 'siding_still_awaiting_human' if state == 'open' else 'siding_outcome_uncertain'
                    receipt = self.bridge.receipt(reason=reason)
                if not self.confirmed(row, receipt):
                    if not isinstance(receipt, dict) or receipt.get('delivered') is True:
                        receipt = self.bridge.receipt(reason='siding_presentation_unconfirmed')
                    refusal = refusal or receipt
                first = first or receipt
            except n.NativeError as error:
                refusal = refusal or self.bridge.receipt(reason=str(error))
        return refusal or first

    def refresh(self):
        if self.client is None:
            return
        with self.lock():
            for row in self.rows():
                try:
                    self.refresh_row(row)
                except n.NativeError:
                    pass  # A failed read supplies no new lifecycle fact or presentation.
