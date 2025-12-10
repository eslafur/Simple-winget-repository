from __future__ import annotations

from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Form, HTTPException, Request, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.data.models import PackageCommonMetadata, VersionMetadata
from app.data.repository import (
    get_data_dir,
    get_repository_index,
    build_index_from_disk,
)


router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


def _get_package_or_404(package_id: str):
    index = get_repository_index()
    pkg_index = index.packages.get(package_id)
    if not pkg_index:
        raise HTTPException(status_code=404, detail="Package not found")
    return pkg_index


def _get_version_or_404(package_id: str, version: str) -> VersionMetadata:
    pkg_index = _get_package_or_404(package_id)
    for v in pkg_index.versions:
        if v.version == version:
            return v
    raise HTTPException(status_code=404, detail="Version not found")


@router.get("/admin/packages", response_class=HTMLResponse)
async def admin_list_packages(request: Request) -> HTMLResponse:
    """
    Admin UI: list all packages with id, name and latest version.
    """
    index = get_repository_index()

    packages = []
    for package_id, pkg_index in sorted(index.packages.items()):
        versions = sorted(
            {v.version for v in pkg_index.versions if v.version},
            reverse=True,
        )
        latest_version: Optional[str] = versions[0] if versions else None
        packages.append(
            {
                "id": package_id,
                "name": pkg_index.package.package_name,
                "latest_version": latest_version,
            }
        )

    return templates.TemplateResponse(
        "admin_packages.html",
        {
            "request": request,
            "packages": packages,
        },
    )


@router.get("/admin/packages/new", response_class=HTMLResponse)
async def admin_new_package(request: Request) -> HTMLResponse:
    """
    Admin UI: create a new package.
    """
    return templates.TemplateResponse(
        "admin_package_new.html",
        {
            "request": request,
        },
    )


@router.post("/admin/packages/new")
async def admin_create_package(
    package_identifier: str = Form(...),
    package_name: str = Form(...),
    publisher: str = Form(...),
    short_description: str = Form(""),
    license: str = Form(""),
    tags: str = Form(""),
) -> RedirectResponse:
    """
    Create a new package directory and package.json on disk.
    """
    data_dir = get_data_dir()
    index = get_repository_index()

    package_identifier = package_identifier.strip()
    if not package_identifier:
        raise HTTPException(status_code=400, detail="Package identifier is required")

    if package_identifier in index.packages:
        # If it already exists, just go to its detail page.
        return RedirectResponse(
            url=f"/admin/packages/{package_identifier}",
            status_code=303,
        )

    pkg_dir = data_dir / package_identifier
    pkg_dir.mkdir(parents=True, exist_ok=True)

    package_json = pkg_dir / "package.json"
    meta = PackageCommonMetadata(
        package_identifier=package_identifier,
        package_name=package_name,
        publisher=publisher,
        short_description=short_description or None,
        license=license or None,
        tags=[t.strip() for t in tags.split(",") if t.strip()],
    )
    package_json.write_text(meta.model_dump_json(indent=2), encoding="utf-8")

    build_index_from_disk()

    return RedirectResponse(
        url=f"/admin/packages/{package_identifier}",
        status_code=303,
    )


@router.get("/admin/packages/{package_id}", response_class=HTMLResponse)
async def admin_edit_package(package_id: str, request: Request) -> HTMLResponse:
    """
    Admin UI: show and edit package metadata, and list its versions.
    """
    pkg_index = _get_package_or_404(package_id)

    versions = sorted(
        pkg_index.versions,
        key=lambda v: (v.version or "", v.architecture or "", v.scope or ""),
        reverse=True,
    )

    return templates.TemplateResponse(
        "admin_package_detail.html",
        {
            "request": request,
            "package_id": package_id,
            "package": pkg_index.package,
            "versions": versions,
        },
    )


@router.post("/admin/packages/{package_id}")
async def admin_update_package(
    package_id: str,
    package_name: str = Form(...),
    publisher: str = Form(...),
    short_description: str = Form(""),
    license: str = Form(""),
    tags: str = Form(""),
) -> RedirectResponse:
    """
    Persist updates to package metadata back to disk.
    """
    pkg_index = _get_package_or_404(package_id)
    data_dir = get_data_dir()

    # Package directory is derived from the first version or from the package_id
    storage_path = pkg_index.storage_path or package_id
    pkg_dir = data_dir / storage_path
    pkg_dir.mkdir(parents=True, exist_ok=True)

    package_json = pkg_dir / "package.json"
    current = pkg_index.package

    updated = PackageCommonMetadata(
        package_identifier=current.package_identifier,
        package_name=package_name,
        publisher=publisher,
        short_description=short_description or None,
        license=license or None,
        tags=[t.strip() for t in tags.split(",") if t.strip()],
        homepage=current.homepage,
        support_url=current.support_url,
        is_example=current.is_example,
    )

    package_json.write_text(updated.model_dump_json(indent=2), encoding="utf-8")

    # Refresh index so the admin UI and API see the changes immediately.
    build_index_from_disk()

    return RedirectResponse(
        url=f"/admin/packages/{package_id}",
        status_code=303,
    )


@router.get(
    "/admin/packages/{package_id}/versions/new",
    response_class=HTMLResponse,
)
async def admin_new_version_form(
    package_id: str,
    request: Request,
) -> HTMLResponse:
    """
    Admin UI: form to create a brand new version for a package.
    """
    # Ensure package exists; will 404 otherwise.
    _get_package_or_404(package_id)

    return templates.TemplateResponse(
        "admin_version_new.html",
        {
            "request": request,
            "package_id": package_id,
        },
    )


@router.post(
    "/admin/packages/{package_id}/versions/new",
)
async def admin_create_version(
    package_id: str,
    version: str = Form(...),
    architecture: str = Form(...),
    scope: str = Form(...),
    installer_type: str = Form("exe"),
    silent_arguments: str = Form(""),
    interactive_arguments: str = Form(""),
    log_arguments: str = Form(""),
    upload: UploadFile | None = File(
        default=None,
        description="Optional installer file for this version.",
    ),
) -> RedirectResponse:
    """
    Create a brand new version directory, optional installer file, and version.json.
    """
    _get_package_or_404(package_id)
    data_dir = get_data_dir()

    # Directory layout: <DATA_DIR>/<package_id>/<version-arch-scope>/
    dir_name = f"{version}-{architecture}-{scope}"
    package_dir = data_dir / package_id
    version_dir = package_dir / dir_name
    version_dir.mkdir(parents=True, exist_ok=True)

    installer_file_name: Optional[str] = None
    if upload is not None:
        installer_file_name = upload.filename
        installer_path = version_dir / installer_file_name
        with installer_path.open("wb") as f:
            contents = await upload.read()
            f.write(contents)

    meta = VersionMetadata(
        version=version,
        architecture=architecture,
        scope=scope,
        installer_type=installer_type,
        installer_file=installer_file_name,
        installer_sha256=None,
        silent_arguments=silent_arguments or None,
        interactive_arguments=interactive_arguments or None,
        log_arguments=log_arguments or None,
        release_date=None,
        release_notes=None,
    )

    version_json = version_dir / "version.json"
    version_json.write_text(meta.model_dump_json(indent=2), encoding="utf-8")

    build_index_from_disk()

    return RedirectResponse(
        url=f"/admin/packages/{package_id}/versions/{version}",
        status_code=303,
    )


@router.get(
    "/admin/packages/{package_id}/versions/{version}",
    response_class=HTMLResponse,
)
async def admin_edit_version(
    package_id: str,
    version: str,
    request: Request,
) -> HTMLResponse:
    """
    Admin UI: edit a specific version's metadata.
    """
    v = _get_version_or_404(package_id, version)

    return templates.TemplateResponse(
        "admin_version_detail.html",
        {
            "request": request,
            "package_id": package_id,
            "version_meta": v,
        },
    )


@router.post("/admin/packages/{package_id}/versions/{version}")
async def admin_update_version(
    package_id: str,
    version: str,
    architecture: str = Form(...),
    scope: str = Form(...),
    installer_type: str = Form("exe"),
    installer_file: str = Form(""),
    silent_arguments: str = Form(""),
    interactive_arguments: str = Form(""),
    log_arguments: str = Form(""),
    upload: UploadFile | None = File(
        default=None,
        description="Optional new installer file for this version.",
    ),
) -> RedirectResponse:
    """
    Update an existing version's metadata on disk.
    """
    v = _get_version_or_404(package_id, version)
    data_dir = get_data_dir()

    if not v.storage_path:
        raise HTTPException(
            status_code=500,
            detail="Version storage path is not available",
        )

    version_dir = data_dir / v.storage_path
    version_dir.mkdir(parents=True, exist_ok=True)

    # Handle optional new installer upload.
    installer_file_name = installer_file or v.installer_file
    if upload is not None:
        installer_file_name = upload.filename
        installer_path = version_dir / installer_file_name
        with installer_path.open("wb") as f:
            contents = await upload.read()
            f.write(contents)

    version_json = version_dir / "version.json"

    updated = VersionMetadata(
        version=version,
        architecture=architecture,
        scope=scope,
        installer_type=installer_type,
        installer_file=installer_file_name,
        installer_sha256=v.installer_sha256,
        silent_arguments=silent_arguments or None,
        interactive_arguments=interactive_arguments or None,
        log_arguments=log_arguments or None,
        release_date=v.release_date,
        release_notes=v.release_notes,
    )

    version_json.write_text(updated.model_dump_json(indent=2), encoding="utf-8")

    build_index_from_disk()

    return RedirectResponse(
        url=f"/admin/packages/{package_id}/versions/{version}",
        status_code=303,
    )


@router.post(
    "/admin/packages/{package_id}/versions/{version}/clone",
)
async def admin_clone_version(
    package_id: str,
    version: str,
    new_version: str = Form(..., description="New version identifier."),
    upload: UploadFile | None = File(
        default=None,
        description="Optional new installer file for the cloned version.",
    ),
) -> RedirectResponse:
    """
    "Update version" action:
    - Copies metadata from an existing version into a new version,
      except for the version string itself.
    - Optionally uploads a new installer file.
    """
    source = _get_version_or_404(package_id, version)
    data_dir = get_data_dir()

    if not source.storage_path:
        raise HTTPException(
            status_code=500,
            detail="Source version storage path is not available",
        )

    # Compute new version directory based on the new version, arch and scope.
    # Layout: <DATA_DIR>/<package_id>/<version-arch-scope>/
    new_dir_name = f"{new_version}-{source.architecture}-{source.scope}"
    package_dir = data_dir / package_id
    new_version_dir = package_dir / new_dir_name
    new_version_dir.mkdir(parents=True, exist_ok=True)

    installer_file_name: Optional[str] = source.installer_file
    if upload is not None:
        installer_file_name = upload.filename
        installer_path = new_version_dir / installer_file_name
        with installer_path.open("wb") as f:
            contents = await upload.read()
            f.write(contents)

    new_meta = VersionMetadata(
        version=new_version,
        architecture=source.architecture,
        scope=source.scope,
        installer_type=source.installer_type,
        installer_file=installer_file_name,
        installer_sha256=None,  # Recalculate separately if desired.
        silent_arguments=source.silent_arguments,
        interactive_arguments=source.interactive_arguments,
        log_arguments=source.log_arguments,
        release_date=None,
        release_notes=None,
    )

    version_json = new_version_dir / "version.json"
    version_json.write_text(new_meta.model_dump_json(indent=2), encoding="utf-8")

    build_index_from_disk()

    return RedirectResponse(
        url=f"/admin/packages/{package_id}/versions/{new_version}",
        status_code=303,
    )


@router.post(
    "/admin/packages/{package_id}/versions/{version}/delete",
)
async def admin_delete_version(
    package_id: str,
    version: str,
) -> RedirectResponse:
    """
    Delete a version directory (including its installer file) from disk.
    """
    v = _get_version_or_404(package_id, version)
    data_dir = get_data_dir()

    if not v.storage_path:
        raise HTTPException(
            status_code=500,
            detail="Version storage path is not available",
        )

    version_dir = data_dir / v.storage_path
    if version_dir.is_dir():
        # Remove files then the directory.
        for child in version_dir.iterdir():
            if child.is_file():
                child.unlink(missing_ok=True)
        version_dir.rmdir()

    build_index_from_disk()

    return RedirectResponse(
        url=f"/admin/packages/{package_id}",
        status_code=303,
    )


