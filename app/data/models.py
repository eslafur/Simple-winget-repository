from __future__ import annotations

from datetime import datetime
from typing import Dict, List, Optional

from pydantic import BaseModel, Field


class RepositoryConfig(BaseModel):
    """
    Top-level configuration describing the repository itself.

    Persisted at: <DATA_DIR>/repository.json
    """

    source_identifier: str = Field(
        default="python-winget-repo",
        description="Unique identifier for this repository instance.",
    )
    display_name: str = Field(
        default="Python winget demo repository",
        description="Human-friendly name for this repository.",
    )
    description: str = Field(
        default="Local JSON-backed winget REST repository implemented with FastAPI.",
        description="Longer description used in documentation and /information.",
    )
    refresh_interval_seconds: int = Field(
        default=3600,
        ge=60,
        description="How often the in-memory index is rebuilt from disk.",
    )
    created_at: datetime = Field(
        default_factory=datetime.utcnow,
        description="Timestamp when this repository configuration was first created.",
    )


class PackageCommonMetadata(BaseModel):
    """
    Information that is shared across all versions of a package.
    Persisted in: <DATA_DIR>/<package_id>/package.json
    """

    package_identifier: str
    package_name: str
    publisher: str
    short_description: Optional[str] = None
    license: Optional[str] = None
    tags: List[str] = Field(default_factory=list)
    homepage: Optional[str] = None
    support_url: Optional[str] = None
    is_example: bool = Field(
        default=False,
        description="If true, this package is for documentation only and is ignored by indexing.",
    )


class VersionMetadata(BaseModel):
    """
    Information that is specific to a concrete version/architecture/scope.
    Persisted in: <DATA_DIR>/<package_id>/<version-arch-scope>/version.json
    """

    version: str
    architecture: str
    scope: str  # e.g. "user" or "machine"

    installer_type: str = "exe"
    installer_file: Optional[str] = Field(
        default=None,
        description="File name of the installer within the version directory.",
    )
    installer_sha256: Optional[str] = None
    silent_arguments: Optional[str] = None
    interactive_arguments: Optional[str] = None
    log_arguments: Optional[str] = None

    release_date: Optional[datetime] = None
    release_notes: Optional[str] = None

    # Relative path from the data directory to the folder containing this version's files.
    # This is populated by the indexer and excluded from JSON persistence so the on-disk
    # layout remains clean and focused on winget metadata.
    storage_path: Optional[str] = Field(
        default=None,
        exclude=True,
        description="Relative path to this version directory from the data directory.",
    )


class PackageIndex(BaseModel):
    """
    In-memory representation of a single package and all known versions.
    """

    package: PackageCommonMetadata
    versions: List[VersionMetadata] = Field(default_factory=list)
    # Optional: where this package is stored on disk relative to the data directory.
    # This makes the on-disk folder layout a detail instead of the logical key.
    storage_path: Optional[str] = Field(
        default=None,
        description="Relative path from the data directory to this package's folder.",
    )


class RepositoryIndex(BaseModel):
    """
    In-memory index for the entire repository, used to answer search queries quickly.
    """

    packages: Dict[str, PackageIndex] = Field(default_factory=dict)
    last_built_at: Optional[datetime] = None


