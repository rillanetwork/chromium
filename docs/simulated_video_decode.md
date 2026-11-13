# Simulated media decode

This fork adds an opt-in Chromium build mode that exercises the normal media
pipeline while avoiding regular audio and video decoding. Protected video may
still require decoding inside its CDM, as described below. It is intended for media
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

With simulated video enabled, `DefaultDecoderFactory` retains
`DecryptingVideoDecoder` for protected video and uses `NullVideoDecoder` for
clear video. Each non-EOS buffer passed to the null decoder produces a
black I420 `VideoFrame` with:

- the configured coded size, visible rectangle, and natural size;
- the input presentation timestamp;
- the configured color space and HDR metadata.

The decoder reuses its black backing frame and creates lightweight wrappers for
individual timestamps. `NullVideoSink` consumes those wrappers on the normal
playback cadence before they reach Blink or Viz. Renderer timing, buffering,
and end-of-stream behavior therefore remain intact without raster or
compositor work for the video.

For CDMs that require decrypt-and-decode, protected video uses the CDM's actual
decoder. Its output still goes to `NullVideoSink` with no frame callback, so
decoded frames do not reach Blink/Viz for presentation. This preserves the
rendering savings but incurs CDM video decoding CPU and frame-memory costs.

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
WebCodecs. WebRTC's separate decoder factories and hardware-secure DRM paths
are outside its scope.

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

## Release workflow

The canonical workflow and patchset live on this `packaging` branch.
See [the release guide](../README.md) for pinned manifests, complete-series
application, packaging, validation, and publication. The old history-based
cherry-pick scripts are retired.

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

The historical M158 branch was ported from M150. New releases use the
[manifest-driven workflow](../README.md); the old `--reuse-branch` procedure
is no longer supported.

To exercise the packaged executable through Playwright 1.65.0, install the test
dependency outside the checkout and pass the executable from the extracted or
installed Debian package:

```sh
npm install --prefix /tmp/chromium-release-smoke --ignore-scripts playwright-core@1.65.0
NODE_PATH=/tmp/chromium-release-smoke/node_modules \
  CHROMIUM_SRC=/path/to/chromium/src node tests/smoke_release.cjs <packaged-chromium-executable>
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
- With component-updated Widevine 4.10.3112.0, the initial check passed EME
  access, MediaKeys creation, and two seconds of the public Shaka DASH sample.
  Later testing established that this exercised only its clear lead; it did
  not validate encrypted video. See the sustained-playback fix below.
- The same version guard rejects the previous M150 package as expected.

The packaged binary's Build ID is
`3d533b084ea53015a8c4e8f38567202d68355589`.

### Headless shell runtime comparison

On 2026-11-13, `headless_shell` was built in the same Linux x86-64 official
PGO output directory as Chrome, with identical GN arguments, both simulated
media modes, and no additional feature removal. The incremental build took
7 minutes 30 seconds, including about 7 minutes of linking.

Playwright 1.65.0 passes the existing media/version smoke test on both binaries.
The comparison harness also passes DOM interaction, request interception,
cookie isolation between contexts, screenshots, CDP evaluation, streaming Wasm
instantiation, Wasm in a worker, and local WebRTC binary data-channel echo.
These are capability checks, not validation of an application wasm-bindgen
bundle, external signaling, or STUN/TURN traversal.

Median results from three alternating trials on the same host:

| Measurement | Chrome in headless mode | Headless shell |
| --- | ---: | ---: |
| PSS, one idle page | 320 MiB | 168 MiB |
| PSS, ten idle pages | 421 MiB | 284 MiB |
| Launch through first local page load | 119 ms | 45 ms |
| Processes, one idle page | 9 | 6 |

PSS includes processes reported by CDP and their descendants, including
zygotes. Pages share one browser context. Trials use fresh profiles, without
flushing the OS file cache; compilation was finished before measurement.
CDP-reported CPU consumption over two-second idle intervals was 0–0.03 CPU
seconds for Chrome and 0–0.01 for the shell. These small samples do not establish
an active-workload CPU improvement. The memory reduction primarily concerns
browser baseline overhead; it should not be extrapolated to per-peer savings
under P2P traffic.

Before the integration described below, Widevine was a compatibility difference:
the shell rejected
`requestMediaKeySystemAccess('com.widevine.alpha', ...)` with
`NotSupportedError`, even with a copy of Chrome's working CDM profile.
The original `HeadlessContentClient` did not register CDMs and the renderer did
not expose their key systems. The explicit provisioning support below addresses
this for Linux software decryption and temporary sessions.

To reproduce after building both targets:

```sh
NODE_PATH=/tmp/chromium-headless-comparison/node_modules \
  CHROMIUM_SRC=/path/to/chromium/src node tests/compare_headless.cjs \
  out/SimulatedDeb-158.0.8127.18/chrome \
  out/SimulatedDeb-158.0.8127.18/headless_shell \
  /tmp/headless-comparison.json
```

Install `playwright-core@1.65.0` into the chosen dependency directory first.
The JSON records raw samples and capability results; the separate Widevine
probe is not part of this harness.

### Widevine in headless shell

Linux builds with `enable_widevine=true` and `enable_library_cdms=true` can
load an explicitly provisioned Widevine CDM using:

```text
--widevine-cdm-directory=/absolute/path/to/WidevineCdm/4.10.3112.0
```

The directory must contain `manifest.json` and the platform-specific library,
for example `_platform_specific/linux_x64/libwidevinecdm.so`. The directory
can come from a working Chrome CDM installation. It is the version directory,
not the profile directory or the library file itself. Keep its contents fixed
while the browser is running, and restart after provisioning a new version.
The shell does not download or update the CDM.

The browser validates the manifest using Chromium's existing CDM parser,
registers software-secure capabilities, and forwards the directory to the
Linux zygotes so the library is loaded before sandbox entry. The renderer uses
Chromium's shared key-system capability implementation. This mode exposes only
temporary sessions; persistent licenses and hardware-secure decryption are
not supported.

Example Playwright launch:

```js
const browser = await chromium.launch({
  executablePath: '/path/to/headless_shell',
  headless: true,
  args: ['--widevine-cdm-directory=/absolute/path/to/WidevineCdm/4.10.3112.0'],
});
```

To run the regression probe, install `playwright-core@1.65.0` and `shaka-player@5.3.4`
outside the checkout and set `NODE_PATH` to that installation's `node_modules`:

```sh
CHROMIUM_SRC=/path/to/chromium/src node tests/smoke_headless_widevine.cjs \
  out/SimulatedDeb-158.0.8127.18/headless_shell \
  /absolute/path/to/WidevineCdm/4.10.3112.0
```

The probe checks missing configuration, relative paths, missing libraries,
malformed manifests, incompatible interface versions, and rejection of
persistent licenses. It then checks MediaKeys creation, usable license keys,
30 seconds of encrypted DASH playback, and CDM mappings in the Linux zygotes.
It also checks that video presentation callbacks remain at zero and that
drawing the video into a prefilled canvas leaves the canvas unchanged. Playback uses
the public Shaka Widevine sample and license server, requiring network access.

Validated on Linux x86-64 with Widevine 4.10.3112.0: the sample advances beyond
30 seconds with 754 decoded frames, zero presented frames, zero dropped frames,
and the CDM preloaded in both zygotes. Playwright's default launch disables the sandbox.
The optional `HEADLESS_TEST_SANDBOX=1` run was blocked before page creation by
this host's `No usable sandbox` error; sandbox-enabled playback remains to be
validated on a host configured to support it.

Set `HEADLESS_WIDEVINE_CDM_DIR` when running `compare_headless.cjs` to include
CDM preloading in the shell's compatibility and memory measurements. Its media
smoke subprocess still uses default launch settings; the dedicated DRM probe
above validates encrypted playback.

With Widevine configured, a new set of three alternating idle trials measured:

| Measurement | Chrome in headless mode | Shell with CDM preloaded |
| --- | ---: | ---: |
| PSS, one idle page | 315 MiB | 170 MiB |
| PSS, ten idle pages | 417 MiB | 285 MiB |
| Launch through first local page load | 116 ms | 51 ms |

The same process-tree accounting and shared-context methodology apply. Chrome
uses a fresh profile; the shell additionally preloads the provisioned CDM.
These samples measure idle overhead with the CDM available, not CPU or memory
during sustained encrypted playback or a P2P workload.

### Sustained protected playback without video presentation

The original simulated-video branch excluded `DecryptingVideoDecoder`. On the
Angel One sample, the resulting decrypt-only path returned `kNoKey` for video
even though EME reported the key as usable. The sample's clear lead hid this
from the original two-second smoke check. Video underflow began around eight
seconds, and the pipeline stopped around eleven seconds after its underflow
delay and the player's recovery seeks.

The fix keeps Chromium's existing CDM video decoder ahead of the null decoder.
Clear video still selects the null decoder. Audio decoding and both null sinks
are unchanged. The regression probe runs past the clear lead, verifies the CDM
video decoder and null audio decoder, and checks that no video output reaches
presentation or canvas. All 52 focused media tests pass. As a positive control,
a normally rendering Chrome for Testing browser produced 751 presentation
callbacks and changed the canvas on the same sample; the fixed shell produced
zero callbacks and left the canvas unchanged.

After rebuilding both targets with this fix, three alternating runs per target
of the encrypted VP9/AAC sample at 640×480 measured the following medians:

| Sustained playback measurement | Chrome in headless mode | Headless shell |
| --- | ---: | ---: |
| Browser CPU, percent of one core | 9.1% | 8.8% |
| Browser process-tree PSS | 454 MiB | 307 MiB |

Each run used a fresh persistent profile, one page, the same Widevine CDM, and
default Playwright launch settings. CPU was sampled between media seconds 10
and 30 across CDP-reported processes; PSS includes their descendants and was
sampled at the end. The Node driver is excluded. Compilation had finished.
All six runs reached 30 seconds with zero presentation callbacks and unchanged
canvas pixels. The CPU difference is small; the clearer benefit is lower memory
use. These results include real CDM video decoding, but not the application's
Wasm or P2P workload, and are not a prediction of capacity at high peer counts.
