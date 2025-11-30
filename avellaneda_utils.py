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

        # 4. 計算 Delta: |Trade_Price - Ref_Mid_Price|
        merged_df['delta'] = (merged_df['price'] - merged_df['ref_mid_price']).abs()
        
        # 5. 計算時間窗口長度 (秒)
        time_span_sec = (trades_df["timestamp"].max() - trades_df["timestamp"].min()).total_seconds()
        if time_span_sec < 1.0:
            time_span_sec = 1.0 # 避免除以零
            
        # 6. 統計 Delta 的分佈 (Histogram)
        # 自動決定分桶數量，或固定一個合理值
        bins_count = 20
        delta_max = merged_df['delta'].max()
        if delta_max == 0: delta_max = 0.0001 # 避免全 0
        
        bins = np.linspace(0, delta_max, bins_count)
        counts, bin_edges = np.histogram(merged_df['delta'], bins=bins)
        
        # 計算每個桶的中心 Delta
        bin_centers = (bin_edges[:-1] + bin_edges[1:]) / 2
        
        # 過濾掉 count = 0 的點
        valid_idx = counts > 0
        if np.sum(valid_idx) < 2:
            logger.warning("有效數據點過少，無法擬合，返回默認值。")
            return 100.0, 50.0
            
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
        if np.sum(valid_idx) < 2:
            return 0.0, 0.0
            
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
        
        return A, k

    except Exception as e:
        logger.error(f"實時參數校準錯誤: {e}")
        return 0.0, 0.0

def auto_calculate_params(coin: str, taker_fee: float) -> tuple[float, float, float]:
    """
    執行參數自動計算與推算，並返回 sigma (per sec), A (per sec), k
    """
    currency_pair = f"{coin}_USDT"
    
    # 1. 獲取 1h K 線數據 (用於長期穩定的波動率計算)
    # 720h = 30 days
    kline_1h = get_gateio_kline(currency_pair, interval="1h", limit=720)
    
    # 2. 計算波動率 (Sigma_1h)
    sigma_1h = calculate_historical_volatility(kline_1h)
    if sigma_1h < 1e-5:
        sigma_1h = 0.005
        logger.warning(f"波動率計算結果過小或失敗，使用預設值 {sigma_1h}")
        
    # 轉換波動率為 [per second]
    # sigma_sec = sigma_1h / sqrt(3600) = sigma_1h / 60
    sigma_sec = sigma_1h / 60.0

    # 3. 獲取 1m K 線數據 (用於校準 A, k 的參考價格)
    # 1000m approx 16 hours, 足夠覆蓋 recent trades
    kline_1m = get_gateio_kline(currency_pair, interval="1m", limit=1000)

    # 4. 獲取近期成交 (用於估算 A, k)
    trades_df = get_gateio_recent_trades(currency_pair, limit=1000)
    
    # 5. 校準 A, k (A 將被標準化為 per second)
    A, k = calibrate_market_params(trades_df, kline_1m)

    logger.info(f"--- Avellaneda (GLFT) 參數推算結果 ---")
    logger.info(f"Sigma (1h): {sigma_1h:.4f} -> Sigma (1s): {sigma_sec:.8f}")
    logger.info(f"A (Intensity): {A:.4f} orders/sec")
    logger.info(f"k (Decay): {k:.4f}")
    logger.info(f"---------------------------------------")

    return sigma_sec, A, k