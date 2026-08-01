# AI Trading Agent untuk CEX (USDT-M Futures)

Bot trading otomatis multi-strategi untuk Binance / Bybit / OKX futures. Satu kode untuk semua exchange berkat [ccxt](https://github.com/ccxt/ccxt).

## Fitur

- **3 exchange**: Binance, Bybit, OKX (USDT-M linear futures) + mode testnet.
- **Multi-strategi**:
  - `momentum` — EMA cross (fast/slow) + filter RSI sungguhan + cooldown re-entry.
  - `ai_signal` — LLM (OpenAI-compatible: OpenAI, DeepSeek, Groq, dll.) baca data teknikal + news → LONG/SHORT/NEUTRAL + confidence. **Non-blocking** (jalan di thread, tick tidak tertahan) + cache per-candle + rate limit + sanitasi prompt-injection.
  - `grid` — grid trading limit order dengan **pairing buy→sell** (tanpa naked short), re-center saat harga drift, hormati `max_grid_orders`.
- **Filter tren multi-timeframe** — sinyal 15m hanya dieksekusi jika searah tren 1h (EMA 21/50), TTL cache per timeframe.
- **Manajemen risiko**:
  - Ukuran posisi % + **sizing adaptif volatilitas** (ATR-based, 0.5–2×).
  - **SL/TP berbasis ATR** (fallback ke % tetap).
  - Batas exposure total + pending order dihitung, max open positions.
  - **Trailing stop** + **daily loss limit** (halt otomatis + close semua).
  - **Filter biaya/spread** (`min_fee_tolerance_pct`) — skip bila spread+fee melebihi toleransi.
- **Penutupan posisi otomatis**: trailing, sinyal reversal, SL/TP, daily loss.
- **Koordinasi 1 posisi per simbol** — momentum & AI tidak buka posisi dobel di simbol yang sama (opsi `one_position_per_symbol`).
- **Persistensi & metrik** — `TradeStore` SQLite: riwayat trade, win rate, profit factor, state trailing (survive restart).
- **Backtest modul** (`backtest.py`) — replay strategi momentum, hitung win rate/PF/drawdown.
- **Hemat API call** — 1 `fetch_balance`/tick, batch tickers, OHLCV paralel (ThreadPool).
- **Notifikasi** via Telegram (opsional): perintah `/status`, `/positions`, `/balance`, `/metrics`, `/trend`, `/screen` (screening koin), `/instruksi` (panduan), + **laporan harian otomatis** (bisa diatur jannya via `reporting.daily_hour`).
- **Screening koin** — scan semua market USDT-M, sortir berdasarkan volume, tampilkan tren (EMA9/21/50) + RSI + rasio volume (konfigurasi di `screener`).

## Setup

```bash
pip install -r requirements.txt
Copy-Item .env.example .env   # isi API key
```

Buat API key futures di exchange masing-masing (untuk Binance/Bybit beri akses **futures + enable testnet**; untuk OKX beri izin trade & pasphrase).

## Konfigurasi cepat (`config.json`)

```jsonc
{
  "exchange": { "name": "binance", "testnet": true },
  "symbols": ["BTC/USDT:USDT"],
  "timeframe": "15m",
  "trend_filter": { "enabled": true, "timeframe": "1h", "ema_fast": 21, "ema_slow": 50 },
  "risk": {
    "leverage": 3,
    "max_position_pct": 20,
    "stop_loss_pct": 1.0,
    "take_profit_pct": 2.0,
    "trailing_stop_pct": 1.5,
    "daily_loss_limit_pct": 5.0,
    "min_fee_tolerance_pct": 0.15,
    "use_atr_stops": true,
    "adaptive_sizing": true
  },
  "strategies": {
    "momentum":  { "enabled": true },
    "ai_signal": { "enabled": false },
    "grid":      { "enabled": false }
  },
  "telegram": { "enabled": false, "bot_token": "", "chat_id": "" },
  "screener": { "timeframe": "1h", "max_coins": 10, "min_volume_usdt": 0 },
  "reporting": { "daily_hour": 8 }
}
```

Aktifkan strategi dengan set `enabled: true`. Isi `.env` sesuai API key exchange & AI yang dipakai.

Untuk notifikasi Telegram: set `telegram.enabled: true`, isi `bot_token` & `chat_id` di `config.json` atau `.env`. Laporan harian dikirim tiap jam `reporting.daily_hour` (default 08:00).

## Menjalankan

```bash
python main.py --check      # cek koneksi & saldo dulu
python main.py              # jalankan agent

python backtest.py --symbol BTC/USDT:USDT --timeframe 15m --days 90   # backtest momentum
python backtest.py --symbol BTC/USDT:USDT --mainnet                   # data historis panjang (mainnet)
```

## Struktur

```
agent/
  bot.py                 # main loop & manajemen posisi
  core/
    exchange.py          # ccxt wrapper (sync + testnet)
    risk.py              # sizing, exposure, SL/TP/ATR, fee filter, daily loss
    trend.py             # filter tren multi-timeframe (EMA)
    utils.py             # EMA, RSI, ATR, logger
  strategies/
    momentum.py          # EMA cross + RSI filter + cooldown
    ai_signal.py         # LLM signal (thread, cache, sanitize)
    grid.py              # grid trading (pairing + re-center)
  execution/
    order.py             # open, SL/TP (multi-exchange), close_all
    notifier.py          # Telegram + log
  data/
    storage.py           # CandleStore (memory/SQLite)
    trades.py            # TradeStore (riwayat trade + state)
backtest.py              # backtest strategi
main.py                  # entry point (--check, --config)
config.json              # konfigurasi utama
```

## Keamanan

- Selalu mulai dengan `"testnet": true`.
- Jangan taruh API key langsung di repo — pakai `.env` (sudah di-`gitignore`).
- Gunakan leverage rendah & limit `max_position_pct` saat uji nyata.
- Backtest dulu (`backtest.py`) sebelum strategi baru dipakai live.
- Bot ini bukan saran keuangan; uji di testnet dulu sebelum dana sungguhan.
