#!/usr/bin/env python3
"""Publish a locally built Chromium .deb as a GitHub release using gh."""

from __future__ import annotations

import argparse
import hashlib
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


SRC_ROOT = Path(__file__).resolve().parents[2]


class PublishError(RuntimeError):
    pass


def run(command: list[str], *, capture_output: bool = False) -> subprocess.CompletedProcess[str]:
    print(f"+ {shlex.join(command)}", flush=True)
    return subprocess.run(
        command,
        cwd=SRC_ROOT,
        check=True,
        text=True,
        stdout=subprocess.PIPE if capture_output else None,
    )


def capture(command: list[str]) -> str:
    return subprocess.run(
        command,
        cwd=SRC_ROOT,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    ).stdout.strip()


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


def resolve_tag(requested: str | None) -> str:
    if requested:
        if not ref_exists(f"refs/tags/{requested}"):
            raise PublishError(f"local tag does not exist: {requested}")
        return requested
    tags = capture(
        ["git", "tag", "--points-at", "HEAD", "--list", "v*-simulated.*"]
    ).splitlines()
    if len(tags) != 1:
        raise PublishError(
            "could not infer one simulated release tag at HEAD; pass --tag"
        )
    return tags[0]


def chromium_version(tag: str) -> str:
    data = capture(["git", "show", f"{tag}:chrome/VERSION"])
    fields = dict(line.split("=", 1) for line in data.splitlines() if "=" in line)
    try:
        return ".".join(
            fields[name] for name in ("MAJOR", "MINOR", "BUILD", "PATCH")
        )
    except KeyError as error:
        raise PublishError(f"could not parse chrome/VERSION at {tag}") from error


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def package_metadata(artifact: Path) -> dict[str, str]:
    if shutil.which("dpkg-deb") is None:
        return {}
    result = subprocess.run(
        ["dpkg-deb", "--field", str(artifact), "Package", "Version", "Architecture"],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    )
    metadata: dict[str, str] = {}
    for line in result.stdout.splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            metadata[key.strip()] = value.strip()
    return metadata


def generated_notes(tag: str, artifact: Path, digest: str) -> str:
    version = chromium_version(tag)
    base = capture(
        ["git", "describe", "--tags", "--match", "[0-9]*", "--abbrev=0", tag]
    )
    commits = capture(["git", "log", "--format=- `%h` %s", f"{base}..{tag}"])
    metadata = package_metadata(artifact)
    package_lines = [
        f"- File: `{artifact.name}`",
        f"- SHA-256: `{digest}`",
    ]
    for key in ("Package", "Version", "Architecture"):
        if key in metadata:
            package_lines.append(f"- {key}: `{metadata[key]}`")
    return "\n".join(
        [
            f"Simulated-media Chromium build based on upstream `{version}`.",
            "",
            "## Package",
            "",
            *package_lines,
            "",
            "## Fork patch stack",
            "",
            commits or "- No commits found above the Chromium tag.",
            "",
            "This release was built locally with the documented official PGO "
            "configuration because the repository intentionally has no CI build.",
        ]
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Push a prepared tag and publish a local .deb with gh."
    )
    parser.add_argument("artifact", type=Path, help="path to the built .deb")
    parser.add_argument("--tag", help="annotated release tag (default: infer at HEAD)")
    parser.add_argument("--repo", default="rillanetwork/chromium")
    parser.add_argument("--remote", default="origin")
    parser.add_argument("--title")
    parser.add_argument("--notes-file", type=Path, help="additional release notes")
    parser.add_argument(
        "--push-branch", action="store_true", help="push the current branch before the tag"
    )
    parser.add_argument("--draft", action="store_true")
    parser.add_argument("--prerelease", action="store_true")
    parser.add_argument(
        "--dry-run", action="store_true", help="validate and print without writing or publishing"
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    artifact = args.artifact.resolve()
    if not artifact.is_file():
        raise PublishError(f"artifact does not exist: {artifact}")
    if artifact.suffix != ".deb":
        raise PublishError(f"artifact is not a .deb: {artifact}")
    if shutil.which("git") is None:
        raise PublishError("required command is not on PATH: git")

    tag = resolve_tag(args.tag)
    if capture(["git", "cat-file", "-t", f"refs/tags/{tag}"]) != "tag":
        raise PublishError(f"release tag must be annotated: {tag}")
    version = chromium_version(tag)
    if version not in artifact.name:
        raise PublishError(
            f"artifact name does not contain tag's Chromium version {version}"
        )

    digest = sha256(artifact)
    checksum = artifact.with_name(f"{artifact.name}.sha256")
    notes = generated_notes(tag, artifact, digest)
    if args.notes_file:
        notes += "\n\n" + args.notes_file.read_text(encoding="utf-8").strip() + "\n"
    title = args.title or f"Simulated media decode for Chromium {version}"

    print(f"Tag: {tag}")
    print(f"Artifact: {artifact}")
    print(f"SHA-256: {digest}")
    if args.dry_run:
        print("\nRelease notes:\n")
        print(notes)
        print("\nDry run: no checksum, push, or GitHub release was created.")
        return 0

    if shutil.which("gh") is None:
        raise PublishError("required command is not on PATH: gh")
    run(["gh", "auth", "status", "--hostname", "github.com"])
    existing = subprocess.run(
        ["gh", "release", "view", tag, "--repo", args.repo],
        cwd=SRC_ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if existing.returncode == 0:
        raise PublishError(f"GitHub release already exists: {tag}")

    checksum.write_text(f"{digest}  {artifact.name}\n", encoding="utf-8")
    if args.push_branch:
        branch = capture(["git", "branch", "--show-current"])
        if not branch:
            raise PublishError("HEAD is detached; cannot --push-branch")
        run(["git", "push", "-u", args.remote, branch])
    run(["git", "push", args.remote, f"refs/tags/{tag}"])

    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as notes_file:
        notes_file.write(notes)
        notes_path = Path(notes_file.name)
    try:
        command = [
            "gh",
            "release",
            "create",
            tag,
            str(artifact),
            str(checksum),
            "--repo",
            args.repo,
            "--verify-tag",
            "--title",
            title,
            "--notes-file",
            str(notes_path),
        ]
        if args.draft:
            command.append("--draft")
        if args.prerelease:
            command.append("--prerelease")
        run(command)
    finally:
        notes_path.unlink(missing_ok=True)

    url = run(
        [
            "gh",
            "release",
            "view",
            tag,
            "--repo",
            args.repo,
            "--json",
            "url",
            "--jq",
            ".url",
        ],
        capture_output=True,
    ).stdout.strip()
    print(f"Published: {url}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (PublishError, subprocess.CalledProcessError, OSError) as error:
        print(f"error: {error}", file=sys.stderr)
        sys.exit(1)
