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


router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request) -> HTMLResponse:
    """
    Show the login page.

    If a valid session already exists, redirect to the admin package list.
    If no user has been created yet, redirect to the registration page.
    """
    session_id = request.cookies.get(SESSION_COOKIE_NAME)
    if session_id and get_user_for_session(session_id):
        return RedirectResponse(url="/admin/packages", status_code=status.HTTP_302_FOUND)

    if not has_any_user():
        return RedirectResponse(url="/register", status_code=status.HTTP_302_FOUND)

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
    Handle login form submission.

    On success, create a session cookie and redirect to the admin package list.
    On failure, re-render the login page with an error.
    """
    if not has_any_user():
        return RedirectResponse(url="/register", status_code=status.HTTP_302_FOUND)

    if verify_user_password(username, password):
        session = create_session(username)
        response = RedirectResponse(
            url="/admin/packages",
            status_code=status.HTTP_302_FOUND,
        )
        # Simple cookie settings suitable for local development.
        response.set_cookie(
            SESSION_COOKIE_NAME,
            session.session_id,
            httponly=True,
            max_age=7 * 24 * 3600,
            samesite="lax",
        )
        return response

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
    Show the registration page.

    This is only accessible when:
      * no user has been created yet, or
      * a valid session already exists (admin creating additional users).
    """
    session_id = request.cookies.get(SESSION_COOKIE_NAME)
    user = get_user_for_session(session_id) if session_id else None
    any_user = has_any_user()

    if any_user and not user:
        # Users already exist and no valid session -> force login.
        return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)

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
    Handle registration form submission.

    Enforces the same access rules as GET /register.
    Stores passwords as SHA256 with a per-user random salt.
    """
    session_id = request.cookies.get(SESSION_COOKIE_NAME)
    user = get_user_for_session(session_id) if session_id else None
    any_user = has_any_user()

    if any_user and not user:
        return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)

    if password != confirm_password:
        return templates.TemplateResponse(
            "register.html",
            {
                "request": request,
                "error": "Passwords do not match.",
            },
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    try:
        create_user(username, password)
    except ValueError:
        return templates.TemplateResponse(
            "register.html",
            {
                "request": request,
                "error": "A user with that username already exists.",
            },
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    # Automatically sign in as the newly created user.
    session = create_session(username)
    response = RedirectResponse(
        url="/admin/packages",
        status_code=status.HTTP_302_FOUND,
    )
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
    Clear the current session (if any) and redirect to the home page.
    """
    session_id = request.cookies.get(SESSION_COOKIE_NAME)
    if session_id:
        clear_session(session_id)

    response = RedirectResponse(url="/", status_code=status.HTTP_302_FOUND)
    response.delete_cookie(SESSION_COOKIE_NAME)
    return response


