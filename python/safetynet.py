#========================================================================================================================
# File: safetynet.py
#========================================================================================================================
import asyncio
import time
from decimal import Decimal
from typing import Any, Dict, Optional, Tuple, Union
from cachetools import TTLCache
from web3 import AsyncWeb3
from eth_account import Account
from web3.exceptions import Web3Exception

from apiconfig import APIConfig
from configuration import Configuration

from loggingconfig import setup_logging
import logging
logger = setup_logging("SafetyNet", level=logging.INFO)

class SafetyNet:
    """
     safety system for risk management and transaction validation.
    """

    def __init__(
        self,
        web3: AsyncWeb3,
        configuration: Optional["Configuration"] = None,
        address: Optional[str] = None,
        account: Optional[Account] = None,
        apiconfig: Optional["APIConfig"] = None,
        marketmonitor: Optional[Any] = None,
    ):
        """
        Initialize Safety Net components.
        ... (rest of the docstring is the same) ...
        """
        self.web3: AsyncWeb3 = web3
        self.address: Optional[str] = address
        self.configuration: Optional["Configuration"] = configuration
        self.account: Optional[Account] = account
        self.apiconfig: Optional["APIConfig"] = apiconfig
        self.price_cache: TTLCache = TTLCache(maxsize=2000, ttl=self.configuration.SAFETYNET_CACHE_TTL) # Use configurable CACHE_TTL
        self.gas_price_cache: TTLCache = TTLCache(maxsize=1, ttl=self.configuration.SAFETYNET_GAS_PRICE_TTL) # Use configurable GAS_PRICE_CACHE_TTL
        self.marketmonitor: Optional[Any] = marketmonitor  # Store marketmonitor reference

        self.price_lock: asyncio.Lock = asyncio.Lock()
        logger.info("SafetyNet is reporting for duty 🛡️")
        time.sleep(1) # ensuring proper initialization

        # Safety checks cache
        self.safety_cache: TTLCache = TTLCache(maxsize=100, ttl=60)  # 1 minute cache # Keep safety_cache TTL hardcoded for now, or make configurable too if needed.

        # Load settings from config object
        if self.configuration:
            self.SLIPPAGE_CONFIG: Dict[str, float] = {
                "default": self.configuration.get_config_value("SLIPPAGE_DEFAULT", 0.1),
                "min": self.configuration.get_config_value("SLIPPAGE_MIN", 0.01),
                "max": self.configuration.get_config_value("SLIPPAGE_MAX", 0.5),
                "high_congestion": self.configuration.get_config_value("SLIPPAGE_HIGH_CONGESTION", 0.05),
                "low_congestion": self.configuration.get_config_value("SLIPPAGE_LOW_CONGESTION", 0.2),
            }
            self.GAS_CONFIG: Dict[str, Union[int, float]] = {
                "max_gas_price_gwei": self.configuration.get_config_value("MAX_GAS_PRICE_GWEI", 500),
                "min_profit_multiplier": self.configuration.get_config_value("MIN_PROFIT_MULTIPLIER", 2.0),
                "base_gas_limit": self.configuration.get_config_value("BASE_GAS_LIMIT", 21000)
            }
        else: # Defaults for testing when config class is not available.
             self.SLIPPAGE_CONFIG: Dict[str, float] = {
                "default":  0.1,
                "min": 0.01,
                "max": 0.5,
                "high_congestion":  0.05,
                "low_congestion": 0.2,
            }
             self.GAS_CONFIG: Dict[str, Union[int, float]] = {
                "max_gas_price_gwei":  500,
                "min_profit_multiplier": 2.0,
                "base_gas_limit":  21000
            }


    async def initialize(self) -> None:
        """Initialize Safety Net components."""
        try:
            # Initialize price cache (already done in __init__, no need to repeat unless TTL needs reset)
            # self.price_cache = TTLCache(maxsize=1000, ttl=self.configuration.SAFETYNET_CACHE_TTL)

            # Initialize gas price cache (already done in __init__, no need to repeat unless TTL needs reset)
            # self.gas_price_cache = TTLCache(maxsize=1, ttl=self.configuration.SAFETYNET_GAS_PRICE_TTL)

            # Initialize safety checks cache (already done in __init__, no need to repeat unless TTL needs reset)
            # self.safety_cache = TTLCache(maxsize=100, ttl=60)

            # Verify web3 connection
            if not self.web3:
                raise RuntimeError("Web3 not initialized in SafetyNet")

            # Test connection
            if not await self.web3.is_connected():
                raise RuntimeError("Web3 connection failed in SafetyNet")

            logger.debug("SafetyNet initialized successfully ✅")
        except RuntimeError as e:
            logger.critical(f"Safety Net initialization failed due to runtime error: {e}") # Specific RuntimeError for init failures
            raise
        except Exception as e:
            logger.critical(f"Safety Net initialization failed: {e}", exc_info=True) # Generic exception with traceback
            raise


    async def get_balance(self, account: Account) -> Decimal:
        """Get account balance with retries and caching."""
        cache_key = f"balance_{account.address}"
        if cache_key in self.price_cache:
            logger.debug("Balance fetched from cache.")
            return self.price_cache[cache_key]

        for attempt in range(3):
            try:
                balance = Decimal(await self.web3.eth.get_balance(account.address)) / Decimal("1e18")
                self.price_cache[cache_key] = balance
                logger.debug(f"Fetched balance: {balance} ETH")
                return balance
            except Web3Exception as e: # Catch Web3 specific exceptions
                logger.warning(f"Attempt {attempt+1} failed to fetch balance (Web3Error): {e}")
                await asyncio.sleep(2 ** attempt)
            except Exception as e: # Catch any other errors
                logger.error(f"Attempt {attempt+1} failed to fetch balance (Unexpected Error): {e}", exc_info=True) # Include traceback for unexpected errors
                await asyncio.sleep(2 ** attempt)

        logger.error("Failed to fetch account balance after multiple retries.")
        return Decimal("0")

    async def ensure_profit(
        self,
        transaction_data: Dict[str, Any],
        minimum_profit_eth: Optional[float] = None,
    ) -> bool:
        """ profit verification with dynamic thresholds and risk assessment."""
        try:
            real_time_price = await self.apiconfig.get_real_time_price(transaction_data['output_token'])
            if real_time_price is None:
                logger.error("Real-time price unavailable, cannot ensure profit.") # More informative log
                return False

            gas_cost_eth = self._calculate_gas_cost(
                Decimal(transaction_data["gas_price"]),
                transaction_data["gas_used"]
            )

            slippage = await self.adjust_slippage_tolerance()
            profit = await self._calculate_profit(
                transaction_data, real_time_price, slippage, gas_cost_eth
            )

            self._log_profit_calculation(transaction_data, real_time_price, gas_cost_eth, profit, minimum_profit_eth or 0.001)

            return profit > Decimal(minimum_profit_eth or 0.001)
        except KeyError as e:
            logger.error(f"Missing key in transaction data for profit check: {e}") # More specific error log
            return False
        except Exception as e:
            logger.error(f"Error in ensure_profit: {e}", exc_info=True) # Include traceback
            return False

    def _validate_gas_parameters(self, gas_price_gwei: Decimal, gas_used: int) -> bool:
        """Validate gas parameters against safety thresholds."""
        if gas_used == 0:
            logger.error("Gas used cannot be zero for transaction validation.") # More informative log
            return False
        if gas_price_gwei > self.GAS_CONFIG["max_gas_price_gwei"]:
            logger.warning(f"Gas price {gas_price_gwei} Gwei exceeds maximum threshold of {self.GAS_CONFIG['max_gas_price_gwei']} Gwei.") # More informative warning
            return False
        return True

    def _calculate_gas_cost(self, gas_price_gwei: Decimal, gas_used: int) -> Decimal:
        """Calculate total gas cost in ETH."""
        return gas_price_gwei * Decimal(gas_used) * Decimal("1e-9")

    async def _calculate_profit(
        self,
        transaction_data: Dict[str, Any],
        real_time_price: Decimal,
        slippage: float,
        gas_cost_eth: Decimal,
    ) -> Decimal:
        """Calculate expected profit considering slippage and gas costs."""
        expected_output = real_time_price * Decimal(transaction_data["amountOut"])
        input_amount = Decimal(transaction_data["amountIn"])
        slippage_adjusted_output = expected_output * (1 - Decimal(slippage))
        return slippage_adjusted_output - input_amount - gas_cost_eth

    def _log_profit_calculation(
        self,
        transaction_data: Dict[str, Any],
        real_time_price: Decimal,
        gas_cost_eth: Decimal,
        profit: Decimal,
        minimum_profit_eth: float,
    ) -> None:
        """Log detailed profit calculation metrics."""
        profitable = "Yes" if profit > Decimal(minimum_profit_eth) else "No"
        logger.debug(
            f"Profit Calculation Summary:\n"
            f"  Token: {transaction_data['output_token']}\n"
            f"  Real-time Price: {real_time_price:.6f} ETH\n"
            f"  Input Amount: {transaction_data['amountIn']:.6f} ETH\n"
            f"  Expected Output: {transaction_data['amountOut']:.6f} tokens\n"
            f"  Gas Cost: {gas_cost_eth:.6f} ETH\n"
            f"  Calculated Profit: {profit:.6f} ETH\n"
            f"  Minimum Required Profit: {minimum_profit_eth} ETH\n"
            f"  Profitable: {profitable}"
        )

    async def estimate_gas(self, transaction_data: Dict[str, Any]) -> int:
        """Estimate the gas required for a transaction."""
        try:
            gas_estimate = await self.web3.eth.estimate_gas(transaction_data)
            return gas_estimate
        except Exception as e:
            logger.error(f"Gas estimation failed: {e}", exc_info=True) # Include traceback
            return 0

    async def adjust_slippage_tolerance(self) -> float:
        """Adjust slippage tolerance based on network conditions."""
        try:
            congestion_level = await self.get_network_congestion()
            if congestion_level > 0.8:
                slippage = self.SLIPPAGE_CONFIG["high_congestion"]
            elif congestion_level < 0.2:
                slippage = self.SLIPPAGE_CONFIG["low_congestion"]
            else:
                slippage = self.SLIPPAGE_CONFIG["default"]
            slippage = min(
                max(slippage, self.SLIPPAGE_CONFIG["min"]), self.SLIPPAGE_CONFIG["max"]
            )
            logger.debug(f"Adjusted slippage tolerance to {slippage * 100:.2f}%") # Formatted percentage in log
            return slippage
        except Exception as e:
            logger.error(f"Error adjusting slippage tolerance: {e}", exc_info=True) # Include traceback
            return self.SLIPPAGE_CONFIG["default"]

    async def get_network_congestion(self) -> float:
        """Estimate the current network congestion level."""
        try:
            latest_block = await self.web3.eth.get_block("latest")
            gas_used = latest_block["gasUsed"]
            gas_limit = latest_block["gasLimit"]
            congestion_level = gas_used / gas_limit
            logger.debug(f"Network congestion level: {congestion_level * 100:.2f}%") # Formatted percentage in log
            return congestion_level
        except Exception as e:
            logger.error(f"Error fetching network congestion: {e}", exc_info=True) # Include traceback
            return 0.5 # Return a default congestion level if fetching fails, to avoid division by zero or None errors

    async def check_transaction_safety(
        self,
        tx_data: Dict[str, Any],
        check_type: str = 'all'
    ) -> Tuple[bool, Dict[str, Any]]:
        """Unified safety check method for transactions."""
        try:
             is_safe = True
             messages = []

             if check_type in ['all', 'gas']:
                gas_price = await self.get_dynamic_gas_price()
                if gas_price > self.configuration.MAX_GAS_PRICE_GWEI:
                     is_safe = False
                     messages.append(f"Gas price too high: {gas_price} Gwei, exceeds limit of {self.configuration.MAX_GAS_PRICE_GWEI} Gwei") # More informative message

             # Check profit potential
             if check_type in ['all', 'profit']:
                profit = await self._calculate_profit(
                    tx_data,
                    await self.apiconfig.get_real_time_price(tx_data['output_token']),
                    await self.adjust_slippage_tolerance(),
                    self._calculate_gas_cost(
                        Decimal(tx_data['gas_price']),
                        tx_data['gas_used']
                    )
                )
                if profit < self.configuration.MIN_PROFIT:
                     is_safe = False
                     messages.append(f"Insufficient profit: {profit:.6f} ETH, below minimum of {self.configuration.MIN_PROFIT} ETH") # More informative message

             # Check network congestion
             if check_type in ['all', 'network']:
                congestion = await self.get_network_congestion()
                if congestion > 0.8:
                     is_safe = False
                     messages.append(f"High network congestion: {congestion:.1%}, above threshold of 80%") # More informative message

             return is_safe, {
                'is_safe': is_safe,
                'gas_ok': is_safe if check_type not in ['all', 'gas'] else gas_price <= self.configuration.MAX_GAS_PRICE_GWEI,
                'profit_ok': is_safe if check_type not in ['all', 'profit'] else profit >= self.configuration.MIN_PROFIT,
                'slippage_ok': True, # Not yet implemented slippage checks
                'congestion_ok': is_safe if check_type not in ['all', 'network'] else congestion <= 0.8,
                'messages': messages
            }

        except Exception as e:
            logger.error(f"Safety check error: {e}", exc_info=True) # Include traceback
            return False, {'is_safe': False, 'messages': [str(e)]}

    async def stop(self) -> None:
         """Stops the 0xBuilder gracefully."""
         try:
            if self.apiconfig:
                await self.apiconfig.close()
            logger.info("Safety Net stopped successfully.")
         except Exception as e:
             logger.error(f"Error stopping safety net: {e}", exc_info=True) # Include traceback
             raise

    async def assess_transaction_risk(
        self,
        tx: Dict[str, Any],
        token_symbol: str,
        market_conditions: Optional[Dict[str, bool]] = None,
        price_change: float = 0,
        volume: float = 0
    ) -> Tuple[float, Dict[str, Any]]:
        """Centralized risk assessment with proper error handling."""
        try:
            risk_score = 1.0

            # Get market conditions if not provided and marketmonitor exists
            if not market_conditions and self.marketmonitor:
                market_conditions = await self.marketmonitor.check_market_conditions(tx.get("to", ""))
            elif not market_conditions:
                market_conditions = {}  # Default empty if no marketmonitor

            # Gas price impact
            gas_price = int(tx.get("gasPrice", 0))
            gas_price_gwei = float(self.web3.from_wei(gas_price, "gwei"))
            if gas_price_gwei > self.configuration.MAX_GAS_PRICE_GWEI:
                risk_score *= 0.7

            # Market conditions impact
            if market_conditions.get("high_volatility", False):
                risk_score *= 0.7
            if market_conditions.get("low_liquidity", False):
                risk_score *= 0.6
            if market_conditions.get("bullish_trend", False):
                risk_score *= 1.2

            # Price change impact
            if price_change > 0:
                risk_score *= min(1.3, 1 + (price_change / 100))

            # Volume impact
            if volume >= 1_000_000:
                risk_score *= 1.2
            elif volume <= 100_000:
                risk_score *= 0.8

            return risk_score, market_conditions

        except Exception as e:
            logger.error(f"Error in risk assessment: {e}", exc_debug=True) # Include traceback
            return 0.0, {}

    async def get_dynamic_gas_price(self) -> Decimal:
        """
        Fetch dynamic gas price with caching.
        ... (rest of the docstring is the same) ...
        """
        if self.gas_price_cache and self.gas_price_cache.get("gas_price"):
            return self.gas_price_cache["gas_price"]

        try:
            #Fetch gas price from the latest block
             latest_block = await self.web3.eth.get_block('latest')
             base_fee = latest_block.get("baseFeePerGas")

             if base_fee:
                  gas_price_wei =  base_fee * 2
                  gas_price_gwei = Decimal(self.web3.from_wei(gas_price_wei, 'gwei'))
             else:
                  gas_price_gwei =  Decimal(self.web3.from_wei(await self.web3.eth.gas_price, 'gwei'))


             #Cache
             self.gas_price_cache["gas_price"] = gas_price_gwei
             logger.debug(f"Fetched dynamic gas price: {gas_price_gwei} Gwei")
             return gas_price_gwei

        except Exception as e:
            logger.error(f"Error fetching dynamic gas price: {e}", exc_debug=True) # Include traceback
            return Decimal(str(self.configuration.get_config_value("MAX_GAS_PRICE_GWEI", 50))) # Default gas price

    async def validate_transaction(self, transaction_data: Dict[str, Any]) -> bool:
        """Validate transaction data before execution."""
        try:
            # Check for required fields
            required_fields = ["output_token", "amountOut", "amountIn", "gas_price", "gas_used"]
            for field in required_fields:
                if field not in transaction_data:
                    logger.error(f"Missing required field in transaction data for validation: {field}") # More informative log
                    return False

            # Validate gas parameters
            gas_price_gwei = Decimal(transaction_data["gas_price"])
            gas_used = transaction_data["gas_used"]
            if not self._validate_gas_parameters(gas_price_gwei, gas_used):
                return False

            return True
        except Exception as e:
            logger.error(f"Error validating transaction data: {e}", exc_info=True) # Include traceback
            return False