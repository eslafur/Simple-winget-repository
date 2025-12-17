from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Optional, List
import hashlib
import os
import urllib.parse
import zipfile
import tempfile
import json
import shutil

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
from app.core.dependencies import get_repository, get_db_manager, get_data_dir, get_caching_service
from app.services.caching import CachingService
from app import custom_installer


templates = Jinja2Templates(directory="app/templates")


async def require_admin_session(request: Request) -> AuthUser:
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
    pkg = repo.get_package(package_id)
    if not pkg:
        raise HTTPException(status_code=404, detail="Package not found")
    return pkg


def _get_version_by_id(repo: Repository, package_id: str, version_id: str) -> Optional[VersionMetadata]:
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
    custom_arg1: List[str] = Form(default=[]),
    custom_arg2: List[str] = Form(default=[]),
    install_mode_interactive: bool = Form(True),
    install_mode_silent: bool = Form(True),
    install_mode_silent_with_progress: bool = Form(True),
    requires_elevation: bool = Form(False),
    package_dependencies: List[str] = Form(default=[]),
    upload: UploadFile | None = File(default=None),
    repo: Repository = Depends(get_repository)
) -> JSONResponse:
    _get_package_or_404(repo, package_id)
    version_id = urllib.parse.unquote(version_id)
    
    existing_version = None
    if version_id != "new":
        existing_version = _get_version_by_id(repo, package_id, version_id)
        
    has_new_upload = upload is not None and upload.filename and len(upload.filename.strip()) > 0
    
    if not has_new_upload and not existing_version:
        return JSONResponse(status_code=400, content={"error": "Installer file is required for new versions"})

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
            arg1 = (custom_arg1[idx] if idx < len(custom_arg1) else "").strip()
            arg2 = (custom_arg2[idx] if idx < len(custom_arg2) else "").strip()
            custom_steps.append(CustomInstallerStep(action_type=action, argument1=arg1 or None, argument2=arg2 or None))

    normalized_dependencies = [d.strip() for d in package_dependencies if d.strip()]

    meta = VersionMetadata(
        version=version,
        architecture=architecture,
        scope=scope,
        product_code=product_code or None,
        installer_type=installer_type,
        # installer_file set later
        installer_sha256=None, # set later
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
    
    if existing_version:
        meta.installer_guid = existing_version.installer_guid
    
    with tempfile.TemporaryDirectory() as tmpdirname:
        work_dir = Path(tmpdirname)
        
        file_to_add: Optional[Path] = None
        
        # Determine if we have a new upload or using existing
        if has_new_upload:
            upload_path = work_dir / upload.filename
            content = await upload.read()
            upload_path.write_bytes(content)
            meta.installer_file = upload.filename
            
            # For non-custom types, we hash the uploaded file now.
            # For custom types, we hash the package.zip later.
            if installer_type != "custom":
                h = hashlib.sha256()
                h.update(content)
                meta.installer_sha256 = h.hexdigest()
            
            file_to_add = upload_path
        else:
            # Re-use existing installer file
            meta.installer_file = existing_version.installer_file
            # If standard, keep sha. If custom, we might regenerate zip so sha changes.
            if installer_type != "custom":
                meta.installer_sha256 = existing_version.installer_sha256

            # Important: For custom installers logic to work (regenerating package.zip),
            # we need the ORIGINAL installer file to be available.
            # If we are updating metadata but NOT uploading a new file,
            # we must ensure the storage manager knows we are keeping the old file
            # or if we need to regenerate package.zip, we need to fetch the old file first.
            
            # If installer_type IS custom, we ALWAYS regenerate package.zip because 
            # custom arguments might have changed.
            if installer_type == "custom" and existing_version:
                # We need to get the path to the current stored installer file (not package.zip)
                # But our current db abstraction makes this tricky if we don't have the path.
                # db.get_file_path returns the served file.
                # For custom, we want the underlying installer_file.
                
                # If we rely on db.add_installer/update_installer, passing file_path=None 
                # implies "keep existing file".
                # BUT here we want to modify the sidecar package.zip.
                pass

        if installer_type == "custom":
            # We need the installer file (either new upload or existing) to generate package.zip
            installer_source_path = None
            
            if has_new_upload:
                installer_source_path = upload_path
            elif existing_version:
                # Need to retrieve the existing installer file from storage
                # to include it in the new package.zip
                # Since we don't have a direct "get_raw_file" API in repo/db yet that guarantees 
                # the raw file, we might need to rely on the fact that for custom installers,
                # the "installer_file" stored in version.json is the raw file name.
                # And get_file_path (before my recent change) pointed to it?
                # Actually, my recent change to get_installer_path logic in Entity fixes the SERVING.
                # The DB still stores the file at `.../installer_file`.
                
                try:
                    # We can use db.get_file_path directly here since we are in admin
                    # and we know the DB implementation details slightly or use a specific method.
                    # Wait, Entity.get_installer_path logic is:
                    # base = db.get_file_path(...) -> returns path to 'installer_file'
                    # if custom: return parent/package.zip
                    
                    # So db.get_file_path(..., version) SHOULD return the raw installer file path
                    # because that's what's stored in version.json's installer_file field.
                    
                    # We need to construct a VersionMetadata that matches the DB record 
                    # so get_file_path works.
                    # existing_version has the data.
                    
                    # NOTE: We must ensure we are getting the RAW file, not package.zip.
                    # The default db.get_file_path implementation in JsonDatabaseManager
                    # returns `_data_dir / storage_path / installer_file`.
                    # This IS the raw file.
                    
                    stored_file_path = repo.db.get_file_path(package_id, existing_version)
                    if stored_file_path.exists():
                        installer_source_path = stored_file_path
                    else:
                        # Only package.zip might exist if we messed up before?
                        # Or if we are converting FROM non-custom TO custom?
                        # If converting from Standard -> Custom, we have installer_file.
                        pass
                except Exception:
                    pass

            if installer_source_path and installer_source_path.exists():
                pkg_zip, zip_hash = _build_custom_installer_package(work_dir, meta, installer_source_path)
                meta.installer_sha256 = zip_hash
                
                # We want to save BOTH the installer file (if new) AND the package.zip.
                # DB.add/update_installer usually handles ONE file.
                # If we have a new upload, we pass it as 'file_path'.
                # But we also need to write 'package.zip'.
                
                # 'add_installer' copies 'file_path' to 'installer_file'.
                # We can manually move package.zip to the destination?
                # Or we can update DB interface?
                
                # Let's rely on DB manager for the main file, and manually place the sidecar 
                # if we can resolve the path.
                
                # But we don't know the destination path inside `add_installer`.
                
                # STRATEGY: 
                # 1. Allow add/update_installer to take the file.
                # 2. After it returns, we calculate the path again and write package.zip?
                #    But we don't know the path easily without re-querying.
                
                # BETTER: Pass both to DB manager? No, that requires API change.
                
                # Hack: 
                # If we use add_installer, it copies the file.
                # If we assume we can get the path back?
                
                # Let's do this:
                # 1. Call add_installer/update_installer with the main installer file (upload or None).
                # 2. Retrieve the stored version to get the path.
                # 3. Write package.zip to that path.
                
                pass
            else:
                 return JSONResponse(status_code=400, content={"error": "Original installer file not found for custom package generation"})

        # 1. Persist the Version (and main file if new)
        if has_new_upload:
            repo.db.add_installer(package_id, meta, file_path=file_to_add)
        else:
            repo.db.update_installer(package_id, meta)

        # 2. Handle Custom Installer sidecar (package.zip)
        if installer_type == "custom":
             # We need to write package.zip to the storage location.
             # Fetch the updated version to get storage path.
             updated_v = _get_version_by_id(repo, package_id, meta.version) # simplistic match might fail if multiple same versions?
             # Better: use the one we just saved/updated.
             
             # Re-fetch is safest to get storage_path.
             # We can't rely on _get_version_by_id finding the EXACT one if duplicates exist,
             # but we assume our filtering logic holds.
             
             # We can use the meta we passed, but add_installer might have generated a GUID 
             # if we didn't have one.
             # If we had existing_version, we reused GUID.
             
             # If it was new, we might need to find it.
             # JsonDB generates GUID if missing.
             # Let's ensure we have a GUID before calling add_installer if possible?
             # JsonDB does: if not installer.installer_guid: installer.installer_guid = str(uuid.uuid4())
             
             # If we can't easily get the path, we can't save package.zip.
             
             # Fix: Generate GUID here if missing, so we can reliably find it.
             if not meta.installer_guid:
                 import uuid
                 meta.installer_guid = str(uuid.uuid4())
                 # Call add_installer again with explicit GUID
                 if has_new_upload:
                      # We called it above... wait, I haven't called it yet in this flow.
                      pass

             # Refined Flow:
             # 1. Ensure GUID.
             if not meta.installer_guid:
                 import uuid
                 meta.installer_guid = str(uuid.uuid4())

             # 2. Save metadata + main file
             if has_new_upload:
                 repo.db.add_installer(package_id, meta, file_path=file_to_add)
             else:
                 repo.db.update_installer(package_id, meta)
                 
             # 3. Generate and save package.zip
             # Now we can ask DB for the path using the GUID
             try:
                 # Re-construct a temporary meta object just for get_file_path lookups if needed
                 # or fetch the actual stored object.
                 saved_path = repo.db.get_file_path(package_id, meta)
                 # saved_path points to the installer_file (e.g. setup.exe)
                 
                 package_zip_dest = saved_path.parent / "package.zip"
                 
                 # We have the generated package.zip in work_dir
                 # We generated it above: pkg_zip
                 if 'pkg_zip' in locals() and pkg_zip and pkg_zip.exists():
                     shutil.copy2(pkg_zip, package_zip_dest)
                     
             except Exception as e:
                 # Log error?
                 print(f"Failed to save package.zip: {e}")
                 return JSONResponse(status_code=500, content={"error": "Failed to save custom installer package"})

        # For non-custom, we are done (add_installer/update_installer handled it)
        
    return JSONResponse(status_code=200, content={"success": True, "message": "Version saved successfully"})


@router.post("/admin/packages/{package_id}/versions/{version_id}/delete")
async def admin_delete_version(
    package_id: str,
    version_id: str,
    repo: Repository = Depends(get_repository)
) -> JSONResponse:
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
    repo.db.delete_package(package_id)
    return JSONResponse(status_code=200, content={"success": True, "message": "Package deleted successfully"})


# ---------------------------------------------------------------------------
# WinGet Import Endpoints
# ---------------------------------------------------------------------------


@router.post("/admin/winget-index/update")
async def admin_update_winget_index(
    caching_service: CachingService = Depends(get_caching_service)
) -> JSONResponse:
    try:
        index_path = await caching_service.update_index()
        return JSONResponse(status_code=200, content={"success": True, "message": "Index updated successfully", "index_path": str(index_path)})
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": f"Failed to update index: {str(e)}"})


@router.get("/admin/winget/search")
async def admin_winget_search(
    q: str = Query(..., description="Search query for package ID or name"),
    caching_service: CachingService = Depends(get_caching_service)
) -> JSONResponse:
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
    version_id = urllib.parse.unquote(version_id)
    pkg = repo.get_package(package_id)
    if not pkg or not pkg.metadata.cached:
        return JSONResponse(status_code=404, content={"error": "Cached package not found"})

    v = _get_version_by_id(repo, package_id, version_id)
    if not v:
        return JSONResponse(status_code=404, content={"error": "Version not found"})

    repo.db.delete_installer(package_id, v)
    return JSONResponse(status_code=200, content={"success": True, "message": "Cached version deleted successfully"})
