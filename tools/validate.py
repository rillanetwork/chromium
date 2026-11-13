#!/usr/bin/env python3
"""Run mandatory native-target checks on extracted, hash-verified artifacts."""
import argparse
import json
import os
from pathlib import Path
import platform
import subprocess
import tarfile
import tempfile
import xml.etree.ElementTree as ET

from build import check_deb, check_elf
from common import (ROOT, REQUIRED_CHECKS, TEST_FILTER, ReleaseError, checkout_lock,
                    git, inside, load_manifest, main_guard, packaging_revision,
                    release_id, run, sha256, verify_source, write_json)


def host_cpu():
    return {'x86_64': 'x64', 'amd64': 'x64', 'aarch64': 'arm64'}.get(platform.machine().lower())


def verify_unit_results(path):
    suites = ET.parse(path).getroot().findall('testsuite')
    required = {name.removesuffix('.*') for name in TEST_FILTER.split(':')}
    completed = set()
    for suite in suites:
        if suite.findall('.//failure') or suite.findall('.//error'):
            raise ReleaseError('media unit tests reported a failure')
        # Chromium's launcher omits gtest's optional result="completed" field.
        if any(case.get('status') == 'run' and case.get('result') in (None, 'completed')
               and case.find('skipped') is None
               for case in suite.findall('testcase')):
            completed.add(suite.get('name'))
    if not required <= completed:
        raise ReleaseError('required media test suites did not run: ' + ', '.join(sorted(required - completed)))


def verify_build(record_path, src):
    b = json.loads(record_path.read_text())
    if b['schema'] != 1:
        raise ReleaseError('unsupported build record')
    manifest = inside(ROOT, b['manifest'])
    m = load_manifest(manifest)
    if b['manifest_sha256'] != sha256(manifest) or b['release'] != release_id(m):
        raise ReleaseError('build record does not match release manifest')
    if b['source_tree'] != m['expected_tree'] or b['upstream_commit'] != m['upstream_commit']:
        raise ReleaseError('build source differs from pinned patchset')
    if b['packaging_commit'] != packaging_revision():
        raise ReleaseError('use the exact committed packaging revision that built this release')
    if git(src, 'rev-parse', b['source_commit'] + '^{tree}') != m['expected_tree']:
        raise ReleaseError('recorded source commit has the wrong tree')
    git(src, 'merge-base', '--is-ancestor', m['upstream_commit'], b['source_commit'])
    if b['target_cpu'] not in m['architectures']:
        raise ReleaseError('build architecture not enabled')
    if sorted(a['kind'] for a in b['artifacts']) != sorted(m['deliverables']):
        raise ReleaseError('missing or duplicate deliverables')
    for artifact in b['artifacts']:
        if Path(artifact['file']).name != artifact['file']:
            raise ReleaseError('artifact name must be a basename')
        if sha256(record_path.parent / artifact['file']) != artifact['sha256']:
            raise ReleaseError(f"artifact checksum mismatch: {artifact['file']}")
    return b, m


def unpack_shell(archive_path, destination):
    with tarfile.open(archive_path) as archive:
        # No archive may write outside staging or introduce links/devices.
        for member in archive.getmembers():
            inside(destination, member.name)
            if not (member.isfile() or member.isdir()):
                raise ReleaseError(f'unsupported archive member: {member.name}')
        archive.extractall(destination, filter='data')
    return destination / 'headless/headless_shell'


def validate(src, record_path, cdm, *, sandbox=False):
    with checkout_lock(src):
        b, m = verify_build(record_path, src)
        build_digest = sha256(record_path)
        if host_cpu() != b['target_cpu']:
            raise ReleaseError('release validation must run natively on the target architecture')
        verify_source(src, m)
        if git(src, 'rev-parse', 'HEAD') != b['source_commit']:
            raise ReleaseError('validation checkout differs from built commit')
        unit = inside(src, b['unit_binary'])
        if sha256(unit) != b['unit_binary_sha256']:
            raise ReleaseError('unit binary changed since build')
        for package, key in [('playwright-core', 'playwright'), ('shaka-player', 'shaka')]:
            installed = json.loads((ROOT / 'tests/node_modules' / package / 'package.json').read_text())
            if installed['version'] != m[key]:
                raise ReleaseError(f'{package} version differs from release manifest; run npm ci')
        evidence = record_path.parent / 'evidence'
        evidence.mkdir(exist_ok=True)
        validation_path = record_path.parent / 'validation.json'
        validation_path.unlink(missing_ok=True)
        env = dict(os.environ, CHROMIUM_SRC=str(src), HEADLESS_WIDEVINE_CDM_DIR=str(cdm),
                   HEADLESS_TEST_SANDBOX='1' if sandbox else '0')
        cdm_libraries = list(cdm.glob('_platform_specific/*/libwidevinecdm.so'))
        if not cdm_libraries:
            raise ReleaseError('CDM library unavailable for provenance')
        cdm_inputs = {str(p.relative_to(cdm)): sha256(p)
                      for p in [cdm / 'manifest.json', *cdm_libraries]}
        checks = {}

        def check(name, command, extra=None):
            stdout = evidence / (name + '.json')
            stderr = evidence / (name + '.stderr')
            print(f'Checking {name}', flush=True)
            with stdout.open('w') as out, stderr.open('w') as err:
                subprocess.run(list(map(str, command)), cwd=src, env={**env, **(extra or {})},
                               stdout=out, stderr=err, check=True, timeout=600)
            checks[name] = {'status': 'passed', 'stdout': str(stdout.relative_to(record_path.parent)),
                            'sha256': sha256(stdout), 'stderr': str(stderr.relative_to(record_path.parent)),
                            'stderr_sha256': sha256(stderr)}

        with tempfile.TemporaryDirectory(prefix='rilla-artifacts-') as staging:
            staging = Path(staging)
            artifacts = {a['kind']: record_path.parent / a['file'] for a in b['artifacts']}
            check_deb(artifacts['chrome-deb'], m, b['target_cpu'])
            chrome_root = staging / 'chrome'
            run(['dpkg-deb', '-x', artifacts['chrome-deb'], chrome_root])
            candidates = list(chrome_root.glob('opt/*/*/chrome'))
            if len(candidates) != 1:
                raise ReleaseError('expected exactly one packaged Chrome executable')
            chrome = candidates[0]
            shell = unpack_shell(artifacts['headless-tar'], staging / 'shell')
            for binary in (chrome, shell, unit):
                check_elf(binary, b['target_cpu'])
            unit_results = evidence / 'media-unit.xml'
            unit_results.unlink(missing_ok=True)
            check('media-unit', [unit, '--gtest_filter=' + TEST_FILTER,
                                  '--gtest_output=xml:' + str(unit_results)])
            verify_unit_results(unit_results)
            for name, binary in [('chrome', chrome), ('shell', shell)]:
                check(name + '-media', ['node', ROOT / 'tests/smoke_release.cjs', binary])
            check('compatibility', ['node', ROOT / 'tests/compare_headless.cjs', chrome, shell,
                                     evidence / 'runtime-comparison.json'])
            for codec in ('vp9', 'avc1'):
                check('widevine-' + codec, ['node', ROOT / 'tests/smoke_headless_widevine.cjs', shell, cdm],
                      {'HEADLESS_TEST_CODEC': codec})
        # Check again after execution, so concurrent edits invalidate results.
        verify_build(record_path, src)
        verify_source(src, m)
        if sha256(record_path) != build_digest:
            raise ReleaseError('build record changed during validation')
        if any(sha256(cdm / name) != digest for name, digest in cdm_inputs.items()):
            raise ReleaseError('CDM changed during validation')
        evidence_archive = record_path.parent / 'evidence.tar.gz'
        with tarfile.open(evidence_archive, 'w:gz') as archive:
            archive.add(evidence, arcname='evidence')
        write_json(validation_path, {
            'schema': 1, 'status': 'passed', 'build_sha256': build_digest,
            'manifest_sha256': b['manifest_sha256'], 'source_commit': b['source_commit'],
            'target_cpu': b['target_cpu'], 'native': True, 'sandbox': sandbox,
            'checks': checks, 'cdm': cdm_inputs,
            'evidence_archive': evidence_archive.name, 'evidence_sha256': sha256(evidence_archive),
            'scope': 'Public test stream and generic Wasm/WebRTC probes; not application P2P load.'})
        print(validation_path)


def verify_validation(record_path, b):
    path = record_path.parent / 'validation.json'
    v = json.loads(path.read_text())
    if (v.get('schema') != 1 or v.get('status') != 'passed' or not v.get('native') or
            v['build_sha256'] != sha256(record_path) or
            v['source_commit'] != b['source_commit'] or v['target_cpu'] != b['target_cpu'] or
            v['manifest_sha256'] != b['manifest_sha256'] or set(v['checks']) != REQUIRED_CHECKS):
        raise ReleaseError('missing, stale, or incomplete native validation')
    for check in v['checks'].values():
        if check['status'] != 'passed' or sha256(inside(record_path.parent, check['stdout'])) != check['sha256']:
            raise ReleaseError('validation evidence changed or did not pass')
        if sha256(inside(record_path.parent, check['stderr'])) != check['stderr_sha256']:
            raise ReleaseError('validation stderr changed')
    if sha256(inside(record_path.parent, v['evidence_archive'])) != v['evidence_sha256']:
        raise ReleaseError('validation evidence archive changed')
    return path


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('build_record', type=Path)
    p.add_argument('--checkout', required=True, type=Path)
    p.add_argument('--widevine-cdm', required=True, type=Path)
    p.add_argument('--sandbox', action='store_true', help='require sandbox-enabled DRM checks')
    a = p.parse_args()
    validate(a.checkout.resolve(), a.build_record.resolve(), a.widevine_cdm.resolve(), sandbox=a.sandbox)


if __name__ == '__main__':
    main_guard(main)
