# INFO: FastAPI dependency that opens the image color scope of a request and
# schedules the computation of the missing colors.

from dataclasses import dataclass
from typing import TypeVar

from fastapi import BackgroundTasks, Depends, Response
from supabase import Client

from core.cache_control import cap_max_age
from core.config import settings
from core.database import get_supabase
from core.image_color_scope import ImageColorScope, open_image_color_scope
from services.image_color_service import compute_image_colors, load_image_colors

T = TypeVar("T")


def get_image_color_db() -> Client:
    # Service-role client: image_colors is not user data (ADR 013 reasons the
    # same for error_logs) and only service_role can read it (migration 042).
    # A function of its own so tests can replace it without touching get_db.
    return get_supabase()


@dataclass(frozen=True)
class ImageColors:
    db: Client
    scope: ImageColorScope
    # The sub-response FastAPI hands to the route too: with_image_colors
    # shortens its Cache-Control through it.
    response: Response


# async def on purpose, and the first one in the repo. FastAPI runs a sync
# dependency in the threadpool on a copy of the context: the ContextVar set
# made there would never reach the serialization of the response, which runs
# in the event loop, and every color would come out null with the color
# loaded. An async dependency is awaited inline, so the set lives in the
# context of the request, the sync endpoint (threadpool, copied context) sees
# the same scope object and so does the serialization. Do not turn it into a
# plain def. The computation is a background task: it runs after the response
# is sent, so a download never delays a request.
async def image_colors(
    background_tasks: BackgroundTasks,
    response: Response,
    db: Client = Depends(get_image_color_db),  # noqa: B008
) -> ImageColors:
    scope = open_image_color_scope()
    background_tasks.add_task(compute_image_colors, db, scope)
    return ImageColors(db=db, scope=scope, response=response)


def with_image_colors(colors: ImageColors, envelope: T) -> T:
    # Depends on the route setting its Cache-Control before calling this (the
    # routes with a max-age do). While a color is still to be computed the
    # max-age is capped so the client asks again soon; no-cache and no-store
    # are left alone.
    load_image_colors(colors.db, colors.scope, envelope)
    if colors.scope.awaiting_colors:
        cap_max_age(colors.response, settings.image_color_pending_max_age)
    return envelope
