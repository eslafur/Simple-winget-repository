from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Optional, List
import hashlib
import os
import urllib.parse
import zipfile

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from app.data.authentication import SESSION_COOKIE_NAME, get_user_for_session
from app.data.models import (
    PackageCommonMetadata,
    VersionMetadata,
    NestedInstallerFile,
    CustomInstallerStep,
    AuthUser,
    ADGroupScopeEntry,
)
from app.data.repository import (
    get_data_dir,
    get_repository_index,
    get_repository_config,
    build_index_from_disk,
)
from app.data.winget_index import WinGetIndexReader
from app.data.winget_importer import WinGetPackageImporter
from app.data.models import CacheSettings
from app.data.winget_index_status import get_index_status_store
from app.data.winget_index_downloader import download_winget_index
from app import custom_installer


templates = Jinja2Templates(directory="app/templates")


async def require_admin_session(request: Request) -> AuthUser:
    """
    Dependency that ensures a valid admin session exists.

    For GET requests, unauthenticated callers are redirected to /login.
    For non-GET requests, a 401 JSON error is returned.
    """
    session_id = request.cookies.get(SESSION_COOKIE_NAME)
    user = get_user_for_session(session_id) if session_id else None

    if user is None:
        if request.method.upper() == "GET":
            # Use a redirect for browser navigation.
            raise HTTPException(
                status_code=status.HTTP_307_TEMPORARY_REDIRECT,
                headers={"Location": "/login"},
            )
        # For XHR/POST calls, return a 401 error so the client can handle it.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
        )

    return user


router = APIRouter(dependencies=[Depends(require_admin_session)])
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


def _parse_ad_group_scopes(
    groups: Optional[List[str]],
    scopes: Optional[List[str]],
) -> List[ADGroupScopeEntry]:
    """
    Parse repeated form fields into a validated list of ADGroupScopeEntry.

    Empty/blank rows are ignored. If a row has a group but missing/invalid scope,
    it will be ignored as well (admin UI should prevent this, but we keep server safe).
    """
    if not groups and not scopes:
        return []
    groups = groups or []
    scopes = scopes or []
    n = min(len(groups), len(scopes))
    config = get_repository_config()
    allowed_scopes = set(config.scope_options or ["user", "machine"])

    result: List[ADGroupScopeEntry] = []
    for i in range(n):
        g = (groups[i] or "").strip()
        s = (scopes[i] or "").strip()
        if not g:
            continue
        if s not in allowed_scopes:
            continue
        result.append(ADGroupScopeEntry(ad_group=g, scope=s))  # type: ignore[arg-type]
    return result


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


def _build_custom_installer_package(
    version_dir: Path,
    meta: VersionMetadata,
) -> str:
    """
    Generate install.bat from the configured steps in the VersionMetadata and
    package it together with the uploaded installer into package.zip.

    Returns the SHA256 of the resulting zip archive.
    """
    script_text = custom_installer.render_install_script(
        meta=meta,
    )
    script_path = version_dir / "install.bat"
    # Use CRLF endings and UTF-8.
    script_path.write_text(script_text, encoding="utf-8", newline="\r\n")

    package_zip_path = version_dir / "package.zip"
    with zipfile.ZipFile(package_zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        installer_path = version_dir / meta.installer_file
        if installer_path.is_file():
            zf.write(installer_path, arcname=meta.installer_file)
        zf.write(script_path, arcname="install.bat")

    hasher = hashlib.sha256()
    with package_zip_path.open("rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            hasher.update(chunk)

    return hasher.hexdigest()


# ---------------------------------------------------------------------------
# Package list
# ---------------------------------------------------------------------------


@router.get("/admin/packages", response_class=HTMLResponse)
async def admin_list_packages(request: Request) -> HTMLResponse:
    """
    Admin UI: list all packages with id, name and latest version.
    Also shows cached WinGet packages below.
    """
    index = get_repository_index()

    owned_packages = []
    for package_id, pkg_index in sorted(index.packages.items()):
        # Skip cached packages in the owned list
        if getattr(pkg_index.package, "cached", False):
            continue
        versions = sorted(
            {v.version for v in pkg_index.versions if v.version},
            reverse=True,
        )
        latest_version: Optional[str] = versions[0] if versions else None
        owned_packages.append(
            {
                "id": package_id,
                "name": pkg_index.package.package_name,
                "latest_version": latest_version,
            }
        )
    
    # Get WinGet index status
    index_status_store = get_index_status_store()
    index_status = index_status_store.get_status()
    
    # Check if index file exists but status hasn't been set
    index_path = _get_winget_index_path()
    if index_path.exists() and not index_status.last_pulled:
        # Index exists but status not tracked - check file modification time
        try:
            mtime = os.path.getmtime(index_path)
            index_status.last_pulled = datetime.fromtimestamp(mtime)
            index_status_store.update_pulled_time(index_path=index_path)
        except Exception:
            pass
    
    index_last_pulled = index_status.last_pulled.strftime('%Y-%m-%d %H:%M:%S') if index_status.last_pulled else None
    
    # Cached packages: derive from repository index (packages with cached=True)
    repo_index = get_repository_index()
    cached_packages_data = []
    for pkg_id, pkg_index in repo_index.packages.items():
        pkg_meta = pkg_index.package
        if not getattr(pkg_meta, "cached", False):
            continue
        latest_version = None
        version_count = len(pkg_index.versions)
        if pkg_index.versions:
            sorted_versions = sorted(
                pkg_index.versions,
                key=lambda v: v.version,
                reverse=True
            )
            latest_version = sorted_versions[0].version
        cs = pkg_meta.cache_settings
        cached_packages_data.append({
            "package_id": pkg_meta.package_identifier,
            "package_name": pkg_meta.package_name,
            "publisher": pkg_meta.publisher,
            "latest_version": latest_version,
            "filters": {
                "architectures": ", ".join(cs.architectures) if cs and cs.architectures else "All",
                "scopes": ", ".join(cs.scopes) if cs and cs.scopes else "All",
                "installer_types": ", ".join(cs.installer_types) if cs and cs.installer_types else "All",
                "version_mode": cs.version_mode if cs else "latest",
            },
            "version_count": version_count,
        })

    return templates.TemplateResponse(
        "admin_packages.html",
        {
            "request": request,
            "owned_packages": owned_packages,
            "cached_packages": cached_packages_data,
            "index_last_pulled": index_last_pulled,
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
    config = get_repository_config()
    return templates.TemplateResponse(
        "admin_package_form_fragment.html",
        {
            "request": request,
            "package_id": "",
            "package": None,
            "scopes": config.scope_options,
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
    config = get_repository_config()
    
    return templates.TemplateResponse(
        "admin_package_form_fragment.html",
        {
            "request": request,
            "package_id": package_id,
            "package": package,
            "scopes": config.scope_options,
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
    ad_group_scopes_group: Optional[List[str]] = Form(None),
    ad_group_scopes_scope: Optional[List[str]] = Form(None),
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
    
    pkg_dir = data_dir / "owned" / package_identifier
    pkg_dir.mkdir(parents=True, exist_ok=True)
    
    package_json = pkg_dir / "package.json"
    
    new_package = PackageCommonMetadata(
        package_identifier=package_identifier,
        package_name=package_name,
        publisher=publisher,
        short_description=short_description or None,
        license=license or None,
        tags=[t.strip() for t in tags.split(",") if t.strip()],
        ad_group_scopes=_parse_ad_group_scopes(ad_group_scopes_group, ad_group_scopes_scope),
        cached=False,
        cache_settings=None,
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
    ad_group_scopes_group: Optional[List[str]] = Form(None),
    ad_group_scopes_scope: Optional[List[str]] = Form(None),
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

    pkg_dir = data_dir / "owned" / package_id
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
            ad_group_scopes=_parse_ad_group_scopes(ad_group_scopes_group, ad_group_scopes_scope)
            or getattr(current, "ad_group_scopes", [])
            or [],
            is_example=current.is_example,
            cached=current.cached,
            cache_settings=current.cache_settings,
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
            ad_group_scopes=_parse_ad_group_scopes(ad_group_scopes_group, ad_group_scopes_scope),
            cached=False,
            cache_settings=None,
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
    index = get_repository_index()
    
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
            # Create a copy with version and installer_file cleared. All other
            # metadata (including nested installers, custom steps, product
            # code, install modes, dependencies, etc.) is cloned so a new
            # version can be quickly created from an existing one.
            cloned_meta = VersionMetadata(
                version="",  # Empty for new version
                architecture=source_version.architecture,
                scope=source_version.scope,
                installer_type=source_version.installer_type,
                installer_file=None,  # Empty - user must upload
                installer_sha256=None,
                silent_arguments=source_version.silent_arguments,
                silent_with_progress_arguments=getattr(
                    source_version, "silent_with_progress_arguments", None
                ),
                interactive_arguments=source_version.interactive_arguments,
                log_arguments=source_version.log_arguments,
                nested_installer_type=source_version.nested_installer_type,
                nested_installer_files=source_version.nested_installer_files,
                custom_installer_steps=source_version.custom_installer_steps,
                product_code=source_version.product_code,
                install_mode_interactive=source_version.install_mode_interactive,
                install_mode_silent=source_version.install_mode_silent,
                install_mode_silent_with_progress=source_version.install_mode_silent_with_progress,
                requires_elevation=source_version.requires_elevation,
                package_dependencies=source_version.package_dependencies,
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
            "nested_installer_type_options": config.nested_installer_type_options,
            "custom_actions": custom_installer.get_available_actions(),
            "all_package_ids": sorted(index.packages.keys()),
        },
    )


@router.post("/admin/packages/{package_id}/versions/{version_id}")
async def admin_save_version(
    package_id: str,
    version_id: str,
    version: str = Form(...),
    architecture: str = Form(...),
    scope: str = Form(...),
    product_code: str = Form(...),
    installer_type: str = Form("exe"),
    silent_arguments: str = Form(""),
    silent_with_progress_arguments: str = Form(""),
    interactive_arguments: str = Form(""),
    log_arguments: str = Form(""),
    nested_installer_type: Optional[str] = Form(default=None),
    # These may appear multiple times when NestedInstallerType == portable
    nested_relative_file_path: List[str] = Form(default=[]),
    nested_portable_command_alias: List[str] = Form(default=[]),
    # Custom installer steps (for installer_type == "custom")
    custom_action_type: List[str] = Form(default=[]),
    custom_arg1: List[str] = Form(default=[]),
    custom_arg2: List[str] = Form(default=[]),
    # Install modes and elevation
    install_mode_interactive: bool = Form(True),
    install_mode_silent: bool = Form(True),
    install_mode_silent_with_progress: bool = Form(True),
    requires_elevation: bool = Form(False),
    # Package dependencies (logical)
    package_dependencies: List[str] = Form(default=[]),
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
    package_dir = data_dir / "owned" / package_id
    version_dir = package_dir / dir_name
    
    # Handle installer upload
    installer_file_name: Optional[str] = None
    installer_sha256: Optional[str] = None
    
    # Check if a file was actually uploaded (not just an empty UploadFile)
    # FastAPI may pass an UploadFile object even when no file is selected
    has_new_upload = upload is not None and upload.filename and len(upload.filename.strip()) > 0
    
    if has_new_upload:
        # New file uploaded
        installer_file_name, installer_sha256 = await _save_upload_and_hash(
            upload, version_dir
        )

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
    
    # Build nested installer metadata (manual nested installers for zip types)
    nested_type_value: Optional[str] = None
    nested_files_value: List[NestedInstallerFile] = []

    nested_installer_type = (nested_installer_type or "").strip() or None

    if installer_type == "zip" and nested_installer_type:
        nested_type_value = nested_installer_type
        # Normalize lists from the form
        paths = [p.strip() for p in nested_relative_file_path]
        aliases = [a.strip() for a in nested_portable_command_alias]

        if nested_installer_type == "portable":
            for idx, path in enumerate(paths):
                if not path:
                    continue
                alias = aliases[idx] if idx < len(aliases) else ""
                alias = alias.strip()
                nested_files_value.append(
                    NestedInstallerFile(
                        relative_file_path=path,
                        portable_command_alias=alias or None,
                    )
                )
        else:
            # For non-portable nested installers we currently support a single
            # RelativeFilePath entry.
            if paths and paths[0]:
                nested_files_value.append(
                    NestedInstallerFile(relative_file_path=paths[0], portable_command_alias=None)
                )

    # Build custom installer steps if requested
    custom_steps: List[CustomInstallerStep] = []
    if installer_type == "custom":
        for idx, action in enumerate(custom_action_type):
            action = (action or "").strip()
            arg1 = (custom_arg1[idx] if idx < len(custom_arg1) else "").strip()
            arg2 = (custom_arg2[idx] if idx < len(custom_arg2) else "").strip()

            # Skip completely empty rows
            if not action and not arg1 and not arg2:
                continue
            if not action:
                # Ignore rows without an action type
                continue

            custom_steps.append(
                CustomInstallerStep(
                    action_type=action,
                    argument1=arg1 or None,
                    argument2=arg2 or None,
                )
            )

    # Normalize package dependency identifiers
    normalized_dependencies = [d.strip() for d in package_dependencies if d.strip()]

    # Build the VersionMetadata object once and pass it through to helper
    # functions instead of plumbing individual fields repeatedly.
    meta = VersionMetadata(
        version=version,
        architecture=architecture,
        scope=scope,
        product_code=product_code or None,
        installer_type=installer_type,
        installer_file=installer_file_name,
        installer_sha256=installer_sha256,
        silent_arguments=silent_arguments or None,
        silent_with_progress_arguments=silent_with_progress_arguments or None,
        interactive_arguments=interactive_arguments or None,
        log_arguments=log_arguments or None,
        install_mode_interactive=install_mode_interactive,
        install_mode_silent=install_mode_silent,
        install_mode_silent_with_progress=install_mode_silent_with_progress,
        requires_elevation=requires_elevation,
        package_dependencies=normalized_dependencies,
        nested_installer_type=nested_type_value,
        nested_installer_files=nested_files_value,
        custom_installer_steps=custom_steps if installer_type == "custom" else [],
        release_date=existing_version.release_date if existing_version else None,
        release_notes=existing_version.release_notes if existing_version else None,
    )

    # For custom installers, build or rebuild the package.zip and compute
    # the SHA256 of the zip (which is what WinGet will download).
    if installer_type == "custom":
        if not meta.installer_file:
            return JSONResponse(
                status_code=400,
                content={"error": "Installer file is required for custom installer versions"},
            )
        meta.installer_sha256 = _build_custom_installer_package(
            version_dir=version_dir,
            meta=meta,
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


# ---------------------------------------------------------------------------
# WinGet Import Endpoints
# ---------------------------------------------------------------------------


def _get_winget_index_path() -> Path:
    """Get the path to the WinGet index database."""
    data_dir = get_data_dir()
    cache_dir = data_dir / "cache"
    index_path = cache_dir / "winget_index" / "index.db"
    
    if not index_path.exists():
        # Try alternative location
        index_path = cache_dir / "index.db"
    
    return index_path


@router.post("/admin/winget-index/update")
async def admin_update_winget_index() -> JSONResponse:
    """
    Force update the WinGet index by downloading the latest version.
    """
    try:
        cache_dir = get_data_dir() / "cache"
        index_path = await download_winget_index(cache_dir)
        
        return JSONResponse(
            status_code=200,
            content={
                "success": True,
                "message": "Index updated successfully",
                "index_path": str(index_path),
            },
        )
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"error": f"Failed to update index: {str(e)}"},
        )


@router.get("/admin/winget/search")
async def admin_winget_search(
    q: str = Query(..., description="Search query for package ID or name"),
) -> JSONResponse:
    """
    Search for packages in the official WinGet repository index.
    """
    index_path = _get_winget_index_path()
    
    if not index_path.exists():
        return JSONResponse(
            status_code=404,
            content={"error": "WinGet index not found. Please download it first."},
        )
    
    try:
        with WinGetIndexReader(index_path) as reader:
            # Simple search - you may want to enhance this
            reader.connect()
            cursor = reader.conn.cursor()
            cursor.execute("""
                SELECT DISTINCT 
                    i.id as package_id,
                    n.name as package_name,
                    p.publisher as publisher
                FROM ids i
                LEFT JOIN names n ON i.id = n.id
                LEFT JOIN publishers p ON i.id = p.id
                WHERE i.id LIKE ? OR n.name LIKE ?
                LIMIT 50
            """, (f"%{q}%", f"%{q}%"))
            
            results = [dict(row) for row in cursor.fetchall()]
            
        return JSONResponse(
            status_code=200,
            content={"success": True, "packages": results},
        )
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"error": f"Search failed: {str(e)}"},
        )


@router.get("/admin/winget/packages/{package_id}/versions")
async def admin_winget_package_versions(
    package_id: str,
    architecture: Optional[str] = Query(None, description="Filter by architecture (x86, x64, arm64)"),
    scope: Optional[str] = Query(None, description="Filter by scope (user, machine)"),
) -> JSONResponse:
    """
    Get available versions for a package from the WinGet repository.
    """
    index_path = _get_winget_index_path()
    
    if not index_path.exists():
        return JSONResponse(
            status_code=404,
            content={"error": "WinGet index not found. Please download it first."},
        )
    
    try:
        with WinGetIndexReader(index_path) as reader:
            versions = reader.get_package_versions(
                package_id,
                architecture=architecture,
                scope=scope
            )
            
        return JSONResponse(
            status_code=200,
            content={"success": True, "versions": versions},
        )
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"error": f"Failed to get versions: {str(e)}"},
        )


@router.post("/admin/winget/packages/{package_id}/import")
async def admin_winget_import_package(
    package_id: str,
    architectures: Optional[str] = Query(None, description="Comma-separated architectures (e.g., 'x64,x86')"),
    scopes: Optional[str] = Query(None, description="Comma-separated scopes (e.g., 'user,machine')"),
    installer_types: Optional[str] = Query(None, description="Comma-separated installer types"),
    version_mode: str = Query("latest", description="'latest' or 'all'"),
    version_filter: Optional[str] = Query(None, description="Version filter (e.g., '1.18.*')"),
) -> JSONResponse:
    """
    Import a package from the official WinGet repository.
    """
    index_path = _get_winget_index_path()
    
    if not index_path.exists():
        return JSONResponse(
            status_code=404,
            content={"error": "WinGet index not found. Please download it first."},
        )
    
    # Parse comma-separated values
    arch_list = [a.strip() for a in architectures.split(",")] if architectures else None
    scope_list = [s.strip() for s in scopes.split(",")] if scopes else None
    type_list = [t.strip() for t in installer_types.split(",")] if installer_types else None
    
    try:
        importer = WinGetPackageImporter(index_path)
        result = await importer.import_package(
            package_id,
            architectures=arch_list,
            scopes=scope_list,
            installer_types=type_list,
            version_mode=version_mode,
            version_filter=version_filter,
            track_cache=True
        )
        importer.close()
        
        # Rebuild index to include imported package
        build_index_from_disk()
        
        return JSONResponse(
            status_code=200,
            content={"success": True, "result": result},
        )
    except ValueError as e:
        return JSONResponse(
            status_code=400,
            content={"error": str(e)},
        )
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"error": f"Import failed: {str(e)}"},
        )


# ---------------------------------------------------------------------------
# Cached Packages Management Endpoints
# ---------------------------------------------------------------------------


@router.get("/admin/cached-packages/new/fragment", response_class=HTMLResponse)
async def admin_new_cached_package_fragment(request: Request) -> HTMLResponse:
    """
    Fragment for adding a new cached package (modal form).
    """
    config = get_repository_config()
    return templates.TemplateResponse(
        "admin_cached_package_form_fragment.html",
        {
            "request": request,
            "architectures": config.architecture_options,
            "scopes": config.scope_options,
            "installer_types": config.installer_type_options,
        },
    )


@router.post("/admin/cached-packages/new")
async def admin_new_cached_package(
    package_id: str = Form(...),
    architectures: Optional[str] = Form(None),
    scopes: Optional[str] = Form(None),
    installer_types: Optional[str] = Form(None),
    version_mode: str = Form("latest"),
    version_filter: Optional[str] = Form(None),
    ad_group_scopes_group: Optional[List[str]] = Form(None),
    ad_group_scopes_scope: Optional[List[str]] = Form(None),
) -> JSONResponse:
    """
    Import a new package from WinGet repository and add it to cache.
    """
    index_path = _get_winget_index_path()
    
    if not index_path.exists():
        return JSONResponse(
            status_code=404,
            content={"error": "WinGet index not found. Please download it first."},
        )
    
    # Parse comma-separated values (empty string means "all" for installer_types)
    arch_list = [a.strip() for a in architectures.split(",") if a.strip()] if architectures and architectures.strip() else None
    scope_list = [s.strip() for s in scopes.split(",") if s.strip()] if scopes and scopes.strip() else None
    # Empty string or None both mean "all types"
    # Also, if all available types are selected, treat as "all types" (None)
    type_list = None
    if installer_types and installer_types.strip():
        parsed_types = [t.strip() for t in installer_types.split(",") if t.strip()]
        # Get all available installer types from config
        config = get_repository_config()
        all_types = set(config.installer_type_options)
        selected_types = set(parsed_types)
        # If all types are selected, treat as None (no filter)
        if selected_types == all_types:
            type_list = None
        else:
            type_list = parsed_types
    
    try:
        importer = WinGetPackageImporter(index_path)
        ad_group_scopes_entries = _parse_ad_group_scopes(ad_group_scopes_group, ad_group_scopes_scope)
        result = await importer.import_package(
            package_id,
            architectures=arch_list,
            scopes=scope_list,
            installer_types=type_list,
            version_mode=version_mode,
            version_filter=version_filter,
            track_cache=True,
            ad_group_scopes=ad_group_scopes_entries,
        )
        importer.close()
        
        # Rebuild index to include imported package
        build_index_from_disk()
        
        return JSONResponse(
            status_code=200,
            content={"success": True, "result": result},
        )
    except ValueError as e:
        return JSONResponse(
            status_code=400,
            content={"error": str(e)},
        )
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"error": f"Import failed: {str(e)}"},
        )


@router.get("/admin/cached-packages/{package_id}", response_class=HTMLResponse)
async def admin_cached_package_detail(
    request: Request,
    package_id: str,
) -> HTMLResponse:
    """
    View and manage a cached package.
    """
    config = get_repository_config()
    
    # Get cached package from repository index
    repo_index = get_repository_index()
    package_index = repo_index.packages.get(package_id)
    if not package_index or not getattr(package_index.package, "cached", False):
        raise HTTPException(status_code=404, detail="Cached package not found")
    
    cached_versions = sorted(
        package_index.versions,
        key=lambda v: (v.version or "", v.architecture or "", v.scope or ""),
        reverse=True,
    )
    
    return templates.TemplateResponse(
        "admin_cached_package_detail.html",
        {
            "request": request,
            "title": f"Cached Package: {package_index.package.package_name}",
            "package": package_index.package,
            "cached_versions": cached_versions,
            "architectures": config.architecture_options,
            "scopes": config.scope_options,
            "installer_types": config.installer_type_options,
        },
    )


@router.get("/admin/cached-packages/{package_id}/fragment", response_class=HTMLResponse)
async def admin_cached_package_form_fragment(
    request: Request,
    package_id: str,
) -> HTMLResponse:
    """
    Fragment for editing an existing cached package (modal form).
    """
    config = get_repository_config()
    repo_index = get_repository_index()
    package_index = repo_index.packages.get(package_id)
    if not package_index or not getattr(package_index.package, "cached", False):
        raise HTTPException(status_code=404, detail="Cached package not found")

    return templates.TemplateResponse(
        "admin_cached_package_form_fragment.html",
        {
            "request": request,
            "package": package_index.package,
            "architectures": config.architecture_options,
            "scopes": config.scope_options,
            "installer_types": config.installer_type_options,
        },
    )


@router.post("/admin/cached-packages/{package_id}")
async def admin_save_cached_package(
    package_id: str,
    package_id_from_form: str = Form(..., alias="package_id"),
    architectures: Optional[str] = Form(None),
    scopes: Optional[str] = Form(None),
    installer_types: Optional[str] = Form(None),
    version_mode: str = Form("latest"),
    version_filter: Optional[str] = Form(None),
    ad_group_scopes_group: Optional[List[str]] = Form(None),
    ad_group_scopes_scope: Optional[List[str]] = Form(None),
) -> JSONResponse:
    """
    Update cache settings for an existing cached package and re-import to apply filters.
    """
    # Validate package identifier matches URL parameter
    if package_id_from_form != package_id:
        return JSONResponse(
            status_code=400,
            content={"error": "Package identifier mismatch"},
        )

    repo_index = get_repository_index()
    pkg_index = repo_index.packages.get(package_id)
    if not pkg_index or not getattr(pkg_index.package, "cached", False):
        return JSONResponse(
            status_code=404,
            content={"error": "Cached package not found"},
        )

    # Normalize filters. Empty lists mean "all".
    arch_list = (
        [a.strip() for a in architectures.split(",") if a.strip()]
        if architectures and architectures.strip()
        else []
    )
    scope_list = (
        [s.strip() for s in scopes.split(",") if s.strip()]
        if scopes and scopes.strip()
        else []
    )

    type_list: List[str] = []
    if installer_types and installer_types.strip():
        parsed_types = [t.strip() for t in installer_types.split(",") if t.strip()]
        config = get_repository_config()
        all_types = set(config.installer_type_options)
        selected_types = set(parsed_types)
        # If all types are selected, treat as empty (no filter)
        if selected_types != all_types:
            type_list = parsed_types

    version_mode = (version_mode or "latest").strip() or "latest"
    if version_mode not in ("latest", "all"):
        return JSONResponse(
            status_code=400,
            content={"error": "Invalid version_mode (must be 'latest' or 'all')"},
        )

    version_filter = (version_filter or "").strip() or None

    ad_group_scopes_entries = _parse_ad_group_scopes(ad_group_scopes_group, ad_group_scopes_scope)

    new_cache_settings = CacheSettings(
        architectures=arch_list,
        scopes=scope_list,
        installer_types=type_list,
        version_mode=version_mode,
        version_filter=version_filter,
        auto_update=pkg_index.package.cache_settings.auto_update
        if pkg_index.package.cache_settings
        else True,
    )

    # Persist updated cache settings to package.json
    data_dir = get_data_dir()
    if not pkg_index.storage_path:
        return JSONResponse(
            status_code=500,
            content={"error": "Package storage path is not available"},
        )
    package_dir = data_dir / pkg_index.storage_path
    package_json = package_dir / "package.json"

    pkg = pkg_index.package
    updated_pkg = PackageCommonMetadata(
        package_identifier=pkg.package_identifier,
        package_name=pkg.package_name,
        publisher=pkg.publisher,
        short_description=pkg.short_description,
        license=pkg.license,
        tags=pkg.tags,
        homepage=pkg.homepage,
        support_url=pkg.support_url,
        ad_group_scopes=ad_group_scopes_entries or getattr(pkg, "ad_group_scopes", []) or [],
        is_example=pkg.is_example,
        cached=True,
        cache_settings=new_cache_settings,
    )
    package_json.write_text(updated_pkg.model_dump_json(indent=2), encoding="utf-8")

    # Re-import with new filters to make cached versions reflect updated settings.
    index_path = _get_winget_index_path()
    if not index_path.exists():
        return JSONResponse(
            status_code=404,
            content={"error": "WinGet index not found"},
        )

    try:
        importer = WinGetPackageImporter(index_path)
        await importer.import_package(
            package_id,
            architectures=arch_list if arch_list else None,
            scopes=scope_list if scope_list else None,
            installer_types=type_list if type_list else None,
            version_mode=version_mode,
            version_filter=version_filter,
            track_cache=True,
            ad_group_scopes=ad_group_scopes_entries or getattr(pkg, "ad_group_scopes", []) or [],
        )
        importer.close()

        build_index_from_disk()
        return JSONResponse(
            status_code=200,
            content={"success": True, "message": "Cached package updated successfully"},
        )
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"error": f"Update failed: {str(e)}"},
    )


@router.post("/admin/cached-packages/{package_id}/update-filters")
async def admin_cached_package_update_filters(
    package_id: str,
    architectures: Optional[str] = Query(None),
    scopes: Optional[str] = Query(None),
    installer_types: Optional[str] = Query(None),
    version_mode: str = Query("latest"),
    version_filter: Optional[str] = Query(None),
) -> JSONResponse:
    """Update filters for a cached package and re-import."""
    repo_index = get_repository_index()
    pkg_index = repo_index.packages.get(package_id)
    if not pkg_index or not getattr(pkg_index.package, "cached", False):
        return JSONResponse(
            status_code=404,
            content={"error": "Cached package not found"},
        )
    
    # Parse filters (empty string means "all")
    arch_list = [a.strip() for a in architectures.split(",") if a.strip()] if architectures and architectures.strip() else []
    scope_list = [s.strip() for s in scopes.split(",") if s.strip()] if scopes and scopes.strip() else []
    # Empty string or None both mean "all types"
    type_list = []
    if installer_types and installer_types.strip():
        parsed_types = [t.strip() for t in installer_types.split(",") if t.strip()]
        # Get all available installer types from config
        config = get_repository_config()
        all_types = set(config.installer_type_options)
        selected_types = set(parsed_types)
        # If all types are selected, treat as empty (no filter)
        if selected_types != all_types:
            type_list = parsed_types
    
    # Update cache settings (empty lists mean "all")
    new_cache_settings = CacheSettings(
        architectures=arch_list,
        scopes=scope_list,
        installer_types=type_list,
        version_mode=version_mode,
        version_filter=version_filter,
        auto_update=pkg_index.package.cache_settings.auto_update if pkg_index.package.cache_settings else True,
    )
    
    # Persist to package.json
    data_dir = get_data_dir()
    if not pkg_index.storage_path:
        return JSONResponse(
            status_code=500,
            content={"error": "Package storage path is not available"},
        )
    package_dir = data_dir / pkg_index.storage_path
    package_json = package_dir / "package.json"
    pkg = pkg_index.package
    updated_pkg = PackageCommonMetadata(
        package_identifier=pkg.package_identifier,
        package_name=pkg.package_name,
        publisher=pkg.publisher,
        short_description=pkg.short_description,
        license=pkg.license,
        tags=pkg.tags,
        homepage=pkg.homepage,
        support_url=pkg.support_url,
        ad_group_scopes=getattr(pkg, "ad_group_scopes", []) or [],
        is_example=pkg.is_example,
        cached=True,
        cache_settings=new_cache_settings,
    )
    package_json.write_text(updated_pkg.model_dump_json(indent=2), encoding="utf-8")
    
    # Re-import with new filters
    index_path = _get_winget_index_path()
    if not index_path.exists():
        return JSONResponse(
            status_code=404,
            content={"error": "WinGet index not found"},
        )
    
    try:
        importer = WinGetPackageImporter(index_path)
        # Empty lists mean "all" (no filter), so pass None to importer
        result = await importer.import_package(
            package_id,
            architectures=arch_list if arch_list else None,
            scopes=scope_list if scope_list else None,
            installer_types=type_list if type_list else None,
            version_mode=version_mode,
            version_filter=version_filter,
            track_cache=True,
            ad_group_scopes=getattr(pkg, "ad_group_scopes", []) or [],
        )
        importer.close()
        
        build_index_from_disk()
        
        return JSONResponse(
            status_code=200,
            content={"success": True, "result": result},
        )
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"error": f"Update failed: {str(e)}"},
        )


@router.post("/admin/cached-packages/{package_id}/delete")
async def admin_cached_package_delete(package_id: str) -> JSONResponse:
    """Remove a package from cache tracking."""
    repo_index = get_repository_index()
    pkg_index = repo_index.packages.get(package_id)
    if not pkg_index or not getattr(pkg_index.package, "cached", False):
        return JSONResponse(
            status_code=404,
            content={"error": "Cached package not found"},
        )
    
    data_dir = get_data_dir()
    if not pkg_index.storage_path:
        return JSONResponse(
            status_code=500,
            content={"error": "Package storage path is not available"},
        )
    package_dir = data_dir / pkg_index.storage_path
    if package_dir.is_dir():
        for child in package_dir.iterdir():
            if child.is_file():
                child.unlink(missing_ok=True)
            elif child.is_dir():
                for version_file in child.iterdir():
                    if version_file.is_file():
                        version_file.unlink(missing_ok=True)
                child.rmdir()
        package_dir.rmdir()
    
    build_index_from_disk()
    
    return JSONResponse(
        status_code=200,
        content={"success": True, "message": "Package removed from cache"},
    )


@router.post("/admin/cached-packages/{package_id}/versions/{version_id}/delete")
async def admin_delete_cached_version(
    package_id: str,
    version_id: str,
) -> JSONResponse:
    """
    Delete a cached version (without removing the whole cached package).
    """
    # Decode URL-encoded version_id
    version_id = urllib.parse.unquote(version_id)

    repo_index = get_repository_index()
    pkg_index = repo_index.packages.get(package_id)
    if not pkg_index or not getattr(pkg_index.package, "cached", False):
        return JSONResponse(
            status_code=404,
            content={"error": "Cached package not found"},
        )

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
        try:
            version_dir.rmdir()
        except OSError:
            # If directory still contains subfolders, remove them as well.
            for child in version_dir.iterdir():
                if child.is_dir():
                    for f in child.iterdir():
                        if f.is_file():
                            f.unlink(missing_ok=True)
                    child.rmdir()
            version_dir.rmdir()

    build_index_from_disk()
    return JSONResponse(
        status_code=200,
        content={"success": True, "message": "Cached version deleted successfully"},
    )
