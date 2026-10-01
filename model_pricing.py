from __future__ import annotations

from dataclasses import dataclass

_AZURE_MODEL_PRICING_USD_PER_M: dict[str, dict[str, float]] = {
    "gpt-5-mini": {
        "input": 0.25,
        "cached_input": 0.025,
        "output": 2.00,
    },
    "gpt-5-chat": {
        "input": 1.25,
        "cached_input": 0.125,
        "output": 10.00,
    },
    "gpt-5.2": {
        "input": 1.75,
        "cached_input": 0.175,
        "output": 14.00,
    },
    "gpt-5.4": {
        "input": 2.50,
        "cached_input": 0.25,
        "output": 15.00,
    },
    "gpt-5.5": {
        "input": 5.00,
        "cached_input": 0.50,
        "output": 30.00,
    },
}

_DEFAULT_PRICING = _AZURE_MODEL_PRICING_USD_PER_M["gpt-5-chat"]

@dataclass(frozen=True)
class ModelPricing:
    input_per_m: float
    cached_input_per_m: float
    output_per_m: float

def pricing_for_model(model: str | None) -> ModelPricing:
    key = (model or "").strip().lower()
    if key in _AZURE_MODEL_PRICING_USD_PER_M:
        row = _AZURE_MODEL_PRICING_USD_PER_M[key]
    else:
        row = _DEFAULT_PRICING
    return ModelPricing(
        input_per_m=row["input"],
        cached_input_per_m=row["cached_input"],
        output_per_m=row["output"],
    )

def estimate_usage_cost_usd(
    model: str | None,
    *,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    cached_tokens: int = 0,
) -> float:
    p = pricing_for_model(model)
    cached = max(0, min(int(cached_tokens or 0), int(prompt_tokens or 0)))
    uncached_input = max(0, int(prompt_tokens or 0) - cached)
    output = max(0, int(completion_tokens or 0))
    return (
        uncached_input * p.input_per_m
        + cached * p.cached_input_per_m
        + output * p.output_per_m
    ) / 1_000_000.0

def format_usd(amount: float) -> str:
    if amount < 0.0001:
        return "$0.00"
    if amount < 0.01:
        return f"${amount:.4f}"
    if amount < 1.0:
        return f"${amount:.3f}"
    return f"${amount:.2f}"

def format_token_count(value: int | None) -> str:
    if value is None:
        return "0"
    return f"{int(value):,}"

def all_model_pricing() -> dict[str, dict[str, float]]:
    return {k: dict(v) for k, v in _AZURE_MODEL_PRICING_USD_PER_M.items()}
