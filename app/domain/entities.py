"""
Domain entities for the winget repository.

This module provides the core business logic entities for managing packages,
installers, and repository operations. It wraps database models with higher-level
functionality for manifest generation, file handling, and search operations.
"""

from typing import List, Optional, Dict, Any, Set
import hashlib
import logging
from pathlib import Path

from app.storage.db_manager import DatabaseManager
from app.domain.models import (
    PackageIndex, 
    VersionMetadata, 
    ManifestSearchRequest,
    PackageMatchFilter,
    RequestMatch
)
from app.domain.winget_utils import match_text, strip_nulls

logger = logging.getLogger(__name__)


class Installer:
    """
    Represents a single installer for a specific version, architecture, and scope.
    
    This class wraps a VersionMetadata entry and provides additional logic for
    file handling, SHA256 computation, and manifest generation. It handles both
    standard installers and custom installers (which use package.zip).
    
    Attributes:
        metadata: The version metadata containing installer information.
        package_id: The package identifier this installer belongs to.
        db: Database manager for file path resolution.
    """
    
    def __init__(self, metadata: VersionMetadata, package_id: str, db: DatabaseManager):
        """
        Initialize an Installer instance.
        
        Args:
            metadata: Version metadata containing installer details.
            package_id: Package identifier (e.g., "Publisher.PackageName").
            db: Database manager instance for file operations.
        """
        self.metadata = metadata
        self.package_id = package_id
        self.db = db

    @property
    def version(self) -> str:
        """Get the version string for this installer."""
        return self.metadata.version

    @property
    def architecture(self) -> str:
        """Get the target architecture (e.g., 'x64', 'x86', 'arm64')."""
        return self.metadata.architecture

    @property
    def scope(self) -> Optional[str]:
        """Get the installation scope ('user' or 'machine'), if specified."""
        return self.metadata.scope
    
    @property
    def installer_guid(self) -> Optional[str]:
        """Get the unique installer GUID identifier."""
        return self.metadata.installer_guid

    @property
    def installer_type(self) -> str:
        """Get the installer type (e.g., 'exe', 'msi', 'zip', 'custom')."""
        return self.metadata.installer_type
    
    def get_file_path(self) -> Path:
        """
        Get the path to the file that should be served for download.
        
        For custom installers, returns the path to package.zip (which contains
        the custom installation script). For standard installers, returns the
        path to the installer file itself.
        
        Returns:
            Path to the file that should be served for this installer.
        """
        base_path = self.db.get_file_path(self.package_id, self.metadata)
        
        if self.metadata.installer_type == "custom":
            # Custom installers are packaged as ZIP files containing install.bat
            # The actual file to serve is package.zip, not the uploaded installer
            package_zip = base_path.parent / "package.zip"
            if package_zip.is_file():
                return package_zip
            # If package.zip doesn't exist, fall back to base_path
            # (error handling is done at the API level)
        
        return base_path

    def compute_sha256(self) -> Optional[str]:
        """
        Compute the SHA256 hash of the installer file.
        
        This method computes the hash from the actual file that will be served
        (which may differ from the stored file for custom installers). The hash
        is computed in chunks to handle large files efficiently.
        
        Returns:
            SHA256 hash as a hexadecimal string, or None if computation fails
            or the file is not available.
        """
        if not self.metadata.installer_file or not self.metadata.storage_path:
            return None

        try:
            file_path = self.get_file_path()
            if not file_path.is_file():
                return None

            h = hashlib.sha256()
            with file_path.open("rb") as f:
                # Read in 8KB chunks to handle large files efficiently
                for chunk in iter(lambda: f.read(8192), b""):
                    h.update(chunk)
            return h.hexdigest()
        except Exception:
            logger.exception(f"Failed to compute SHA256 for installer {self.installer_guid}")
            return None

    def get_manifest_snippet(self, base_url: str) -> Dict[str, Any]:
        """
        Generate the installer manifest snippet for winget manifest format.
        
        This method creates a dictionary representing a single installer entry
        in the 'Installers' array of a winget manifest. It includes all required
        fields such as URLs, hashes, switches, dependencies, etc.
        
        Args:
            base_url: Base URL for constructing installer download URLs.
            
        Returns:
            Dictionary containing installer manifest data, or empty dict if
            SHA256 cannot be computed (which would make the installer invalid).
        """
        v = self.metadata
        
        installer_identifier = v.installer_guid
        installer_url = f"{base_url}/winget/packages/{self.package_id}/versions/{installer_identifier}/installer"

        # SHA256 is required for manifest validity
        sha256 = v.installer_sha256 or self.compute_sha256()
        if not sha256:
            return {}

        # Handle installer type and nested installer configuration
        installer_type_value = v.installer_type
        nested_type = None
        nested_files: List[dict] = []

        if v.installer_type == "custom":
            # Custom installers are packaged as ZIP files containing install.bat
            installer_type_value = "zip"
            nested_type = "exe"
            nested_files = [
                {"RelativeFilePath": "install.bat", "PortableCommandAlias": None}
            ]
        elif v.installer_type == "zip":
            # ZIP installers may contain nested installers
            nested_type = getattr(v, "nested_installer_type", None)
            nested_files_attr = getattr(v, "nested_installer_files", []) or []
            for f in nested_files_attr:
                nested_files.append({
                    "RelativeFilePath": f.relative_file_path,
                    "PortableCommandAlias": getattr(f, "portable_command_alias", None),
                })

        # Build list of supported installation modes
        install_modes: List[str] = []
        if getattr(v, "install_mode_interactive", True):
            install_modes.append("interactive")
        if getattr(v, "install_mode_silent", True):
            install_modes.append("silent")
        if getattr(v, "install_mode_silent_with_progress", True):
            install_modes.append("silentWithProgress")

        # Build package dependencies list
        package_deps: List[dict] = []
        for dep_id in getattr(v, "package_dependencies", []) or []:
            package_deps.append({"PackageIdentifier": dep_id})

        # Determine elevation requirement
        elevation_requirement = "elevationRequired" if getattr(v, "requires_elevation", False) else "none"

        return {
            "InstallerIdentifier": installer_identifier,
            "InstallerSha256": sha256,
            "InstallerUrl": installer_url,
            "Architecture": v.architecture,
            "InstallerLocale": "en-US",
            "Platform": ["Windows.Desktop"],
            "MinimumOSVersion": "10.0.0.0",
            "InstallerType": installer_type_value,
            "Scope": v.scope,
            "SignatureSha256": None,
            "InstallModes": install_modes,
            "InstallerSwitches": {
                "Silent": v.silent_arguments,
                "SilentWithProgress": getattr(v, "silent_with_progress_arguments", None) or v.silent_arguments,
                "Interactive": v.interactive_arguments,
                "InstallLocation": None,
                "Log": v.log_arguments,
                "Upgrade": None,
                "Custom": None,
                "Repair": None,
            },
            "InstallerSuccessCodes": [],
            "ExpectedReturnCodes": [],
            "UpgradeBehavior": "install",
            "Commands": [],
            "Protocols": [],
            "FileExtensions": [],
            "Dependencies": {
                "WindowsFeatures": [],
                "WindowsLibraries": [],
                "PackageDependencies": package_deps,
                "ExternalDependencies": [],
            },
            "PackageFamilyName": None,
            "ProductCode": getattr(v, "product_code", None),
            "Capabilities": [],
            "RestrictedCapabilities": [],
            "MSStoreProductIdentifier": None,
            "InstallerAbortsTerminal": False,
            "ReleaseDate": v.release_date.date().isoformat() if v.release_date else None,
            "InstallLocationRequired": False,
            "RequireExplicitUpgrade": False,
            "ElevationRequirement": elevation_requirement,
            "UnsupportedOSArchitectures": [],
            "AppsAndFeaturesEntries": [],
            "Markets": None,
            "NestedInstallerType": nested_type,
            "NestedInstallerFiles": nested_files,
            "DisplayInstallWarnings": False,
            "UnsupportedArguments": [],
            "InstallationMetadata": {
                "DefaultInstallLocation": None,
                "Files": [],
            },
            "DownloadCommandProhibited": False,
            "RepairBehavior": "installer",
            "ArchiveBinariesDependOnPath": False,
            "Authentication": {
                "AuthenticationType": "none",
                "MicrosoftEntraIdAuthenticationInfo": None,
            },
        }


class Package:
    """
    Represents a package with all its versions and installers.
    
    A package contains multiple versions, and each version may have multiple
    installers (for different architectures/scopes). This class provides
    functionality to group installers by version and generate complete
    winget manifest responses.
    
    Attributes:
        index: Package index containing package metadata and versions.
        metadata: Package-level metadata (name, publisher, etc.).
        db: Database manager for file operations.
    """
    
    def __init__(self, index: PackageIndex, db: DatabaseManager):
        """
        Initialize a Package instance.
        
        Args:
            index: Package index containing package and version metadata.
            db: Database manager instance for file operations.
        """
        self.index = index
        self.metadata = index.package
        # Create Installer entities for each version entry
        self._installers = [Installer(v, self.metadata.package_identifier, db) for v in index.versions]
        self.db = db

    @property
    def package_id(self) -> str:
        """Get the package identifier (e.g., 'Publisher.PackageName')."""
        return self.metadata.package_identifier

    @property
    def versions(self) -> List[VersionMetadata]:
        """
        Get raw version metadata list.
        
        This property is maintained for compatibility with the admin interface
        which may need direct access to version metadata.
        
        Returns:
            List of VersionMetadata objects for all versions in this package.
        """
        return self.index.versions

    @property
    def installers(self) -> List[Installer]:
        """
        Get all installer entities for this package.
        
        Returns:
            List of Installer objects for all versions/architectures/scopes.
        """
        return self._installers
    
    def get_versions_grouped(self) -> Dict[str, List[Installer]]:
        """
        Group installers by version string.
        
        Returns:
            Dictionary mapping version strings to lists of Installer objects
            for that version. Multiple installers per version are common when
            different architectures or scopes are supported.
        """
        groups: Dict[str, List[Installer]] = {}
        for inst in self._installers:
            groups.setdefault(inst.version, []).append(inst)
        return groups

    def get_installer_path(self, installer_id: str) -> Path:
        """
        Find an installer by GUID and return its file path.
        
        Args:
            installer_id: The installer GUID to search for.
            
        Returns:
            Path to the installer file.
            
        Raises:
            ValueError: If no installer with the given GUID is found.
        """
        for inst in self._installers:
            if inst.installer_guid == installer_id:
                return inst.get_file_path()
        raise ValueError(f"Installer with GUID {installer_id} not found in package {self.package_id}")

    def get_manifest(self, base_url: str) -> Dict[str, Any]:
        """
        Build the complete winget manifest response for this package.
        
        This method generates a manifest in the winget manifest format,
        including all versions and their installers. Versions are sorted
        in descending order (newest first).
        
        Args:
            base_url: Base URL for constructing installer download URLs.
            
        Returns:
            Dictionary containing the complete manifest structure with
            PackageIdentifier and Versions array. Null values are stripped
            to match winget manifest format requirements.
        """
        pkg = self.metadata
        
        # Group installers by version string
        versions_by_version = self.get_versions_grouped()
        
        version_entries: List[dict] = []
        # Process versions in descending order (newest first)
        for version_str, installer_list in sorted(versions_by_version.items(), reverse=True):
            if not installer_list:
                continue
                
            # Use the first installer in the group as representative for version-level metadata
            # (e.g., release notes that are version-specific)
            representative = installer_list[0].metadata

            # Set defaults for required fields
            license_value = pkg.license or "Proprietary"
            short_description = pkg.short_description or f"{pkg.package_name} installer"

            # Build default locale entry (winget requires at least one locale)
            default_locale = {
                "PackageLocale": "en-US",
                "Publisher": pkg.publisher,
                "PublisherUrl": pkg.homepage,
                "PublisherSupportUrl": pkg.support_url,
                "PrivacyUrl": None,
                "Author": pkg.publisher,
                "PackageName": pkg.package_name,
                "PackageUrl": pkg.homepage,
                "License": license_value,
                "LicenseUrl": None,
                "Copyright": None,
                "CopyrightUrl": None,
                "ShortDescription": short_description,
                "Description": None,
                "Tags": pkg.tags or None,
                "ReleaseNotes": representative.release_notes,
                "ReleaseNotesUrl": None,
                "Agreements": [],
                "PurchaseUrl": None,
                "InstallationNotes": None,
                "Documentations": [],
                "Icons": [],
                "Moniker": None,
            }

            # Build installer entries for this version
            installers_data: List[dict] = []
            for inst in installer_list:
                snippet = inst.get_manifest_snippet(base_url)
                if snippet:
                    installers_data.append(snippet)
            
            # Skip versions with no valid installers (e.g., missing SHA256)
            if not installers_data:
                continue

            # Add version entry to manifest
            version_entries.append({
                "PackageVersion": version_str,
                "Channel": None,
                "DefaultLocale": default_locale,
                "Locales": [],
                "Installers": installers_data,
            })

        # Build final manifest structure
        data = {
            "PackageIdentifier": self.package_id,
            "Versions": version_entries,
        }
        
        # Remove null values to match winget manifest format
        return strip_nulls(data)


class Repository:
    """
    Repository manager for package operations.
    
    This class provides high-level operations for retrieving and searching
    packages in the repository. It handles the search logic including
    query matching, filtering, and result formatting for winget API responses.
    
    Attributes:
        db: Database manager for accessing package data.
    """
    
    def __init__(self, db: DatabaseManager):
        """
        Initialize a Repository instance.
        
        Args:
            db: Database manager instance for package data access.
        """
        self.db = db

    def get_package(self, package_id: str) -> Optional[Package]:
        """
        Retrieve a package by its identifier.
        
        Args:
            package_id: Package identifier (e.g., 'Publisher.PackageName').
            
        Returns:
            Package instance if found, None otherwise.
        """
        idx = self.db.get_package(package_id)
        if idx:
            return Package(idx, self.db)
        return None

    def get_all_packages(self) -> List[Package]:
        """
        Retrieve all packages in the repository.
        
        Returns:
            List of all Package instances in the repository.
        """
        indexes = self.db.get_all_packages()
        return [Package(idx, self.db) for idx in indexes]

    def search_packages(self, body: ManifestSearchRequest) -> List[Dict[str, Any]]:
        """
        Execute package search and return formatted results.
        
        This method implements the winget manifest search API logic:
        1. Determine candidate packages based on Query and Inclusions
        2. Apply Filters to narrow down results
        3. Format results for manifestSearch API response
        
        Args:
            body: Search request containing query, filters, and inclusions.
            
        Returns:
            List of dictionaries suitable for manifestSearch response Data field.
            Each dictionary contains PackageIdentifier, PackageName, Publisher,
            and Versions array with version and product code information.
        """
        index = self.db.get_repository_index()
        all_ids = list(index.packages.keys())

        # Step 1: Determine candidate packages based on Query and Inclusions
        if body.FetchAllManifests:
            # FetchAllManifests overrides all other search criteria
            candidate_ids = set(all_ids)
        else:
            candidate_ids: Set[str] = set()

            # Apply keyword query if provided
            if body.Query and body.Query.KeyWord:
                for package_id, pkg_index in index.packages.items():
                    if self._package_matches_query(package_id, pkg_index, body.Query):
                        candidate_ids.add(package_id)

            # Apply inclusion filters (packages matching any inclusion are added)
            for inc in body.Inclusions or []:
                for package_id, pkg_index in index.packages.items():
                    if self._package_matches_filter(package_id, pkg_index, inc):
                        candidate_ids.add(package_id)

            # If no search criteria provided, return all packages
            if not candidate_ids and not (body.Query and body.Query.KeyWord) and not body.Inclusions:
                candidate_ids = set(all_ids)

        # Step 2: Apply exclusion Filters (packages must match ALL filters)
        filtered_ids: List[str] = []
        for package_id in candidate_ids:
            pkg_index = index.packages.get(package_id)
            if not pkg_index:
                continue

            # Package must match all filters to be included
            matches_all_filters = True
            for flt in body.Filters or []:
                if not self._package_matches_filter(package_id, pkg_index, flt):
                    matches_all_filters = False
                    break

            if matches_all_filters:
                filtered_ids.append(package_id)

        # Step 3: Build formatted response for manifestSearch API
        results: List[dict] = []
        for package_id in filtered_ids:
            pkg_index = index.packages.get(package_id)
            if not pkg_index:
                continue
            pkg = pkg_index.package

            # Collect unique version strings, sorted newest first
            version_strings = sorted(
                {v.version for v in pkg_index.versions if v.version},
                reverse=True,
            )

            # Build version payloads with product codes
            versions_payload: List[dict] = []
            for ver in version_strings:
                # Collect product codes for this version (may be multiple per version)
                product_codes = sorted(
                    {
                        v.product_code
                        for v in pkg_index.versions
                        if v.version == ver and getattr(v, "product_code", None)
                    }
                )
                versions_payload.append({
                    "PackageVersion": ver,
                    "Channel": None,
                    "PackageFamilyNames": [],
                    "ProductCodes": product_codes,
                    "AppsAndFeaturesEntryVersions": [],
                    "UpgradeCodes": [],
                })

            # Skip packages with no valid versions
            if not versions_payload:
                continue

            results.append({
                "PackageIdentifier": package_id,
                "PackageName": pkg.package_name,
                "Publisher": pkg.publisher,
                "Versions": versions_payload,
            })
            
        return results

    def _values_for_field(self, field: str, package_id: str, pkg_index: PackageIndex) -> List[str]:
        """
        Extract searchable values for a given field from a package.
        
        This helper method returns all values that can be matched against
        for a specific field type. Used by filter matching logic.
        
        Args:
            field: Field name to extract values for (e.g., 'PackageName', 'Tag').
            package_id: Package identifier.
            pkg_index: Package index containing package and version data.
            
        Returns:
            List of string values for the specified field. Empty list if field
            is not supported or has no values.
        """
        pkg = pkg_index.package
        field = field or ""

        if field == "PackageIdentifier":
            return [package_id]
        if field == "PackageName":
            return [pkg.package_name] if pkg.package_name else []
        if field == "Tag":
            return pkg.tags or []
        if field == "ProductCode":
            # Product codes are version-specific, collect from all versions
            vals: List[str] = []
            for v in pkg_index.versions:
                code = getattr(v, "product_code", None)
                if code:
                    vals.append(code)
            return vals
        return []

    def _package_matches_filter(self, package_id: str, pkg_index: PackageIndex, flt: PackageMatchFilter) -> bool:
        """
        Check if a package matches a filter criteria.
        
        A package matches if any value in the specified field matches the
        filter's keyword according to the match type (exact, case-insensitive, etc.).
        
        Args:
            package_id: Package identifier.
            pkg_index: Package index containing package data.
            flt: Filter containing field, keyword, and match type.
            
        Returns:
            True if package matches the filter, False otherwise.
            Returns True if filter is empty/invalid (matches everything).
        """
        if not flt or not flt.Match:
            return True
        keyword = flt.Match.KeyWord or ""
        match_type = flt.Match.MatchType
        values = self._values_for_field(flt.PackageMatchField, package_id, pkg_index)
        for v in values:
            if match_text(str(v), keyword, match_type):
                return True
        return False

    def _package_matches_query(self, package_id: str, pkg_index: PackageIndex, query: RequestMatch) -> bool:
        """
        Check if a package matches a search query.
        
        A package matches if the keyword matches any of: package identifier,
        package name, publisher, or any tag. Uses the specified match type.
        
        Args:
            package_id: Package identifier.
            pkg_index: Package index containing package data.
            query: Query containing keyword and match type.
            
        Returns:
            True if package matches the query, False otherwise.
            Returns False if query is empty/invalid.
        """
        if not query or not query.KeyWord:
            return False
        keyword = query.KeyWord
        match_type = query.MatchType
        pkg = pkg_index.package
        # Search across multiple fields: ID, name, publisher, and tags
        candidates = [
            package_id,
            pkg.package_name or "",
            pkg.publisher or "",
            *(pkg.tags or []),
        ]
        for value in candidates:
            if match_text(value, keyword, match_type):
                return True
        return False
