from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Optional

from .models import (
    PackageCommonMetadata,
    PackageIndex,
    RepositoryConfig,
    RepositoryIndex,
    VersionMetadata,
) 


DATA_ROOT_ENV_VAR = "WINGET_REPO_DATA_DIR"

# Resolve repository root (project root, not the Python package root)
_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_DATA_DIR = _REPO_ROOT / "data"


_data_dir: Optional[Path] = None
_repository_config: Optional[RepositoryConfig] = None
_repository_index: RepositoryIndex = RepositoryIndex()

_INDEX_TASK: Optional[asyncio.Task] = None

# Package identifier used for the example/demo package, which is excluded from the index.
EXAMPLE_PACKAGE_ID = "our.example"


def get_data_dir() -> Path:
    """
    Determine the data directory path.

    Priority:
    1. Environment variable WINGET_REPO_DATA_DIR
    2. '<workspace root>/data' (works naturally with VS Code workspace)
    """
    global _data_dir
    if _data_dir is None:
        env_path = os.environ.get(DATA_ROOT_ENV_VAR)
        if env_path:
            _data_dir = Path(env_path).expanduser()
        else:
            _data_dir = _DEFAULT_DATA_DIR

        _data_dir.mkdir(parents=True, exist_ok=True)
    return _data_dir


def get_repository_config() -> RepositoryConfig:
    """
    Return the current repository configuration (after initialization).
    """
    if _repository_config is None:
        # This should not happen if initialize_repository() has been called,
        # but we provide a safe fallback.
        return RepositoryConfig()
    return _repository_config


def get_repository_index() -> RepositoryIndex:
    """
    Return the current in-memory repository index.
    """
    return _repository_index


def _config_path() -> Path:
    return get_data_dir() / "repository.json"


def _load_repository_config_from_disk() -> RepositoryConfig:
    """
    Load repository.json, merging with defaults for any missing fields,
    and write it back so any new fields are persisted.
    """
    path = _config_path()
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            config = RepositoryConfig(**raw)
        except Exception:
            # If parsing fails, fall back to defaults and overwrite file.
            config = RepositoryConfig()
    else:
        config = RepositoryConfig()

    # Persist with all fields populated (including any new defaults).
    path.write_text(config.model_dump_json(indent=2), encoding="utf-8")
    return config


def _ensure_example_package_exists() -> None:
    """
    Ensure the example package tree exists on disk with reasonable example data.

    This package is *ignored* when building the in-memory index, but clearly
    documents the expected on-disk structure.
    """
    data_dir = get_data_dir()
    pkg_dir = data_dir / EXAMPLE_PACKAGE_ID
    package_json = pkg_dir / "package.json"

    pkg_dir.mkdir(parents=True, exist_ok=True)

    if package_json.exists():
        try:
            raw = json.loads(package_json.read_text(encoding="utf-8"))
            pkg_meta = PackageCommonMetadata(**raw)
        except Exception:
            pkg_meta = PackageCommonMetadata(
                package_identifier=EXAMPLE_PACKAGE_ID,
                package_name="Our Example App",
                publisher="Example Publisher",
                short_description="Example package used to document the repository layout.",
                license="Freeware",
                tags=["example", "documentation"],
                is_example=True,
            )
    else:
        pkg_meta = PackageCommonMetadata(
            package_identifier=EXAMPLE_PACKAGE_ID,
            package_name="Our Example App",
            publisher="Example Publisher",
            short_description="Example package used to document the repository layout.",
            license="Freeware",
            tags=["example", "documentation"],
            is_example=True,
        )

    # Ensure is_example is set so the indexer will skip this package.
    if not pkg_meta.is_example:
        pkg_meta.is_example = True

    package_json.write_text(pkg_meta.model_dump_json(indent=2), encoding="utf-8")

    # Example version: data/our.example/1.0-x86-user/version.json
    version_dir = pkg_dir / "1.0-x86-user"
    version_dir.mkdir(parents=True, exist_ok=True)
    version_json = version_dir / "version.json"

    if version_json.exists():
        try:
            raw = json.loads(version_json.read_text(encoding="utf-8"))
            version_meta = VersionMetadata(**raw)
        except Exception:
            version_meta = VersionMetadata(
                version="1.0",
                architecture="x86",
                scope="user",
                installer_type="exe",
                installer_file="our-example-installer.exe",
                silent_arguments="/quiet",
                interactive_arguments="/passive",
                log_arguments="/log install.log",
            )
    else:
        version_meta = VersionMetadata(
            version="1.0",
            architecture="x86",
            scope="user",
            installer_type="exe",
            installer_file="our-example-installer.exe",
            silent_arguments="/quiet",
            interactive_arguments="/passive",
            log_arguments="/log install.log",
        )

    version_json.write_text(version_meta.model_dump_json(indent=2), encoding="utf-8")


def build_index_from_disk() -> None:
    """
    Crawl the data directory and build the in-memory index of packages/versions.

    The example package is intentionally excluded.
    """
    global _repository_index

    data_dir = get_data_dir()
    index = RepositoryIndex()

    if not data_dir.exists():
        data_dir.mkdir(parents=True, exist_ok=True)

    for pkg_dir in data_dir.iterdir():
        if not pkg_dir.is_dir():
            continue

        package_json = pkg_dir / "package.json"

        if not package_json.exists():
            # Not a valid package folder; skip.
            continue

        try:
            raw = json.loads(package_json.read_text(encoding="utf-8"))
            pkg_meta = PackageCommonMetadata(**raw)
        except Exception:
            # Skip malformed packages for now.
            continue

        # Use the package identifier from metadata as the logical key.
        # The on-disk folder name is treated as an implementation detail.
        package_id = pkg_meta.package_identifier

        # Skip the example package entirely when building the index.
        if package_id == EXAMPLE_PACKAGE_ID or pkg_meta.is_example:
            continue

        package_index = PackageIndex(
            package=pkg_meta,
            versions=[],
            storage_path=str(pkg_dir.relative_to(data_dir)),
        )

        # Traverse version folders directly under the package directory.
        # Expected convention: "<version>-<architecture>-<scope>", e.g. "1.0-x86-user".
        # The folder name is used only as a hint; the JSON fields are authoritative.
        for version_dir in pkg_dir.iterdir():
            if not version_dir.is_dir():
                continue
            if version_dir.name == "x86" or version_dir.name == "arm" or version_dir.name == "x64":
                # Old-style nested layout (arch/scope/version) is ignored by this indexer.
                continue

            version_json = version_dir / "version.json"
            if not version_json.exists():
                continue

            try:
                raw = json.loads(version_json.read_text(encoding="utf-8"))
            except Exception:
                continue

            # Derive hints from the folder name if it matches "<version>-<arch>-<scope>".
            folder_version = folder_arch = folder_scope = None
            parts = version_dir.name.split("-")
            if len(parts) == 3:
                folder_version, folder_arch, folder_scope = parts

            # If the JSON is missing core fields, fall back to the folder hints.
            # We intentionally only *fill in* missing values instead of overriding
            # anything that is already present in the JSON.
            if "version" not in raw and folder_version is not None:
                raw["version"] = folder_version
            if "architecture" not in raw and folder_arch is not None:
                raw["architecture"] = folder_arch
            if "scope" not in raw and folder_scope is not None:
                raw["scope"] = folder_scope

            try:
                version_meta = VersionMetadata(**raw)
            except Exception:
                continue

            # Record where this version lives on disk relative to the data directory
            # so that APIs can construct paths to installer files, logs, etc.
            version_meta.storage_path = str(version_dir.relative_to(data_dir))

            package_index.versions.append(version_meta)

        # Always include the package in the index, even if it currently has
        # no versions. This is important for the admin UI, which needs to
        # manage packages before any versions are created.
        index.packages[package_id] = package_index

    index.last_built_at = get_repository_config().created_at if index.last_built_at is None else index.last_built_at
    _repository_index = index


async def _periodic_rebuild_loop() -> None:
    """
    Background task that refreshes the in-memory index every refresh_interval_seconds.
    """
    while True:
        config = get_repository_config()
        await asyncio.sleep(config.refresh_interval_seconds)
        build_index_from_disk()


async def initialize_repository() -> None:
    """
    Called by FastAPI on startup.

    Responsibilities:
    * Resolve and create the data directory.
    * Load + persist repository.json (applying defaults where needed).
    * Ensure the example package exists on disk.
    * Build the initial in-memory index.
    * Start a background task that rebuilds the index periodically.
    """
    global _repository_config, _INDEX_TASK

    # Ensure data directory exists.
    get_data_dir()

    # Load and persist repository config.
    _repository_config = _load_repository_config_from_disk()

    # Create / update the example package used to document structure.
    _ensure_example_package_exists()

    # Initial index build.
    build_index_from_disk()

    # Start periodic refresh if not already running.
    if _INDEX_TASK is None:
        _INDEX_TASK = asyncio.create_task(_periodic_rebuild_loop())


