"""自定义异常类"""

class TradingBotError(Exception):
    """基础异常"""

class InsufficientFundsError(TradingBotError):
    """资金不足"""

class RiskLimitExceededError(TradingBotError):
    """超出风控限制"""

class CircuitBreakerError(TradingBotError):
    """熔断触发"""

class DataFetchError(TradingBotError):
    """数据拉取失败"""

class OrderError(TradingBotError):
    """下单失败"""

class ModelNotTrainedError(TradingBotError):
    """模型未训练"""
