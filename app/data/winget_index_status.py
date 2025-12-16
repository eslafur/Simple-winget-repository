"""
Track WinGet index download status and metadata.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Optional
from pydantic import BaseModel, Field

from app.data.repository import get_data_dir


class WinGetIndexStatus(BaseModel):
    """Status information for the WinGet index."""
    last_pulled: Optional[datetime] = Field(default=None, description="When the index was last downloaded")
    index_path: Optional[str] = Field(default=None, description="Path to the index database file")
    index_version: Optional[str] = Field(default=None, description="Version of the index if available")


class WinGetIndexStatusStore:
    """Manages storage of WinGet index status."""
    
    def __init__(self, data_dir: Optional[Path] = None):
        self.data_dir = data_dir or get_data_dir()
        self.status_file = self.data_dir / "cache" / "winget_index_status.json"
        self._status: Optional[WinGetIndexStatus] = None
        self._load()
    
    def _load(self):
        """Load index status from disk."""
        if self.status_file.exists():
            try:
                data = json.loads(self.status_file.read_text(encoding="utf-8"))
                self._status = WinGetIndexStatus(**data)
            except Exception:
                self._status = WinGetIndexStatus()
        else:
            self._status = WinGetIndexStatus()
            self._save()
    
    def _save(self):
        """Save index status to disk."""
        self.status_file.parent.mkdir(parents=True, exist_ok=True)
        self.status_file.write_text(
            self._status.model_dump_json(indent=2, exclude_none=True),
            encoding="utf-8"
        )
    
    def get_status(self) -> WinGetIndexStatus:
        """Get the current index status."""
        if self._status is None:
            self._load()
        return self._status
    
    def update_pulled_time(self, index_path: Optional[Path] = None, index_version: Optional[str] = None):
        """Update the last pulled timestamp."""
        if self._status is None:
            self._status = WinGetIndexStatus()
        
        self._status.last_pulled = datetime.utcnow()
        if index_path:
            self._status.index_path = str(index_path)
        if index_version:
            self._status.index_version = index_version
        
        self._save()


# Global instance
_index_status_store: Optional[WinGetIndexStatusStore] = None


def get_index_status_store() -> WinGetIndexStatusStore:
    """Get the global index status store."""
    global _index_status_store
    if _index_status_store is None:
        _index_status_store = WinGetIndexStatusStore()
    return _index_status_store

