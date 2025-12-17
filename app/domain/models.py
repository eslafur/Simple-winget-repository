"""
Pydantic models for the winget repository.

This module defines all data models used throughout the application, including:
- Repository configuration and settings
- Package and version metadata
- Authentication and session management
- API request/response models

All models use Pydantic for validation, serialization, and type safety.
"""

from __future__ import annotations

from datetime import datetime
from typing import Dict, List, Optional, Literal

from pydantic import BaseModel, Field, ConfigDict


# ---------------------------------------------------------------------------
# Cache Configuration Models
# ---------------------------------------------------------------------------


class CacheSettings(BaseModel):
    """
    Configuration for cached packages imported from the WinGet index.
    
    Empty lists in filter fields mean "no filter" (include all). Used to
    control which versions, architectures, scopes, and installer types are
    imported when caching a package from the public WinGet repository.
    """
    
    architectures: List[str] = Field(
        default_factory=list,
        description="Filter by architectures (e.g., ['x64', 'x86']). Empty list means all.",
    )
    scopes: List[str] = Field(
        default_factory=list,
        description="Filter by installation scopes (e.g., ['user', 'machine']). Empty list means all.",
    )
    installer_types: List[str] = Field(
        default_factory=list,
        description="Filter by installer types (e.g., ['msi', 'exe']). Empty list means all.",
    )
    version_mode: str = Field(
        default="latest",
        description="Version import mode: 'latest' to import only the newest version, 'all' to import all versions.",
    )
    version_filter: Optional[str] = Field(
        default=None,
        description="Optional version wildcard filter (e.g., '1.*' to match 1.x versions).",
    )
    auto_update: bool = Field(
        default=True,
        description="If True, automatically update this cached package when the index refreshes.",
    )


# Type alias for installation scope values
InstallScope = Literal["user", "machine"]


# ---------------------------------------------------------------------------
# Corporate Deployment Models
# ---------------------------------------------------------------------------


class ADGroupScopeEntry(BaseModel):
    """
    Corporate deployment targeting rule for automatic package installation.
    
    When a client reports membership in the specified Active Directory group,
    the server may instruct it to install the package using the given scope.
    This enables enterprise-wide package deployment based on AD group membership.
    """

    ad_group: str = Field(
        description="Active Directory group name to match against client membership.",
    )
    scope: InstallScope = Field(
        description="Installation scope to use for clients in this AD group ('user' or 'machine').",
    )


# ---------------------------------------------------------------------------
# Repository Configuration Models
# ---------------------------------------------------------------------------


class SourceAgreement(BaseModel):
    """
    A single agreement/terms of service entry shown to users.
    
    Used in the WinGet REST source contract to present legal agreements
    or terms of service when users add this repository as a source.
    """
    
    agreement_label: str = Field(
        description="Short label/name for this agreement.",
    )
    agreement: str = Field(
        description="Full text of the agreement.",
    )
    agreement_url: Optional[str] = Field(
        default=None,
        description="Optional URL where users can view the full agreement online.",
    )


class SourceAgreementsConfig(BaseModel):
    """
    Configuration for source agreements shown to WinGet clients.
    
    This is part of the WinGet REST source contract and is used to present
    legal agreements or terms of service when users add this repository.
    """
    
    agreements_identifier: str = Field(
        default="agreements-v1",
        description="Version identifier for the current set of source agreements.",
    )
    agreements: List[SourceAgreement] = Field(
        default_factory=list,
        description="List of agreements to present to users when adding this source.",
    )


class AuthenticationConfig(BaseModel):
    """
    Authentication configuration for the WinGet REST source.
    
    Defines how clients authenticate with this repository. Currently supports
    'none' (no authentication) and 'microsoftEntraId' (Azure AD authentication).
    """
    
    authentication_type: str = Field(
        default="none",
        description="Authentication type: 'none' or 'microsoftEntraId'.",
    )
    microsoft_entra_id_authentication_info: Optional[dict] = Field(
        default=None,
        description="Additional Azure AD configuration when authentication_type is 'microsoftEntraId'.",
    )


class RepositoryConfig(BaseModel):
    """
    Top-level configuration for the winget repository.
    
    This model contains all repository-level settings including identification,
    WinGet REST API contract configuration, and admin UI constraints.
    
    Persisted at: <DATA_DIR>/repository.json
    """

    # Basic repository identification
    source_identifier: str = Field(
        default="python-winget-repo",
        description="Unique identifier for this repository instance (used in WinGet source URLs).",
    )
    display_name: str = Field(
        default="Python winget demo repository",
        description="Human-friendly name displayed to users and in documentation.",
    )
    description: str = Field(
        default="Local JSON-backed winget REST repository implemented with FastAPI.",
        description="Longer description used in documentation and the /information endpoint.",
    )
    refresh_interval_seconds: int = Field(
        default=3600,
        ge=60,
        description="How often (in seconds) the in-memory index is rebuilt from disk. Minimum: 60 seconds.",
    )
    created_at: datetime = Field(
        default_factory=datetime.utcnow,
        description="Timestamp when this repository configuration was first created.",
    )
    
    # WinGet REST API contract configuration
    # These fields populate the /information endpoint response
    source_agreements: Optional[SourceAgreementsConfig] = Field(
        default=None,
        description="Optional agreements/terms of service presented to users when adding this source.",
    )
    server_supported_versions: List[str] = Field(
        default_factory=lambda: ["1.0.0", "1.1.0", "1.4.0", "1.5.0", "1.6.0", "1.7.0", "1.9.0", "1.10.0", "1.12.0"],
        description="List of WinGet REST API contract versions supported by this server.",
    )
    unsupported_package_match_fields: List[str] = Field(
        default_factory=lambda: ["NormalizedPackageNameAndPublisher"],
        description="Package match fields that this source does not support (reported to WinGet clients).",
    )
    required_package_match_fields: List[str] = Field(
        default_factory=list,
        description="Package match fields that this source requires (reported to WinGet clients).",
    )
    unsupported_query_parameters: List[str] = Field(
        default_factory=lambda: ["Market"],
        description="Query parameters that this source does not support (reported to WinGet clients).",
    )
    required_query_parameters: List[str] = Field(
        default_factory=list,
        description="Query parameters that this source requires (reported to WinGet clients).",
    )
    authentication: AuthenticationConfig = Field(
        default_factory=AuthenticationConfig,
        description="Authentication configuration for this REST source.",
    )
    
    # Admin UI validation constraints
    # These option lists are used by the admin UI to constrain and validate fields
    # that are effectively enums in the WinGet manifest contract. They are NOT
    # exposed to WinGet clients; they are internal repository configuration.
    architecture_options: List[str] = Field(
        default_factory=lambda: ["x86", "x64", "arm64"],
        description="Valid architecture values for installers (used for admin UI validation).",
    )
    scope_options: List[str] = Field(
        default_factory=lambda: ["user", "machine"],
        description="Valid installation scope values (used for admin UI validation).",
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
        description="Valid installer type values (used for admin UI validation).",
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
        description="Valid NestedInstallerType values for zip installers (used for admin UI validation).",
    )


# ---------------------------------------------------------------------------
# Package Metadata Models
# ---------------------------------------------------------------------------


class PackageCommonMetadata(BaseModel):
    """
    Package-level metadata shared across all versions.
    
    This contains information that is consistent for all versions of a package,
    such as name, publisher, description, and corporate deployment settings.
    
    Persisted in: <DATA_DIR>/<package_id>/package.json
    """

    # Required package identification
    package_identifier: str = Field(
        description="Unique package identifier in format 'Publisher.PackageName'.",
    )
    package_name: str = Field(
        description="Display name of the package.",
    )
    publisher: str = Field(
        description="Publisher/author of the package.",
    )
    
    # Optional package information
    short_description: Optional[str] = Field(
        default=None,
        description="Brief description of the package.",
    )
    license: Optional[str] = Field(
        default=None,
        description="License type (e.g., 'MIT', 'Proprietary'). Defaults to 'Proprietary' if not specified.",
    )
    tags: List[str] = Field(
        default_factory=list,
        description="List of tags for categorizing and searching packages.",
    )
    homepage: Optional[str] = Field(
        default=None,
        description="URL to the package's homepage or website.",
    )
    support_url: Optional[str] = Field(
        default=None,
        description="URL for package support or help documentation.",
    )
    
    # Corporate deployment configuration
    ad_group_scopes: List[ADGroupScopeEntry] = Field(
        default_factory=list,
        description="Active Directory group-based auto-install targeting rules.",
    )
    
    # Package source and status
    cached: bool = Field(
        default=False,
        description="True if this package is cached from the public WinGet index, False if it's an owned package.",
    )
    cache_settings: Optional[CacheSettings] = Field(
        default=None,
        description="Cache configuration for cached packages (only used when cached=True).",
    )
    is_example: bool = Field(
        default=False,
        description="If True, this package is for documentation/examples only and is excluded from indexing.",
    )


class NestedInstallerFile(BaseModel):
    """
    Configuration for a nested installer file within a ZIP archive.
    
    When an installer type is 'zip', it may contain one or more nested installers
    that need to be extracted and executed. This model defines which files to
    extract and optionally create portable command aliases for them.
    
    This mirrors the WinGet manifest contract but uses snake_case for JSON persistence.
    """

    relative_file_path: str = Field(
        description="Path to the nested installer file relative to the archive root.",
    )
    portable_command_alias: Optional[str] = Field(
        default=None,
        description="Optional command alias for portable installers (e.g., 'myapp' for 'myapp.exe').",
    )


class CustomInstallerStep(BaseModel):
    """
    A single logical step in a server-generated custom installer script.
    
    Custom installers allow administrators to define custom installation logic
    that is executed via a generated install.bat script. Each step represents
    one action (e.g., extract, run, copy) with associated arguments.
    """

    action_type: str = Field(
        description="Type of action to perform (e.g., 'extract', 'run', 'copy').",
    )
    argument1: Optional[str] = Field(
        default=None,
        description="First argument for the action (context-dependent).",
    )
    argument2: Optional[str] = Field(
        default=None,
        description="Second argument for the action (context-dependent).",
    )


class VersionMetadata(BaseModel):
    """
    Version-specific metadata for a single installer.
    
    This model represents one specific installer for a package, identified by
    version, architecture, and scope. Multiple VersionMetadata entries can
    exist for the same package version (e.g., different architectures).
    
    Persisted in: <DATA_DIR>/<package_id>/<version-arch-scope>/version.json
    """

    # Version identification
    version: str = Field(
        description="Package version string (e.g., '1.2.3').",
    )
    architecture: str = Field(
        description="Target architecture (e.g., 'x64', 'x86', 'arm64').",
    )
    scope: Optional[str] = Field(
        default=None,
        description="Installation scope: 'user' for per-user installs, 'machine' for system-wide, or None for default.",
    )

    # Installer identification
    installer_guid: Optional[str] = Field(
        default=None,
        description="Unique GUID for this installer instance. Required when multiple installers exist for the same version/architecture.",
    )

    # Installer file information
    installer_type: str = Field(
        default="exe",
        description="Type of installer (e.g., 'exe', 'msi', 'zip', 'custom').",
    )
    installer_file: Optional[str] = Field(
        default=None,
        description="Filename of the installer within the version directory.",
    )
    installer_sha256: Optional[str] = Field(
        default=None,
        description="SHA256 hash of the installer file (or package.zip for custom installers).",
    )
    
    # Installer command-line arguments
    silent_arguments: Optional[str] = Field(
        default=None,
        description="Command-line arguments for silent/unattended installation.",
    )
    silent_with_progress_arguments: Optional[str] = Field(
        default=None,
        description="Command-line arguments for silent installation with progress UI. If not provided, silent_arguments is reused.",
    )
    interactive_arguments: Optional[str] = Field(
        default=None,
        description="Command-line arguments for interactive installation.",
    )
    log_arguments: Optional[str] = Field(
        default=None,
        description="Command-line arguments for logging (e.g., log file path).",
    )

    # WinGet manifest metadata
    product_code: Optional[str] = Field(
        default=None,
        description="Product code (GUID) for MSI-style installers. Required by WinGet for MSI installers but optional in model for backwards compatibility.",
    )

    # Installation mode flags
    # These control which installation modes are advertised in the WinGet manifest
    install_mode_interactive: bool = Field(
        default=True,
        description="If True, advertise interactive installation mode.",
    )
    install_mode_silent: bool = Field(
        default=True,
        description="If True, advertise silent installation mode.",
    )
    install_mode_silent_with_progress: bool = Field(
        default=True,
        description="If True, advertise silent installation with progress UI mode.",
    )

    # Security and dependencies
    requires_elevation: bool = Field(
        default=False,
        description="If True, this installer requires administrator/elevated privileges.",
    )
    package_dependencies: List[str] = Field(
        default_factory=list,
        description="List of package identifiers this version depends on (mapped to Dependencies.PackageDependencies in manifest).",
    )

    # Nested installer configuration (for ZIP installers)
    nested_installer_type: Optional[str] = Field(
        default=None,
        description="Type of nested installer within a ZIP archive (used when installer_type == 'zip').",
    )
    nested_installer_files: List[NestedInstallerFile] = Field(
        default_factory=list,
        description="List of nested installer files to extract from a ZIP archive.",
    )

    # Custom installer configuration
    # Custom installers use a generated install.bat script that executes the steps defined here.
    # The uploaded installer file is stored in installer_file, and a package.zip containing
    # install.bat + the installer is generated. WinGet downloads package.zip, and its hash
    # is stored in installer_sha256.
    custom_installer_steps: List[CustomInstallerStep] = Field(
        default_factory=list,
        description="Logical steps that will be rendered into install.bat for custom installers (used when installer_type == 'custom').",
    )

    # Release information
    release_date: Optional[datetime] = Field(
        default=None,
        description="Release date for this version.",
    )
    release_notes: Optional[str] = Field(
        default=None,
        description="Release notes or changelog for this version.",
    )

    # Internal storage path (not persisted to JSON)
    storage_path: Optional[str] = Field(
        default=None,
        exclude=True,
        description="Relative path to this version directory from the data directory. Populated by indexer, excluded from JSON persistence.",
    )


class PackageIndex(BaseModel):
    """
    In-memory representation of a single package with all its versions.
    
    This model combines package-level metadata with all version-specific
    metadata entries. It's used for efficient in-memory operations and
    search queries without needing to access the filesystem.
    """

    package: PackageCommonMetadata = Field(
        description="Package-level metadata shared across all versions.",
    )
    versions: List[VersionMetadata] = Field(
        default_factory=list,
        description="List of all version/architecture/scope combinations for this package.",
    )
    storage_path: Optional[str] = Field(
        default=None,
        description="Relative path from the data directory to this package's folder. Used to decouple logical package identity from on-disk layout.",
    )


class RepositoryIndex(BaseModel):
    """
    In-memory index for the entire repository.
    
    This is the main data structure used for fast search operations. It contains
    all packages and their versions in memory, allowing queries without filesystem
    access. The index is rebuilt periodically from disk according to the
    refresh_interval_seconds setting.
    """

    packages: Dict[str, PackageIndex] = Field(
        default_factory=dict,
        description="Dictionary mapping package identifiers to their PackageIndex entries.",
    )
    last_built_at: Optional[datetime] = Field(
        default=None,
        description="Timestamp when this index was last built from disk.",
    )


# ---------------------------------------------------------------------------
# Authentication models (authentication.json)
# ---------------------------------------------------------------------------


class AuthCredential(BaseModel):
    """
    A single credential entry for user authentication.
    
    Supports two credential types:
    - "cleartext": Password stored as plain text (will be normalized to SHA256 on first use)
    - "sha256": Password field contains the SHA256 hash, with optional per-user salt
    
    The system automatically migrates cleartext passwords to SHA256 hashes.
    """

    type: str = Field(
        description='Credential type: "cleartext" or "sha256".',
    )
    password: str = Field(
        description="Password value: plain text if type is 'cleartext', SHA256 hash if type is 'sha256'.",
    )
    salt: Optional[str] = Field(
        default=None,
        description="Per-user salt used for SHA256 hashing (only used when type == 'sha256').",
    )


class AuthUser(BaseModel):
    """
    User account entry in the authentication store.
    
    A user can have multiple credential entries, allowing for password rotation
    or multiple authentication methods.
    """

    username: str = Field(
        description="Unique username for this user account.",
    )
    authentications: List[AuthCredential] = Field(
        default_factory=list,
        description="List of credential entries for this user (supports multiple passwords for rotation).",
    )


class AuthSession(BaseModel):
    """
    Active session entry for authenticated users.
    
    Sessions track successful logins and are used to maintain authentication
    state. The field name uses a hyphen in JSON ("last-login") to match
    the API contract.
    """

    model_config = ConfigDict(populate_by_name=True)

    session_id: str = Field(
        description="Unique session identifier.",
    )
    last_login: datetime = Field(
        alias="last-login",
        serialization_alias="last-login",
        description="Timestamp of the last successful login for this session.",
    )
    username: str = Field(
        description="Username associated with this session.",
    )


class AuthenticationStore(BaseModel):
    """
    Root object for the authentication system.
    
    Contains all user accounts and active sessions. This is the complete
    authentication state persisted to disk.
    
    Persisted at: <DATA_DIR>/authentication.json
    """

    users: List[AuthUser] = Field(
        default_factory=list,
        description="List of all user accounts in the system.",
    )
    sessions: List[AuthSession] = Field(
        default_factory=list,
        description="List of all active authentication sessions.",
    )


# ---------------------------------------------------------------------------
# WinGet API Request Models
# ---------------------------------------------------------------------------
# These models match the WinGet REST API contract exactly (using PascalCase
# field names). They are used for parsing incoming API requests from WinGet clients.


class RequestMatch(BaseModel):
    """
    Search/match criteria for package queries and filters.
    
    Used in both Query (general search) and PackageMatchFilter (field-specific
    filtering) contexts. The MatchType determines how the keyword is matched
    (exact, case-insensitive, substring, etc.).
    """

    KeyWord: Optional[str] = Field(
        default=None,
        description="Search keyword to match against package fields.",
    )
    MatchType: Optional[str] = Field(
        default=None,
        description="Type of matching to perform (e.g., 'Exact', 'CaseInsensitive', 'Substring').",
    )


class PackageMatchFilter(BaseModel):
    """
    Field-specific filter for package search operations.
    
    Used in Inclusions (add packages matching any inclusion) and Filters
    (exclude packages that don't match all filters) lists in search requests.
    """

    PackageMatchField: str = Field(
        description="Field to match against (e.g., 'PackageName', 'Tag', 'ProductCode').",
    )
    Match: RequestMatch = Field(
        alias="RequestMatch",
        description="Match criteria (keyword and match type) to apply to the specified field.",
    )


class ManifestSearchRequest(BaseModel):
    """
    WinGet manifest search API request.
    
    This model represents the request body for the /manifestSearch endpoint.
    It supports complex search queries with keyword matching, inclusions,
    and filters.
    
    Search algorithm:
    1. Determine candidate packages from Query and Inclusions
    2. Apply Filters to narrow results (packages must match ALL filters)
    3. Return formatted results with version and product code information
    """

    MaximumResults: Optional[int] = Field(
        default=None,
        description="Maximum number of results to return (not currently enforced).",
    )
    FetchAllManifests: Optional[bool] = Field(
        default=None,
        description="If True, return all packages regardless of Query/Inclusions (overrides other search criteria).",
    )
    Query: Optional[RequestMatch] = Field(
        default=None,
        description="General keyword query to search across package identifier, name, publisher, and tags.",
    )
    Inclusions: List[PackageMatchFilter] = Field(
        default_factory=list,
        description="List of inclusion filters. Packages matching ANY inclusion are added to candidates.",
    )
    Filters: List[PackageMatchFilter] = Field(
        default_factory=list,
        description="List of exclusion filters. Packages must match ALL filters to be included in results.",
    )
