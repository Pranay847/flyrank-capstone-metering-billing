"""Validation at the boundary. Strict types (no "100" strings, no floats, no bools as
ints) and extra fields rejected, so bad input becomes a clean 422, never a 500."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

MAX_TOKENS = 10_000_000


class TokenUsageIn(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    input_tokens: int = Field(ge=0, le=MAX_TOKENS,
                              description="Total prompt tokens, INCLUDING cached ones.")
    cached_input_tokens: int = Field(0, ge=0, le=MAX_TOKENS,
                                     description="Cached subset of input_tokens.")
    output_tokens: int = Field(ge=0, le=MAX_TOKENS,
                               description="Visible output tokens, EXCLUDING reasoning.")
    reasoning_tokens: int = Field(0, ge=0, le=MAX_TOKENS,
                                  description="Hidden thinking tokens; billed at the output rate.")

    @model_validator(mode="after")
    def cached_is_subset(self):
        if self.cached_input_tokens > self.input_tokens:
            raise ValueError("cached_input_tokens cannot exceed input_tokens "
                             "(cached tokens are a subset of input tokens)")
        return self


class GenerateIn(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    prompt: str = Field(min_length=1, max_length=10_000)
    usage: TokenUsageIn | None = Field(
        None, description="Simulated provider token counts. If omitted, derived from the prompt.")


class TenantIn(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    name: str = Field(min_length=1, max_length=128)
