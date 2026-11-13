"""Release correctness tests using real Git conflict/recovery and artifact I/O."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import build
import common
import prepare
import publish
import validate


class WorkflowTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.control = self.root / 'packaging'
        self.control.mkdir()
        self.src = self.root / 'checkout/src'
        self.src.mkdir(parents=True)
        self.env = mock.patch.dict(os.environ, {
            'GIT_AUTHOR_NAME': 'Release Test', 'GIT_AUTHOR_EMAIL': 'test@example.invalid',
            'GIT_COMMITTER_NAME': 'Release Test', 'GIT_COMMITTER_EMAIL': 'test@example.invalid',
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.git('init', '-q')
        self.git('config', 'commit.gpgsign', 'false')
        (self.src / 'chrome').mkdir()
        (self.src / 'chrome/VERSION').write_text('MAJOR=1\nMINOR=0\nBUILD=0\nPATCH=0\n')
        (self.src / 'alpha').write_text('base\n')
        self.commit('initial')
        self.base = self.git('rev-parse', 'HEAD')
        for number, (name, content) in enumerate([('alpha', 'patched\n'), ('tail', 'must apply\n')], 1):
            (self.src / name).write_text(content)
            self.commit(f'patch {number}')
            (self.control / f'{number}.patch').write_text(self.git('format-patch', '--stdout', '-1', 'HEAD'))
        self.expected = self.git('rev-parse', 'HEAD^{tree}')
        self.git('switch', '--detach', self.base)
        self.git('tag', '1.0.0.0')
        self.git('remote', 'add', 'upstream', str(self.src))
        (self.control / 'series').write_text('1.patch\n2.patch\n')
        (self.control / 'args.gn').write_text('is_debug=false\n')
        self.manifest = self.control / 'release.json'
        self.data = {
            'schema': 1, 'chromium_version': '1.0.0.0', 'revision': 1,
            'upstream_tag': '1.0.0.0', 'upstream_commit': self.base,
            'expected_tree': self.expected, 'series': 'series',
            'patches': [{'path': f'{i}.patch', 'sha256': common.sha256(self.control / f'{i}.patch')}
                        for i in (1, 2)],
            'configs': {k: {'path': 'args.gn', 'sha256': common.sha256(self.control / 'args.gn')}
                        for k in ('test', 'release')},
            'architectures': ['x64', 'arm64'], 'deliverables': ['chrome-deb', 'headless-tar'],
            'required_checks': sorted(common.REQUIRED_CHECKS), 'playwright': '1.65.0', 'shaka': '5.3.4',
        }
        common.write_json(self.manifest, self.data)
        for module in (common, prepare, validate, build, publish):
            patcher = mock.patch.object(module, 'ROOT', self.control)
            patcher.start()
            self.addCleanup(patcher.stop)

    def git(self, *args):
        return subprocess.check_output(['git', *args], cwd=self.src, text=True,
                                       stderr=subprocess.DEVNULL).strip()

    def commit(self, subject):
        self.git('add', '.')
        self.git('commit', '-qm', subject)

    def conflict(self):
        (self.src / 'alpha').write_text('new upstream\n')
        self.commit('upstream changes same line')
        self.base = self.git('rev-parse', 'HEAD')
        self.git('tag', '-f', '1.0.0.0')
        self.data['upstream_commit'] = self.base
        common.write_json(self.manifest, self.data)
        with self.assertRaises(subprocess.CalledProcessError):
            prepare.prepare(self.src, self.manifest)

    def test_clean_application_and_resume(self):
        prepare.prepare(self.src, self.manifest)
        prepare.prepare(self.src, self.manifest, resume=True)
        self.assertEqual(self.git('rev-parse', 'HEAD^{tree}'), self.expected)
        self.assertEqual(common.verify_prepared(self.src, self.manifest, self.data)['status'], 'prepared')

    def test_conflict_continue_applies_the_entire_tail(self):
        self.conflict()
        with self.assertRaises(common.ReleaseError):
            prepare.prepare(self.src, self.manifest, resume=True)
        (self.src / 'alpha').write_text('patched\n')
        self.git('add', 'alpha')
        self.git('am', '--continue')
        self.assertEqual((self.src / 'tail').read_text(), 'must apply\n')
        prepare.prepare(self.src, self.manifest, resume=True)

    def test_skipped_patch_cannot_be_built(self):
        self.conflict()
        self.git('am', '--skip')
        with self.assertRaisesRegex(common.ReleaseError, 'complete pinned patch series'):
            prepare.prepare(self.src, self.manifest, resume=True)
        with self.assertRaisesRegex(common.ReleaseError, 'prepare must finish'):
            common.verify_prepared(self.src, self.manifest, self.data)

    def test_aborted_series_cannot_be_resumed_as_complete(self):
        self.conflict()
        self.git('am', '--abort')
        with self.assertRaises(common.ReleaseError):
            prepare.prepare(self.src, self.manifest, resume=True)

    def test_resume_rejects_changed_manifest(self):
        prepare.prepare(self.src, self.manifest)
        self.data['revision'] = 2
        common.write_json(self.manifest, self.data)
        with self.assertRaisesRegex(common.ReleaseError, 'resume inputs'):
            prepare.prepare(self.src, self.manifest, resume=True)

    def test_patch_tampering_is_rejected_before_checkout_changes(self):
        (self.control / '1.patch').write_text('tampered')
        with self.assertRaisesRegex(common.ReleaseError, 'checksum mismatch'):
            prepare.prepare(self.src, self.manifest)
        self.assertEqual(self.git('rev-parse', 'HEAD'), self.base)

    def test_series_reordering_is_rejected(self):
        (self.control / 'series').write_text('2.patch\n1.patch\n')
        with self.assertRaisesRegex(common.ReleaseError, 'series order'):
            common.load_manifest(self.manifest)

    def test_dry_run_does_not_switch_or_create_state(self):
        prepare.prepare(self.src, self.manifest, dry_run=True)
        self.assertEqual(self.git('rev-parse', 'HEAD'), self.base)
        self.assertFalse(common.state_path(self.src).exists())

    def test_checkout_lock_prevents_concurrent_operations(self):
        with common.checkout_lock(self.src):
            with self.assertRaisesRegex(common.ReleaseError, 'another release command'):
                with common.checkout_lock(self.src):
                    self.fail('second owner acquired lock')

    def test_versions_architectures_and_revisions_have_separate_outputs(self):
        first = set(build.output_dirs(self.src, self.data, 'x64'))
        self.assertTrue(first.isdisjoint(build.output_dirs(self.src, self.data, 'arm64')))
        self.data['revision'] = 2
        self.assertTrue(first.isdisjoint(build.output_dirs(self.src, self.data, 'x64')))

    def test_renamed_wrong_architecture_elf_is_rejected(self):
        binary = self.root / 'arm64-browser'
        binary.write_bytes(b'\x7fELF\x02\x01' + b'\0' * 12 + (62).to_bytes(2, 'little'))
        build.check_elf(binary, 'x64')
        with self.assertRaisesRegex(common.ReleaseError, 'wrong ELF'):
            build.check_elf(binary, 'arm64')

    def test_wrong_architecture_deb_is_rejected(self):
        with mock.patch.object(build, 'capture', side_effect=['amd64', '1.0.0.0-1']):
            with self.assertRaisesRegex(common.ReleaseError, 'package metadata mismatch'):
                build.check_deb(Path('renamed-arm64.deb'), self.data, 'arm64')

    def test_uncommitted_dependency_changes_cannot_be_attested(self):
        (self.src / 'alpha').write_text('unrecorded local change\n')
        with mock.patch.object(build, 'capture', return_value='src: https://example.invalid/src@sha'):
            with self.assertRaisesRegex(common.ReleaseError, 'dirty dependency checkout'):
                build.provenance(self.src)

    def test_nested_deps_revision_override_is_clean_but_local_edits_are_not(self):
        child = self.src / 'vendor'
        child.mkdir()
        def child_git(*args):
            return subprocess.check_output(['git', *args], cwd=child, text=True,
                                           stderr=subprocess.DEVNULL).strip()
        child_git('init', '-q')
        (child / 'code').write_text('original')
        child_git('add', 'code')
        child_git('-c', 'commit.gpgsign=false', 'commit', '-qm', 'original')
        old = child_git('rev-parse', 'HEAD')
        self.git('update-index', '--add', '--cacheinfo', f'160000,{old},vendor')
        self.git('commit', '-qm', 'parent pins original child')
        (child / 'code').write_text('DEPS-pinned update')
        child_git('commit', '-qam', 'new child')
        revisions = 'src: https://example.invalid/src@sha\nsrc/vendor: https://example.invalid/vendor@sha'
        with mock.patch.object(build, 'capture', return_value=revisions):
            self.assertEqual(build.dependency_snapshot(self.src), revisions)
            (child / 'code').write_text('uncommitted child edit')
            with self.assertRaisesRegex(common.ReleaseError, 'dirty dependency checkout: src/vendor'):
                build.dependency_snapshot(self.src)

    def record(self):
        prepare.prepare(self.src, self.manifest)
        record = self.root / 'dist/build.json'
        record.parent.mkdir()
        artifacts = []
        for name in self.data['deliverables']:
            path = record.parent / name
            path.write_text(name)
            artifacts.append({'kind': name, 'file': name, 'sha256': common.sha256(path)})
        b = {'schema': 1, 'manifest': 'release.json', 'manifest_sha256': common.sha256(self.manifest),
             'release': common.release_id(self.data), 'source_tree': self.expected,
             'upstream_commit': self.base, 'source_commit': self.git('rev-parse', 'HEAD'),
             'packaging_commit': 'control-sha', 'target_cpu': 'x64', 'artifacts': artifacts}
        common.write_json(record, b)
        return record, b

    def validation(self, record, b):
        log = record.parent / 'evidence.txt'
        log.write_text('passed\n')
        checks = {name: {'status': 'passed', 'stdout': log.name, 'sha256': common.sha256(log),
                         'stderr': log.name, 'stderr_sha256': common.sha256(log)}
                  for name in common.REQUIRED_CHECKS}
        path = record.parent / 'validation.json'
        archive = record.parent / 'evidence.tar.gz'
        archive.write_bytes(b'test archive')
        common.write_json(path, {'schema': 1, 'status': 'passed', 'native': True,
                                'build_sha256': common.sha256(record), 'source_commit': b['source_commit'],
                                'target_cpu': b['target_cpu'], 'manifest_sha256': b['manifest_sha256'],
                                'checks': checks, 'evidence_archive': archive.name,
                                'evidence_sha256': common.sha256(archive)})
        return path

    def test_modified_artifact_cannot_pass_build_verification(self):
        record, b = self.record()
        with mock.patch.object(validate, 'packaging_revision', return_value='control-sha'):
            validate.verify_build(record, self.src)
            (record.parent / 'headless-tar').write_text('older package')
            with self.assertRaisesRegex(common.ReleaseError, 'artifact checksum mismatch'):
                validate.verify_build(record, self.src)

    def test_validation_is_bound_to_build_record(self):
        record, b = self.record()
        self.validation(record, b)
        validate.verify_validation(record, b)
        b['source_commit'] = self.base
        common.write_json(record, b)
        with self.assertRaisesRegex(common.ReleaseError, 'stale'):
            validate.verify_validation(record, b)

    def test_missing_drm_check_blocks_release(self):
        record, b = self.record()
        path = self.validation(record, b)
        v = json.loads(path.read_text())
        del v['checks']['widevine-avc1']
        common.write_json(path, v)
        with self.assertRaisesRegex(common.ReleaseError, 'incomplete'):
            validate.verify_validation(record, b)

    def test_altered_evidence_blocks_release(self):
        record, b = self.record()
        self.validation(record, b)
        (record.parent / 'evidence.txt').write_text('different test result')
        with self.assertRaisesRegex(common.ReleaseError, 'evidence changed'):
            validate.verify_validation(record, b)

    def test_source_tag_cannot_be_reused_for_other_artifacts(self):
        record, b = self.record()
        validation = self.validation(record, b)
        tag = 'v1.0.0.0-simulated.1-linux-amd64'
        self.git('tag', '-a', tag, '-m', 'old build')
        with mock.patch.object(publish, 'verify_build', return_value=(b, self.data)):
            with self.assertRaisesRegex(common.ReleaseError, 'existing tag'):
                publish.publish(self.src, record, tag_only=True)

    def test_dry_run_does_not_tag_and_local_tag_binds_records(self):
        record, b = self.record()
        validation = self.validation(record, b)
        for artifact in b['artifacts']:
            (record.parent / (artifact['file'] + '.sha256')).write_text(
                f"{artifact['sha256']}  {artifact['file']}\n")
        with mock.patch.object(publish, 'verify_build', return_value=(b, self.data)):
            publish.publish(self.src, record, dry_run=True)
            self.assertEqual(self.git('tag', '--list', 'v*'), '')
            publish.publish(self.src, record, tag_only=True)
            tag = 'v1.0.0.0-simulated.1-linux-amd64'
            self.assertEqual(self.git('rev-parse', tag + '^{commit}'), b['source_commit'])
            message = self.git('for-each-ref', '--format=%(contents)', 'refs/tags/' + tag)
            self.assertIn(common.sha256(record), message)
            self.assertIn(common.sha256(validation), message)
            publish.publish(self.src, record, tag_only=True)

    def test_build_refuses_incomplete_series_before_running_commands(self):
        self.conflict()
        (self.src.parent / '.gclient').write_text('solutions=[]\n')
        with mock.patch.object(build, 'run') as command:
            with self.assertRaisesRegex(common.ReleaseError, 'prepare must finish'):
                build.build(self.src, self.manifest, 'x64', 1)
            command.assert_not_called()

    def test_archive_cannot_escape_staging(self):
        archive = self.root / 'bad.tar'
        with tarfile.open(archive, 'w') as tar:
            entry = tarfile.TarInfo('../escape')
            entry.size = 1
            tar.addfile(entry, io.BytesIO(b'x'))
        with self.assertRaisesRegex(common.ReleaseError, 'escapes root'):
            validate.unpack_shell(archive, self.root / 'staging')

    def test_archive_cannot_create_symlinks(self):
        archive = self.root / 'bad.tar'
        with tarfile.open(archive, 'w') as tar:
            entry = tarfile.TarInfo('headless/link')
            entry.type = tarfile.SYMTYPE
            entry.linkname = '/tmp'
            tar.addfile(entry)
        with self.assertRaisesRegex(common.ReleaseError, 'unsupported archive member'):
            validate.unpack_shell(archive, self.root / 'staging')

    def test_empty_gtest_filter_success_is_not_validation(self):
        results = self.root / 'unit.xml'
        results.write_text('<testsuites tests="0"/>')
        with self.assertRaisesRegex(common.ReleaseError, 'did not run'):
            validate.verify_unit_results(results)

    def test_all_required_media_suites_must_complete(self):
        results = self.root / 'unit.xml'
        suites = ''.join(f'<testsuite name="{name.removesuffix(".*")}">'
                         '<testcase status="run" result="completed"/></testsuite>'
                         for name in common.TEST_FILTER.split(':'))
        results.write_text('<testsuites>' + suites + '</testsuites>')
        validate.verify_unit_results(results)
        results.write_text(results.read_text().replace(' result="completed"', ''))
        validate.verify_unit_results(results)
        results.write_text('<testsuites>' + suites + '</testsuites>')
        results.write_text(results.read_text().replace('result="completed"', 'result="skipped"'))
        with self.assertRaisesRegex(common.ReleaseError, 'did not run'):
            validate.verify_unit_results(results)


@unittest.skipUnless(os.environ.get('RILLA_CHROMIUM_SRC'), 'set RILLA_CHROMIUM_SRC for real Chromium replay')
class ChromiumReplayTest(unittest.TestCase):
    def test_real_series_reconstructs_every_runtime_change(self):
        root = Path(__file__).resolve().parents[1]
        src = Path(os.environ['RILLA_CHROMIUM_SRC'])
        manifest = root / 'releases/158.0.8127.18-simulated.2.json'
        m = common.load_manifest(manifest)
        with tempfile.TemporaryDirectory() as temporary:
            env = dict(os.environ, GIT_INDEX_FILE=str(Path(temporary) / 'index'))
            subprocess.run(['git', 'read-tree', m['upstream_commit']], cwd=src, env=env, check=True)
            for patch in m['patches']:
                subprocess.run(['git', 'apply', '--cached', root / patch['path']], cwd=src, env=env, check=True)
            tree = subprocess.check_output(['git', 'write-tree'], cwd=src, env=env, text=True).strip()
            self.assertEqual(tree, m['expected_tree'])
            groups = json.loads((root / 'patches/groups.json').read_text())
            paths = [p for g in groups for p in g['paths']]
            self.assertEqual(common.git(src, 'diff', '--stat', tree, m['exported_from'], '--', *paths), '')


if __name__ == '__main__':
    unittest.main()
