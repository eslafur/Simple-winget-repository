"""
Download and extract the WinGet repository index.
"""
from __future__ import annotations

import asyncio
import shutil
import zipfile
from pathlib import Path
import httpx
import aiofiles


WINGET_BASE_URL = "https://cdn.winget.microsoft.com/cache"
INDEX_PACKAGE_V2 = "source2.msix"
INDEX_PACKAGE_V1 = "source.msix"
INDEX_DB_PATH = "Public/index.db"


async def download_winget_index(cache_dir: Path) -> Path:
    """
    Download and extract the WinGet index MSIX package.
    
    Args:
        cache_dir: Directory to store the cached index
    
    Returns:
        Path to the extracted index.db file
    """
    index_dir = cache_dir / "winget_index"
    index_dir.mkdir(parents=True, exist_ok=True)

    # Always write the extracted DB to a stable location.
    index_db_path = index_dir / "index.db"
    
    # Try source2.msix first, fallback to source.msix
    for package_name in [INDEX_PACKAGE_V2, INDEX_PACKAGE_V1]:
        package_url = f"{WINGET_BASE_URL}/{package_name}"
        package_path = index_dir / package_name
        package_tmp_path = index_dir / f"{package_name}.tmp"
        
        try:
            print(f"Downloading {package_name}...")
            # Download to a temp file first to avoid leaving partial/corrupt MSIX behind.
            if package_tmp_path.exists():
                package_tmp_path.unlink()

            # Basic retry loop for flaky connections (seen on Windows sometimes).
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
                                        print(f"\rProgress: {percent:.1f}%", end="", flush=True)
                    last_error = None
                    break
                except Exception as e:
                    last_error = e
                    # Clean up temp file and retry.
                    if package_tmp_path.exists():
                        package_tmp_path.unlink(missing_ok=True)
                    if attempt < 3:
                        print(f"\nDownload failed (attempt {attempt}/3): {e}. Retrying...")
                        await asyncio.sleep(1.0 * attempt)
                    else:
                        raise

            if last_error:
                raise last_error

            # Move temp file into place.
            if package_path.exists():
                package_path.unlink(missing_ok=True)
            package_tmp_path.replace(package_path)
            
            print(f"\nExtracting index.db from {package_name}...")
            
            # Extract index.db from MSIX (MSIX is a ZIP file). Instead of extracting to
            # "Public/index.db" and renaming (which is fragile on Windows when files
            # already exist), write directly to index_db_path.
            with zipfile.ZipFile(package_path, "r") as zip_ref:
                if INDEX_DB_PATH not in zip_ref.namelist():
                    print(f"Warning: {INDEX_DB_PATH} not found in {package_name}")
                    continue

                # Ensure target doesn't exist / isn't locked.
                if index_db_path.exists():
                    index_db_path.unlink(missing_ok=True)

                with zip_ref.open(INDEX_DB_PATH, "r") as src, open(index_db_path, "wb") as dst:
                    shutil.copyfileobj(src, dst)

            print(f"Index database extracted to: {index_db_path}")

            # Best-effort cleanup of legacy extracted folder.
            public_dir = index_dir / "Public"
            if public_dir.exists():
                shutil.rmtree(public_dir, ignore_errors=True)

            return index_db_path
                    
        except Exception as e:
            print(f"Failed to download {package_name}: {e}")
            if package_tmp_path.exists():
                package_tmp_path.unlink(missing_ok=True)
            if package_path.exists():
                package_path.unlink(missing_ok=True)
            continue
    
    raise Exception("Failed to download and extract index from both source2.msix and source.msix")


if __name__ == "__main__":
    import sys
    
    if len(sys.argv) > 1:
        cache_dir = Path(sys.argv[1])
    else:
        # Default to data/cache
        cache_dir = Path(__file__).resolve().parents[2] / "data" / "cache"
    
    print(f"Downloading WinGet index to: {cache_dir}")
    
    try:
        index_path = asyncio.run(download_winget_index(cache_dir))
        print(f"\nSuccess! Index database available at: {index_path}")
    except Exception as e:
        print(f"\nError: {e}")
        sys.exit(1)
