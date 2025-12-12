from __future__ import annotations

import hashlib
import json
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from app.data.models import AuthenticationStore, AuthUser, AuthCredential, AuthSession
from app.data.repository import get_data_dir


SESSION_COOKIE_NAME = "winget_admin_session"


_auth_store: Optional[AuthenticationStore] = None


def _auth_path() -> Path:
    """
    Location of authentication.json alongside repository.json.
    """
    return get_data_dir() / "authentication.json"


def _hash_password_sha256(password: str, salt: str) -> str:
    """
    Compute SHA256 hash for the given password+salt combination.
    """
    data = (salt + password).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def _normalize_store(store: AuthenticationStore) -> AuthenticationStore:
    """
    Normalize an AuthenticationStore in-place:

    * Convert any cleartext credentials to salted SHA256 credentials.
    * Ensure that for each user only the last 'sha256' credential is kept.
    """
    for user in store.users:
        normalized_auths: list[AuthCredential] = []

        # First pass: convert cleartext entries to sha256.
        for cred in user.authentications:
            if cred.type == "cleartext":
                # Generate per-user salt and replace password with hash.
                salt = secrets.token_hex(16)
                hashed = _hash_password_sha256(cred.password, salt)
                normalized_auths.append(
                    AuthCredential(type="sha256", password=hashed, salt=salt)
                )
            else:
                normalized_auths.append(cred)

        # Second pass: keep only the last sha256 entry per user.
        sha_indices = [i for i, c in enumerate(normalized_auths) if c.type == "sha256"]
        if len(sha_indices) > 1:
            last_index = sha_indices[-1]
            normalized_auths = [
                c
                for i, c in enumerate(normalized_auths)
                if c.type != "sha256" or i == last_index
            ]

        user.authentications = normalized_auths

    return store


def _load_store_from_disk() -> AuthenticationStore:
    """
    Load authentication.json from disk, applying normalization rules and
    persisting any changes back to disk.
    """
    path = _auth_path()
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            store = AuthenticationStore(**raw)
        except Exception:
            store = AuthenticationStore()
    else:
        store = AuthenticationStore()

    store = _normalize_store(store)
    path.write_text(store.model_dump_json(by_alias=True, indent=2), encoding="utf-8")
    return store


def _save_store_to_disk(store: AuthenticationStore) -> None:
    """
    Persist the given AuthenticationStore to authentication.json.
    """
    path = _auth_path()
    path.write_text(store.model_dump_json(by_alias=True, indent=2), encoding="utf-8")


def get_auth_store(refresh: bool = False) -> AuthenticationStore:
    """
    Return the in-memory AuthenticationStore, loading from disk on first use
    or when refresh=True.
    """
    global _auth_store
    if _auth_store is None or refresh:
        _auth_store = _load_store_from_disk()
    return _auth_store


def initialize_authentication() -> None:
    """
    Ensure authentication.json exists and is normalized on startup.
    """
    get_auth_store(refresh=True)


def _find_user(username: str) -> Optional[AuthUser]:
    store = get_auth_store()
    for user in store.users:
        if user.username == username:
            return user
    return None


def has_any_user() -> bool:
    """
    Return True if at least one user account exists.
    """
    store = get_auth_store()
    return len(store.users) > 0


def create_user(username: str, password: str) -> AuthUser:
    """
    Create a new user with a salted SHA256 password.
    """
    global _auth_store
    store = get_auth_store()

    if _find_user(username) is not None:
        raise ValueError("User already exists")

    salt = secrets.token_hex(16)
    hashed = _hash_password_sha256(password, salt)
    cred = AuthCredential(type="sha256", password=hashed, salt=salt)
    user = AuthUser(username=username, authentications=[cred])
    store.users.append(user)
    _normalize_store(store)
    _save_store_to_disk(store)
    _auth_store = store
    return user


def verify_user_password(username: str, password: str) -> bool:
    """
    Verify the supplied password for the given user.
    """
    user = _find_user(username)
    if user is None:
        return False

    sha_creds = [c for c in user.authentications if c.type == "sha256"]
    if not sha_creds:
        return False
    cred = sha_creds[-1]
    if not cred.salt:
        return False

    expected = cred.password
    actual = _hash_password_sha256(password, cred.salt)
    return secrets.compare_digest(expected, actual)


def create_session(username: str) -> AuthSession:
    """
    Create a new session for the given user and persist it.
    """
    global _auth_store
    store = get_auth_store()

    session_id = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    session = AuthSession(session_id=session_id, last_login=now, username=username)
    store.sessions.append(session)
    _save_store_to_disk(store)
    _auth_store = store
    return session


def get_user_for_session(session_id: str) -> Optional[AuthUser]:
    """
    Resolve a session ID to an AuthUser, updating last_login when found.
    """
    global _auth_store
    if not session_id:
        return None

    store = get_auth_store()
    target_session: Optional[AuthSession] = None
    for s in store.sessions:
        if s.session_id == session_id:
            target_session = s
            break

    if not target_session:
        return None

    user = _find_user(target_session.username)
    if not user:
        return None

    # Update last_login timestamp for this session.
    target_session.last_login = datetime.now(timezone.utc)
    _save_store_to_disk(store)
    _auth_store = store
    return user


def clear_session(session_id: str) -> None:
    """
    Remove a session from the store.
    """
    global _auth_store
    if not session_id:
        return

    store = get_auth_store()
    store.sessions = [s for s in store.sessions if s.session_id != session_id]
    _save_store_to_disk(store)
    _auth_store = store


