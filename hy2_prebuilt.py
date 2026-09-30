from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
from pathlib import Path


_MANIFEST_NAME = "manifest.json"
_MANIFEST_SCHEMA = 1


class Hy2PrebuiltError(RuntimeError):
    pass


def _tracked_source_files(source_dir: Path) -> list[Path]:
    tracked: list[Path] = []
    for name in ("Cargo.toml", "Cargo.lock", "rust-toolchain.toml", "build.rs"):
        candidate = source_dir / name
        if candidate.is_file():
            tracked.append(candidate)
    src_dir = source_dir / "src"
    if src_dir.is_dir():
        tracked.extend(sorted(path for path in src_dir.rglob("*.rs") if path.is_file()))
    return sorted(tracked, key=lambda path: path.relative_to(source_dir).as_posix())


def source_fingerprint(source_dir: Path) -> str:
    digest = hashlib.sha256()
    files = _tracked_source_files(source_dir)
    if not files:
        raise Hy2PrebuiltError(f"没有找到可用于生成 Hy2 源码指纹的文件：{source_dir}")
    for path in files:
        relative = path.relative_to(source_dir).as_posix().encode("utf-8")
        digest.update(relative)
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def current_platform_key() -> str | None:
    system = platform.system().strip().lower()
    machine = platform.machine().strip().lower()
    if system == "darwin":
        system = "macos"
    elif system not in {"windows", "linux"}:
        return None

    if machine in {"amd64", "x86_64", "x64"}:
        machine = "x86_64"
    elif machine in {"arm64", "aarch64"}:
        machine = "aarch64"
    else:
        return None
    return f"{system}-{machine}"


def _latest_source_mtime_ns(source_dir: Path) -> int:
    latest = 0
    for path in _tracked_source_files(source_dir):
        try:
            latest = max(latest, path.stat().st_mtime_ns)
        except OSError:
            return 0
    return latest


def install_prebuilt_runner(
    source_dir: Path,
    runner_path: Path,
    *,
    platform_key: str | None = None,
) -> bool:
    """Install a checked-in CI-built runner when it exactly matches current source.

    Returns True when a verified prebuilt is available for the current source/platform.
    The function may return True without copying when the already-installed runner is
    newer than the source, which keeps a fresh local Cargo build intact.
    """
    source_dir = source_dir.resolve()
    runner_path = runner_path.resolve()

    if runner_path.is_file():
        try:
            if runner_path.stat().st_mtime_ns >= _latest_source_mtime_ns(source_dir):
                return True
        except OSError:
            pass

    prebuilt_dir = source_dir / "prebuilt"
    manifest_path = prebuilt_dir / _MANIFEST_NAME
    if not manifest_path.is_file():
        return False

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise Hy2PrebuiltError(f"Hy2 预编译清单无法读取：{exc}") from exc

    if manifest.get("schema") != _MANIFEST_SCHEMA:
        return False
    current_fingerprint = source_fingerprint(source_dir)
    if manifest.get("source_sha256") != current_fingerprint:
        return False

    key = platform_key or current_platform_key()
    if not key:
        return False
    runners = manifest.get("runners")
    if not isinstance(runners, dict):
        return False
    entry = runners.get(key)
    if not isinstance(entry, dict):
        return False

    relative_file = str(entry.get("file") or "")
    expected_sha256 = str(entry.get("sha256") or "")
    if not relative_file or not expected_sha256:
        return False
    candidate = (prebuilt_dir / relative_file).resolve()
    try:
        candidate.relative_to(prebuilt_dir.resolve())
    except ValueError as exc:
        raise Hy2PrebuiltError("Hy2 预编译清单包含非法路径") from exc
    if not candidate.is_file():
        return False
    actual_sha256 = _sha256_file(candidate)
    if actual_sha256 != expected_sha256:
        raise Hy2PrebuiltError(
            f"Hy2 预编译组件校验失败：{candidate.name} SHA-256 不匹配"
        )

    runner_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = runner_path.with_name(runner_path.name + ".prebuilt.tmp")
    shutil.copyfile(candidate, temporary)
    if os.name != "nt":
        temporary.chmod(0o755)
    temporary.replace(runner_path)
    # Mark installation time, so the existing mtime-based source fallback sees this
    # verified runner as current and does not immediately invoke Cargo again.
    os.utime(runner_path, None)
    return True


def package_prebuilt_artifacts(artifact_root: Path, source_dir: Path, output_dir: Path) -> None:
    """Collect GitHub Actions artifacts and generate the checked-in manifest."""
    artifact_root = artifact_root.resolve()
    source_dir = source_dir.resolve()
    output_dir = output_dir.resolve()
    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    runners: dict[str, dict[str, str]] = {}
    for artifact_dir in sorted(artifact_root.glob("hy2-*")):
        if not artifact_dir.is_dir():
            continue
        key = artifact_dir.name.removeprefix("hy2-")
        candidates = [
            artifact_dir / "hy2_serve.exe",
            artifact_dir / "hy2_serve",
        ]
        binary = next((path for path in candidates if path.is_file()), None)
        if binary is None:
            continue
        platform_dir = output_dir / key
        platform_dir.mkdir(parents=True, exist_ok=True)
        destination = platform_dir / binary.name
        shutil.copyfile(binary, destination)
        if destination.suffix != ".exe":
            destination.chmod(0o755)
        relative = destination.relative_to(output_dir).as_posix()
        runners[key] = {
            "file": relative,
            "sha256": _sha256_file(destination),
        }

    if not runners:
        raise Hy2PrebuiltError(f"没有找到 Hy2 runner artifacts：{artifact_root}")
    manifest = {
        "schema": _MANIFEST_SCHEMA,
        "source_sha256": source_fingerprint(source_dir),
        "runners": runners,
    }
    (output_dir / _MANIFEST_NAME).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _main() -> None:
    parser = argparse.ArgumentParser(description="Hy2 prebuilt runner helper")
    subparsers = parser.add_subparsers(dest="command", required=True)
    package = subparsers.add_parser("package")
    package.add_argument("artifact_root", type=Path)
    package.add_argument("source_dir", type=Path)
    package.add_argument("output_dir", type=Path)
    args = parser.parse_args()
    if args.command == "package":
        package_prebuilt_artifacts(args.artifact_root, args.source_dir, args.output_dir)


if __name__ == "__main__":
    _main()
