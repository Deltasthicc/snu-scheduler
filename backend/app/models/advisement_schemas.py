"""Strict transport model for private advisement-report parsing.

The PDF is processed in memory and is never persisted by the API.
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator


class AdvisementParseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    filename: str = Field(..., min_length=1, max_length=240)
    content_base64: str = Field(..., min_length=8, max_length=12_000_000)

    @field_validator("filename")
    @classmethod
    def pdf_filename(cls, value: str) -> str:
        name = value.strip()
        if not name.lower().endswith(".pdf"):
            raise ValueError("advisement report must be a PDF")
        return name
