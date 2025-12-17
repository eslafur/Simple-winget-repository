from typing import List, Optional, Dict, Any, Set
import hashlib
import logging
from datetime import datetime

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

class Package:
    def __init__(self, index: PackageIndex, db: DatabaseManager):
        self.index = index
        self.metadata = index.package
        self.versions = index.versions
        self.db = db

    @property
    def package_id(self) -> str:
        return self.metadata.package_identifier

    def get_installer_path(self, version: VersionMetadata) -> Path:
        """
        Get the path to the file that should be served for download.
        For custom installers, this is package.zip.
        For standard installers, it's the installer file itself.
        """
        base_path = self.db.get_file_path(self.package_id, version)
        
        if version.installer_type == "custom":
            # For custom installers, base_path points to the uploaded installer.
            # We want to serve package.zip which should be in the same folder.
            package_zip = base_path.parent / "package.zip"
            if package_zip.is_file():
                return package_zip
            
            # Fallback/Edge case: if package.zip doesn't exist but we expect it.
            # Logic in admin should ensure package.zip is created.
            # If not found, returning base_path is wrong if type is custom.
            # We'll return it but it might not be what the client expects (zip vs exe).
            # But strictly speaking, db.get_file_path returns the "installer_file".
        
        return base_path

    def compute_installer_sha256(self, version: VersionMetadata) -> Optional[str]:
        """
        Best-effort computation of the installer SHA256 for a given version
        from the on-disk installer file.
        """
        if not version.installer_file or not version.storage_path:
            return None

        try:
            # Use get_installer_path to hash the actual file served (e.g. package.zip for custom)
            file_path = self.get_installer_path(version)
            if not file_path.is_file():
                return None

            h = hashlib.sha256()
            with file_path.open("rb") as f:
                for chunk in iter(lambda: f.read(8192), b""):
                    h.update(chunk)
            return h.hexdigest()
        except Exception:
            return None

    def get_manifest(self, base_url: str) -> Dict[str, Any]:
        """
        Build the full manifest response for this package.
        """
        pkg = self.metadata
        
        # Group versions
        versions_by_version: Dict[str, List[VersionMetadata]] = {}
        for v in self.versions:
            versions_by_version.setdefault(v.version, []).append(v)

        version_entries: List[dict] = []
        for version_str, version_list in sorted(versions_by_version.items(), reverse=True):
            v0 = version_list[0] # Representative

            license_value = pkg.license or "Proprietary"
            short_description = pkg.short_description or f"{pkg.package_name} installer"

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
                "ReleaseNotes": v0.release_notes,
                "ReleaseNotesUrl": None,
                "Agreements": [],
                "PurchaseUrl": None,
                "InstallationNotes": None,
                "Documentations": [],
                "Icons": [],
                "Moniker": None,
            }

            installers: List[dict] = []
            for v in version_list:
                # Identifier construction must match routing expectations
                # Route: /packages/{package_id}/versions/{installer_id}/installer
                # Installer ID typically: <ver>-<arch>-<scope>[-<guid>]
                
                # Check how we construct this ID vs how we stored it?
                # The ID is generated here for the manifest.
                # If we have a GUID, we should append it to ensure uniqueness if needed, 
                # but the URL routing needs to match.
                
                parts = [v.version, v.architecture]
                if v.scope:
                    parts.append(v.scope)
                else:
                    parts.append("user") # default if None, though model says optional

                if v.installer_guid:
                    parts.append(v.installer_guid)
                
                installer_identifier = "-".join(parts)
                
                installer_url = f"{base_url}/winget/packages/{self.package_id}/versions/{installer_identifier}/installer"

                sha256 = v.installer_sha256 or self.compute_installer_sha256(v)
                if not sha256:
                    continue

                installer_type_value = v.installer_type
                nested_type = None
                nested_files: List[dict] = []

                if v.installer_type == "custom":
                    installer_type_value = "zip"
                    nested_type = "exe"
                    nested_files = [
                        {"RelativeFilePath": "install.bat", "PortableCommandAlias": None}
                    ]
                elif v.installer_type == "zip":
                    nested_type = getattr(v, "nested_installer_type", None)
                    nested_files_attr = getattr(v, "nested_installer_files", []) or []
                    for f in nested_files_attr:
                        nested_files.append({
                            "RelativeFilePath": f.relative_file_path,
                            "PortableCommandAlias": getattr(f, "portable_command_alias", None),
                        })

                install_modes: List[str] = []
                if getattr(v, "install_mode_interactive", True):
                    install_modes.append("interactive")
                if getattr(v, "install_mode_silent", True):
                    install_modes.append("silent")
                if getattr(v, "install_mode_silent_with_progress", True):
                    install_modes.append("silentWithProgress")

                package_deps: List[dict] = []
                for dep_id in getattr(v, "package_dependencies", []) or []:
                    package_deps.append({"PackageIdentifier": dep_id})

                elevation_requirement = "elevationRequired" if getattr(v, "requires_elevation", False) else "none"

                installers.append({
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
                })

            version_entries.append({
                "PackageVersion": version_str,
                "Channel": None,
                "DefaultLocale": default_locale,
                "Locales": [],
                "Installers": installers,
            })

        data = {
            "PackageIdentifier": self.package_id,
            "Versions": version_entries,
        }
        
        return strip_nulls(data)


class Repository:
    def __init__(self, db: DatabaseManager):
        self.db = db

    def get_package(self, package_id: str) -> Optional[Package]:
        idx = self.db.get_package(package_id)
        if idx:
            return Package(idx, self.db)
        return None

    def get_all_packages(self) -> List[Package]:
        indexes = self.db.get_all_packages()
        return [Package(idx, self.db) for idx in indexes]

    def search_packages(self, body: ManifestSearchRequest) -> List[Dict[str, Any]]:
        """
        Execute search and return list of result dicts suitable for manifestSearch response Data.
        """
        index = self.db.get_repository_index()
        all_ids = list(index.packages.keys())

        # Step 1: determine candidates
        if body.FetchAllManifests:
            candidate_ids = set(all_ids)
        else:
            candidate_ids: Set[str] = set()

            if body.Query and body.Query.KeyWord:
                for package_id, pkg_index in index.packages.items():
                    if self._package_matches_query(package_id, pkg_index, body.Query):
                        candidate_ids.add(package_id)

            for inc in body.Inclusions or []:
                for package_id, pkg_index in index.packages.items():
                    if self._package_matches_filter(package_id, pkg_index, inc):
                        candidate_ids.add(package_id)

            if not candidate_ids and not (body.Query and body.Query.KeyWord) and not body.Inclusions:
                candidate_ids = set(all_ids)

        # Step 2: apply Filters
        filtered_ids: List[str] = []
        for package_id in candidate_ids:
            pkg_index = index.packages.get(package_id)
            if not pkg_index:
                continue

            matches_all_filters = True
            for flt in body.Filters or []:
                if not self._package_matches_filter(package_id, pkg_index, flt):
                    matches_all_filters = False
                    break

            if matches_all_filters:
                filtered_ids.append(package_id)

        # Step 3: build response
        results: List[dict] = []
        for package_id in filtered_ids:
            pkg_index = index.packages.get(package_id)
            if not pkg_index:
                continue
            pkg = pkg_index.package

            version_strings = sorted(
                {v.version for v in pkg_index.versions if v.version},
                reverse=True,
            )

            versions_payload: List[dict] = []
            for ver in version_strings:
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
        pkg = pkg_index.package
        field = field or ""

        if field == "PackageIdentifier":
            return [package_id]
        if field == "PackageName":
            return [pkg.package_name] if pkg.package_name else []
        if field == "Tag":
            return pkg.tags or []
        if field == "ProductCode":
            vals: List[str] = []
            for v in pkg_index.versions:
                code = getattr(v, "product_code", None)
                if code:
                    vals.append(code)
            return vals
        return []

    def _package_matches_filter(self, package_id: str, pkg_index: PackageIndex, flt: PackageMatchFilter) -> bool:
        if not flt or not flt.RequestMatch:
            return True
        keyword = flt.RequestMatch.KeyWord or ""
        match_type = flt.RequestMatch.MatchType
        values = self._values_for_field(flt.PackageMatchField, package_id, pkg_index)
        for v in values:
            if match_text(str(v), keyword, match_type):
                return True
        return False

    def _package_matches_query(self, package_id: str, pkg_index: PackageIndex, query: RequestMatch) -> bool:
        if not query or not query.KeyWord:
            return False
        keyword = query.KeyWord
        match_type = query.MatchType
        pkg = pkg_index.package
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

