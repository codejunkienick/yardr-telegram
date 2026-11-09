#!/usr/bin/env python3
"""Record siding API limitations using a disposable real core and no Telegram."""
import argparse
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import time


FLOW = '''intake -> harbor | repair
harbor -> launch | repair
repair -> harbor | launch
launch -> complete
complete terminal
'''


def probe(binary):
    spec = importlib.util.spec_from_file_location(
        'native_cards', Path(__file__).resolve().parents[1] / 'bridge' / 'run_asks.py')
    native = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(native)
    with tempfile.TemporaryDirectory(prefix='siding-') as temporary:
        root = Path(temporary)
        home = root / 'yard'
        home.mkdir(mode=0o700)
        (root / 'bin').mkdir()
        (root / 'bin' / 'yardr').symlink_to(binary)
        depot = root / 'depot'
        depot.mkdir()
        env = dict(HOME=str(root), XDG_CONFIG_HOME=str(root / 'config'),
                   XDG_STATE_HOME=str(root / 'state'), XDG_CACHE_HOME=str(root / 'cache'),
                   PATH=str(root / 'bin') + ':/usr/local/bin:/usr/bin:/bin', USER='pat')
        command = [str(binary), '--home', str(home), '--actor', 'pat']

        def cli(*args):
            result = subprocess.run(command + list(args), env=env, cwd=root,
                                    capture_output=True, text=True, timeout=20)
            if result.returncode:
                raise RuntimeError('isolated core CLI failed: ' + args[0])
            return result.stdout.strip()

        # No daemon or hook listener: this server listens on its private Unix socket only.
        server = subprocess.Popen(command + ['server', 'run'], env=env, cwd=root,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline = time.monotonic() + 15
            while not (home / 'yard.sock').exists():
                if server.poll() is not None or time.monotonic() >= deadline:
                    raise RuntimeError('isolated core socket unavailable')
                time.sleep(.02)
            client = native.YardSocketClient(home, 'tgbridge')
            cli('settings', 'set', 'operator', 'pat')
            cli('settings', 'set', 'default_kind', 'codex')
            source = root / 'source.yaml'
            source.write_text('name: tgbridge\nbase: https://example.invalid\ncalls:\n'
                              '  health:\n    kind: read\n'
                              '    request: {method: GET, path: /health}\nhook:\n'
                              '  bodies: unique\n  verify:\n    scheme: secret\n'
                              '    signature: {header: X-Secret}\n  normalize: [/bin/false]\n')
            cli('source', 'add', str(source))
            cli('user', 'identity', 'pat', '--source', 'tgbridge', '--id', '123')
            native.verify_operator(client, dict(yardr_operator='pat', yardr_source='tgbridge', user_id=123))
            cli('depot', 'add', 'sample', str(depot), '--kind', 'dir')
            flow = root / 'rotation.flow'
            flow.write_text(FLOW)
            cli('flow', 'set', 'rotation.f', '-f', str(flow))
            cli('depot', 'flow', 'sample', 'rotation.f')
            cli('group', 'add', 'keepers', '--runner', 'manual', '--members', 'pat', '--set', 'fold_after=24h')
            cli('route', 'add', '--stage', 'harbor', '--group', 'keepers', '--depot', 'sample')

            def create(number):
                bead = 'sample-' + str(number)
                cli('bead', 'create', 'Synthetic part ' + str(number), '--id', bead,
                    '--depot', 'sample', '--stage', 'harbor', '--label', 'size:small')
                return client.call('GetBead', [bead])

            def fold(ids):
                # Verified client_gen.go order; FoldOptions uses exported Go field names.
                return client.call('FoldBeads', [ids, dict(To='launch', Title=''), 'pat'])

            def advance(bead, revision):
                return client.call('Advance', [bead, 'launch', dict(stage='harbor', revision=revision), 'pat'])

            before = create(1)
            create(2)
            status = client.call('SidingBeads', [['sample-1', 'sample-2']])
            if len(status) != 2 or any(row.get('stuck') or row['points'] != 1 for row in status):
                raise RuntimeError('unexpected canonical SidingBeads response')
            cli('bead', 'update', 'sample-1', '--title', 'Changed after review',
                '--remove-label', 'size:small', '--add-label', 'size:large')
            changed = client.call('GetBead', ['sample-1'])
            if changed['revision'] == before['revision']:
                raise RuntimeError('bead change did not change revision')
            carrier = fold(['sample-1', 'sample-2'])
            if carrier['stage'] != 'launch' or 'Changed after review' not in carrier['body']:
                raise RuntimeError('unexpected resulting carrier')
            folded = [event['data'] for event in client.call('Events', [carrier['id'], 50])
                      if event['kind'] == 'folded']
            if len(folded) != 1 or folded[0]['points'] != 5 or folded[0]['members'] != ['sample-1', 'sample-2']:
                raise RuntimeError('unexpected canonical fold event')
            for bead in ('sample-1', 'sample-2'):
                member = client.call('GetBead', [bead])
                if member['status'] != 'closed' or carrier['id'] not in member['close_reason']:
                    raise RuntimeError('fold did not canonically close member')
            replay_refused = False
            try:
                fold(['sample-1', 'sample-2'])
            except native.RPCRefused:
                replay_refused = True
            if not replay_refused:
                raise RuntimeError('fold replay unexpectedly succeeded')
            if sum(event['kind'] == 'folded' for event in client.call('Events', [carrier['id'], 50])) != 1:
                raise RuntimeError('fold replay changed canonical event count')

            single = create(3)
            old_group = client.call('GetGroup', ['keepers'])
            cli('group', 'set', 'keepers', 'fold_after=12h')
            new_group = client.call('GetGroup', ['keepers'])
            if old_group['config']['fold_after'] != '24h' or new_group['config']['fold_after'] != '12h':
                raise RuntimeError('unexpected canonical group change')
            unchanged = client.call('GetBead', [single['id']])
            if unchanged['revision'] != single['revision']:
                raise RuntimeError('group change unexpectedly changed bead revision')
            released = advance(single['id'], single['revision'])
            if released['stage'] != 'launch':
                raise RuntimeError('unexpected release stage')
            try:
                advance(single['id'], single['revision'])
            except native.RPCRefused:
                pass
            else:
                raise RuntimeError('release replay unexpectedly succeeded')
            if sum(event['kind'] == 'advanced' for event in client.call('Events', [single['id'], 50])) != 1:
                raise RuntimeError('release replay changed canonical advance count')

            stale = create(4)
            cli('bead', 'update', stale['id'], '--title', 'Changed before release')
            try:
                advance(stale['id'], stale['revision'])
            except native.RPCRefused:
                if client.call('GetBead', [stale['id']])['stage'] != 'harbor':
                    raise RuntimeError('stale release moved bead')
            else:
                raise RuntimeError('stale bead revision was accepted by Advance')

            return dict(
                core=cli('--version'), isolated=True, telegram='not contacted',
                positional_contracts=dict(SidingBeads=['ids'], FoldBeads=['ids', 'FoldOptions{To,Title}', 'actor'],
                                          Advance=['id', 'stage', 'Expect{session,stage,revision}', 'actor']),
                canonical_status=status,
                fold=dict(changed_revision=True, changed_title_in_carrier=True,
                          reviewed_points=sum(row['points'] for row in status), committed_points=folded[0]['points'],
                          carrier_stage=carrier['stage'], fold_events=1,
                          members_closed=True, replay_refused=True,
                          limitation='FoldBeads cannot pin displayed member revisions or configuration'),
                release=dict(group_changed=True, bead_revision_unchanged=True,
                             group_before=old_group['config'], group_after=new_group['config'],
                             stage=released['stage'], advance_events=1, replay_refused=True, stale_bead_refused=True,
                             limitation='Advance expectation cannot pin displayed flow/route/group configuration'))
        finally:
            server.terminate()
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=10)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--yardr', type=Path, required=True, help='absolute core executable path')
    args = parser.parse_args()
    if not args.yardr.is_absolute() or not args.yardr.is_file():
        parser.error('--yardr must name an existing absolute executable')
    print(json.dumps(probe(args.yardr), indent=2))


if __name__ == '__main__':
    main()
