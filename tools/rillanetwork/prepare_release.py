#!/usr/bin/env python3
"""Port the simulated-video patch stack to a Chromium release and build it."""

from __future__ import annotations

import argparse
import hashlib
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path


SRC_ROOT = Path(__file__).resolve().parents[2]
CHECKOUT_ROOT = SRC_ROOT.parent
VERSION_RE = re.compile(r"^(\d+)\.\d+\.\d+\.\d+$")
TEST_FILTER = (
    "NullAudioDecoderTest.*:NullVideoDecoderTest.*:NullVideoSinkTest.*:"
    "DefaultDecoderFactoryTest.*:SupportedTypesTest.*"
)
DEV_ARGS = (
    'is_debug=true is_component_build=true symbol_level=0 '
    'proprietary_codecs=true ffmpeg_branding="Chrome" '
    "enable_widevine=true bundle_widevine_cdm=false "
    "enable_simulated_video_decode=true enable_simulated_audio_decode=true"
)
RELEASE_ARGS = (
    "is_debug=false is_official_build=true is_component_build=false "
    "symbol_level=0 blink_symbol_level=0 proprietary_codecs=true "
    'ffmpeg_branding="Chrome" enable_widevine=true bundle_widevine_cdm=false '
    "enable_simulated_video_decode=true "
    "enable_simulated_audio_decode=true "
    "enable_linux_installer=true"
)


class ReleaseError(RuntimeError):
    pass


class Runner:
    def __init__(self, dry_run: bool) -> None:
        self.dry_run = dry_run

    def run(self, command: list[str], cwd: Path = SRC_ROOT) -> None:
        print(f"+ (cd {cwd} && {shlex.join(command)})", flush=True)
        if not self.dry_run:
            subprocess.run(command, cwd=cwd, check=True)


def capture(command: list[str], cwd: Path = SRC_ROOT) -> str:
    return subprocess.run(
        command,
        cwd=cwd,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    ).stdout.strip()


def require_tool(name: str) -> None:
    if shutil.which(name) is None:
        raise ReleaseError(f"required command is not on PATH: {name}")


def require_clean_tracked_worktree() -> None:
    status = capture(["git", "status", "--porcelain", "--untracked-files=no"])
    if status:
        raise ReleaseError(
            "tracked or staged changes are present; commit or stash them before "
            "preparing a release"
        )


def chromium_version(ref: str = "HEAD") -> str:
    data = capture(["git", "show", f"{ref}:chrome/VERSION"])
    fields = dict(line.split("=", 1) for line in data.splitlines() if "=" in line)
    try:
        return ".".join(
            fields[name] for name in ("MAJOR", "MINOR", "BUILD", "PATCH")
        )
    except KeyError as error:
        raise ReleaseError(f"could not parse chrome/VERSION at {ref}") from error


def nearest_chromium_tag(ref: str) -> str:
    try:
        return capture(
            ["git", "describe", "--tags", "--match", "[0-9]*", "--abbrev=0", ref]
        )
    except subprocess.CalledProcessError as error:
        raise ReleaseError(
            f"could not find a numeric Chromium tag reachable from {ref}; "
            "pass --source-base explicitly"
        ) from error


def commits_between(base: str, ref: str) -> list[str]:
    output = capture(["git", "rev-list", "--reverse", f"{base}..{ref}"])
    commits = output.splitlines() if output else []
    if not commits:
        raise ReleaseError(f"no fork commits found in {base}..{ref}")
    return commits


def ref_exists(ref: str) -> bool:
    return (
        subprocess.run(
            ["git", "rev-parse", "--quiet", "--verify", ref],
            cwd=SRC_ROOT,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ).returncode
        == 0
    )


def write_checksum(artifact: Path) -> tuple[Path, str]:
    digest = hashlib.sha256()
    with artifact.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    value = digest.hexdigest()
    checksum = artifact.with_name(f"{artifact.name}.sha256")
    checksum.write_text(f"{value}  {artifact.name}\n", encoding="utf-8")
    return checksum, value


def find_package(output_dir: Path, version: str) -> Path:
    candidates = sorted(
        path for path in output_dir.glob("*.deb") if version in path.name
    )
    if len(candidates) != 1:
        names = ", ".join(path.name for path in candidates) or "none"
        raise ReleaseError(
            f"expected one {version} .deb in {output_dir}, found: {names}"
        )
    return candidates[0].resolve()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Create a release branch from a Chromium version tag, replay the "
            "current fork patch stack, validate it, and build an optimized .deb."
        )
    )
    parser.add_argument("version", help="Chromium tag, for example 150.0.7871.114")
    parser.add_argument(
        "--source-ref",
        default="HEAD",
        help="fork branch/ref whose commits should be replayed (default: HEAD)",
    )
    parser.add_argument(
        "--source-base",
        help="base Chromium tag for --source-ref (default: nearest numeric tag)",
    )
    parser.add_argument("--branch", help="new branch name")
    parser.add_argument("--tag", help="local annotated release tag")
    parser.add_argument(
        "--release-revision",
        type=int,
        default=1,
        help="suffix used by the default release tag (default: 1)",
    )
    parser.add_argument(
        "--upstream", default="upstream", help="Chromium Git remote (default: upstream)"
    )
    parser.add_argument(
        "--reuse-branch",
        action="store_true",
        help="skip fetch/switch/cherry-pick and build the current branch",
    )
    parser.add_argument("--skip-sync", action="store_true")
    parser.add_argument("--skip-tests", action="store_true")
    parser.add_argument("--skip-pgo-download", action="store_true")
    parser.add_argument("--skip-package", action="store_true")
    parser.add_argument("--skip-tag", action="store_true")
    parser.add_argument(
        "--artifact",
        type=Path,
        help="existing .deb to checksum/tag when --skip-package is used",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="print mutating commands without running them"
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    match = VERSION_RE.fullmatch(args.version)
    if not match:
        raise ReleaseError("version must have four numeric components")
    if args.release_revision < 1:
        raise ReleaseError("--release-revision must be positive")
    if args.artifact and not args.skip_package:
        raise ReleaseError("--artifact is only valid with --skip-package")

    major = match.group(1)
    branch = args.branch or f"simulated-video-decode-m{major}"
    tag = args.tag or f"v{args.version}-simulated.{args.release_revision}"
    dev_output = Path(f"out/Simulated-{args.version}")
    release_output = Path(f"out/SimulatedDeb-{args.version}")
    runner = Runner(args.dry_run)

    for tool in ("git", "gn", "autoninja"):
        require_tool(tool)
    if not args.skip_sync:
        require_tool("gclient")

    require_clean_tracked_worktree()

    source_ref = capture(["git", "rev-parse", args.source_ref])
    source_base = args.source_base or nearest_chromium_tag(source_ref)
    patch_commits = commits_between(source_base, source_ref)

    print(f"Source patch stack: {source_base}..{source_ref}")
    for commit in patch_commits:
        subject = capture(["git", "show", "-s", "--format=%s", commit])
        print(f"  {commit[:12]} {subject}")

    if not args.reuse_branch:
        if ref_exists(f"refs/heads/{branch}"):
            raise ReleaseError(f"local branch already exists: {branch}")
        runner.run(["git", "fetch", args.upstream, "tag", args.version])
        runner.run(["git", "switch", "-c", branch, args.version])
        for commit in patch_commits:
            try:
                runner.run(["git", "cherry-pick", commit])
            except subprocess.CalledProcessError as error:
                print(
                    "Cherry-pick stopped. Resolve conflicts, run "
                    "`git cherry-pick --continue`, then rerun this command with "
                    "--reuse-branch.",
                    file=sys.stderr,
                )
                raise ReleaseError(f"failed to cherry-pick {commit}") from error

    if not args.skip_sync:
        runner.run(["gclient", "sync"], cwd=CHECKOUT_ROOT)

    if not args.dry_run:
        actual_version = chromium_version()
        if actual_version != args.version:
            raise ReleaseError(
                f"checked-out chrome/VERSION is {actual_version}, expected {args.version}"
            )

    if not args.skip_tests:
        runner.run(["gn", "gen", str(dev_output), f"--args={DEV_ARGS}"])
        runner.run(["autoninja", "-C", str(dev_output), "media_unittests"])
        runner.run(
            [str(dev_output / "media_unittests"), f"--gtest_filter={TEST_FILTER}"]
        )

    if not args.skip_pgo_download:
        runner.run(
            [
                "python3",
                "tools/update_pgo_profiles.py",
                "--target=linux",
                "update",
                "--gs-url-base=chromium-optimization-profiles/pgo_profiles",
            ]
        )
        runner.run(
            [
                "python3",
                "v8/tools/builtins-pgo/download_profiles.py",
                "download",
                "--depot-tools",
                "third_party/depot_tools",
                "--check-v8-revision",
                "--quiet",
            ]
        )

    artifact = args.artifact.resolve() if args.artifact else None
    if not args.skip_package:
        runner.run(["gn", "gen", str(release_output), f"--args={RELEASE_ARGS}"])
        runner.run(
            [
                "autoninja",
                "-C",
                str(release_output),
                "chrome/installer/linux:unstable_deb",
            ]
        )
        if not args.dry_run:
            artifact = find_package(SRC_ROOT / release_output, args.version)

    if args.dry_run:
        if not args.skip_tag:
            print(f"+ create annotated tag {tag} after locating and hashing the .deb")
        return 0

    if artifact is not None and not artifact.is_file():
        raise ReleaseError(f"artifact does not exist: {artifact}")
    if not args.skip_tag and artifact is None:
        raise ReleaseError("tagging requires a built artifact or --artifact")

    if artifact is not None:
        checksum_path, checksum = write_checksum(artifact)
        print(f"Artifact: {artifact}")
        print(f"Checksum: {checksum_path} ({checksum})")

    if not args.skip_tag:
        if ref_exists(f"refs/tags/{tag}"):
            raise ReleaseError(f"local tag already exists: {tag}")
        require_clean_tracked_worktree()
        message = (
            f"Simulated media decode for Chromium {args.version}\n\n"
            f"Artifact: {artifact.name}\n"
            f"SHA256: {checksum}"
        )
        runner.run(["git", "tag", "-a", tag, "-m", message])
        print(f"Created local tag: {tag}")

    print("Preparation complete.")
    if artifact is not None:
        print("Publish with:")
        publish = SRC_ROOT / "tools/rillanetwork/publish_release.py"
        command = ["python3", str(publish), str(artifact)]
        if not args.skip_tag:
            command.extend(["--tag", tag])
        print(shlex.join(command))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ReleaseError, subprocess.CalledProcessError) as error:
        print(f"error: {error}", file=sys.stderr)
        sys.exit(1)
