import asyncio
import time
import math
import logging
import os
# 假設 GridTradingBot 和所有必要的常量、logger 都從 as.py 導入
# 實際導入時請確保 as.py 檔案名稱正確
from bot import GridTradingBot, logger 

# ==================== Avellaneda 參數配置 ====================
# 這些參數可以與 as.py 中的其他配置放在一起，但為了清晰，放在這裡
AVE_GAMMA = 0.001     # 風險厭惡係數 (Risk Aversion, gamma)
AVE_ETA = 0.0001      # 交易成本係數 (Lambda / Eta)
AVE_SIGMA = 0.005     # 波動率估計 (Volatility, sigma)
AVE_T_END = 3600      # 交易時間週期 (T, Time to End, 3600秒 = 1小時)

# 假設 as.py 中的核心配置
API_KEY = "YOUR_API_KEY" 
API_SECRET = "YOUR_API_SECRET"
COIN_NAME = "XRP" 
GRID_SPACING = 0.0006
TAKE_PROFIT_SPACING = 0.0004
INITIAL_QUANTITY = 1
LEVERAGE = 20
POSITION_THRESHOLD = 500

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
        self.T_end = T_end          # 交易總時間 (簡化為固定週期)
        self.reserve_price = 0      # 內部公允價格 (Reserve Price)
        self.inventory = 0          # 淨持倉量 (Long - Short)
        self.best_bid = 0           # Avellaneda 最佳買入報價
        self.best_ask = 0           # Avellaneda 最佳賣出報價
        
    
    def _calculate_avellaneda_prices(self, price):
        """
        [輔助方法] 計算 Avellaneda 模型下的公允價格和最佳報價
        這個方法只做計算，不屬於 GridTradingBot 的既有方法，但會被覆寫的方法調用。
        """
        
        # 1. 更新庫存
        self.inventory = self.long_position - self.short_position
        
        # 2. 剩餘時間 T (T 簡化為固定週期 T_end)
        T = self.T_end
        
        # 3. 公允價格 (Reserve Price) 計算
        # R = S - q * gamma * sigma^2 * T
        self.reserve_price = price - self.inventory * self.gamma * (self.sigma**2) * T

        # 4. 最優報價寬度 (delta) 計算
        # Delta = 1/2 * gamma * sigma^2 * T + 1/gamma * ln(1 + gamma / eta)
        try:
            term1 = 0.5 * self.gamma * (self.sigma**2) * T
            term2 = (1 / self.gamma) * math.log(1 + self.gamma / self.eta)
            delta = term1 + term2
        except (ValueError, ZeroDivisionError):
            # 避免 math domain error 或除以零
            delta = self.grid_spacing * price * 0.5
            
        # 5. 計算最佳報價
        self.best_bid = self.reserve_price - delta
        self.best_ask = self.reserve_price + delta
        
        logger.debug(f"Avellaneda: R={self.reserve_price:.4f}, Inv={self.inventory:.2f}, Delta={delta:.6f}")
        
    
    # 3. 覆寫核心定價方法
    def update_mid_price(self, side, price):
        """
        [覆寫] 覆寫父類別的價格計算方法，改為執行 Avellaneda 計算。
        """
        
        # 執行 Avellaneda 計算，結果會儲存在 self.best_bid/ask 中
        self._calculate_avellaneda_prices(price)
        
        # 將 Avellaneda 的結果映射到父類別的屬性上，以便後續邏輯調用 (雖然我們覆寫了下單，但這樣更安全)
        # 報價上沿 (用於賣出/止盈)
        self.upper_price_long = self.upper_price_short = self.best_ask
        
        # 報價下沿 (用於買入/補倉)
        self.lower_price_long = self.lower_price_short = self.best_bid
        
        # 將 Reserve Price 設為中間價 (可選)
        self.mid_price_long = self.mid_price_short = self.reserve_price


    # 4. 覆寫多頭下單方法
    async def place_long_orders(self, latest_price):
        """
        [覆寫] 根據 Avellaneda 的價格掛出多頭開倉和止盈單。
        """
        try:
            # 確保價格已更新並計算 Avellaneda 報價
            self.update_mid_price('long', latest_price) 
            self.get_take_profit_quantity(self.long_position, 'long')

            if self.long_position > 0:
                # 保留父類別的裝死/冷卻邏輯
                if self.long_position > POSITION_THRESHOLD:
                    # 裝死模式下，使用 Avellaneda 的 Ask 報價作為止盈價
                    if self.sell_long_orders <= 0:
                        self.place_take_profit_order(self.ccxt_symbol, 'long', self.best_ask, self.long_initial_quantity)
                else:
                    # 正常模式：撤銷舊單，使用 Avellaneda 報價掛單
                    self.cancel_orders_for_side('long')
                    
                    # 1. 止盈單 (平多頭倉位): 在 Ask Price 賣出 (Reduce Only)
                    self.place_take_profit_order(self.ccxt_symbol, 'long', self.best_ask, self.long_initial_quantity)
                    
                    # 2. 補倉單 (開多頭倉位): 在 Bid Price 買入 (非 Reduce Only)
                    self.place_order('buy', self.best_bid, self.long_initial_quantity, False, 'long')
                    
                    logger.info(f"[A-Long] 止盈@{self.best_ask:.4f} | 補倉@{self.best_bid:.4f}")

        except Exception as e:
            logger.error(f"掛 Avellaneda 多頭訂單失敗: {e}")

    # 5. 覆寫空頭下單方法
    async def place_short_orders(self, latest_price):
        """
        [覆寫] 根據 Avellaneda 的價格掛出空頭開倉和止盈單。
        """
        try:
            # 確保價格已更新並計算 Avellaneda 報價
            self.update_mid_price('short', latest_price) 
            self.get_take_profit_quantity(self.short_position, 'short')

            if self.short_position > 0:
                # 保留父類別的裝死/冷卻邏輯
                if self.short_position > POSITION_THRESHOLD:
                    # 裝死模式下，使用 Avellaneda 的 Bid 報價作為止盈價
                    if self.buy_short_orders <= 0:
                        self.place_take_profit_order(self.ccxt_symbol, 'short', self.best_bid, self.short_initial_quantity)
                else:
                    # 正常模式：撤銷舊單，使用 Avellaneda 報價掛單
                    self.cancel_orders_for_side('short')
                    
                    # 1. 止盈單 (平空頭倉位): 在 Bid Price 買入 (Reduce Only)
                    self.place_take_profit_order(self.ccxt_symbol, 'short', self.best_bid, self.short_initial_quantity)
                    
                    # 2. 補倉單 (開空頭倉位): 在 Ask Price 賣出 (非 Reduce Only)
                    self.place_order('sell', self.best_ask, self.short_initial_quantity, False, 'short')
                    
                    logger.info(f"[A-Short] 止盈@{self.best_bid:.4f} | 補倉@{self.best_ask:.4f}")

        except Exception as e:
            logger.error(f"掛 Avellaneda 空頭訂單失敗: {e}")
            
# 6. 主程序入口
async def main():
    # 這裡實例化新的 AvellanedaGridBot
    bot = AvellanedaGridBot(
        API_KEY, API_SECRET, COIN_NAME,
        GRID_SPACING, INITIAL_QUANTITY, LEVERAGE,
        TAKE_PROFIT_SPACING,
        gamma=AVE_GAMMA, eta=AVE_ETA, sigma=AVE_SIGMA, T_end=AVE_T_END
    )
    await bot.run()


if __name__ == "__main__":
    asyncio.run(main())