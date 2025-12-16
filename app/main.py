import asyncio
import logging

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.data.repository import initialize_repository
from app.data.authentication import initialize_authentication
from app.data.cached_packages_updater import daily_update_loop

# Configure logging
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


app = FastAPI(
    title="Python winget REST Repository",
    version="0.1.0",
    description="Minimal FastAPI-based implementation of a winget-compatible REST source.",
)


# Static files (CSS, images, JS)
app.mount("/static", StaticFiles(directory="app/static"), name="static")

# HTML templates (Jinja2)
templates = Jinja2Templates(directory="app/templates")


@app.on_event("startup")
async def startup_event() -> None:
    """
    Initialize the JSON-backed repository, build the in-memory index,
    set up authentication storage, and start background tasks.
    """
    await initialize_repository()
    initialize_authentication()
    
    # Job #1 (repository refresh from disk) is started inside initialize_repository().
    # Job #2 (cached package updates) runs daily at 06:00 local time.
    asyncio.create_task(daily_update_loop(run_hour=6, run_minute=0))


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    """
    Simple landing page so you can see something in a browser.
    """
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "title": "winget REST Repository",
        },
    )


@app.get("/health")
async def health() -> dict:
    """
    Lightweight health check endpoint.
    """
    return {"status": "ok"}


# Import API routes for winget REST source (defined in app/api/winget.py)
try:
    from app.api.winget import router as winget_router

    app.include_router(winget_router, prefix="/winget", tags=["winget"])
    logger.info("Successfully loaded winget router")
except ImportError as e:
    # During very early scaffolding, the router may not exist yet.
    logger.warning(f"Failed to import winget router: {e}")
except Exception as e:
    logger.error(f"Error loading winget router: {e}", exc_info=True)

# Admin UI routes (HTML tooling for managing packages/versions)
try:
    from app.api.admin import router as admin_router

    app.include_router(admin_router, tags=["admin"])
    logger.info("Successfully loaded admin router")
except ImportError as e:
    logger.warning(f"Failed to import admin router: {e}")
except Exception as e:
    logger.error(f"Error loading admin router: {e}", exc_info=True)

# Authentication routes (login/registration/logout)
try:
    from app.api.auth import router as auth_router

    app.include_router(auth_router, tags=["auth"])
    logger.info("Successfully loaded auth router")
except ImportError as e:
    logger.warning(f"Failed to import auth router: {e}")
except Exception as e:
    logger.error(f"Error loading auth router: {e}", exc_info=True)


if __name__ == "__main__":
    """
    Allow running `python app/main.py` (or debugging this file in VS Code)
    to start the Uvicorn development server.
    """
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
    )


