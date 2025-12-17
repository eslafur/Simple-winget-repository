"""
Unified caching service for managing the upstream WinGet repository index and importing packages.

This service handles:
- Downloading and updating the WinGet index database
- Querying the index for packages and versions
- Downloading manifests and installers
- Importing packages into the local repository
- Periodic updates of cached packages
"""
from __future__ import annotations

import asyncio
import fnmatch
import hashlib
import json
import logging
import shutil
import sqlite3
import struct
import tempfile
import zipfile
import zlib
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, List, Dict, Any
import httpx
import yaml
import aiofiles

from app.domain.models import (
    PackageCommonMetadata,
    VersionMetadata,
    CacheSettings,
    ADGroupScopeEntry
)
from app.storage.db_manager import DatabaseManager

logger = logging.getLogger(__name__)

WINGET_BASE_URL = "https://cdn.winget.microsoft.com/cache"
INDEX_PACKAGE_V2 = "source2.msix"
INDEX_PACKAGE_V1 = "source.msix"
INDEX_DB_PATH = "Public/index.db"


class CachingService:
    """
    Unified service for managing WinGet upstream repository caching.
    
    Handles index downloads, package queries, version checking, and installer imports.
    """
    
    def __init__(self, db_manager: DatabaseManager, base_url: str = WINGET_BASE_URL):
        self.db = db_manager
        self.base_url = base_url
        
        # Get data directory and set up cache paths
        from app.core.dependencies import get_data_dir
        self.data_dir = get_data_dir()
        self.cache_dir = self.data_dir / "cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        
        # Determine index path (try new location first, fallback to old)
        self.index_path = self.cache_dir / "winget_index" / "index.db"
        if not self.index_path.exists():
            self.index_path = self.cache_dir / "index.db"
    
    # ========================================================================
    # Index Management
    # ========================================================================
    
    def _get_status_path(self) -> Path:
        """Get path to index status JSON file."""
        return self.cache_dir / "winget_index_status.json"
    
    def _update_status(self, last_pulled: datetime = None):
        """Update the index status file."""
        status_path = self._get_status_path()
        status_path.parent.mkdir(parents=True, exist_ok=True)
        data = {}
        if status_path.exists():
            try:
                data = json.loads(status_path.read_text(encoding="utf-8"))
            except Exception:
                pass
        
        if last_pulled:
            data["last_pulled"] = last_pulled.isoformat()
            
        status_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    
    def get_index_status(self) -> Dict[str, Any]:
        """Get the status of the WinGet index."""
        status_path = self._get_status_path()
        last_pulled = None
        if status_path.exists():
            try:
                data = json.loads(status_path.read_text(encoding="utf-8"))
                if data.get("last_pulled"):
                    last_pulled = datetime.fromisoformat(data["last_pulled"])
            except Exception:
                pass
        
        if not last_pulled and self.index_path.exists():
            # Fallback to file mtime
            try:
                last_pulled = datetime.fromtimestamp(self.index_path.stat().st_mtime)
            except Exception:
                pass

        return {
            "exists": self.index_path.exists(),
            "path": str(self.index_path),
            "last_pulled": last_pulled
        }
    
    async def update_index(self) -> Path:
        """
        Download and extract the WinGet index database.
        
        Returns:
            Path to the extracted index.db file
        """
        index_dir = self.cache_dir / "winget_index"
        index_dir.mkdir(parents=True, exist_ok=True)
        
        # Always write the extracted DB to a stable location
        index_db_path = index_dir / "index.db"
        
        # Try source2.msix first, fallback to source.msix
        for package_name in [INDEX_PACKAGE_V2, INDEX_PACKAGE_V1]:
            package_url = f"{self.base_url}/{package_name}"
            package_path = index_dir / package_name
            package_tmp_path = index_dir / f"{package_name}.tmp"
            
            try:
                logger.info(f"Downloading {package_name}...")
                # Download to a temp file first to avoid leaving partial/corrupt MSIX behind
                if package_tmp_path.exists():
                    package_tmp_path.unlink()

                # Basic retry loop for flaky connections
                last_error: Exception | None = None
                for attempt in range(1, 4):
                    try:
                        async with httpx.AsyncClient(follow_redirects=True, timeout=60.0) as client:
                            async with client.stream("GET", package_url) as response:
                                response.raise_for_status()

                                total_size = int(response.headers.get("content-length", 0))
                                downloaded = 0

                                async with aiofiles.open(package_tmp_path, "wb") as f:
                                    async for chunk in response.aiter_bytes():
                                        await f.write(chunk)
                                        downloaded += len(chunk)
                                        if total_size > 0:
                                            percent = (downloaded / total_size) * 100
                                            logger.debug(f"Progress: {percent:.1f}%")
                        last_error = None
                        break
                    except Exception as e:
                        last_error = e
                        # Clean up temp file and retry
                        if package_tmp_path.exists():
                            package_tmp_path.unlink(missing_ok=True)
                        if attempt < 3:
                            logger.warning(f"Download failed (attempt {attempt}/3): {e}. Retrying...")
                            await asyncio.sleep(1.0 * attempt)
                        else:
                            raise

                if last_error:
                    raise last_error

                # Move temp file into place
                if package_path.exists():
                    package_path.unlink(missing_ok=True)
                package_tmp_path.replace(package_path)
                
                logger.info(f"Extracting index.db from {package_name}...")
                
                # Extract index.db from MSIX (MSIX is a ZIP file)
                with zipfile.ZipFile(package_path, "r") as zip_ref:
                    if INDEX_DB_PATH not in zip_ref.namelist():
                        logger.warning(f"Warning: {INDEX_DB_PATH} not found in {package_name}")
                        continue

                    # Ensure target doesn't exist / isn't locked
                    if index_db_path.exists():
                        index_db_path.unlink(missing_ok=True)

                    with zip_ref.open(INDEX_DB_PATH, "r") as src, open(index_db_path, "wb") as dst:
                        shutil.copyfileobj(src, dst)

                logger.info(f"Index database extracted to: {index_db_path}")

                # Best-effort cleanup of legacy extracted folder
                public_dir = index_dir / "Public"
                if public_dir.exists():
                    shutil.rmtree(public_dir, ignore_errors=True)

                # Update index path reference and status
                self.index_path = index_db_path
                self._update_status(last_pulled=datetime.now())
                return index_db_path
                        
            except Exception as e:
                logger.error(f"Failed to download {package_name}: {e}")
                if package_tmp_path.exists():
                    package_tmp_path.unlink(missing_ok=True)
                if package_path.exists():
                    package_path.unlink(missing_ok=True)
                continue
        
        raise Exception("Failed to download and extract index from both source2.msix and source.msix")
    
    # ========================================================================
    # Index Querying
    # ========================================================================
    
    def _get_index_connection(self) -> sqlite3.Connection:
        """Get a connection to the index database."""
        if not self.index_path.exists():
            raise FileNotFoundError(f"Index database not found: {self.index_path}")
        
        conn = sqlite3.connect(str(self.index_path))
        conn.row_factory = sqlite3.Row
        return conn
    
    def find_package_by_id(self, package_id: str) -> Optional[Dict[str, Any]]:
        """Find a package by its identifier in the index."""
        logger.debug(f"Querying package: {package_id}")
        conn = self._get_index_connection()
        try:
            cursor = conn.cursor()
            
            cursor.execute("""
                SELECT 
                    p.id as package_id,
                    p.name as package_name,
                    p.latest_version,
                    p.hash
                FROM packages p
                WHERE p.id = ?
                LIMIT 1
            """, (package_id,))
            
            row = cursor.fetchone()
            if not row:
                logger.debug(f"Package not found: {package_id}")
                return None
            
            result = dict(row)
            
            # Convert hash BLOB to hex string
            hash_blob = result.pop("hash")
            if isinstance(hash_blob, bytes):
                hash_hex = hash_blob.hex()
            else:
                hash_hex = str(hash_blob)
            
            result["hash_hex"] = hash_hex
            result["hash_prefix"] = hash_hex[:8]
            
            # Try to get publisher from norm_publishers2 table
            try:
                cursor.execute("""
                    SELECT np.norm_publisher
                    FROM norm_publishers2 np
                    JOIN packages p ON np.package = p.rowid
                    WHERE p.id = ?
                    LIMIT 1
                """, (package_id,))
                pub_row = cursor.fetchone()
                result["publisher"] = pub_row[0] if pub_row and pub_row[0] else None
            except Exception as e:
                logger.debug(f"Could not retrieve publisher for {package_id}: {e}")
                result["publisher"] = None
            
            return result
        except sqlite3.OperationalError as e:
            logger.error(f"Database error querying package {package_id}: {e}", exc_info=True)
            raise ValueError(f"Failed to query package: {e}")
        finally:
            conn.close()
    
    def search_upstream_packages(self, query: str, limit: int = 50) -> List[Dict[str, Any]]:
        """Search for packages in the upstream WinGet index."""
        if not self.index_path.exists():
            return []
            
        try:
            conn = self._get_index_connection()
            try:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT DISTINCT 
                        i.id as package_id,
                        n.name as package_name,
                        p.publisher as publisher
                    FROM ids i
                    LEFT JOIN names n ON i.id = n.id
                    LEFT JOIN publishers p ON i.id = p.id
                    WHERE i.id LIKE ? OR n.name LIKE ?
                    LIMIT ?
                """, (f"%{query}%", f"%{query}%", limit))
                
                return [dict(row) for row in cursor.fetchall()]
            finally:
                conn.close()
        except Exception as e:
            logger.error(f"Search failed: {e}")
            raise
    
    async def get_upstream_package_versions(
        self, 
        package_id: str, 
        architecture: Optional[str] = None, 
        scope: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """
        Get available versions for a package from upstream.
        
        Note: The V2 index structure requires downloading manifests to get versions.
        Architecture and scope filters are not applied at this level since they
        are properties of installers within versions.
        """
        if not self.index_path.exists():
            return []
            
        try:
            package_info = self.find_package_by_id(package_id)
            if not package_info:
                return []
            
            hash_prefix = package_info.get("hash_prefix")
            if not hash_prefix:
                return []
            
            manifest = await self._download_package_version_data_manifest(package_id, hash_prefix)
            if not manifest:
                return []
            
            return self._get_all_versions_from_manifest(manifest)
        except Exception as e:
            logger.error(f"Get versions failed: {e}")
            raise
    
    # ========================================================================
    # Manifest and Version Data
    # ========================================================================
    
    async def _download_package_version_data_manifest(
        self,
        package_id: str,
        hash_prefix: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """Download and parse PackageVersionDataManifest for a package."""
        if hash_prefix is None:
            package_info = self.find_package_by_id(package_id)
            if not package_info:
                return None
            hash_prefix = package_info.get("hash_prefix")
            if not hash_prefix:
                return None
        
        # Download compressed MSZIP version
        url = f"{self.base_url}/packages/{package_id}/{hash_prefix}/versionData.mszyml"
        logger.debug(f"Downloading PackageVersionDataManifest from {url}")
        
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get(url)
                response.raise_for_status()
                compressed_data = response.content
                logger.debug(f"Downloaded {len(compressed_data)} bytes of compressed data for {package_id}")
                
                # Decompress MSZIP
                decompressed_data = self._decompress_mszip(compressed_data)
                logger.debug(f"Decompressed to {len(decompressed_data)} bytes for {package_id}")
                content = decompressed_data.decode('utf-8')
        except Exception as e:
            logger.error(f"Failed to download and decompress PackageVersionDataManifest for {package_id}: {e}", exc_info=True)
            return None
        
        # Parse YAML
        try:
            manifest = yaml.safe_load(content)
            version_count = len(manifest.get("vD", []))
            logger.debug(f"Successfully parsed PackageVersionDataManifest for {package_id}: {version_count} versions found")
            return manifest
        except Exception as e:
            logger.error(f"Failed to parse PackageVersionDataManifest YAML for {package_id}: {e}", exc_info=True)
            return None
    
    def _decompress_mszip(self, compressed_data: bytes) -> bytes:
        """
        Decompress MSZIP compressed data using only Python standard library.
        
        MSZIP format:
        - 24-byte header starting with magic number 0x0a51e5c01800
        - Uncompressed size at offset 8-16 (8 bytes, little-endian)
        - Chunks, each with:
          - 4-byte chunk size (little-endian)
          - 2-byte 'CK' signature
          - Compressed DEFLATE data
        """
        if not compressed_data:
            logger.error("Empty compressed data provided")
            raise ValueError("Empty compressed data")
        
        # Check for MSZIP header magic number
        if len(compressed_data) < 24:
            logger.error(f"MSZIP file too small: {len(compressed_data)} bytes (expected at least 24)")
            raise ValueError("MSZIP file too small (missing header)")
        
        # MSZIP header magic: 0x0a51e5c01800
        expected_magic = b'\x0a\x51\xe5\xc0\x18\x00'
        if compressed_data[:6] != expected_magic:
            logger.error(f"Invalid MSZIP header magic: expected {expected_magic.hex()}, got {compressed_data[:6].hex()}")
            raise ValueError(f"Invalid MSZIP header. Expected magic {expected_magic.hex()}, got {compressed_data[:6].hex()}")
        
        # Extract uncompressed size from header (offset 8-16, 8 bytes little-endian)
        uncompressed_size = struct.unpack('<Q', compressed_data[8:16])[0]
        logger.debug(f"MSZIP header: compressed={len(compressed_data)} bytes, uncompressed={uncompressed_size} bytes")
        
        # Create decompressor for raw DEFLATE streams
        decompressor = zlib.decompressobj(-zlib.MAX_WBITS)
        decompressed_data = bytearray()
        
        # Process chunks starting after the 24-byte header
        offset = 24
        chunk_count = 0
        
        while len(decompressed_data) < uncompressed_size and offset < len(compressed_data):
            # Read chunk size (4 bytes little-endian)
            if offset + 4 > len(compressed_data):
                break
            
            chunk_size = struct.unpack('<I', compressed_data[offset:offset+4])[0]
            offset += 4
            
            # Read 'CK' signature (2 bytes)
            if offset + 2 > len(compressed_data):
                logger.error("Unexpected end of file when reading chunk signature")
                raise ValueError("Unexpected end of file when reading chunk signature")
            
            ck_signature = compressed_data[offset:offset+2]
            if ck_signature != b'CK':
                logger.error(f"Invalid chunk signature at offset {offset}: expected 'CK', got {ck_signature}")
                raise ValueError(f"Invalid chunk signature. Expected 'CK', got {ck_signature}")
            
            offset += 2
            
            # Read compressed data (chunk_size includes the 2-byte CK signature)
            compressed_chunk_size = chunk_size - 2
            if offset + compressed_chunk_size > len(compressed_data):
                logger.error(f"Unexpected end of file when reading compressed chunk (offset={offset}, chunk_size={compressed_chunk_size}, total={len(compressed_data)})")
                raise ValueError("Unexpected end of file when reading compressed chunk")
            
            compressed_chunk = compressed_data[offset:offset+compressed_chunk_size]
            offset += compressed_chunk_size
            
            # Decompress the chunk
            try:
                decompressed_chunk = decompressor.decompress(compressed_chunk)
                decompressed_data.extend(decompressed_chunk)
                chunk_count += 1
            except zlib.error as e:
                logger.error(f"Failed to decompress chunk {chunk_count}: {e}", exc_info=True)
                raise ValueError(f"Failed to decompress chunk: {e}") from e
        
        # Flush any remaining data
        try:
            remaining = decompressor.flush()
            if remaining:
                decompressed_data.extend(remaining)
        except Exception as e:
            logger.debug(f"Error flushing decompressor: {e}")
        
        result = bytes(decompressed_data)
        
        # Trim to exact uncompressed size if needed
        if len(result) > uncompressed_size:
            logger.debug(f"Trimming decompressed data from {len(result)} to {uncompressed_size} bytes")
            result = result[:uncompressed_size]
        
        logger.debug(f"MSZIP decompression complete: {chunk_count} chunks processed, result size={len(result)} bytes (expected {uncompressed_size})")
        
        if len(result) != uncompressed_size:
            logger.warning(f"Decompressed size mismatch: expected {uncompressed_size} bytes, got {len(result)} bytes")
        
        return result
    
    def _get_all_versions_from_manifest(self, version_data_manifest: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Extract all versions from PackageVersionDataManifest."""
        version_data_list = version_data_manifest.get("vD", [])  # "vD" = VersionData
        
        result = []
        for vd in version_data_list:
            # Ensure version is always a string (YAML might parse numeric versions as floats)
            version_value = vd.get("v", "")  # "v" = Version
            version_str = str(version_value) if version_value is not None else ""
            version_info = {
                "version": version_str,
                "relative_path": vd.get("rP", ""),  # "rP" = RelativePath
                "manifest_hash": vd.get("s256H", ""),  # "s256H" = SHA256Hash
            }
            result.append(version_info)
        
        logger.debug(f"Extracted {len(result)} versions from PackageVersionDataManifest")
        return result
    
    # ========================================================================
    # Manifest and Installer Downloading
    # ========================================================================
    
    async def _download_manifest_with_text(
        self,
        relative_path: str,
        expected_hash: Optional[str] = None,
    ) -> tuple[Dict[str, Any], str, str]:
        """Download and parse a manifest file, returning parsed dict, text, and hash."""
        manifest_url = f"{self.base_url}/{relative_path}"
        logger.debug(f"Downloading manifest from {manifest_url}")

        async with httpx.AsyncClient() as client:
            response = await client.get(manifest_url)
            response.raise_for_status()
            content = response.text

        actual_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()

        if expected_hash:
            if actual_hash.lower() != expected_hash.lower():
                logger.error(f"Manifest hash mismatch for {relative_path}")
                raise ValueError(f"Manifest hash mismatch: expected {expected_hash}, got {actual_hash}")

        try:
            manifest = yaml.safe_load(content)
            return manifest, content, actual_hash
        except Exception as e:
            logger.error(f"Failed to parse YAML manifest from {relative_path}: {e}")
            raise
    
    async def _download_manifest(
        self,
        relative_path: str,
        expected_hash: Optional[str] = None
    ) -> Dict[str, Any]:
        """Download and parse a manifest file."""
        manifest, _, _ = await self._download_manifest_with_text(relative_path, expected_hash)
        return manifest
    
    def _extract_installer_info(
        self,
        manifest: Dict[str, Any],
        architecture: Optional[List[str]] = None,
        scope: Optional[List[str]] = None,
        installer_types: Optional[List[str]] = None
    ) -> List[Dict[str, Any]]:
        """Extract installer information from a manifest with optional filters."""
        installers = []
        versions = manifest.get("Versions", [])
        if not versions:
            if "Installers" in manifest:
                versions = [manifest]
        
        for version_entry in versions:
            version_installers = version_entry.get("Installers", [])
            version_str = version_entry.get("PackageVersion") or manifest.get("PackageVersion")
            
            version_scope = (version_entry.get("Scope") or manifest.get("Scope") or "user").lower()
            version_installer_type = (version_entry.get("InstallerType") or manifest.get("InstallerType") or "").lower()
            
            for installer in version_installers:
                inst_arch = installer.get("Architecture", "").lower()
                inst_scope = (installer.get("Scope") or version_scope or "user").lower()
                inst_type = (installer.get("InstallerType") or version_installer_type).lower()
                
                arch_list = [a.lower() for a in architecture] if architecture else []
                scope_list = [s.lower() for s in scope] if scope else []
                type_list = [it.lower() for it in installer_types] if installer_types else []
                
                if arch_list and inst_arch not in arch_list:
                    continue
                if scope_list and inst_scope not in scope_list:
                    continue
                if type_list and inst_type not in type_list:
                    continue
                
                installer_info = {
                    "url": installer.get("InstallerUrl"),
                    "sha256": installer.get("InstallerSha256"),
                    "architecture": installer.get("Architecture"),
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
    
    async def _download_installer(
        self,
        url: str,
        target_path: Path,
        expected_hash: Optional[str] = None
    ) -> str:
        """Download an installer file and verify its hash."""
        logger.debug(f"Downloading installer from {url}")
        
        hasher = hashlib.sha256()
        async with httpx.AsyncClient(follow_redirects=True, timeout=60.0) as client:
            async with client.stream("GET", url) as response:
                response.raise_for_status()
                async with aiofiles.open(target_path, "wb") as f:
                    async for chunk in response.aiter_bytes():
                        hasher.update(chunk)
                        await f.write(chunk)
        
        actual_hash = hasher.hexdigest()
        
        if expected_hash:
            if actual_hash.lower() != expected_hash.lower():
                logger.error(f"Installer hash mismatch for {url}: expected {expected_hash}, got {actual_hash}")
                if target_path.exists():
                    target_path.unlink()
                raise ValueError(f"Installer hash mismatch: expected {expected_hash}, got {actual_hash}")
        
        return actual_hash
    
    # ========================================================================
    # Package Importing
    # ========================================================================
    
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
        """Load all version data from manifests for a package."""
        hash_prefix = package_info.get("hash_prefix")
        if not hash_prefix:
            raise ValueError(f"Package {package_id} missing hash prefix")
        
        version_data_manifest = await self._download_package_version_data_manifest(
            package_id,
            hash_prefix
        )
        if not version_data_manifest:
            raise ValueError(f"Failed to download PackageVersionDataManifest for {package_id}")
                
        version_list = self._get_all_versions_from_manifest(version_data_manifest)
        
        if version_mode == "latest":
            def version_key(v: str) -> tuple:
                v_str = str(v) if v is not None else ""
                parts = []
                for part in v_str.replace("-", ".").split("."):
                    try:
                        parts.append((0, int(part)))
                    except ValueError:
                        parts.append((1, part))
                return tuple(parts)
            
            version_list.sort(key=lambda v: version_key(v["version"]), reverse=True)
            version_list = version_list[:1]
        
        all_version_data = []
        for version_info in version_list:
            version_str = str(version_info["version"]) if version_info["version"] is not None else ""
            manifest_relative_path = version_info["relative_path"]
            manifest_hash = version_info["manifest_hash"]
                    
            if version_filter and not fnmatch.fnmatch(version_str, version_filter):
                continue
                    
            try:
                manifest_text: Optional[str] = None
                manifest_actual_hash: Optional[str] = None
                if save_upstream_manifests:
                    manifest, manifest_text, manifest_actual_hash = (
                        await self._download_manifest_with_text(
                            manifest_relative_path,
                            manifest_hash,
                        )
                    )
                else:
                    manifest = await self._download_manifest(
                        manifest_relative_path,
                        manifest_hash,
                    )
            except Exception as e:
                logger.warning(f"Failed to download manifest for {package_id} version {version_str}: {e}")
                continue
                    
            installers = self._extract_installer_info(
                manifest,
                architecture=architectures,
                scope=scopes,
                installer_types=installer_types
            )
            
            for installer in installers:
                all_version_data.append({
                    "version": version_str,
                    "architecture": installer["architecture"],
                    "scope": installer["scope"],
                    "installer_type": installer["installer_type"],
                    "installer": installer,
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
        """Select the latest version for each unique architecture/scope/type combination."""
        def version_key(v: str) -> tuple:
            v_str = str(v) if v is not None else ""
            parts = []
            for part in v_str.replace("-", ".").split("."):
                try:
                    parts.append((0, int(part)))
                except ValueError:
                    parts.append((1, part))
            return tuple(parts)
        
        groups = {}
        for vd in version_data:
            arch = vd.get("architecture", "x64")
            scp = vd.get("scope", "user")
            inst_type = vd.get("installer_type", "exe")
            
            if installer_types and inst_type not in installer_types:
                continue
            
            key = (arch, scp, inst_type)
            if key not in groups:
                groups[key] = []
            groups[key].append(vd)
        
        selected = []
        for group_versions in groups.values():
            group_versions.sort(key=lambda x: version_key(x["version"]), reverse=True)
            selected.append(group_versions[0])
        
        return selected
    
    async def _import_version_from_data(
        self,
        package_id: str,
        version_data: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Import a single version from version data."""
        version = version_data["version"]
        arch = version_data["architecture"] or "x64"
        scp = version_data["scope"] or "user"
        inst_type = version_data.get("installer_type") or "exe"

        # Check if identical installer already exists
        pkg_index = self.db.get_package(package_id)
        if pkg_index:
            for v in pkg_index.versions:
                v_scope = (v.scope or "user").lower()
                req_scope = (scp or "user").lower()
                
                v_arch = (v.architecture or "").lower()
                req_arch = (arch or "").lower()
                
                v_type = (v.installer_type or "exe").lower()
                req_type = (inst_type or "exe").lower()
                
                if (v.version == version and 
                    v_arch == req_arch and 
                    v_scope == req_scope and 
                    v_type == req_type):
                    
                    logger.info(f"Skipping existing installer for {package_id} {version} {arch} {scp}")
                    return {
                        "status": "skipped",
                        "version": version,
                        "architecture": arch,
                        "scope": scp,
                        "installer_file": v.installer_file
                    }

        installer_info = version_data["installer"]
        
        installer_url = installer_info["url"]
        installer_filename = Path(installer_url).name
        if not installer_filename or installer_filename == installer_url:
            ext = Path(installer_url.split("?")[0]).suffix or ".exe"
            installer_filename = f"{package_id.replace('.', '_')}{ext}"
        
        with tempfile.TemporaryDirectory() as tmpdirname:
            tmp_path = Path(tmpdirname) / installer_filename
            
            installer_hash = await self._download_installer(
                installer_url,
                tmp_path,
                installer_info.get("sha256")
            )
            
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
            
            self.db.add_installer(package_id, version_metadata, file_path=tmp_path)
            
        return {
            "version": version,
            "architecture": arch,
            "scope": scp,
            "installer_file": installer_filename
        }
    
    async def import_package(
        self,
        package_id: str,
        architectures: Optional[List[str]] = None,
        scopes: Optional[List[str]] = None,
        installer_types: Optional[List[str]] = None,
        version_mode: str = "latest",
        version_filter: Optional[str] = None,
        track_cache: bool = True,
        ad_group_scopes: Optional[List[ADGroupScopeEntry]] = None,
    ) -> Dict[str, Any]:
        """
        Import a package from the upstream WinGet repository.
        
        Args:
            package_id: Package identifier
            architectures: Optional list of architectures to filter
            scopes: Optional list of scopes to filter
            installer_types: Optional list of installer types to filter
            version_mode: "latest" or "all"
            version_filter: Optional version wildcard filter
            track_cache: Whether to mark this as a cached package
            ad_group_scopes: Optional AD group scope entries
            
        Returns:
            Dictionary with import results
        """
        logger.info(f"Importing package: {package_id}")
        
        if not self.index_path.exists():
            raise FileNotFoundError("WinGet index not found. Please run update_index() first.")
        
        package_info = self.find_package_by_id(package_id)
        if not package_info:
            raise ValueError(f"Package not found: {package_id}")
        
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
            raise ValueError(f"No versions found for package {package_id} with filters")
        
        if version_mode == "latest":
            version_data_list = self._select_latest_version_data(all_version_data, architectures, scopes, installer_types)
        else:
            version_data_list = all_version_data
        
        # Create/Update Package Metadata
        package_metadata = PackageCommonMetadata(
            package_identifier=package_id,
            package_name=package_info.get("package_name") or package_id,
            publisher=package_info.get("publisher") or "Unknown",
            short_description="Imported from WinGet repository",
            cached=track_cache,
            cache_settings=CacheSettings(
                architectures=architectures or [],
                scopes=scopes or [],
                installer_types=installer_types or [],
                version_mode=version_mode,
                version_filter=version_filter,
                auto_update=track_cache,
            ) if track_cache else None,
            ad_group_scopes=ad_group_scopes or []
        )
        
        self.db.save_package(package_metadata)
        
        imported_versions = []
        errors = []
        
        for version_data in version_data_list:
            version_str = version_data.get("version")
            try:
                result = await self._import_version_from_data(package_id, version_data)
                imported_versions.append(result)
            except Exception as e:
                logger.error(f"Failed to import version {version_str}: {e}", exc_info=True)
                errors.append({"version": version_str, "error": str(e)})
        
        return {
            "package_id": package_id,
            "imported_versions": len(imported_versions),
            "errors": errors
        }
    
    # ========================================================================
    # Cached Package Updates
    # ========================================================================
    
    async def update_cached_packages(self):
        """Update all cached packages that have auto_update enabled."""
        logger.info("Starting cached packages update")
        
        # Ensure index exists
        if not self.index_path.exists():
            try:
                await self.update_index()
            except Exception:
                logger.error("No local WinGet index available. Aborting update.")
                return
        else:
            # Always try to update index first in the daily loop
            try:
                await self.update_index()
            except Exception as e:
                logger.warning(f"Failed to update index, using existing: {e}")

        try:
            all_packages = self.db.get_all_packages()
            
            for pkg_index in all_packages:
                pkg = pkg_index.package
                if not pkg.cached or not pkg.cache_settings or not pkg.cache_settings.auto_update:
                    continue
                
                logger.info(f"Checking updates for {pkg.package_identifier}")
                
                try:
                    upstream_info = self.find_package_by_id(pkg.package_identifier)
                    if not upstream_info:
                        logger.warning(f"Package {pkg.package_identifier} not found in upstream index")
                        continue
                        
                    upstream_latest = str(upstream_info.get("latest_version", ""))
                    
                    local_versions = [v.version for v in pkg_index.versions]
                    def version_key(v):
                        parts = []
                        for part in str(v).replace("-", ".").split("."):
                            try:
                                parts.append((0, int(part)))
                            except ValueError:
                                parts.append((1, part))
                        return tuple(parts)
                    
                    local_versions.sort(key=version_key, reverse=True)
                    local_latest = local_versions[0] if local_versions else None
                    
                    if local_latest == upstream_latest:
                        logger.info(f"Package {pkg.package_identifier} is up to date ({local_latest})")
                        continue
                         
                    logger.info(f"Updating {pkg.package_identifier} from {local_latest} to {upstream_latest}")
                    
                    await self.import_package(
                        pkg.package_identifier,
                        architectures=pkg.cache_settings.architectures or None,
                        scopes=pkg.cache_settings.scopes or None,
                        installer_types=pkg.cache_settings.installer_types or None,
                        version_mode=pkg.cache_settings.version_mode,
                        version_filter=pkg.cache_settings.version_filter,
                        track_cache=True,
                        ad_group_scopes=pkg.ad_group_scopes
                    )
                    
                except Exception as e:
                    logger.error(f"Failed to update {pkg.package_identifier}: {e}")
                    
        except Exception as e:
            logger.error(f"Error during cached packages update: {e}")
            
        logger.info("Cached packages update completed")
    
    async def run_periodic_updates(self, run_hour: int = 6, run_minute: int = 0):
        """Run periodic updates at the specified time each day."""
        while True:
            now = datetime.now()
            target = now.replace(hour=run_hour, minute=run_minute, second=0, microsecond=0)
            if target <= now:
                target = target + timedelta(days=1)
            
            wait_seconds = (target - now).total_seconds()
            logger.info(f"Next update scheduled in {wait_seconds:.0f} seconds (at {target})")
            
            await asyncio.sleep(wait_seconds)
            try:
                await self.update_cached_packages()
            except Exception as e:
                logger.error(f"Error in daily update loop: {e}")
