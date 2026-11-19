"""Static pages and assets: the HTML shells, the allowlisted scripts and styles, and home."""

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse

from rehearse_ai.server.build import PACKAGE_DIR
from rehearse_ai.server.context import ServerContext
from rehearse_ai.server.dependencies import Ctx, Db

WEB_DIR = PACKAGE_DIR / "web"

# Only these names are served under /assets/. The URL names are part of the browser contract.
ASSETS = frozenset(
    {
        "app.js",
        "media_setup.js",
        "styles.css",
        "account.js",
        "account.css",
        "history.js",
        "theme.css",
        "assigned.js",
        "assignments.js",
        "schedule.js",
        "room_capture.js",
    }
)

router = APIRouter()


def render_page(ctx: ServerContext, name: str) -> HTMLResponse:
    html = (WEB_DIR / name).read_text(encoding="utf-8")
    return HTMLResponse(
        html.replace("__BUILD_ID__", ctx.build_id),
        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
    )


def sign_in_redirect(ctx: ServerContext, request: Request) -> RedirectResponse:
    reason = "expired" if ctx.accounts.cookies.cookie_name in request.cookies else "required"
    return RedirectResponse(f"/login?reason={reason}", status_code=303)


@router.get("/assets/{name}")
async def asset(name: str) -> Response:
    if name not in ASSETS:
        raise HTTPException(404)
    path = WEB_DIR / name
    media = "text/javascript" if name.endswith(".js") else "text/css"
    return Response(path.read_text(encoding="utf-8"), media_type=media)


@router.get("/")
async def home(ctx: Ctx, request: Request, db: Db) -> RedirectResponse:
    user = await ctx.accounts.current_user(request, db)
    if user and user.is_superuser:
        return RedirectResponse("/admin/interviews", status_code=303)
    return sign_in_redirect(ctx, request)


@router.get("/healthz")
async def health(ctx: Ctx, response: Response) -> dict[str, str]:
    response.headers["Cache-Control"] = "no-store"
    return {"status": "ready", "build_id": ctx.build_id}
