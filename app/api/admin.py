from __future__ import annotations

from pathlib import Path
from typing import Optional
import hashlib
import urllib.parse

from fastapi import APIRouter, Form, HTTPException, Request, UploadFile, File, Query
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from app.data.models import PackageCommonMetadata, VersionMetadata
from app.data.repository import (
    get_data_dir,
    get_repository_index,
    get_repository_config,
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


def _get_version_by_id(package_id: str, version_id: str) -> Optional[VersionMetadata]:
    """
    Get a version by its unique ID (format: version-architecture-scope).
    Returns None if not found.
    """
    pkg_index = _get_package_or_404(package_id)
    for v in pkg_index.versions:
        version_id_candidate = f"{v.version}-{v.architecture}-{v.scope}"
        if version_id_candidate == version_id:
            return v
    return None


async def _save_upload_and_hash(upload: UploadFile, target_dir: Path) -> tuple[str, str]:
    """
    Save an uploaded installer file into the given directory and return
    (filename, sha256_hex).
    """
    target_dir.mkdir(parents=True, exist_ok=True)
    filename = upload.filename
    path = target_dir / filename

    hasher = hashlib.sha256()
    with path.open("wb") as f:
        while True:
            chunk = await upload.read(8192)
            if not chunk:
                break
            f.write(chunk)
            hasher.update(chunk)

    return filename, hasher.hexdigest()


# ---------------------------------------------------------------------------
# Package list
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Unified package form (create/edit)
# ---------------------------------------------------------------------------


@router.get("/admin/packages/{package_id}", response_class=HTMLResponse)
async def admin_package_detail(package_id: str, request: Request) -> HTMLResponse:
    """
    Package detail page: shows package and its versions. Only accessible if package exists.
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


@router.get("/admin/packages/new/fragment", response_class=HTMLResponse)
async def admin_package_form_fragment_new(request: Request) -> HTMLResponse:
    """
    Returns new package form as a fragment (for modal overlay).
    """
    return templates.TemplateResponse(
        "admin_package_form_fragment.html",
        {
            "request": request,
            "package_id": "",
            "package": None,
        },
    )


@router.get("/admin/packages/{package_id}/fragment", response_class=HTMLResponse)
async def admin_package_form_fragment(
    package_id: str,
    request: Request,
) -> HTMLResponse:
    """
    Returns package form as a fragment (for modal overlay).
    """
    index = get_repository_index()
    pkg_index = index.packages.get(package_id)
    
    # Package may or may not exist (for create vs edit)
    package = pkg_index.package if pkg_index else None
    
    return templates.TemplateResponse(
        "admin_package_form_fragment.html",
        {
            "request": request,
            "package_id": package_id,
            "package": package,
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
) -> JSONResponse:
    """
    Create a new package. Returns JSON for AJAX response.
    """
    data_dir = get_data_dir()
    index = get_repository_index()
    
    # Check if package already exists
    if package_identifier in index.packages:
        return JSONResponse(
            status_code=400,
            content={"error": "Package already exists"},
        )
    
    pkg_dir = data_dir / package_identifier
    pkg_dir.mkdir(parents=True, exist_ok=True)
    
    package_json = pkg_dir / "package.json"
    
    new_package = PackageCommonMetadata(
        package_identifier=package_identifier,
        package_name=package_name,
        publisher=publisher,
        short_description=short_description or None,
        license=license or None,
        tags=[t.strip() for t in tags.split(",") if t.strip()],
    )
    
    package_json.write_text(new_package.model_dump_json(indent=2), encoding="utf-8")
    build_index_from_disk()
    
    return JSONResponse(
        status_code=200,
        content={"success": True, "message": "Package created successfully", "package_id": package_identifier},
    )


@router.post("/admin/packages/{package_id}")
async def admin_save_package(
    package_id: str,
    package_identifier: str = Form(...),
    package_name: str = Form(...),
    publisher: str = Form(...),
    short_description: str = Form(""),
    license: str = Form(""),
    tags: str = Form(""),
) -> JSONResponse:
    """
    Unified save: creates package if it doesn't exist, updates if it does.
    Returns JSON for AJAX response.
    """
    data_dir = get_data_dir()
    index = get_repository_index()
    pkg_index = index.packages.get(package_id)
    
    # Validate package_identifier matches URL parameter
    if package_identifier != package_id:
        return JSONResponse(
            status_code=400,
            content={"error": "Package identifier mismatch"},
        )

    pkg_dir = data_dir / package_id
    pkg_dir.mkdir(parents=True, exist_ok=True)

    package_json = pkg_dir / "package.json"
    
    if pkg_index:
        # Update existing
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
    else:
        # Create new
        updated = PackageCommonMetadata(
            package_identifier=package_id,
            package_name=package_name,
            publisher=publisher,
            short_description=short_description or None,
            license=license or None,
            tags=[t.strip() for t in tags.split(",") if t.strip()],
        )

    package_json.write_text(updated.model_dump_json(indent=2), encoding="utf-8")
    build_index_from_disk()

    return JSONResponse(
        status_code=200,
        content={"success": True, "message": "Package saved successfully"},
    )


# ---------------------------------------------------------------------------
# Version form fragment (for modal)
# ---------------------------------------------------------------------------


@router.get("/admin/packages/{package_id}/versions/{version_id}/fragment", response_class=HTMLResponse)
async def admin_version_form_fragment(
    package_id: str,
    version_id: str,
    request: Request,
    clone_from: Optional[str] = Query(default=None),
) -> HTMLResponse:
    """
    Returns version form as a fragment (for modal overlay).
    version_id format: version-architecture-scope, or "new" for creating
    clone_from: optional source version_id to clone data from (version and installer_file will be empty)
    """
    _get_package_or_404(package_id)
    config = get_repository_config()
    
    # Decode URL-encoded version_id
    version_id = urllib.parse.unquote(version_id)
    
    # Try to load existing version, or None for new
    version_meta = None
    if version_id != "new":
        version_meta = _get_version_by_id(package_id, version_id)
    
    # If clone_from is specified, load that version's data but clear version and installer_file
    cloned_meta = None
    if clone_from:
        clone_from = urllib.parse.unquote(clone_from)
        source_version = _get_version_by_id(package_id, clone_from)
        if source_version:
            # Create a copy with version and installer_file cleared
            cloned_meta = VersionMetadata(
                version="",  # Empty for new version
                architecture=source_version.architecture,
                scope=source_version.scope,
                installer_type=source_version.installer_type,
                installer_file=None,  # Empty - user must upload
                installer_sha256=None,
                silent_arguments=source_version.silent_arguments,
                interactive_arguments=source_version.interactive_arguments,
                log_arguments=source_version.log_arguments,
                release_date=None,
                release_notes=None,
            )
    
    # Use cloned_meta if available, otherwise use version_meta
    form_meta = cloned_meta if cloned_meta else version_meta
    
    return templates.TemplateResponse(
        "admin_version_form_fragment.html",
        {
            "request": request,
            "package_id": package_id,
            "version_id": "new",  # Always "new" when cloning
            "version_meta": form_meta,
            "architecture_options": config.architecture_options,
            "scope_options": config.scope_options,
            "installer_type_options": config.installer_type_options,
        },
    )


@router.post("/admin/packages/{package_id}/versions/{version_id}")
async def admin_save_version(
    package_id: str,
    version_id: str,
    version: str = Form(...),
    architecture: str = Form(...),
    scope: str = Form(...),
    installer_type: str = Form("exe"),
    silent_arguments: str = Form(""),
    interactive_arguments: str = Form(""),
    log_arguments: str = Form(""),
    upload: UploadFile | None = File(default=None),
) -> JSONResponse:
    """
    Unified save: creates version if it doesn't exist, updates if it does.
    Returns JSON for AJAX response (modal will handle refresh).
    """
    _get_package_or_404(package_id)
    data_dir = get_data_dir()
    
    # Decode URL-encoded version_id
    version_id = urllib.parse.unquote(version_id)
    
    # Compute the actual version ID from form data
    actual_version_id = f"{version}-{architecture}-{scope}"
    
    # Check if version exists (unless it's "new")
    existing_version = None
    if version_id != "new":
        existing_version = _get_version_by_id(package_id, version_id)
        # Also check if the new ID (from form) already exists (user changed version/arch/scope)
        if actual_version_id != version_id:
            existing_by_new_id = _get_version_by_id(package_id, actual_version_id)
            if existing_by_new_id:
                return JSONResponse(
                    status_code=400,
                    content={"error": f"Version {actual_version_id} already exists"},
                )
    
    # Compute directory name from form data
    dir_name = f"{version}-{architecture}-{scope}"
    package_dir = data_dir / package_id
    version_dir = package_dir / dir_name
    
    # Handle installer upload
    installer_file_name: Optional[str] = None
    installer_sha256: Optional[str] = None
    
    # Check if a file was actually uploaded (not just an empty UploadFile)
    # FastAPI may pass an UploadFile object even when no file is selected
    has_new_upload = upload is not None and upload.filename and len(upload.filename.strip()) > 0
    
    if has_new_upload:
        # New file uploaded
        installer_file_name, installer_sha256 = await _save_upload_and_hash(upload, version_dir)
        
        # If updating and old installer had different name, delete old file
        if existing_version and existing_version.installer_file and existing_version.installer_file != installer_file_name:
            old_installer_path = version_dir / existing_version.installer_file
            if old_installer_path.exists():
                old_installer_path.unlink()
    elif existing_version:
        # Keep existing installer info
        installer_file_name = existing_version.installer_file
        installer_sha256 = existing_version.installer_sha256
    else:
        # New version without upload - this shouldn't happen per our rules, but handle gracefully
        return JSONResponse(
            status_code=400,
            content={"error": "Installer file is required for new versions"},
        )
    
    # If directory name changed (arch/scope changed), we need to move/delete old directory
    if existing_version and existing_version.storage_path:
        old_dir = data_dir / existing_version.storage_path
        if old_dir.exists() and old_dir != version_dir:
            # Move files if possible, otherwise copy and delete
            version_dir.mkdir(parents=True, exist_ok=True)
            for item in old_dir.iterdir():
                if item.is_file():
                    item.rename(version_dir / item.name)
            try:
                old_dir.rmdir()
            except OSError:
                pass  # Directory not empty or already gone
    
    version_dir.mkdir(parents=True, exist_ok=True)
    version_json = version_dir / "version.json"
    
    meta = VersionMetadata(
        version=version,
        architecture=architecture,
        scope=scope,
        installer_type=installer_type,
        installer_file=installer_file_name,
        installer_sha256=installer_sha256,
        silent_arguments=silent_arguments or None,
        interactive_arguments=interactive_arguments or None,
        log_arguments=log_arguments or None,
        release_date=existing_version.release_date if existing_version else None,
        release_notes=existing_version.release_notes if existing_version else None,
    )
    
    version_json.write_text(meta.model_dump_json(indent=2), encoding="utf-8")
    build_index_from_disk()
    
    return JSONResponse(
        status_code=200,
        content={"success": True, "message": "Version saved successfully"},
    )


@router.post("/admin/packages/{package_id}/versions/{version_id}/delete")
async def admin_delete_version(
    package_id: str,
    version_id: str,
) -> JSONResponse:
    """
    Delete a version. Returns JSON for AJAX response.
    """
    # Decode URL-encoded version_id
    version_id = urllib.parse.unquote(version_id)
    v = _get_version_by_id(package_id, version_id)
    if not v:
        return JSONResponse(
            status_code=404,
            content={"error": "Version not found"},
        )
    
    data_dir = get_data_dir()
    
    if not v.storage_path:
        return JSONResponse(
            status_code=500,
            content={"error": "Version storage path is not available"},
        )
    
    version_dir = data_dir / v.storage_path
    if version_dir.is_dir():
        for child in version_dir.iterdir():
            if child.is_file():
                child.unlink(missing_ok=True)
        version_dir.rmdir()
    
    build_index_from_disk()
    
    return JSONResponse(
        status_code=200,
        content={"success": True, "message": "Version deleted successfully"},
    )


@router.post("/admin/packages/{package_id}/delete")
async def admin_delete_package(
    package_id: str,
) -> JSONResponse:
    """
    Delete a package and all its versions. Returns JSON for AJAX response.
    """
    pkg_index = _get_package_or_404(package_id)
    
    data_dir = get_data_dir()
    
    if not pkg_index.storage_path:
        return JSONResponse(
            status_code=500,
            content={"error": "Package storage path is not available"},
        )
    
    package_dir = data_dir / pkg_index.storage_path
    if package_dir.is_dir():
        # Delete all files and subdirectories
        for child in package_dir.iterdir():
            if child.is_file():
                child.unlink(missing_ok=True)
            elif child.is_dir():
                # Delete version directories
                for version_file in child.iterdir():
                    if version_file.is_file():
                        version_file.unlink(missing_ok=True)
                child.rmdir()
        package_dir.rmdir()
    
    build_index_from_disk()
    
    return JSONResponse(
        status_code=200,
        content={"success": True, "message": "Package deleted successfully"},
    )
