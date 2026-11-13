#!/usr/bin/env python3
"""Export the complete reviewed runtime delta into a NEW release manifest."""
import argparse
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import tempfile

from common import ROOT, REQUIRED_CHECKS, ReleaseError, git, main_guard, sha256, write_json


def export(src, ref, version, revision, *, legacy=False):
    if not re.fullmatch(r'\d+\.\d+\.\d+\.\d+', version) or revision < 1:
        raise ReleaseError('invalid Chromium version or release revision')
    identity = f'{version}-simulated.{revision}'
    destination = ROOT / 'releases' / (identity + '.json')
    directory = ROOT / 'patches' / identity
    if destination.exists() or directory.exists():
        raise ReleaseError('release inputs already exist; choose a new revision')
    base = git(src, 'rev-parse', f'{version}^{{commit}}')
    tip = git(src, 'rev-parse', f'{ref}^{{commit}}')
    author, email, date = git(src, 'show', '-s', '--format=%an%n%ae%n%aI', tip).splitlines()
    commit_env = dict(os.environ, GIT_AUTHOR_NAME=author, GIT_AUTHOR_EMAIL=email,
                      GIT_COMMITTER_NAME=author, GIT_COMMITTER_EMAIL=email,
                      GIT_AUTHOR_DATE=date, GIT_COMMITTER_DATE=date)
    git(src, 'merge-base', '--is-ancestor', base, tip)
    groups = json.loads((ROOT / 'patches/groups.json').read_text())
    changed = git(src, 'diff', '--name-only', '--no-renames', base, tip).splitlines()
    allowed_legacy = {'README.md', 'docs/simulated_video_decode.md', 'docs/linux_arm64_release.md'}
    for path in changed:
        matches = [g for g in groups if any(path == p or
                   (p.endswith('/') and path.startswith(p)) for p in g['paths'])]
        if len(matches) != 1:
            if legacy and (path in allowed_legacy or path.startswith('tools/rillanetwork/')):
                continue
            raise ReleaseError(f'changed path must belong to exactly one patch group: {path}')
    directory.mkdir(parents=True)
    patches = []
    # A temporary index reconstructs the entire Chromium tree without checking it out.
    with tempfile.TemporaryDirectory() as temporary:
        env = dict(os.environ, GIT_INDEX_FILE=str(Path(temporary) / 'index'))
        subprocess.run(['git', 'read-tree', base], cwd=src, env=env, check=True)
        parent = base
        for group in groups:
            diff = subprocess.check_output(['git', 'diff', '--binary', '--full-index',
                                            base, tip, '--', *group['paths']], cwd=src)
            if not diff:
                raise ReleaseError(f"empty patch group: {group['name']}")
            subprocess.run(['git', 'apply', '--cached'], input=diff, cwd=src, env=env, check=True)
            tree = subprocess.check_output(['git', 'write-tree'], cwd=src, env=env, text=True).strip()
            commit = subprocess.check_output(['git', 'commit-tree', tree, '-p', parent,
                                               '-m', group['subject']], cwd=src, env=commit_env, text=True).strip()
            patch = directory / group['name']
            patch.write_bytes(subprocess.check_output(
                ['git', 'format-patch', '--stdout', '--no-signature', '-1', commit], cwd=src))
            patches.append({'path': str(patch.relative_to(ROOT)), 'sha256': sha256(patch)})
            parent = commit
    series = directory / 'series'
    series.write_text(''.join(p['path'] + '\n' for p in patches))
    configs = {}
    config_dir = ROOT / 'configs' / identity
    config_dir.mkdir(parents=True)
    for name in ('release', 'test'):
        config = config_dir / f'linux-{name}.gnargs'
        shutil.copyfile(ROOT / 'configs' / config.name, config)
        configs[name] = {'path': str(config.relative_to(ROOT)), 'sha256': sha256(config)}
    dependencies = json.loads((ROOT / 'tests/package.json').read_text())['dependencies']
    manifest = {
        'schema': 1, 'chromium_version': version, 'revision': revision,
        'upstream_tag': version, 'upstream_commit': base, 'expected_tree': tree,
        'exported_from': tip, 'series': str(series.relative_to(ROOT)), 'patches': patches,
        'configs': configs, 'architectures': ['x64', 'arm64'],
        'deliverables': ['chrome-deb', 'headless-tar'],
        'required_checks': sorted(REQUIRED_CHECKS),
        'playwright': dependencies['playwright-core'], 'shaka': dependencies['shaka-player'],
        'validation_scope': 'Software Widevine temporary sessions; no video presentation; native target runtime required.'}
    write_json(destination, manifest)
    print(destination)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--checkout', required=True, type=Path)
    p.add_argument('--source-ref', default='HEAD')
    p.add_argument('--version', required=True)
    p.add_argument('--revision', required=True, type=int)
    p.add_argument('--allow-legacy-metadata', action='store_true',
                   help='one-time migration: omit old README/docs/tools from the runtime series')
    a = p.parse_args()
    export(a.checkout.resolve(), a.source_ref, a.version, a.revision,
           legacy=a.allow_legacy_metadata)


if __name__ == '__main__':
    main_guard(main)
