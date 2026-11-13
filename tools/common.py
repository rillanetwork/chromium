"""Shared release invariants. No Chromium checkout is inferred from this file."""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]
TEST_FILTER = ('NullAudioDecoderTest.*:NullVideoDecoderTest.*:NullVideoSinkTest.*:'
               'DefaultDecoderFactoryTest.*:SupportedTypesTest.*')
ARCH = {'x64': 'amd64', 'arm64': 'arm64'}
REQUIRED_CHECKS = {'media-unit', 'chrome-media', 'shell-media', 'compatibility',
                   'widevine-vp9', 'widevine-avc1'}


class ReleaseError(RuntimeError):
    pass


def run(command, cwd=None, *, env=None):
    print('+ ' + ' '.join(map(str, command)), flush=True)
    subprocess.run(list(map(str, command)), cwd=cwd, env=env, check=True)


def capture(command, cwd=None, *, env=None):
    return subprocess.check_output(list(map(str, command)), cwd=cwd, env=env,
                                   text=True).strip()


def git(src, *args):
    return capture(['git', *args], src)


def sha256(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(json.dumps(value, indent=2) + '\n')
    temporary.replace(path)


def inside(root, relative):
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ReleaseError(f'path escapes root: {relative}')
    return path


def load_manifest(path, root=None):
    root = ROOT if root is None else root
    path = Path(path).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ReleaseError('manifest must live in the packaging checkout')
    m = json.loads(path.read_text())
    if m['schema'] != 1 or not re.fullmatch(r'\d+\.\d+\.\d+\.\d+', m['chromium_version']):
        raise ReleaseError('unsupported manifest schema or Chromium version')
    if not isinstance(m['revision'], int) or m['revision'] < 1:
        raise ReleaseError('release revision must be positive')
    for key in ('upstream_commit', 'expected_tree'):
        if not re.fullmatch('[0-9a-f]{40}', m[key]):
            raise ReleaseError(f'{key} must be a full Git object ID')
    if m['upstream_tag'] != m['chromium_version']:
        raise ReleaseError('upstream tag must match Chromium version')
    series = inside(root, m['series']).read_text().splitlines()
    series = [p.strip() for p in series if p.strip() and not p.startswith('#')]
    if series != [p['path'] for p in m['patches']] or len(set(series)) != len(series):
        raise ReleaseError('series order differs from manifest or has duplicates')
    for entry in [*m['patches'], *m['configs'].values()]:
        if sha256(inside(root, entry['path'])) != entry['sha256']:
            raise ReleaseError(f"input checksum mismatch: {entry['path']}")
    if set(m['required_checks']) != REQUIRED_CHECKS:
        raise ReleaseError('manifest must require every release validation check')
    if set(m['deliverables']) != {'chrome-deb', 'headless-tar'}:
        raise ReleaseError('both Chrome and headless deliverables are required')
    if not m['architectures'] or not set(m['architectures']) <= ARCH.keys():
        raise ReleaseError('unsupported target architecture')
    return m


def release_id(m):
    return f"{m['chromium_version']}-simulated.{m['revision']}"


def state_path(src):
    # Siblings/dependency checkouts in one gclient tree share this lock/state.
    return Path(src).resolve().parent / '.rillanetwork' / 'source.json'


@contextlib.contextmanager
def checkout_lock(src):
    path = state_path(src).parent / 'checkout.lock'
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ReleaseError('another release command owns this gclient checkout') from error
        yield


def clean(src):
    if git(src, 'status', '--porcelain', '--untracked-files=no'):
        raise ReleaseError('tracked/staged changes are present')
    for name in ('rebase-apply', 'rebase-merge', 'sequencer', 'CHERRY_PICK_HEAD'):
        path = Path(git(src, 'rev-parse', '--git-path', name))
        if not path.is_absolute():
            path = src / path
        if path.exists():
            raise ReleaseError('unfinished Git operation; resolve it before proceeding')


def verify_source(src, m):
    clean(src)
    git(src, 'merge-base', '--is-ancestor', m['upstream_commit'], 'HEAD')
    if git(src, 'rev-parse', 'HEAD^{tree}') != m['expected_tree']:
        raise ReleaseError('source tree does not match the complete pinned patch series')
    fields = dict(line.split('=', 1) for line in (src / 'chrome/VERSION').read_text().splitlines())
    actual = '.'.join(fields[k] for k in ('MAJOR', 'MINOR', 'BUILD', 'PATCH'))
    if actual != m['chromium_version']:
        raise ReleaseError('chrome/VERSION differs from manifest')


def verify_prepared(src, manifest, m):
    state = json.loads(state_path(src).read_text())
    if state.get('status') != 'prepared' or state['manifest_sha256'] != sha256(manifest):
        raise ReleaseError('prepare must finish with this exact manifest before building')
    verify_source(src, m)
    if state['source_commit'] != git(src, 'rev-parse', 'HEAD'):
        raise ReleaseError('source commit changed since preparation')
    return state


def packaging_revision():
    clean(ROOT)
    if git(ROOT, 'ls-files', '--others', '--exclude-standard'):
        raise ReleaseError('commit untracked packaging inputs before building')
    return git(ROOT, 'rev-parse', 'HEAD')


def main_guard(main):
    try:
        main()
    except (ReleaseError, subprocess.CalledProcessError, OSError, ValueError, KeyError) as error:
        raise SystemExit(f'error: {error}') from error
