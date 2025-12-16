"""
Import packages from the official WinGet repository into the local repository.
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import logging
from pathlib import Path
from typing import Optional, List, Dict, Any
import httpx
import yaml
import aiofiles

logger = logging.getLogger(__name__)

from app.data.models import (
    PackageCommonMetadata,
    VersionMetadata,
)
from app.data.repository import get_data_dir
from app.data.winget_index import WinGetIndexReader
from app.data.cached_packages import (
    get_cached_packages_store,
    CachedPackage,
)
from app.data.models import CacheSettings


WINGET_BASE_URL = "https://cdn.winget.microsoft.com/cache"


class ManifestDownloader:
    """Downloads and parses WinGet manifest files."""
    
    def __init__(self, base_url: str = WINGET_BASE_URL):
        self.base_url = base_url

    async def download_manifest_with_text(
        self,
        relative_path: str,
        expected_hash: Optional[str] = None,
    ) -> tuple[Dict[str, Any], str, str]:
        """
        Download a manifest file, verify its hash (if provided), and return both
        the parsed YAML and the original raw YAML text.

        Returns:
            (manifest_dict, raw_yaml_text, sha256_hex_of_text)
        """
        manifest_url = f"{self.base_url}/{relative_path}"
        logger.debug(f"Downloading manifest from {manifest_url}")

        async with httpx.AsyncClient() as client:
            response = await client.get(manifest_url)
            response.raise_for_status()
            content = response.text

        actual_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()

        # Verify hash if provided
        if expected_hash:
            if actual_hash.lower() != expected_hash.lower():
                logger.error(
                    f"Manifest hash mismatch for {relative_path}: "
                    f"expected {expected_hash}, got {actual_hash}"
                )
                raise ValueError(
                    f"Manifest hash mismatch: expected {expected_hash}, got {actual_hash}"
                )
            logger.debug(f"Manifest hash verified for {relative_path}")

        # Parse YAML
        try:
            manifest = yaml.safe_load(content)
            logger.debug(f"Successfully parsed manifest from {relative_path}")
            return manifest, content, actual_hash
        except Exception as e:
            logger.error(
                f"Failed to parse YAML manifest from {relative_path}: {e}",
                exc_info=True,
            )
            raise
    
    async def download_manifest(
        self,
        relative_path: str,
        expected_hash: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Download a manifest file and parse it as YAML.
        
        Args:
            relative_path: Relative path to manifest (e.g., "manifests/m/Microsoft/WindowsTerminal/1.18.2632.0/Microsoft.WindowsTerminal.yaml")
            expected_hash: Expected SHA256 hash for verification
        
        Returns:
            Parsed manifest dictionary
        """
        manifest_url = f"{self.base_url}/{relative_path}"
        logger.debug(f"Downloading manifest from {manifest_url}")
        
        async with httpx.AsyncClient() as client:
            response = await client.get(manifest_url)
            response.raise_for_status()
            content = response.text
        
        # Verify hash if provided
        if expected_hash:
            actual_hash = hashlib.sha256(content.encode('utf-8')).hexdigest()
            if actual_hash.lower() != expected_hash.lower():
                logger.error(
                    f"Manifest hash mismatch for {relative_path}: "
                    f"expected {expected_hash}, got {actual_hash}"
                )
                raise ValueError(f"Manifest hash mismatch: expected {expected_hash}, got {actual_hash}")
            logger.debug(f"Manifest hash verified for {relative_path}")
        
        # Parse YAML
        try:
            manifest = yaml.safe_load(content)
            logger.debug(f"Successfully parsed manifest from {relative_path}")
            return manifest
        except Exception as e:
            logger.error(f"Failed to parse YAML manifest from {relative_path}: {e}", exc_info=True)
            raise
    
    def extract_installer_info(
        self,
        manifest: Dict[str, Any],
        architecture: Optional[List[str]] = None,
        scope: Optional[List[str]] = None,
        installer_types: Optional[List[str]] = None
    ) -> List[Dict[str, Any]]:
        """
        Extract installer information from a manifest.
        
        Args:
            manifest: Parsed manifest dictionary
            architecture: Filter by architecture (list)
            scope: Filter by scope (list)
            installer_types: Filter by installer types (empty = all)
        
        Returns:
            List of installer dictionaries with URL, hash, architecture, scope, etc.
        """
        installers = []
        
        # Handle both single version and multi-version manifests
        versions = manifest.get("Versions", [])
        if not versions:
            # Single version manifest - check if Installers exists at root
            if "Installers" in manifest:
                versions = [manifest]
        
        for version_entry in versions:
            version_installers = version_entry.get("Installers", [])
            version_str = version_entry.get("PackageVersion") or manifest.get("PackageVersion")
            
            logger.debug(
                f"Processing version {version_str} with {len(version_installers)} installers "
                f"(filters: arch={architecture}, scope={scope}, types={installer_types})"
            )
            
            # Get scope and installer type from version entry or manifest root (fallback)
            # WinGet default: if Scope is omitted, treat it as "user".
            version_scope = (version_entry.get("Scope") or manifest.get("Scope") or "user").lower()
            version_installer_type = (version_entry.get("InstallerType") or manifest.get("InstallerType") or "").lower()
            
            logger.debug(
                f"Version-level defaults: scope={version_scope}, installer_type={version_installer_type}"
            )
            
            for installer in version_installers:
                inst_arch = installer.get("Architecture", "").lower()
                # Scope and InstallerType can be at installer level, version level, or manifest root
                # WinGet default: if Scope is omitted, treat it as "user".
                inst_scope = (installer.get("Scope") or version_scope or "user").lower()
                inst_type = (installer.get("InstallerType") or version_installer_type).lower()
                
                logger.debug(
                    f"Processing installer: arch={inst_arch}, scope={inst_scope}, "
                    f"type={inst_type}, url={installer.get('InstallerUrl', 'N/A')}"
                )
                
                # Apply filters
                arch_list = [a.lower() for a in architecture] if architecture else []
                scope_list = [s.lower() for s in scope] if scope else []
                type_list = [it.lower() for it in installer_types] if installer_types else []
                
                if arch_list and inst_arch not in arch_list:
                    logger.debug(f"Skipping installer: architecture {inst_arch} not in {arch_list}")
                    continue
                if scope_list and inst_scope not in scope_list:
                    logger.debug(f"Skipping installer: scope {inst_scope} not in {scope_list}")
                    continue
                if type_list and inst_type not in type_list:
                    logger.debug(f"Skipping installer: type {inst_type} not in {type_list}")
                    continue
                
                logger.debug(f"Installer passed all filters: {installer.get('InstallerUrl', 'N/A')}")
                
                installer_info = {
                    "url": installer.get("InstallerUrl"),
                    "sha256": installer.get("InstallerSha256"),
                    "architecture": installer.get("Architecture"),
                    # WinGet default: if Scope is omitted, treat it as "user".
                    "scope": installer.get("Scope") or version_entry.get("Scope") or manifest.get("Scope") or "user",
                    "installer_type": installer.get("InstallerType") or version_entry.get("InstallerType") or manifest.get("InstallerType"),
                    "silent_arguments": installer.get("InstallerSwitches", {}).get("Silent"),
                    "interactive_arguments": installer.get("InstallerSwitches", {}).get("Interactive"),
                    "log_arguments": installer.get("InstallerSwitches", {}).get("Log"),
                    "product_code": installer.get("ProductCode"),
                    "requires_elevation": installer.get("ElevationRequirement") == "elevationRequired",
                    "version": version_str,
                }
                
                if installer_info["url"]:
                    installers.append(installer_info)
        
        return installers


class InstallerDownloader:
    """Downloads installer files."""
    
    async def download_installer(
        self,
        url: str,
        target_path: Path,
        expected_hash: Optional[str] = None
    ) -> str:
        """
        Download an installer file.
        
        Args:
            url: URL to download from
            target_path: Path to save the file
            expected_hash: Expected SHA256 hash for verification
        
        Returns:
            Actual SHA256 hash of downloaded file
        """
        logger.debug(f"Downloading installer from {url} to {target_path}")
        target_path.parent.mkdir(parents=True, exist_ok=True)
        
        hasher = hashlib.sha256()
        # Configure httpx to follow redirects (GitHub releases use redirects)
        async with httpx.AsyncClient(follow_redirects=True, timeout=60.0) as client:
            async with client.stream("GET", url) as response:
                response.raise_for_status()
                
                logger.debug(f"Downloading installer from {response.url} (redirected from {url})")
                
                async with aiofiles.open(target_path, "wb") as f:
                    async for chunk in response.aiter_bytes():
                        hasher.update(chunk)
                        await f.write(chunk)
        
        actual_hash = hasher.hexdigest()
        logger.debug(f"Downloaded installer to {target_path}, hash: {actual_hash}")
        
        # Verify hash if provided
        if expected_hash:
            if actual_hash.lower() != expected_hash.lower():
                logger.error(
                    f"Installer hash mismatch for {url}: "
                    f"expected {expected_hash}, got {actual_hash}"
                )
                target_path.unlink()  # Remove invalid file
                raise ValueError(f"Installer hash mismatch: expected {expected_hash}, got {actual_hash}")
            logger.debug(f"Installer hash verified for {target_path}")
        
        return actual_hash


class WinGetPackageImporter:
    """Imports packages from the official WinGet repository using V2 index."""
    
    def __init__(
        self,
        index_db_path: Path,
        data_dir: Optional[Path] = None,
        base_url: str = WINGET_BASE_URL
    ):
        self.index_reader = WinGetIndexReader(index_db_path, base_url)
        self.manifest_downloader = ManifestDownloader(base_url)
        self.installer_downloader = InstallerDownloader()
        self.data_dir = data_dir or get_data_dir()
        self.index_reader.connect()
    
    async def _load_all_versions_from_manifests(
        self,
        package_id: str,
        package_info: Dict[str, Any],
        architectures: Optional[List[str]],
        scopes: Optional[List[str]],
        installer_types: Optional[List[str]],
        version_filter: Optional[str],
        version_mode: str = "all",
        save_upstream_manifests: bool = False,
    ) -> List[Dict[str, Any]]:
        """
        Load versions and installers from manifest files using V2 index.
        
        Args:
            version_mode: "latest" to only load the latest version, "all" to load all versions
        """
        all_version_data = []
        
        # Get hash prefix from package info
        hash_prefix = package_info.get("hash_prefix")
        if not hash_prefix:
            raise ValueError(f"Package {package_id} missing hash prefix")
        
        # Download PackageVersionDataManifest
        version_data_manifest = await self.index_reader.download_package_version_data_manifest(
            package_id,
            hash_prefix
        )
        
        if not version_data_manifest:
            raise ValueError(f"Failed to download PackageVersionDataManifest for {package_id}")
                
        # Extract versions from PackageVersionDataManifest
        version_list = self.index_reader.get_all_versions_from_manifest(version_data_manifest)
        
        # If only latest is requested, find and process only the latest version
        if version_mode == "latest":
            # Sort versions to find the latest (newest first)
            def version_key(v: str) -> tuple:
                """Convert version string to sortable tuple."""
                # Ensure v is a string (YAML might parse numeric versions as floats)
                v_str = str(v) if v is not None else ""
                parts = []
                for part in v_str.replace("-", ".").split("."):
                    try:
                        parts.append((0, int(part)))
                    except ValueError:
                        parts.append((1, part))
                return tuple(parts)
            
            # Sort versions descending (newest first)
            version_list.sort(key=lambda v: version_key(v["version"]), reverse=True)
            # Only process the latest version
            version_list = version_list[:1]
        
        # Download manifests for each version and extract installers
        for version_info in version_list:
            # Ensure version is a string (YAML might parse numeric versions as floats)
            version_str = str(version_info["version"]) if version_info["version"] is not None else ""
            manifest_relative_path = version_info["relative_path"]
            manifest_hash = version_info["manifest_hash"]
                    
            # Apply version filter
            if version_filter and not fnmatch.fnmatch(version_str, version_filter):
                continue
                    
            # Download manifest
            try:
                manifest_text: Optional[str] = None
                manifest_actual_hash: Optional[str] = None
                if save_upstream_manifests:
                    manifest, manifest_text, manifest_actual_hash = (
                        await self.manifest_downloader.download_manifest_with_text(
                            manifest_relative_path,
                            manifest_hash,
                        )
                    )
                else:
                    manifest = await self.manifest_downloader.download_manifest(
                    manifest_relative_path,
                        manifest_hash,
                )
            except Exception as e:
                # Skip versions we can't download
                logger.warning(
                    f"Failed to download manifest for {package_id} version {version_str}: {e}",
                    exc_info=True
                )
                continue
                    
            # Extract installers from this version's manifest
            installers = self.manifest_downloader.extract_installer_info(
                manifest,
                architecture=architectures,
                scope=scopes,
                installer_types=installer_types
            )
            
            logger.debug(
                f"Extracted {len(installers)} installers from manifest for {package_id} version {version_str} "
                f"(filters: arch={architectures}, scope={scopes}, types={installer_types})"
            )
            
            if len(installers) == 0:
                logger.warning(
                    f"No installers matched filters for {package_id} version {version_str}. "
                    f"Manifest may have different architecture/scope/installer_type values."
                )
                    
            # Add each installer as version data
            for installer in installers:
                all_version_data.append({
                    "version": version_str,
                    "architecture": installer["architecture"],
                    "scope": installer["scope"],
                    "installer_type": installer["installer_type"],
                    "installer": installer,
                    # Debugging: persist the exact upstream YAML that produced these entries.
                    "upstream_manifest_relative_path": manifest_relative_path,
                    "upstream_manifest_expected_hash": manifest_hash,
                    "upstream_manifest_actual_hash": manifest_actual_hash,
                    "upstream_manifest_text": manifest_text,
                })
        
        return all_version_data
    
    def _select_latest_version_data(
        self,
        version_data: List[Dict[str, Any]],
        architectures: Optional[List[str]],
        scopes: Optional[List[str]],
        installer_types: Optional[List[str]]
    ) -> List[Dict[str, Any]]:
        """Select latest version for each architecture/scope/installer_type combination."""
        def version_key(v: str) -> tuple:
            """Convert version string to sortable tuple."""
            # Ensure v is a string (YAML might parse numeric versions as floats)
            v_str = str(v) if v is not None else ""
            parts = []
            for part in v_str.replace("-", ".").split("."):
                try:
                    parts.append((0, int(part)))
                except ValueError:
                    parts.append((1, part))
            return tuple(parts)
        
        # Group by architecture/scope/installer_type
        groups = {}
        for vd in version_data:
            arch = vd.get("architecture", "x64")
            scp = vd.get("scope", "user")
            inst_type = vd.get("installer_type", "exe")
            
            # Filter by installer type if specified
            if installer_types and inst_type not in installer_types:
                continue
            
            key = (arch, scp, inst_type)
            if key not in groups:
                groups[key] = []
            groups[key].append(vd)
        
        # Select latest version from each group
        selected = []
        for group_versions in groups.values():
            # Sort by version (descending)
            group_versions.sort(
                key=lambda x: version_key(x["version"]),
                reverse=True
            )
            selected.append(group_versions[0])  # Latest
        
        return selected
    
    async def import_package(
        self,
        package_id: str,
        architectures: Optional[List[str]] = None,
        scopes: Optional[List[str]] = None,
        installer_types: Optional[List[str]] = None,
        version_mode: str = "latest",
        version_filter: Optional[str] = None,
        track_cache: bool = True
    ) -> Dict[str, Any]:
        """
        Import a package from the official WinGet repository using V2 index.
        
        Args:
            package_id: Package identifier (e.g., "Microsoft.WindowsTerminal")
            architectures: List of architectures to filter (x86, x64, arm64)
            scopes: List of scopes to filter (user, machine)
            installer_types: List of installer types to filter (empty = all)
            version_mode: "latest" to import only latest per arch/scope/type, "all" for all versions
            version_filter: Optional version filter (e.g., "1.18.*")
            track_cache: Whether to track this as a cached package
        
        Returns:
            Dictionary with import results
        """
        # Find package in database
        logger.info(f"Importing package: {package_id}")
        package_info = self.index_reader.find_package_by_id(package_id)
        if not package_info:
            logger.error(f"Package not found in index: {package_id}")
            raise ValueError(f"Package not found: {package_id}")
        
        logger.debug(
            f"Found package {package_id}: "
            f"name={package_info.get('package_name')}, "
            f"latest_version={package_info.get('latest_version')}, "
            f"hash_prefix={package_info.get('hash_prefix')}"
        )
        
        # Load versions from manifests using PackageVersionDataManifest
        # If version_mode is "latest", only the latest version will be loaded
        logger.info(
            f"Loading versions for {package_id} "
            f"(mode={version_mode}, architectures={architectures}, "
            f"scopes={scopes}, installer_types={installer_types}, "
            f"version_filter={version_filter})"
        )
        all_version_data = await self._load_all_versions_from_manifests(
            package_id,
            package_info,
            architectures,
            scopes,
            installer_types,
            version_filter,
            version_mode,
            save_upstream_manifests=track_cache,
        )
        
        if not all_version_data:
            logger.warning(f"No versions found for package {package_id} with specified filters")
            raise ValueError(f"No versions found for package {package_id} with filters")
        
        logger.info(f"Found {len(all_version_data)} version entries for {package_id}")
        
        # If version_mode is "latest", we already only loaded the latest version
        # But we still need to select latest per architecture/scope/installer_type combination
        if version_mode == "latest":
            version_data_list = self._select_latest_version_data(all_version_data, architectures, scopes, installer_types)
        else:
            version_data_list = all_version_data
        
        # Create package directory in cached folder
        package_dir = self.data_dir / "cached" / package_id
        package_dir.mkdir(parents=True, exist_ok=True)
        
        # Create package metadata (mark as cached and persist cache settings)
        package_metadata = PackageCommonMetadata(
            package_identifier=package_id,
            package_name=package_info.get("package_name") or package_id,
            publisher=package_info.get("publisher") or "Unknown",
            short_description="Imported from WinGet repository",
            cached=True,
            cache_settings=CacheSettings(
                architectures=architectures or [],
                scopes=scopes or [],
                installer_types=installer_types or [],
                version_mode=version_mode,
                version_filter=version_filter,
                auto_update=True,
            ),
        )
        
        # Save package.json
        package_json_path = package_dir / "package.json"
        package_json_path.write_text(
            package_metadata.model_dump_json(indent=2),
            encoding="utf-8"
        )
        
        imported_versions = []
        cached_version_infos = []
        errors = []
        
        # Import each version
        logger.info(f"Importing {len(version_data_list)} versions for {package_id}")
        for version_data in version_data_list:
            version_str = version_data.get("version")
            try:
                logger.debug(
                    f"Importing version {version_str} "
                    f"(arch={version_data.get('architecture')}, "
                    f"scope={version_data.get('scope')})"
                )
                result = await self._import_version_from_data(
                    package_id,
                    version_data,
                    package_dir
                )
                imported_versions.append(result)
                logger.debug(f"Successfully imported version {version_str}")
            except Exception as e:
                logger.error(
                    f"Failed to import version {version_str} for {package_id}: {e}",
                    exc_info=True
                )
                errors.append({
                    "version": version_str,
                    "error": str(e)
                })
        
        # Track as cached package if requested
        if track_cache:
            logger.debug(f"Tracking {package_id} as cached package")
            cache_store = get_cached_packages_store()
            cached_package = CachedPackage(
                package_id=package_id,
                package_name=package_metadata.package_name,
                publisher=package_info.get("publisher") or "Unknown",
                cache_settings=CacheSettings(
                    # Store empty lists when filters are None (meaning "all")
                    architectures=architectures or [],
                    scopes=scopes or [],
                    installer_types=installer_types or [],
                    version_mode=version_mode,
                    version_filter=version_filter,
                    auto_update=True,  # Default to auto-update enabled
                ),
            )
            cache_store.add_or_update(cached_package)
        
        logger.info(
            f"Successfully imported {package_id}: "
            f"{len(imported_versions)} versions imported, {len(errors)} errors"
        )
        return {
            "package_id": package_id,
            "package_name": package_metadata.package_name,
            "imported_versions": len(imported_versions),
            "versions": imported_versions,
            "errors": errors
        }
    
    async def _import_version_from_data(
        self,
        package_id: str,
        version_data: Dict[str, Any],
        package_dir: Path
    ) -> Dict[str, Any]:
        """Import a single version from version data."""
        version = version_data["version"]
        arch = version_data["architecture"]
        scp = version_data["scope"]
        installer_info = version_data["installer"]
        
        # Provide defaults for None/empty values
        # Scope defaults to "user" if not specified (WinGet convention)
        if not scp:
            scp = "user"
        # Architecture should always be present, but provide fallback
        if not arch:
            arch = "x64"
        
        # Create version directory
        version_dir_name = f"{version}-{arch}-{scp}"
        version_dir = package_dir / version_dir_name
        version_dir.mkdir(parents=True, exist_ok=True)

        # Debugging: save the upstream manifest YAML that produced this cached version.
        upstream_text = version_data.get("upstream_manifest_text")
        if upstream_text:
            (version_dir / "upstream_manifest.yaml").write_text(
                upstream_text,
                encoding="utf-8",
            )
            # Save a tiny bit of provenance so we can trace what was downloaded.
            upstream_relative_path = version_data.get("upstream_manifest_relative_path")
            upstream_expected = version_data.get("upstream_manifest_expected_hash")
            upstream_actual = version_data.get("upstream_manifest_actual_hash")
            (version_dir / "upstream_manifest_source.json").write_text(
                json.dumps(
                    {
                        "relative_path": upstream_relative_path,
                        "url": f"{self.manifest_downloader.base_url}/{upstream_relative_path}"
                        if upstream_relative_path
                        else None,
                        "expected_sha256": upstream_expected,
                        "actual_sha256": upstream_actual,
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
        
        # Determine installer filename
        installer_url = installer_info["url"]
        installer_filename = Path(installer_url).name
        if not installer_filename or installer_filename == installer_url:
            # Fallback: use package_id and extension from URL
            ext = Path(installer_url.split("?")[0]).suffix or ".exe"
            installer_filename = f"{package_id.replace('.', '_')}{ext}"
        
        installer_path = version_dir / installer_filename
        
        # Download installer
        installer_hash = await self.installer_downloader.download_installer(
            installer_url,
            installer_path,
            installer_info.get("sha256")
        )
        
        # Create version metadata
        version_metadata = VersionMetadata(
            version=version,
            architecture=arch,
            scope=scp,
            installer_type=installer_info.get("installer_type", "exe"),
            installer_file=installer_filename,
            installer_sha256=installer_hash,
            silent_arguments=installer_info.get("silent_arguments"),
            interactive_arguments=installer_info.get("interactive_arguments"),
            log_arguments=installer_info.get("log_arguments"),
            product_code=installer_info.get("product_code"),
            requires_elevation=installer_info.get("requires_elevation", False),
        )
        
        # Save version.json
        version_json_path = version_dir / "version.json"
        version_json_path.write_text(
            version_metadata.model_dump_json(indent=2),
            encoding="utf-8"
        )
        
        return {
            "version": version,
            "architecture": arch,
            "scope": scp,
            "installer_type": installer_info.get("installer_type", "exe"),
            "installer_file": installer_filename,
            "installer_hash": installer_hash,
        }
    
    def close(self):
        """Close the index reader."""
        self.index_reader.close()
