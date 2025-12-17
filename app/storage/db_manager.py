from abc import ABC, abstractmethod
from typing import Optional
from pathlib import Path
from app.domain.models import (
    PackageCommonMetadata, 
    PackageIndex, 
    RepositoryConfig, 
    VersionMetadata,
    RepositoryIndex,
    AuthenticationStore
)

class DatabaseManager(ABC):
    """
    Abstract base class for storage/database management.
    """

    @abstractmethod
    def initialize(self) -> None:
        """Initialize the storage subsystem (e.g. load from disk)."""
        pass

    @abstractmethod
    def get_repository_config(self) -> RepositoryConfig:
        """Retrieve repository configuration."""
        pass

    @abstractmethod
    def save_repository_config(self, config: RepositoryConfig) -> None:
        """Save repository configuration."""
        pass

    @abstractmethod
    def get_repository_index(self) -> RepositoryIndex:
        """Get the full repository index."""
        pass

    @abstractmethod
    def get_package(self, package_id: str) -> Optional[PackageIndex]:
        """Get a specific package and its versions by ID."""
        pass

    @abstractmethod
    def save_package(self, package: PackageCommonMetadata) -> None:
        """Save package metadata (create or update)."""
        pass

    @abstractmethod
    def add_installer(self, package_id: str, installer: VersionMetadata, file_path: Optional[Path] = None) -> None:
        """
        Add a new installer (VersionMetadata) to a package.
        If file_path is provided, the storage manager handles copying/storing the file.
        """
        pass
    
    @abstractmethod
    def update_installer(self, package_id: str, installer: VersionMetadata) -> None:
        """Update metadata for an existing installer."""
        pass

    @abstractmethod
    def delete_installer(self, package_id: str, installer: VersionMetadata) -> None:
        """Delete an installer (metadata and files)."""
        pass

    @abstractmethod
    def delete_package(self, package_id: str) -> None:
        """Delete a package and all its versions."""
        pass

    @abstractmethod
    def get_file_path(self, package_id: str, installer: VersionMetadata) -> Path:
        """
        Get the absolute path to the installer file on disk (or temporary location).
        Required for serving downloads.
        """
        pass

    @abstractmethod
    def get_auth_store(self) -> AuthenticationStore:
        """Get the authentication store."""
        pass

    @abstractmethod
    def save_auth_store(self, store: AuthenticationStore) -> None:
        """Save the authentication store."""
        pass

