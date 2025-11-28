這個專案是一個基於 Python `asyncio` 和 `websockets` 實現的 Gate.io 期貨網格交易機器人。它設計為無 GUI 的命令行應用程式，適合在雲端伺服器 (如 VPS) 上穩定運行，透過 WebSocket 實時獲取數據並執行交易。

## ⚠️ 免責聲明

**本程式碼僅供學習和研究用途。** 交易有高風險，可能導致資金損失。使用本機器人進行真實交易的**所有風險由您自行承擔**，作者對任何損失不負任何責任。在使用前，請確保您完全理解其交易邏輯和風險。

## ✨ 特點

* **實時數據:** 透過 Gate.io WebSocket 連接，實時獲取 Ticker、持倉、訂單和餘額更新。
* **網格策略:** 實現基礎網格補倉 (開倉) 和止盈 (平倉) 機制。
* **持倉鎖定/裝死模式:** 當單邊持倉超過設定閾值 (`POSITION_THRESHOLD`) 時，機器人將進入「裝死」模式，只掛單止盈，不再繼續開倉，以控制風險。
* **雙向減倉:** 雙向持倉都超過一定閾值時，會進行部分減倉操作。
* **CCXT 整合:** 使用 `ccxt` 庫處理 REST API 請求，確保交易的可靠性。

## ⚙️ 環境與安裝

### 1. 依賴項

您的專案依賴於以下模組：

**`requirements.txt`**
```text
websockets
ccxt
2. 安裝請確保您安裝了 Python 3.8 或更高版本。Bash# 建議建立並啟用虛擬環境
python -m venv venv
source venv/bin/activate  # Linux/macOS
# .\venv\Scripts\activate # Windows

# 安裝所有依賴
pip install -r requirements.txt
🛠️ 配置教學所有配置參數都集中在 as.py 檔案的頂部，請替換您的 API 資訊並根據您的交易計畫修改參數：Python# ==================== 配置 (as.py 頂部) ====================
API_KEY = "YOUR_API_KEY"         # 替換為你的 API Key
API_SECRET = "YOUR_API_SECRET"   # 替換為你的 API Secret
COIN_NAME = "XRP"                # 交易幣種 (例如: BTC, ETH, XRP)
GRID_SPACING = 0.006             # 補倉間距 (0.6%)
TAKE_PROFIT_SPACING = 0.004      # 止盈間距 (0.4%)
INITIAL_QUANTITY = 1             # 初始交易數量 (張數)
LEVERAGE = 20                    # 槓桿倍數 (需在 Gate.io 後台設定好)
POSITION_THRESHOLD = 500         # 鎖倉閾值：超過此持倉量進入「裝死」模式
POSITION_LIMIT = 100             # 持倉數量閾值：超過此限制，下次開倉數量翻倍 (用於調整 long/short_initial_quantity)
ORDER_COOLDOWN_TIME = 60         # 鎖倉後的反向掛單冷卻時間（秒）
SYNC_TIME = 3                    # 同步時間（秒）：每隔多久使用 REST API 重新檢查持倉/掛單
ORDER_FIRST_TIME = 1             # 首單間隔時間：防止重複初始化開倉單
# ==========================================================
▶️ 運行與日誌運行指令在配置完成後，請確保虛擬環境已啟用，然後執行主程式：Bashpython as.py
🗃️ 日誌文件機器人將運行和錯誤訊息同時寫入到控制台和日誌文件中，方便追蹤：日誌路徑: log/as.log🧠 核心邏輯詳解1. 價格計算機器人基於市場最新價格或中間價來計算掛單價格。方向類型計算公式說明多頭 (Long)止盈價Price * (1 + TAKE_PROFIT_SPACING)賣出平多多頭 (Long)補倉價Price * (1 - GRID_SPACING)買入開多空頭 (Short)止盈價Price * (1 - TAKE_PROFIT_SPACING)買入平空空頭 (Short)補倉價Price * (1 + GRID_SPACING)賣出開空2. 交易流程與風險控制🚀 正常網格交易無持倉時: 嘗試在當前中間價附近掛出初始開倉單。有持倉時: 撤銷舊單，並在計算出的 補倉價 掛上開倉/補倉單 (reduce_only: False)，在 止盈價 掛上止盈單 (reduce_only: True)。🥶 鎖倉/裝死模式當單邊持倉量超過 POSITION_THRESHOLD 時，機器人進入裝死模式：停止開倉: 不再掛出新的補倉單。單邊止盈: 只會在一個基於倉位比率調整的較遠價格掛出減倉止盈單。冷卻時間: 平倉後，該方向需等待 ORDER_COOLDOWN_TIME 後才考慮重新開倉。⚖️ 雙向減倉若多空倉位都達到 POSITION_THRESHOLD * 0.8，機器人將執行一個 POSITION_THRESHOLD * 0.1 的市價減倉操作，以釋放保證金並平衡風險。