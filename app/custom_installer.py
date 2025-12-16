from __future__ import annotations

from pathlib import Path
from typing import Iterable, List, Dict, Any

from app.domain.models import CustomInstallerStep, VersionMetadata


def get_available_actions() -> List[Dict[str, Any]]:
    """
    Return the list of available custom installer actions for the admin UI.

    For now we expose:
    - run_installer: run the uploaded installer with optional extra arguments.
    - write_version_to_registry: write DisplayVersion into the appropriate
      Uninstall registry key so WinGet can detect the installed version even
      if the installer does not set it correctly.
    """
    return [
        {
            "id": "run_installer",
            "label": "Run installer",
        },
        {
            "id": "write_version_to_registry",
            "label": "Write version to registry",
        },
    ]


def render_install_script(
    meta: VersionMetadata,
) -> str:
    """
    Render a Windows batch script that executes the configured steps.

    We currently support:
    - run_installer: cds into the directory where the batch file lives and
      starts the uploaded installer with the extra arguments from argument1.
    - write_version_to_registry: writes DisplayVersion into the appropriate
      Uninstall registry key (per scope + architecture).
    """
    installer_filename = meta.installer_file or "installer.exe"
    version = meta.version
    architecture = meta.architecture
    scope = meta.scope
    steps = list[CustomInstallerStep](meta.custom_installer_steps or [])
    lines: List[str] = [
        "@echo off",
        "setlocal",
        'cd /d "%~dp0"',
        "",
    ]

    added_registry_snippet = False

    for step in steps:
        if step.action_type == "run_installer":
            extra_args = (step.argument1 or "").strip()
            if extra_args:
                # Use CALL so that the batch script waits for the installer
                # to complete before continuing with subsequent steps.
                lines.append(f'call "{installer_filename}" {extra_args}')
            else:
                lines.append(f'call "{installer_filename}"')
        elif step.action_type == "write_version_to_registry" and not added_registry_snippet:
            added_registry_snippet = True
            # Compute the final ARP key and reg.exe invocation in Python so the
            # batch script only needs a single reg add command. We use the
            # scope (user/machine), architecture (32/64-bit view) and product
            # code from the metadata.
            root = "HKCU" if scope == "user" else "HKLM"
            if architecture == "x86":
                reg_view = "/reg:32"
            else:
                # x64 and arm64 both use the 64-bit view.
                reg_view = "/reg:64"

            product_code = meta.product_code or ""
            uninstall_key = (
                rf'{root}\Software\Microsoft\Windows\CurrentVersion\Uninstall\{product_code}'
            )
            reg_command = (
                f'reg add "{uninstall_key}" /v DisplayVersion /t REG_SZ '
                f'/d "{version}" /f {reg_view}'
            )
            lines.append(
                "rem Ensure DisplayVersion is set in ARP so WinGet can detect the install"
            )
            lines.append(reg_command)

    # If no steps were configured, default to a simple "run installer" step
    if len(lines) <= 4:
        lines.append(f'start "" "{installer_filename}"')

    lines.append("")
    lines.append("endlocal")
    lines.append("")
    # Use CRLF line endings for Windows batch files.
    return "\r\n".join(lines)



