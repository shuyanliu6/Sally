import os
from dotenv import load_dotenv

load_dotenv()

# API Keys
POLYGON_API_KEY = os.getenv("POLYGON_API_KEY", "")
FRED_API_KEY = os.getenv("FRED_API_KEY", "")
FMP_API_KEY = os.getenv("FMP_API_KEY", "")

# Polygon rate limiting
# Free tier = 5 req/min. Paid Starter = unlimited (set to 300).
POLYGON_REQUESTS_PER_MINUTE = int(os.getenv("POLYGON_REQUESTS_PER_MINUTE", "5"))
# How long to pause when a 429 is received before retrying (seconds)
POLYGON_RATE_LIMIT_BACKOFF = int(os.getenv("POLYGON_RATE_LIMIT_BACKOFF", "65"))
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# QuestDB connection
QUESTDB_HOST = os.getenv("QUESTDB_HOST", "localhost")
QUESTDB_PG_PORT = int(os.getenv("QUESTDB_PG_PORT", "8812"))
QUESTDB_USER = os.getenv("QUESTDB_USER", "admin")
QUESTDB_PASSWORD = os.getenv("QUESTDB_PASSWORD", "quest")
QUESTDB_DATABASE = os.getenv("QUESTDB_DATABASE", "qdb")

# SQLite path for portfolio state
SQLITE_PATH = os.getenv("SQLITE_PATH", "data/meta.db")

# Parquet storage
PARQUET_DIR = os.getenv("PARQUET_DIR", "data/parquet")

# Signal thresholds — 10Y Yield (DGS10)
YIELD_FAST_MA = 20
YIELD_SLOW_MA = 60
YIELD_STRONG_BULL_THRESHOLD = 4.0   # yield < this is strong bullish
YIELD_STRONG_BEAR_THRESHOLD = 5.0   # yield > this is strong bearish
YIELD_NEUTRAL_BAND_BPS = 0.10       # 10bps neutral band

# Signal thresholds — VIX
VIX_EXTREME_LOW = 15
VIX_LOW = 20
VIX_HIGH = 25
VIX_PANIC = 35

# Signal thresholds — Fed Balance Sheet (WALCL)
FED_BS_MA_WEEKS = 13

# Signal thresholds — Credit Spread (BAMLC0A0CM)
CREDIT_FAST_MA = 20
CREDIT_SLOW_MA = 60
CREDIT_STRONG_BEAR_OAS = 200  # bps

# Regime classification
REGIME_RISK_ON_MIN = 5
REGIME_MODERATE_ON_MIN = 2
REGIME_NEUTRAL_MIN = -1
REGIME_MODERATE_OFF_MIN = -4
# Below REGIME_MODERATE_OFF_MIN is RISK_OFF

REGIME_LABELS = {
    "RISK_ON": "RISK_ON",
    "MODERATE_ON": "MODERATE_ON",
    "NEUTRAL": "NEUTRAL",
    "MODERATE_OFF": "MODERATE_OFF",
    "RISK_OFF": "RISK_OFF",
}

# Portfolio
STOP_LOSS_ALERT_PCT = 0.05   # Alert when within 5% of stop
MODERATE_OFF_STOP_TIGHTEN_PCT = 0.05  # Tighten stops by 5% in MODERATE_OFF

# Dashboard
DASHBOARD_REFRESH_SECONDS = 60
SIGNAL_HISTORY_DAYS = 60

# Pipeline
PIPELINE_LOG_PATH = "logs/pipeline.log"
