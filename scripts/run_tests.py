#!/usr/bin/env python3
"""Parallel unittest runner for the bridge and yardr pack test suites.

Discovers the exact suites of `bridge` and `yardr/tests` via unittest, runs them
across isolated worker subprocesses (stdlib only), and reports per-unit failures
and exact aggregated counts. Use `--serial` for classic serial discovery or pass
a test file path (e.g. `bridge/test_albums.py`) to run one file directly.
"""
import argparse
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
SUITES = ('bridge', 'yardr/tests')
DEFAULT_JOBS_CAP = 8
# Classes whose methods are slow enough to shard individually across workers.
SPLIT_BY_METHOD = frozenset({
    ('bridge', 'test_run_asks', 'NativeTests'),
    ('bridge', 'test_run_asks', 'RealNativeTests'),
    ('bridge', 'test_run_asks', 'NativeSettingsTests'),
    ('bridge', 'test_settings', 'InstanceTests'),
    ('bridge', 'test_settings', 'InstallScriptTests'),
})
# Modules split by TestCase class so heavy modules spread across workers.
SPLIT_BY_CLASS = frozenset({'test_run_asks', 'test_settings'})


def _iter_cases(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from _iter_cases(item)
        else:
            yield item


def _parse_unittest_flags(raw_args):
    """Split recognized unittest flags/patterns from positional targets."""
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument('-v', '--verbose', action='count', default=0)
    parser.add_argument('-q', '--quiet', action='count', default=0)
    parser.add_argument('-f', '--failfast', action='store_true')
    parser.add_argument('-b', '--buffer', action='store_true')
    parser.add_argument('-c', '--catch', action='store_true')
    parser.add_argument('--locals', action='store_true')
    parser.add_argument('-k', '--test-name-pattern', action='append', dest='patterns')
    parser.add_argument('-p', '--pattern', default=None)
    known, rest = parser.parse_known_args(raw_args)
    flags = [a for a in raw_args if a not in rest]
    # Reconstruct exact flag list cleanly for forwarding.
    forwarded = []
    if known.verbose:
        forwarded.append('-' + 'v' * known.verbose)
    elif known.quiet:
        forwarded.append('-q')
    if known.failfast:
        forwarded.append('-f')
    if known.buffer:
        forwarded.append('-b')
    if known.catch:
        forwarded.append('-c')
    if known.locals:
        forwarded.append('--locals')
    for pat in known.patterns or ():
        forwarded.extend(['-k', pat])
    return known, forwarded, rest


def _run_worker(suite_dir, result_path, raw_args, test_ids):
    suite_path = str(ROOT / suite_dir)
    if suite_path not in sys.path:
        sys.path.insert(0, suite_path)
    known, _, _ = _parse_unittest_flags(raw_args)
    verbosity = 1 + known.verbose - known.quiet
    if verbosity < 0:
        verbosity = 0
    loader = unittest.TestLoader()
    if known.patterns:
        loader.testNamePatterns = [
            p if ('*' in p or '?' in p or '[' in p) else f'*{p}*' for p in known.patterns
        ]
    suite = loader.loadTestsFromNames(test_ids)
    runner = unittest.TextTestRunner(
        stream=sys.stderr,
        verbosity=verbosity,
        failfast=known.failfast,
        buffer=known.buffer,
        tb_locals=known.locals,
    )
    started = time.monotonic()
    result = runner.run(suite)
    elapsed = time.monotonic() - started
    summary = {
        'ran': result.testsRun,
        'skipped': len(result.skipped),
        'failures': len(result.failures),
        'errors': len(result.errors),
        'expected_failures': len(result.expectedFailures),
        'unexpected_successes': len(result.unexpectedSuccesses),
        'was_successful': result.wasSuccessful(),
        'duration': elapsed,
    }
    Path(result_path).write_text(json.dumps(summary), encoding='utf-8')
    return 0 if result.wasSuccessful() else 1


def _discover_suite(suite_dir, patterns, discover_pattern=None):
    """Return ordered list of leaf test IDs discovered in `suite_dir`."""
    suite_path = str(ROOT / suite_dir)
    added = suite_path not in sys.path
    if added:
        sys.path.insert(0, suite_path)
    try:
        loader = unittest.TestLoader()
        if patterns:
            loader.testNamePatterns = [
                p if ('*' in p or '?' in p or '[' in p) else f'*{p}*' for p in patterns
            ]
        discovered = loader.discover(suite_path,
                                     pattern='test*.py' if discover_pattern is None else discover_pattern,
                                     top_level_dir=suite_path)
        return [case.id() for case in _iter_cases(discovered)]
    finally:
        if added and suite_path in sys.path:
            sys.path.remove(suite_path)


def _priority(suite_dir, label, test_ids):
    """Heuristic descending weight so the longest tests start immediately."""
    weights = (
        ('test_source_and_pack_carried_leaves', 1200000),
        ('test_refusals_write_nothing', 1100000),
        ('test_an_interrupted_or_concurrent_activation', 1050000),
        ('test_source_and_pack_every_does_row', 1000000),
        ('test_source_and_pack_prose_values', 950000),
        ('test_two_instances_stay_independent', 900000),
        ('test_model_switch', 850000),
        ('test_depot_pack_review_and_envelope_fail_closed', 800000),
        ('InstanceTests', 500000),
        ('InstallScriptTests', 450000),
    )
    for needle, weight in weights:
        if needle in label:
            return (weight, label)
    if (suite_dir, label.split('.')[0], label.split('.')[1] if '.' in label else '') in SPLIT_BY_METHOD:
        return (300000, label)
    mod_name = label.split('.', 1)[0]
    mod_file = ROOT / suite_dir / f'{mod_name}.py'
    size = mod_file.stat().st_size if mod_file.is_file() else 0
    return (size + len(test_ids) * 100, label)


def _build_units(patterns, discover_pattern=None):
    """Build work units across both suites without losing or duplicating any test."""
    units = []
    for suite_dir in SUITES:
        ids = _discover_suite(suite_dir, patterns, discover_pattern)
        by_module = {}
        for tid in ids:
            mod = tid.split('.', 1)[0]
            by_module.setdefault(mod, []).append(tid)
        for mod, mod_ids in by_module.items():
            if mod not in SPLIT_BY_CLASS:
                units.append((suite_dir, mod, mod_ids))
                continue
            by_class = {}
            for tid in mod_ids:
                parts = tid.split('.')
                cls = parts[1] if len(parts) >= 2 else ''
                by_class.setdefault(cls, []).append(tid)
            for cls, cls_ids in by_class.items():
                if (suite_dir, mod, cls) in SPLIT_BY_METHOD:
                    for tid in cls_ids:
                        units.append((suite_dir, tid, [tid]))
                else:
                    units.append((suite_dir, f'{mod}.{cls}' if cls else mod, cls_ids))
    units.sort(key=lambda u: _priority(*u), reverse=True)
    return units


def _host_go_caches():
    """Real GOMODCACHE/GOCACHE for fixture builds; workers run with an empty HOME.

    testyardr builds its Go fixtures with GOPROXY=off, which only reads the
    module cache. Under a worker's empty HOME the go tool resolves both caches
    to a bare directory and the build fails, so the parent (which still has
    the real HOME) resolves them once here and each worker inherits them.
    Explicitly set values always win; a missing go tool just yields nothing.
    """
    caches = {}
    for key in ('GOMODCACHE', 'GOCACHE'):
        value = os.environ.get(key, '').strip()
        if value:
            caches[key] = value
    missing = [key for key in ('GOMODCACHE', 'GOCACHE') if key not in caches]
    if missing:
        try:
            probe = subprocess.run(['go', 'env', *missing], capture_output=True, text=True, timeout=30,
                                   env=dict(os.environ, GOTOOLCHAIN='local'))
        except (OSError, subprocess.TimeoutExpired):
            probe = None
        if probe is not None and probe.returncode == 0:
            for key, line in zip(missing, probe.stdout.splitlines()):
                if line.strip():
                    caches[key] = line.strip()
    return caches


def _worker_env(home_dir, tmp_dir, go_caches, slot_port_range):
    """Isolated worker env: empty HOME/TMPDIR plus the host Go caches.

    An exported-but-empty GOMODCACHE/GOCACHE must not shadow the probed host
    paths: Go reads an empty value as unset and resolves both caches under
    the worker's empty HOME, so the offline fixture build fails. Explicit
    non-blank values always win.
    """
    env = os.environ.copy()
    env['HOME'] = str(home_dir)
    for key, value in go_caches.items():
        if not env.get(key, '').strip():
            env[key] = value
    env['TMPDIR'] = str(tmp_dir)
    env['PYTHONDONTWRITEBYTECODE'] = '1'
    if slot_port_range is not None:
        env['TEST_PORT_RANGE'] = slot_port_range
    return env


def _partition_port_range(raw, slots):
    """Split TEST_PORT_RANGE across worker slots when wide enough, else None."""
    if not raw or slots <= 1:
        return [raw] * slots
    try:
        low_s, high_s = raw.split('-', 1)
        low, high = int(low_s, 10), int(high_s, 10)
    except ValueError:
        return [raw] * slots
    total = high - low + 1
    if total < slots * 8:
        return [raw] * slots
    step = total // slots
    ranges = []
    for idx in range(slots):
        sub_low = low + idx * step
        sub_high = high if idx == slots - 1 else sub_low + step - 1
        ranges.append(f'{sub_low}-{sub_high}')
    return ranges


def _suite_file(arg):
    """Map a test file path to (suite_dir, file name), or None when not a suite file."""
    candidate = Path(arg)
    if not candidate.is_file():
        return None
    resolved = candidate.resolve()
    for suite_dir in SUITES:
        suite_root = (ROOT / suite_dir).resolve()
        if resolved.parent == suite_root:
            return (suite_dir, resolved.name)
    return None


def _run_target(args, unittest_args, serial=False):
    """Run suite files as per-file discovers, other targets directly with unittest."""
    env = None
    if serial:
        env = os.environ.copy()
        env['TGBRIDGE_TEST_JOBS'] = '1'
    mapped = [_suite_file(arg) for arg in args]
    if args and all(item is not None for item in mapped):
        rc = 0
        for suite_dir, name in mapped:
            proc_rc = subprocess.run(
                [sys.executable, '-B', '-m', 'unittest', 'discover', '-s', suite_dir,
                 '-p', name, *unittest_args],
                cwd=ROOT, env=env,
            ).returncode
            if proc_rc != 0 and rc == 0:
                rc = proc_rc
        return rc
    return subprocess.run([sys.executable, '-B', '-m', 'unittest', *args, *unittest_args],
                          cwd=ROOT, env=env).returncode


def _run_serial(unittest_args, has_pattern=False, discover_pattern=None):
    ran_any = False
    env = os.environ.copy()
    env['TGBRIDGE_TEST_JOBS'] = '1'
    pattern_args = ['-p', discover_pattern] if discover_pattern is not None else []
    for suite_dir in SUITES:
        rc = subprocess.run(
            [sys.executable, '-B', '-m', 'unittest', 'discover', '-s', suite_dir,
             *pattern_args, *unittest_args],
            cwd=ROOT, env=env,
        ).returncode
        if rc == 0:
            ran_any = True
        elif rc == 5 and has_pattern:
            continue
        else:
            return rc
    return 0 if (ran_any or not has_pattern) else 5


def _format_summary(name, stats, wall_s):
    status = 'OK' if (stats['failures'] == 0 and stats['errors'] == 0 and stats['unexpected_successes'] == 0) else 'FAILED'
    details = []
    if stats['failures']:
        details.append(f"failures={stats['failures']}")
    if stats['errors']:
        details.append(f"errors={stats['errors']}")
    if stats['skipped']:
        details.append(f"skipped={stats['skipped']}")
    if stats['expected_failures']:
        details.append(f"expected failures={stats['expected_failures']}")
    if stats['unexpected_successes']:
        details.append(f"unexpected successes={stats['unexpected_successes']}")
    suffix = f" ({', '.join(details)})" if details else ''
    return f"{name}: Ran {stats['ran']} tests in {wall_s:.3f}s\n{status}{suffix}"


def _default_jobs():
    raw = os.environ.get('TGBRIDGE_TEST_JOBS', '').strip()
    if raw:
        try:
            value = int(raw, 10)
            if value >= 1:
                return value
        except ValueError:
            pass
    cpus = os.cpu_count() or 1
    base = min(cpus, DEFAULT_JOBS_CAP)
    if base <= 1:
        return base
    try:
        load1 = os.getloadavg()[0]
    except (OSError, AttributeError):
        return base
    return min(base, max(2, cpus - int(load1)))


def main(argv=None):
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    if len(raw_argv) >= 3 and raw_argv[0] == '--worker':
        _, suite_dir, result_path, *rest = raw_argv
        sep = rest.index('--')
        return _run_worker(suite_dir, result_path, rest[:sep], rest[sep + 1:])

    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument('--serial', action='store_true')
    parser.add_argument('-j', '--jobs', type=int, default=None)
    parser.add_argument('-h', '--help', action='store_true')
    top_args, remaining = parser.parse_known_args(raw_argv)

    if top_args.help:
        print(
            'usage: sh scripts/test.sh [--serial] [-j N] [unittest options | test file]\n'
            '  --serial         run bridge then yardr/tests serially\n'
            '  -j, --jobs N     worker process count (default: min(cpus, 8) scaled down under load, or $TGBRIDGE_TEST_JOBS)\n'
            '  <file>           run one test file (e.g. bridge/test_albums.py)\n'
            '  unittest flags   -v, -q, -f/--failfast, -b/--buffer, -c/--catch, --locals, -k PATTERN, -p PATTERN'
        )
        return 0

    known, unittest_args, positional = _parse_unittest_flags(remaining)
    for arg in positional:
        if arg.startswith('-') and len(arg) > 1:
            sys.stderr.write(
                f'sh scripts/test.sh: unsupported option {arg!r}; supported options: '
                '--serial, -j N, -v, -q, -f, -b, -c, --locals, -k PATTERN, -p PATTERN, <test file>\n'
            )
            return 2
    if positional and known.pattern:
        sys.stderr.write(
            'sh scripts/test.sh: -p/--pattern cannot be combined with explicit test targets; '
            'run without targets to filter by pattern, or pass a file path alone\n'
        )
        return 2
    has_filter = bool(known.patterns) or bool(known.pattern)
    if positional:
        serial_mode = top_args.serial or (top_args.jobs is not None and top_args.jobs <= 1)
        return _run_target(positional, unittest_args, serial_mode)
    if top_args.serial:
        return _run_serial(unittest_args, has_filter, known.pattern)

    jobs = top_args.jobs if top_args.jobs is not None else _default_jobs()
    if jobs <= 1:
        return _run_serial(unittest_args, has_filter, known.pattern)

    units = _build_units(known.patterns, known.pattern)
    if not units:
        return _run_serial(unittest_args, has_filter, known.pattern)

    worker_count = min(jobs, len(units))
    port_ranges = _partition_port_range(os.environ.get('TEST_PORT_RANGE'), worker_count)
    go_caches = _host_go_caches()
    script_path = str(Path(__file__).resolve())
    run_tmp = Path(tempfile.mkdtemp(prefix='yt-', dir='/tmp'))
    work_q = queue.Queue()
    for idx, unit in enumerate(units):
        work_q.put((idx, unit))

    lock = threading.Lock()
    stop_event = threading.Event()
    results = []
    suite_stats = {
        s: {'ran': 0, 'skipped': 0, 'failures': 0, 'errors': 0,
            'expected_failures': 0, 'unexpected_successes': 0}
        for s in SUITES
    }

    def worker_loop(slot_id):
        slot_port_range = port_ranges[slot_id]
        while not stop_event.is_set():
            try:
                idx, (suite_dir, label, test_ids) = work_q.get_nowait()
            except queue.Empty:
                return
            unit_dir = run_tmp / f'u{slot_id}-{idx}'
            home_dir = unit_dir / 'home'
            tmp_dir = unit_dir / 'tmp'
            home_dir.mkdir(parents=True, mode=0o700)
            tmp_dir.mkdir(parents=True, mode=0o700)
            result_path = unit_dir / 'result.json'
            env = _worker_env(home_dir, tmp_dir, go_caches, slot_port_range)
            cmd = [
                sys.executable, '-B', script_path, '--worker',
                suite_dir, str(result_path), *unittest_args, '--', *test_ids,
            ]
            started = time.monotonic()
            proc = subprocess.run(cmd, cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            wall = time.monotonic() - started
            summary = None
            if result_path.is_file():
                try:
                    summary = json.loads(result_path.read_text(encoding='utf-8'))
                except ValueError:
                    summary = None
            shutil.rmtree(unit_dir, ignore_errors=True)
            failed = proc.returncode != 0 or summary is None or not summary.get('was_successful')
            with lock:
                results.append((suite_dir, label, proc.returncode, wall, summary, proc.stdout, failed))
                if summary is not None:
                    st = suite_stats[suite_dir]
                    for key in st:
                        st[key] += summary.get(key, 0)
                elif failed:
                    suite_stats[suite_dir]['errors'] += 1
                if known.verbose and not failed:
                    sys.stderr.write(f'[{suite_dir}] {label} ({wall:.2f}s)\n')
                    if proc.stdout:
                        sys.stderr.write(proc.stdout)
                        if not proc.stdout.endswith('\n'):
                            sys.stderr.write('\n')
                    sys.stderr.flush()
                if failed:
                    sys.stderr.write(f'\n=== FAIL [{suite_dir}] {label} (exit {proc.returncode}, {wall:.2f}s) ===\n')
                    sys.stderr.write(proc.stdout or '(no output)\n')
                    if proc.stdout and not proc.stdout.endswith('\n'):
                        sys.stderr.write('\n')
                    sys.stderr.flush()
                    if known.failfast:
                        stop_event.set()
            work_q.task_done()

    t0 = time.monotonic()
    try:
        threads = [threading.Thread(target=worker_loop, args=(slot,), daemon=True) for slot in range(worker_count)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    finally:
        shutil.rmtree(run_tmp, ignore_errors=True)

    total_wall = time.monotonic() - t0
    any_failed = any(item[-1] for item in results)
    for suite_dir in SUITES:
        sys.stderr.write('\n' + _format_summary(suite_dir, suite_stats[suite_dir], total_wall) + '\n')
    total_ran = sum(st['ran'] for st in suite_stats.values())
    total_skipped = sum(st['skipped'] for st in suite_stats.values())
    sys.stderr.write(f'Total: {total_ran} tests ({total_skipped} skipped) in {total_wall:.3f}s wall ({worker_count} workers)\n')
    return 1 if any_failed else 0


if __name__ == '__main__':
    sys.exit(main())
