#!/usr/bin/env python3
"""Build both deliverables and record their exact inputs; validation is separate."""
import argparse
from pathlib import Path
import shutil
import tarfile

from common import (ARCH, ROOT, ReleaseError, capture, checkout_lock, git, inside,
                    load_manifest, main_guard, packaging_revision, release_id, run,
                    sha256, verify_prepared, write_json)


def output_dirs(src, m, cpu):
    root = src / 'out' / f"Rilla-{release_id(m)}-{cpu}-{m['expected_tree'][:12]}"
    return root / 'test', root / 'release'


def check_deb(artifact, m, cpu):
    arch = capture(['dpkg-deb', '--field', artifact, 'Architecture'])
    version = capture(['dpkg-deb', '--field', artifact, 'Version'])
    if arch != ARCH[cpu] or version.split('-', 1)[0] != m['chromium_version']:
        raise ReleaseError(f'package metadata mismatch: {arch}, {version}')


def check_elf(binary, cpu):
    header = Path(binary).open('rb')
    with header:
        data = header.read(20)
    expected = {'x64': 62, 'arm64': 183}[cpu]
    if len(data) < 20 or data[:4] != b'\x7fELF' or data[4:6] != b'\x02\x01' or int.from_bytes(data[18:20], 'little') != expected:
        raise ReleaseError(f'wrong ELF architecture: {binary}')


def pack_headless(src, output, artifact, cpu):
    deps = capture(['gn', 'desc', output, '//headless:headless_shell', 'runtime_deps'], src)
    paths = sorted({str(Path(p)) for p in deps.splitlines() if p.strip()})
    if 'headless_shell' not in paths:
        raise ReleaseError('GN runtime dependencies omit headless_shell')
    check_elf(output / 'headless_shell', cpu)
    # Package GN's whole runtime closure, preserving its relative paths. Do not
    # guess which .pak/.so/snapshot files a future Chromium needs.
    paths += ['LICENSE.headless_shell']
    with tarfile.open(artifact, 'w:gz', compresslevel=1, dereference=True) as archive:
        for name in sorted(set(paths)):
            path = inside(output, name)
            if not path.exists():
                raise ReleaseError(f'missing headless runtime dependency: {path}')
            archive.add(path, arcname='headless/' + name)
        archive.add(src / 'LICENSE', arcname='headless/LICENSE.chromium')
    return paths


def provenance(src):
    dependencies = capture(['gclient', 'revinfo', '--actual'], src.parent)
    for line in dependencies.splitlines():
        relative, separator, _ = line.partition(': ')
        if not separator:
            continue
        dependency = inside(src.parent, relative)
        if (dependency / '.git').exists() and git(dependency, 'status', '--porcelain', '--untracked-files=no'):
            raise ReleaseError(f'dirty dependency checkout: {relative}')
    profile_name = (src / 'chrome/build/linux.pgo.txt').read_text().strip()
    paths = [src / 'chrome/build/pgo_profiles' / profile_name,
             src / 'third_party/llvm-build/Release+Asserts/cr_build_revision',
             src / 'v8/tools/builtins-pgo/profiles/meta.json']
    paths += sorted((src / 'v8/tools/builtins-pgo/profiles').glob('*.profile'))
    return {
        'clang': capture([src / 'third_party/llvm-build/Release+Asserts/bin/clang', '--version']),
        'files': {str(p.relative_to(src)): sha256(p) for p in paths},
        'dependencies': dependencies,
        'gclient_config_sha256': sha256(src.parent / '.gclient'),
        'depot_tools_commit': capture(['git', '-C', Path(shutil.which('gclient')).resolve().parent,
                                       'rev-parse', 'HEAD']),
    }


def build(src, manifest, cpu, jobs, *, dry_run=False):
    m = load_manifest(manifest)
    if cpu not in m['architectures']:
        raise ReleaseError('architecture not enabled in manifest')
    test_out, release_out = output_dirs(src, m, cpu)
    if dry_run:
        print(f'Prepare must match {sha256(manifest)}; target Linux {cpu}')
        print(f'Test output: {test_out}\nRelease output: {release_out}')
        print('Sync dependencies; download matching sysroot/PGO; build media_unittests, '
              'chrome/installer/linux:unstable_deb and headless_shell; package and record hashes.')
        return
    if not (src.parent / '.gclient').is_file():
        raise ReleaseError('build requires a complete gclient checkout')
    with checkout_lock(src):
        state = verify_prepared(src, manifest, m)
        packaging_commit = packaging_revision()
        dist = src.parent / 'dist' / release_id(m) / cpu
        dist.mkdir(parents=True, exist_ok=True)
        # A failed/restarted build must never retain a publishable old record.
        for name in ('build.json', 'validation.json'):
            (dist / name).unlink(missing_ok=True)
        run(['gclient', 'sync'], src.parent)
        verify_prepared(src, manifest, m)
        run(['python3', 'build/linux/sysroot_scripts/install-sysroot.py', f'--arch={ARCH[cpu]}'], src)
        run(['python3', 'tools/update_pgo_profiles.py', '--target=linux', 'update',
             '--gs-url-base=chromium-optimization-profiles/pgo_profiles'], src)
        run(['python3', 'v8/tools/builtins-pgo/download_profiles.py', 'download',
             '--depot-tools', 'third_party/depot_tools', '--check-v8-revision', '--quiet'], src)
        inputs = provenance(src)
        args = {}
        for kind, output in [('test', test_out), ('release', release_out)]:
            args[kind] = (ROOT / m['configs'][kind]['path']).read_text() + f'\ntarget_os = "linux"\ntarget_cpu = "{cpu}"\n'
            run(['gn', 'gen', output, '--args=' + args[kind]], src)
        run(['autoninja', '-C', test_out, '-j', str(jobs), 'media_unittests'], src)
        run(['autoninja', '-C', release_out, '-j', str(jobs),
             'chrome/installer/linux:unstable_deb', 'headless_shell'], src)
        candidates = list(release_out.glob('*.deb'))
        if len(candidates) != 1:
            raise ReleaseError(f'expected exactly one .deb, found {len(candidates)}')
        check_deb(candidates[0], m, cpu)
        check_elf(release_out / 'chrome', cpu)
        check_elf(test_out / 'media_unittests', cpu)
        deb = dist / f'chromium-{release_id(m)}-linux-{ARCH[cpu]}.deb'
        shutil.copy2(candidates[0], deb)
        shell = dist / f'headless-shell-{release_id(m)}-linux-{ARCH[cpu]}.tar.gz'
        dependencies = pack_headless(src, release_out, shell, cpu)
        verify_prepared(src, manifest, m)
        if packaging_revision() != packaging_commit:
            raise ReleaseError('packaging sources changed during build')
        if provenance(src) != inputs:
            raise ReleaseError('dependency/toolchain/PGO inputs changed during build')
        artifacts = [{'kind': kind, 'file': path.name, 'sha256': sha256(path)}
                     for kind, path in [('chrome-deb', deb), ('headless-tar', shell)]]
        for artifact in artifacts:
            (dist / (artifact['file'] + '.sha256')).write_text(
                f"{artifact['sha256']}  {artifact['file']}\n")
        write_json(dist / 'build.json', {
            'schema': 1, 'release': release_id(m), 'manifest': str(manifest.relative_to(ROOT)),
            'manifest_sha256': sha256(manifest), 'source_commit': state['source_commit'],
            'source_tree': m['expected_tree'], 'upstream_commit': m['upstream_commit'],
            'packaging_commit': packaging_commit, 'target_cpu': cpu, 'gn_args': args,
            'toolchain': inputs, 'artifacts': artifacts,
            'headless_runtime_dependencies': dependencies,
            'unit_binary': str((test_out / 'media_unittests').relative_to(src)),
            'unit_binary_sha256': sha256(test_out / 'media_unittests'),
            'validation': 'pending'})
        print(f'Built {dist}; run validate.py before tagging or publishing.')


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('manifest', type=Path)
    p.add_argument('--checkout', required=True, type=Path)
    p.add_argument('--target-cpu', choices=ARCH, default='x64')
    p.add_argument('--jobs', type=int, default=6)
    p.add_argument('--dry-run', action='store_true')
    a = p.parse_args()
    if a.jobs < 1:
        raise ReleaseError('--jobs must be positive')
    build(a.checkout.resolve(), a.manifest.resolve(), a.target_cpu, a.jobs, dry_run=a.dry_run)


if __name__ == '__main__':
    main_guard(main)
