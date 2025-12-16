"""
Query the official WinGet repository SQLite index (V2) to find packages and versions.
"""
from __future__ import annotations

import logging
import sqlite3
import struct
import zlib
from pathlib import Path
from typing import Optional, List, Dict, Any
import yaml
import httpx

logger = logging.getLogger(__name__)


class WinGetIndexReader:
    """Reads and queries the WinGet SQLite index database (V2 only)."""
    
    def __init__(self, index_db_path: Path, base_url: str = "https://cdn.winget.microsoft.com/cache"):
        self.index_db_path = index_db_path
        self.base_url = base_url
        self.conn: Optional[sqlite3.Connection] = None
    
    def connect(self):
        """Open connection to index database."""
        if self.conn is None:
            if not self.index_db_path.exists():
                logger.error(f"Index database not found: {self.index_db_path}")
                raise FileNotFoundError(f"Index database not found: {self.index_db_path}")
            logger.debug(f"Connecting to index database: {self.index_db_path}")
            self.conn = sqlite3.connect(str(self.index_db_path))
            self.conn.row_factory = sqlite3.Row
            logger.debug("Successfully connected to index database")
    
    def close(self):
        """Close database connection."""
        if self.conn:
            self.conn.close()
            self.conn = None
    
    @property
    def index_version(self) -> int:
        """Get the index version (always 2 for V2-only support)."""
        return 2
    
    def find_package_by_id(self, package_id: str) -> Optional[Dict[str, Any]]:
        """
        Find a package by its identifier.
        
        Returns:
            Dictionary with package_id, package_name, publisher, latest_version, and hash_hex
        """
        logger.debug(f"Querying package: {package_id}")
        self.connect()
        cursor = self.conn.cursor()
        
        try:
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
            
            logger.debug(
                f"Found package {package_id}: "
                f"name={result.get('package_name')}, "
                f"publisher={result.get('publisher')}, "
                f"latest_version={result.get('latest_version')}"
            )
            return result
        except sqlite3.OperationalError as e:
            logger.error(f"Database error querying package {package_id}: {e}", exc_info=True)
            raise ValueError(f"Failed to query package: {e}")
    
    def get_package_hash(self, package_id: str) -> Optional[str]:
        """
        Get the package hash (hex string) from the database.
        
        Returns:
            Full hash as hex string, or None if package not found
        """
        self.connect()
        cursor = self.conn.cursor()
        
        cursor.execute("SELECT hash FROM packages WHERE id = ? LIMIT 1", (package_id,))
        row = cursor.fetchone()
        if not row:
            return None
        
        hash_blob = row[0]
        if isinstance(hash_blob, bytes):
            return hash_blob.hex()
        return str(hash_blob)
    
    async def download_package_version_data_manifest(
        self,
        package_id: str,
        hash_prefix: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """
        Download and parse PackageVersionDataManifest for a package.
        
        Args:
            package_id: Package identifier
            hash_prefix: First 8 characters of hash (if None, will query from DB)
        
        Returns:
            Parsed YAML dictionary with version data, or None if not found
        """
        if hash_prefix is None:
            logger.debug(f"Querying hash for {package_id}")
            hash_hex = self.get_package_hash(package_id)
            if not hash_hex:
                logger.warning(f"Could not find hash for package {package_id}")
                return None
            hash_prefix = hash_hex[:8]
        
        # Download compressed MSZIP version
        url = f"{self.base_url}/packages/{package_id}/{hash_prefix}/versionData.mszyml"
        logger.debug(f"Downloading PackageVersionDataManifest from {url}")
        
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get(url)
                response.raise_for_status()
                compressed_data = response.content
                logger.debug(
                    f"Downloaded {len(compressed_data)} bytes of compressed data "
                    f"for {package_id}"
                )
                
                # Decompress MSZIP using standard library
                decompressed_data = self._decompress_mszip(compressed_data)
                logger.debug(
                    f"Decompressed to {len(decompressed_data)} bytes "
                    f"for {package_id}"
                )
                content = decompressed_data.decode('utf-8')
        except Exception as e:
            logger.error(
                f"Failed to download and decompress PackageVersionDataManifest "
                f"for {package_id}: {e}",
                exc_info=True
            )
            raise ValueError(f"Failed to download and decompress PackageVersionDataManifest: {e}")
        
        # Parse YAML
        try:
            manifest = yaml.safe_load(content)
            version_count = len(manifest.get("vD", []))
            logger.debug(
                f"Successfully parsed PackageVersionDataManifest for {package_id}: "
                f"{version_count} versions found"
            )
            return manifest
        except Exception as e:
            logger.error(
                f"Failed to parse PackageVersionDataManifest YAML for {package_id}: {e}",
                exc_info=True
            )
            raise ValueError(f"Failed to parse PackageVersionDataManifest YAML: {e}")
    
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
        
        Uses zlib.decompressobj with -MAX_WBITS for raw DEFLATE streams.
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
            logger.error(
                f"Invalid MSZIP header magic: expected {expected_magic.hex()}, "
                f"got {compressed_data[:6].hex()}"
            )
            raise ValueError(
                f"Invalid MSZIP header. Expected magic {expected_magic.hex()}, "
                f"got {compressed_data[:6].hex()}"
            )
        
        # Extract uncompressed size from header (offset 8-16, 8 bytes little-endian)
        uncompressed_size = struct.unpack('<Q', compressed_data[8:16])[0]
        logger.debug(
            f"MSZIP header: compressed={len(compressed_data)} bytes, "
            f"uncompressed={uncompressed_size} bytes"
        )
        
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
                logger.error(
                    f"Unexpected end of file when reading compressed chunk "
                    f"(offset={offset}, chunk_size={compressed_chunk_size}, "
                    f"total={len(compressed_data)})"
                )
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
            logger.debug(
                f"Trimming decompressed data from {len(result)} to {uncompressed_size} bytes"
            )
            result = result[:uncompressed_size]
        
        logger.debug(
            f"MSZIP decompression complete: {chunk_count} chunks processed, "
            f"result size={len(result)} bytes (expected {uncompressed_size})"
        )
        
        if len(result) != uncompressed_size:
            logger.warning(
                f"Decompressed size mismatch: expected {uncompressed_size} bytes, "
                f"got {len(result)} bytes"
            )
        
        return result
    
    def get_all_versions_from_manifest(
        self,
        version_data_manifest: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        """
        Extract all versions from PackageVersionDataManifest.
        
        Args:
            version_data_manifest: Parsed PackageVersionDataManifest YAML
        
        Returns:
            List of version dictionaries with:
            - version: Version string
            - relative_path: Manifest relative path
            - manifest_hash: SHA256 hash of manifest
        """
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
    
    
    def __enter__(self):
        self.connect()
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

