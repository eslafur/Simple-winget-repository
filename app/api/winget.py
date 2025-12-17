from __future__ import annotations

from typing import Dict, List, Optional
import logging

from fastapi import APIRouter, HTTPException, Query, Request, Response, status, Depends
from fastapi.responses import FileResponse, JSONResponse

from app.core.dependencies import get_repository
from app.domain.entities import Repository, Package
from app.domain.models import ManifestSearchRequest
from app.domain.winget_utils import strip_nulls

logger = logging.getLogger(__name__)
router = APIRouter()

# ---------------------------------------------------------------------------
# 1. GET /information
# ---------------------------------------------------------------------------

@router.get("/information")
async def get_information(repo: Repository = Depends(get_repository)) -> dict:
    """
    WinGet REST source `/information` endpoint.
    """
    config = repo.db.get_repository_config()

    source_agreements = None
    if config.source_agreements is not None:
        source_agreements = {
            "AgreementsIdentifier": config.source_agreements.agreements_identifier,
            "Agreements": [
                {
                    "AgreementLabel": a.agreement_label,
                    "Agreement": a.agreement,
                    "AgreementUrl": a.agreement_url,
                }
                for a in config.source_agreements.agreements
            ],
        }

    data = {
        "SourceIdentifier": config.source_identifier,
        "SourceAgreements": source_agreements,
        "ServerSupportedVersions": config.server_supported_versions,
        "UnsupportedPackageMatchFields": config.unsupported_package_match_fields,
        "RequiredPackageMatchFields": config.required_package_match_fields,
        "UnsupportedQueryParameters": config.unsupported_query_parameters,
        "RequiredQueryParameters": config.required_query_parameters,
        "Authentication": {
            "AuthenticationType": config.authentication.authentication_type,
            "MicrosoftEntraIdAuthenticationInfo": config.authentication.microsoft_entra_id_authentication_info,
        },
    }

    return {
        "Data": strip_nulls(data),
        "ContinuationToken": None,
    }


# ---------------------------------------------------------------------------
# 2. POST /manifestSearch
# ---------------------------------------------------------------------------

@router.post("/manifestSearch")
async def manifest_search(
    body: ManifestSearchRequest, 
    repo: Repository = Depends(get_repository)
) -> Response:
    """
    WinGet REST `/manifestSearch` endpoint.
    """
    results = repo.search_packages(body)
    config = repo.db.get_repository_config()

    if not results:
        # Per spec: 204 No Content when there are no results.
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    # Apply simple maximum limit if requested.
    if body.MaximumResults is not None and body.MaximumResults > 0:
        results = results[: body.MaximumResults]

    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={
            "Data": results,
            "ContinuationToken": None,
            "RequiredPackageMatchFields": config.required_package_match_fields,
            "UnsupportedPackageMatchFields": config.unsupported_package_match_fields,
        },
    )


# ---------------------------------------------------------------------------
# 3. GET /packageManifests/{PackageIdentifier}
# ---------------------------------------------------------------------------

@router.get("/packageManifests/{package_id}")
async def get_package_manifests(
    package_id: str, 
    request: Request,
    repo: Repository = Depends(get_repository)
) -> dict:
    """
    WinGet REST `/packageManifests/{PackageIdentifier}` endpoint.
    """
    pkg = repo.get_package(package_id)
    if not pkg:
        raise HTTPException(status_code=404, detail="Package not found")

    base_url = str(request.base_url).rstrip("/")
    data = pkg.get_manifest(base_url)
    
    config = repo.db.get_repository_config()

    return {
        "Data": data, # strip_nulls is called inside get_manifest
        "ContinuationToken": None,
        "UnsupportedQueryParameters": config.unsupported_query_parameters,
        "RequiredQueryParameters": config.required_query_parameters,
    }


# ---------------------------------------------------------------------------
# 4. Installer download (internal helper used by manifests)
# ---------------------------------------------------------------------------

@router.get("/packages/{package_id}/versions/{installer_id}/installer")
async def download_installer(
    package_id: str, 
    installer_id: str,
    repo: Repository = Depends(get_repository)
) -> FileResponse:
    """
    Download endpoint that serves the installer file.
    installer_id is <version>-<architecture>-<scope>[-<guid>]
    """
    pkg = repo.get_package(package_id)
    if not pkg:
        raise HTTPException(status_code=404, detail="Package not found")

    # We need to find the specific version/installer instance from the ID.
    # The ID was constructed in Package.get_manifest as: "-".join(parts)
    # parts = [v.version, v.architecture, v.scope, v.installer_guid] (if guid exists)
    
    # Brute force search in versions to find match
    # Since version string might contain dashes, simply splitting installer_id is risky.
    # But we can reconstruct the ID for each version and check equality.
    
    target_version = None
    for v in pkg.versions:
        parts = [v.version, v.architecture]
        parts.append(v.scope if v.scope else "user")
        if v.installer_guid:
            parts.append(v.installer_guid)
        
        constructed_id = "-".join(parts)
        if constructed_id == installer_id:
            target_version = v
            break
            
    # Fallback for legacy IDs without GUID if not found above?
    # If the URL was generated by our new code, it matches the construction.
    # If it was generated by old code (cached manifest), it might be missing GUID.
    if not target_version:
         for v in pkg.versions:
            parts = [v.version, v.architecture]
            parts.append(v.scope if v.scope else "user")
            # Try without GUID
            constructed_id = "-".join(parts)
            if constructed_id == installer_id:
                target_version = v
                break

    if not target_version:
        raise HTTPException(status_code=404, detail="Version/Installer not found")

    if not target_version.installer_file:
         raise HTTPException(status_code=404, detail="Installer file is not defined for this version")

    try:
        # Use get_installer_path to determine correct file to serve
        # For custom installers, this logic is now encapsulated there.
        served_path = pkg.get_installer_path(target_version)
    except ValueError:
         raise HTTPException(status_code=500, detail="Storage path not available")

    if not served_path.is_file():
        raise HTTPException(status_code=404, detail="Installer file not found on disk")
    
    return FileResponse(
        path=str(served_path),
        filename=served_path.name,
        media_type="application/octet-stream",
    )
