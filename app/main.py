from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.data.repository import initialize_repository


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
    Initialize the JSON-backed repository and build the in-memory index.
    """
    await initialize_repository()


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
except ImportError:
    # During very early scaffolding, the router may not exist yet.
    pass

# Admin UI routes (HTML tooling for managing packages/versions)
try:
    from app.api.admin import router as admin_router

    app.include_router(admin_router, tags=["admin"])
except ImportError:
    pass


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


