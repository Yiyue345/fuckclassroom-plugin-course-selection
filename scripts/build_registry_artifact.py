from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import tempfile
import zipfile
from pathlib import Path

PLUGIN_ID = "course_selection"
SEMVER_RE = re.compile(
    r"^(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
ROOT_FILES = (
    "__init__.py", "plugin.json", "plugin.py", "accounts.py", "lifecycle.py",
    "routes.py", "services.py", "settings_hooks.py", "worker.py",
    "assistant.py", "api.py", "automation.py", "browser.py", "catalog.py",
    "helpers.py", "hy2_prebuilt.py", "models.py", "proxy.py", "session.py",
    "requirements.txt", "README.md",
)
TREE_DIRS = ("templates", "static", "rust_module")


def validate_manifest(manifest: dict[str, object], version: str) -> None:
    if manifest.get("id") != PLUGIN_ID or manifest.get("version") != version:
        raise ValueError("plugin id/version mismatch")
    if manifest.get("api_version") != 1 or manifest.get("execution") != "in_process":
        raise ValueError("course_selection must use Plugin API v1 in_process execution")
    if manifest.get("entry") != "plugin.py" or manifest.get("requires") != ["core_ui"]:
        raise ValueError("invalid course_selection entry/dependency")
    if manifest.get("route_prefix") is not None or manifest.get("rpc_api_version") is not None or manifest.get("rpc_permissions") != []:
        raise ValueError("in-process plugin cannot declare RPC runtime fields")
    source_dir = Path("rust_module") / "hy2_proxy"
    if not (source_dir / "Cargo.toml").is_file():
        raise ValueError("course_selection artifact must include the Hy2 Rust source")


def build(version: str, root: Path) -> tuple[Path, Path]:
    if not SEMVER_RE.fullmatch(version):
        raise ValueError(f"invalid SemVer: {version}")
    source_manifest = json.loads((root / "plugin.json").read_text(encoding="utf-8"))
    if version.split("-", 1)[0] != str(source_manifest["version"]).split("-", 1)[0]:
        raise ValueError("release base version differs from plugin.json")

    artifact = root / f"{PLUGIN_ID}-{version}.zip"
    checksum = root / f"{artifact.name}.sha256"
    artifact.unlink(missing_ok=True)
    checksum.unlink(missing_ok=True)

    with tempfile.TemporaryDirectory(prefix="course-selection-package-") as temp:
        package = Path(temp)
        for relative in ROOT_FILES:
            source = root / relative
            if not source.is_file():
                raise FileNotFoundError(relative)
            shutil.copy2(source, package / relative)
        for directory in TREE_DIRS:
            source = root / directory
            if not source.is_dir():
                raise FileNotFoundError(directory)
            shutil.copytree(source, package / directory)

        manifest_path = package / "plugin.json"
        packaged = json.loads(manifest_path.read_text(encoding="utf-8"))
        packaged["version"] = version
        validate_manifest(packaged, version)
        manifest_path.write_text(json.dumps(packaged, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        with zipfile.ZipFile(artifact, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for source in sorted(package.rglob("*")):
                if source.is_file():
                    archive.write(source, source.relative_to(package).as_posix())

    with zipfile.ZipFile(artifact) as archive:
        names = archive.namelist()
        if names.count("plugin.json") != 1 or any(name.startswith(f"{PLUGIN_ID}/") for name in names):
            raise ValueError("invalid Registry v1 ZIP layout")
        validate_manifest(json.loads(archive.read("plugin.json")), version)

    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    checksum.write_text(f"{digest}  {artifact.name}\n", encoding="utf-8")
    return artifact, checksum


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("version")
    args = parser.parse_args()
    artifact, checksum = build(args.version, Path(__file__).resolve().parents[1])
    print(artifact.name)
    print(checksum.name)


if __name__ == "__main__":
    main()
