"""
Client API endpoints for automatic package installation.

This module provides endpoints that clients can use to query which packages
should be automatically installed based on their Active Directory group membership.
The primary use case is enterprise deployment where packages are assigned to
AD groups and clients report their group membership to determine what to install.
"""

from __future__ import annotations

from typing import List, Set, Tuple

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.core.dependencies import get_repository
from app.domain.entities import Repository


router = APIRouter()


class AutoInstallRequest(BaseModel):
    """
    Request model for the auto-install endpoint.
    
    Clients send their Active Directory group membership list, and the server
    returns matching package installation targets based on configured AD group rules.
    """
    
    groups: List[str] = Field(
        default_factory=list,
        description="List of Active Directory group names that the client is a member of.",
    )


class AutoInstallResult(BaseModel):
    """
    Result model for a single package installation target.
    
    Represents a package that should be installed on the client with the specified
    installation scope based on AD group membership matching.
    """
    
    app_id: str = Field(description="Package identifier (e.g., 'Publisher.PackageName').")
    scope: str = Field(description="Installation scope: 'user' or 'machine'.")


@router.post("/auto-install")
async def auto_install(
    request: AutoInstallRequest,
    repo: Repository = Depends(get_repository),
) -> dict:
    """
    Determine which packages should be automatically installed based on AD group membership.
    
    This endpoint matches the client's reported AD group membership against configured
    package deployment rules. Each package can have one or more AD group targeting rules
    that specify which AD group should trigger installation and what scope to use.
    
    The matching is case-insensitive and handles whitespace normalization. Results are
    de-duplicated and sorted by package ID and scope for consistent output.
    
    Args:
        request: The auto-install request containing the client's AD group membership list.
        repo: The repository instance (injected via dependency).
    
    Returns:
        A dictionary with a "results" key containing a list of AutoInstallResult objects,
        each representing a package that should be installed with its target scope.
        
        Example response:
        {
            "results": [
                {"app_id": "Publisher.PackageName", "scope": "machine"},
                {"app_id": "Another.Package", "scope": "user"}
            ]
        }
    """
    # Normalize and validate the provided AD group names
    # - Strip whitespace and convert to lowercase for case-insensitive matching
    # - Filter out empty strings and non-string values
    group_set: Set[str] = {
        g.strip().casefold()
        for g in (request.groups or [])
        if isinstance(g, str) and g.strip()
    }
    
    # Track unique (package_id, scope) pairs that match the client's group membership
    matches: Set[Tuple[str, str]] = set()
    
    # Iterate over all packages in the repository
    packages = repo.get_all_packages()
    
    for pkg in packages:
        # Get the AD group targeting rules for this package
        # Each rule specifies an AD group name and the installation scope to use
        rules = getattr(pkg.metadata, "ad_group_scopes", None) or []
        
        for rule in rules:
            # Extract and normalize the AD group name from the rule
            group_name = (getattr(rule, "ad_group", "") or "").strip().casefold()
            # Extract the installation scope (must be 'user' or 'machine')
            scope = (getattr(rule, "scope", "") or "").strip()
            
            # If the rule's AD group matches one of the client's groups and scope is valid,
            # add this package-scope combination to the results
            if group_name and group_name in group_set and scope:
                matches.add((pkg.package_id, scope))
    
    # Convert matches to the response format, sorted by package ID then scope
    # This ensures consistent, deterministic output ordering
    results = [
        {"app_id": app_id, "scope": scope}
        for app_id, scope in sorted(matches, key=lambda x: (x[0], x[1]))
    ]
    
    return {"results": results}
