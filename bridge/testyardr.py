"""The yardr build the native integration tests drive, and an isolated server of it.

TGBRIDGE_TEST_YARDR names the build by absolute path; unset, the yardr on PATH.
Optional families are checked on their own read RPCs, never inferred from
version strings or the presence of skills. The historical alignment probe
is used only by the retired pre-alignment fixture.
"""
import ctypes
import http.client
import http.server
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import time

import testports

NAMED = 'TGBRIDGE_TEST_YARDR'
PROBE = (b'POST /call/RecordSkillAdded HTTP/1.1\r\nHost: localhost\r\nX-Yardr-Connection: tgbridge\r\n'
         b'Content-Type: application/json\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{}')
NO_SUCH_METHOD = {'error': {'code': 'error', 'message': 'no such method RecordSkillAdded'}}


def binary():
    """The build the native tests run, or None to skip them."""
    named = os.environ.get(NAMED)
    if named:
        if not (os.path.isabs(named) and os.access(named, os.X_OK)):
            raise AssertionError(f'{NAMED} must name an executable yardr by absolute path')
        return named
    return shutil.which('yardr')


class _ProcBsdInfo(ctypes.Structure):
    # Apple's sys/proc_info.h: PROC_PIDTBSDINFO (3), including microsecond start identity.
    _fields_ = [(name, ctypes.c_uint32) for name in
                'flags status xstatus pid ppid uid gid ruid rgid svuid svgid reserved'.split()] + [
        ('comm', ctypes.c_char * 16), ('name', ctypes.c_char * 32),
        ('nfiles', ctypes.c_uint32), ('pgid', ctypes.c_uint32), ('pjobc', ctypes.c_uint32),
        ('tdev', ctypes.c_uint32), ('tpgid', ctypes.c_uint32), ('nice', ctypes.c_int32),
        ('start_seconds', ctypes.c_uint64), ('start_microseconds', ctypes.c_uint64)]


def process_start(pid):
    """Match the core's kernel process identity without starting its real dispatcher."""
    if sys.platform == 'linux':
        start = int(Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[19])
    elif sys.platform == 'darwin':
        info = _ProcBsdInfo()
        query = ctypes.CDLL('/usr/lib/libproc.dylib', use_errno=True).proc_pidinfo
        query.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int]
        query.restype = ctypes.c_int
        if (query(pid, 3, 0, ctypes.byref(info), ctypes.sizeof(info)) != ctypes.sizeof(info)
                or info.pid != pid or info.start_microseconds >= 1000000):
            raise AssertionError('native test process identity unavailable')
        start = info.start_seconds * 1000000 + info.start_microseconds
    else:
        raise AssertionError('native test process identity unsupported on this platform')
    if start <= 0:
        raise AssertionError('native test process identity unavailable')
    return start


def start(root, binary):
    """Start an isolated server of binary, its home under root; return it with its home, command and environment.

    The caller stops the server with stop().
    """
    home = root / 'yard'
    home.mkdir(mode=0o700)
    env = dict(HOME=str(root), XDG_CONFIG_HOME=str(root / 'config'), XDG_STATE_HOME=str(root / 'state'),
               XDG_CACHE_HOME=str(root / 'cache'), PATH=str(root) + ':/usr/local/bin:/usr/bin:/bin', USER='pat')
    command = [binary, '--home', str(home), '--actor', 'pat']
    server = subprocess.Popen(command + ['server', 'run'], env=env, cwd=root,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.monotonic() + 15
    while not (home / 'yard.sock').exists():
        if server.poll() is not None or time.monotonic() > deadline:
            stop(server)
            raise AssertionError('isolated native server did not start')
        time.sleep(.02)
    return server, home, command, env


def stop(server):
    server.terminate()
    try:
        server.wait(timeout=10)
    except subprocess.TimeoutExpired:
        server.kill()
        server.wait(timeout=10)


def _log_tail(log_path, lines=20):
    """The last lines of the hook server log for a failure message; teardown deletes the file."""
    try:
        text = log_path.read_text()
    except OSError:
        return ''
    return '\n'.join(text.splitlines()[-lines:])


def restart_with_hook(root, server, command, env, cli, port=None):
    """Restart an isolated yard with a hook listener and return its bound port.

    A restart whose assigned port stays taken moves to a fresh port; the caller
    takes the returned port as the yard's address.
    """
    requested = port
    moved = False
    log_path = root / 'hook-server.log'
    try:
        for _ in range(3):
            if requested is None or moved:
                # The core settings require a concrete port. Keep it reserved through setup.
                with testports.bind_test_server(http.server.BaseHTTPRequestHandler) as probe:
                    port = probe.server_address[1]
                    cli('settings', 'set', 'hook_addr', f'127.0.0.1:{port}')
                    stop(server)
            else:
                stop(server)
            with log_path.open('w') as log:
                server = subprocess.Popen(command + ['server', 'run'], env=env, cwd=root,
                                          stdout=log, stderr=log)
            deadline = time.monotonic() + 30
            while True:
                text = log_path.read_text()
                if re.search(r'hook receiver ready[^\n]*\b127\.0\.0\.1:' + str(port) + r'\b', text):
                    return server, port
                failed = re.search(r'hook receiver not started[^\n]*', text)
                if failed:
                    if 'address already in use' in failed[0]:
                        # Another worker holds this port for its whole test; move to a fresh port.
                        moved = True
                        break
                    raise AssertionError('isolated hook receiver could not open its loopback listener: '
                                         + _log_tail(log_path))
                status = server.poll()
                if status is not None:
                    raise AssertionError(f'isolated yard exited with status {status} before hook readiness: '
                                         + _log_tail(log_path))
                if time.monotonic() > deadline:
                    raise AssertionError('isolated hook receiver did not report readiness within 30 seconds: '
                                         + _log_tail(log_path))
                time.sleep(.02)
        raise AssertionError('isolated hook receiver hit a port collision on all three attempts: '
                             + _log_tail(log_path))
    except BaseException:
        stop(server)
        raise


def _probe(home, request):
    """(status, decoded JSON answer or None) of one raw request to the isolated server."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(10)
        sock.connect(str(home / 'yard.sock'))
        sock.sendall(request)
        response = http.client.HTTPResponse(sock)
        try:
            response.begin()
            status, body = response.status, response.read()
        finally:
            response.close()
    try:
        return status, json.loads(body)
    except ValueError:
        return status, None


def aligned(home):
    """True for an aligned or upstream build: its server has no RecordSkillAdded method, which the alignment dropped.

    A pre-alignment build refuses the empty probe on its skill name, so it writes nothing. Any other answer,
    including one that is not JSON, means pre-alignment. This is historical: it says nothing of an optional
    feature a build adds or lacks since, which serves() asks about by the feature's own method.
    """
    status, answer = _probe(home, PROBE)
    return status == 200 and answer == NO_SUCH_METHOD


def serves(home, method):
    """Whether the isolated server serves the read method at all: a feature probe, not a version or alignment test.

    The empty call refuses its missing arguments on a build that has the method, and a build without it answers
    no such method; anything else fails the test, since neither says what the build offers.
    """
    status, answer = _probe(home, (f'POST /call/{method} HTTP/1.1\r\nHost: localhost\r\nX-Yardr-Connection: tgbridge\r\n'
                                   'Content-Type: application/json\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{}').encode())
    if status != 200 or not isinstance(answer, dict) or not answer.keys() & {'error', 'result'}:
        raise AssertionError(f'native feature probe of {method} answered neither its method nor its absence')
    return answer != {'error': {'code': 'error', 'message': 'no such method ' + method}}


def general_store_fixture(root):
    """Build a separate offline fixture against an unchanged candidate source, when supplied."""
    return store_fixture(root, 'general')


def _version_tuple(text):
    """Numeric prefix of a dotted version as a tuple; '1.27.4' -> (1, 27, 4)."""
    parts = []
    for piece in str(text).split('.'):
        digits = ''
        for ch in piece:
            if not ch.isdigit():
                break
            digits += ch
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts)


def _version_at_least(have, need):
    """True when version tuple have satisfies need, padding the shorter one."""
    width = max(len(have), len(need))
    return (have + (0,) * width)[:width] >= (need + (0,) * width)[:width]


def _go_requirement(source):
    """(minimum version, exact toolchain version or None) from source go.mod."""
    need, exact = (), None
    try:
        text = (Path(source) / 'go.mod').read_text()
    except OSError:
        return need, exact
    for line in text.splitlines():
        words = line.split()
        if len(words) < 2:
            continue
        if words[0] == 'go':
            need = _version_tuple(words[1])
        elif words[0] == 'toolchain':
            exact = _version_tuple(words[1].removeprefix('go'))
    return need, exact


def _cached_toolchain_go(source, build_env, platform=None):
    """Absolute path of an extracted cached toolchain go satisfying source.

    Switching toolchains verifies the toolchain against the checksum database
    even with GOPROXY=off, so a cold sumdb ticket breaks the offline fixture
    on the switch itself. Running the already-extracted cached toolchain
    directly never switches and stays offline. None when no runnable cached
    toolchain fits, and the caller falls back to the switching go.
    """
    modcache = build_env.get('GOMODCACHE', '').strip()
    if not modcache:
        return None
    if platform is None:
        try:
            probe = subprocess.run(['go', 'env', 'GOOS', 'GOARCH'], capture_output=True, text=True, timeout=30,
                                   env=dict(build_env, GOTOOLCHAIN='local'))
        except (OSError, subprocess.TimeoutExpired):
            return None
        if probe.returncode != 0:
            return None
        lines = probe.stdout.splitlines()
        if len(lines) < 2 or not all(line.strip() for line in lines[:2]):
            return None
        platform = (lines[0].strip(), lines[1].strip())
    goos, goarch = platform
    need, exact = _go_requirement(source)
    suffix = '.%s-%s' % (goos, goarch)
    best = None
    for entry in sorted((Path(modcache) / 'golang.org').glob('toolchain@*')):
        middle = entry.name.partition('-go')[2]
        if not middle or not middle.endswith(suffix):
            continue
        version = _version_tuple(middle[:len(middle) - len(suffix)])
        if not version:
            continue
        binary = entry / 'bin' / 'go'
        if not (binary.is_file() and os.access(binary, os.X_OK)):
            continue
        if exact is not None:
            if _version_at_least(version, exact) and _version_at_least(exact, version):
                return str(binary)
        elif _version_at_least(version, need):
            if best is None or not _version_at_least(version, best[0]):
                best = (version, str(binary))
    return best[1] if best else None


def _fallback_go(source, build_env):
    """Absolute path of the local go when it builds source offline, else None.

    The probe runs with GOTOOLCHAIN=local so a source needing a newer
    toolchain never verifies or fetches it from the network; the caller
    reports the missing warmed cache instead of attempting the switch.
    """
    probe_env = dict(build_env, GOTOOLCHAIN='local')
    try:
        probe = subprocess.run(['go', 'env', 'GOROOT', 'GOVERSION'], cwd=source,
                               capture_output=True, text=True, timeout=30, env=probe_env)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if probe.returncode != 0:
        return None
    lines = probe.stdout.splitlines()
    if len(lines) < 2 or not lines[0].strip():
        return None
    goroot, have = lines[0].strip(), _version_tuple(lines[1].strip().removeprefix('go'))
    need, exact = _go_requirement(source)
    if exact is not None:
        ok = _version_at_least(have, exact) and _version_at_least(exact, have)
    elif need:
        ok = _version_at_least(have, need)
    else:
        ok = True
    if not ok:
        return None
    candidate = Path(goroot) / 'bin' / 'go'
    if not (candidate.is_file() and os.access(candidate, os.X_OK)):
        return None
    return str(candidate)


def _fixture_want(source):
    """Human wording of the Go version source go.mod needs, for errors."""
    need, exact = _go_requirement(source)
    if exact is not None:
        return 'go' + '.'.join(str(n) for n in exact)
    if need:
        return 'go' + '.'.join(str(n) for n in need) + ' or newer'
    return 'the go.mod toolchain'


def store_fixture(root, name):
    """Build fixtures/<name>/store_fixture.go offline against the unchanged candidate source; None without one."""
    source = os.environ.get('TGBRIDGE_TEST_YARDR_SOURCE')
    if not source:
        return None
    source = Path(source)
    if not source.is_absolute() or not (source / 'go.mod').is_file():
        raise AssertionError('TGBRIDGE_TEST_YARDR_SOURCE must name the absolute candidate source directory')
    fixture = root / ('store-fixture' if name == 'general' else name + '-store-fixture')
    fixture.mkdir()
    mod = (source / 'go.mod').read_text().replace('module yardr.dev/yardr\n', 'module yardr.dev/yardr/telegram-fixture\n', 1)
    (fixture / 'go.mod').write_text(mod + '\nrequire yardr.dev/yardr v0.0.0\nreplace yardr.dev/yardr => ' + str(source) + '\n')
    shutil.copyfile(source / 'go.sum', fixture / 'go.sum')
    shutil.copyfile(Path(__file__).parent / 'fixtures' / name / 'store_fixture.go', fixture / 'main.go')
    binary = fixture / 'fixture'
    # The parallel gate runs each worker with an empty HOME, so the module and
    # build caches must be named explicitly: under the empty HOME the go tool
    # resolves them to a bare directory and the offline build fails with
    # `module lookup disabled by GOPROXY=off`. GOPROXY=off keeps the build
    # offline; the toolchain stays on automatic so the warmed cache's newer
    # toolchain is used (GOTOOLCHAIN=local refuses a source that needs it, and
    # GOSUMDB=off refuses to verify the cached toolchain). The build runs
    # through the extracted cached toolchain itself: asking the switching go
    # for GOROOT verifies the toolchain against the checksum database despite
    # GOPROXY=off, which breaks the offline fixture on a cold sumdb ticket.
    # The fallback stays offline too: it probes the local go with
    # GOTOOLCHAIN=local and reports the missing warmed cache when the local
    # go cannot build the source.
    build_env = dict(os.environ, GOPROXY='off')
    for key in ('GOMODCACHE', 'GOCACHE'):
        if not build_env.get(key):
            try:
                value = subprocess.check_output(
                    ['go', 'env', key], cwd=source, text=True, timeout=30,
                    env=dict(build_env, GOTOOLCHAIN='local')).strip()
            except (subprocess.CalledProcessError, OSError, subprocess.TimeoutExpired):
                value = ''
            if value:
                build_env[key] = value
    go_binary = _cached_toolchain_go(source, build_env)
    if go_binary is None:
        go_binary = _fallback_go(source, build_env)
        if go_binary is None:
            raise AssertionError(
                'synthetic native store fixture needs a warmed Go toolchain cache for '
                'TGBRIDGE_TEST_YARDR_SOURCE go.mod (%s); no extracted cached toolchain '
                'and the local go cannot build it offline' % _fixture_want(source))
        build_env = dict(build_env, GOTOOLCHAIN='local')
    result = subprocess.run(['nice', '-n10', go_binary, 'build', '-mod=mod', '-o', str(binary), '.'],
                            cwd=fixture, env=build_env,
                            capture_output=True, text=True, timeout=120)
    if result.returncode:
        raise AssertionError('synthetic native store fixture did not build: ' + result.stderr)
    return binary


def restart(root, binary, home, command, env):
    """Restart the same isolated store without creating or altering its home."""
    server = subprocess.Popen(command + ['server', 'run'], env=env, cwd=root,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.monotonic() + 15
    while True:
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.connect(str(home / 'yard.sock'))
            return server
        except OSError:
            if server.poll() is not None or time.monotonic() > deadline:
                stop(server)
                raise AssertionError('isolated native server did not restart')
            time.sleep(.02)
