"""
Admin API endpoints for managing packages, versions, and cached packages.

This module provides the administrative interface for:
- Creating and editing packages and versions
    - Managing cached packages from the upstream repository index
- Importing packages from upstream WinGet repository
- Managing custom installers with package.zip generation
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional, List
import hashlib
import urllib.parse
import zipfile
import tempfile
import shutil
import uuid
import json

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

from app.services.authentication import SESSION_COOKIE_NAME, get_user_for_session
from app.domain.models import (
    PackageCommonMetadata,
    VersionMetadata,
    NestedInstallerFile,
    CustomInstallerStep,
    AuthUser,
    ADGroupScopeEntry,
    CacheSettings,
)
from app.domain.entities import Repository
from app.core.dependencies import get_repository, get_caching_service
from app.services.caching import CachingService
from app import custom_installer


templates = Jinja2Templates(directory="app/templates")


async def require_admin_session(request: Request) -> AuthUser:
    """
    Dependency function to require an authenticated admin session.
    
    For GET requests, redirects to login page. For other requests,
    returns 401 Unauthorized if no valid session is found.
    
    Args:
        request: The FastAPI request object.
        
    Returns:
        The authenticated user object.
        
    Raises:
        HTTPException: If user is not authenticated.
    """
    session_id = request.cookies.get(SESSION_COOKIE_NAME)
    user = get_user_for_session(session_id) if session_id else None

    if user is None:
        if request.method.upper() == "GET":
            raise HTTPException(
                status_code=status.HTTP_307_TEMPORARY_REDIRECT,
                headers={"Location": "/login"},
            )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
        )

    return user


router = APIRouter(dependencies=[Depends(require_admin_session)])


def _get_package_or_404(repo: Repository, package_id: str):
    """
    Retrieve a package by ID or raise 404 if not found.
    
    Args:
        repo: The repository instance.
        package_id: The package identifier.
        
    Returns:
        The package entity.
        
    Raises:
        HTTPException: 404 if package not found.
    """
    pkg = repo.get_package(package_id)
    if not pkg:
        raise HTTPException(status_code=404, detail="Package not found")
    return pkg


def _get_version_by_id(
    repo: Repository, 
    package_id: str, 
    version_id: str
) -> Optional[VersionMetadata]:
    """
    Find a version by its composite ID.
    
    Version IDs can be in two formats:
    - Without GUID: "{version}-{architecture}-{scope}"
    - With GUID: "{version}-{architecture}-{scope}-{installer_guid}"
    
    Args:
        repo: The repository instance.
        package_id: The package identifier.
        version_id: The version identifier (URL-decoded).
        
    Returns:
        The version metadata if found, None otherwise.
    """
    pkg = _get_package_or_404(repo, package_id)
    for v in pkg.versions:
        parts_no_guid = [v.version, v.architecture]
        parts_no_guid.append(v.scope if v.scope else "user")
        
        id_no_guid = "-".join(parts_no_guid)
        
        id_with_guid = None
        if v.installer_guid:
            id_with_guid = f"{id_no_guid}-{v.installer_guid}"
            
        if version_id == id_no_guid or (id_with_guid and version_id == id_with_guid):
            return v
    return None


def _parse_ad_group_scopes(
    groups: Optional[List[str]],
    scopes: Optional[List[str]],
    repo: Repository
) -> List[ADGroupScopeEntry]:
    """
    Parse and validate AD group scope mappings from form data.
    
    Takes parallel lists of AD groups and scopes, validates that scopes
    are allowed by repository configuration, and returns a list of
    ADGroupScopeEntry objects.
    
    Args:
        groups: List of AD group names (can be None or empty).
        scopes: List of scope values corresponding to groups.
        repo: Repository instance for configuration access.
        
    Returns:
        List of validated ADGroupScopeEntry objects.
    """
    if not groups and not scopes:
        return []
    groups = groups or []
    scopes = scopes or []
    n = min(len(groups), len(scopes))
    config = repo.db.get_repository_config()
    allowed_scopes = set(config.scope_options or ["user", "machine"])

    result: List[ADGroupScopeEntry] = []
    for i in range(n):
        g = (groups[i] or "").strip()
        s = (scopes[i] or "").strip()
        if not g:
            continue
        if s not in allowed_scopes:
            continue
        result.append(ADGroupScopeEntry(ad_group=g, scope=s))
    return result


def _build_custom_installer_package(
    work_dir: Path,
    meta: VersionMetadata,
    installer_path: Path
) -> tuple[Path, str]:
    """
    Build a custom installer package.zip file.
    
    Creates a ZIP file containing:
    - The original installer file (e.g., setup.exe)
    - An install.bat script generated from the custom installer steps
    
    The package.zip is what gets served to clients for custom installers.
    
    Args:
        work_dir: Temporary directory for building the package.
        meta: Version metadata containing custom installer configuration.
        installer_path: Path to the original installer file.
        
    Returns:
        Tuple of (package_zip_path, sha256_hash) for the generated package.
    """
    script_text = custom_installer.render_install_script(meta=meta)
    script_path = work_dir / "install.bat"
    script_path.write_text(script_text, encoding="utf-8", newline="\r\n")

    package_zip_path = work_dir / "package.zip"
    with zipfile.ZipFile(package_zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(installer_path, arcname=meta.installer_file)
        zf.write(script_path, arcname="install.bat")

    hasher = hashlib.sha256()
    with package_zip_path.open("rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            hasher.update(chunk)

    return package_zip_path, hasher.hexdigest()


# ---------------------------------------------------------------------------
# Package list
# ---------------------------------------------------------------------------


@router.get("/admin/packages", response_class=HTMLResponse)
async def admin_list_packages(
    request: Request,
    repo: Repository = Depends(get_repository),
    caching_service: CachingService = Depends(get_caching_service),
) -> HTMLResponse:
    """
    Display the admin package list page.
    
    Shows two separate lists:
    - Owned packages: Packages created/edited directly in this repository
    - Cached packages: Packages imported from the upstream repository index with caching enabled
    
    Also displays the last time the upstream repository index was updated.
    
    Args:
        request: FastAPI request object.
        repo: Repository dependency.
        caching_service: Caching service dependency.
        
    Returns:
        HTML template response with package lists.
    """
    packages = repo.get_all_packages()

    owned_packages = []
    cached_packages_data = []
    
    for pkg in packages:
        versions = sorted(
            pkg.versions,
            key=lambda v: v.version,
            reverse=True
        )
        latest_version = versions[0].version if versions else None
        
        if pkg.metadata.cached:
            cs = pkg.metadata.cache_settings
            cached_packages_data.append({
                "package_id": pkg.package_id,
                "package_name": pkg.metadata.package_name,
                "publisher": pkg.metadata.publisher,
                "latest_version": latest_version,
                "filters": {
                    "architectures": ", ".join(cs.architectures) if cs and cs.architectures else "All",
                    "scopes": ", ".join(cs.scopes) if cs and cs.scopes else "All",
                    "installer_types": ", ".join(cs.installer_types) if cs and cs.installer_types else "All",
                    "version_mode": cs.version_mode if cs else "latest",
                },
                "version_count": len(pkg.versions),
            })
        else:
            owned_packages.append({
                "id": pkg.package_id,
                "name": pkg.metadata.package_name,
                "latest_version": latest_version,
            })
            
    owned_packages.sort(key=lambda x: x["id"])
    cached_packages_data.sort(key=lambda x: x["package_id"])
    
    status_info = caching_service.get_index_status()
    index_last_pulled = None
    if status_info.get("last_pulled"):
        index_last_pulled = status_info["last_pulled"].strftime('%Y-%m-%d %H:%M:%S')

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
async def admin_package_detail(
    package_id: str, 
    request: Request,
    repo: Repository = Depends(get_repository)
) -> HTMLResponse:
    """
    Display the package detail page showing all versions.
    
    Args:
        package_id: The package identifier.
        request: FastAPI request object.
        repo: Repository dependency.
        
    Returns:
        HTML template response with package details and versions.
    """
    pkg = _get_package_or_404(repo, package_id)
    
    versions = sorted(
        pkg.versions,
        key=lambda v: (v.version or "", v.architecture or "", v.scope or ""),
        reverse=True,
    )

    return templates.TemplateResponse(
        "admin_package_detail.html",
        {
            "request": request,
            "package_id": package_id,
            "package": pkg.metadata,
            "versions": versions,
        },
    )


@router.get("/admin/packages/new/fragment", response_class=HTMLResponse)
async def admin_package_form_fragment_new(
    request: Request,
    repo: Repository = Depends(get_repository)
) -> HTMLResponse:
    """
    Return the HTML fragment for creating a new package.
    
    Args:
        request: FastAPI request object.
        repo: Repository dependency.
        
    Returns:
        HTML fragment template for new package form.
    """
    config = repo.db.get_repository_config()
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
    repo: Repository = Depends(get_repository)
) -> HTMLResponse:
    """
    Return the HTML fragment for editing an existing package.
    
    Args:
        package_id: The package identifier.
        request: FastAPI request object.
        repo: Repository dependency.
        
    Returns:
        HTML fragment template for package edit form.
    """
    pkg = repo.get_package(package_id)
    config = repo.db.get_repository_config()
    
    return templates.TemplateResponse(
        "admin_package_form_fragment.html",
        {
            "request": request,
            "package_id": package_id,
            "package": pkg.metadata if pkg else None,
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
    repo: Repository = Depends(get_repository)
) -> JSONResponse:
    """
    Create a new package in the repository.
    
    Args:
        package_identifier: Unique package identifier (e.g., "Publisher.PackageName").
        package_name: Display name of the package.
        publisher: Publisher name.
        short_description: Optional short description.
        license: Optional license information.
        tags: Comma-separated list of tags.
        ad_group_scopes_group: List of AD group names for scope restrictions.
        ad_group_scopes_scope: List of scope values corresponding to groups.
        repo: Repository dependency.
        
    Returns:
        JSON response with success status or error message.
    """
    if repo.get_package(package_identifier):
        return JSONResponse(status_code=400, content={"error": "Package already exists"})
    
    new_package = PackageCommonMetadata(
        package_identifier=package_identifier,
        package_name=package_name,
        publisher=publisher,
        short_description=short_description or None,
        license=license or None,
        tags=[t.strip() for t in tags.split(",") if t.strip()],
        ad_group_scopes=_parse_ad_group_scopes(ad_group_scopes_group, ad_group_scopes_scope, repo),
        cached=False,
        cache_settings=None,
    )
    
    repo.db.save_package(new_package)
    
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
    repo: Repository = Depends(get_repository)
) -> JSONResponse:
    """
    Update an existing package's metadata.
    
    Preserves existing fields like homepage, support_url, and cache settings
    that are not editable through this form.
    
    Args:
        package_id: The package identifier from the URL path.
        package_identifier: The package identifier from the form (must match).
        package_name: Display name of the package.
        publisher: Publisher name.
        short_description: Optional short description.
        license: Optional license information.
        tags: Comma-separated list of tags.
        ad_group_scopes_group: List of AD group names for scope restrictions.
        ad_group_scopes_scope: List of scope values corresponding to groups.
        repo: Repository dependency.
        
    Returns:
        JSON response with success status or error message.
    """
    if package_identifier != package_id:
        return JSONResponse(status_code=400, content={"error": "Package identifier mismatch"})

    pkg = repo.get_package(package_id)
    
    if pkg:
        current = pkg.metadata
        updated = PackageCommonMetadata(
            package_identifier=current.package_identifier,
            package_name=package_name,
            publisher=publisher,
            short_description=short_description or None,
            license=license or None,
            tags=[t.strip() for t in tags.split(",") if t.strip()],
            homepage=current.homepage,
            support_url=current.support_url,
            ad_group_scopes=_parse_ad_group_scopes(ad_group_scopes_group, ad_group_scopes_scope, repo)
            or getattr(current, "ad_group_scopes", []) or [],
            is_example=current.is_example,
            cached=current.cached,
            cache_settings=current.cache_settings,
        )
    else:
        updated = PackageCommonMetadata(
            package_identifier=package_id,
            package_name=package_name,
            publisher=publisher,
            short_description=short_description or None,
            license=license or None,
            tags=[t.strip() for t in tags.split(",") if t.strip()],
            ad_group_scopes=_parse_ad_group_scopes(ad_group_scopes_group, ad_group_scopes_scope, repo),
            cached=False,
            cache_settings=None,
        )

    repo.db.save_package(updated)

    return JSONResponse(
        status_code=200,
        content={"success": True, "message": "Package saved successfully"},
    )


# ---------------------------------------------------------------------------
# Version form fragment
# ---------------------------------------------------------------------------


@router.get("/admin/packages/{package_id}/versions/{version_id}/fragment", response_class=HTMLResponse)
async def admin_version_form_fragment(
    package_id: str,
    version_id: str,
    request: Request,
    clone_from: Optional[str] = Query(default=None),
    repo: Repository = Depends(get_repository)
) -> HTMLResponse:
    """
    Return the HTML fragment for creating or editing a version.
    
    Supports cloning from an existing version via the clone_from query parameter.
    When cloning, all installer settings are copied except version, file, and SHA256.
    
    Args:
        package_id: The package identifier.
        version_id: The version identifier (URL-encoded, "new" for new versions).
        request: FastAPI request object.
        clone_from: Optional version ID to clone settings from.
        repo: Repository dependency.
        
    Returns:
        HTML fragment template for version form.
    """
    _get_package_or_404(repo, package_id)
    config = repo.db.get_repository_config()
    
    version_id = urllib.parse.unquote(version_id)
    
    version_meta = None
    if version_id != "new":
        version_meta = _get_version_by_id(repo, package_id, version_id)
    
    cloned_meta = None
    if clone_from:
        clone_from = urllib.parse.unquote(clone_from)
        source_version = _get_version_by_id(repo, package_id, clone_from)
        if source_version:
            cloned_meta = VersionMetadata(
                version="", 
                architecture=source_version.architecture,
                scope=source_version.scope,
                installer_type=source_version.installer_type,
                installer_file=None, 
                installer_sha256=None,
                silent_arguments=source_version.silent_arguments,
                silent_with_progress_arguments=getattr(source_version, "silent_with_progress_arguments", None),
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
    
    form_meta = cloned_meta if cloned_meta else version_meta
    
    return templates.TemplateResponse(
        "admin_version_form_fragment.html",
        {
            "request": request,
            "package_id": package_id,
            "version_id": "new" if clone_from else version_id,
            "version_meta": form_meta,
            "architecture_options": config.architecture_options,
            "scope_options": config.scope_options,
            "installer_type_options": config.installer_type_options,
            "nested_installer_type_options": config.nested_installer_type_options,
            "custom_actions": custom_installer.get_available_actions(),
            "all_package_ids": list(repo.db.get_repository_index().packages.keys()),
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
    nested_relative_file_path: List[str] = Form(default=[]),
    nested_portable_command_alias: List[str] = Form(default=[]),
    custom_action_type: List[str] = Form(default=[]),
    custom_args_json: List[str] = Form(default=[]),
    install_mode_interactive: bool = Form(True),
    install_mode_silent: bool = Form(True),
    install_mode_silent_with_progress: bool = Form(True),
    requires_elevation: bool = Form(False),
    package_dependencies: List[str] = Form(default=[]),
    upload: UploadFile | None = File(default=None),
    repo: Repository = Depends(get_repository)
) -> JSONResponse:
    """
    Create or update a package version.
    
    Handles both standard installers and custom installers. For custom installers,
    generates a package.zip file containing the installer and install.bat script.
    
    Args:
        package_id: The package identifier.
        version_id: The version identifier (URL-encoded, "new" for new versions).
        version: Version string (e.g., "1.0.0").
        architecture: Target architecture (e.g., "x64", "x86", "arm64").
        scope: Installation scope ("user" or "machine").
        product_code: Optional product code for MSI installers.
        installer_type: Type of installer ("exe", "msi", "zip", "custom", etc.).
        silent_arguments: Silent installation arguments.
        silent_with_progress_arguments: Silent installation with progress arguments.
        interactive_arguments: Interactive installation arguments.
        log_arguments: Logging arguments.
        nested_installer_type: For ZIP installers, the nested installer type.
        nested_relative_file_path: For ZIP installers, relative paths to nested files.
        nested_portable_command_alias: For portable ZIP installers, command aliases.
        custom_action_type: For custom installers, list of action types.
        custom_args_json: For custom installers, JSON-encoded arguments dictionary for each action.
        install_mode_interactive: Whether interactive mode is supported.
        install_mode_silent: Whether silent mode is supported.
        install_mode_silent_with_progress: Whether silent with progress mode is supported.
        requires_elevation: Whether elevation is required.
        package_dependencies: List of package IDs this version depends on.
        upload: Optional uploaded installer file.
        repo: Repository dependency.
        
    Returns:
        JSON response with success status or error message.
    """
    _get_package_or_404(repo, package_id)
    version_id = urllib.parse.unquote(version_id)
    
    # Check if this is an update to an existing version
    existing_version = None
    if version_id != "new":
        existing_version = _get_version_by_id(repo, package_id, version_id)
        
    # Validate that we have an installer file (either new upload or existing)
    has_new_upload = upload is not None and upload.filename and len(upload.filename.strip()) > 0
    if not has_new_upload and not existing_version:
        return JSONResponse(
            status_code=400,
            content={"error": "Installer file is required for new versions"}
        )

    nested_type_value = None
    nested_files_value = []
    nested_installer_type = (nested_installer_type or "").strip() or None

    if installer_type == "zip" and nested_installer_type:
        nested_type_value = nested_installer_type
        paths = [p.strip() for p in nested_relative_file_path]
        aliases = [a.strip() for a in nested_portable_command_alias]

        if nested_installer_type == "portable":
            for idx, path in enumerate(paths):
                if not path: continue
                alias = aliases[idx] if idx < len(aliases) else ""
                nested_files_value.append(NestedInstallerFile(relative_file_path=path, portable_command_alias=alias.strip() or None))
        else:
            if paths and paths[0]:
                nested_files_value.append(NestedInstallerFile(relative_file_path=paths[0], portable_command_alias=None))

    custom_steps = []
    if installer_type == "custom":
        for idx, action in enumerate(custom_action_type):
            action = (action or "").strip()
            if not action: continue
            
            # Parse JSON arguments
            arguments_dict = {}
            if idx < len(custom_args_json) and custom_args_json[idx] and custom_args_json[idx].strip():
                try:
                    parsed_args = json.loads(custom_args_json[idx])
                    if isinstance(parsed_args, dict):
                        # Filter out empty values
                        arguments_dict = {k: v for k, v in parsed_args.items() if v and str(v).strip()}
                except (json.JSONDecodeError, TypeError):
                    pass
            
            # Create step with arguments dict (always set, even if empty)
            custom_steps.append(CustomInstallerStep(
                action_type=action,
                arguments=arguments_dict
            ))

    normalized_dependencies = [d.strip() for d in package_dependencies if d.strip()]

    # Build version metadata object
    meta = VersionMetadata(
        version=version,
        architecture=architecture,
        scope=scope,
        product_code=product_code or None,
        installer_type=installer_type,
        installer_file=None,  # Set later based on upload or existing
        installer_sha256=None,  # Set later after hashing
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
        custom_installer_steps=custom_steps,
        release_date=existing_version.release_date if existing_version else None,
        release_notes=existing_version.release_notes if existing_version else None,
    )
    
    # Preserve installer GUID if updating existing version
    if existing_version:
        meta.installer_guid = existing_version.installer_guid
    
    # Ensure GUID exists for new versions (needed for custom installer path resolution)
    if not meta.installer_guid:
        meta.installer_guid = str(uuid.uuid4())
    
    with tempfile.TemporaryDirectory() as tmpdirname:
        work_dir = Path(tmpdirname)
        file_to_add: Optional[Path] = None
        pkg_zip: Optional[Path] = None
        
        # Handle file upload or reuse existing file
        if has_new_upload:
            upload_path = work_dir / upload.filename
            content = await upload.read()
            upload_path.write_bytes(content)
            meta.installer_file = upload.filename
            
            # For standard installers, hash the file now
            # For custom installers, hash the package.zip later
            if installer_type != "custom":
                h = hashlib.sha256()
                h.update(content)
                meta.installer_sha256 = h.hexdigest()
            
            file_to_add = upload_path
        else:
            # Reuse existing installer file
            meta.installer_file = existing_version.installer_file
            # For standard installers, keep existing SHA256
            # For custom installers, we'll regenerate package.zip and recalculate SHA256
            if installer_type != "custom":
                meta.installer_sha256 = existing_version.installer_sha256

        # Handle custom installer package.zip generation
        if installer_type == "custom":
            installer_source_path: Optional[Path] = None
            
            if has_new_upload:
                installer_source_path = upload_path
            elif existing_version:
                # Retrieve the existing installer file from storage
                # db.get_file_path returns the raw installer file path (not package.zip)
                try:
                    stored_file_path = repo.db.get_file_path(package_id, existing_version)
                    if stored_file_path.exists():
                        installer_source_path = stored_file_path
                except Exception:
                    pass

            if not installer_source_path or not installer_source_path.exists():
                return JSONResponse(
                    status_code=400,
                    content={"error": "Original installer file not found for custom package generation"}
                )

            # Generate package.zip containing installer + install.bat
            pkg_zip, zip_hash = _build_custom_installer_package(work_dir, meta, installer_source_path)
            meta.installer_sha256 = zip_hash

        # Save version metadata and installer file
        if has_new_upload:
            repo.db.add_installer(package_id, meta, file_path=file_to_add)
        else:
            repo.db.update_installer(package_id, meta)

        # Save package.zip sidecar for custom installers
        if installer_type == "custom" and pkg_zip and pkg_zip.exists():
            try:
                # Get the storage path where the installer file was saved
                saved_path = repo.db.get_file_path(package_id, meta)
                # saved_path points to the installer_file (e.g., setup.exe)
                # package.zip should be in the same directory
                package_zip_dest = saved_path.parent / "package.zip"
                shutil.copy2(pkg_zip, package_zip_dest)
            except Exception as e:
                return JSONResponse(
                    status_code=500,
                    content={"error": f"Failed to save custom installer package: {str(e)}"}
                )
        
    return JSONResponse(
        status_code=200,
        content={"success": True, "message": "Version saved successfully"}
    )


@router.post("/admin/packages/{package_id}/versions/{version_id}/delete")
async def admin_delete_version(
    package_id: str,
    version_id: str,
    repo: Repository = Depends(get_repository)
) -> JSONResponse:
    """
    Delete a specific version of a package.
    
    Args:
        package_id: The package identifier.
        version_id: The version identifier (URL-encoded).
        repo: Repository dependency.
        
    Returns:
        JSON response with success status or error message.
    """
    version_id = urllib.parse.unquote(version_id)
    v = _get_version_by_id(repo, package_id, version_id)
    if not v:
        return JSONResponse(status_code=404, content={"error": "Version not found"})
    
    repo.db.delete_installer(package_id, v)
    
    return JSONResponse(status_code=200, content={"success": True, "message": "Version deleted successfully"})


@router.post("/admin/packages/{package_id}/delete")
async def admin_delete_package(
    package_id: str,
    repo: Repository = Depends(get_repository)
) -> JSONResponse:
    """
    Delete an entire package and all its versions.
    
    Args:
        package_id: The package identifier.
        repo: Repository dependency.
        
    Returns:
        JSON response with success status.
    """
    repo.db.delete_package(package_id)
    return JSONResponse(status_code=200, content={"success": True, "message": "Package deleted successfully"})


# ---------------------------------------------------------------------------
# WinGet Import Endpoints
# ---------------------------------------------------------------------------


@router.post("/admin/cached-packages/update")
async def admin_update_cached_packages(
    caching_service: CachingService = Depends(get_caching_service)
) -> JSONResponse:
    """
    Update all cached packages that have auto_update enabled.
    
    Updates the upstream repository index and checks for new versions of all
    cached packages, importing them if they match the package's filter settings.
    
    Args:
        caching_service: Caching service dependency.
        
    Returns:
        JSON response with success status or error message.
    """
    try:
        await caching_service.update_cached_packages()
        return JSONResponse(
            status_code=200,
            content={"success": True, "message": "Cached packages updated successfully"}
        )
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"error": f"Failed to update cached packages: {str(e)}"}
        )


@router.get("/admin/winget/search")
async def admin_winget_search(
    q: str = Query(..., description="Search query for package ID or name"),
    caching_service: CachingService = Depends(get_caching_service)
) -> JSONResponse:
    """
    Search for packages in the upstream WinGet repository.
    
    Args:
        q: Search query string (package ID or name).
        caching_service: Caching service dependency.
        
    Returns:
        JSON response with matching packages or error message.
    """
    try:
        results = caching_service.search_upstream_packages(q)
        return JSONResponse(status_code=200, content={"success": True, "packages": results})
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": f"Search failed: {str(e)}"})


@router.get("/admin/winget/packages/{package_id}/versions")
async def admin_winget_package_versions(
    package_id: str,
    architecture: Optional[str] = Query(None),
    scope: Optional[str] = Query(None),
    caching_service: CachingService = Depends(get_caching_service)
) -> JSONResponse:
    """
    Get available versions for a package from the upstream WinGet repository.
    
    Args:
        package_id: The package identifier to look up.
        architecture: Optional architecture filter (e.g., "x64", "x86").
        scope: Optional scope filter ("user" or "machine").
        caching_service: Caching service dependency.
        
    Returns:
        JSON response with available versions or error message.
    """
    try:
        versions = await caching_service.get_upstream_package_versions(package_id, architecture, scope)
        return JSONResponse(status_code=200, content={"success": True, "versions": versions})
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": f"Failed to get versions: {str(e)}"})


@router.post("/admin/winget/packages/{package_id}/import")
async def admin_winget_import_package(
    package_id: str,
    architectures: Optional[str] = Query(None),
    scopes: Optional[str] = Query(None),
    installer_types: Optional[str] = Query(None),
    version_mode: str = Query("latest"),
    version_filter: Optional[str] = Query(None),
    caching_service: CachingService = Depends(get_caching_service)
) -> JSONResponse:
    """
    Import a package from the upstream WinGet repository.
    
    Downloads and imports package versions matching the specified filters.
    This creates a regular (non-cached) package in the repository.
    
    Args:
        package_id: The package identifier to import.
        architectures: Comma-separated list of architectures to filter (e.g., "x64,x86").
        scopes: Comma-separated list of scopes to filter (e.g., "user,machine").
        installer_types: Comma-separated list of installer types to filter (e.g., "exe,msi").
        version_mode: Version import mode ("latest" or "all").
        version_filter: Optional version filter string.
        caching_service: Caching service dependency.
        
    Returns:
        JSON response with import result or error message.
    """
    arch_list = [a.strip() for a in architectures.split(",")] if architectures else None
    scope_list = [s.strip() for s in scopes.split(",")] if scopes else None
    type_list = [t.strip() for t in installer_types.split(",")] if installer_types else None
    
    try:
        result = await caching_service.import_package(
            package_id,
            architectures=arch_list,
            scopes=scope_list,
            installer_types=type_list,
            version_mode=version_mode,
            version_filter=version_filter,
        )
        return JSONResponse(status_code=200, content={"success": True, "result": result})
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": f"Import failed: {str(e)}"})


# ---------------------------------------------------------------------------
# Cached Packages
# ---------------------------------------------------------------------------


@router.get("/admin/cached-packages/new/fragment", response_class=HTMLResponse)
async def admin_new_cached_package_fragment(
    request: Request,
    repo: Repository = Depends(get_repository)
) -> HTMLResponse:
    """
    Return the HTML fragment for creating a new cached package.
    
    Cached packages are automatically synced from the upstream WinGet repository
    based on configured filters.
    
    Args:
        request: FastAPI request object.
        repo: Repository dependency.
        
    Returns:
        HTML fragment template for new cached package form.
    """
    config = repo.db.get_repository_config()
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
    repo: Repository = Depends(get_repository),
    caching_service: CachingService = Depends(get_caching_service),
) -> JSONResponse:
    """
    Create a new cached package from the upstream WinGet repository.
    
    Cached packages automatically sync versions from the upstream repository
    based on the configured filters. The package is marked as cached and will
    be updated when the index is refreshed.
    
    Args:
        package_id: The package identifier to cache from upstream.
        architectures: Comma-separated list of architectures to filter.
        scopes: Comma-separated list of scopes to filter.
        installer_types: Comma-separated list of installer types to filter.
        version_mode: Version import mode ("latest" or "all").
        version_filter: Optional version filter string.
        ad_group_scopes_group: List of AD group names for scope restrictions.
        ad_group_scopes_scope: List of scope values corresponding to groups.
        repo: Repository dependency.
        caching_service: Caching service dependency.
        
    Returns:
        JSON response with import result or error message.
    """
    arch_list = [a.strip() for a in architectures.split(",") if a.strip()] if architectures and architectures.strip() else None
    scope_list = [s.strip() for s in scopes.split(",") if s.strip()] if scopes and scopes.strip() else None
    type_list = None
    if installer_types and installer_types.strip():
        parsed_types = [t.strip() for t in installer_types.split(",") if t.strip()]
        config = repo.db.get_repository_config()
        if set(parsed_types) != set(config.installer_type_options):
            type_list = parsed_types
            
    try:
        ad_group_scopes_entries = _parse_ad_group_scopes(ad_group_scopes_group, ad_group_scopes_scope, repo)
        result = await caching_service.import_package(
            package_id,
            architectures=arch_list,
            scopes=scope_list,
            installer_types=type_list,
            version_mode=version_mode,
            version_filter=version_filter,
            ad_group_scopes=ad_group_scopes_entries,
        )
        return JSONResponse(status_code=200, content={"success": True, "result": result})
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": f"Import failed: {str(e)}"})


@router.get("/admin/cached-packages/{package_id}", response_class=HTMLResponse)
async def admin_cached_package_detail(
    request: Request,
    package_id: str,
    repo: Repository = Depends(get_repository)
) -> HTMLResponse:
    """
    Display the cached package detail page showing all cached versions.
    
    Args:
        request: FastAPI request object.
        package_id: The package identifier.
        repo: Repository dependency.
        
    Returns:
        HTML template response with cached package details.
        
    Raises:
        HTTPException: 404 if cached package not found.
    """
    pkg = repo.get_package(package_id)
    if not pkg or not pkg.metadata.cached:
        raise HTTPException(status_code=404, detail="Cached package not found")
    
    config = repo.db.get_repository_config()
    cached_versions = sorted(pkg.versions, key=lambda v: v.version, reverse=True)
    
    return templates.TemplateResponse(
        "admin_cached_package_detail.html",
        {
            "request": request,
            "title": f"Cached Package: {pkg.metadata.package_name}",
            "package": pkg.metadata,
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
    repo: Repository = Depends(get_repository)
) -> HTMLResponse:
    """
    Return the HTML fragment for editing a cached package's settings.
    
    Args:
        request: FastAPI request object.
        package_id: The package identifier.
        repo: Repository dependency.
        
    Returns:
        HTML fragment template for cached package edit form.
        
    Raises:
        HTTPException: 404 if cached package not found.
    """
    pkg = repo.get_package(package_id)
    if not pkg or not pkg.metadata.cached:
        raise HTTPException(status_code=404, detail="Cached package not found")
    config = repo.db.get_repository_config()
    
    return templates.TemplateResponse(
        "admin_cached_package_form_fragment.html",
        {
            "request": request,
            "package": pkg.metadata,
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
    repo: Repository = Depends(get_repository),
    caching_service: CachingService = Depends(get_caching_service),
) -> JSONResponse:
    """
    Update a cached package's filter settings and re-import versions.
    
    Updates the cache settings and triggers a re-import of versions from
    the upstream repository based on the new filters.
    
    Args:
        package_id: The package identifier from the URL path.
        package_id_from_form: The package identifier from the form (must match).
        architectures: Comma-separated list of architectures to filter.
        scopes: Comma-separated list of scopes to filter.
        installer_types: Comma-separated list of installer types to filter.
        version_mode: Version import mode ("latest" or "all").
        version_filter: Optional version filter string.
        ad_group_scopes_group: List of AD group names for scope restrictions.
        ad_group_scopes_scope: List of scope values corresponding to groups.
        repo: Repository dependency.
        caching_service: Caching service dependency.
        
    Returns:
        JSON response with success status or error message.
    """
    if package_id_from_form != package_id:
        return JSONResponse(status_code=400, content={"error": "Package identifier mismatch"})

    pkg = repo.get_package(package_id)
    if not pkg or not pkg.metadata.cached:
        return JSONResponse(status_code=404, content={"error": "Cached package not found"})

    # Normalize filters
    arch_list = [a.strip() for a in architectures.split(",") if a.strip()] if architectures and architectures.strip() else []
    scope_list = [s.strip() for s in scopes.split(",") if s.strip()] if scopes and scopes.strip() else []
    type_list = []
    if installer_types and installer_types.strip():
        parsed_types = [t.strip() for t in installer_types.split(",") if t.strip()]
        config = repo.db.get_repository_config()
        if set(parsed_types) != set(config.installer_type_options):
            type_list = parsed_types

    ad_group_scopes_entries = _parse_ad_group_scopes(ad_group_scopes_group, ad_group_scopes_scope, repo)

    new_cache_settings = CacheSettings(
        architectures=arch_list,
        scopes=scope_list,
        installer_types=type_list,
        version_mode=version_mode,
        version_filter=(version_filter or "").strip() or None,
        auto_update=pkg.metadata.cache_settings.auto_update if pkg.metadata.cache_settings else True,
    )

    current = pkg.metadata
    updated_pkg = PackageCommonMetadata(
        package_identifier=current.package_identifier,
        package_name=current.package_name,
        publisher=current.publisher,
        short_description=current.short_description,
        license=current.license,
        tags=current.tags,
        homepage=current.homepage,
        support_url=current.support_url,
        ad_group_scopes=ad_group_scopes_entries or getattr(current, "ad_group_scopes", []) or [],
        is_example=current.is_example,
        cached=True,
        cache_settings=new_cache_settings,
    )
    
    # Save settings first
    repo.db.save_package(updated_pkg)
    
    try:
        await caching_service.import_package(
            package_id,
            architectures=arch_list if arch_list else None,
            scopes=scope_list if scope_list else None,
            installer_types=type_list if type_list else None,
            version_mode=version_mode,
            version_filter=new_cache_settings.version_filter,
            ad_group_scopes=ad_group_scopes_entries or getattr(current, "ad_group_scopes", []) or [],
        )
        return JSONResponse(status_code=200, content={"success": True, "message": "Cached package updated successfully"})
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": f"Update failed: {str(e)}"})


@router.post("/admin/cached-packages/{package_id}/delete")
async def admin_cached_package_delete(
    package_id: str,
    repo: Repository = Depends(get_repository)
) -> JSONResponse:
    """
    Remove a cached package from the repository.
    
    Deletes the package and all its cached versions. This does not affect
    the upstream repository.
    
    Args:
        package_id: The package identifier.
        repo: Repository dependency.
        
    Returns:
        JSON response with success status or error message.
    """
    pkg = repo.get_package(package_id)
    if not pkg or not pkg.metadata.cached:
        return JSONResponse(status_code=404, content={"error": "Cached package not found"})
    
    repo.db.delete_package(package_id)
    return JSONResponse(status_code=200, content={"success": True, "message": "Package removed from cache"})


@router.post("/admin/cached-packages/{package_id}/versions/{version_id}/delete")
async def admin_delete_cached_version(
    package_id: str,
    version_id: str,
    repo: Repository = Depends(get_repository)
) -> JSONResponse:
    """
    Delete a specific version from a cached package.
    
    Removes a single cached version. The version may be re-imported on the
    next cache update if it still matches the package's filter settings.
    
    Args:
        package_id: The package identifier.
        version_id: The version identifier (URL-encoded).
        repo: Repository dependency.
        
    Returns:
        JSON response with success status or error message.
    """
    version_id = urllib.parse.unquote(version_id)
    pkg = repo.get_package(package_id)
    if not pkg or not pkg.metadata.cached:
        return JSONResponse(status_code=404, content={"error": "Cached package not found"})

    v = _get_version_by_id(repo, package_id, version_id)
    if not v:
        return JSONResponse(status_code=404, content={"error": "Version not found"})

    repo.db.delete_installer(package_id, v)
    return JSONResponse(status_code=200, content={"success": True, "message": "Cached version deleted successfully"})
