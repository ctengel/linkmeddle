import pydantic

class PlaylistCreate(pydantic.BaseModel):
    url: str