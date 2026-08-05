# RESEARCH_SUMMARY — trading-agent

Tanggal penutupan: 5 Agustus 2026 (WIB). Dokumen ini merangkum pekerjaan validasi
strategi trading untuk repo `trading-agent`. Seluruh angka memakai metodologi yang
sudah dikoreksi (lintasan kontinu + partisi out-of-sample, funding + slippage + fee).

---

## 1. Bug / Perbaikan Teknis yang Sudah Diterapkan (tetap dipakai)

### a. Minimum stop distance floor + ATR-strict guard — `agent/core/risk.py`
- `build_stop_loss()` / `build_take_profit()` memakai guard **ATR-strict**:
  level hanya dibangun jika `is_valid_atr(atr)` (ATR finite & > 0), jarak dari
  `atr_stop_mult` (1.0) dan `atr_tp_mult` (2.5) dari risk config.
- Floor jarak minimum persen (`min_stop_distance_pct`) yang dulu dipakai **dihapus**
  karena bermasalah; diganti validasi ATR yang ketat. Logika yang sama dipakai
  konsisten di `agent/execution/order.py` (live) dan `backtest.py`.

### b. Order type limit (postOnly, best bid/ask) — `agent/execution/order.py`
- `open_position()` kini mengirim **limit order postOnly** di harga best book
  (bid untuk LONG, ask untuk SHORT), bukan market order.
- TTL entry `entry_ttl_seconds=180`, polling fill, batalkan (`cancel_pending`) bila
  tidak terisi dalam waktu; verifikasi fill via `fetch_order` sebelum lanjut.

### c. Fix duplikat log — `agent/execution/notifier.py`
- Setiap method notifikasi kini menulis log **sekali** (`logger.info`/`logger.warning`)
  lalu kirim Telegram bila dikonfigurasi — tidak ada duplikasi baris log lagi.

### d. Perbaikan metodologi `backtest.py`
- **Partisi train/test dari 1 lintasan kontinu** (bukan 3 run terisolasi dari nol).
  Dulu `full ≠ train + test` (contoh BTC/limit/ADX-ON: full=55 vs train+test=51;
  25 trade hanya muncul di satu run). Kini: 1 kali `run()` → partisi trade by
  `entry_bar` → `train + test == full` (terverifikasi 52=35+17, 767=767).
- **Funding rate mainnet** disimulasikan (ccxt `fetch_funding_rate_history`),
  **slippage** default 0.05%, **fee** market 0.05% / limit 0.02%.
- **Expectancy** dihitung eksplisit: `WR×avgW + (1−WR)×avgL` (kolom
  `expectancy_pct`), plus win rate, profit factor, avg win/loss, net, max DD.

---

## 2. Hasil Riset Validasi Strategi

Semua: timeframe 15m, mainnet, 11 simbol, limit order, 20 seed simulasi fill-rate,
split 70/30, segmen **TEST** (out-of-sample). Expectancy per trade.

### a. Momentum EMA9/21 crossover — 90 hari
- BTC/limit/ADX-ON, **setelah koreksi partisi**: test **−0.109%/trade**
  (sebelum koreksi metodologi: −0.253% dari run terisolasi yang bias).
- Hasil lengkap 11 simbol (market vs limit, ADX on/off): `backtest_results/backtest_v2_full.txt`.

### b. Momentum EMA9/21 POLOS (tanpa filter) — 365 hari
- Agregat test: **−0.159%/trade** (90d: −0.156%). Hampir identik →
  **90 hari sudah representatif; bukan soal kurang data.**
- 5/11 simbol punya data 365d penuh (BTC, ETH, SOL, ZEC, HYPE); XAU 237d,
  CL 126d, SNDK/MU 120d, **KORU 44d & SKHY 26d (listing baru, tidak representatif)**.

### c. Filter tambahan MEMPERBURUK hasil
- Versi polos 90d: **−0.156%** vs versi dgn filter trend+RSI+ADX (ADX-off) 90d:
  **−0.215%**. Filter yang dipakai live selama ini menurunkan performa.

### d. Limit order konsisten lebih baik dari market
- Menang di **20/22 skenario** (90d filtered), gain ~**+0.10pp**/trade.
- Berlaku juga di versi polos & mean-reversion (market test −0.28…−0.31%
  vs limit −0.16…−0.19%). **Keduanya tetap negatif.**

### e. Mean-reversion (Bollinger BB20/2 + RSI14) — 365 hari
- Modul baru: `agent/strategies/mean_reversion.py` (entry LONG di bawah lower band
  & RSI<30; SHORT di atas upper band & RSI>70; exit kembali ke middle band atau
  TP ATR; SL ATR-strict). Terdaftar di `STRATEGY_MAP`, config `enabled: false`.
- Agregat test: **−0.187%/trade** (sedikit lebih buruk dari momentum −0.159%),
  tapi menang 6/11 simbol (SOL +0.006 positif; ZEC +0.103; SKHY +0.349 lebih baik).
- WR tinggi (30–50%) tapi avg win kecil vs avg loss besar (PF 0.42–1.02) —
  profil "menang kecil, kalah besar sekali-sekali".

### Tabel final — test expectancy limit (%/trade), mean 20 seed

| Simbol  | data | Momentum polos | Mean-reversion |
|---------|------|---------------|----------------|
| BTC     | 365d | −0.098         | −0.078          |
| ETH     | 365d | −0.067         | −0.109          |
| SOL     | 365d | −0.074         | +0.006          |
| SNDK    | 120d | +0.013         | −0.313          |
| KORU*   | 44d  | +0.172         | −0.306          |
| XAU     | 237d | −0.086         | −0.130          |
| CL      | 126d | −0.151         | −0.178          |
| MU      | 120d | −0.274         | −0.235          |
| ZEC     | 365d | −0.172         | −0.068          |
| SKHY*   | 26d  | −0.851         | −0.502          |
| HYPE    | 365d | −0.165         | −0.146          |
| **Agregat** |    | **−0.1593**   | **−0.1872**     |

\* = listing baru, data <90 hari, tidak representatif — harus dikecualikan.

### f. Kesimpulan
Kedua pendekatan rule-based sederhana (EMA crossover & mean-reversion) **TIDAK
menunjukkan edge yang valid secara out-of-sample** pada 11 simbol yang diuji,
baik di 90 maupun 365 hari. Limit order secara konsisten unggul dari market order,
tapi gap itu tidak cukup untuk membuat expectancy positif.

---

## 3. Status Akhir Bot Live

### Config saat ini (`config.json`) — nilai AKTUAL
- `execution.order_type` = **limit**; `entry_ttl_seconds` = 180; `poll_interval_seconds` = 60.
- `risk.max_open_positions` = **2**; `risk.max_position_pct` = **10**; `risk.leverage` = **5**.
- Strategi entry: **momentum: enabled** (diubah ke disabled saat pause),
  **ai_signal: enabled** (diubah ke disabled saat pause),
  **grid: disabled**, **mean_reversion: disabled** (baru, off dulu).
- `screener.auto_trade` = true (diubah ke **false** saat pause); `screener.enabled` tetap true
  untuk monitoring sinyal; pattern/smart_money/whale tetap enabled (monitoring).

### Proses
- PM2 **id 1 `trading-agent`** — PID 2434351, status **online**, restart 25×,
  jalan via `main.py`. Bot tetap online untuk monitoring/notifikasi.
- Catatan penting: config dibaca **sekali saat startup** (`self.config = config`,
  `agent/bot.py`). Perubahan `config.json` baru aktif setelah `pm2 restart trading-agent`.
- Status pause: **auto_trade=false, momentum/ai_signal disabled → tidak ada entry
  baru** selama masa istirahat; proses tetap dipantau.

---

## 4. Arsip Hasil Backtest
Disimpan permanen di repo: **`backtest_results/`** (JSON + log, log di-rename .txt
karena `*.log` di-ignore git). Berisi: `backtest_compare_v2.json`, `backtest_plain_90.json`,
`backtest_plain_365.json`, `backtest_mr_365.json`, seluruh log perbandingan, artefak
audit fill-rate Bagian-A (signals/fills), dan skrip debugging partisi (backtest_trace,
debug_split*, probe_depth). Commit referensi: lihat `git log` setelah commit penutupan ini.
