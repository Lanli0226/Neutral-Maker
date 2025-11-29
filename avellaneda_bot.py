import asyncio
import time
import math
import logging
import os
# 假設 GridTradingBot 和所有必要的常量、logger 都從 bot.py 導入
from bot import GridTradingBot, logger 

# ==================== Avellaneda 參數配置 (修正版) ====================
# 【重要修正】參數已調整尺度，以確保 Delta 合理，防止 'invalid negative price'
# 假設 AVE_SIGMA (0.005) 是小時波動率，故 T_END 設為 1 小時單位
AVE_GAMMA = 10.0      # 風險厭惡係數 (從 0.001 調整至 10.0)
AVE_ETA = 100.0       # 交易成本係數 (從 0.0001 調整至 100.0)
AVE_SIGMA = 0.005     # 波動率估計 (Volatility, sigma)
AVE_T_END = 1         # 交易時間週期 (T, 調整為 1 小時)

# 假設 bot.py 中的核心配置
API_KEY = "" 
API_SECRET = ""
COIN_NAME = "XRP" 
GRID_SPACING = 0.0006
TAKE_PROFIT_SPACING = 0.0004
INITIAL_QUANTITY = 1
LEVERAGE = 20
POSITION_THRESHOLD = 500
ORDER_COOLDOWN_TIME = 60 

# ==================== Avellaneda 繼承類 ====================
class AvellanedaGridBot(GridTradingBot):
    
    def __init__(self, api_key, api_secret, coin_name, grid_spacing, initial_quantity, leverage, 
                 take_profit_spacing=None, gamma=AVE_GAMMA, eta=AVE_ETA, sigma=AVE_SIGMA, T_end=AVE_T_END):
        
        # 1. 呼叫父類別的初始化方法
        super().__init__(api_key, api_secret, coin_name, grid_spacing, initial_quantity, leverage, take_profit_spacing)
        
        # 2. 初始化 Avellaneda 專有參數
        self.gamma = gamma          # 風險厭惡係數
        self.eta = eta              # 交易成本係數
        self.sigma = sigma          # 波動率估計
        self.T_end = T_end          # 交易總時間 (單位：小時)
        self.reserve_price = 0      # 內部公允價格 (Reserve Price)
        self.inventory = 0          # 淨持倉量 (Long - Short)
        self.best_bid = 0           # Avellaneda 最佳買入報價
        self.best_ask = 0           # Avellaneda 最佳賣出報價
        
    
    def _calculate_avellaneda_prices(self, price):
        """
        [輔助方法] 計算 Avellaneda 模型下的公允價格和最佳報價
        """
        
        # 1. 更新庫存 (淨持倉量)
        self.inventory = self.long_position - self.short_position
        
        # 2. 剩餘時間 T (T 簡化為固定週期 T_end)
        T = self.T_end
        
        # 3. 公允價格 (Reserve Price) 計算: R = S - q * gamma * sigma^2 * T
        self.reserve_price = price - self.inventory * self.gamma * (self.sigma**2) * T

        # 4. 最優報價寬度 (delta) 計算: Delta = 1/2 * gamma * sigma^2 * T + 1/gamma * ln(1 + gamma / eta)
        try:
            # 修正後的參數將使 Delta 保持在 0.01 左右，避免負價格
            term1 = 0.5 * self.gamma * (self.sigma**2) * T
            term2 = (1 / self.gamma) * math.log(1 + self.gamma / self.eta)
            delta = term1 + term2
        except (ValueError, ZeroDivisionError):
            delta = self.grid_spacing * price * 0.5
            
        # 5. 計算最佳報價
        self.best_bid = self.reserve_price - delta
        self.best_ask = self.reserve_price + delta
        
        # 將日誌精確度提高到 8 位，以便觀察微小變動
        logger.info(f"Avellaneda: R={self.reserve_price:.8f}, Inv={self.inventory:.2f}, Delta={delta:.8f}")
        
    
    # 3. 覆寫核心定價方法
    def update_mid_price(self, side, price):
        """[覆寫] 執行 Avellaneda 計算並映射到父類別屬性。"""
        self._calculate_avellaneda_prices(price)
        self.upper_price_long = self.upper_price_short = self.best_ask
        self.lower_price_long = self.lower_price_short = self.best_bid
        self.mid_price_long = self.mid_price_short = self.reserve_price


    # 4. 覆寫多頭下單方法
    async def place_long_orders(self, latest_price):
        """[覆寫] 根據 Avellaneda 的價格掛出多頭開倉和止盈單。"""
        try:
            # 在 adjust_grid_strategy 中已調用 update_mid_price，這裡保留是為了安全
            self.update_mid_price('long', latest_price) 
            self.get_take_profit_quantity(self.long_position, 'long')

            if self.long_position > 0:
                if self.long_position > POSITION_THRESHOLD:
                    if self.sell_long_orders <= 0:
                        self.place_take_profit_order(self.ccxt_symbol, 'long', self.best_ask, self.long_initial_quantity)
                else:
                    # 正常模式：強制撤銷舊單，使用 Avellaneda 報價掛新單
                    self.cancel_orders_for_side('long')
                    self.place_take_profit_order(self.ccxt_symbol, 'long', self.best_ask, self.long_initial_quantity)
                    self.place_order('buy', self.best_bid, self.long_initial_quantity, False, 'long')
                    
                    logger.info(f"[A-Long] 止盈@{self.best_ask:.8f} | 補倉@{self.best_bid:.8f}")

        except Exception as e:
            logger.error(f"掛 Avellaneda 多頭訂單失敗: {e}")

    # 5. 覆寫空頭下單方法
    async def place_short_orders(self, latest_price):
        """[覆寫] 根據 Avellaneda 的價格掛出空頭開倉和止盈單。"""
        try:
            # 在 adjust_grid_strategy 中已調用 update_mid_price，這裡保留是為了安全
            self.update_mid_price('short', latest_price) 
            self.get_take_profit_quantity(self.short_position, 'short')

            if self.short_position > 0:
                if self.short_position > POSITION_THRESHOLD:
                    if self.buy_short_orders <= 0:
                        self.place_take_profit_order(self.ccxt_symbol, 'short', self.best_bid, self.short_initial_quantity)
                else:
                    # 正常模式：強制撤銷舊單，使用 Avellaneda 報價掛新單
                    self.cancel_orders_for_side('short')
                    self.place_take_profit_order(self.ccxt_symbol, 'short', self.best_bid, self.short_initial_quantity)
                    self.place_order('sell', self.best_ask, self.short_initial_quantity, False, 'short')
                    
                    logger.info(f"[A-Short] 止盈@{self.best_bid:.8f} | 補倉@{self.best_ask:.8f}")

        except Exception as e:
            logger.error(f"掛 Avellaneda 空頭訂單失敗: {e}")
            

    # 6. 覆寫調整策略方法，**強制持續更新報價**
    async def adjust_grid_strategy(self):
        """
        [覆寫] 調整網格策略。強制每次價格更新時都執行 Avellaneda 計算和掛單。
        """
        self.check_and_reduce_positions()
        current_time = time.time()
        latest_price = self.latest_price
        
        # 2. 強制 Avellaneda 價格計算 (確保 R, Bid, Ask, Delta 被更新並記錄日誌)
        if latest_price:
            self.update_mid_price(None, latest_price) 

        # 3. 多頭邏輯 (無條件調用 place_long_orders，以強制撤單和更新報價)
        if self.long_position == 0:
            await self.initialize_long_orders()
        else:
            if not (self.long_position > POSITION_THRESHOLD and current_time - self.last_long_order_time < ORDER_COOLDOWN_TIME):
                await self.place_long_orders(latest_price)

        # 4. 空頭邏輯 (無條件調用 place_short_orders，以強制撤單和更新報價)
        if self.short_position == 0:
            await self.initialize_short_orders()
        else:
            if not (self.short_position > POSITION_THRESHOLD and current_time - self.last_short_order_time < ORDER_COOLDOWN_TIME):
                await self.place_short_orders(latest_price)


# 7. 主程序入口
async def main():
    bot = AvellanedaGridBot(
        API_KEY, API_SECRET, COIN_NAME,
        GRID_SPACING, INITIAL_QUANTITY, LEVERAGE,
        TAKE_PROFIT_SPACING,
        gamma=AVE_GAMMA, eta=AVE_ETA, sigma=AVE_SIGMA, T_end=AVE_T_END
    )
    await bot.run()


if __name__ == "__main__":
    asyncio.run(main())