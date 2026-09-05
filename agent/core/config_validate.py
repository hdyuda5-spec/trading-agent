"""Config validation — fail fast, fail explainable (Phase 27).

Runs before anything else starts. Invalid config must never silently produce
half-configured behavior; each violation raises ``ConfigError`` with a clear,
Indonesian-language message identifying the exact key.
"""

from typing import Dict, List, Optional, Tuple

VALID_EXCHANGES = {"binance"}
VALID_MODES = {"paper", "live"}
VALID_ORDER_TYPES = {"limit", "market"}
POSITIVE_KEYS = (
    "min_equity_usdt",
    "daily_loss_limit_pct",
    "max_position_pct",
    "max_open_positions",
    "leverage",
    "max_leverage",
    "fixed_notional_usdt",
    "risk_per_trade_pct",
    "retry_attempts",
    "entry_ttl_seconds",
    "poll_interval_seconds",
    "order_ttl_seconds",
    "auto_interval_minutes",
    "min_fee_tolerance_pct",
)


class ConfigError(ValueError):
    pass


def validate_config(config: dict) -> List[str]:
    """Return list of problems (empty == valid). Never raises."""
    problems: List[str] = []
    add = problems.append

    ex = config.get("exchange", {})
    if ex.get("name") not in VALID_EXCHANGES:
        add(f"exchange.name harus salah satu dari {VALID_EXCHANGES}, dapat {ex.get('name')!r}")
    testnet = ex.get("testnet", True)
    if not isinstance(testnet, bool):
        add("exchange.testnet harus boolean")

    if not config.get("symbols"):
        add("symbols tidak boleh kosong")

    trading = config.get("trading", {}) or {}
    mode = str(trading.get("mode", "paper")).lower()
    if mode not in VALID_MODES:
        add(f"trading.mode harus {VALID_MODES}, dapat {mode!r}")

    exec_cfg = config.get("execution", {}) or {}
    if exec_cfg.get("order_type") not in VALID_ORDER_TYPES:
        add(f"execution.order_type harus {VALID_ORDER_TYPES}, dapat {exec_cfg.get('order_type')!r}")

    risk = config.get("risk", {}) or {}
    for key in POSITIVE_KEYS:
        if key in risk:
            try:
                val = float(risk[key])
            except (TypeError, ValueError):
                add(f"risk.{key} harus angka, dapat {risk[key]!r}")
                continue
            if val <= 0:
                add(f"risk.{key} harus > 0, dapat {val}")

    mag = (config.get("decision", {}) or {}).get("min_margin", 0.0)
    try:
        if float(mag) < 0:
            add("decision.min_margin tidak boleh negatif")
    except (TypeError, ValueError):
        add("decision.min_margin harus angka")

    screener = config.get("screener", {}) or {}
    if mode == "live" and screener.get("auto_trade", False):
        add("LIVE trading: screener.auto_trade=true AKTIF — jalankan dengan paper dulu "
            "dan konfirmasi eksplisit sebelum live.")
    return problems


def validate_or_raise(config: dict) -> None:
    problems = validate_config(config)
    if problems:
        raise ConfigError("Config tidak valid:\n  - " + "\n  - ".join(problems))