from __future__ import annotations

from datetime import datetime
from typing import Dict, List, Optional

from pydantic import BaseModel, Field


class SourceAgreement(BaseModel):
    agreement_label: str
    agreement: str
    agreement_url: Optional[str] = None


class SourceAgreementsConfig(BaseModel):
    agreements_identifier: str = Field(
        default="agreements-v1",
        description="Identifier for the current set of source agreements.",
    )
    agreements: List[SourceAgreement] = Field(
        default_factory=list,
        description="List of agreements shown to the user when adding the source.",
    )


class AuthenticationConfig(BaseModel):
    authentication_type: str = Field(
        default="none",
        description="Authentication type for this REST source (none or microsoftEntraId).",
    )
    microsoft_entra_id_authentication_info: Optional[dict] = Field(
        default=None,
        description="Additional configuration when authentication_type is microsoftEntraId.",
    )


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
    # Additional fields used to populate the /information endpoint for the
    # WinGet REST source contract.
    source_agreements: Optional[SourceAgreementsConfig] = Field(
        default=None,
        description="Optional agreements that are presented to the user.",
    )
    server_supported_versions: List[str] = Field(
        default_factory=lambda: ["1.0.0"],
        description="WinGet REST API contract versions supported by this server.",
    )
    unsupported_package_match_fields: List[str] = Field(
        default_factory=lambda: ["NormalizedPackageNameAndPublisher"],
        description="Package match fields that this source does not support.",
    )
    required_package_match_fields: List[str] = Field(
        default_factory=list,
        description="Package match fields that this source requires.",
    )
    unsupported_query_parameters: List[str] = Field(
        default_factory=lambda: ["Market"],
        description="Query parameters that this source does not support.",
    )
    required_query_parameters: List[str] = Field(
        default_factory=list,
        description="Query parameters that this source requires.",
    )
    authentication: AuthenticationConfig = Field(
        default_factory=AuthenticationConfig,
        description="Authentication configuration for this REST source.",
    )
    # Option lists used by the admin UI to constrain and validate fields that
    # are effectively enums in the WinGet manifest contract. These are NOT
    # exposed to the WinGet client; they are internal repository configuration.
    architecture_options: List[str] = Field(
        default_factory=lambda: ["x86", "x64", "arm64"],
        description="Valid architectures for installers.",
    )
    scope_options: List[str] = Field(
        default_factory=lambda: ["user", "machine"],
        description="Valid scope values for installers.",
    )
    installer_type_options: List[str] = Field(
        default_factory=lambda: ["exe", "msi", "msix", "zip"],
        description="Valid installer types.",
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


