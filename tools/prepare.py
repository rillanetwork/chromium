#!/usr/bin/env python3
"""Apply one complete, resumable series; never infer patches from Git history."""
import argparse
import json
from pathlib import Path

from common import (ROOT, ReleaseError, checkout_lock, clean, git, load_manifest,
                    main_guard, release_id, run, sha256, state_path, verify_source,
                    write_json)


def prepare(src, manifest, *, resume=False, remote='upstream', dry_run=False):
    m = load_manifest(manifest)
    branch = 'release/' + release_id(m)
    patches = [ROOT / p['path'] for p in m['patches']]
    if dry_run:
        print(f"Base: {m['upstream_tag']} ({m['upstream_commit']})\nBranch: {branch}")
        print('One git am sequence:\n' + '\n'.join(map(str, patches)))
        print(f"Required result tree: {m['expected_tree']}")
        return
    with checkout_lock(src):
        if resume:
            state = json.loads(state_path(src).read_text())
            if state['manifest_sha256'] != sha256(manifest) or state['branch'] != branch:
                raise ReleaseError('resume inputs differ from the saved preparation')
            if git(src, 'branch', '--show-current') != branch:
                raise ReleaseError('resume must run on the saved release branch')
        else:
            clean(src)
            if state_path(src).exists():
                old = json.loads(state_path(src).read_text())
                if old.get('status') == 'applying':
                    raise ReleaseError('unfinished preparation; use --resume or a separate checkout')
            run(['git', 'fetch', '--no-tags', remote, 'tag', m['upstream_tag']], src)
            if git(src, 'rev-parse', f"{m['upstream_tag']}^{{commit}}") != m['upstream_commit']:
                raise ReleaseError('upstream tag does not resolve to pinned commit')
            run(['git', 'switch', '-c', branch, m['upstream_commit']], src)
            state = {'status': 'applying', 'branch': branch,
                     'manifest_sha256': sha256(manifest), 'upstream_commit': m['upstream_commit']}
            write_json(state_path(src), state)
            # One invocation preserves the entire remaining queue after a conflict.
            try:
                run(['git', 'am', '--3way', *patches], src)
            except Exception:
                print('Resolve conflicts, git add the resolution, then git am --continue. '
                      'Repeat until the WHOLE queue finishes; rerun prepare.py with --resume. '
                      'If resolutions change the pinned tree, export a new release revision and apply it in a fresh checkout.')
                raise
        verify_source(src, m)
        state.update(status='prepared', source_commit=git(src, 'rev-parse', 'HEAD'))
        write_json(state_path(src), state)
        print(f'Prepared {branch}; complete patch tree verified.')


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('manifest', type=Path)
    p.add_argument('--checkout', required=True, type=Path, help='Chromium src directory')
    p.add_argument('--resume', action='store_true')
    p.add_argument('--upstream', default='upstream')
    p.add_argument('--dry-run', action='store_true')
    a = p.parse_args()
    prepare(a.checkout.resolve(), a.manifest.resolve(), resume=a.resume,
            remote=a.upstream, dry_run=a.dry_run)


if __name__ == '__main__':
    main_guard(main)
