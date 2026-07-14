# Simulated video decode

This fork adds an opt-in Chromium build mode that exercises the normal media
pipeline without decoding compressed video pictures. It is intended for media
workloads where demuxing, buffering, timing, seeking, end-of-stream behavior,
audio, and browser APIs must keep working while video decode cost is removed.

Set the GN argument `enable_simulated_video_decode=true` to enable it. The
argument defaults to `false`; upstream behavior is unchanged in normal builds.

## What the mode does

In a simulated build, `DefaultDecoderFactory` exposes only
`NullVideoDecoder` for video. Each non-EOS compressed video buffer produces a
black I420 `VideoFrame` with:

- the configured coded size, visible rectangle, and natural size;
- the input presentation timestamp;
- the configured color space and HDR metadata.

The decoder reuses its black backing frame and creates lightweight wrappers for
individual timestamps. Audio decoder selection is unchanged. Demuxing,
buffering, DRM decrypt-only operation, renderer scheduling, and compositing
still occur.

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
gn gen out/Simulated --args='is_debug=true is_component_build=true symbol_level=0 proprietary_codecs=true ffmpeg_branding="Chrome" enable_simulated_video_decode=true'
autoninja -C out/Simulated chrome media_unittests
```

Run the focused tests with:

```sh
out/Simulated/media_unittests \
  --gtest_filter='NullVideoDecoderTest.*:DefaultDecoderFactoryTest.*:SupportedTypesTest.*'
```

The filter intentionally includes the broader `SupportedTypesTest` suite so a
simulated-build capability change cannot silently broaden normal codec or color
space reporting.

## Optimized Debian package

The release configuration uses proprietary-codec parsing, Chrome's FFmpeg
branding, the Linux installer, and the simulated decoder. It remains Chromium
branded unless other branding arguments are supplied.

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
gn gen out/SimulatedDeb --args='is_debug=false is_official_build=true is_component_build=false symbol_level=0 blink_symbol_level=0 proprietary_codecs=true ffmpeg_branding="Chrome" enable_simulated_video_decode=true enable_linux_installer=true'
autoninja -C out/SimulatedDeb chrome/installer/linux:unstable_deb
```

The package is written beneath `out/SimulatedDeb`. Find the exact versioned
filename with:

```sh
find out/SimulatedDeb -maxdepth 1 -name '*.deb' -print
```

Disabling PGO with `chrome_pgo_phase=0` is possible, but it produces a less
representative performance build and is not the recommended release process.

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

- the GN argument and generated media build flag;
- `NullVideoDecoder` and its unit tests;
- `DefaultDecoderFactory`, which controls normal renderer and WebCodecs decoder
  construction;
- capability reporting and WebCodecs hardware-preference handling;
- the persisted decoder-type histogram enum.

Keeping demuxer, renderer, compositor, and platform decoder implementations out
of the patch reduces the number of release-specific conflicts.

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
gn gen out/Simulated-m<major> --args='is_debug=true is_component_build=true symbol_level=0 proprietary_codecs=true ffmpeg_branding="Chrome" enable_simulated_video_decode=true'
autoninja -C out/Simulated-m<major> media_unittests

out/Simulated-m<major>/media_unittests \
  --gtest_filter='NullVideoDecoderTest.*:DefaultDecoderFactoryTest.*:SupportedTypesTest.*'
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

gn gen out/SimulatedDeb-m<major> --args='is_debug=false is_official_build=true is_component_build=false symbol_level=0 blink_symbol_level=0 proprietary_codecs=true ffmpeg_branding="Chrome" enable_simulated_video_decode=true enable_linux_installer=true'
autoninja -C out/SimulatedDeb-m<major> chrome/installer/linux:unstable_deb
```

### Release validation checklist

Before treating the port as releasable:

1. Confirm `chrome/VERSION` matches the intended stable tag.
2. Confirm the focused 40-test filter passes.
3. Build the official package with revision-matched Chrome and V8 PGO data.
4. Launch the exact packaged binary, not an older browser on `PATH`.
5. Verify clear MP4 playback, seeking, EOS, audio, and more than one coded and
   natural video size.
6. Verify WebCodecs and `<video>` capability reporting for required codecs.
7. Open DevTools during a direct-media test. Treat page-specific synchronous
   layout or JavaScript loops separately from decoder/compositor failures.
8. Confirm encrypted content separately if decrypt-only DRM is required.
9. Run a normal build with `enable_simulated_video_decode=false` when upstream
   media APIs changed materially.
10. Record the stable tag, patch commit, package filename, and checksum in the
    release notes.

Keep release compatibility fixes in the new major-version branch. If a fix is
not inherently version-specific, fold it back into the logical patch used for
the next cherry-pick so the patchset does not grow into a chain of historical
workarounds.

## Current release branches

- `simulated-video-decode-m150` is based on Chromium `150.0.7871.114`.
- `simulated-video-decode-m152` preserves the original tip-of-tree development
  implementation for comparison; it is not the release base.

The M150 focused media test filter passes all 40 selected tests.
