"""
Background task for automatically updating cached WinGet packages.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from pathlib import Path

from app.data.cached_packages import get_cached_packages_store
from app.data.winget_importer import WinGetPackageImporter
from app.data.repository import build_index_from_disk, get_data_dir, get_repository_index
from app.data.winget_index_downloader import download_winget_index

logger = logging.getLogger(__name__)


def _get_winget_index_path() -> Path:
    """Get the path to the WinGet index database."""
    data_dir = get_data_dir()
    cache_dir = data_dir / "cache"
    index_path = cache_dir / "winget_index" / "index.db"
    
    if not index_path.exists():
        index_path = cache_dir / "index.db"
    
    return index_path


def _version_key(v: str) -> tuple:
    """
    Convert a version string into a sortable tuple.
    This mirrors the simple logic used elsewhere in the importer.
    """
    v_str = str(v) if v is not None else ""
    parts = []
    for part in v_str.replace("-", ".").split("."):
        try:
            parts.append((0, int(part)))
        except ValueError:
            parts.append((1, part))
    return tuple(parts)


def _get_local_latest_version(package_id: str) -> str | None:
    pkg_index = get_repository_index().packages.get(package_id)
    if not pkg_index or not pkg_index.versions:
        return None
    versions = [v.version for v in pkg_index.versions if v.version]
    if not versions:
        return None
    versions.sort(key=_version_key, reverse=True)
    return versions[0]


async def update_cached_packages_if_needed() -> None:
    """
    Daily job:
    - download the newest WinGet index
    - compare upstream latest_version vs local latest cached version
    - re-import only packages that changed (with filters applied)
    """
    # Make sure we see any on-disk changes (external edits) before comparing.
    build_index_from_disk()

    cache_store = get_cached_packages_store()
    cached_packages = [pkg for pkg in cache_store.get_all() if pkg.cache_settings.auto_update]
    
    if not cached_packages:
        return

    data_dir = get_data_dir()
    cache_dir = data_dir / "cache"

    # Always pull the latest index at the start of the daily run.
    try:
        index_path = await download_winget_index(cache_dir)
    except Exception as e:
        logger.error(f"Failed to download WinGet index: {e}")
        # Fall back to existing index if present.
        index_path = _get_winget_index_path()
        if not index_path.exists():
            return

    importer = WinGetPackageImporter(index_path)
    
    try:
        for cached_pkg in cached_packages:
            try:
                upstream_info = importer.index_reader.find_package_by_id(cached_pkg.package_id)
                upstream_latest = None
                if upstream_info:
                    upstream_latest_raw = upstream_info.get("latest_version")
                    upstream_latest = str(upstream_latest_raw) if upstream_latest_raw is not None else None

                local_latest = _get_local_latest_version(cached_pkg.package_id)

                # If we can't determine upstream latest, skip (no signal).
                if not upstream_latest:
                    continue

                # Only import when the upstream latest differs from what we have cached.
                if local_latest == upstream_latest:
                    continue

                # Empty lists mean "all" (no filter), so pass None to importer
                await importer.import_package(
                    cached_pkg.package_id,
                    architectures=cached_pkg.cache_settings.architectures if cached_pkg.cache_settings.architectures else None,
                    scopes=cached_pkg.cache_settings.scopes if cached_pkg.cache_settings.scopes else None,
                    installer_types=cached_pkg.cache_settings.installer_types if cached_pkg.cache_settings.installer_types else None,
                    version_mode=cached_pkg.cache_settings.version_mode,
                    version_filter=cached_pkg.cache_settings.version_filter,
                    track_cache=True
                )
            except Exception as e:
                # Log error but continue with other packages
                logger.error(f"Failed to update cached package {cached_pkg.package_id}: {e}")
        
        # Rebuild index after all updates
        build_index_from_disk()
    finally:
        importer.close()


def _seconds_until(hour: int, minute: int) -> float:
    """
    Compute seconds until the next occurrence of the given local wall-clock time.
    """
    now = datetime.now()
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target = target + timedelta(days=1)
    return (target - now).total_seconds()


async def daily_update_loop(run_hour: int = 6, run_minute: int = 0):
    """
    Run the cached-package updater job every day at a fixed local time (default 06:00).
    """
    while True:
        await asyncio.sleep(_seconds_until(run_hour, run_minute))
        try:
            await update_cached_packages_if_needed()
        except Exception as e:
            logger.error(f"Error in daily update loop: {e}")

