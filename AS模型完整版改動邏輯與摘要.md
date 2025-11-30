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
- $A$：當 $\delta=0$ 時的基礎訂單到達強度 (Intensity)，單位標準化為 **Orders/Min** (v2.0更新: 從秒制改為分鐘制)。
- $k$ (Kappa)：流動性衰減係數。$k$ 越大，隨著報價遠離中間價，成交機率下降越快 (市場深度越淺)。

**實作方式** (`avellaneda_utils.py`):
1.  **歷史初始化**：啟動時獲取最近 1000 筆成交與 1m K線，使用 `pd.merge_asof` 匹配計算初始參數。
2.  **波動率計算**：使用 720 根 1m K線計算歷史波動率 $\sigma$，直接使用分鐘級波動率。

### 2.2 最佳報價計算 (Optimal Quotes)

使用 GLFT 模型的漸近閉式解 (Asymptotic Closed-form Solution) 替代原有的近似公式。

**新公式 (GLFT)**：
$$ r^* = c_1 + \frac{\Delta}{2} \sigma_{tick} c_2 $$
$$ s^* = \sigma_{tick} c_2 $$

其中係數 $c_1, c_2$ 計算如下 (基於 Tick 單位)：
$$ c_1 = \frac{1}{\gamma \Delta} \ln \left( 1 + \frac{\gamma \Delta}{k} \right) $$
$$ c_2 = \sqrt{\frac{\gamma}{2 A \Delta k} \left( 1 + \frac{\gamma \Delta}{k} \right)^{\frac{k}{\gamma \Delta} + 1}} $$

*註：代碼中所有參數 ($A, k, \sigma, \gamma$) 均統一轉換為以 Tick 為單位和 Minute 為時間單位的數值。*

## 3. 實時流動性校準機制 (Real-time Calibration)

為了讓機器人適應盤中即時的流動性變化，我們引入了基於 WebSocket 數據流的動態校準機制。

### 3.1 數據收集 (`bot.py`)
- **訂閱頻道**：`futures.trades` (公開成交數據)。
- **實時計算**：每當市場發生一筆成交，立即計算該成交價格與機器人當前認知的中間價 (`(Bid+Ask)/2`) 的距離 $\delta$。
- **緩衝存儲**：將 `(timestamp, delta)` 存入 `deque` 環形緩衝區 (最大 10000 筆)。這比事後匹配 K 線更精確，因為它捕捉了 Tick 級別的盤口狀態。

### 3.2 動態更新 (`avellaneda_bot.py`)
- **觸發機制**：策略循環中，每隔 `CALIBRATION_INTERVAL` (300秒) 檢查一次。
- **參數重算**：
    1. 從 `bot.py` 提取最近 300秒 (Rolling Window) 的 Delta 數據列表。
    2. 若數據不足 200 筆，自動擴大範圍取最近 1000 筆，確保樣本充足。
    3. 調用 `calibrate_from_deltas` 執行回歸分析。
    4. 若回歸成功，更新策略的 $A$ 和 $k$ 參數，並同步觸發 Gamma 自動調整 (`auto_tune_gamma`)。

## 4. 代碼模組變更摘要

### `avellaneda_utils.py`
- **新增** `calibrate_from_deltas`: 專為實時數據流設計的輕量化回歸函數。
- **新增** `solve_gamma_for_risk_target`: 基於最大持倉量反推合理 Gamma 值的算法。
- **更新** `calibrate_market_params`: 初始校準使用 `merge_asof` 提高精度，且加入參數上限保護 (Capping)。

### `bot.py`
- **新增** `subscribe_public_trades`: 訂閱市場成交。
- **新增** `handle_public_trade_update`: 處理成交流，計算 Delta 並緩存。
- **配置**：支援從環境變數讀取 `POSITION_LIMIT`。

### `avellaneda_bot.py`
- **新增** `recalibrate_parameters`: 執行週期性參數更新邏輯。
- **新增** `auto_tune_gamma`: 自動計算風險厭惡係數。
- **機制**：訂單定期刷新 (Refresh)、暖機檢查 (Warm-up)、最小價差保護 (Min Spread)。

## 5. 預期效果
- **冷啟動優化**：啟動時利用歷史數據 (1m K線匹配) 獲得一個不錯的初始狀態。
- **動態適應**：
    - **高頻交易時段**：$A$ 值顯著升高，機器人會傾向於縮窄價差以捕捉流量。
    - **流動性枯竭時段**：$k$ 值變大 (成交機率衰減快) 或成交稀疏，機器人會自動調整報價寬度以保護自身。
- **精度提升**：實時 Delta 計算消除了 K 線聚合帶來的時間和價格誤差。

## 6. 近期重大更新 (v2.0)

### 6.1 時間基準變更 (Minute Base)
- 所有參數 ($\sigma, A$) 從原本的「秒制」改為「分鐘制」。
- 理由：加密貨幣市場的高頻特性使得秒制參數數值差異極大，分鐘制更便於觀察與調試。
- 影響：$A$ 值擴大約 60 倍，$\sigma$ 擴大約 $\sqrt{60}$ 倍。

### 6.2 自動 Gamma 調整 (Auto Gamma Tuning)
- **問題**：Gamma 很難憑直覺設定，且隨 $A$ 變化，固定 Gamma 會導致 Skew 失效。
- **解法**：新增 `solve_gamma_for_risk_target`。
- **邏輯**：設定目標為「當持倉達到 `POSITION_LIMIT` 時，報價偏移量 (Skew) 應達到半價差 (Half Spread) 的一定比例 (預設 80%)」。
- **效果**：機器人能根據當前流動性與最大持倉限制，自動計算出最合適的 Gamma，確保庫存風險控制始終有效。

### 6.3 參數暴走保護 (Parameter Capping)
- **限制**：$A \le 2,000,000$ (分鐘制), $k \le 50,000$。
- **目的**：防止在極端行情或數據異常集中時，回歸出天文數字般的 $A$，導致模型失效 (Skew 歸零)。

### 6.4 暖機機制 (Warm-up)
- 啟動時若緩衝區數據不足 200 筆，機器人將處於「暖機模式」，只收集數據不掛單。
- 待數據充足並完成首次實時校準後，才正式開始交易。

### 6.5 訂單刷新與保護
- **訂單刷新**：每隔 `ORDER_REFRESH_TIME` (20秒) 強制撤單重掛，確保報價緊跟市場。
- **最小價差**：新增 `MIN_SPREAD_PERCENT` (0.02%) 保護，防止 Spread 小於手續費導致虧損。