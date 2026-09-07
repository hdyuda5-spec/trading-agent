# AI Trading Agent untuk CEX (USDT-M Futures)

Bot trading otomatis multi-strategi untuk Binance / Bybit / OKX futures. Satu kode untuk semua exchange berkat [ccxt](https://github.com/ccxt/ccxt).

## Fitur

- **3 exchange**: Binance, Bybit, OKX (USDT-M linear futures) + mode testnet.
- **Multi-strategi**:
  - `momentum` — EMA cross (fast/slow) + filter RSI sungguhan + cooldown re-entry.
  - `ai_signal` — **opsional / offline-first** (tidak wajib): hanya aktif jika
    provider LLM eksternal dikonfigurasi eksplisit (`base_url` + `model` + key).
    Tanpa provider, strategi ini no-op — pipeline core berjalan deterministik.
    Ia membaca data teknikal + news dan memberi **advisory saja** (structured
    JSON: `market_summary`, `trend`, `macro`, `news`, `risk`, `confidence`) —
    **LLM tidak pernah mengeluarkan BUY/SELL**; arah trade diputuskan oleh
    Decision Engine (advisory ikut dihitung sebagai vote berbobot `ai_advisory`).
    **Tanpa API AI eksternal**, bot tetap 100% berfungsi.
  - `grid` — grid trading limit order dengan **pairing buy→sell** (tanpa naked short), re-center saat harga drift, hormati `max_grid_orders`.
- **Filter tren multi-timeframe** — sinyal 15m hanya dieksekusi jika searah tren 1h (EMA 21/50), TTL cache per timeframe.
- **Manajemen risiko**:
  - Ukuran posisi % + **sizing adaptif volatilitas** (ATR-based, 0.5–2×).
  - **SL/TP berbasis ATR** (fallback ke % tetap).
  - Batas exposure total + pending order dihitung, max open positions.
  - **Trailing stop** + **daily loss limit** (halt otomatis + close semua, baseline per-hari di-persist supaya tetap aktif walau restart).
  - **Filter biaya/spread** (`min_fee_tolerance_pct`) — skip bila spread+fee melebihi toleransi.
  - **Minimal balance guard** (`risk.min_equity_usdt`, default 20) — kalau saldo di bawah ambang, trading dipause + alert Telegram, lanjut otomatis saat saldo pulih.
- **Eksekusi order yang aman**:
  - Entry default **market** (tidak ada order menggantung). Bila memakai limit: harga live dari ticker (bukan close candle) + verifikasi fill sebelum pasang SL/TP + auto-cancel order yang tak terisi dalam `execution.entry_ttl_seconds` (default 180 s).
  - Dukungan **hedge mode**: set `execution.position_mode: "hedge"` agar SL/TP/close_all menyertakan `positionSide`.
- **Penutupan posisi otomatis**: trailing, sinyal reversal, SL/TP, daily loss.
- **Koordinasi 1 posisi per simbol** — momentum & AI tidak buka posisi dobel di simbol yang sama (opsi `one_position_per_symbol`).
- **Persistensi & metrik** — `TradeStore` SQLite: riwayat trade, win rate, profit factor, state trailing (survive restart).
- **Backtest modul** (`backtest.py`) — replay strategi momentum, hitung win rate/PF/drawdown.
- **Hemat API call** — 1 `fetch_balance`/tick, batch tickers, OHLCV paralel (ThreadPool).
- **Notifikasi** via Telegram (opsional): perintah `/status`, `/positions`, `/balance`, `/metrics`, `/trend`, `/screen` (screening koin), `/instruksi` (panduan), + **laporan harian otomatis** (bisa diatur jannya via `reporting.daily_hour`).
- **Screening koin** — scan semua market USDT-M, sortir berdasarkan volume, tampilkan tren (EMA9/21/50) + RSI + rasio volume (konfigurasi di `screener`).
- **Decision Engine (V2)** — pipeline explainable `HardVeto → Regime → Evidence → Verdict (PASS/WAIT/REJECT)` dengan `reason_code`, `TradeTicket` ber-idempotency, telemetri signal funnel, missed-trade journal, dan trade reviewer. Semua keputusan + tiket dipersist ke `TradeStore` v2.
- **Paper trading (default)** — `trading.mode: "paper"` menjalankan seluruh stack terhadap harga pasar **sungguhan** tetapi dengan akun tentur (simulasi balance/posisi). Pergi live = `trading.mode:"live"` + `screener.auto_trade:true` (saat ini `false`).
- **Config fail-fast** — validasi `config.json` sebelum start (`agent.core.config_validate`), secret tidak pernah masuk log (`agent.core.secrets`).
- **Perintah Telegram baru**: `/funnel`, `/reject`, `/decisions`, `/missed`, `/paper`.

Dokumentasi build baru: `docs/DECISION_ENGINE.md`, `docs/RISK_ENGINE.md`, `docs/EXECUTION.md`, `docs/LIQUIDITY_ENGINE.md`, `docs/PAPER_TRADING.md`, `docs/OPERATIONS.md`, `docs/ARCHITECTURE_AUDIT.md`.

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
  bot.py                 # orchestrator (event-driven), wiring services
  core/
    exchange.py          # ccxt wrapper (sync + testnet)
    risk.py              # policy engine: sizing, exposure, SL/TP, RR, daily loss
    config_validate.py   # fail-fast config validation
    timestamps.py        # unit-agnostic order age (ms/s/us)
    secrets.py           # redaksi secret untuk log
    trend.py             # filter tren multi-timeframe (EMA)
    utils.py             # EMA, RSI, ATR, logger
  decision/
    engine.py            # UnifiedDecisionEngine (veto+regime+evidence+verdict)
    types.py             # DecisionVerdict, EvidenceItem, Rejection
    weights.py           # weights/thresholds dari config
    regime.py            # MarketRegimeEngine
    evidence.py          # HardVeto + EvidenceCollector
    ticket.py            # TradeTicket
    telemetry.py         # SignalFunnel
    reviewer.py          # TradeReviewer + MissedTradeJournal
  strategies/
    momentum.py          # EMA cross + RSI filter + cooldown
    ai_signal.py         # LLM signal (thread, cache, sanitize)
    grid.py              # grid trading (pairing + re-center)
  execution/
    order.py             # open, SL/TP, close_all, stale-order sweep
    paper.py             # PaperExchange (mode paper)
    notifier.py          # Telegram + log
    telegram_ctl.py      # /funnel /reject /decisions /missed /paper ...
  data/
    trades.py            # TradeStore v2 (trades + decisions/tickets/funnel/journal)
  services/
    signals.py           # live entry: filters -> decision gate -> ticket
    screener_service.py  # auto-screen + auto-trade candidate (decision-gated)
    execution.py         # order lifecycle + funnel persistence
    candle_store.py      # bounded candle cache (800 bars)
    portfolio.py, reporting.py, whale_service.py, ...
backtest.py              # backtest strategi
main.py                  # entry point (--check --config; validate fail-fast)
config.json              # konfigurasi utama (trading.mode=paper default)
ecosystem.config.js      # pm2 (repo-local logs)
```

## Auto-restart (live server)

Bot ini **tidak** restart sendiri — pakai systemd atau pm2 agar tetap jalan saat crash/putus internet.

### systemd (VPS Linux)

```bash
sudo cp deploy/trading-agent.service /etc/systemd/system/
sudo mkdir -p /opt/trading-agent
# salin kode + .env ke /opt/trading-agent, sesuaikan User/Python path di .service
sudo systemctl daemon-reload
sudo systemctl enable --now trading-agent
sudo systemctl status trading-agent
```

### pm2

```bash
npm install -g pm2
mkdir -p /opt/trading-agent /var/log/trading-agent
# salin kode + .env ke /opt/trading-agent
cd /opt/trading-agent && pm2 start deploy/ecosystem.config.js
pm2 save && pm2 startup
```

## Keamanan

- Selalu mulai dengan `"testnet": true`.
- Jangan taruh API key langsung di repo — pakai `.env` (sudah di-`gitignore`).
- Gunakan leverage rendah & limit `max_position_pct` saat uji nyata.
- Backtest dulu (`backtest.py`) sebelum strategi baru dipakai live.
- Pastikan akun dalam **one-way mode** (bukan hedge mode) atau set `execution.position_mode: "hedge"` agar SL/TP & close_all jalan.
- Bot ini bukan saran keuangan; uji di testnet dulu sebelum dana sungguhan.
