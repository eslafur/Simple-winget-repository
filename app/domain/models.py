from __future__ import annotations

from datetime import datetime
from typing import Dict, List, Optional, Literal

from pydantic import BaseModel, Field, ConfigDict

# Cache settings used for cached packages. Empty lists mean "no filter" (all).
class CacheSettings(BaseModel):
    architectures: List[str] = Field(default_factory=list)
    scopes: List[str] = Field(default_factory=list)
    installer_types: List[str] = Field(default_factory=list)
    version_mode: str = Field(default="latest", description="'latest' or 'all'")
    version_filter: Optional[str] = Field(default=None, description="Version wildcard filter")
    auto_update: bool = Field(default=True, description="Automatically update this cached package")


InstallScope = Literal["user", "machine"]


class ADGroupScopeEntry(BaseModel):
    """
    One corporate deployment targeting rule.

    If a client reports membership in ad_group, the server may instruct it to
    install the package using the given scope.
    """

    ad_group: str = Field(description="Active Directory group name to match against client membership.")
    scope: InstallScope = Field(description="Install scope ('user' or 'machine').")


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
        default_factory=lambda: ["1.0.0", "1.1.0", "1.4.0", "1.5.0", "1.6.0", "1.7.0", "1.9.0", "1.10.0", "1.12.0"],
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
        default_factory=lambda: [
            "msix",
            "msi",
            "appx",
            "exe",
            "zip",
            "inno",
            "nullsoft",
            "wix",
            "burn",
            "pwa",
            "portable",
            "font",
            "custom",
        ],
        description="Valid installer types.",
    )
    nested_installer_type_options: List[str] = Field(
        default_factory=lambda: [
            "msix",
            "msi",
            "appx",
            "exe",
            "inno",
            "nullsoft",
            "wix",
            "burn",
            "portable",
            "font",
        ],
        description="Valid NestedInstallerType values for zip installers.",
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
    ad_group_scopes: List[ADGroupScopeEntry] = Field(
        default_factory=list,
        description="Corporate auto-install targets: AD group name + desired install scope.",
    )
    cached: bool = Field(
        default=False,
        description="True if this package is a cached package pulled from the WinGet index.",
    )
    cache_settings: Optional[CacheSettings] = Field(
        default=None,
        description="Cache settings for cached packages (ignored for owned packages).",
    )
    is_example: bool = Field(
        default=False,
        description="If true, this package is for documentation only and is ignored by indexing.",
    )


class NestedInstallerFile(BaseModel):
    """
    Internal representation of a single NestedInstallerFile entry.

    This mirrors the WinGet contract but uses snake_case for JSON persistence.
    """

    relative_file_path: str
    portable_command_alias: Optional[str] = None


class CustomInstallerStep(BaseModel):
    """
    One logical step in a server-generated custom installer script.
    """

    action_type: str
    argument1: Optional[str] = None
    argument2: Optional[str] = None


class VersionMetadata(BaseModel):
    """
    Information that is specific to a concrete version/architecture/scope.
    Persisted in: <DATA_DIR>/<package_id>/<version-arch-scope>/version.json
    """

    version: str
    architecture: str
    scope: Optional[str] = None  # e.g. "user" or "machine"; can be None/absent

    # GUID to uniquely identify this installer instance (especially when multiple exist for same version/arch).
    # New installers should have this; legacy ones might not.
    installer_guid: Optional[str] = Field(default=None, description="Unique GUID for this installer.")

    installer_type: str = "exe"
    installer_file: Optional[str] = Field(
        default=None,
        description="File name of the installer within the version directory.",
    )
    installer_sha256: Optional[str] = None
    silent_arguments: Optional[str] = None
    # Optional separate arguments for the SilentWithProgress mode. If not
    # provided, silent_arguments will be reused when building the manifest.
    silent_with_progress_arguments: Optional[str] = None
    interactive_arguments: Optional[str] = None
    log_arguments: Optional[str] = None

    # Additional version-specific metadata used by WinGet but not previously
    # stored explicitly.
    #
    # ProductCode is required by WinGet for MSI-style installers, but we treat
    # it as optional in the data model for backwards compatibility. The admin
    # UI enforces it as required when editing/creating versions.
    product_code: Optional[str] = None

    # InstallModes are represented as three booleans which control the list of
    # modes emitted into the WinGet manifest. Defaults preserve the previous
    # behavior where all three modes were always advertised.
    install_mode_interactive: bool = True
    install_mode_silent: bool = True
    install_mode_silent_with_progress: bool = True

    # Whether this installer requires elevation/administrator rights.
    requires_elevation: bool = False

    # Logical package dependencies (mapped into Dependencies.PackageDependencies
    # in the WinGet manifest).
    package_dependencies: List[str] = Field(
        default_factory=list,
        description="List of package identifiers this version depends on.",
    )

    # Nested installer metadata (used when installer_type == 'zip').
    nested_installer_type: Optional[str] = None
    nested_installer_files: List[NestedInstallerFile] = Field(
        default_factory=list,
        description="List of nested installer files inside an archive installer.",
    )

    # Custom installer metadata (used when installer_type == 'custom').
    # The uploaded installer file name is stored in installer_file; the
    # generated package.zip (containing install.bat + installer_file) is
    # what WinGet will download, and its hash is stored in installer_sha256.
    custom_installer_steps: List[CustomInstallerStep] = Field(
        default_factory=list,
        description="Logical steps that will be rendered into install.bat.",
    )

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


# ---------------------------------------------------------------------------
# Authentication models (authentication.json)
# ---------------------------------------------------------------------------


class AuthCredential(BaseModel):
    """
    One credential entry for a user.

    type:
      - "cleartext": password stored as plain text (will be normalized to sha256)
      - "sha256": password field holds the SHA256 hash, salt holds the per-user salt
    """

    type: str = Field(
        description='Credential type ("cleartext" or "sha256").',
    )
    password: str = Field(
        description="Password value (clear text or SHA256 hash, depending on type).",
    )
    salt: Optional[str] = Field(
        default=None,
        description="Per-user salt used when type == 'sha256'.",
    )


class AuthUser(BaseModel):
    """
    User entry in authentication.json.
    """

    username: str
    authentications: List[AuthCredential] = Field(
        default_factory=list,
        description="List of credential entries for this user.",
    )


class AuthSession(BaseModel):
    """
    Session entry in authentication.json.
    """

    # Use alias "last-login" in JSON to match the requested field name.
    model_config = ConfigDict(populate_by_name=True)

    session_id: str
    last_login: datetime = Field(
        alias="last-login",
        serialization_alias="last-login",
        description="Timestamp of the last successful login for this session.",
    )
    username: str


class AuthenticationStore(BaseModel):
    """
    Root object persisted to <DATA_DIR>/authentication.json.

    Contains separate user and session lists.
    """

    users: List[AuthUser] = Field(default_factory=list)
    sessions: List[AuthSession] = Field(default_factory=list)

