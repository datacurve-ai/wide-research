"""Per-subtask cost estimation from openai-agents Usage.

Pricing comes from the OpenRouter public catalog
(`https://openrouter.ai/api/v1/models`, no auth required) and is cached
on disk for a week. We refuse to ship hard-coded numbers because they
go stale fast and are easy to get wrong.

Resolution order for a model name:

1. `WR_PRICE_TABLE_JSON` env var (JSON `{model: {input, output,
   cached_input?}}` in USD per 1M tokens) — wins if present.
2. OpenRouter catalog (cached). We try, in order:
     - exact id (`gpt-5.4` matches `openai/gpt-5.4` if the user wrote it
       bare, or any provider id verbatim like `anthropic/claude-…`),
     - `openai/{model}` (the common case here),
     - any catalog id that ends in `/{model}`.
3. Nothing → return `cost_usd=None` with `priced=False`. Never raises.

Disable the network entirely with `WR_DISABLE_PRICE_FETCH=1` (the
override table is still honoured). Bust the cache by deleting
`~/.wide-research/cache/openrouter-models.json` or the directory
pointed to by `WR_PRICE_CACHE_DIR`.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from wide_research.paths import tool_home


log = logging.getLogger("wide_research.cost")


_OPENROUTER_URL = "https://openrouter.ai/api/v1/models"
_CACHE_FILENAME = "openrouter-models.json"
_CACHE_TTL_SECONDS = 7 * 24 * 3600  # one week
_HTTP_TIMEOUT_SECONDS = 8.0

# Set on first successful fetch / load. {model_id: {"input", "output",
# "cached_input"}} in USD per 1M tokens. None means "not yet attempted".
_CATALOG: dict[str, dict[str, float]] | None = None
_CATALOG_SOURCE: str | None = None
_CATALOG_FETCH_FAILED = False  # set after one failure so we don't retry on every subtask
_CATALOG_REFRESHED_AFTER_MISS = False


@dataclass(slots=True)
class CostBreakdown:
    model: str
    requests: int
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    reasoning_tokens: int
    total_tokens: int
    cost_usd: float | None  # None ⇒ no pricing data
    priced: bool            # False ⇒ model not in table

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ── Catalog loading ─────────────────────────────────────────────────────────


def _cache_dir() -> Path:
    env = os.environ.get("WR_PRICE_CACHE_DIR")
    if env:
        return Path(env).expanduser()
    return tool_home() / "cache"


def _cache_path() -> Path:
    return _cache_dir() / _CACHE_FILENAME


def _parse_openrouter_payload(raw: dict[str, Any]) -> dict[str, dict[str, float]]:
    """Convert OpenRouter `/models` JSON into our {id: {input, output,
    cached_input}} map. Prices in OpenRouter are USD per token; we store
    USD per 1M tokens to match the override schema."""
    out: dict[str, dict[str, float]] = {}
    for entry in raw.get("data", []) or []:
        mid = entry.get("id")
        pricing = entry.get("pricing") or {}
        if not isinstance(mid, str) or not pricing:
            continue
        try:
            in_per_tok = float(pricing.get("prompt") or 0.0)
            out_per_tok = float(pricing.get("completion") or 0.0)
        except (TypeError, ValueError):
            continue
        if in_per_tok <= 0 and out_per_tok <= 0:
            # Free models or malformed entries — skip rather than bill $0.
            continue
        record = {
            "input": in_per_tok * 1_000_000,
            "output": out_per_tok * 1_000_000,
        }
        cache_read = pricing.get("input_cache_read")
        if cache_read not in (None, ""):
            try:
                record["cached_input"] = float(cache_read) * 1_000_000
            except (TypeError, ValueError):
                pass
        out[mid] = record
    return out


def _load_cached_catalog() -> dict[str, dict[str, float]] | None:
    path = _cache_path()
    if not path.exists():
        return None
    try:
        age = time.time() - path.stat().st_mtime
        if age > _CACHE_TTL_SECONDS:
            return None
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and data:
            return data
    except (OSError, json.JSONDecodeError) as exc:
        log.debug("cost: ignoring unreadable price cache at %s (%s)", path, exc)
    return None


def _write_cache(catalog: dict[str, dict[str, float]]) -> None:
    path = _cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(catalog, f)
        tmp.replace(path)
    except OSError as exc:
        log.debug("cost: could not write price cache at %s (%s)", path, exc)


def _fetch_openrouter_catalog() -> dict[str, dict[str, float]] | None:
    req = urllib.request.Request(
        _OPENROUTER_URL,
        headers={"User-Agent": "wide-research/0 (+pricing)"},
    )
    try:
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_SECONDS) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
        log.warning(
            "cost: could not reach OpenRouter for prices (%s); "
            "set WR_PRICE_TABLE_JSON to provide them manually",
            exc,
        )
        return None
    catalog = _parse_openrouter_payload(raw)
    if not catalog:
        log.warning("cost: OpenRouter returned no parsable pricing entries")
        return None
    _write_cache(catalog)
    return catalog


def _get_catalog() -> dict[str, dict[str, float]]:
    """Return a model → price map. Empty dict if unavailable."""
    global _CATALOG, _CATALOG_FETCH_FAILED, _CATALOG_SOURCE
    if _CATALOG is not None:
        return _CATALOG
    cached = _load_cached_catalog()
    if cached is not None:
        _CATALOG = cached
        _CATALOG_SOURCE = "cache"
        return _CATALOG
    if _CATALOG_FETCH_FAILED or os.environ.get("WR_DISABLE_PRICE_FETCH"):
        _CATALOG = {}
        _CATALOG_SOURCE = "empty"
        return _CATALOG
    fetched = _fetch_openrouter_catalog()
    if fetched is None:
        _CATALOG_FETCH_FAILED = True
        _CATALOG = {}
        _CATALOG_SOURCE = "empty"
    else:
        _CATALOG = fetched
        _CATALOG_SOURCE = "fetch"
    return _CATALOG


# ── Override table ─────────────────────────────────────────────────────────


def _override_table() -> dict[str, dict[str, float]]:
    raw = os.environ.get("WR_PRICE_TABLE_JSON")
    if not raw:
        return {}
    try:
        table = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        log.warning("WR_PRICE_TABLE_JSON malformed (%s); ignoring", exc)
        return {}
    if not isinstance(table, dict):
        return {}
    return table


# ── Resolution ─────────────────────────────────────────────────────────────


def _find_in_catalog(model: str, catalog: dict[str, dict[str, float]]) -> dict[str, float] | None:
    if model in catalog:
        return catalog[model]
    openai_id = f"openai/{model}"
    if openai_id in catalog:
        return catalog[openai_id]
    suffix = f"/{model}"
    for cid, price in catalog.items():
        if cid.endswith(suffix):
            return price
    return None


def _find_price(model: str) -> dict[str, float] | None:
    global _CATALOG, _CATALOG_SOURCE, _CATALOG_FETCH_FAILED, _CATALOG_REFRESHED_AFTER_MISS

    override = _override_table()
    if model in override:
        return override[model]

    catalog = _get_catalog()
    price = _find_in_catalog(model, catalog)
    if price is not None:
        return price

    if (
        _CATALOG_SOURCE == "cache"
        and not _CATALOG_REFRESHED_AFTER_MISS
        and not _CATALOG_FETCH_FAILED
        and not os.environ.get("WR_DISABLE_PRICE_FETCH")
    ):
        _CATALOG_REFRESHED_AFTER_MISS = True
        refreshed = _fetch_openrouter_catalog()
        if refreshed is None:
            _CATALOG_FETCH_FAILED = True
        else:
            _CATALOG = refreshed
            _CATALOG_SOURCE = "fetch"
            return _find_in_catalog(model, refreshed)

    return None


# ── Public API ─────────────────────────────────────────────────────────────


def estimate(model: str, usage: Any) -> CostBreakdown:
    """Summarise one Usage object into a CostBreakdown. Never raises."""
    try:
        requests = int(getattr(usage, "requests", 0) or 0)
        input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
        output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
        total_tokens = int(getattr(usage, "total_tokens", 0) or 0)
        in_details = getattr(usage, "input_tokens_details", None)
        out_details = getattr(usage, "output_tokens_details", None)
        cached = int(getattr(in_details, "cached_tokens", 0) or 0) if in_details else 0
        reasoning = int(getattr(out_details, "reasoning_tokens", 0) or 0) if out_details else 0
    except Exception as exc:  # noqa: BLE001 — fail open
        log.warning("cost: could not read usage fields (%s); returning zeros", exc)
        return CostBreakdown(model, 0, 0, 0, 0, 0, 0, None, False)

    price = _find_price(model)
    if price is None:
        log.warning("cost: no price for model %r; reporting None", model)
        return CostBreakdown(
            model=model,
            requests=requests,
            input_tokens=input_tokens,
            cached_input_tokens=cached,
            output_tokens=output_tokens,
            reasoning_tokens=reasoning,
            total_tokens=total_tokens,
            cost_usd=None,
            priced=False,
        )

    in_per_m = price.get("input", 0.0)
    cached_per_m = price.get("cached_input", in_per_m)
    out_per_m = price.get("output", 0.0)
    # Subtract cached from input so we don't bill both rates on those tokens.
    billed_input = max(0, input_tokens - cached)
    cost = (
        (billed_input * in_per_m)
        + (cached * cached_per_m)
        + (output_tokens * out_per_m)
    ) / 1_000_000

    return CostBreakdown(
        model=model,
        requests=requests,
        input_tokens=input_tokens,
        cached_input_tokens=cached,
        output_tokens=output_tokens,
        reasoning_tokens=reasoning,
        total_tokens=total_tokens,
        cost_usd=round(cost, 6),
        priced=True,
    )


def sum_costs(items: list[CostBreakdown]) -> dict[str, Any]:
    """Aggregate a list of breakdowns. None-cost items are counted but their
    cost rolls into `unpriced_subtasks` — the $ total ignores them."""
    totals = {
        "requests": 0,
        "input_tokens": 0,
        "cached_input_tokens": 0,
        "output_tokens": 0,
        "reasoning_tokens": 0,
        "total_tokens": 0,
        "cost_usd": 0.0,
        "priced_subtasks": 0,
        "unpriced_subtasks": 0,
        "unpriced_models": set(),
    }
    for b in items:
        totals["requests"] += b.requests
        totals["input_tokens"] += b.input_tokens
        totals["cached_input_tokens"] += b.cached_input_tokens
        totals["output_tokens"] += b.output_tokens
        totals["reasoning_tokens"] += b.reasoning_tokens
        totals["total_tokens"] += b.total_tokens
        if b.priced and b.cost_usd is not None:
            totals["cost_usd"] += b.cost_usd
            totals["priced_subtasks"] += 1
        else:
            totals["unpriced_subtasks"] += 1
            totals["unpriced_models"].add(b.model)
    totals["cost_usd"] = round(totals["cost_usd"], 6)
    totals["unpriced_models"] = sorted(totals["unpriced_models"])
    return totals
