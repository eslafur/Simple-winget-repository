from __future__ import annotations

from typing import Dict, List, Optional
import hashlib
import re

from fastapi import APIRouter, HTTPException, Query, Request, Response, status
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from app.data.repository import (
    get_data_dir,
    get_repository_config,
    get_repository_index,
)


router = APIRouter()


def _strip_nulls(value):
    """
    Recursively remove keys with value None from dictionaries.

    Lists are preserved, but their elements are also cleaned.
    """
    if isinstance(value, dict):
        return {k: _strip_nulls(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [_strip_nulls(v) for v in value]
    return value


def _compute_installer_sha256(
    installer_file: Optional[str],
    storage_path: Optional[str],
) -> Optional[str]:
    """
    Best-effort computation of the installer SHA256 for a given version
    from the on-disk installer file.

    Returns the hex digest, or None if the file is missing or not readable.
    """
    if not installer_file or not storage_path:
        return None

    data_dir = get_data_dir()
    installer_path = data_dir / storage_path / installer_file

    if not installer_path.is_file():
        return None

    h = hashlib.sha256()
    try:
        with installer_path.open("rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                h.update(chunk)
    except OSError:
        return None

    return h.hexdigest()


# ---------------------------------------------------------------------------
# 1. GET /information
# ---------------------------------------------------------------------------


@router.get("/information")
async def get_information() -> dict:
    """
    WinGet REST source `/information` endpoint.
    """
    config = get_repository_config()

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
        "Data": _strip_nulls(data),
        "ContinuationToken": None,
    }


# ---------------------------------------------------------------------------
# 2. POST /manifestSearch
# ---------------------------------------------------------------------------


class RequestMatch(BaseModel):
    KeyWord: Optional[str] = None
    MatchType: Optional[str] = None


class PackageMatchFilter(BaseModel):
    PackageMatchField: str
    RequestMatch: RequestMatch


class ManifestSearchRequest(BaseModel):
    MaximumResults: Optional[int] = None
    FetchAllManifests: Optional[bool] = None
    Query: Optional[RequestMatch] = None
    Inclusions: List[PackageMatchFilter] = []
    Filters: List[PackageMatchFilter] = []


def _match_text(value: str, keyword: str, match_type: Optional[str]) -> bool:
    """
    Apply WinGet-style text matching rules to a single value.
    """
    if keyword is None:
        return False
    keyword = keyword or ""
    match = (match_type or "Substring").strip() or "Substring"

    # Exact is case-sensitive; everything else we treat as case-insensitive.
    if match == "Exact":
        return value == keyword

    v = value.lower()
    k = keyword.lower()

    if match in ("CaseInsensitive",):
        return v == k
    if match == "StartsWith":
        return v.startswith(k)
    if match in ("Substring", "Fuzzy", "FuzzySubstring"):
        return k in v
    if match == "Wildcard":
        # Very simple wildcard support: * and ?
        pattern = "^" + re.escape(keyword).replace(r"\*", ".*").replace(r"\?", ".") + "$"
        return re.search(pattern, value, flags=re.IGNORECASE) is not None

    # Fallback: case-insensitive substring
    return k in v


def _values_for_field(
    field: str,
    package_id: str,
    pkg_index,
) -> List[str]:
    """
    Return the list of string values for a given PackageMatchField for a package.

    We only support a subset of fields; unsupported fields return an empty list,
    which will cause filters using them to fail to match.
    """
    pkg = pkg_index.package
    field = field or ""

    if field == "PackageIdentifier":
        return [package_id]
    if field == "PackageName":
        return [pkg.package_name] if pkg.package_name else []
    if field == "Moniker":
        # Not currently modeled.
        return []
    if field == "Command":
        # Not currently modeled.
        return []
    if field == "Tag":
        return pkg.tags or []
    if field == "PackageFamilyName":
        # Not currently modeled.
        return []
    if field == "ProductCode":
        vals: List[str] = []
        for v in pkg_index.versions:
            code = getattr(v, "product_code", None)
            if code:
                vals.append(code)
        return vals
    if field == "UpgradeCode":
        # Not currently modeled.
        return []
    if field == "NormalizedPackageNameAndPublisher":
        # Explicitly unsupported per repository configuration.
        return []
    if field == "Market":
        # Not modeled at all in this simple repository.
        return []
    if field == "HasInstallerType":
        # Could be implemented, but we don't use it today.
        return []

    # Unknown / unsupported field.
    return []


def _package_matches_filter(
    package_id: str,
    pkg_index,
    flt: PackageMatchFilter,
) -> bool:
    """
    Evaluate a single PackageMatchFilter against a package.
    """
    if not flt or not flt.RequestMatch:
        return True

    keyword = flt.RequestMatch.KeyWord or ""
    match_type = flt.RequestMatch.MatchType

    values = _values_for_field(flt.PackageMatchField, package_id, pkg_index)
    if not values:
        return False

    for v in values:
        if _match_text(str(v), keyword, match_type):
            return True
    return False


def _package_matches_query(
    package_id: str,
    pkg_index,
    query: Optional[RequestMatch],
) -> bool:
    """
    Apply the top-level Query.RequestMatch to a package.

    Per spec, this matches against source-defined fields; here we use
    identifier, name, publisher and tags as the searchable surface.
    """
    if not query or not query.KeyWord:
        return False

    keyword = query.KeyWord
    match_type = query.MatchType

    pkg = pkg_index.package
    candidates = [
        package_id,
        pkg.package_name or "",
        pkg.publisher or "",
        *(pkg.tags or []),
    ]

    for value in candidates:
        if _match_text(value, keyword, match_type):
            return True
    return False


@router.post("/manifestSearch")
async def manifest_search(body: ManifestSearchRequest) -> Response:
    """
    WinGet REST `/manifestSearch` endpoint.

    We implement a simplified search that uses only the main Query.KeyWord
    against the identifier, name, publisher and tags.
    """
    index = get_repository_index()
    config = get_repository_config()

    # Step 1: determine the starting set of packages.
    all_ids = list(index.packages.keys())

    if body.FetchAllManifests:
        candidate_ids = set(all_ids)
    else:
        candidate_ids: set[str] = set()

        # Query across source-defined fields.
        if body.Query and body.Query.KeyWord:
            for package_id, pkg_index in index.packages.items():
                if _package_matches_query(package_id, pkg_index, body.Query):
                    candidate_ids.add(package_id)

        # Inclusions: OR-ed with Query results.
        for inc in body.Inclusions or []:
            for package_id, pkg_index in index.packages.items():
                if _package_matches_filter(package_id, pkg_index, inc):
                    candidate_ids.add(package_id)

        # If neither Query nor Inclusions were provided, start from all packages.
        if not candidate_ids and not (body.Query and body.Query.KeyWord) and not (
            body.Inclusions
        ):
            candidate_ids = set(all_ids)

    # Step 2: apply Filters (AND across all filters).
    filtered_ids: List[str] = []
    for package_id in candidate_ids:
        pkg_index = index.packages.get(package_id)
        if not pkg_index:
            continue

        matches_all_filters = True
        for flt in body.Filters or []:
            if not _package_matches_filter(package_id, pkg_index, flt):
                matches_all_filters = False
                break

        if matches_all_filters:
            filtered_ids.append(package_id)

    # Step 3: build the response payload.
    results: List[dict] = []
    for package_id in filtered_ids:
        pkg_index = index.packages.get(package_id)
        if not pkg_index:
            continue
        pkg = pkg_index.package

        version_strings = sorted(
            {v.version for v in pkg_index.versions if v.version},
            reverse=True,
        )

        versions_payload: List[dict] = []
        for ver in version_strings:
            # Collect product codes for this logical version (across arch/scope).
            product_codes = sorted(
                {
                    v.product_code
                    for v in pkg_index.versions
                    if v.version == ver and getattr(v, "product_code", None)
                }
            )
            versions_payload.append(
                {
                    "PackageVersion": ver,
                    "Channel": None,
                    "PackageFamilyNames": [],
                    "ProductCodes": product_codes,
                    "AppsAndFeaturesEntryVersions": [],
                    "UpgradeCodes": [],
                }
            )

        if not versions_payload:
            continue

        results.append(
            {
                "PackageIdentifier": package_id,
                "PackageName": pkg.package_name,
                "Publisher": pkg.publisher,
                "Versions": versions_payload,
            }
        )

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
async def get_package_manifests(package_id: str, request: Request) -> dict:
    """
    WinGet REST `/packageManifests/{PackageIdentifier}` endpoint.
    """
    index = get_repository_index()
    pkg_index = index.packages.get(package_id)
    if not pkg_index:
        raise HTTPException(status_code=404, detail="Package not found")

    pkg = pkg_index.package

    # Group versions by PackageVersion (in case we later support multiple
    # architectures / scopes for the same logical version).
    versions_by_version: Dict[str, List] = {}
    for v in pkg_index.versions:
        versions_by_version.setdefault(v.version, []).append(v)

    base_url = str(request.base_url).rstrip("/")

    version_entries: List[dict] = []
    for version_str, version_list in sorted(versions_by_version.items(), reverse=True):
        # Use the first entry as the representative for DefaultLocale fields.
        v0 = version_list[0]

        # License and ShortDescription are required by the client; fall back to
        # simple defaults if the package metadata does not provide them.
        license_value = pkg.license or "Proprietary"
        short_description = (
            pkg.short_description or f"{pkg.package_name} installer"
        )

        default_locale = {
            "PackageLocale": "en-US",
            "Publisher": pkg.publisher,
            "PublisherUrl": pkg.homepage,
            "PublisherSupportUrl": pkg.support_url,
            "PrivacyUrl": None,
            "Author": pkg.publisher,
            "PackageName": pkg.package_name,
            "PackageUrl": pkg.homepage,
            "License": license_value,
            "LicenseUrl": None,
            "Copyright": None,
            "CopyrightUrl": None,
            "ShortDescription": short_description,
            "Description": None,
            "Tags": pkg.tags or None,
            "ReleaseNotes": v0.release_notes,
            "ReleaseNotesUrl": None,
            "Agreements": [],
            "PurchaseUrl": None,
            "InstallationNotes": None,
            "Documentations": [],
            "Icons": [],
            "Moniker": None,
        }

        installers: List[dict] = []
        for v in version_list:
            installer_identifier = f"{v.version}-{v.architecture}-{v.scope}"
            installer_url = (
                f"{base_url}/winget/packages/{package_id}/versions/{installer_identifier}/installer"
            )

            # InstallerSha256 is required for non-MSStore installers.
            sha256 = v.installer_sha256 or _compute_installer_sha256(
                v.installer_file,
                v.storage_path,
            )
            if not sha256:
                # Skip installers that cannot satisfy the contract.
                continue

            # Determine installer type and any nested installer metadata.
            installer_type_value = v.installer_type
            nested_type = None
            nested_files: List[dict] = []

            if v.installer_type == "custom":
                # Custom installers are exposed to WinGet as a zip that contains
                # an install.bat which orchestrates the real installer.
                installer_type_value = "zip"
                nested_type = "exe"
                nested_files = [
                    {
                        "RelativeFilePath": "install.bat",
                        "PortableCommandAlias": None,
                    }
                ]
            elif v.installer_type == "zip":
                nested_type = getattr(v, "nested_installer_type", None)
                nested_files_attr = getattr(v, "nested_installer_files", []) or []
                for f in nested_files_attr:
                    nested_files.append(
                        {
                            "RelativeFilePath": f.relative_file_path,
                            "PortableCommandAlias": getattr(
                                f, "portable_command_alias", None
                            ),
                        }
                    )

            # InstallModes derived from per-version booleans (falling back to
            # previous behavior if fields are missing).
            install_modes: List[str] = []
            if getattr(v, "install_mode_interactive", True):
                install_modes.append("interactive")
            if getattr(v, "install_mode_silent", True):
                install_modes.append("silent")
            if getattr(v, "install_mode_silent_with_progress", True):
                install_modes.append("silentWithProgress")

            # Dependencies (only PackageDependencies are currently modeled).
            package_deps: List[dict] = []
            for dep_id in getattr(v, "package_dependencies", []) or []:
                package_deps.append({"PackageIdentifier": dep_id})

            # Elevation requirement derived from the requires_elevation flag.
            elevation_requirement = (
                "elevationRequired"
                if getattr(v, "requires_elevation", False)
                else "none"
            )

            installers.append(
                {
                    "InstallerIdentifier": f"{v.version}-{v.architecture}-{v.scope}",
                    "InstallerSha256": sha256,
                    "InstallerUrl": installer_url,
                    "Architecture": v.architecture,
                    "InstallerLocale": "en-US",
                    "Platform": ["Windows.Desktop"],
                    "MinimumOSVersion": "10.0.0.0",
                    "InstallerType": installer_type_value,
                    "Scope": v.scope,
                    "SignatureSha256": None,
                    "InstallModes": install_modes,
                    "InstallerSwitches": {
                        "Silent": v.silent_arguments,
                        "SilentWithProgress": getattr(
                            v, "silent_with_progress_arguments", None
                        )
                        or v.silent_arguments,
                        "Interactive": v.interactive_arguments,
                        "InstallLocation": None,
                        "Log": v.log_arguments,
                        "Upgrade": None,
                        "Custom": None,
                        "Repair": None,
                    },
                    "InstallerSuccessCodes": [],
                    "ExpectedReturnCodes": [],
                    "UpgradeBehavior": "install",
                    "Commands": [],
                    "Protocols": [],
                    "FileExtensions": [],
                    "Dependencies": {
                        "WindowsFeatures": [],
                        "WindowsLibraries": [],
                        "PackageDependencies": package_deps,
                        "ExternalDependencies": [],
                    },
                    "PackageFamilyName": None,
                    "ProductCode": getattr(v, "product_code", None),
                    "Capabilities": [],
                    "RestrictedCapabilities": [],
                    "MSStoreProductIdentifier": None,
                    "InstallerAbortsTerminal": False,
                    "ReleaseDate": v.release_date.date().isoformat()
                    if v.release_date
                    else None,
                    "InstallLocationRequired": False,
                    "RequireExplicitUpgrade": False,
                    "ElevationRequirement": elevation_requirement,
                    "UnsupportedOSArchitectures": [],
                    "AppsAndFeaturesEntries": [],
                    "Markets": None,
                    "NestedInstallerType": nested_type,
                    "NestedInstallerFiles": nested_files,
                    "DisplayInstallWarnings": False,
                    "UnsupportedArguments": [],
                    "InstallationMetadata": {
                        "DefaultInstallLocation": None,
                        "Files": [],
                    },
                    "DownloadCommandProhibited": False,
                    "RepairBehavior": "installer",
                    "ArchiveBinariesDependOnPath": False,
                    "Authentication": {
                        "AuthenticationType": "none",
                        "MicrosoftEntraIdAuthenticationInfo": None,
                    },
                }
            )

        version_entries.append(
            {
                "PackageVersion": version_str,
                "Channel": None,
                "DefaultLocale": default_locale,
                "Locales": [],
                "Installers": installers,
            }
        )

    data = {
        "PackageIdentifier": package_id,
        "Versions": version_entries,
    }

    config = get_repository_config()

    return {
        "Data": _strip_nulls(data),
        "ContinuationToken": None,
        "UnsupportedQueryParameters": config.unsupported_query_parameters,
        "RequiredQueryParameters": config.required_query_parameters,
    }


# ---------------------------------------------------------------------------
# 4. Installer download (internal helper used by manifests)
# ---------------------------------------------------------------------------


@router.get("/packages/{package_id}/versions/{installer_id}/installer")
async def download_installer(package_id: str, installer_id: str) -> FileResponse:
    """
    Download endpoint that serves the installer file for a given
    (version, architecture, scope) combination.

    The installer file is expected to live alongside the version.json file
    inside the version's directory. The installer_id parameter is expected
    to be in the form "<version>-<architecture>-<scope>" and must match the
    InstallerIdentifier emitted in the manifest.
    """
    index = get_repository_index()
    pkg_index = index.packages.get(package_id)
    if not pkg_index:
        raise HTTPException(status_code=404, detail="Package not found")

    # Find the concrete version entry matching the installer identifier.
    matching_versions = [
        v
        for v in pkg_index.versions
        if f"{v.version}-{v.architecture}-{v.scope}" == installer_id
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

    # For custom installer types we serve the generated package.zip which
    # contains install.bat and the real installer. For all other types we
    # serve the uploaded installer file directly.
    if v.installer_type == "custom":
        installer_filename = "package.zip"
    else:
        installer_filename = v.installer_file

    installer_path = data_dir / v.storage_path / installer_filename

    if not installer_path.is_file():
        raise HTTPException(
            status_code=404,
            detail="Installer file not found on disk",
        )

    # Let FastAPI/Uvicorn stream the file efficiently.
    return FileResponse(
        path=str(installer_path),
        filename=installer_filename,
        media_type="application/octet-stream",
    )


