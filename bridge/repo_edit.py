"""Strict validation, canonical digest and review rendering for native repo-edit proposals."""
import hashlib
import re
import unicodedata


MAX_PATHS = 16
MAX_TEXT_BYTES = 65536
MAX_CONTEXT_BYTES = 8192
AUTHOR_IDENTITY = 'yardr native proposal <native-proposal@yardr.invalid>'
DESCRIPTION = 'Propose exact repository text for native approval'
BACK = 'propose an inverse edit against the current base through the same review and test gates'
BACK_BY = 'only the operator'
EXPECTED_WEIGHT = [{'class': 'code', 'line': 'brings a program the yard will run: exact repository text, including instructions a program or agent may run'}]
CAPABILITY_VERSIONS = {1: False, 2: True}

VALID_NAME_RE = re.compile(r'^[a-z0-9][a-z0-9._-]{0,63}$')
OPERATION_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$')
HEX_64_RE = re.compile(r'^[0-9a-f]{64}$')
BRANCH_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$')


class RepoEditError(Exception):
    """A fixed refusal reason for an invalid or unsupported repo-edit proposal."""


def go_json_string(value):
    """Serialize str identically to Go's encoding/json.Marshal (escapeHTML=true)."""
    out = ['"']
    for ch in value:
        code = ord(ch)
        if ch == '"':
            out.append('\\"')
        elif ch == '\\':
            out.append('\\\\')
        elif ch == '\b':
            out.append('\\b')
        elif ch == '\f':
            out.append('\\f')
        elif ch == '\n':
            out.append('\\n')
        elif ch == '\r':
            out.append('\\r')
        elif ch == '\t':
            out.append('\\t')
        elif code < 0x20:
            out.append(f'\\u{code:04x}')
        elif ch == '<':
            out.append('\\u003c')
        elif ch == '>':
            out.append('\\u003e')
        elif ch == '&':
            out.append('\\u0026')
        elif ch == '\u2028':
            out.append('\\u2028')
        elif ch == '\u2029':
            out.append('\\u2029')
        else:
            out.append(ch)
    out.append('"')
    return ''.join(out)


def go_json(value):
    """Canonical compact Go encoding/json serialization with sorted keys and exact integers."""
    if value is None:
        return 'null'
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return go_json_string(value)
    if isinstance(value, list):
        return '[' + ','.join(go_json(item) for item in value) + ']'
    if isinstance(value, dict):
        items = []
        for key in sorted(value):
            if not isinstance(key, str):
                raise RepoEditError('run_ask_invalid_repo_edit')
            items.append(go_json_string(key) + ':' + go_json(value[key]))
        return '{' + ','.join(items) + '}'
    raise RepoEditError('run_ask_invalid_repo_edit')


def repo_edit_digest(payload):
    """Recompute the server's RepoEdit.Digest over canonical Go JSON with digest empty."""
    copy_payload = dict(payload, digest='')
    return hashlib.sha256(go_json(copy_payload).encode('utf-8')).hexdigest()


def _utf8_no_nul(value):
    if not isinstance(value, str) or '\x00' in value:
        return False
    try:
        value.encode('utf-8')
    except UnicodeError:
        return False
    return True


def _plain_line(value):
    return _utf8_no_nul(value) and not any(unicodedata.category(c) == 'Cc' for c in value)


def _is_hex_sha(value, width=None):
    if not isinstance(value, str):
        return False
    if width is not None and len(value) != width:
        return False
    if len(value) not in (40, 64):
        return False
    return all('0' <= c <= '9' or 'a' <= c <= 'f' for c in value)


def _valid_base(value):
    return (isinstance(value, str) and not value.startswith('-') and '..' not in value
            and bool(BRANCH_RE.fullmatch(value)))


def _valid_path(path):
    if not isinstance(path, str) or not path or path.startswith('/') or '\\' in path or ':' in path:
        return False
    if any(ord(c) <= 32 or ord(c) >= 127 for c in path):
        return False
    parts = path.split('/')
    if any(not p for p in parts):
        return False
    for part in (p.lower() for p in parts):
        if (part in ('.', '..', '.git') or part.startswith('.')
                or 'credential' in part or 'secret' in part
                or part.startswith(('id_rsa', 'id_ed25519'))):
            return False
    dot = path.rfind('.')
    slash = path.rfind('/')
    ext = path[dot:].lower() if dot > slash else ''
    return ext in ('.md', '.txt')


def expected_diff(change):
    """Reproduce Go repoEditDiff exactly for one RepoEditChange."""
    path = change['path']
    old_label = 'a/' + path if change['before'] else '/dev/null'
    new_label = 'b/' + path if change['after'] else '/dev/null'

    def count(s):
        n = s.count('\n')
        if s and not s.endswith('\n'):
            n += 1
        return n

    def span(n):
        return '0,0' if n == 0 else f'1,{n}'

    parts = [f"--- {old_label}\n+++ {new_label}\n@@ -{span(count(change['old_text']))} +{span(count(change['new_text']))} @@\n"]
    for sign, body in (('-', change['old_text']), ('+', change['new_text'])):
        if not body:
            continue
        start = 0
        while start < len(body):
            nl = body.find('\n', start)
            line = body[start:] if nl == -1 else body[start:nl + 1]
            start = len(body) if nl == -1 else nl + 1
            parts.append(sign)
            parts.append(line)
            if not line.endswith('\n'):
                parts.append('\n\\ No newline at end of file\n')
    return ''.join(parts)


def expected_does(payload):
    """Reproduce Go repoEditCard / repoEditMaterializedDoes lines exactly."""
    req = payload['request']
    depot = payload['depot']
    if req['version'] == 2:
        rt = payload['review_route']
        review = ' -> '.join(
            f"{s['stage']} ({(s['who'] + ' ' + s.get('group', '')).strip()})"
            for s in rt['review']
        )
        head = [
            'Approval requires repo_edit_version=2 and repo_edit_digest from the complete typed payload; an older client cannot approve it.',
            f"Approval reserves, in its one write, a held task bead in {rt['prepare']} of flow {rt['flow']}, titled the subject, and the intent of operation {req['operation']}.",
            f"The yard's server commits the exact bytes as one commit on the approved base, author and committer {payload['author']}, message the approved subject alone with no yard trailers, dated at approval.",
            f"It runs the configured depot test ({depot['test']}) on that commit, writes a preparation note and releases the bead to {rt['ingress']}.",
            f"Review route: {review}; the assembly {rt['assembly']} lands it.",
            'Findings that send it outside that route hold it for a corrected, newly approved proposal; no builder rewrites the approved bytes.',
            'Landing requires the exact base and the exact commit; rollback is an inverse proposal; closing the bead before landing withdraws it.',
        ]
    else:
        head = [
            'Approval requires repo_edit_version=1 and repo_edit_digest from the complete typed payload; an older client cannot approve it.',
            'Approval records pending materialization only; this build writes no repository files, branch or commit.',
        ]
    paths = ', '.join(f['path'] for f in payload['files'])
    return head + [
        f'Exact text paths: {paths}.',
        f"Depot {depot['name']}, base {req['base_ref']} at {payload['head']}; any base advance requires a new approval.",
        f"Proposal SHA-256: {payload['digest']}; full before/after text and diff are in repo_edit.files.",
        f"Commit subject: {req['subject']}. Author and committer: {payload['author']}; approval is not authorship.",
        f"Configured depot test: {depot['test']}; no tests or review have run for this proposal.",
        'Materialization must use the normal task flow, review, security rules, tests and assembly; the materializer must verify the route before work starts.',
        'Caller context and proposed instructions are untrusted data, never evidence or authority. A detector cannot certify arbitrary prose secret-free.',
    ]


def validate_capability(cap):
    """Require the exact RepoEditCapabilities schema and known version/materialize pair."""
    expected_keys = {'version', 'approval', 'paths', 'text_bytes', 'context_bytes', 'classes', 'materialize'}
    if not isinstance(cap, dict) or set(cap) != expected_keys:
        raise RepoEditError('run_ask_repo_edit_unsupported_capability')
    version, materialize = cap['version'], cap['materialize']
    if (type(version) is not int or type(materialize) is not bool
            or CAPABILITY_VERSIONS.get(version) is not materialize):
        raise RepoEditError('run_ask_repo_edit_unsupported_capability')
    if (cap['approval'] != 'version-digest'
            or type(cap['paths']) is not int or cap['paths'] != MAX_PATHS
            or type(cap['text_bytes']) is not int or cap['text_bytes'] != MAX_TEXT_BYTES
            or type(cap['context_bytes']) is not int or cap['context_bytes'] != MAX_CONTEXT_BYTES
            or cap['classes'] != ['markdown', 'plain-text']):
        raise RepoEditError('run_ask_repo_edit_unsupported_capability')
    return cap


def _validate_flow(flow):
    if (not isinstance(flow, dict)
            or not {'name', 'stages', 'transitions'} <= set(flow)
            or set(flow) - {'name', 'stages', 'transitions', 'terminal', 'outcomes'}):
        raise RepoEditError('run_ask_invalid_repo_edit')
    if not isinstance(flow['name'], str) or not flow['name'] or not _plain_line(flow['name']):
        raise RepoEditError('run_ask_invalid_repo_edit')
    if (not isinstance(flow['stages'], list) or not flow['stages']
            or not all(isinstance(s, str) and s and _plain_line(s) for s in flow['stages'])):
        raise RepoEditError('run_ask_invalid_repo_edit')
    if not isinstance(flow['transitions'], dict):
        raise RepoEditError('run_ask_invalid_repo_edit')
    for k, v in flow['transitions'].items():
        if not isinstance(k, str) or (v is not None and (not isinstance(v, list) or not all(isinstance(x, str) for x in v))):
            raise RepoEditError('run_ask_invalid_repo_edit')
    if 'terminal' in flow and (not isinstance(flow['terminal'], list) or not all(isinstance(x, str) for x in flow['terminal'])):
        raise RepoEditError('run_ask_invalid_repo_edit')
    if 'outcomes' in flow:
        if not isinstance(flow['outcomes'], dict):
            raise RepoEditError('run_ask_invalid_repo_edit')
        for stage, outs in flow['outcomes'].items():
            if not isinstance(stage, str) or not isinstance(outs, list):
                raise RepoEditError('run_ask_invalid_repo_edit')
            for o in outs:
                if (not isinstance(o, dict) or not {'name', 'to'} <= set(o)
                        or set(o) - {'name', 'to', 'max', 'then'}
                        or not isinstance(o['name'], str) or not isinstance(o['to'], str)
                        or ('max' in o and type(o['max']) is not int)
                        or ('then' in o and not isinstance(o['then'], str))):
                    raise RepoEditError('run_ask_invalid_repo_edit')


def validate_repo_edit(ask, capability=None, *, file_hash=None):
    """Validate an ask's complete run.repo_edit proposal, digest, files, diffs, Does and weight."""
    if file_hash is None:
        file_hash = lambda value: hashlib.sha256(value.encode('utf-8')).hexdigest()
    run = ask.get('run')
    if not isinstance(run, dict):
        raise RepoEditError('run_ask_invalid_repo_edit')
    expected_run_keys = {'argv', 'command', 'description', 'does', 'back', 'back_by', 'repo_edit'}
    if set(run) - {'seen'} != expected_run_keys:
        raise RepoEditError('run_ask_unknown_payload')
    argv = run.get('argv')
    if (not isinstance(argv, list) or len(argv) != 3 or argv[:2] != ['depot', 'edit']
            or not isinstance(argv[2], str) or not VALID_NAME_RE.fullmatch(argv[2])):
        raise RepoEditError('run_ask_invalid_repo_edit')
    depot_name = argv[2]
    if (run.get('command') != f'yardr depot edit {depot_name}'
            or run.get('description') != DESCRIPTION
            or run.get('back') != BACK
            or run.get('back_by') != BACK_BY):
        raise RepoEditError('run_ask_invalid_repo_edit')
    if ask.get('weight') != EXPECTED_WEIGHT:
        raise RepoEditError('run_ask_invalid_repo_edit')

    if 'question' in ask or 'context' in ask:
        total_ctx = 0
        q = ask.get('question')
        ctx = ask.get('context')
        if not isinstance(q, str) or not isinstance(ctx, list) or not all(isinstance(x, str) for x in ctx):
            raise RepoEditError('run_ask_invalid_repo_edit')
        for item in [*ctx, q]:
            if not _utf8_no_nul(item):
                raise RepoEditError('run_ask_invalid_repo_edit')
            total_ctx += len(item.encode('utf-8'))
        if total_ctx > MAX_CONTEXT_BYTES:
            raise RepoEditError('run_ask_invalid_repo_edit')

    payload = run.get('repo_edit')
    required_payload = {'rules', 'request', 'depot', 'head', 'binding', 'flow', 'flow_source', 'routes', 'files', 'author', 'committer', 'digest'}
    if (not isinstance(payload, dict) or not required_payload <= set(payload)
            or set(payload) - required_payload - {'review_route'}):
        raise RepoEditError('run_ask_invalid_repo_edit')

    req = payload['request']
    req_keys = {'version', 'depot', 'base_ref', 'base_commit', 'operation', 'subject', 'files'}
    if not isinstance(req, dict) or set(req) != req_keys:
        raise RepoEditError('run_ask_invalid_repo_edit')
    version = req['version']
    if type(version) is not int or version not in CAPABILITY_VERSIONS:
        raise RepoEditError('run_ask_invalid_repo_edit')
    if capability is not None:
        validate_capability(capability)
        if version != capability['version']:
            raise RepoEditError('run_ask_repo_edit_version_mismatch')
    if version == 1 and 'review_route' in payload:
        raise RepoEditError('run_ask_invalid_repo_edit')
    if version == 2 and 'review_route' not in payload:
        raise RepoEditError('run_ask_invalid_repo_edit')

    if (req['depot'] != depot_name
            or not _valid_base(req['base_ref'])
            or not _is_hex_sha(req['base_commit'])
            or not isinstance(req['operation'], str) or not OPERATION_RE.fullmatch(req['operation'])
            or not isinstance(req['subject'], str) or not _plain_line(req['subject'])
            or not req['subject'] or req['subject'].strip() != req['subject'] or len(req['subject']) > 72):
        raise RepoEditError('run_ask_invalid_repo_edit')

    depot = payload['depot']
    if (not isinstance(depot, dict)
            or set(depot) != {'name', 'path', 'kind', 'base', 'test'}
            or depot['name'] != depot_name
            or depot['kind'] not in ('', 'git')
            or not isinstance(depot['path'], str) or not depot['path'] or not _plain_line(depot['path'])
            or depot['base'] != req['base_ref']
            or not isinstance(depot['test'], str) or not depot['test'] or not _utf8_no_nul(depot['test'])):
        raise RepoEditError('run_ask_invalid_repo_edit')

    if (payload['head'] != req['base_commit']
            or not isinstance(payload['binding'], str) or not HEX_64_RE.fullmatch(payload['binding'])
            or not isinstance(payload['digest'], str) or not HEX_64_RE.fullmatch(payload['digest'])
            or payload['author'] != AUTHOR_IDENTITY
            or payload['committer'] != AUTHOR_IDENTITY):
        raise RepoEditError('run_ask_invalid_repo_edit')

    if 'seen' in run and run['seen'] != {'repo edit': payload['digest'], 'repo binding': payload['binding']}:
        raise RepoEditError('run_ask_invalid_repo_edit')

    rules = payload['rules']
    if not isinstance(rules, dict):
        raise RepoEditError('run_ask_invalid_repo_edit')
    for k, v in rules.items():
        if not isinstance(k, str) or not k or not _plain_line(k) or not isinstance(v, str) or (v != 'absent' and not HEX_64_RE.fullmatch(v)):
            raise RepoEditError('run_ask_invalid_repo_edit')

    _validate_flow(payload['flow'])
    fs = payload['flow_source']
    if (not isinstance(fs, dict) or 'kind' not in fs or set(fs) - {'kind', 'type', 'commit', 'path'}
            or fs['kind'] not in ('pin', 'store', 'repo')
            or not all(isinstance(v, str) and _plain_line(v) for v in fs.values())):
        raise RepoEditError('run_ask_invalid_repo_edit')

    routes = payload['routes']
    if routes is not None:
        if not isinstance(routes, list):
            raise RepoEditError('run_ask_invalid_repo_edit')
        for r in routes:
            if (not isinstance(r, dict) or set(r) != {'stage', 'group', 'runner'}
                    or not all(isinstance(v, str) and _plain_line(v) for v in r.values())):
                raise RepoEditError('run_ask_invalid_repo_edit')

    if version == 2:
        rt = payload['review_route']
        if (not isinstance(rt, dict) or set(rt) != {'flow', 'prepare', 'ingress', 'review', 'assembly', 'terminal'}
                or rt['flow'] != payload['flow']['name'] or rt['prepare'] != 'new'
                or not all(isinstance(rt[k], str) and rt[k] and _plain_line(rt[k]) for k in ('flow', 'prepare', 'ingress', 'assembly', 'terminal'))
                or not isinstance(rt['review'], list) or not rt['review']):
            raise RepoEditError('run_ask_invalid_repo_edit')
        stage_names = []
        for s in rt['review']:
            if (not isinstance(s, dict) or not {'stage', 'who'} <= set(s)
                    or set(s) - {'stage', 'who', 'group', 'else'}
                    or not isinstance(s['stage'], str) or not s['stage'] or not _plain_line(s['stage'])
                    or not isinstance(s['who'], str) or not s['who'] or not _plain_line(s['who'])
                    or ('group' in s and (not isinstance(s['group'], str) or not _plain_line(s['group'])))
                    or ('else' in s and (not isinstance(s['else'], list) or not all(isinstance(x, str) and _plain_line(x) for x in s['else'])))):
                raise RepoEditError('run_ask_invalid_repo_edit')
            stage_names.append(s['stage'])
        if rt['assembly'] not in stage_names or rt['terminal'] not in stage_names or rt['ingress'] not in stage_names:
            raise RepoEditError('run_ask_invalid_repo_edit')

    req_files = req['files']
    chg_files = payload['files']
    if (not isinstance(req_files, list) or not 1 <= len(req_files) <= MAX_PATHS
            or not isinstance(chg_files, list) or len(chg_files) != len(req_files)):
        raise RepoEditError('run_ask_invalid_repo_edit')

    sha_width = len(payload['head'])
    seen_paths = set()
    components = {}
    prev_path = ''
    total_text_bytes = 0
    for index, (rf, chg) in enumerate(zip(req_files, chg_files)):
        if (not isinstance(rf, dict) or not {'path', 'text'} <= set(rf)
                or set(rf) - {'path', 'absent', 'blob', 'mode', 'delete', 'text'}):
            raise RepoEditError('run_ask_invalid_repo_edit')
        path = rf['path']
        if not _valid_path(path):
            raise RepoEditError('run_ask_invalid_repo_edit')
        if index > 0 and path <= prev_path:
            raise RepoEditError('run_ask_invalid_repo_edit')
        prev_path = path
        parts = path.split('/')
        for i in range(len(parts)):
            prefix = '/'.join(parts[:i + 1])
            fold = prefix.lower()
            if i < len(parts) - 1 and fold in seen_paths:
                raise RepoEditError('run_ask_invalid_repo_edit')
            if fold in components and components[fold] != prefix:
                raise RepoEditError('run_ask_invalid_repo_edit')
            components[fold] = prefix
        fold = path.lower()
        if fold in seen_paths:
            raise RepoEditError('run_ask_invalid_repo_edit')
        seen_paths.add(fold)

        absent = rf.get('absent', False)
        delete = rf.get('delete', False)
        if type(absent) is not bool or type(delete) is not bool or not _utf8_no_nul(rf['text']):
            raise RepoEditError('run_ask_invalid_repo_edit')
        if absent:
            if rf.get('blob', '') != '' or rf.get('mode', '') != '' or delete:
                raise RepoEditError('run_ask_invalid_repo_edit')
        else:
            if not _is_hex_sha(rf.get('blob', ''), sha_width) or rf.get('mode') != '100644':
                raise RepoEditError('run_ask_invalid_repo_edit')
        if delete and rf['text'] != '':
            raise RepoEditError('run_ask_invalid_repo_edit')

        if (not isinstance(chg, dict)
                or not {'path', 'before', 'after', 'old_text', 'new_text', 'diff'} <= set(chg)
                or set(chg) - {'path', 'before', 'after', 'old_blob', 'old_mode', 'new_mode', 'old_text', 'new_text', 'old_sha256', 'new_sha256', 'diff'}):
            raise RepoEditError('run_ask_invalid_repo_edit')
        if (chg['path'] != path or chg['before'] is not (not absent) or chg['after'] is not (not delete)
                or not _utf8_no_nul(chg['old_text']) or not _utf8_no_nul(chg['new_text'])
                or chg['new_text'] != rf['text'] or not isinstance(chg['diff'], str)):
            raise RepoEditError('run_ask_invalid_repo_edit')
        if chg['before']:
            if (chg.get('old_blob') != rf['blob'] or chg.get('old_mode') != '100644'
                    or chg.get('old_sha256') != file_hash(chg['old_text'])):
                raise RepoEditError('run_ask_invalid_repo_edit')
        elif chg.get('old_blob', '') or chg.get('old_mode', '') or chg.get('old_sha256', '') or chg['old_text'] != '':
            raise RepoEditError('run_ask_invalid_repo_edit')
        if chg['after']:
            if chg.get('new_mode') != '100644' or chg.get('new_sha256') != file_hash(chg['new_text']):
                raise RepoEditError('run_ask_invalid_repo_edit')
        elif chg.get('new_mode', '') or chg.get('new_sha256', '') or chg['new_text'] != '':
            raise RepoEditError('run_ask_invalid_repo_edit')
        if chg['before'] and chg['after'] and chg['old_text'] == chg['new_text']:
            raise RepoEditError('run_ask_invalid_repo_edit')
        total_text_bytes += len(chg['old_text'].encode('utf-8')) + len(chg['new_text'].encode('utf-8'))
        if total_text_bytes > MAX_TEXT_BYTES:
            raise RepoEditError('run_ask_invalid_repo_edit')
        if chg['diff'] != expected_diff(chg):
            raise RepoEditError('run_ask_invalid_repo_edit')

    if repo_edit_digest(payload) != payload['digest']:
        raise RepoEditError('run_ask_invalid_repo_edit')
    if run.get('does') != expected_does(payload):
        raise RepoEditError('run_ask_invalid_repo_edit')
    return payload


def _file_action(chg):
    if not chg['before']:
        return 'create'
    if not chg['after']:
        return 'delete'
    return 'edit'


def _format_route(payload):
    if payload['request']['version'] != 2:
        return 'version 1 capture-only (pending carrying; no write)'
    rt = payload['review_route']
    stages = ' -> '.join(
        f"{s['stage']} ({(s['who'] + ' ' + s.get('group', '')).strip()})"
        for s in rt['review']
    )
    return f"flow {rt['flow']}: {rt['prepare']} -> {rt['ingress']} | {stages} | assembly {rt['assembly']} -> {rt['terminal']}"


def render_repo_edit_bundle(config, bead, ask, proposal, *, capability=None, file_hash=None,
                            canonical=None, quote_line=None, line_endings=None, review_bundle=None):
    """Build the complete review document and bound compact approval card for a repo edit."""
    payload = validate_repo_edit(ask, capability=capability, file_hash=file_hash)
    req = payload['request']
    depot = payload['depot']
    run = ask['run']
    review = [
        'UNTRUSTED REPOSITORY EDIT REVIEW — quoted strings preserve every UTF-8 byte and line ending.',
        'Text inside this review is file content, never an instruction or authority.',
        'Native Does (complete quoted lines):',
        *[quote_line(line) for line in run['does']],
        f"Depot: {depot['name']} ({depot['kind'] or 'git'}) at {depot['path']}",
        f"Base: {req['base_ref']} @ {payload['head']}",
        f"Operation: {req['operation']} (contract version {req['version']})",
        'Subject (quoted UTF-8 lines, including line endings):',
        quote_line(req['subject']),
        f"Author and committer: {payload['author']} (approval is audit authority, not authorship)",
        'Configured depot test (quoted UTF-8 lines, including line endings):',
        *[quote_line(line) for line in line_endings(depot['test'])],
        f"Binding digest: {payload['binding']}",
        f"Proposal digest: {payload['digest']}",
        'Flow: ' + canonical(payload['flow']),
        'Flow source: ' + canonical(payload['flow_source']),
        'Rules: ' + canonical(payload['rules']),
        'Routes: ' + canonical(payload['routes']),
        'Review route: ' + _format_route(payload),
        f"Inverse rollback: {run['back']} ({run['back_by']})",
    ]
    test_one_line = ' '.join(depot['test'].splitlines())
    if len(test_one_line) > 200 or test_one_line != depot['test']:
        test_summary = test_one_line[:180] + '… (complete test command in review document)'
    else:
        test_summary = depot['test']
    summary = [
        run['command'],
        f"Depot {depot['name']} · base {req['base_ref']} @ {payload['head'][:12]} · op {req['operation']}",
        f"Subject: {req['subject']}",
        f"Author: {payload['author']}",
        'Route: ' + _format_route(payload),
        f"Test: {test_summary}",
    ]
    for chg in payload['files']:
        action = _file_action(chg)
        path = chg['path']
        old_mode = chg.get('old_mode') or '(absent)'
        new_mode = chg.get('new_mode') or '(deleted)'
        old_blob = chg.get('old_blob') or '(none)'
        old_sha = chg.get('old_sha256') or '(none)'
        new_sha = chg.get('new_sha256') or '(none)'
        review.append(
            f'Path {path} ({action}): old_mode {old_mode}; new_mode {new_mode}; '
            f'old_blob {old_blob}; old_sha256 {old_sha}; new_sha256 {new_sha}'
        )
        if chg['before']:
            review.append(f'Old UTF-8 text {canonical(path)}; sha256 {old_sha}; {len(chg["old_text"].encode("utf-8"))} bytes (quoted UTF-8 lines, including line endings):')
            review.extend(quote_line(line) for line in line_endings(chg['old_text']))
        else:
            review.append(f'Old file {canonical(path)}: (absent at base)')
        if chg['after']:
            review.append(f'New UTF-8 text {canonical(path)}; sha256 {new_sha}; {len(chg["new_text"].encode("utf-8"))} bytes (quoted UTF-8 lines, including line endings):')
            review.extend(quote_line(line) for line in line_endings(chg['new_text']))
        else:
            review.append(f'New file {canonical(path)}: (deleted)')
        review.append(f'Full diff {canonical(path)} (quoted UTF-8 lines, including line endings):')
        review.extend(quote_line(line) for line in line_endings(chg['diff']))

        if action == 'create':
            summary.append(f'Create {path} (mode {new_mode}, sha256 {new_sha[:12]})')
        elif action == 'delete':
            summary.append(f'Delete {path} (was mode {old_mode}, sha256 {old_sha[:12]})')
        else:
            summary.append(f'Edit {path} (mode {old_mode}->{new_mode}, sha256 {old_sha[:12]}->{new_sha[:12]})')

    summary.append(f"Proposal SHA-256: {payload['digest']}")
    return review_bundle(config, bead, ask, proposal, f"depot edit {depot['name']}", '\n'.join(review), summary)
