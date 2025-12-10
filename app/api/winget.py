from fastapi import APIRouter


router = APIRouter()


@router.get("/information")
async def get_information() -> dict:
    """
    Repository metadata endpoint.

    This corresponds to the winget REST source `/information` endpoint.
    For now it returns placeholder data suitable for initial wiring.
    """
    return {
        "SourceIdentifier": "python-winget-repo",
        "ServerSupportedVersions": ["1.0.0"],
    }


@router.get("/packages")
async def list_packages() -> dict:
    """
    List or search packages.

    In the full implementation this would support filters and paging
    using query parameters as per the official winget REST spec.
    """
    # Placeholder shape; real implementation should follow Microsoft's schema.
    return {
        "Items": [],
        "TotalCount": 0,
    }


@router.get("/packages/{package_id}")
async def get_package(package_id: str) -> dict:
    """
    Get high-level information about a single package.
    """
    return {
        "PackageIdentifier": package_id,
        "Versions": [],
    }


@router.get("/packages/{package_id}/versions")
async def list_package_versions(package_id: str) -> dict:
    """
    List all versions of a package.
    """
    return {
        "PackageIdentifier": package_id,
        "Versions": [],
    }


@router.get("/packages/{package_id}/versions/{version}")
async def get_package_version(package_id: str, version: str) -> dict:
    """
    Get information about a specific version of a package.
    """
    return {
        "PackageIdentifier": package_id,
        "Version": version,
        "Manifests": [],
    }


@router.get("/packages/{package_id}/versions/{version}/manifest")
async def get_package_manifest(package_id: str, version: str) -> dict:
    """
    Get the manifest for a specific version of a package.
    """
    return {
        "PackageIdentifier": package_id,
        "Version": version,
        "ManifestType": "singleton",
        "ManifestVersion": "1.6.0",
        "Installers": [],
    }



