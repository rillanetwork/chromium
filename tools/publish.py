#!/usr/bin/env python3
"""Bind validated artifacts to an annotated source tag, then publish explicitly."""
import argparse
import json
from pathlib import Path
import subprocess
import tempfile

from common import ARCH, ROOT, ReleaseError, checkout_lock, git, main_guard, run, sha256
from validate import verify_build, verify_validation


def tag_message(record, b, validation):
    return (f"Rillanetwork Chromium {b['release']} Linux {ARCH[b['target_cpu']]}\n\n"
            f"Source: {b['source_commit']}\nPackaging: {b['packaging_commit']}\n"
            f"Build-SHA256: {sha256(record)}\nValidation-SHA256: {sha256(validation)}\n")


def publish(src, record, *, remote='origin', repo='rillanetwork/chromium',
            dry_run=False, tag_only=False, draft=False):
    with checkout_lock(src):
        b, m = verify_build(record, src)
        validation = verify_validation(record, b)
        tag = f"v{b['release']}-linux-{ARCH[b['target_cpu']]}"
        message = tag_message(record, b, validation)
        exists = subprocess.run(['git', 'show-ref', '--verify', '--quiet', f'refs/tags/{tag}'], cwd=src).returncode == 0
        if exists:
            if (git(src, 'cat-file', '-t', f'refs/tags/{tag}') != 'tag' or
                    git(src, 'rev-parse', f'{tag}^{{commit}}') != b['source_commit'] or
                    git(src, 'for-each-ref', '--format=%(contents)', f'refs/tags/{tag}').strip() != message.strip()):
                raise ReleaseError('existing tag does not bind these exact source/artifact/validation records')
        assets = [record, validation, record.parent / json.loads(validation.read_text())['evidence_archive']]
        for artifact in b['artifacts']:
            path = record.parent / artifact['file']
            sidecar = path.with_name(path.name + '.sha256')
            expected = f"{artifact['sha256']}  {artifact['file']}\n"
            if sidecar.read_text() != expected:
                raise ReleaseError('checksum sidecar differs from build record')
            assets.extend([path, sidecar])
        if dry_run:
            print(message + f'\nTag: {tag}\nAssets:\n' + '\n'.join(map(str, assets)))
            print('No tags, pushes, or releases created.')
            return
        if not exists:
            run(['git', 'tag', '-a', tag, b['source_commit'], '-m', message], src)
        if tag_only:
            print(f'Created/verified local tag {tag}; nothing published.')
            return
        run(['gh', 'auth', 'status', '--hostname', 'github.com'], ROOT)
        existing = subprocess.run(['gh', 'release', 'view', tag, '--repo', repo],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if existing.returncode == 0:
            raise ReleaseError('release already exists; use a new revision')
        # Make the small, pinned control history reachable as well as the source tag.
        run(['git', 'push', remote, 'HEAD:refs/heads/packaging'], ROOT)
        run(['git', 'push', remote, f'refs/tags/{tag}'], src)
        with tempfile.TemporaryDirectory() as temporary:
            notes = Path(temporary) / 'notes.txt'
            notes.write_text(message + '\nBoth packaged targets passed native media, Playwright, Wasm, '
                             'local WebRTC, and sustained VP9/H.264 Widevine no-presentation checks. '
                             'Sandbox mode and test scope are recorded in validation.json.\n')
            command = ['gh', 'release', 'create', tag, *assets, '--repo', repo,
                       '--verify-tag', '--title', f"Chromium {b['release']} ({ARCH[b['target_cpu']]})",
                       '--notes-file', notes]
            if draft:
                command.append('--draft')
            run(command, ROOT)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('build_record', type=Path)
    p.add_argument('--checkout', required=True, type=Path)
    p.add_argument('--remote', default='origin')
    p.add_argument('--repo', default='rillanetwork/chromium')
    p.add_argument('--dry-run', action='store_true')
    p.add_argument('--tag-only', action='store_true')
    p.add_argument('--draft', action='store_true')
    a = p.parse_args()
    publish(a.checkout.resolve(), a.build_record.resolve(), remote=a.remote, repo=a.repo,
            dry_run=a.dry_run, tag_only=a.tag_only, draft=a.draft)


if __name__ == '__main__':
    main_guard(main)
