"""
Data models and storage for cached WinGet packages.

Cached packages are stored in data/cached/{package_id}/package.json
Each package has cache_settings that define how it should be cached.
Version information comes from the repository, not from cached metadata.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict, Any
from pydantic import BaseModel, Field

from app.data.repository import get_data_dir
from app.data.models import CacheSettings


class CachedPackage(BaseModel):
    """Metadata for a cached WinGet package."""
    package_id: str  # legacy field name used by cache store
    package_name: str
    publisher: str
    cached: bool = True  # cached packages default to True
    cache_settings: CacheSettings = Field(default_factory=CacheSettings)
    last_updated: datetime = Field(default_factory=datetime.utcnow)


class CachedPackagesStore:
    """Manages storage of cached package metadata."""
    
    def __init__(self, data_dir: Optional[Path] = None):
        self.data_dir = data_dir or get_data_dir()
        self.cached_dir = self.data_dir / "cached"
        self._packages: Dict[str, CachedPackage] = {}
        self._load()
    
    def _get_package_path(self, package_id: str) -> Path:
        """Get the path to a cached package's directory."""
        return self.cached_dir / package_id
    
    def _get_package_json_path(self, package_id: str) -> Path:
        """Get the path to a cached package's package.json file."""
        return self._get_package_path(package_id) / "package.json"
    
    def _load(self):
        """Load cached packages from disk."""
        if not self.cached_dir.exists():
            self.cached_dir.mkdir(parents=True, exist_ok=True)
            return
        
        # Scan cached directory for package.json files
        for pkg_dir in self.cached_dir.iterdir():
            if not pkg_dir.is_dir():
                continue
            
            package_json = pkg_dir / "package.json"
            if not package_json.exists():
                continue
            
            try:
                data = json.loads(package_json.read_text(encoding="utf-8"))
                # Handle migration: if old format with "filters", convert to "cache_settings"
                if "filters" in data and "cache_settings" not in data:
                    data["cache_settings"] = data.pop("filters")
                    # Also migrate auto_update if it was at package level
                    if "auto_update" in data and "cache_settings" in data:
                        data["cache_settings"]["auto_update"] = data.pop("auto_update", True)
                cached_pkg = CachedPackage(**data)
                self._packages[cached_pkg.package_id] = cached_pkg
            except Exception as e:
                # Skip malformed packages
                print(f"Warning: Failed to load cached package from {package_json}: {e}")
                continue
    
    def _save_package(self, package: CachedPackage):
        """Save a single cached package to disk."""
        pkg_dir = self._get_package_path(package.package_id)
        pkg_dir.mkdir(parents=True, exist_ok=True)
        
        package_json = self._get_package_json_path(package.package_id)
        package_json.write_text(
            json.dumps(package.model_dump(mode="json", exclude_none=True), indent=2, default=str),
            encoding="utf-8"
        )
    
    def get(self, package_id: str) -> Optional[CachedPackage]:
        """Get a cached package by ID."""
        return self._packages.get(package_id)
    
    def get_all(self) -> List[CachedPackage]:
        """Get all cached packages."""
        return list(self._packages.values())
    
    def add_or_update(self, package: CachedPackage):
        """Add or update a cached package."""
        self._packages[package.package_id] = package
        self._save_package(package)
    
    def remove(self, package_id: str):
        """Remove a cached package."""
        if package_id in self._packages:
            del self._packages[package_id]
            # Remove package directory
            pkg_dir = self._get_package_path(package_id)
            if pkg_dir.exists():
                import shutil
                shutil.rmtree(pkg_dir)
    
    def update_cache_settings(
        self,
        package_id: str,
        cache_settings: CacheSettings
    ):
        """Update cache settings for a cached package."""
        if package_id in self._packages:
            self._packages[package_id].cache_settings = cache_settings
            self._packages[package_id].last_updated = datetime.utcnow()
            self._save_package(self._packages[package_id])


# Global instance
_cached_packages_store: Optional[CachedPackagesStore] = None


def get_cached_packages_store() -> CachedPackagesStore:
    """Get the global cached packages store."""
    global _cached_packages_store
    if _cached_packages_store is None:
        _cached_packages_store = CachedPackagesStore()
    return _cached_packages_store
