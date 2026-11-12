"""The yardr build the native integration tests drive, and an isolated server of it.

TGBRIDGE_TEST_YARDR names the build by absolute path; unset, the yardr on PATH.
Whether a build predates the alignment is read from its running server: the
probe is a wire-level request, not a version string.
"""
import http.client
import json
import os
import shutil
import socket
import subprocess
import time

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


def aligned(home):
    """True for an aligned or upstream build: its server has no RecordSkillAdded method, which the alignment dropped.

    A pre-alignment build refuses the empty probe on its skill name, so it writes nothing. Any other answer,
    including one that is not JSON, means pre-alignment.
    """
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(10)
        sock.connect(str(home / 'yard.sock'))
        sock.sendall(PROBE)
        response = http.client.HTTPResponse(sock)
        try:
            response.begin()
            status, body = response.status, response.read()
        finally:
            response.close()
    try:
        answer = json.loads(body)
    except ValueError:
        return False
    return status == 200 and answer == NO_SUCH_METHOD
