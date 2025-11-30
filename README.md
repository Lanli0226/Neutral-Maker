# Avellaneda-Stoikov (Guéant–Lehalle–Fernandez-Tapia) Market Making Bot for Gate.io

這是一個基於 **Avellaneda-Stoikov (Guéant–Lehalle–Fernandez-Tapia 閉式解) 做市商模型 (Market Making)** 的高頻交易機器人，專為 **Gate.io 合約交易 (USDT-Margined)** 設計。

與傳統網格不同，此機器人會根據當前的**持倉庫存 (Inventory)** 和**市場波動率 (Volatility)**，並透過**實時觀察市場微觀結構 (Microstructure)** 來動態估計市場流動性參數，進而調整買賣價格。其目標是在賺取買賣價差 (Spread) 的同時，將庫存風險降至最低。

## 📂 檔案結構

確保你的目錄中包含以下核心檔案：
1. **`bot.py`**: 基礎網格交易類別、Gate.io API/WebSocket 連接層，以及實時公共成交數據收集器。
2. **`avellaneda_bot.py`**: 包含 Guéant–Lehalle–Fernandez-Tapia (GLFT) 策略邏輯的主程序 (入口)，負責策略執行與參數動態校準。
3. **`avellaneda_utils.py`**: 包含 GLFT 模型相關的數學工具函數、歷史數據抓取、以及 $A, k$ 參數的校準邏輯。

## 🚀 快速開始

### 1. 安裝環境
確保你的電腦已安裝 Python 3.8 或更高版本。

安裝依賴庫：
```bash
pip install -r requirements.txt
```

### 2\. 配置 API Key

打開 `avellaneda_bot.py`，找到以下部分並填入你的 Gate.io API 資訊：

```python
# avellaneda_bot.py

API_KEY = "你的_API_KEY" 
API_SECRET = "你的_API_SECRET"
COIN_NAME = "XRP"      # 交易幣種 (例如 XRP, BTC, ETH)
INITIAL_QUANTITY = 1   # 每次下單的合約張數
LEVERAGE = 20          # 槓桿倍數
```

> ⚠️ **注意**：請確保 API Key 權限已開啟 **合約交易 (Futures)** 的讀寫權限。

### 3\. 啟動機器人

在終端機 (Terminal) 執行以下命令：

```bash
python avellaneda_bot.py
```

-----

## ⚙️ 策略參數詳解 (GLFT Parameters)

在 `avellaneda_bot.py` 中，你可以調整以下固定參數來改變機器人的行為風格。市場流動性參數 ($A, k$) 和波動率 ($\sigma$) 將由機器人**自動估算和實時校準**。

| 參數變數 | 建議值 | 說明 |
| :--- | :--- | :--- |
| **`AVE_GAMMA`** | `0.0001` | **風險厭惡係數 (Risk Aversion)**。<br>數值越大，機器人越討厭持倉。當有庫存時，它會更激進地降價拋售或提價回補，以盡快回到 0 持倉。此值應根據您的資金規模和可承受的風險來設定。 |
| **`CALIBRATION_INTERVAL`** | `60` | **實時校準間隔 (秒)**。<br>機器人每隔此時間長度，會重新根據收集到的實時成交數據校準 $A$ 和 $k$ 參數。設定過短可能增加計算開銷，過長則反應不夠及時。 |

## 📊 策略邏輯簡述

本機器人採用 Guéant–Lehalle–Fernandez-Tapia (GLFT) 閉式解模型，並增加了實時流動性參數校準機制：

1.  **市場參數初始估算**：
    *   機器人啟動時，會從 Gate.io API 獲取歷史 1 小時 K 線數據估算**波動率 ($\sigma$)**。
    *   同時，會獲取近期成交數據和 1 分鐘 K 線數據，使用回歸分析估算**訂單到達強度 ($A$)** 和**流動性衰減係數 ($k$)**。
    *   所有參數都將被標準化為一致的單位 (例如：每秒鐘的波動率，每秒鐘的訂單強度)，以確保數學計算的準確性。

2.  **實時數據收集**：
    *   `bot.py` 會通過 WebSocket 持續訂閱公共成交數據 (`futures.trades`)。
    *   每當有新的成交發生，機器人會立即計算該成交價格與當前最佳買賣價中間價的距離 ($\delta$)。這些 `(時間戳, $\delta$ 值)` 對將被存入一個環形緩衝區。

3.  **動態流動性校準**：
    *   在策略主循環中，`avellaneda_bot.py` 會定期 (例如每 60 秒) 提取緩衝區中的最新 $\delta$ 數據。
    *   利用這些實時數據，重新執行回歸分析，動態更新當前市場的 $A$ 和 $k$ 參數。
    *   這樣，機器人能夠「邊跑邊學」，實時適應市場流動性的變化。

4.  **最佳報價計算**：
    *   基於當前庫存量、市場價格、以及實時校準後的 $\sigma, A, k, \gamma$ 參數，模型會計算出動態的**公允價格 (Reserve Price)** 和**最優報價價差 (Optimal Spread)**。
    *   機器人會根據這些計算結果，智能地掛出買賣訂單，以實現庫存中性和捕獲價差的目標。

### GLFT 模型核心公式

**訂單流強度估計**：
$$ \lambda(\delta) = A e^{-k \delta} $$
其中：
- $\delta$：訂單距離中間價的價格距離。
- $A$：訂單到達強度（Orders/Sec），$k$：流動性衰減係數。

**GLFT 係數 $c_1, c_2$ 計算**：
$$ c_1 = \frac{1}{\gamma \Delta} \ln \left( 1 + \frac{\gamma \Delta}{k} \right) $$
$$ c_2 = \sqrt{\frac{\gamma}{2 A \Delta k} \left( 1 + \frac{\gamma \Delta}{k} \right)^{\frac{k}{\gamma \Delta} + 1}} $$
其中：
- $\gamma$：風險厭惡係數（單位為 1/貨幣單位）。
- $\Delta$：離散價格步長（在代碼中設為 1 Tick）。
- $A, k$：同上。

**最佳報價**：
$$ P_{bid} = P_{mid} - (c_1 + \frac{\Delta}{2} \sigma c_2 + q \sigma c_2) $$
$$ P_{ask} = P_{mid} + (c_1 + \frac{\Delta}{2} \sigma c_2 - q \sigma c_2) $$
其中：
- $P_{mid}$：當前市場中間價。
- $\sigma$：波動率（標準化為每秒）。
- $q$：當前庫存量。
- 其他符號同上。

## ⚠️ 風險提示 (Disclaimer)

  * **高頻撤單**：此策略會頻繁撤單和掛單，請留意交易所的 API Rate Limit (頻率限制)。
  * **趨勢風險**：Avellaneda 模型適合震盪行情。在單邊暴漲或暴跌的趨勢中，做市商策略可能會面臨持續的逆勢持倉虧損。
  * **數據依賴**：模型的有效性高度依賴於正確且及時的市場數據 (K線、成交、盤口)。數據異常可能導致策略失效。
  * **參數敏感**：`AVE_GAMMA` 等少數固定參數對策略表現有顯著影響，需要謹慎調整和充分測試。
  * **本軟件按「現狀」提供**，不保證獲利。使用者需自行承擔交易風險。建議先在模擬盤或使用極小資金進行測試。

## 📝 日誌 (Logging)

運行過程中會自動生成 `log/` 文件夾，你可以在 `avellaneda_bot.log` 中查看詳細的計算數據 (例如：更新後的 $A, k$ 值、公允價格、價差、持倉量等)。

```