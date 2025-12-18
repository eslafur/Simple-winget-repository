from __future__ import annotations

from pathlib import Path
from typing import Iterable, List, Dict, Any

from app.domain.models import CustomInstallerStep, VersionMetadata


def get_available_actions() -> List[Dict[str, Any]]:
    """
    Return the list of available custom installer actions for the admin UI.

    Each action includes:
    - id: unique identifier for the action
    - label: display name for the action
    - arguments: list of argument definitions, each with:
      - name: argument identifier (e.g., "arg1", "arg2")
      - label: display label for the argument field
      - placeholder: placeholder text to show in the input field
      - description: help text explaining what the argument does
      - required: whether the argument is required
      - visible: whether the argument field should be shown (defaults to True)
    """
    return [
        {
            "id": "run_installer",
            "label": "Run installer",
            "arguments": [
                {
                    "name": "arg1",
                    "label": "Extra arguments",
                    "placeholder": "e.g., /S /D=C:\\Program Files\\App",
                    "description": "Optional command-line arguments to pass to the installer",
                    "required": False,
                    "visible": True,
                },
            ],
        },
        {
            "id": "write_version_to_registry",
            "label": "Write version to registry",
            "arguments": [],
        },
        {
            "id": "register_dlls_in_folder",
            "label": "Register all DLLs in folder",
            "arguments": [
                {
                    "name": "arg1",
                    "label": "Folder path",
                    "placeholder": "e.g., C:\\Program Files\\App\\bin",
                    "description": "Path to the folder containing DLL files to register",
                    "required": True,
                    "visible": True,
                },
            ],
        },
        {
            "id": "register_ocx_in_folder",
            "label": "Register all OCX files in folder",
            "arguments": [
                {
                    "name": "arg1",
                    "label": "Folder path",
                    "placeholder": "e.g., C:\\Program Files\\App\\bin",
                    "description": "Path to the folder containing OCX files to register",
                    "required": True,
                    "visible": True,
                },
            ],
        },
        {
            "id": "connect_network_drive",
            "label": "Connect network drive",
            "arguments": [
                {
                    "name": "arg1",
                    "label": "Network path",
                    "placeholder": "e.g., \\\\server\\share",
                    "description": "UNC path to the network share",
                    "required": True,
                    "visible": True,
                },
                {
                    "name": "arg2",
                    "label": "Drive letter",
                    "placeholder": "e.g., Z or Z:",
                    "description": "Drive letter to assign to the network share",
                    "required": True,
                    "visible": True,
                },
            ],
        },
    ]


def render_install_script(
    meta: VersionMetadata,
) -> str:
    """
    Render a Windows batch script that executes the configured steps.

    We currently support:
    - run_installer: cds into the directory where the batch file lives and
      starts the uploaded installer with the extra arguments from the 'arg1' argument.
    - write_version_to_registry: writes DisplayVersion into the appropriate
      Uninstall registry key (per scope + architecture).
    - register_dlls_in_folder: registers all DLL files in the folder specified
      in the 'arg1' argument using regsvr32.
    - register_ocx_in_folder: registers all OCX files in the folder specified
      in the 'arg1' argument using regsvr32.
    - connect_network_drive: maps a network drive using net use, where 'arg1'
      is the network path and 'arg2' is the drive letter.
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

    def get_arg(step: CustomInstallerStep, arg_name: str, default: str = "") -> str:
        """Get argument value from step's arguments dictionary."""
        if step.arguments and arg_name in step.arguments:
            value = step.arguments.get(arg_name)
            if value:
                return str(value).strip()
        return default

    for step in steps:
        if step.action_type == "run_installer":
            extra_args = get_arg(step, "arg1")
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
        elif step.action_type == "register_dlls_in_folder":
            folder_path = get_arg(step, "arg1")
            if folder_path:
                lines.append(f'rem Register all DLL files in "{folder_path}"')
                lines.append(f'for %%f in ("{folder_path}\\*.dll") do (')
                lines.append('    regsvr32 /s "%%f"')
                lines.append('    if errorlevel 1 (')
                lines.append('        echo Failed to register %%f')
                lines.append('    )')
                lines.append(')')
        elif step.action_type == "register_ocx_in_folder":
            folder_path = get_arg(step, "arg1")
            if folder_path:
                lines.append(f'rem Register all OCX files in "{folder_path}"')
                lines.append(f'for %%f in ("{folder_path}\\*.ocx") do (')
                lines.append('    regsvr32 /s "%%f"')
                lines.append('    if errorlevel 1 (')
                lines.append('        echo Failed to register %%f')
                lines.append('    )')
                lines.append(')')
        elif step.action_type == "connect_network_drive":
            network_path = get_arg(step, "arg1")
            drive_letter = get_arg(step, "arg2").upper()
            if network_path and drive_letter:
                # Ensure drive letter format is correct (e.g., "Z:" or "Z")
                if not drive_letter.endswith(":"):
                    drive_letter = f"{drive_letter}:"
                lines.append(f'rem Connect network drive {drive_letter} to "{network_path}"')
                lines.append(f'net use {drive_letter} "{network_path}" /persistent:no')
                lines.append('if errorlevel 1 (')
                lines.append(f'    echo Failed to connect network drive {drive_letter}')
                lines.append('    exit /b 1')
                lines.append(')')

    # If no steps were configured, default to a simple "run installer" step
    if len(lines) <= 4:
        lines.append(f'start "" "{installer_filename}"')

    lines.append("")
    lines.append("endlocal")
    lines.append("")
    # Use CRLF line endings for Windows batch files.
    return "\r\n".join(lines)



