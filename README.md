# Rillanetwork Chromium packaging

This is the **canonical patchset and release workflow**, on the `packaging`
branch of the Chromium fork. Chromium release branches are generated from these
inputs. They contain upstream Chromium plus the runtime patches; tooling and
historical build notes are not replayed into every new release.

The current manifest is `releases/158.0.8127.18-simulated.2.json`. It captures the
runtime implementation through `d3ce0aed26`, including sustained Widevine
playback with no video presentation. Revision 2 is a candidate: exporting a
manifest does not assert that new release artifacts have been built or tested.
Previously built binaries and published revision 1 are unchanged.

## Layout and ownership

```text
patches/<version>-simulated.<revision>/   ordered mail patches and series
patches/groups.json                     runtime paths assigned to logical patches
releases/*.json                         exact upstream/tree/patch/config pins
configs/<version>-simulated.<revision>/  frozen release and test GN arguments
configs/*.gnargs                        templates for future releases
tools/{export,prepare,build,validate,publish}.py
tests/                                  release probes, unit tests, npm lockfile
docs/                                   behaviour and historical measurements
```

The three patches cover (1) simulated media, capabilities, stable telemetry IDs,
and null sinks; (2) WebCodecs integration; (3) headless Widevine provisioning.
Tests that depend on Chromium internals live inside their implementation patch.
The CDM decoder fix is part of the media patch, not an optional follow-up.

Edit generated source branches for development, then export the reviewed delta
back here. Patch files are the source of truth for releases. Do not maintain a
second independent implementation on a moving fork branch. Never modify a
published release manifest, its patches, or its configuration; create a new
revision. Check out the recorded packaging commit to reproduce an older release.

## Checkouts

Keep this small control tree outside the `gclient` source tree:

```sh
# From an existing Chromium Git checkout, once the packaging branch is available:
git worktree add ../packaging packaging
```

For parallel Chromium versions, use **separate complete gclient checkouts**,
each with its own `src`, dependencies, and `out`. A Chromium Git worktree alone
does not isolate dependencies updated by `gclient sync`.

All source-changing/build/validation commands take a nonblocking lock in the
gclient root's `.rillanetwork/checkout.lock`. The lock coordinates these tools;
manual Git, gclient, and compiler commands must respect the same ownership.
Never switch source or sync dependencies while another build uses that checkout.

Requirements: Linux, Python 3.12+, configured Git author/committer identity,
depot_tools on PATH, normal Chromium Linux build dependencies, Node 20+, npm,
and `gh` only for publication. Keep the tool checkout committed before building.

## Prepare and build

Run from this packaging worktree. Replace `/path/to/chromium/src` with a complete
checkout that is available for this release. These commands do not resume or
reuse the historical ARM64 build outputs.

```sh
python3 tools/prepare.py releases/158.0.8127.18-simulated.2.json \
  --checkout /path/to/chromium/src --dry-run

python3 tools/prepare.py releases/158.0.8127.18-simulated.2.json \
  --checkout /path/to/chromium/src

python3 tools/build.py releases/158.0.8127.18-simulated.2.json \
  --checkout /path/to/chromium/src --target-cpu x64 --jobs 6
```

Preparation verifies patch/configuration hashes, fetches and checks the exact
upstream tag SHA, creates `release/<version>-simulated.<revision>`, and applies
the entire ordered series with **one `git am --3way` operation**. It verifies
the resulting whole source tree against the manifest before recording success.
No patches are inferred from the nearest tag or from incidental branch history.

If application conflicts, resolve and stage the affected files, then run
`git am --continue` in `src`. Git retains the remaining queue. Continue until
the entire sequence finishes, then rerun `prepare.py` with `--resume` and the
same manifest. Skipping a patch, aborting, editing the manifest, changing the
branch, or ending with a different tree cannot produce a prepared release.
If a resolution changes the intended tree, export a new revision and verify it
from a fresh base. `--resume` does not bless arbitrary edits.

Builds always sync dependencies, install the target sysroot, refresh Chrome and
V8 PGO, and build `media_unittests`, the Chrome `.deb`, and `headless_shell`.
Output directories include version, fork revision, architecture, and source
tree identity. A restarted build invalidates previous validation records.

The headless `.tar.gz` contains GN's runtime dependency closure and licenses.
Widevine is **not bundled**; provision the appropriate CDM separately. Build
records include exact source/packaging commits, configuration, dependency
revisions, toolchain/PGO hashes, artifact hashes, and the unit-test binary hash.
They are provenance records, not a promise of byte-for-byte reproducible builds.

## Validate, tag, publish

Install pinned test dependencies and validate on a native target-architecture
Linux host. Cross-building is supported; emulated runs do not satisfy the
native release gate. For a moved build, preserve the source commit, unit output
tree, and `dist` records; use the recorded packaging commit.

```sh
npm ci --prefix tests --ignore-scripts

python3 tools/validate.py \
  /path/to/chromium/dist/158.0.8127.18-simulated.2/x64/build.json \
  --checkout /path/to/chromium/src \
  --widevine-cdm /absolute/path/to/WidevineCdm/4.10.3112.0

python3 tools/publish.py \
  /path/to/chromium/dist/158.0.8127.18-simulated.2/x64/build.json \
  --checkout /path/to/chromium/src --dry-run

# Optional: create the bound local tag for inspection without pushing anything.
python3 tools/publish.py \
  /path/to/chromium/dist/158.0.8127.18-simulated.2/x64/build.json \
  --checkout /path/to/chromium/src --tag-only

# Explicit publication; use --draft if appropriate.
python3 tools/publish.py \
  /path/to/chromium/dist/158.0.8127.18-simulated.2/x64/build.json \
  --checkout /path/to/chromium/src
```

Validation extracts both artifacts into fresh staging directories and runs:

- focused C++ media tests using the recorded test binary;
- version, MP4/WebM, seeking/EOS, WebCodecs, null-decoder selection, and zero
  presentation checks on both packaged browsers;
- Playwright DOM/CDP, context isolation, request interception, Wasm/worker, and
  local WebRTC data-channel checks, plus CPU/PSS comparison samples;
- **30 seconds each of VP9 and H.264 Widevine playback**, requiring usable keys,
  the CDM video decoder, null audio decoder, zero presentation callbacks, and
  unchanged video-to-canvas pixels; invalid CDM configuration tests also run.

The DRM tests use the public Shaka Angel One manifest/license service, so network
availability is required. A missing CDM or unavailable service fails validation;
it is not silently skipped. `--sandbox` requires sandbox-enabled DRM tests.
Default Playwright launch settings disable the sandbox, and the record states
which mode was tested. Generic P2P/Wasm probes do not validate the application
bundle or STUN/TURN. Performance samples are recorded, not used as noisy gates.

Publication requires every check, unmodified evidence and artifacts, and the
exact packaging revision. An annotated tag binds the source commit to hashes
of both build and validation records. Separate tags such as
`v158.0.8127.18-simulated.2-linux-amd64` and `...-linux-arm64` allow architectures
to finish independently without overwriting a release. The command pushes the
packaging branch and exact source tag, then uploads both artifacts, checksums,
records, and the hashed test-evidence archive. It refuses an existing mismatched
tag or GitHub release.

There are no `--skip-tests`/`--reuse-branch` shortcuts to a publishable release.
Build-only output is simply pending validation. ARM64 remains unvalidated until
native execution, including a provisioned ARM64 CDM, passes all checks.

## Port to another Chromium release

1. Choose an exact Chromium release supported by the intended Playwright
   version. Update `tests/package.json` and its lockfile deliberately.
2. In an available complete gclient checkout, fetch the new upstream tag and
   create a temporary port branch from it. Apply the previous `series` in order
   with one `git am --3way` command. Resolve API changes and keep the media
   behaviour, telemetry IDs, and no-presentation requirements intact.
3. Commit compatibility fixes on that development branch, then export:

   ```sh
   python3 tools/export.py --checkout /path/to/chromium/src \
     --source-ref <reviewed-port-commit> --version <exact-upstream-version> \
     --revision 1
   ```

4. Review the resulting patches, manifest, and configuration snapshots. Unknown
   changed paths cause export to fail; add intentional integration paths to
   `patches/groups.json`. `--allow-legacy-metadata` was used only for the initial
   migration to omit the old README/docs/tools; normal ports do not need it.
5. Commit packaging inputs, prepare the new manifest from its clean upstream
   base, build, and validate. This fresh replay proves the exported patchset is
   complete. Keep the previous source tags and packaging history intact.

No mechanism can guarantee conflict-free application across arbitrary upstream
API changes. This workflow makes failures explicit and verifies that a resolved
port can be applied again without relying on the development checkout.

## Tooling checks

```sh
python3 -m unittest discover -s tests -p 'test_*.py'
# Also reconstruct the real current Chromium tree using a temporary Git index:
RILLA_CHROMIUM_SRC=/path/to/chromium/src \
  python3 -m unittest discover -s tests -p 'test_*.py'
```

Tests exercise real Git conflicts, continuation, skipped patches, interrupted
preparation, input tampering, checkout exclusion, architecture checks, stale
validation, artifact changes, tag mismatches, and safe archive extraction.
They do not compile Chromium. Full builds remain local and explicit.
