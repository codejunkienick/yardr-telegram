#!/bin/sh
# Install one yard's Telegram bridge as its own release copy, idempotently:
#   install.sh --settings <file> [--root <dir>] [--dry-run]
#   install.sh --settings <file> [--root <dir>] --rollback [--dry-run]
# The instance's root (default ~/.local/share/yardr-telegram/<unit stem>):
#   releases/<commit>/  a git archive of this checkout's HEAD, with RELEASE and SHA256SUMS
#   current, previous   links to releases/<commit>
#   bin/telegram-bridge, bin/voice-transcribe   launchers with the settings' python
#   service.previous    the service file this install last replaced
#   packs/<digest>/     a release's yardr/ with its base line naming the settings' port
#   pack                a link to the active release's packs/<digest>, for yardr pack apply
# An install, in order:
# 1. checks python and the settings, and the contract: the instance runs,
#    reads and writes nothing in the source (this script's directory, with
#    links followed at every component), neither its root nor the settings
#    file nor any path the settings name (bridge.py source_dependencies);
#    then the source must be a git work tree with no tracked changes;
# 2. stages HEAD under releases/.stage-*, or reuses releases/<commit> when
#    its files match HEAD's and the settings depend on nothing in the source
#    its RELEASE records;
# 3. creates a missing callback or hook secret and the host-wide
#    transcription queue directory, owner-only: the only writes outside the
#    root before step 4, kept whatever happens after;
# 4. runs the staged or reused release's check-install; a failure removes
#    the stage and changes nothing else;
# 5. activates the release: previous -> the old current, current -> the
#    release, then the service file (its systemd user unit, or its launchd
#    plist on macOS; the replaced one kept in service.previous) and the
#    launchers, each only when it changed, restoring them all if one fails;
#    pack moves last, to the release's own rendered pack; one activation
#    of a root runs at a time, and a signal waits until it is done.
# --rollback swaps current and previous after the same checks of the
# previous release, and needs no git; pack returns to that release's.
# Removing an instance is disabling its service, removing its service file
# and rm -rf of its root.
# It never touches ~/.local/bin/voice-transcribe, never starts, stops or
# restarts a service and never contacts Telegram: the operator does that
# afterwards with the printed next: line (yardr/SETUP.md, step 9).
set -eu
pack=$(CDPATH= cd -- "$(dirname "$0")" && pwd -P)
usage='usage: install.sh --settings <file> [--root <dir>] [--rollback] [--dry-run]'
settings= root= dry_run=0 rollback=0
while [ $# -gt 0 ]; do
  case $1 in
    --settings) settings=${2:?--settings needs a file}; shift 2 ;;
    --settings=*) settings=${1#*=}; shift ;;
    --root) root=${2:?--root needs a directory}; shift 2 ;;
    --root=*) root=${1#*=}; shift ;;
    --dry-run) dry_run=1; shift ;;
    --rollback) rollback=1; shift ;;
    *) echo "install.sh: unknown argument $1 ($usage)" >&2; exit 64 ;;
  esac
done
[ -n "$settings" ] || { echo "install.sh: --settings <file> is required" >&2; exit 64; }
case $settings in /*) ;; *) settings="$PWD/$settings" ;; esac
case $root in /*|'') ;; *) root="$PWD/$root" ;; esac
bridge="$pack/bridge/bridge.py"
mode=install
[ "$rollback" = 0 ] || mode=rollback

python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))' \
  || { echo 'install.sh: python3 on PATH must be Python 3.11 or later (python3 --version)' >&2; exit 1; }

# A source path with a newline cannot be recorded in the line-based RELEASE.
if [ "$mode" = install ]; then
  case $pack in *'
'*) echo 'install.sh: the source path has a newline in it; install from a checkout whose path has none' >&2; exit 1 ;; esac
fi

# The settings are loaded once for the values this script needs; a settings
# error, or a root or settings value that depends on the source, is one line
# per problem on stderr and stops the script (set -eu) before anything is created.
settings_out=$(python3 -B -c '
import os, runpy, shlex, sys
from pathlib import Path
bridge, settings, pack, root, mode = sys.argv[1:]
try:
    m = runpy.run_path(bridge)
    s = m["load_settings"](settings)
    root = Path(os.path.normpath(root)) if root else Path.home() / ".local/share/yardr-telegram" / s.unit.removesuffix(".service")
    m["render_service"](s, bridge_path=root / "current/bridge/bridge.py")  # refuses an unsafe root
    # A rollback runs from a release, not a source: its check-install checks the settings against RELEASE.
    problems = m["source_dependencies"](pack, s if mode == "install" else None, root)
    if problems:
        sys.exit("\n".join("install.sh: " + line for line in problems))
    print("root=" + shlex.quote(str(root)))
    print("python=" + shlex.quote(str(s.python)))
    print("port=" + str(s.port))
    print("queue_dir=" + shlex.quote(str(s.queue_dir) if s.transcribe else ""))
    print("secret=" + shlex.quote(str(s.callback_secret_file)))
    print("hook_secret=" + shlex.quote(str(s.hook_secret_file)))
    print("service_file=" + shlex.quote(str(m["service_file"](s))))
    print("next_line=" + shlex.quote(m["service_next_line"](s, root / "bin/telegram-bridge")))
except Exception as exc:
    print(str(exc), file=sys.stderr)
    sys.exit(1)
' "$bridge" "$settings" "$pack" "$root" "$mode")
eval "$settings_out"

if [ "$mode" = install ]; then
  top=$(git -C "$pack" rev-parse --show-toplevel 2>/dev/null) && [ "$top" = "$pack" ] \
    && commit=$(git -C "$pack" rev-parse --verify -q 'HEAD^{commit}') \
    || { echo "install.sh: the source $pack is not a git work tree with a commit; install from a checkout" >&2; exit 1; }
  # GIT_OPTIONAL_LOCKS=0: a refusal leaves even the source's index as it was.
  [ -z "$(GIT_OPTIONAL_LOCKS=0 git -C "$pack" status --porcelain --untracked-files=no)" ] \
    || { echo "install.sh: the source $pack has tracked changes; commit or restore them first" >&2; exit 1; }
  target=releases/$commit
  # A reused release keeps the source its RELEASE records, so the settings
  # are checked against that source too, before anything is written.
  if [ -e "$root/$target" ]; then
    python3 -B -c '
import runpy, sys
bridge, settings, release, pack = sys.argv[1:]
m = runpy.run_path(bridge)
recorded = m["release_source"](release)
if recorded and recorded != pack:
    problems = m["source_dependencies"](recorded, m["load_settings"](settings))
    if problems:
        sys.exit("\n".join("install.sh: " + line for line in problems))
' "$bridge" "$settings" "$root/$target" "$pack"
  fi
else
  # A rollback activates the previous release, verified as installed.
  target=$(readlink "$root/previous" 2>/dev/null) || target=
  case $target in releases/*) ;; *) target= ;; esac
  case ${target#releases/} in ''|*[!0-9a-f]*) target= ;; esac
  if [ -z "$target" ] && [ -e "$root/service.previous" ]; then
    echo "install.sh: $root has no previous release; to undo the first install, restore $root/service.previous to $service_file" >&2
    exit 1
  elif [ -z "$target" ]; then
    echo "install.sh: $root has no previous release to roll back to" >&2
    exit 1
  fi
  why=$(python3 -B -c 'import runpy, sys; print(runpy.run_path(sys.argv[1])["release_mismatch"](sys.argv[2]) or "")' \
    "$bridge" "$root/$target")
  [ -z "$why" ] || { echo "install.sh: the previous release $root/$target: $why; not rolled back" >&2; exit 1; }
  echo "release $root/$target files match SHA256SUMS"
fi
release="$root/$target"
if [ -L "$root/releases" ]; then
  echo "install.sh: $root/releases is a link; this root keeps its releases in a directory of its own" >&2
  exit 1
fi
# The instance's pack is rendered from the release's yardr/ (the clean
# checkout's is HEAD's); one it cannot render or publish stops the script here.
if [ "$mode" = install ]; then pack_from=$pack/yardr; else pack_from=$release/yardr; fi
python3 -B -c '
import runpy, sys
try:
    runpy.run_path(sys.argv[1])["pack_plan"](sys.argv[2], sys.argv[3], int(sys.argv[4]))
except ValueError as error:
    sys.exit(f"install.sh: {error}")
except OSError as error:
    sys.exit(f"install.sh: cannot read {error.filename}: {error.strerror}")
' "$bridge" "$root" "$pack_from" "$port"

# Stage the commit under the root; it becomes the release only once its own
# check-install passes. An existing release of the commit is reused only
# when its files are the staged ones.
stage= made=
cleanup() {
  if [ -n "$stage" ]; then rm -rf "$stage"; fi
  for dir in $made; do rmdir "$dir" 2>/dev/null || :; done
}
code=$release
if [ "$mode" = install ] && [ "$dry_run" = 1 ]; then
  if [ -e "$release" ] || [ -L "$release" ]; then
    echo "would reuse $release once its files match HEAD's"
  else
    echo "would stage $commit as $release"
  fi
  code=$pack
elif [ "$mode" = install ]; then
  trap cleanup EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM
  for dir in "$root" "$root/releases"; do
    if [ ! -d "$dir" ]; then mkdir -p -m 700 "$dir"; made="$dir $made"; fi
  done
  stage=$(mktemp -d "$root/releases/.stage-XXXXXX")
  git -C "$pack" archive --format=tar -o "$stage/.archive.tar" "$commit"
  tar -xf "$stage/.archive.tar" -C "$stage"
  rm -f "$stage/.archive.tar"
  python3 -B -c '
import runpy, sys
from pathlib import Path
stage = Path(sys.argv[2])
sums = runpy.run_path(sys.argv[1])["release_sums"](stage)
(stage / "SHA256SUMS").write_text(sums)
' "$stage/bridge/bridge.py" "$stage"
  printf 'commit=%s\nsource=%s\ninstalled=%s\n' "$commit" "$pack" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >"$stage/RELEASE"
  if [ -e "$release" ] || [ -L "$release" ]; then
    why=$(python3 -B -c '
import runpy, sys
from pathlib import Path
stage, release = Path(sys.argv[2]), Path(sys.argv[3])
try:
    same = (release / "SHA256SUMS").read_bytes() == (stage / "SHA256SUMS").read_bytes()
except OSError:
    same = False
print(runpy.run_path(sys.argv[1])["release_mismatch"](release) or "" if same else "its SHA256SUMS differs")
' "$stage/bridge/bridge.py" "$stage" "$release")
    [ -z "$why" ] || { echo "install.sh: $release does not match HEAD ($why); move it aside, then run install.sh again" >&2; exit 1; }
    echo "release $commit reused from $release"
    # Its check-install judges the settings against the source it records.
    code=$release
  else
    echo "staged $commit"
    code=$stage
  fi
fi

# The host-wide transcription queue every yard's bridge shares; the unit
# cannot create it (its home is read-only), so it is made here.
if [ -n "$queue_dir" ] && [ ! -d "$queue_dir" ]; then
  if [ "$dry_run" = 1 ]; then
    echo "would create $queue_dir (mode 700)"
  else
    mkdir -p "$queue_dir" && chmod 700 "$queue_dir" && echo "created $queue_dir"
  fi
fi

# The two secrets: the callback secret the yard's source sends as its
# credential, and the hook secret the bridge sends to the yard's hook. The
# unit may write only its state and lock directories, so a missing secret
# is minted here, owner-only, and never printed.
for pair in "callback:$secret" "hook:$hook_secret"; do
  what=${pair%%:*}
  file=${pair#*:}
  if [ ! -e "$file" ] && [ ! -L "$file" ]; then
    if [ "$dry_run" = 1 ]; then
      echo "would create $what secret $file (mode 600)"
    else
      python3 -B -c 'import runpy, sys; m = runpy.run_path(sys.argv[1]); m["capability"](sys.argv[2])' "$bridge" "$file" \
        && echo "created $what secret $file (mode 600)"
    fi
  fi
done

# The release is checked by the interpreter its service and launchers run.
"$python" -B "$code/bridge/bridge.py" --settings "$settings" check-install

# Activate: every new file and link is written beside its destination
# first, then each is renamed into place; a failure puts back what was
# already replaced, so the instance keeps the release it had.
python3 -B - "$code/bridge/bridge.py" "$settings" "$root" "$target" "$stage" "$dry_run" "$bridge" <<'EOF'
import fcntl, os, plistlib, runpy, shlex, shutil, signal, sys, tempfile
from pathlib import Path
bridge, settings, root, target, stage, dry_run, renderer = sys.argv[1:]
root, dry_run = Path(root), dry_run == '1'
m = runpy.run_path(bridge)
r = runpy.run_path(renderer)  # this script's own: an older release has no pack_plan
s = m['load_settings'](settings)
current_bridge = root / 'current/bridge/bridge.py'
python = shlex.quote(str(s.python))
service = Path(m['service_file'](s))
files = {service: (m['render_service'](s, bridge_path=current_bridge).encode(), 0o644)}
files[root / 'bin/telegram-bridge'] = (
    f'#!/bin/sh\nexec {python} {current_bridge} --settings {shlex.quote(settings)} "$@"\n'.encode(), 0o755)
files[root / 'bin/voice-transcribe'] = (
    f'#!/bin/sh\nexec {python} -B {root / "current/transcriber/voice_transcribe.py"} "$@"\n'.encode(), 0o755)


def read(path):
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def written(path, text, mode):
    """Whether path is a file of its own holding text with mode, so it is left alone."""
    return not path.is_symlink() and read(path) == text and os.stat(path).st_mode & 0o7777 == mode


def bridge_of(text):
    """The bridge path a service file runs, for the operator's eyes only."""
    try:
        if text.lstrip().startswith(b'<?xml'):
            return plistlib.loads(text)['ProgramArguments'][1]
        line = next(l for l in text.decode().splitlines() if l.startswith('ExecStart='))
        return line.split()[1]
    except Exception:
        return 'an unreadable ExecStart'


if not dry_run:
    # One activation of a root at a time: a failed one cannot take back a release another adopted.
    lock = os.open(root, os.O_RDONLY)
    fcntl.flock(lock, fcntl.LOCK_EX)
current = os.readlink(root / 'current') if (root / 'current').is_symlink() else None
if current is None and (root / 'current').exists():
    sys.exit(f'install.sh: {root / "current"} is not a link; not activated')
links = {}
if current == target:
    print(f'release {target} is current')
else:
    if current is not None:
        links[root / 'previous'] = current
    links[root / 'current'] = target
changes = [(path, text, mode) for path, (text, mode) in files.items() if not written(path, text, mode)]
old_service = read(service)
if old_service is not None and old_service != files[service][0]:
    changes.insert(0, (root / 'service.previous', old_service, 0o600))
    print(f'{"would replace" if dry_run else "replacing"} {service}: bridge {bridge_of(old_service)} -> '
          f'{current_bridge}; the old file is kept in {root / "service.previous"}')
for path, (text, mode) in files.items():
    if written(path, text, mode):
        print(f'{"unit" if path == service else "launcher"} {path} is current')
# The pack is a new generation beside the old ones, and the link moves to it.
try:
    # A reused release another activation took back is renamed from this stage, which holds its files.
    release = Path(stage) if stage and not (root / target).exists() else Path(bridge).parents[1]
    pack_files, generation, pack_state = r['pack_plan'](root, release / 'yardr', s.port)
except (ValueError, OSError) as error:
    sys.exit(f'install.sh: {error}; not activated')
pack_line = f'pack {root / "pack"} (base http://127.0.0.1:{s.port})'
if pack_state == 'current':
    print(f'pack {root / "pack"} is current')
if dry_run:
    for link, value in links.items():
        print(f'would link {link} -> {value}')
    for path, text, _ in changes:
        print(f'would write {path}' + (':\n' + text.decode().rstrip('\n') if path == service else ''))
    if pack_state != 'current':
        print('would write ' + pack_line)
    sys.exit(0)

prepared = []  # (temporary, destination, the destination's old link or bytes)
pack_stage = None
try:
    if pack_state == 'write':
        (root / 'packs').mkdir(mode=0o700, parents=True, exist_ok=True)
        pack_stage = Path(tempfile.mkdtemp(prefix='.stage-', dir=root / 'packs'))
        r['stage_pack'](pack_files, pack_stage)
    for link, value in links.items():
        tmp = Path(tempfile.mktemp(prefix=f'.{link.name}.', dir=root))
        os.symlink(value, tmp)
        prepared.append((tmp, link, ('link', os.readlink(link) if link.is_symlink() else None)))
    for path, text, mode in changes:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=f'.{path.name}.', dir=path.parent)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(text)
        os.chmod(tmp, mode)
        if path.is_symlink():
            prepared.append((Path(tmp), path, ('link', os.readlink(path))))
        else:
            prepared.append((Path(tmp), path, ('file', (read(path), os.stat(path).st_mode & 0o7777)
                                               if path.exists() else None)))
    # The pack link moves last, so a stop before it keeps the previous pack.
    if pack_state != 'current':
        tmp = Path(tempfile.mktemp(prefix='.pack.', dir=root))
        os.symlink(generation, tmp)
        prepared.append((tmp, root / 'pack', ('link', os.readlink(root / 'pack') if (root / 'pack').is_symlink() else None)))
except OSError as error:
    for tmp, _, _ in prepared:
        tmp.unlink(missing_ok=True)
    if pack_stage:
        shutil.rmtree(pack_stage, ignore_errors=True)
    sys.exit(f'install.sh: cannot write beside {error.filename}: {error.strerror}; not activated')

# A signal waits until the renames, or their undo, are all done.
signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT, signal.SIGTERM, signal.SIGHUP})
done = []
try:
    if stage and not (root / target).exists():
        os.rename(stage, root / target)
        done.append((None, root / target, ('stage', stage)))
    if pack_stage:
        os.rename(pack_stage, root / generation)
        done.append((None, root / generation, ('stage', pack_stage)))
    for tmp, path, old in prepared:
        os.replace(tmp, path)
        done.append((tmp, path, old))
except OSError as error:
    for tmp, path, (kind, old) in reversed(done):
        if kind == 'stage':
            os.rename(path, old)
        elif old is None:
            path.unlink(missing_ok=True)
        elif kind == 'link':
            os.symlink(old, tmp)
            os.replace(tmp, path)
        else:
            path.write_bytes(old[0])
            os.chmod(path, old[1])
    for tmp, _, _ in prepared:
        tmp.unlink(missing_ok=True)
    if pack_stage:
        shutil.rmtree(pack_stage, ignore_errors=True)
    sys.exit(f'install.sh: activating {error.filename} failed: {error.strerror}; '
             'the previous release, service file, launchers and pack are back in place')
finally:
    signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGINT, signal.SIGTERM, signal.SIGHUP})
for link, value in links.items():
    print(f'linked {link} -> {value}')
for path, _, _ in changes:
    print(f'wrote {path}')
if pack_state != 'current':
    print('wrote ' + pack_line)
EOF

link="$HOME/.local/bin/voice-transcribe"
if [ -e "$link" ] || [ -L "$link" ]; then
  echo "note: $link is left as it is; this instance's own is $root/bin/voice-transcribe"
fi
echo "$next_line"
