# ![Logo](chrome/app/theme/chromium/product_logo_64.png) Chromium

## Rillanetwork simulated video decode fork

This fork can build Chromium with `enable_simulated_video_decode=true`. In that
configuration Chromium still demuxes media, advances the media timeline, decodes
audio, and presents correctly sized black video frames, but it does not decode
the compressed video pictures. The option defaults to `false`, so ordinary
Chromium builds are unchanged.

For the architecture, limitations, complete build instructions, and the process
for moving the patch to a new Chromium stable release, see
[Simulated video decode](docs/simulated_video_decode.md).

### Quick start (Linux development build)

From a complete `gclient` checkout with this branch checked out:

```sh
gclient sync
cd src

gn gen out/Simulated --args='is_debug=true is_component_build=true symbol_level=0 proprietary_codecs=true ffmpeg_branding="Chrome" enable_simulated_video_decode=true'
autoninja -C out/Simulated chrome media_unittests

out/Simulated/media_unittests \
  --gtest_filter='NullVideoDecoderTest.*:DefaultDecoderFactoryTest.*:SupportedTypesTest.*'
```

### Quick start (optimized Debian package)

Official builds need both the Chrome and V8 PGO profiles for the checked-out
revision. Refresh both after changing Chromium versions:

```sh
python3 tools/update_pgo_profiles.py \
  --target=linux update \
  --gs-url-base=chromium-optimization-profiles/pgo_profiles
python3 v8/tools/builtins-pgo/download_profiles.py download \
  --depot-tools third_party/depot_tools \
  --check-v8-revision --quiet

gn gen out/SimulatedDeb --args='is_debug=false is_official_build=true is_component_build=false symbol_level=0 blink_symbol_level=0 proprietary_codecs=true ffmpeg_branding="Chrome" enable_simulated_video_decode=true enable_linux_installer=true'
autoninja -C out/SimulatedDeb chrome/installer/linux:unstable_deb
```

Use a fresh, versioned output directory when changing Chromium releases; build
outputs and generated headers are not portable between release branches.

Chromium is an open-source browser project that aims to build a safer, faster,
and more stable way for all users to experience the web.

The project's web site is https://www.chromium.org.

To check out the source code locally, don't use `git clone`! Instead,
follow [the instructions on how to get the code](docs/get_the_code.md).

Documentation in the source is rooted in [docs/README.md](docs/README.md).

Learn how to [Get Around the Chromium Source Code Directory
Structure](https://www.chromium.org/developers/how-tos/getting-around-the-chrome-source-code).

For historical reasons, there are some small top level directories. Now the
guidance is that new top level directories are for product (e.g. Chrome,
Android WebView, Ash). Even if these products have multiple executables, the
code should be in subdirectories of the product.



If you found a bug, please file it at https://crbug.com/new.
