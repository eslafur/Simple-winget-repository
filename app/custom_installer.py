from __future__ import annotations

from pathlib import Path
from typing import Iterable, List, Dict, Any

from app.data.models import CustomInstallerStep


def get_available_actions() -> List[Dict[str, Any]]:
    """
    Return the list of available custom installer actions for the admin UI.

    For now we expose a single action:
    - run_installer: run the uploaded installer with optional extra arguments.
    """
    return [
        {
            "id": "run_installer",
            "label": "Run installer",
        }
    ]


def render_install_script(
    steps: Iterable[CustomInstallerStep],
    installer_filename: str,
) -> str:
    """
    Render a Windows batch script that executes the configured steps.

    For now we support only the 'run_installer' action, which:
    - cds into the directory where the batch file lives
    - starts the uploaded installer with the extra arguments from argument1
    """
    lines: List[str] = [
        "@echo off",
        "setlocal",
        'cd /d "%~dp0"',
        "",
    ]

    for step in steps:
        if step.action_type == "run_installer":
            extra_args = (step.argument1 or "").strip()
            if extra_args:
                lines.append(f'start "" "{installer_filename}" {extra_args}')
            else:
                lines.append(f'start "" "{installer_filename}"')

    # If no steps were configured, default to a simple "run installer" step
    if len(lines) <= 4:
        lines.append(f'start "" "{installer_filename}"')

    lines.append("")
    lines.append("endlocal")
    lines.append("")
    # Use CRLF line endings for Windows batch files.
    return "\r\n".join(lines)



