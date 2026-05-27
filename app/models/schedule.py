from pydantic import BaseModel, Field
from typing import Literal, Optional


class Block(BaseModel):
    id: str
    title: str
    start: str                              # display '9:00 AM'
    end: str                                # display '10:30 AM'
    startISO: str                           # '2026-05-20T09:00:00'
    endISO: str                             # '2026-05-20T10:30:00'
    durationMin: int
    category: Literal["class", "gym", "food", "work"]
    location: Optional[str] = None
    hint: Optional[str] = None
    changed: Optional[bool] = False
    done: Optional[bool] = False


class VoiceCommandRequest(BaseModel):
    transcript: str = Field(..., min_length=1, max_length=2000)
    current_schedule: list[Block]
    current_time_context: str               # ISO local datetime e.g. '2026-05-20T11:30:00'


class VoiceCommandResponse(BaseModel):
    status: Literal["ok", "error"]
    new_schedule: list[Block]
    message: str


class GenerateRequest(BaseModel):
    description: str = Field(..., min_length=1, max_length=2000)
    user_name: Optional[str] = None
    target_date: str                        # 'YYYY-MM-DD'


class GenerateResponse(BaseModel):
    status: Literal["ok", "error"]
    schedule: list[Block]
    message: str
