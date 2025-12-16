import json
import os
import shutil
import uuid
from pathlib import Path
from typing import Optional, List, Dict
from datetime import datetime
import logging

from app.storage.db_manager import DatabaseManager
from app.domain.models import (
    PackageCommonMetadata,
    PackageIndex,
    RepositoryConfig,
    RepositoryIndex,
    VersionMetadata,
    AuthenticationStore,
)

logger = logging.getLogger(__name__)

class JsonDatabaseManager(DatabaseManager):
    def __init__(self, data_dir: Path):
        self._data_dir = data_dir
        self._repository_index = RepositoryIndex()
        self._repository_config: Optional[RepositoryConfig] = None
        self._auth_store: Optional[AuthenticationStore] = None
        
        # Ensure data directory exists
        if not self._data_dir.exists():
            self._data_dir.mkdir(parents=True, exist_ok=True)

    def initialize(self) -> None:
        self._load_repository_config()
        self._build_index_from_disk()

    def get_repository_config(self) -> RepositoryConfig:
        if self._repository_config is None:
            return self._load_repository_config()
        return self._repository_config

    def save_repository_config(self, config: RepositoryConfig) -> None:
        self._repository_config = config
        config_path = self._data_dir / "repository.json"
        config_path.write_text(config.model_dump_json(indent=2), encoding="utf-8")

    def get_repository_index(self) -> RepositoryIndex:
        return self._repository_index

    def get_package(self, package_id: str) -> Optional[PackageIndex]:
        return self._repository_index.packages.get(package_id)

    def get_all_packages(self) -> List[PackageIndex]:
        return list(self._repository_index.packages.values())

    def save_package(self, package: PackageCommonMetadata) -> None:
        # Determine directory (owned vs cached)
        existing_pkg = self.get_package(package.package_identifier)
        if existing_pkg and existing_pkg.storage_path:
            pkg_dir = self._data_dir / existing_pkg.storage_path
        else:
            subdir = "cached" if package.cached else "owned"
            pkg_dir = self._data_dir / subdir / package.package_identifier
        
        pkg_dir.mkdir(parents=True, exist_ok=True)
        
        # Write package.json
        package_json_path = pkg_dir / "package.json"
        package_json_path.write_text(package.model_dump_json(indent=2, exclude_none=True), encoding="utf-8")
        
        # Update in-memory index
        if existing_pkg:
            existing_pkg.package = package
        else:
            # New package
            new_index = PackageIndex(
                package=package,
                versions=[],
                storage_path=str(pkg_dir.relative_to(self._data_dir))
            )
            self._repository_index.packages[package.package_identifier] = new_index

    def add_installer(self, package_id: str, installer: VersionMetadata, file_path: Optional[Path] = None) -> None:
        pkg_index = self.get_package(package_id)
        if not pkg_index:
            raise ValueError(f"Package {package_id} not found")

        # Generate GUID if not present
        if not installer.installer_guid:
            installer.installer_guid = str(uuid.uuid4())

        # Construct folder name: <version>-<arch>-<scope>-<guid>
        scope_part = installer.scope if installer.scope else "user" 
        folder_name = f"{installer.version}-{installer.architecture}-{scope_part}"
        if installer.installer_guid:
            folder_name += f"-{installer.installer_guid}"
        
        pkg_dir = self._data_dir / pkg_index.storage_path
        version_dir = pkg_dir / folder_name
        version_dir.mkdir(parents=True, exist_ok=True)

        # Handle file
        if file_path:
            target_filename = file_path.name
            installer.installer_file = target_filename
            shutil.copy2(file_path, version_dir / target_filename)

        # Save version.json
        version_json_path = version_dir / "version.json"
        installer.storage_path = str(version_dir.relative_to(self._data_dir))
        
        version_json_path.write_text(installer.model_dump_json(indent=2, exclude_none=True), encoding="utf-8")

        # Update in-memory index
        pkg_index.versions.append(installer)

    def update_installer(self, package_id: str, installer: VersionMetadata) -> None:
        pkg_index = self.get_package(package_id)
        if not pkg_index:
            raise ValueError(f"Package {package_id} not found")
            
        target_version = None
        for v in pkg_index.versions:
            if installer.installer_guid and v.installer_guid == installer.installer_guid:
                target_version = v
                break
            if (v.version == installer.version and 
                v.architecture == installer.architecture and 
                v.scope == installer.scope and
                v.installer_guid is None and installer.installer_guid is None):
                target_version = v
                break
        
        if not target_version:
             if installer.storage_path:
                 target_version = installer
             else:
                raise ValueError("Installer not found in index")

        if not target_version.storage_path:
             raise ValueError("Installer has no storage path")
             
        version_dir = self._data_dir / target_version.storage_path
        version_json_path = version_dir / "version.json"
        
        version_json_path.write_text(installer.model_dump_json(indent=2, exclude_none=True), encoding="utf-8")
        
        if installer is not target_version:
            try:
                idx = pkg_index.versions.index(target_version)
                pkg_index.versions[idx] = installer
            except ValueError:
                pass 

    def delete_installer(self, package_id: str, installer: VersionMetadata) -> None:
        pkg_index = self.get_package(package_id)
        if not pkg_index:
            raise ValueError(f"Package {package_id} not found")
            
        if not installer.storage_path:
             raise ValueError("Installer has no storage path")
             
        version_dir = self._data_dir / installer.storage_path
        if version_dir.exists():
            shutil.rmtree(version_dir)
            
        if installer in pkg_index.versions:
            pkg_index.versions.remove(installer)

    def delete_package(self, package_id: str) -> None:
        pkg_index = self.get_package(package_id)
        if not pkg_index:
            raise ValueError(f"Package {package_id} not found")
            
        if pkg_index.storage_path:
            pkg_dir = self._data_dir / pkg_index.storage_path
            if pkg_dir.exists():
                shutil.rmtree(pkg_dir)
        
        del self._repository_index.packages[package_id]

    def get_file_path(self, package_id: str, installer: VersionMetadata) -> Path:
        if not installer.storage_path:
             pkg = self.get_package(package_id)
             if pkg:
                 for v in pkg.versions:
                     if v.installer_guid == installer.installer_guid: 
                         installer.storage_path = v.storage_path
                         break
        
        if not installer.storage_path:
            raise ValueError("Storage path not found for installer")
            
        if not installer.installer_file:
            raise ValueError("Installer has no file defined")

        return self._data_dir / installer.storage_path / installer.installer_file

    def get_auth_store(self) -> AuthenticationStore:
        if self._auth_store:
            return self._auth_store
        
        path = self._data_dir / "authentication.json"
        if path.exists():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                store = AuthenticationStore(**raw)
            except Exception:
                store = AuthenticationStore()
        else:
            store = AuthenticationStore()
        
        self._auth_store = store
        return store

    def save_auth_store(self, store: AuthenticationStore) -> None:
        self._auth_store = store
        path = self._data_dir / "authentication.json"
        path.write_text(store.model_dump_json(by_alias=True, indent=2), encoding="utf-8")

    def _load_repository_config(self) -> RepositoryConfig:
        path = self._data_dir / "repository.json"
        if path.exists():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                config = RepositoryConfig(**raw)
            except Exception:
                config = RepositoryConfig()
        else:
            config = RepositoryConfig()

        config.installer_type_options = RepositoryConfig.model_fields["installer_type_options"].default_factory()
        config.nested_installer_type_options = RepositoryConfig.model_fields["nested_installer_type_options"].default_factory()
        
        path.write_text(config.model_dump_json(indent=2), encoding="utf-8")
        self._repository_config = config
        return config

    def _build_index_from_disk(self) -> None:
        index = RepositoryIndex()
        
        owned_dir = self._data_dir / "owned"
        cached_dir = self._data_dir / "cached"
        
        for scan_dir in [owned_dir, cached_dir]:
            if not scan_dir.exists():
                continue
            
            for pkg_dir in scan_dir.iterdir():
                if not pkg_dir.is_dir():
                    continue
                
                package_json = pkg_dir / "package.json"
                if not package_json.exists():
                    continue
                
                try:
                    raw = json.loads(package_json.read_text(encoding="utf-8"))
                    if "package_identifier" not in raw and "package_id" in raw:
                        raw["package_identifier"] = raw.pop("package_id")
                    pkg_meta = PackageCommonMetadata(**raw)
                except Exception:
                    continue

                if pkg_meta.package_identifier == "our.example": 
                    continue

                package_index = PackageIndex(
                    package=pkg_meta,
                    versions=[],
                    storage_path=str(pkg_dir.relative_to(self._data_dir))
                )

                for version_dir in pkg_dir.iterdir():
                    if not version_dir.is_dir():
                        continue
                    if version_dir.name in ["x86", "x64", "arm"]:
                        continue # legacy

                    version_json = version_dir / "version.json"
                    if not version_json.exists():
                        continue
                    
                    try:
                        raw = json.loads(version_json.read_text(encoding="utf-8"))
                    except Exception:
                        continue
                    
                    folder_version = folder_arch = folder_scope = None
                    parts = version_dir.name.split("-")
                    
                    if len(parts) >= 3:
                        pass

                    try:
                        version_meta = VersionMetadata(**raw)
                    except Exception:
                        continue

                    version_meta.storage_path = str(version_dir.relative_to(self._data_dir))
                    package_index.versions.append(version_meta)

                index.packages[pkg_meta.package_identifier] = package_index
        
        index.last_built_at = datetime.utcnow()
        self._repository_index = index
