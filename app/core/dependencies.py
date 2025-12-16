from pathlib import Path
from typing import Optional
import os

from app.storage.db_manager import DatabaseManager
from app.storage.json_db_manager import JsonDatabaseManager
from app.domain.entities import Repository
from app.services.caching import CachingService

DATA_ROOT_ENV_VAR = "WINGET_REPO_DATA_DIR"
_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_DATA_DIR = _REPO_ROOT / "data"

_db_manager: Optional[DatabaseManager] = None
_repository: Optional[Repository] = None
_caching_service: Optional[CachingService] = None

def get_data_dir() -> Path:
    env_path = os.environ.get(DATA_ROOT_ENV_VAR)
    if env_path:
        d = Path(env_path).expanduser()
    else:
        d = _DEFAULT_DATA_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d

def get_db_manager() -> DatabaseManager:
    global _db_manager
    if _db_manager is None:
        _db_manager = JsonDatabaseManager(get_data_dir())
        _db_manager.initialize()
    return _db_manager

def get_repository() -> Repository:
    global _repository
    if _repository is None:
        _repository = Repository(get_db_manager())
    return _repository

def get_caching_service() -> CachingService:
    global _caching_service
    if _caching_service is None:
        _caching_service = CachingService(get_db_manager())
    return _caching_service
