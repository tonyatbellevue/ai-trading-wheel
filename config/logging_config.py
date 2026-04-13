"""日志配置"""
import sys
import os
from loguru import logger
from config.settings import LOGS_DIR

os.makedirs(LOGS_DIR, exist_ok=True)

def setup_logger(name: str = "trading_bot") -> None:
    logger.remove()
    logger.add(sys.stdout, level="INFO",
               format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan> - <level>{message}</level>")
    logger.add(
        os.path.join(LOGS_DIR, f"{name}.log"),
        level="DEBUG",
        rotation="1 day",
        retention="30 days",
        compression="zip",
        format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name} - {message}"
    )

setup_logger()
