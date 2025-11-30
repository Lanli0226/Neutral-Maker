import asyncio
import time
import os
# 假設 GridTradingBot 和所有必要的常量、logger 都從 bot.py 導入
from bot import GridTradingBot, logger, POSITION_LIMIT
from avellaneda_utils import auto_calculate_params, compute_glft_params, calibrate_from_deltas, solve_gamma_for_risk_target
from dotenv import load_dotenv
load_dotenv()

# ==================== Avellaneda 參數配置 (動態獲取) ====================
# 【重要】這些參數將在 main 函數中被 auto_calculate_params 的結果覆蓋
AVE_GAMMA = float(os.getenv("AVE_GAMMA", 10)) # (作為備用/初始值)
AUTO_GAMMA_TARGET_RATIO = float(os.getenv("AUTO_GAMMA_TARGET_RATIO", 0.8)) # 自動 Gamma 目標: 滿倉時 Skew 是 Spread 的幾倍 (0.8 = 80% HalfSpread)

AVE_T_END = 1         # (GLFT 模型中此參數不再直接用於定價，保留作為參考或擴展)
Taker_Fee_Rate = 0.0005 # <-- 【請在此處設置您的 Taker 費率】
AVE_SIGMA = 0.0       # <--- 初始為 0，將被計算值覆蓋
AVE_A = 0.0           # <--- 初始為 0，將被計算值覆蓋 (交易強度)
AVE_K = 0.0           # <--- 初始為 0，將被計算值覆蓋 (流動性衰減)

# 假設 bot.py 中的核心配置
API_KEY = os.getenv("API_KEY")
API_SECRET = os.getenv("API_SECRET")
COIN_NAME = "XRP" 
GRID_SPACING = 0.0006
TAKE_PROFIT_SPACING = 0.0004
INITIAL_QUANTITY = 1
LEVERAGE = 20
POSITION_THRESHOLD = 500
ORDER_COOLDOWN_TIME = 60
CALIBRATION_INTERVAL = 300 # 實時校準間隔 (秒) - 改為 5 分鐘
ORDER_REFRESH_TIME = float(os.getenv("ORDER_REFRESH_TIME", 20)) # 訂單刷新/重掛單間隔 (秒)
MIN_SPREAD_PERCENT = float(os.getenv("MIN_SPREAD_PERCENT", 0.02)) # 最小掛單價差保護 (百分比, 例如 0.02 代表 0.02%)

# ==================== Avellaneda 繼承類 (保持不變) ====================
class AvellanedaGridBot(GridTradingBot):
    
    def __init__(self, api_key, api_secret, coin_name, grid_spacing, initial_quantity, leverage, 
                 take_profit_spacing=None, gamma=AVE_GAMMA, A=AVE_A, k=AVE_K, sigma=AVE_SIGMA, T_end=AVE_T_END):
        
        # 1. 呼叫父類別的初始化方法
        super().__init__(api_key, api_secret, coin_name, grid_spacing, initial_quantity, leverage, take_profit_spacing)
        
        # 2. 初始化 Avellaneda 專有參數
        self.gamma = gamma          # 初始 Gamma
        self.A = A                  # 交易強度參數 A
        self.k = k                  # 流動性衰減參數 k (kappa)
        self.sigma = sigma          # 波動率估計 (百分比/時間)
        self.T_end = T_end          
        self.reserve_price = 0      
        self.inventory = 0          
        self.best_bid = 0           
        self.best_ask = 0           
        self.last_calibration_time = time.time()
        self.last_order_refresh_time = time.time()
        self.is_warmed_up = False # 新增暖機狀態標記
        
        # 初始化時嘗試自動調整 Gamma (如果價格已知，否則等 run loop)
        # 這裡暫時先用預設，等 loop 中第一次獲取價格後調整
        
        logger.info(f"Avellaneda (GLFT) Bot 初始化: A={A:.2f}, k={k:.2f}, Sigma={sigma:.8f}, InitGamma={gamma}")

    def auto_tune_gamma(self):
        """
        根據當前市場參數 (A, k, sigma) 和 最大持倉限制 (POSITION_LIMIT)
        自動計算合適的 Gamma
        """
        if self.latest_price <= 0: return

        try:
            tick_size = 10 ** (-self.price_precision)
        except:
            tick_size = 0.0001
            
        new_gamma = solve_gamma_for_risk_target(
            self.sigma, self.A, self.k, 
            tick_size, self.latest_price, 
            POSITION_LIMIT, # 來自 bot.py
            target_ratio=AUTO_GAMMA_TARGET_RATIO
        )
        
        self.gamma = new_gamma

    def _calculate_avellaneda_prices(self, price):
        """
        [輔助方法] 計算 Avellaneda-GLFT 模型下的公允價格和最佳報價
        使用 Guéant–Lehalle–Fernandez-Tapia (GLFT) 閉式解
        """
        
        # 1. 更新庫存 (淨持倉量)
        self.inventory = self.long_position - self.short_position
        
        # 2. 準備參數轉換 (轉為 Tick 單位以符合 GLFT 離散模型公式)
        try:
            tick_size = 10 ** (-self.price_precision)
        except:
            tick_size = 0.0001 # Fallback
            
        # Sigma (百分比) -> Sigma (Tick)
        # 假設 self.sigma 是單位時間的對數收益率標準差 (e.g. 1小時)
        # 價格波動 (絕對值) ~ Price * Sigma
        # Sigma_tick = (Price * Sigma) / Tick_Size
        sigma_tick = (price * self.sigma) / tick_size
        
        # k (per $) -> k (per Tick)
        # k_tick = k_$ * Tick_Size
        k_tick = self.k * tick_size
        
        # Gamma (per $) -> Gamma (per Tick)
        # Gamma_tick = Gamma_$ * Tick_Size
        gamma_tick = self.gamma * tick_size
        
        # 3. 計算 GLFT 係數 c1, c2
        # delta (價格單位) 設為 1.0 (代表 1 Tick)
        c1, c2 = compute_glft_params(gamma_tick, sigma_tick, None, self.A, k_tick, delta_price=1.0)
        
        # 4. 計算 Spread 和 Skew (Tick 單位)
        # 根據 PDF:
        # half_spread = c1 + (Delta/2) * sigma * c2
        # skew = sigma * c2
        # 這裡 Delta=1 (Tick)
        
        half_spread_tick = c1 + 0.5 * sigma_tick * c2
        skew_tick = sigma_tick * c2
        
        # 5. 轉回價格單位
        half_spread_price = half_spread_tick * tick_size
        skew_price = skew_tick * tick_size

        # --- 最小價差保護 (Min Spread Protection) ---
        # 確保 half_spread 不小於設定的百分比 (例如 0.02% 的一半)
        min_abs_half_spread = price * (MIN_SPREAD_PERCENT / 100) / 2 
        if half_spread_price < min_abs_half_spread:
            logger.debug(f"觸發最小價差保護: 計算值 {half_spread_price:.8f} < 最小保護 {min_abs_half_spread:.8f}, 調整為 {min_abs_half_spread:.8f}")
            half_spread_price = min_abs_half_spread
        # --- End Min Spread Protection ---
        
        # 6. 計算報價
        # GLFT 定義的 Reservation Price (公允價) 偏移量是 Skew * q
        # 如果 q > 0 (多頭)，Reserve Price 下降，鼓勵賣出
        self.reserve_price = price - (skew_price * self.inventory)
        
        self.best_bid = self.reserve_price - half_spread_price
        self.best_ask = self.reserve_price + half_spread_price
        
        # 價格保護
        self.best_bid = max(0.0, self.best_bid) 
        self.best_ask = max(0.0, self.best_ask) 
        
        # 計算百分比價差
        spread_percent = (2 * half_spread_price / price) * 100
        
        logger.info(f"GLFT: P={price:.4f}, Inv={self.inventory}, R={self.reserve_price:.4f}, "
                    f"Spread={2*half_spread_price:.4f} ({spread_percent:.4f}%), HS={half_spread_price:.4f}, Skew={skew_price:.6f}")
        
    def recalibrate_parameters(self):
        """定期根據收集的實時成交數據重新校準 A, k"""
        current_time = time.time()
        if current_time - self.last_calibration_time < CALIBRATION_INTERVAL:
            return

        # 檢查是否有足夠數據 (提高門檻到 200 筆)
        if len(self.public_trade_deltas) < 200:
            return
            
        # 提取數據 (複製以防並發修改)
        trades_snapshot = list(self.public_trade_deltas)
        
        # 只保留最近 300 秒的數據 (Rolling Window)
        # 改為 5 分鐘窗口，與校準間隔一致
        cutoff_time = current_time - 300
        recent_trades = [x for x in trades_snapshot if x[0] >= cutoff_time]
        
        # 如果最近 300秒數據太少 (< 200筆)，回退到使用更多數據
        if len(recent_trades) < 200:
            logger.info(f"最近 300s 數據不足 ({len(recent_trades)}筆)，擴大範圍使用最近 1000 筆...")
            recent_trades = trades_snapshot[-1000:] # 取最近 1000 筆
            
        if not recent_trades:
            return

        deltas = [x[1] for x in recent_trades]
        
        # 計算時間窗口 (秒)
        min_time = recent_trades[0][0]
        max_time = recent_trades[-1][0]
        time_span = max_time - min_time
        
        # 保護: 如果數據都在同一毫秒，time_span 可能為 0
        if time_span <= 0.1: time_span = 1.0
        
        # 執行校準
        # calibrate_from_deltas 返回的是 orders/sec
        new_A_sec, new_k = calibrate_from_deltas(deltas, time_span)
        
        if new_A_sec > 0 and new_k > 0:
            # 轉換為 orders/min
            self.A = new_A_sec * 60.0
            self.k = new_k
            self.last_calibration_time = current_time
            logger.info(f"!!! 參數實時更新 !!! A={self.A:.4f}/min, k={self.k:.4f} (Window={time_span:.1f}s, N={len(deltas)})")
            
            # 【新增】參數更新後，同步自動調整 Gamma
            self.auto_tune_gamma()
            
        else:
            logger.warning(f"實時校準失敗或參數無效，保持原值: A={self.A}, k={self.k}")

    def update_mid_price(self, side, price):
        self._calculate_avellaneda_prices(price)
        self.upper_price_long = self.upper_price_short = self.best_ask
        self.lower_price_long = self.lower_price_short = self.best_bid
        self.mid_price_long = self.mid_price_short = self.reserve_price


    async def place_long_orders(self, latest_price):
        """[覆寫] 根據 Avellaneda 的價格掛出多頭開倉和止盈單。"""
        try:
            self.update_mid_price('long', latest_price) 
            self.get_take_profit_quantity(self.long_position, 'long')

            if self.long_position > 0:
                if self.long_position > POSITION_THRESHOLD:
                    if self.sell_long_orders <= 0:
                        self.place_take_profit_order(self.ccxt_symbol, 'long', self.best_ask, self.long_initial_quantity)
                else:
                    self.cancel_orders_for_side('long')
                    self.place_take_profit_order(self.ccxt_symbol, 'long', self.best_ask, self.long_initial_quantity)
                    self.place_order('buy', self.best_bid, self.long_initial_quantity, False, 'long')
                    
                    logger.info(f"[A-Long] 止盈@{self.best_ask:.8f} | 補倉@{self.best_bid:.8f}")

        except Exception as e:
            logger.error(f"掛 Avellaneda 多頭訂單失敗: {e}")

    async def place_short_orders(self, latest_price):
        """[覆寫] 根據 Avellaneda 的價格掛出空頭開倉和止盈單。"""
        try:
            self.update_mid_price('short', latest_price) 
            self.get_take_profit_quantity(self.short_position, 'short')

            if self.short_position > 0:
                if self.short_position > POSITION_THRESHOLD:
                    if self.buy_short_orders <= 0:
                        self.place_take_profit_order(self.ccxt_symbol, 'short', self.best_bid, self.short_initial_quantity)
                else:
                    self.cancel_orders_for_side('short')
                    self.place_take_profit_order(self.ccxt_symbol, 'short', self.best_bid, self.short_initial_quantity)
                    self.place_order('sell', self.best_ask, self.short_initial_quantity, False, 'short')
                    
                    logger.info(f"[A-Short] 止盈@{self.best_bid:.8f} | 補倉@{self.best_ask:.8f}")

        except Exception as e:
            logger.error(f"掛 Avellaneda 空頭訂單失敗: {e}")
            

    async def adjust_grid_strategy(self):
        # --- 暖機檢查 (Warm-up Check) ---
        if not self.is_warmed_up:
            data_count = len(self.public_trade_deltas)
            required_count = 200
            
            if data_count < required_count:
                # 為了避免日誌刷屏，可以每隔幾次或一定時間輸出一次，這裡簡單處理每次輸出
                # 建議使用者如果覺得煩，可以自己加計時器控制 log 頻率
                logger.info(f"機器人暖機中: 收集實時成交數據 {data_count}/{required_count} ... 暫不掛單")
                return
            else:
                logger.info("數據收集完成，執行首次實時參數校準...")
                # 強制執行一次校準 (忽略時間間隔檢查，因為這是第一次)
                # 這裡我們手動調用內部邏輯或重置時間
                self.last_calibration_time = 0 
                self.recalibrate_parameters()
                
                # 【新增】暖機完成後，立即自動調整一次 Gamma
                self.auto_tune_gamma()
                
                self.is_warmed_up = True
                logger.info("暖機完成！開始執行 GLFT 造市策略。")
        # --- End Warm-up Check ---

        self.check_and_reduce_positions()
        
        # 嘗試進行實時參數校準
        self.recalibrate_parameters()
        
        current_time = time.time()
        
        # --- 訂單定期刷新機制 (Re-quoting) ---
        # GLFT 策略需要緊跟市場價格，定期撤單重掛是標準操作
        if current_time - self.last_order_refresh_time > ORDER_REFRESH_TIME:
            logger.info(f"達到刷新週期 ({ORDER_REFRESH_TIME}s)，撤銷舊單以重置報價...")
            self.cancel_orders_for_side('long')
            self.cancel_orders_for_side('short')
            self.last_order_refresh_time = current_time
        
        latest_price = self.latest_price
        
        if latest_price:
            self.update_mid_price(None, latest_price) 

        if self.long_position == 0:
            await self.initialize_long_orders()
        else:
            if not (self.long_position > POSITION_THRESHOLD and current_time - self.last_long_order_time < ORDER_COOLDOWN_TIME):
                await self.place_long_orders(latest_price)

        if self.short_position == 0:
            await self.initialize_short_orders()
        else:
            if not (self.short_position > POSITION_THRESHOLD and current_time - self.last_short_order_time < ORDER_COOLDOWN_TIME):
                await self.place_short_orders(latest_price)


# 7. 主程序入口
async def main():
    # 步驟 1: 自動計算 Avellaneda 參數，並覆蓋全局變量
    global AVE_SIGMA, AVE_A, AVE_K
    AVE_SIGMA, AVE_A, AVE_K = auto_calculate_params(COIN_NAME, Taker_Fee_Rate)

    # 步驟 2: 實例化機器人，使用計算後的參數
    bot = AvellanedaGridBot(
        API_KEY, API_SECRET, COIN_NAME,
        GRID_SPACING, INITIAL_QUANTITY, LEVERAGE,
        TAKE_PROFIT_SPACING,
        gamma=AVE_GAMMA, A=AVE_A, k=AVE_K, sigma=AVE_SIGMA, T_end=AVE_T_END
    )
    
    # 步驟 3: 運行機器人
    await bot.run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("機器人已由用戶停止。")
    except Exception as e:
        logger.critical(f"主程序發生致命錯誤: {e}")