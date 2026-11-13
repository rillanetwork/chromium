# Simulated media decode

This fork adds an opt-in Chromium build mode that exercises the normal media
pipeline without decoding compressed audio or video. It is intended for media
workloads where demuxing, buffering, timing, seeking, end-of-stream behavior,
and browser APIs must keep working while decoded pixels and samples are not
consumed.

The two independent GN arguments are:

- `enable_simulated_video_decode=true` for null video decode and presentation;
- `enable_simulated_audio_decode=true` for null audio decode and output.

Both arguments default to `false`; upstream behavior is unchanged in normal
builds. Enable both for the lowest media-playback CPU footprint, or only video
when audible audio is required.

## What the mode does

With simulated video enabled, `DefaultDecoderFactory` exposes only
`NullVideoDecoder` for video. Each non-EOS compressed video buffer produces a
black I420 `VideoFrame` with:

- the configured coded size, visible rectangle, and natural size;
- the input presentation timestamp;
- the configured color space and HDR metadata.

The decoder reuses its black backing frame and creates lightweight wrappers for
individual timestamps. `NullVideoSink` consumes those wrappers on the normal
playback cadence before they reach Blink or Viz. Renderer timing, buffering,
and end-of-stream behavior therefore remain intact without raster or
compositor work for the video.

With simulated audio enabled, `DefaultDecoderFactory` exposes only
`NullAudioDecoder`. It ignores compressed payload bytes and emits
`AudioBuffer::CreateEmptyBuffer()` results: the buffers preserve timestamps,
duration, channel layout, sample rate, and discard padding but allocate no PCM.
Unknown packet duration is inferred from the following timestamp. A
`NullAudioSink` drives Chromium's normal audio clock without opening an output
device or sending samples to the audio service. Output is always silent.

Demuxing and buffering still occur for both streams. Container parsing is
unaffected when audio and video share one file because demuxing separates them
before decoder selection. DRM decrypt-only operation remains available where
Chromium can supply clear buffers to the null decoders.

The initial implementation covers the normal renderer media pipeline and
WebCodecs. WebRTC's separate decoder factories and secure DRM modes that require
decrypt-and-decode are outside its scope.

## Checkout preparation

Chromium is a `gclient` checkout, not a standalone Git repository. Keep the
fork as `origin` and Chromium's source repository as `upstream`:

```sh
git remote set-url origin https://github.com/rillanetwork/chromium.git
git remote add upstream https://chromium.googlesource.com/chromium/src.git
```

If `upstream` already exists, only verify it with `git remote -v`. Run dependency
sync from the directory containing `.gclient` whenever the Chromium revision
changes:

```sh
gclient sync
cd src
```

Rerun an interrupted `gclient sync` rather than manually repairing nested
dependency repositories. `gclient status` should complete successfully before
building.

## Development build

This configuration is useful for unit tests and debugging. `symbol_level=0`
keeps its first build and link time manageable; increase it only when symbols
are required.

```sh
gn gen out/Simulated --args='is_debug=true is_component_build=true symbol_level=0 proprietary_codecs=true ffmpeg_branding="Chrome" enable_widevine=true bundle_widevine_cdm=false enable_simulated_video_decode=true enable_simulated_audio_decode=true'
autoninja -C out/Simulated chrome media_unittests
```

Run the focused tests with:

```sh
out/Simulated/media_unittests \
  --gtest_filter='NullAudioDecoderTest.*:NullVideoDecoderTest.*:NullVideoSinkTest.*:DefaultDecoderFactoryTest.*:SupportedTypesTest.*'
```

The filter intentionally includes the broader `SupportedTypesTest` suite so a
simulated-build capability change cannot silently broaden normal codec or color
space reporting.

## Optimized Debian package

The release configuration uses proprietary-codec parsing, Chrome's FFmpeg
branding, the Linux installer, and the simulated decoder. It remains Chromium
branded unless other branding arguments are supplied.

Widevine EME is enabled, preserving the `150.0.7871.114-simulated.3` release
configuration. The proprietary CDM is not bundled or redistributed; Chromium's
component updater downloads it into the browser profile at runtime. First use
can require a network connection and browser restart. The automated development
and release configurations both enable this support.

### 1. Fetch revision-matched optimization profiles

Chromium's default `.gclient` configuration does not fetch PGO data. An official
build needs two independent profiles:

```sh
python3 tools/update_pgo_profiles.py \
  --target=linux update \
  --gs-url-base=chromium-optimization-profiles/pgo_profiles

python3 v8/tools/builtins-pgo/download_profiles.py download \
  --depot-tools third_party/depot_tools \
  --check-v8-revision --quiet
```

Run both commands after every Chromium version change. A missing Chrome profile
causes GN generation to fail. A stale V8 builtins profile can make
`mksnapshot` terminate with `SIGTRAP` because official builds validate profile
data.

Alternatively, set `checkout_pgo_profiles: True` in the solution's `.gclient`
`custom_vars` and run `gclient runhooks`. The explicit commands above are useful
for a narrowly configured local checkout.

### 2. Generate and build

```sh
gn gen out/SimulatedDeb --args='is_debug=false is_official_build=true is_component_build=false symbol_level=0 blink_symbol_level=0 proprietary_codecs=true ffmpeg_branding="Chrome" enable_widevine=true bundle_widevine_cdm=false enable_simulated_video_decode=true enable_simulated_audio_decode=true enable_linux_installer=true'
autoninja -C out/SimulatedDeb chrome/installer/linux:unstable_deb
```

The package is written beneath `out/SimulatedDeb`. Find the exact versioned
filename with:

```sh
find out/SimulatedDeb -maxdepth 1 -name '*.deb' -print
```

Disabling PGO with `chrome_pgo_phase=0` is possible, but it produces a less
representative performance build and is not the recommended release process.

## Automated release workflow

Two standard-library Python scripts turn the manual steps below into a local
release workflow. They deliberately do not hide Git conflicts or publish from
CI.

### Prepare a release

Run the preparation script while checked out on the previous known-good fork
branch:

```sh
python3 tools/rillanetwork/prepare_release.py 151.0.0.0 --dry-run
python3 tools/rillanetwork/prepare_release.py <new-stable-version>
```

`prepare_release.py`:

1. finds the nearest numeric Chromium tag behind the source ref;
2. treats every commit above that tag as the ordered fork patch stack;
3. fetches the requested stable tag and creates
   `simulated-video-decode-m<major>` from it;
4. cherry-picks the patch stack;
5. runs `gclient sync`, focused tests, both PGO downloads, and the official
   Debian package build in exact-version output directories;
6. writes a SHA-256 sidecar and creates the annotated local tag
   `v<chromium-version>-simulated.1`.

Use `--source-ref` or `--source-base` when preparing from something other than
the current branch. If a cherry-pick conflicts, the script stops without
discarding work. Resolve it, run `git cherry-pick --continue`, commit any
release-specific fix, and restart the remaining workflow with:

```sh
python3 tools/rillanetwork/prepare_release.py <new-stable-version> \
  --reuse-branch
```

The script accepts `--skip-sync`, `--skip-tests`, `--skip-pgo-download`,
`--skip-package`, and `--skip-tag` for recovery or advanced use. Supplying
`--artifact` with `--skip-package` lets it checksum and tag an already completed
package. It refuses to switch branches or tag while tracked changes are
present; unrelated untracked files are left alone.

### Publish a GitHub release

After inspecting the package and local tag:

```sh
python3 tools/rillanetwork/publish_release.py \
  out/SimulatedDeb-<version>/<package>.deb \
  --tag v<version>-simulated.1 \
  --push-branch \
  --dry-run

python3 tools/rillanetwork/publish_release.py \
  out/SimulatedDeb-<version>/<package>.deb \
  --tag v<version>-simulated.1 \
  --push-branch
```

`publish_release.py` verifies that the annotated tag exists, reads the Chromium
version at that tag, checks that it matches the artifact name, extracts Debian
package metadata, recomputes SHA-256, and generates release notes from the fork
patch stack. It then authenticates `gh`, refuses to overwrite an existing
release, optionally pushes the current branch, pushes the exact tag, and
uploads both the `.deb` and checksum file.

Use `--draft` for a GitHub draft release or `--prerelease` for a visible
prerelease. The default repository is `rillanetwork/chromium`; override it with
`--repo` when testing against another fork.

## Maintaining the patch across Chromium releases

### Branch model

Keep Chromium's release tag as the branch base and the simulated-video change
as a small logical patch on top:

```text
Chromium stable tag
  + simulated video decode patch
  + release-specific compatibility fixes, if any
```

Use a branch name containing the Chromium major version, for example
`simulated-video-decode-m150`. Keep an old release branch as a known-good safety
point. Do not merge Chromium's moving `main` into these branches; create the new
branch from the next stable tag and replay the patch.

The patch is deliberately concentrated at stable media seams:

- the GN arguments and generated media build flags;
- `NullVideoDecoder`, `NullAudioDecoder`, and their unit tests;
- `DefaultDecoderFactory`, which controls normal renderer and WebCodecs decoder
  construction;
- `RendererImplFactory`, which substitutes `NullVideoSink` and
  `NullAudioSink`;
- capability reporting and WebCodecs hardware-preference handling;
- the persisted audio and video decoder-type histogram enums.

Keeping demuxer, compositor, and platform decoder implementations out of the
patch reduces the number of release-specific conflicts.

### Porting procedure

The following example ports the patch from one release branch to a new stable
tag. Replace the placeholders before running it.

```sh
git fetch upstream tag <new-stable-version>
git switch -c simulated-video-decode-m<major> <new-stable-version>
git cherry-pick <previous-simulated-video-patch-commit>

cd ..
gclient sync
cd src
```

If the cherry-pick conflicts, resolve only against the new release's APIs and
keep the behavior described in this document. The most likely conflict points
are:

- `media/renderers/default_decoder_factory.cc` when decoder construction order
  changes;
- `media/base/supported_types.cc` when codec capability rules change;
- Blink WebCodecs `video_decoder.cc` when hardware preference handling changes;
- `media/BUILD.gn`, `media/filters/BUILD.gn`, or renderer test targets;
- `tools/metrics/histograms/enums.xml`; the existing persisted numeric value
  must never be renumbered or reused.

After syncing, use new output directories. Reusing an output tree from a newer
or older release can leave generated headers whose timestamps look current but
whose declarations do not match the source checkout.

```sh
gn gen out/Simulated-m<major> --args='is_debug=true is_component_build=true symbol_level=0 proprietary_codecs=true ffmpeg_branding="Chrome" enable_widevine=true bundle_widevine_cdm=false enable_simulated_video_decode=true enable_simulated_audio_decode=true'
autoninja -C out/Simulated-m<major> media_unittests

out/Simulated-m<major>/media_unittests \
  --gtest_filter='NullAudioDecoderTest.*:NullVideoDecoderTest.*:NullVideoSinkTest.*:DefaultDecoderFactoryTest.*:SupportedTypesTest.*'
```

If an old directory must be reused, run `gn clean <output-directory>` before
regenerating. A clean build is slower but avoids subtle cross-version failures.

Then refresh both PGO profiles and build the package in another new output
directory:

```sh
python3 tools/update_pgo_profiles.py \
  --target=linux update \
  --gs-url-base=chromium-optimization-profiles/pgo_profiles
python3 v8/tools/builtins-pgo/download_profiles.py download \
  --depot-tools third_party/depot_tools \
  --check-v8-revision --quiet

gn gen out/SimulatedDeb-m<major> --args='is_debug=false is_official_build=true is_component_build=false symbol_level=0 blink_symbol_level=0 proprietary_codecs=true ffmpeg_branding="Chrome" enable_widevine=true bundle_widevine_cdm=false enable_simulated_video_decode=true enable_simulated_audio_decode=true enable_linux_installer=true'
autoninja -C out/SimulatedDeb-m<major> chrome/installer/linux:unstable_deb
```

### Release validation checklist

Before treating the port as releasable:

1. Confirm `chrome/VERSION` matches the intended stable tag.
2. Confirm the focused null audio/video and capability filter passes.
3. Build the official package with revision-matched Chrome and V8 PGO data.
4. Launch the exact packaged binary, not an older browser on `PATH`.
5. Verify clear MP4 and WebM playback, seeking, EOS, silence, and more than one
   coded and natural video size. Run a video-only build separately when audible
   audio is required.
6. Verify WebCodecs and `<video>` capability reporting for required codecs.
7. Open DevTools during a direct-media test. Treat page-specific synchronous
   layout or JavaScript loops separately from decoder/compositor failures.
8. Confirm encrypted content separately if decrypt-only DRM is required.
9. Run a normal build with both simulated flags disabled when upstream media
   APIs changed materially.
10. Record the stable tag, patch commit, package filename, and checksum in the
    release notes.

Keep release compatibility fixes in the new major-version branch. If a fix is
not inherently version-specific, fold it back into the logical patch used for
the next cherry-pick so the patchset does not grow into a chain of historical
workarounds.

## Initial validated release

- `simulated-video-decode-m150` is based on Chromium `150.0.7871.114`.
- `simulated-video-decode-m152` preserves the original tip-of-tree development
  implementation for comparison; it is not the release base.

The M150 combined focused media filter passes all 51 selected tests.

## Playwright 1.65 update

The local `simulated-video-decode-m158` port is based on Chromium
`158.0.8127.18`, the exact browser version in Playwright 1.65.0's
[`browsers.json`](https://github.com/microsoft/playwright/blob/v1.65.0/packages/playwright-core/browsers.json).
It replays the five M150 fork commits; it does not use the fork's moving `main`.

M158 adds upstream IAMF decoder tests and assigns audio decoder enum value 12
to IAMF. That value is already persisted by this fork's released null audio
decoder. The port preserves null audio at 12 and assigns the newly introduced
IAMF decoder value 13, with matching histogram entries. These fork telemetry
IDs differ from stock Chromium; previously shipped fork IDs remain stable.
The decoder-factory tests retain the upstream IAMF coverage for normal audio
builds and check the null decoder in simulated audio builds. The test source
is included once when either IAMF or simulated decoding is enabled.

Build and validate this branch using the existing release workflow:

```sh
python3 tools/rillanetwork/prepare_release.py 158.0.8127.18 \
  --reuse-branch --source-ref 328e0eef6502d4257c947dfbbd66ced8acc9462e \
  --source-base 150.0.7871.114
```

The source ref identifies the previous released patch stack for the release
report; `--reuse-branch` builds the already ported M158 branch. Publish the
resulting `.deb` and SHA-256 sidecar to GitHub Releases with the existing
`publish_release.py` workflow above.

To exercise the packaged executable through Playwright 1.65.0, install the test
dependency outside the checkout and pass the executable from the extracted or
installed Debian package:

```sh
npm install --prefix /tmp/chromium-release-smoke --ignore-scripts playwright-core@1.65.0
NODE_PATH=/tmp/chromium-release-smoke/node_modules \
  node tools/rillanetwork/smoke_release.cjs <packaged-chromium-executable>
```

The smoke test checks source, Playwright, and running-browser versions for exact
agreement. It also checks MP4, WebM, and WebCodecs capability reporting and
plays both media fixtures through a seek to end-of-stream. A mismatched future
Playwright update fails the version check before browser launch.

### M158 validation

Validated on 2026-11-13:

- All 52 focused media tests pass, including the persisted null-audio ID check.
- The official PGO Debian package builds with both simulated modes and Widevine
  enabled; the proprietary CDM is not bundled.
- Playwright 1.65.0 launches the extracted package and reports the exact expected
  browser version, `158.0.8127.18`.
- MP4 and WebM capability reporting, playback, seeking, dimensions, and EOS pass.
- WebCodecs reports VP8 support with a hardware preference in the simulated build.
- With component-updated Widevine 4.10.3112.0, EME access, MediaKeys creation,
  and the public Shaka encrypted DASH sample pass. Playback advances beyond two
  seconds with usable keys, 54 video frames, and zero dropped frames.
- The same version guard rejects the previous M150 package as expected.

The packaged binary's Build ID is
`3d533b084ea53015a8c4e8f38567202d68355589`.
