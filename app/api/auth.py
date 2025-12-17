"""
Authentication API routes for user login, registration, and logout.

This module provides FastAPI routes for handling user authentication:
- Login page and form submission
- Registration page and form submission
- Logout functionality

All routes use session-based authentication with HTTP-only cookies.
"""

from __future__ import annotations

from fastapi import APIRouter, Form, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.services.authentication import (
    SESSION_COOKIE_NAME,
    create_session,
    create_user,
    get_user_for_session,
    has_any_user,
    verify_user_password,
    clear_session,
)


# FastAPI router for authentication endpoints
router = APIRouter()

# Jinja2 templates for rendering HTML pages
templates = Jinja2Templates(directory="app/templates")


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request) -> HTMLResponse:
    """
    Display the login page.

    Args:
        request: The FastAPI request object containing cookies and session data.

    Returns:
        HTMLResponse: The rendered login page, or a redirect response if:
            - User is already authenticated (redirects to /admin/packages)
            - No users exist yet (redirects to /register)

    Behavior:
        - Checks for existing valid session cookie
        - Redirects authenticated users to admin panel
        - Redirects to registration if this is the first user
        - Otherwise displays the login form
    """
    # Check if user already has a valid session
    session_id = request.cookies.get(SESSION_COOKIE_NAME)
    if session_id and get_user_for_session(session_id):
        return RedirectResponse(url="/admin/packages", status_code=status.HTTP_302_FOUND)

    # If no users exist, redirect to registration page
    if not has_any_user():
        return RedirectResponse(url="/register", status_code=status.HTTP_302_FOUND)

    # Display login form
    return templates.TemplateResponse(
        "login.html",
        {
            "request": request,
            "error": None,
        },
    )


@router.post("/login")
async def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
):
    """
    Process login form submission and authenticate user.

    Args:
        request: The FastAPI request object.
        username: The username submitted from the login form.
        password: The password submitted from the login form.

    Returns:
        RedirectResponse: Redirects to /admin/packages on successful login,
            or to /register if no users exist yet.
        HTMLResponse: Renders login page with error message on authentication failure.

    Behavior:
        - Verifies credentials against stored user data
        - Creates a new session on successful authentication
        - Sets HTTP-only cookie with session ID (7-day expiration)
        - Returns 401 status with error message on failure
    """
    # Ensure at least one user exists before allowing login
    if not has_any_user():
        return RedirectResponse(url="/register", status_code=status.HTTP_302_FOUND)

    # Verify credentials and create session if valid
    if verify_user_password(username, password):
        session = create_session(username)
        response = RedirectResponse(
            url="/admin/packages",
            status_code=status.HTTP_302_FOUND,
        )
        # Set secure session cookie
        # httponly=True prevents JavaScript access (XSS protection)
        # max_age=7 days (7 * 24 * 3600 seconds)
        # samesite="lax" provides CSRF protection while allowing normal navigation
        response.set_cookie(
            SESSION_COOKIE_NAME,
            session.session_id,
            httponly=True,
            max_age=7 * 24 * 3600,
            samesite="lax",
        )
        return response

    # Authentication failed - show error message
    return templates.TemplateResponse(
        "login.html",
        {
            "request": request,
            "error": "Invalid username or password.",
        },
        status_code=status.HTTP_401_UNAUTHORIZED,
    )


@router.get("/register", response_class=HTMLResponse)
async def register_page(request: Request) -> HTMLResponse:
    """
    Display the registration page.

    Args:
        request: The FastAPI request object containing cookies and session data.

    Returns:
        HTMLResponse: The rendered registration page, or a redirect to /login
            if users exist but the current session is invalid.

    Access Control:
        Registration is accessible in two scenarios:
        1. No users exist yet (first-time setup)
        2. User has a valid authenticated session (admin creating additional users)

        If users exist but the session is invalid, redirects to login page.
    """
    # Check for existing session
    session_id = request.cookies.get(SESSION_COOKIE_NAME)
    user = get_user_for_session(session_id) if session_id else None
    any_user = has_any_user()

    # If users exist but no valid session, require login first
    if any_user and not user:
        return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)

    # Display registration form
    return templates.TemplateResponse(
        "register.html",
        {
            "request": request,
            "error": None,
        },
    )


@router.post("/register")
async def register_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    confirm_password: str = Form(...),
):
    """
    Process registration form submission and create new user account.

    Args:
        request: The FastAPI request object.
        username: The desired username from the registration form.
        password: The password from the registration form.
        confirm_password: The password confirmation field to verify matching passwords.

    Returns:
        RedirectResponse: Redirects to /admin/packages on successful registration,
            or to /login if access is denied.
        HTMLResponse: Renders registration page with error message on validation failure.

    Behavior:
        - Enforces the same access control rules as GET /register
        - Validates that password and confirm_password match
        - Creates user with SHA256 hashed password and per-user random salt
        - Automatically logs in the newly created user
        - Returns 400 status with error message on validation failures

    Raises:
        ValueError: If username already exists (handled internally).
    """
    # Check access control (same rules as GET /register)
    session_id = request.cookies.get(SESSION_COOKIE_NAME)
    user = get_user_for_session(session_id) if session_id else None
    any_user = has_any_user()

    if any_user and not user:
        return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)

    # Validate password confirmation matches
    if password != confirm_password:
        return templates.TemplateResponse(
            "register.html",
            {
                "request": request,
                "error": "Passwords do not match.",
            },
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    # Create new user account (password is hashed with SHA256 and salt)
    try:
        create_user(username, password)
    except ValueError:
        # Username already exists
        return templates.TemplateResponse(
            "register.html",
            {
                "request": request,
                "error": "A user with that username already exists.",
            },
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    # Automatically authenticate the newly created user
    session = create_session(username)
    response = RedirectResponse(
        url="/admin/packages",
        status_code=status.HTTP_302_FOUND,
    )
    # Set secure session cookie (same settings as login)
    response.set_cookie(
        SESSION_COOKIE_NAME,
        session.session_id,
        httponly=True,
        max_age=7 * 24 * 3600,
        samesite="lax",
    )
    return response


@router.get("/logout")
async def logout(request: Request):
    """
    Log out the current user and clear their session.

    Args:
        request: The FastAPI request object containing the session cookie.

    Returns:
        RedirectResponse: Redirects to the home page (/) after clearing the session.

    Behavior:
        - Clears the session from storage if it exists
        - Deletes the session cookie from the client
        - Always redirects to home page, even if no session was active
    """
    # Clear session from storage if it exists
    session_id = request.cookies.get(SESSION_COOKIE_NAME)
    if session_id:
        clear_session(session_id)

    # Redirect to home and delete cookie
    response = RedirectResponse(url="/", status_code=status.HTTP_302_FOUND)
    response.delete_cookie(SESSION_COOKIE_NAME)
    return response