# INFO: Genre response models.

from pydantic import BaseModel


class Genre(BaseModel):
    slug: str
    name: str
    description: str | None = None


class GenreList(BaseModel):
    genres: list[Genre]
