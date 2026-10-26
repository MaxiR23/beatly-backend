# INFO: The per-request place where the image URLs of a response are noted and
# the colors read for them are looked up.

# Leaf module on purpose, stdlib only: core/thumbnails.py imports it and the
# models import core/thumbnails.py. The scope lives in a ContextVar because
# the computed fields of the models have no access to the request.
#
# The ContextVar must be set by an `async def` dependency (see
# core/image_color_dependency.py): FastAPI runs a sync dependency in the
# threadpool on a copy of the context, and a set() made there never reaches
# the serialization of the response.

from contextvars import ContextVar
from dataclasses import dataclass, field


@dataclass
class ImageColorScope:
    # image_key -> the small-tier URL, the first one seen for that key.
    seen: dict[str, str] = field(default_factory=dict)
    # image_key -> "#rrggbb", loaded by services/image_color_service.py.
    colors: dict[str, str] = field(default_factory=dict)
    # image_key -> URL still to be computed after the response is sent.
    pending: dict[str, str] = field(default_factory=dict)
    loaded: bool = False
    read_failed: bool = False


_scope: ContextVar[ImageColorScope | None] = ContextVar(
    "image_color_scope", default=None
)


def open_image_color_scope() -> ImageColorScope:
    scope = ImageColorScope()
    _scope.set(scope)
    return scope


def observe_image(key: str, url: str) -> str | None:
    # Outside a request (no scope) it records nothing and the color is None.
    scope = _scope.get()
    if scope is None:
        return None
    scope.seen.setdefault(key, url)
    return scope.colors.get(key)
