import os
import threading

import requests


class Reflector:
    """Optional post-trade LLM reflection (offline by default).

    Not part of the core runtime. It only activates when an external LLM
    provider is explicitly configured (``AI_API_KEY`` env or
    ``strategies.ai_signal.base_url`` + ``model``). Without a provider it is a
    pure no-op: no threads, no network, no effect on the trade pipeline.

    When active, it upgrades the deterministic lesson with an async,
    outcome-aware analysis (port of TradingAgents' Phase B reflection).
    """

    def __init__(self, config, strat_cfg, notifier, store):
        self.config = config
        self.strat_cfg = strat_cfg
        self.notifier = notifier
        self.store = store
        self.api_key = os.getenv("AI_API_KEY", strat_cfg.get("api_key", ""))
        # No default endpoint: reflection stays offline unless an external
        # provider is explicitly configured.
        self.base_url = strat_cfg.get("base_url", "")
        self.model = strat_cfg.get("model", "")
        ref_cfg = strat_cfg.get("reflection", {})
        self.enabled = bool(self.api_key and self.base_url and self.model) and ref_cfg.get("enabled", True)
        self.max_workers = max(1, int(ref_cfg.get("max_workers", 1)))
        self.lesson_max_chars = int(ref_cfg.get("lesson_max_chars", 500))
        self._lock = threading.Lock()
        self._threads = {}

    def reflect_async(self, exp_id, symbol, side, strategy, setup, pnl, pnl_pct, reason):
        if not self.enabled:
            return
        thread = threading.Thread(
            target=self._reflect,
            args=(exp_id, symbol, side, strategy, setup, pnl, pnl_pct, reason),
            daemon=True,
        )
        with self._lock:
            active = sum(1 for t in self._threads.values() if t.is_alive())
            if active >= self.max_workers:
                return
            self._threads[exp_id] = thread
        thread.start()

    def _reflect(self, exp_id, symbol, side, strategy, setup, pnl, pnl_pct, reason):
        try:
            prompt = self._prompt(symbol, side, strategy, setup, pnl, pnl_pct, reason)
            lesson = self._ask_ai(prompt)
            if lesson:
                self.store.update_experience(exp_id, lesson[: self.lesson_max_chars])
        finally:
            with self._lock:
                self._threads.pop(exp_id, None)

    def _prompt(self, symbol, side, strategy, setup, pnl, pnl_pct, reason):
        meta = setup.get("metadata") or {}
        rsi = meta.get("rsi")
        rsi_txt = f" RSI saat entry: {rsi:.1f}." if rsi is not None else ""
        bull = meta.get("bull_conviction")
        bear = meta.get("bear_conviction")
        debate_txt = ""
        if bull is not None and bear is not None:
            debate_txt = f" Debate AI saat entry: bull conviction={bull:.0f}, bear conviction={bear:.0f}."
        if side == "long":
            side_txt = "LONG"
        elif side == "short":
            side_txt = "SHORT"
        else:
            side_txt = str(side).upper()
        return (
            f"Kamu adalah analis trading kripto futures yang mengevaluasi keputusan trade bot sendiri "
            f"yang sudah selesai (outcome diketahui).\n\n"
            f"Trade: {symbol} {side_txt} via {strategy}. Exit reason: {reason}."
            f"{rsi_txt}{debate_txt} PnL: {pnl:+.2f} USDT ({pnl_pct:+.1f}%).\n\n"
            f"Setup yang direkam: {setup.get('setup') if isinstance(setup.get('setup'), str) else setup}\n\n"
            f"Tulis refleksi TEPAT 2-4 kalimat, tanpa bullet/header/markdown, dalam urutan:\n"
            f"1. Apakah arah prediksi benar? (sebut angka pnl)\n"
            f"2. Bagian mana dari tesis yang benar/gagal?\n"
            f"3. Satu pelajaran konkret untuk trade serupa berikutnya.\n\n"
            f"Refleksi ini akan dibaca ulang oleh bot untuk trade berikutnya, jadi harus padat dan actionable."
        )

    def _ask_ai(self, prompt):
        try:
            resp = requests.post(
                f"{self.base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self.model,
                    "messages": [
                        {
                            "role": "system",
                            "content": (
                                "Kamu adalah analis kripto yang menulis pelajaran trading singkat. "
                                "Keluarkan hanya teks refleksi 2-4 kalimat, tanpa format."
                            ),
                        },
                        {"role": "user", "content": prompt},
                    ],
                    "temperature": 0.2,
                },
                timeout=30,
            )
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"].strip()
        except Exception as e:
            self.notifier.alert("Reflection request failed", str(e))
            return None
