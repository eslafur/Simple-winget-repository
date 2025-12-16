from __future__ import annotations

from typing import List, Set, Tuple

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.data.repository import get_repository_index


router = APIRouter()


class AutoInstallRequest(BaseModel):
    groups: List[str] = Field(default_factory=list, description="AD group names the client is a member of.")


class AutoInstallResult(BaseModel):
    app_id: str
    scope: str


@router.post("/auto-install")
async def auto_install(request: AutoInstallRequest) -> dict:
    """
    Client endpoint: given AD group membership list, return matching package install targets.

    Result is a de-duplicated list of (app_id, scope) pairs where any configured
    package rule has an ad_group that matches one of the provided group names.
    """
    group_set: Set[str] = {
        g.strip().casefold()
        for g in (request.groups or [])
        if isinstance(g, str) and g.strip()
    }

    matches: Set[Tuple[str, str]] = set()
    index = get_repository_index()
    for package_id, pkg_index in index.packages.items():
        rules = getattr(pkg_index.package, "ad_group_scopes", None) or []
        for rule in rules:
            group_name = (getattr(rule, "ad_group", "") or "").strip().casefold()
            scope = (getattr(rule, "scope", "") or "").strip()
            if group_name and group_name in group_set and scope:
                matches.add((package_id, scope))

    results = [
        {"app_id": app_id, "scope": scope}
        for app_id, scope in sorted(matches, key=lambda x: (x[0], x[1]))
    ]
    return {"results": results}


