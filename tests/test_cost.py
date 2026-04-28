from __future__ import annotations

from types import SimpleNamespace

from wide_research import cost


def _reset_catalog(monkeypatch) -> None:
    monkeypatch.setattr(cost, "_CATALOG", None)
    monkeypatch.setattr(cost, "_CATALOG_SOURCE", None)
    monkeypatch.setattr(cost, "_CATALOG_FETCH_FAILED", False)
    monkeypatch.setattr(cost, "_CATALOG_REFRESHED_AFTER_MISS", False)


def test_estimate_refreshes_cached_catalog_on_model_miss(monkeypatch) -> None:
    _reset_catalog(monkeypatch)
    monkeypatch.delenv("WR_PRICE_TABLE_JSON", raising=False)
    monkeypatch.delenv("WR_DISABLE_PRICE_FETCH", raising=False)
    monkeypatch.setattr(
        cost,
        "_load_cached_catalog",
        lambda: {"openai/gpt-5.4": {"input": 2.5, "output": 15.0}},
    )
    monkeypatch.setattr(
        cost,
        "_fetch_openrouter_catalog",
        lambda: {
            "openai/gpt-5.5": {
                "input": 5.0,
                "cached_input": 0.5,
                "output": 30.0,
            },
        },
    )
    usage = SimpleNamespace(
        requests=1,
        input_tokens=1000,
        output_tokens=500,
        total_tokens=1500,
        input_tokens_details=SimpleNamespace(cached_tokens=200),
        output_tokens_details=SimpleNamespace(reasoning_tokens=25),
    )

    breakdown = cost.estimate("gpt-5.5", usage)

    assert breakdown.priced is True
    assert breakdown.cost_usd == 0.0191
    assert breakdown.cached_input_tokens == 200

