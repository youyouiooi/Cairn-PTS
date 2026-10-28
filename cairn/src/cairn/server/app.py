from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from cairn import __version__
from cairn.server import auth, db
from cairn.server.routers import export, findings, hints, intents, llm_config, projects, settings
from cairn.server.routers import auth as auth_router

STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.configure(db.current_path() or db.default_path())
    generated = auth.bootstrap_auth()
    if generated:
        print("Cairn auth bootstrap (shown once; also written to credentials file):")
        if generated.get("username"):
            print(f"  username: {generated['username']}")
        if generated.get("password"):
            print(f"  password: {generated['password']}")
        if generated.get("api_token"):
            print(f"  api_token: {generated['api_token']}")
        if generated.get("credentials_file"):
            print(f"  credentials_file: {generated['credentials_file']}")
        print("Set CAIRN_AUTH_USERNAME / CAIRN_AUTH_PASSWORD / CAIRN_API_TOKEN to use your own values.")
    yield


app = FastAPI(
    title="Cairn",
    description="Fact-graph based collaborative exploration protocol",
    version=__version__,
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
)

app.middleware("http")(auth.auth_middleware)

app.include_router(auth_router.router)
app.include_router(settings.router)
app.include_router(llm_config.router)
app.include_router(projects.router)
app.include_router(findings.router)
app.include_router(hints.router)
app.include_router(intents.router)
app.include_router(export.router)


@app.get("/healthz", include_in_schema=False)
def healthz():
    return JSONResponse({"ok": True})


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
