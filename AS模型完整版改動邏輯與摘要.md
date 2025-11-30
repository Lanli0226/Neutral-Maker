# AS模型完整版 (Guéant–Lehalle–Fernandez-Tapia) 改動邏輯與摘要

本文件記錄了將原有的 Avellaneda-Stoikov 簡化版模型升級為 Guéant–Lehalle–Fernandez-Tapia (GLFT) 閉式解完整版的改動邏輯與技術細節，包含實時流動性校準機制。

## 1. 改動背景與目標

**原模型問題**：
原實作使用基於手續費率 (Taker Fee) 的啟發式 `eta` 參數來替代流動性參數。這種方法無法反映真實市場訂單簿的流動性深度與訂單到達率，導致在不同市場環境下報價可能過寬或過窄。

**新模型目標**：
依據 "Guéant–Lehalle–Fernandez-Tapia Market Making Model" (GLFT)，引入對市場訂單流強度 (Trading Intensity) 的估計，使用參數 $A$ 和 $k$ (或 $\kappa$) 來描述市場流動性，並使用該模型的閉式解 (Closed-form solution) 計算最佳報價。

## 2. 核心改動邏輯

### 2.1 市場參數估計 (Calibration)

引入了對訂單到達率 $\lambda(\delta)$ 的估計，假設其服從指數衰減分佈：
$$ \lambda(\delta) = A e^{-k \delta} $$
其中：
- $\delta$：報價距離中間價的距離 (Price Distance)。
- $A$：當 $\delta=0$ 時的基礎訂單到達強度 (Intensity)，單位標準化為 **Orders/Sec**。
- $k$ (Kappa)：流動性衰減係數。$k$ 越大，隨著報價遠離中間價，成交機率下降越快 (市場深度越淺)。

**實作方式** (`avellaneda_utils.py`):
1.  **歷史初始化**：啟動時獲取最近 1000 筆成交與 1m K線，使用 `pd.merge_asof` 匹配計算初始參數。
2.  **波動率計算**：使用 720 根 1h K線計算長期穩定的歷史波動率 $\sigma$，並轉換為每秒波動率。

### 2.2 最佳報價計算 (Optimal Quotes)

使用 GLFT 模型的漸近閉式解 (Asymptotic Closed-form Solution) 替代原有的近似公式。

**新公式 (GLFT)**：
$$ r^* = c_1 + \frac{\Delta}{2} \sigma_{tick} c_2 $$
$$ s^* = \sigma_{tick} c_2 $$

其中係數 $c_1, c_2$ 計算如下 (基於 Tick 單位)：
$$ c_1 = \frac{1}{\gamma \Delta} \ln \left( 1 + \frac{\gamma \Delta}{k} \right) $$
$$ c_2 = \sqrt{\frac{\gamma}{2 A \Delta k} \left( 1 + \frac{\gamma \Delta}{k} \right)^{\frac{k}{\gamma \Delta} + 1}} $$

*註：代碼中所有參數 ($A, k, \sigma, \gamma$) 均統一轉換為以 Tick 為單位和 Second 為時間單位的數值。*

## 3. 實時流動性校準機制 (Real-time Calibration)

為了讓機器人適應盤中即時的流動性變化，我們引入了基於 WebSocket 數據流的動態校準機制。

### 3.1 數據收集 (`bot.py`)
- **訂閱頻道**：`futures.trades` (公開成交數據)。
- **實時計算**：每當市場發生一筆成交，立即計算該成交價格與機器人當前認知的中間價 (`(Bid+Ask)/2`) 的距離 $\delta$。
- **緩衝存儲**：將 `(timestamp, delta)` 存入 `deque` 環形緩衝區 (最大 10000 筆)。這比事後匹配 K 線更精確，因為它捕捉了 Tick 級別的盤口狀態。

### 3.2 動態更新 (`avellaneda_bot.py`)
- **觸發機制**：策略循環中，每隔 `CALIBRATION_INTERVAL` (默認 60秒) 檢查一次。
- **參數重算**：
    1. 從 `bot.py` 提取累積的 Delta 數據列表。
    2. 計算數據的時間跨度 $T_{span}$。
    3. 調用 `calibrate_from_deltas` 執行回歸分析。
    4. 若回歸成功，即時更新策略的 $A$ 和 $k$ 參數。

## 4. 代碼模組變更摘要

### `avellaneda_utils.py`
- **新增** `calibrate_from_deltas`: 專為實時數據流設計的輕量化回歸函數，不依賴 DataFrame，直接處理數值列表。
- **更新** `calibrate_market_params`: 初始校準使用 `merge_asof` 提高精度。

### `bot.py`
- **新增** `subscribe_public_trades`: 訂閱市場成交。
- **新增** `handle_public_trade_update`: 處理成交流，計算 Delta 並緩存。
- **結構**：引入 `deque` 存儲實時數據。

### `avellaneda_bot.py`
- **新增** `recalibrate_parameters`: 執行週期性參數更新邏輯。
- **集成**：在 `adjust_grid_strategy` 中調用校準，實現「邊跑邊學」。

## 5. 預期效果
- **冷啟動優化**：啟動時利用歷史數據 (1m K線匹配) 獲得一個不錯的初始狀態。
- **動態適應**：
    - **高頻交易時段**：$A$ 值顯著升高，機器人會傾向於縮窄價差以捕捉流量。
    - **流動性枯竭時段**：$k$ 值變大 (成交機率衰減快) 或成交稀疏，機器人會自動調整報價寬度以保護自身。
- **精度提升**：實時 Delta 計算消除了 K 線聚合帶來的時間和價格誤差。
