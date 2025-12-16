import logging
import asyncio
from datetime import datetime, timedelta
from pathlib import Path
import json
from typing import Optional, List, Dict, Any

from app.storage.db_manager import DatabaseManager
from app.services.importer.package_importer import WinGetPackageImporter
from app.services.importer.index_downloader import download_winget_index
from app.services.importer.winget_index import WinGetIndexReader
from app.domain.models import PackageCommonMetadata, ADGroupScopeEntry

logger = logging.getLogger(__name__)

class CachingService:
    def __init__(self, db_manager: DatabaseManager):
        self.db = db_manager
        # Assuming data dir is parent of auth store path as a heuristic or using dependency logic
        # We really should have get_data_dir() in DB interface or similar.
        # reusing the import from dependencies for now
        from app.core.dependencies import get_data_dir
        self.data_dir = get_data_dir()
        self.cache_dir = self.data_dir / "cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.index_path = self.cache_dir / "winget_index" / "index.db"
        if not self.index_path.exists():
            self.index_path = self.cache_dir / "index.db"

    def _get_status_path(self) -> Path:
        return self.cache_dir / "winget_index_status.json"

    def _update_status(self, last_pulled: datetime = None):
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
        """Force update the WinGet index."""
        try:
            path = await download_winget_index(self.cache_dir)
            self.index_path = path # Update current path reference
            self._update_status(last_pulled=datetime.now())
            return path
        except Exception as e:
            logger.error(f"Failed to update index: {e}")
            raise

    def search_upstream_packages(self, query: str, limit: int = 50) -> List[Dict[str, Any]]:
        """Search for packages in the upstream WinGet index."""
        if not self.index_path.exists():
            return []
            
        try:
            with WinGetIndexReader(self.index_path) as reader:
                return reader.search_packages(query, limit)
        except Exception as e:
            logger.error(f"Search failed: {e}")
            raise

    async def get_upstream_package_versions(
        self, 
        package_id: str, 
        architecture: Optional[str] = None, 
        scope: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Get available versions for a package from upstream."""
        if not self.index_path.exists():
            return []
            
        try:
            # We use reader mainly for connection, but get_package_versions_async handles network
            # It's cleaner to instantiate it.
            reader = WinGetIndexReader(self.index_path)
            # No context manager here because get_package_versions_async handles its stuff,
            # but reader needs to connect to DB to get hash.
            try:
                reader.connect()
                # get_package_versions_async only returns list of versions.
                # If we want to filter by arch/scope, we might need to inspect the manifests?
                # Actually V2 index structure is complex.
                # The importer logic downloads manifests and parses them.
                # For a simple list, we just return the version strings if possible.
                # BUT the V2 index DOES NOT have a simple "list versions" table.
                # It has 'packages' table with 'latest_version'.
                # To get all versions, we must fetch the data manifest.
                
                # Our implementation of get_package_versions_async returns [{"version": "...", ...}].
                # It does NOT filter by arch/scope because the manifest structure lists versions,
                # and arch/scope are inside the installer definitions within those versions.
                # So we can only return the list of versions.
                
                versions = await reader.get_package_versions_async(package_id)
                return versions
            finally:
                reader.close()
        except Exception as e:
            logger.error(f"Get versions failed: {e}")
            raise

    async def import_package(
        self,
        package_id: str,
        architectures: Optional[List[str]] = None,
        scopes: Optional[List[str]] = None,
        installer_types: Optional[List[str]] = None,
        version_mode: str = "latest",
        version_filter: Optional[str] = None,
        ad_group_scopes: Optional[List[ADGroupScopeEntry]] = None,
    ) -> Dict[str, Any]:
        """Import a package from upstream."""
        if not self.index_path.exists():
            raise FileNotFoundError("WinGet index not found")
            
        importer = WinGetPackageImporter(self.db, self.index_path)
        try:
            return await importer.import_package(
                package_id,
                architectures=architectures,
                scopes=scopes,
                installer_types=installer_types,
                version_mode=version_mode,
                version_filter=version_filter,
                track_cache=True,
                ad_group_scopes=ad_group_scopes
            )
        finally:
            importer.close()

    async def update_cached_packages(self):
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

        importer = WinGetPackageImporter(self.db, self.index_path)
        
        try:
            all_packages = self.db.get_all_packages()
            
            for pkg_index in all_packages:
                pkg = pkg_index.package
                if not pkg.cached or not pkg.cache_settings or not pkg.cache_settings.auto_update:
                    continue
                
                logger.info(f"Checking updates for {pkg.package_identifier}")
                
                try:
                    upstream_info = importer.index_reader.find_package_by_id(pkg.package_identifier)
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
                    
                    await importer.import_package(
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
                    
        finally:
            importer.close()
            
        logger.info("Cached packages update completed")
        
    async def run_periodic_updates(self, run_hour: int = 6, run_minute: int = 0):
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
