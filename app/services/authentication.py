from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timezone
from typing import Optional

from app.domain.models import AuthenticationStore, AuthUser, AuthCredential, AuthSession
from app.core.dependencies import get_db_manager

SESSION_COOKIE_NAME = "winget_admin_session"

def _hash_password_sha256(password: str, salt: str) -> str:
    data = (salt + password).encode("utf-8")
    return hashlib.sha256(data).hexdigest()

def _normalize_store(store: AuthenticationStore) -> AuthenticationStore:
    for user in store.users:
        normalized_auths: list[AuthCredential] = []

        # First pass: convert cleartext entries to sha256.
        for cred in user.authentications:
            if cred.type == "cleartext":
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

def initialize_authentication() -> None:
    db = get_db_manager()
    store = db.get_auth_store()
    # Normalize on startup
    _normalize_store(store)
    db.save_auth_store(store)

def _find_user(username: str) -> Optional[AuthUser]:
    db = get_db_manager()
    store = db.get_auth_store()
    for user in store.users:
        if user.username == username:
            return user
    return None

def has_any_user() -> bool:
    db = get_db_manager()
    store = db.get_auth_store()
    return len(store.users) > 0

def create_user(username: str, password: str) -> AuthUser:
    db = get_db_manager()
    store = db.get_auth_store()

    if _find_user(username) is not None:
        raise ValueError("User already exists")

    salt = secrets.token_hex(16)
    hashed = _hash_password_sha256(password, salt)
    cred = AuthCredential(type="sha256", password=hashed, salt=salt)
    user = AuthUser(username=username, authentications=[cred])
    store.users.append(user)
    
    _normalize_store(store)
    db.save_auth_store(store)
    return user

def verify_user_password(username: str, password: str) -> bool:
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
    db = get_db_manager()
    store = db.get_auth_store()

    session_id = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    session = AuthSession(session_id=session_id, last_login=now, username=username)
    store.sessions.append(session)
    db.save_auth_store(store)
    return session

def get_user_for_session(session_id: str) -> Optional[AuthUser]:
    if not session_id:
        return None

    db = get_db_manager()
    store = db.get_auth_store()
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
    db.save_auth_store(store)
    return user

def clear_session(session_id: str) -> None:
    if not session_id:
        return

    db = get_db_manager()
    store = db.get_auth_store()
    store.sessions = [s for s in store.sessions if s.session_id != session_id]
    db.save_auth_store(store)

