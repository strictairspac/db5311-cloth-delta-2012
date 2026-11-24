from typing import Any

from pydantic import BaseModel, Field


class LoginForm(BaseModel):
    username: str = Field(min_length=1, max_length=40)
    password: str = Field(min_length=1, max_length=128)


class CaseForm(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    payload: dict[str, Any]


class ReviewForm(BaseModel):
    note: str = Field(min_length=1, max_length=500)


class TripForm(BaseModel):
    trip_no: str = Field(min_length=1, max_length=64)
    capacity_g: int = Field(ge=0, le=1_000_000_000)
    capacity_pieces: int = Field(ge=0, le=1_000_000_000)


class BookingForm(BaseModel):
    evidence_id: int
    batch_id: str = Field(min_length=1, max_length=64)


class MoveForm(BaseModel):
    trip_id: int


class ResizeForm(BaseModel):
    capacity_g: int = Field(ge=0, le=1_000_000_000)
    capacity_pieces: int = Field(ge=0, le=1_000_000_000)
