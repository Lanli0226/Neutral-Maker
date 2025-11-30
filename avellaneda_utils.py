# avellaneda_utils.py
import pandas as pd
import numpy as np
import requests
import math
import logging

# 設置日誌
logger = logging.getLogger('AvellanedaBot')
# 確保日誌在 utils 檔案中也能正常顯示
if not logger.handlers:
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- Gate.io K 線資料抓取 ---
def get_gateio_kline(currency_pair: str, interval: str = "1h", limit: int = 720) -> pd.DataFrame:
    """
    從 Gate.io API 取得歷史 K 線資料
    """
    try:
        base_url = "https://api.gateio.ws/api/v4/spot/candlesticks"
        params = {
            "currency_pair": currency_pair.upper(),
            "interval": interval,
            "limit": limit
        }

        response = requests.get(base_url, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()

        df = pd.DataFrame(data, columns=[
            "timestamp", "volume_quote", "close", "high", "low", "open", "volume_base", "closed"
        ])
        if df.empty:
             return df
             
        df["timestamp"] = pd.to_datetime(df["timestamp"].astype(float), unit="s", utc=True)
        df[["open", "high", "low", "close"]] = df[["open", "high", "low", "close"]].astype(float)
        
        df = df.sort_values("timestamp").reset_index(drop=True)
        return df[["timestamp", "open", "high", "low", "close"]]
        
    except requests.RequestException as e:
        logger.error(f"獲取 Gate.io K 線資料失敗: {e}")
        return pd.DataFrame()


def calculate_historical_volatility(df: pd.DataFrame) -> float:
    """計算歷史波動率 (基於 K 線間隔)"""
    if df.empty or len(df) < 2:
        logger.warning("K線數據不足，無法計算波動率。")
        return 0.0

    # 計算對數收益率 Ri = ln(Pi / P(i-1))
    df['log_return'] = np.log(df['close'] / df['close'].shift(1))
    
    # 計算對數收益率的標準差 (即該時間間隔的波動率)
    volatility = df['log_return'].std()
    
    return volatility if not math.isnan(volatility) else 0.0

def get_gateio_recent_trades(currency_pair: str, limit: int = 1000) -> pd.DataFrame:
    """
    從 Gate.io API 取得近期成交紀錄
    """
    try:
        base_url = "https://api.gateio.ws/api/v4/spot/trades"
        params = {
            "currency_pair": currency_pair.upper(),
            "limit": limit
        }

        response = requests.get(base_url, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()

        df = pd.DataFrame(data)
        if df.empty:
             return df
        
        # 轉換數值
        df["create_time_ms"] = df["create_time_ms"].astype(float)
        df["price"] = df["price"].astype(float)
        df["amount"] = df["amount"].astype(float)
        df["timestamp"] = pd.to_datetime(df["create_time_ms"], unit="ms", utc=True)
        
        return df
        
    except requests.RequestException as e:
        logger.error(f"獲取 Gate.io 成交紀錄失敗: {e}")
        return pd.DataFrame()

def calibrate_market_params(trades_df: pd.DataFrame, kline_df: pd.DataFrame) -> tuple[float, float]:
    """
    根據成交紀錄與 K 線數據，估算交易強度參數 A 和 k (kappa)。
    模型: lambda(delta) = A * exp(-k * delta)
    
    改進:
    1. 使用 merge_asof 匹配每筆交易對應的 K 線 (1m) 以獲取更精確的 Mid Price。
    2. 計算 A 時考慮時間窗口，標準化為 [orders / second]。
    """
    if trades_df.empty or kline_df.empty:
        logger.warning("數據不足，無法校準 A, k。使用默認值。")
        return 100.0, 50.0 # Safe defaults

    try:
        # 1. 資料準備與排序
        trades_df = trades_df.sort_values("timestamp")
        kline_df = kline_df.sort_values("timestamp")
        
        # 2. 為 K 線計算參考中間價 (High + Low) / 2，比 Close 更能代表區間中心
        kline_df["ref_mid_price"] = (kline_df["high"] + kline_df["low"]) / 2.0
        
        # 3. 匹配交易數據與最近的 K 線 (backward search)
        # 這會找到交易發生時刻或之前的最近一根 K 線
        merged_df = pd.merge_asof(
            trades_df, 
            kline_df[["timestamp", "ref_mid_price"]], 
            on="timestamp", 
            direction="backward"
        )
        
        # 填補缺失值 (如果交易早於第一根 K 線)
        if merged_df["ref_mid_price"].isnull().any():
            merged_df["ref_mid_price"] = merged_df["ref_mid_price"].fillna(method='bfill').fillna(trades_df['price'].mean())

        # 5. 計算 Delta: |Trade_Price - Ref_Mid_Price|
        merged_df['delta'] = (merged_df['price'] - merged_df['ref_mid_price']).abs()
        
        # --- 優化: 只取最近一段時間 (例如 600秒 = 10分鐘) 的數據進行校準 ---
        # 避免因為抓取了 1000 筆但跨度長達 1小時，導致 A (強度) 被平均得過低或過高
        max_ts = merged_df["timestamp"].max()
        cutoff_ts = max_ts - pd.Timedelta(seconds=600)
        
        recent_df = merged_df[merged_df["timestamp"] >= cutoff_ts]
        
        # 如果最近數據太少，還是用全部
        if len(recent_df) < 50:
            recent_df = merged_df
            
        # 5. 計算時間窗口長度 (秒)
        # A = N / T. 如果 T 太大 (包含了很多無交易的空檔)，A 會變小。
        # 但如果 T 只算首尾時間差，這就是真實的 "平均到達率"。
        time_span_sec = (recent_df["timestamp"].max() - recent_df["timestamp"].min()).total_seconds()
        if time_span_sec < 1.0:
            time_span_sec = 1.0 # 避免除以零
            
        # 6. 統計 Delta 的分佈 (Histogram)
        # 自動決定分桶數量，或固定一個合理值
        bins_count = 20
        delta_max = recent_df['delta'].max()
        if delta_max == 0: delta_max = 0.0001 # 避免全 0
        
        bins = np.linspace(0, delta_max, bins_count)
        counts, bin_edges = np.histogram(recent_df['delta'], bins=bins)
        
        # 計算每個桶的中心 Delta
        bin_centers = (bin_edges[:-1] + bin_edges[1:]) / 2
        
        # 過濾掉 count = 0 的點
        valid_idx = counts > 0
        if np.sum(valid_idx) < 2:
            logger.warning("有效數據點過少(集中)，使用簡易估算。")
            # Fallback: A = N / T, k = default
            total_count = len(merged_df)
            A_est = total_count / time_span_sec
            k_est = 2000.0
            
            # Apply limits (Seconds base)
            if A_est > 33333.0: A_est = 33333.0
            
            return A_est, k_est
            
        log_counts = np.log(counts[valid_idx])
        valid_deltas = bin_centers[valid_idx]

        # 7. 線性回歸: ln(count) = ln(A_window) - k * delta
        slope, intercept = np.polyfit(valid_deltas, log_counts, 1)
        
        k = -slope
        
        # 截距是 ln(A_window * bin_width?) 
        # 注意: counts 是落在 bin 裡的數量。lambda(delta) 是密度。
        # 理論上 lambda(delta) * time * d_delta = count
        # count = (A * exp(-k*delta)) * time * bin_width
        # ln(count) = ln(A) + ln(time) + ln(bin_width) - k*delta
        # intercept = ln(A) + ln(time) + ln(bin_width)
        # ln(A) = intercept - ln(time) - ln(bin_width)
        
        bin_width = bins[1] - bins[0]
        ln_A = intercept - np.log(time_span_sec) - np.log(bin_width)
        A = np.exp(ln_A)
        
        # 保護 k 為正值
        k = max(0.1, k)
        # A 必須為正
        A = max(0.001, A)
        
        # --- 防止參數暴走 (Capping) ---
        # 這裡是秒制 A。限制為 33333 (約對應分制 200萬)
        if A > 33333.0: A = 33333.0
        if k > 50000.0: k = 50000.0
        
        logger.info(f"參數校準完成 (Window={time_span_sec:.1f}s): A={A:.4f} orders/sec, k={k:.2f}")
        return A, k

    except Exception as e:
        logger.error(f"參數校準發生錯誤: {e}")
        return 100.0, 50.0

def compute_glft_params(gamma, sigma, T, A, k, delta_price):
    """
    計算 GLFT 模型的參數 c1, c2
    輸入單位需保持一致 (例如全部基於 Tick)
    """
    try:
        xi = gamma
        delta = delta_price 
        
        # 避免除以零
        if k < 1e-9: k = 1e-9
        if xi < 1e-9: xi = 1e-9
        if A < 1e-9: A = 1e-9
        
        inv_k = 1.0 / k
        
        # Formula: c1 = 1 / (xi * delta) * ln(1 + xi * delta / k)
        term1 = xi * delta
        c1 = (1.0 / term1) * np.log(1.0 + term1 * inv_k)
        
        # Formula: c2 = sqrt( gamma / (2*A*delta*k) * (1 + xi*delta/k)^(k/(xi*delta) + 1) )
        term2_base = 1.0 + term1 * inv_k
        exponent = (k / term1) + 1.0
        
        factor1 = gamma / (2 * A * delta * k)
        
        c2 = np.sqrt(factor1 * (term2_base ** exponent))
        
        return c1, c2
        
    except Exception as e:
        logger.error(f"GLFT 參數計算錯誤: {e}, 返回默認值")
        return 0.0, 0.0

def solve_gamma_for_risk_target(sigma_min, A_min, k, tick_size, price, max_pos, target_ratio=0.8) -> float:
    """
    反推 Gamma:
    目標: 當持倉量 = max_pos 時，Skew = target_ratio * HalfSpread
    也就是: Skew * max_pos = target_ratio * (HalfSpread)
    
    參數:
    - sigma_min: 每分鐘波動率
    - A_min: 每分鐘交易強度
    - k: 衰減係數 (無量綱或對應價格單位，這裡假設輸入是原始 k)
    - tick_size: 最小跳動點
    - price: 當前價格
    - max_pos: 最大持倉量
    - target_ratio: 目標比例 (預設 0.8，即滿倉時偏移 80% 的 Spread)
    """
    try:
        # 1. 單位轉換 (全部轉為 Tick 單位，與 bot 邏輯一致)
        # Sigma (min) -> Sigma (Tick)
        # 注意: bot 裡用的是 sigma_min (因為參數都是分鐘制)
        # 但 GLFT 公式裡的 sigma 需對應時間單位。
        # 我們的 A, k 都是分鐘制，所以 sigma 也要用分鐘制。
        sigma_tick = (price * sigma_min) / tick_size
        
        # k (per $) -> k (per Tick)
        k_tick = k * tick_size
        
        # 搜尋範圍 Gamma (per $)
        # Gamma_tick = Gamma_$ * Tick_Size
        # 我們直接搜尋 Gamma_$
        low = 1e-5
        high = 1000.0
        best_gamma = 10.0
        min_diff = float('inf')
        
        for _ in range(20): # 二分搜尋 20 次
            mid_gamma = (low + high) / 2
            gamma_tick = mid_gamma * tick_size
            
            # 計算 c1, c2 (Delta=1 Tick)
            c1, c2 = compute_glft_params(gamma_tick, sigma_tick, None, A_min, k_tick, delta_price=1.0)
            
            half_spread_tick = c1 + 0.5 * sigma_tick * c2
            skew_tick = sigma_tick * c2
            
            # 目標方程: skew * max_pos = ratio * half_spread
            lhs = skew_tick * max_pos
            rhs = target_ratio * half_spread_tick
            
            diff = lhs - rhs
            
            if abs(diff) < min_diff:
                min_diff = abs(diff)
                best_gamma = mid_gamma
            
            # 如果 skew 太大 (lhs > rhs)，代表 gamma 太大 -> 往小搜
            # 如果 skew 太小 (lhs < rhs)，代表 gamma 太小 -> 往大搜
            if lhs > rhs:
                high = mid_gamma
            else:
                low = mid_gamma
                
        logger.info(f"Gamma 自動反推: Target={target_ratio:.1f}*Spread@MaxPos({max_pos}) -> New Gamma={best_gamma:.4f}")
        return best_gamma

    except Exception as e:
        logger.error(f"Gamma 反推計算失敗: {e}")
        return 10.0 # Fallback

def calibrate_from_deltas(deltas: list[float], time_span_sec: float) -> tuple[float, float]:
    """
    根據預先計算好的 Delta (成交價與中間價距離) 列表估算 A, k。
    適用於 Bot 實時收集的數據流。
    """
    if not deltas or len(deltas) < 10 or time_span_sec <= 0:
        return 0.0, 0.0  # 數據不足或無效

    try:
        deltas_arr = np.array(deltas)
        
        # 6. 統計 Delta 的分佈 (Histogram)
        # 自動決定分桶數量，或固定一個合理值
        bins_count = 20
        delta_max = deltas_arr.max()
        if delta_max == 0: delta_max = 0.0001 
        
        bins = np.linspace(0, delta_max, bins_count)
        counts, bin_edges = np.histogram(deltas_arr, bins=bins)
        
        # 計算每個桶的中心 Delta
        bin_centers = (bin_edges[:-1] + bin_edges[1:]) / 2
        
        # 過濾掉 count = 0 的點
        valid_idx = counts > 0
        
        # --- Fallback 邏輯：如果數據太集中 (例如只有 1 個 bin 有值)，無法做回歸 ---
        if np.sum(valid_idx) < 2:
            # 這通常發生在價格非常穩定的時候，Delta 幾乎都一樣
            # 我們可以做一個合理的估計：
            # A ~ N / T (總成交頻率)
            # k ~ 較大的值 (因為 Delta 很小且集中，代表流動性衰減快?) 或者給個默認值
            
            total_count = len(deltas_arr)
            A_est = total_count / time_span_sec
            
            # 使用一個相對保守的 k 值，或者沿用上一次的 k (但這裡無法獲取上一次的值)
            # 假設市場集中，k 應該不小
            k_est = 2000.0 
            
            # 同樣套用上限保護
            if A_est > 2000000.0: A_est = 2000000.0
            
            return A_est, k_est
            
        log_counts = np.log(counts[valid_idx])
        valid_deltas = bin_centers[valid_idx]

        # 7. 線性回歸: ln(count) = ln(A_window) - k * delta
        slope, intercept = np.polyfit(valid_deltas, log_counts, 1)
        
        k = -slope
        
        # 還原 A (Orders per second)
        bin_width = bins[1] - bins[0]
        ln_A = intercept - np.log(time_span_sec) - np.log(bin_width)
        A = np.exp(ln_A)
        
        # 保護
        k = max(0.1, k)
        A = max(0.001, A)

        # --- 防止參數暴走 (Capping) ---
        # 如果 A 太大，Skew 會趨近於 0，導致失去庫存管理能力。
        # 2,000,000 / min approx 33,333 / sec (非常極端的高頻)
        if A > 2000000.0: 
            A = 2000000.0
        
        if k > 50000.0:
            k = 50000.0
            
        return A, k

    except Exception as e:
        logger.error(f"實時參數校準錯誤: {e}")
        return 0.0, 0.0

def auto_calculate_params(coin: str, taker_fee: float) -> tuple[float, float, float]:
    """
    執行參數自動計算與推算，並返回 sigma (per sec), A (per sec), k
    """
    currency_pair = f"{coin}_USDT"
    
    # 1. 獲取 1m K 線數據 (用於長期穩定的波動率計算)
    # 720m = 12 小時 (通常足夠獲取近期波動率，且比 1h K 線更靈敏)
    kline_1m = get_gateio_kline(currency_pair, interval="1m", limit=720)
    
    # 2. 計算波動率 (Sigma_1m)
    sigma_1m = calculate_historical_volatility(kline_1m)
    if sigma_1m < 1e-5:
        sigma_1m = 0.005
        logger.warning(f"波動率計算結果過小或失敗，使用預設值 {sigma_1m}")
        
    # 轉換波動率為 [per minute]
    # 用戶要求使用每分鐘波動率，不再除以 sqrt(60)
    sigma_min = sigma_1m 

    # 3. 獲取 1m K 線數據 (用於校準 A, k 的參考價格)
    # 1000m approx 16 hours, 足夠覆蓋 recent trades
    kline_1m_for_ak = get_gateio_kline(currency_pair, interval="1m", limit=1000)

    # 4. 獲取近期成交 (用於估算 A, k)
    trades_df = get_gateio_recent_trades(currency_pair, limit=1000)
    
    # 5. 校準 A, k
    # 原始 calibrate_market_params 返回的是 orders/sec，這裡需要轉換為 orders/min
    A_sec, k = calibrate_market_params(trades_df, kline_1m_for_ak)
    A_min = A_sec * 60.0

    logger.info(f"--- Avellaneda (GLFT) 參數推算結果 (Base: Minute) ---")
    logger.info(f"Sigma (1m): {sigma_min:.8f}")
    logger.info(f"A (Intensity): {A_min:.4f} orders/min (Orig: {A_sec:.4f}/sec)")
    logger.info(f"k (Decay): {k:.4f}")
    logger.info(f"---------------------------------------")

    return sigma_min, A_min, k