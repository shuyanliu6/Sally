"""
Signal registry loader.

Reads config/signals_registry.yaml to determine which signals are active,
their weights, and layer-level weights. The composite scoring functions in
macro.py, sector.py, stock.py, and aggregator.py call this module before
computing each signal.

Usage:
    from quantamental.signals import registry

    if registry.is_enabled("macro", "vix"):
        v_signal = score_vix(latest_vix)

    weight = registry.signal_weight("sector", "tsmc_revenue")   # → 1.0
    lw = registry.layer_weight("macro")                          # → 1.0
    active = registry.enabled_signals("stock")                   # → ["ema", "rsi", ...]

No caching — the YAML is tiny (~2 KB) and we want hot-reload on every pipeline
run so changes take effect without restarting the process.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_REGISTRY_PATH = Path(__file__).parent.parent / "config" / "signals_registry.yaml"


def load() -> dict:
    """Return the raw registry dict from YAML."""
    try:
        import yaml  # PyYAML
    except ImportError:
        logger.error(
            "PyYAML not installed — run `pip install PyYAML>=6.0`. "
            "Falling back to empty registry (all signals treated as enabled, weight=1.0)."
        )
        return {}

    if not _REGISTRY_PATH.exists():
        logger.warning(
            "signals_registry.yaml not found at %s — "
            "all signals treated as enabled with weight 1.0.",
            _REGISTRY_PATH,
        )
        return {}

    return yaml.safe_load(_REGISTRY_PATH.read_text()) or {}


def _signal_cfg(layer: str, signal: str) -> dict:
    """Return the config dict for one signal, or {} if not found."""
    return load().get(layer, {}).get("signals", {}).get(signal, {})


def is_enabled(layer: str, signal: str) -> bool:
    """Return True if the signal is active in the registry.

    Unknown signals (not listed) are treated as enabled so new signals
    can be wired into code before their YAML entry is added.
    """
    return bool(_signal_cfg(layer, signal).get("enabled", True))


def signal_weight(layer: str, signal: str) -> float:
    """Return the weight for a signal (default 1.0 if not specified)."""
    return float(_signal_cfg(layer, signal).get("weight", 1.0))


def layer_weight(layer: str) -> float:
    """Return the layer-level weight (default 1.0 if not specified)."""
    return float(load().get(layer, {}).get("layer_weight", 1.0))


def enabled_signals(layer: str) -> list[str]:
    """Return names of all enabled signals in a layer, in declaration order."""
    sigs = load().get(layer, {}).get("signals", {})
    return [name for name, cfg in sigs.items() if cfg.get("enabled", True)]


def describe(layer: str, signal: str) -> str:
    """Return the human-readable description for a signal."""
    return _signal_cfg(layer, signal).get("description", "")
