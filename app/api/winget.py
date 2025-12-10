from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse

from app.data.repository import (
    get_data_dir,
    get_repository_config,
    get_repository_index,
)


router = APIRouter()


@router.get("/information")
async def get_information() -> dict:
    """
    Repository metadata endpoint.

    This corresponds to the winget REST source `/information` endpoint.
    It is built from the JSON-backed repository configuration.
    """
    config = get_repository_config()
    return {
        "SourceIdentifier": config.source_identifier,
        "ServerSupportedVersions": ["1.0.0"],
    }


@router.get("/packages")
async def list_packages(
    q: str | None = Query(
        default=None,
        description="Optional search term to match against identifier, name, or tags.",
    ),
) -> dict:
    """
    List or search packages.

    Basic search over the in-memory index.
    """
    index = get_repository_index()

    items: list[dict] = []
    query = (q or "").strip().lower()

    for package_id, pkg_index in index.packages.items():
        pkg = pkg_index.package

        if query:
            haystack = " ".join(
                [
                    package_id,
                    pkg.package_name or "",
                    pkg.publisher or "",
                    " ".join(pkg.tags or []),
                ]
            ).lower()
            if query not in haystack:
                continue

        # For now, return a minimal projection suitable for discovery.
        items.append(
            {
                "PackageIdentifier": package_id,
                "PackageName": pkg.package_name,
                "Publisher": pkg.publisher,
                "Tags": pkg.tags,
                "Versions": sorted(
                    {v.version for v in pkg_index.versions if v.version},
                    reverse=True,
                ),
            }
        )

    return {
        "Items": items,
        "TotalCount": len(items),
    }


@router.get("/packages/{package_id}")
async def get_package(package_id: str) -> dict:
    """
    Get high-level information about a single package.
    """
    index = get_repository_index()
    pkg_index = index.packages.get(package_id)
    if not pkg_index:
        raise HTTPException(status_code=404, detail="Package not found")

    return {
        "PackageIdentifier": package_id,
        "PackageName": pkg_index.package.package_name,
        "Publisher": pkg_index.package.publisher,
        "Versions": sorted(
            {v.version for v in pkg_index.versions if v.version},
            reverse=True,
        ),
    }


@router.get("/packages/{package_id}/versions")
async def list_package_versions(package_id: str) -> dict:
    """
    List all versions of a package.
    """
    index = get_repository_index()
    pkg_index = index.packages.get(package_id)
    if not pkg_index:
        raise HTTPException(status_code=404, detail="Package not found")

    versions = sorted(
        {
            v.version
            for v in pkg_index.versions
            if v.version
        },
        reverse=True,
    )

    return {
        "PackageIdentifier": package_id,
        "Versions": versions,
    }


@router.get("/packages/{package_id}/versions/{version}")
async def get_package_version(package_id: str, version: str) -> dict:
    """
    Get information about a specific version of a package.
    """
    index = get_repository_index()
    pkg_index = index.packages.get(package_id)
    if not pkg_index:
        raise HTTPException(status_code=404, detail="Package not found")

    matching_versions = [
        v for v in pkg_index.versions if v.version == version
    ]
    if not matching_versions:
        raise HTTPException(status_code=404, detail="Version not found")

    manifests: list[dict] = []
    for v in matching_versions:
        manifests.append(
            {
                "PackageIdentifier": package_id,
                "PackageVersion": v.version,
                "Architecture": v.architecture,
                "InstallerType": v.installer_type,
                "Scope": v.scope,
            }
        )

    return {
        "PackageIdentifier": package_id,
        "Version": version,
        "Manifests": manifests,
    }


@router.get("/packages/{package_id}/versions/{version}/manifest")
async def get_package_manifest(package_id: str, version: str) -> dict:
    """
    Get the manifest for a specific version of a package.
    """
    index = get_repository_index()
    pkg_index = index.packages.get(package_id)
    if not pkg_index:
        raise HTTPException(status_code=404, detail="Package not found")

    # For now we assume a singleton-style manifest built from our version metadata.
    matching_versions = [
        v for v in pkg_index.versions if v.version == version
    ]
    if not matching_versions:
        raise HTTPException(status_code=404, detail="Version not found")

    # Use the first match; in a more advanced repo you might distinguish by arch/scope
    # in the URL, but here we keep the API simple.
    v = matching_versions[0]

    installers: list[dict] = [
        {
            "Architecture": v.architecture,
            "InstallerType": v.installer_type,
            "Scope": v.scope,
            "InstallerSha256": v.installer_sha256,
            "InstallerUrl": f"/winget/packages/{package_id}/versions/{version}/installer",
            "InstallerFile": v.installer_file,
            "InstallerSilent": v.silent_arguments,
            "InstallerInteractive": v.interactive_arguments,
            "InstallerLog": v.log_arguments,
        }
    ]

    manifest: dict = {
        "PackageIdentifier": package_id,
        "PackageVersion": version,
        "PackageName": pkg_index.package.package_name,
        "Publisher": pkg_index.package.publisher,
        "ShortDescription": pkg_index.package.short_description,
        "License": pkg_index.package.license,
        "Tags": pkg_index.package.tags,
        "ManifestType": "singleton",
        "ManifestVersion": "1.6.0",
        "Installers": installers,
    }

    if v.release_date is not None:
        manifest["ReleaseDate"] = v.release_date.isoformat()
    if v.release_notes:
        manifest["ReleaseNotes"] = v.release_notes

    return manifest


@router.get("/packages/{package_id}/versions/{version}/installer")
async def download_installer(package_id: str, version: str) -> FileResponse:
    """
    Download endpoint that serves the installer file for a given package version.

    The installer file is expected to live alongside the version.json file
    inside the version's directory.
    """
    index = get_repository_index()
    pkg_index = index.packages.get(package_id)
    if not pkg_index:
        raise HTTPException(status_code=404, detail="Package not found")

    matching_versions = [
        v for v in pkg_index.versions if v.version == version
    ]
    if not matching_versions:
        raise HTTPException(status_code=404, detail="Version not found")

    v = matching_versions[0]

    if not v.installer_file:
        raise HTTPException(
            status_code=404,
            detail="Installer file is not defined for this version",
        )
    if not v.storage_path:
        raise HTTPException(
            status_code=500,
            detail="Version storage path is not available",
        )

    data_dir = get_data_dir()
    installer_path = data_dir / v.storage_path / v.installer_file

    if not installer_path.is_file():
        raise HTTPException(
            status_code=404,
            detail="Installer file not found on disk",
        )

    # Let FastAPI/Uvicorn stream the file efficiently.
    return FileResponse(
        path=str(installer_path),
        filename=v.installer_file,
        media_type="application/octet-stream",
    )



