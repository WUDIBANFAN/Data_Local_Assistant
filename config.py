#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
================================================================================
config.py — 全局配置中心
保时捷测试工单智能分析系统 | 模块化重构版 v4.0
================================================================================
集中管理所有路径、常量、环境变量，供其他模块引用。
================================================================================
"""

import os
import sys
import tempfile
from pathlib import Path
from dotenv import load_dotenv

# ============================================================
# 1. 项目根目录（基于本文件位置自动推断，不硬编码）
# ============================================================
PROJECT_DIR = str(Path(__file__).resolve().parent)

# ============================================================
# 2. CSV 数据目录（硬编码 OneDrive 路径）
# ============================================================
CSV_DIR = (
    r"C:\Users\GRHYSFH\OneDrive - Dr. Ing. h.c. F. Porsche AG"
    r"\Sinan_ Infotainment@MLBevo China - Cross - Validation and Verification"
    r"\09_Error Management\CSV"
)
# 本地测试 CSV 备用目录
CSV_EXAMPLE_DIR = os.path.join(PROJECT_DIR, "csv_example")

# ============================================================
# 3. 缓存 / 临时文件路径
# ============================================================
CACHE_DIR = os.path.join(PROJECT_DIR, "CSV_DATA_CACHE")
CACHE_META_FILE = os.path.join(CACHE_DIR, "cache_meta.json")
DB_PATH = os.path.join(tempfile.gettempdir(), "porsche_tickets_analysis.db")
LOG_PATH = os.path.join(CACHE_DIR, "load_progress.txt")

# ============================================================
# 4. CSV 解析配置（德式格式）
# ============================================================
CSV_PATTERN = "RechercheExport_*.csv"               # 文件名匹配模式
CSV_ENCODING = "utf-8-sig"                           # 首选编码（带 BOM）
CSV_FALLBACK_ENCODINGS = ["utf-8", "latin-1", "gbk"] # 兜底编码列表
CSV_DELIMITER = ";"                                  # 德式分号分隔
CSV_DECIMAL = ","                                    # 德式逗号小数点
CSV_RETRY_MAX = 5                                    # OneDrive 锁最大重试次数
CSV_RETRY_DELAY = 1.0                                # 基础重试间隔（秒）

# ============================================================
# 5. DuckDB 配置
# ============================================================
DUCKDB_TABLE_NAME = "tickets"                        # 主表名
DUCKDB_MAX_DISPLAY_ROWS = 100                        # SQL 结果最多展示行数

# ============================================================
# 6. AI / LLM 配置（从 .env 读取）
# ============================================================
# 加载 .env 文件
_env_loaded = False
_env_path = os.path.join(PROJECT_DIR, ".env")
if os.path.exists(_env_path):
    load_dotenv(_env_path, override=True)
    _env_loaded = True
else:
    load_dotenv()
    _env_loaded = True


def get_llm_config() -> dict:
    """
    返回 LLM 配置字典，兼容 DeepSeek / OpenAI / 通义千问 等。
    优先使用 .env 中的配置。
    """
    api_key = os.getenv("LLM_API_KEY") or os.getenv("OPENAI_API_KEY") or ""

    # 根据 LLM_TYPE 或 base_url 自动推断
    llm_type = (os.getenv("LLM_TYPE") or "").lower()

    base_url = (
        os.getenv("DEEPSEEK_BASE_URL")
        or os.getenv("OPENAI_BASE_URL")
        or os.getenv("LLM_BASE_URL")
        or ""
    )

    if not base_url and llm_type == "deepseek":
        base_url = "https://api.deepseek.com/v1"

    model = (
        os.getenv("LLM_MODEL")
        or ("deepseek-chat" if llm_type == "deepseek" else "gpt-3.5-turbo")
    )

    return {
        "api_key": api_key,
        "model": model,
        "base_url": base_url.strip() if base_url else None,
        "temperature": float(os.getenv("LLM_TEMPERATURE", "0")),
        "max_tokens": int(os.getenv("LLM_MAX_TOKENS", "2000")),
        "llm_type": llm_type,
    }


def is_env_configured() -> bool:
    """检查必要的 LLM 环境变量是否已配置"""
    cfg = get_llm_config()
    return bool(cfg["api_key"])


# ============================================================
# 7. 确保必需的目录存在
# ============================================================
os.makedirs(CACHE_DIR, exist_ok=True)


# ============================================================
# 8. 模块级自检（导入时输出配置摘要）
# ============================================================
def _print_config_summary():
    """在终端输出配置摘要，便于调试"""
    try:
        sys.__stdout__.write(f"[config] PROJECT_DIR = {PROJECT_DIR}\n")
        sys.__stdout__.write(f"[config] CSV_DIR     = {CSV_DIR}\n")
        sys.__stdout__.write(f"[config] DB_PATH     = {DB_PATH}\n")
        sys.__stdout__.write(f"[config] LLM configured = {is_env_configured()}\n")
        try:
            sys.__stdout__.flush()
        except Exception:
            pass
    except Exception:
        pass


_print_config_summary()
