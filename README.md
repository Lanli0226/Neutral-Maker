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

### 2. 配置環境變數 (.env)

在專案根目錄創建 `.env` 檔案，並填入以下配置：

```ini
# Gate.io API 設定
API_KEY=你的_API_KEY
API_SECRET=你的_API_SECRET

# 策略核心配置
POSITION_LIMIT=100       # 最大持倉限制 (張數)
ORDER_REFRESH_TIME=20    # 訂單刷新/重掛單週期 (秒)
MIN_SPREAD_PERCENT=0.02  # 最小掛單價差保護 (百分比)

# Gamma 自動調整設定
# 滿倉時，報價偏移量 (Skew) 佔半價差 (Half Spread) 的比例
# 0.8 代表滿倉時偏移 80% 的 Spread，極度不鼓勵繼續開倉
AUTO_GAMMA_TARGET_RATIO=0.8

# (可選) 手動 Gamma 設定 - 僅作為自動調整前的初始值
AVE_GAMMA=10
```

> ⚠️ **注意**：請確保 API Key 權限已開啟 **合約交易 (Futures)** 的讀寫權限。

### 3. 啟動機器人

在終端機 (Terminal) 執行以下命令：

```bash
python avellaneda_bot.py
```

-----

## ⚙️ 策略參數詳解 (GLFT Parameters)

所有參數現已支援動態調整。市場流動性參數 ($A, k$) 和波動率 ($\sigma$) 將由機器人**自動估算和實時校準**。

| 環境變數 | 預設值 | 說明 |
| :--- | :--- | :--- |
| **`POSITION_LIMIT`** | `100` | **最大持倉限制 (張)**。<br>這不僅是硬性限制，也是自動計算 Gamma 的核心依據。設定得越小，機器人對庫存越敏感。 |
| **`ORDER_REFRESH_TIME`** | `20` | **訂單刷新時間 (秒)**。<br>為了緊跟市場價格，機器人每隔此時間會強制撤單重掛。 |
| **`MIN_SPREAD_PERCENT`** | `0.02` | **最小價差保護 (%)**。<br>防止因參數異常導致報價過窄而虧損手續費。建議設為略低於雙邊手續費。 |
| **`AUTO_GAMMA_TARGET_RATIO`** | `0.8` | **自動 Gamma 目標比例**。<br>控制風險厭惡程度。值越大，滿倉時報價偏移越劇烈。 |
| **`CALIBRATION_INTERVAL`** | `300` | **實時校準間隔 (秒)**。<br>設定為 300 秒 (5分鐘)，以確保收集足夠的成交數據進行穩定回歸。 |

## 📊 策略邏輯簡述 (v2.0 更新)

本機器人採用 Guéant–Lehalle–Fernandez-Tapia (GLFT) 閉式解模型，並增加了實時流動性參數校準機制：

1.  **全自動 Gamma 調整**：
    *   不再依賴人工設定抽象的 `AVE_GAMMA`。
    *   機器人根據 `POSITION_LIMIT` 和當前市場流動性，反推最優 Gamma，確保在庫存滿載時能有效偏移報價進行平倉。

2.  **實時數據收集與暖機**：
    *   啟動時進入「暖機模式」，收集至少 200 筆成交數據。
    *   數據收集完成後，執行首次校準並開始掛單。
    *   數據收集與參數計算均採用**分鐘制 (Minute Base)**，以適應加密貨幣市場的高頻特性。

3.  **動態流動性校準**：
    *   每 5 分鐘 (300秒) 提取最近的成交數據進行回歸分析。
    *   包含**參數暴走保護**，防止 $A$ 值過大導致模型失效。

4.  **最佳報價計算**：
    *   基於當前庫存量、市場價格、以及實時校準後的 $\sigma, A, k, \gamma$ 參數，模型會計算出動態的**公允價格 (Reserve Price)** 和**最優報價價差 (Optimal Spread)**。

### GLFT 模型核心公式

**訂單流強度估計**：
$$ \lambda(\delta) = A e^{-k \delta} $$
其中：
- $\delta$：訂單距離中間價的價格距離。
- $A$：訂單到達強度（Orders/Min），$k$：流動性衰減係數。

**GLFT 係數 $c_1, c_2$ 計算**：
$$ c_1 = \frac{1}{\gamma \Delta} \ln \left( 1 + \frac{\gamma \Delta}{k} \right) $$
$$ c_2 = \sqrt{\frac{\gamma}{2 A \Delta k} \left( 1 + \frac{\gamma \Delta}{k} \right)^{\frac{k}{\gamma \Delta} + 1}} $$

**最佳報價**：
$$ P_{bid} = P_{mid} - (c_1 + \frac{\Delta}{2} \sigma c_2 + q \sigma c_2) $$
$$ P_{ask} = P_{mid} + (c_1 + \frac{\Delta}{2} \sigma c_2 - q \sigma c_2) $$

## ⚠️ 風險提示 (Disclaimer)

  * **高頻撤單**：此策略會頻繁撤單和掛單，請留意交易所的 API Rate Limit (頻率限制)。
  * **趨勢風險**：Avellaneda 模型適合震盪行情。在單邊暴漲或暴跌的趨勢中，做市商策略可能會面臨持續的逆勢持倉虧損。
  * **數據依賴**：模型的有效性高度依賴於正確且及時的市場數據 (K線、成交、盤口)。數據異常可能導致策略失效。
  * **本軟件按「現狀」提供**，不保證獲利。使用者需自行承擔交易風險。建議先在模擬盤或使用極小資金進行測試。

## 📝 日誌 (Logging)

運行過程中會自動生成 `log/` 文件夾，你可以在 `avellaneda_bot.log` 中查看詳細的計算數據 (例如：更新後的 $A, k$ 值、公允價格、價差、持倉量等)。
