#!/usr/bin/env bash
# =============================================================================
# replicate.sh - Replikasi setup Trading Agent ke perangkat baru
# Semua kode & konfigurasi sudah ada di repo (branch main), jadi cukup clone.
#
# Cara pakai (di perangkat BARU):
#   bash replicate.sh
#
# Opsional:
#   REPO_URL=https://github.com/hdyuda5-spec/trading-agent.git bash replicate.sh
#   DIR=/opt/trading-agent bash replicate.sh
#   COMMIT=<hash> bash replicate.sh   # mis. pin ke versi tertentu
# =============================================================================
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/hdyuda5-spec/trading-agent.git}"
DIR="${DIR:-$HOME/trading-agent}"
COMMIT="${COMMIT:-origin/main}"

echo "==> [1/5] Ambil kode sumber dari $REPO_URL"
if [ -d "$DIR/.git" ]; then
  echo "    Folder $DIR sudah ada, pakai yang ada."
else
  git clone "$REPO_URL" "$DIR"
fi
cd "$DIR"
git fetch origin --quiet
git checkout main --quiet
git reset --hard "$COMMIT"
git status --short
echo "    Repo di commit: $(git rev-parse --short HEAD)"

echo "==> [2/5] Virtual env + dependensi"
python3 -m venv .venv
./.venv/bin/pip install --quiet --upgrade pip
./.venv/bin/pip install --quiet -r requirements.txt

echo "==> [3/5] File .env (ISI API KEY & TOKEN, lalu lanjutkan)"
if [ ! -f .env ]; then
  cp .env.example .env
  echo "    Dibuat .env dari .env.example."
  echo "    >>> ISI SEKARANG: nano .env  (BINANCE_API_KEY/SECRET, AI_*, TELEGRAM_*)"
  read -r -p "    Sudah diisi? tekan ENTER untuk lanjut... " _
else
  echo "    .env sudah ada."
fi

echo "==> [4/5] Pastikan konfigurasi kunci (SL:TP 1:2, leverage 5x, AI 5 mnt)"
./.venv/bin/python - <<'PY'
import json
p = "config.json"
c = json.load(open(p))
c["risk"]["leverage"] = 5
c["risk"]["max_leverage"] = 5
c["risk"]["atr_stop_mult"] = 1.5
c["risk"]["atr_tp_mult"] = 3.0          # TP = 2 x SL -> rasio 1:2
c["strategies"]["ai_signal"]["min_interval_seconds"] = 300  # hemat token
json.dump(c, open(p, "w"), indent=2)
print("    config.json siap: leverage=5x, SL:TP=1:2, AI=300s")
PY

echo "==> [5/5] Jalankan via pm2 (satu-satunya supervisor)"
if command -v pm2 >/dev/null 2>&1; then
  pm2 start main.py --name trading-agent --interpreter ./.venv/bin/python --update-env
  pm2 save
  echo "    OK. Bot berjalan via pm2. Monitor: pm2 logs trading-agent"
else
  echo "    pm2 belum terpasang. Pasang: npm install -g pm2"
fi

echo ""
echo "================================================================"
echo "SELESAI. Cek di Telegram: ketik /dashboard (atau tap tombol menu)."
echo "Jika bot tidak membalas, cek: tail -f logs/agent.log"
echo "================================================================"
