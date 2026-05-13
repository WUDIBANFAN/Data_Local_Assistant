#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
================================================================================
data_loader.py — 数据加载核心模块
保时捷测试工单智能分析系统 | 模块化重构版 v4.0
================================================================================
职责：
 - 扫描 CSV 目录（根目录 + 子目录 01-12），按时间范围匹配文件
 - 读取 CSV（带 OneDrive 文件锁重试机制）
 - 数据清洗（时间转换、空值处理、德式数字格式）
 - 批量导入 DuckDB
 - DuckDBWrapper 类（LangChain SQL Agent 兼容接口）
================================================================================
"""

import os
import re
import glob
import time
import traceback
from datetime import datetime, date
from typing import List, Tuple, Optional, Dict, Any

import pandas as pd
import duckdb
import streamlit as st

# [关键] 禁用 pandas 2.x 的自动 StringDtype 推断
# 否则 pd.read_csv / pd.concat 等操作会产生 StringDtype，导致 DuckDB 报错
try:
    pd.set_option("future.infer_string", False)
except Exception:
    pass

from config import (
    CSV_DIR,
    CSV_EXAMPLE_DIR,
    CSV_PATTERN,
    CSV_ENCODING,
    CSV_FALLBACK_ENCODINGS,
    CSV_DELIMITER,
    CSV_DECIMAL,
    CSV_RETRY_MAX,
    CSV_RETRY_DELAY,
    DUCKDB_TABLE_NAME,
    DUCKDB_MAX_DISPLAY_ROWS,
    CACHE_DIR,
    LOG_PATH,
)

# ------------- 模块级日志文件（复用于多次加载） -------------
_LOG_FILE = None


def _get_log_file():
    """懒加载日志文件句柄"""
    global _LOG_FILE
    if _LOG_FILE is None:
        os.makedirs(CACHE_DIR, exist_ok=True)
        _LOG_FILE = open(LOG_PATH, "w", encoding="utf-8")
    return _LOG_FILE


def _log(tag: str, message: str):
    """输出进度日志到文件 + 终端 stderr"""
    ts = datetime.now().strftime("%H:%M:%S.%f")[:12]
    line = f"[{ts}] [{tag}] {message}"
    try:
        fh = _get_log_file()
        fh.write(line + "\n")
        fh.flush()
    except Exception:
        pass
    try:
        import sys
        sys.stderr.write(line + "\n")
        sys.stderr.flush()
    except Exception:
        pass


# ============================================================
# 1. CSV 文件扫描
# ============================================================

def _get_scan_directories() -> List[str]:
    """
    构建扫描目录列表：根 CSV 目录 + 存在的月子目录 (01-12)。
    返回存在的目录路径列表。
    """
    dirs = []
    # 主目录
    if os.path.isdir(CSV_DIR):
        dirs.append(CSV_DIR)
    # 示例目录（本地测试用）
    if os.path.isdir(CSV_EXAMPLE_DIR):
        dirs.append(CSV_EXAMPLE_DIR)
    # 月子目录
    for m in range(1, 13):
        sub = os.path.join(CSV_DIR, f"{m:02d}")
        if os.path.isdir(sub):
            dirs.append(sub)
    return dirs


def _detect_skiprows_and_encoding_from_bytes(content: bytes) -> Tuple[int, str]:
    """
    从字节流内容检测 CSV 编码和应跳过的元数据行数。
    用于处理上传的文件（没有磁盘路径，无法用 open() 打开）。

    Returns:
        (skiprows, encoding): 跳过的行数和检测到的编码。
    """
    raw_lines = None
    enc = CSV_ENCODING
    for enc_candidate in [CSV_ENCODING] + CSV_FALLBACK_ENCODINGS:
        try:
            decoded = content[:10000].decode(enc_candidate)
            raw_lines = decoded.splitlines()
            enc = enc_candidate
            break
        except (UnicodeDecodeError, UnicodeError):
            continue

    if raw_lines is None:
        enc = "latin-1"
        decoded = content[:10000].decode(enc, errors="replace")
        raw_lines = decoded.splitlines()

    # 找真正的表头行
    for i, line in enumerate(raw_lines):
        stripped = line.strip()
        if not stripped:
            continue
        parts = stripped.split(CSV_DELIMITER)
        if len(parts) >= 5:
            return i, enc

    return 0, enc


def load_uploaded_files_to_duckdb(
    uploaded_files: list,
    db_path: str,
    progress_callback: callable = None,
) -> Tuple[str, "DuckDBWrapper", pd.DataFrame]:
    """
    从 Streamlit 上传的 CSV 文件加载数据到 DuckDB。
    自动检测编码和元数据头行（与 read_csv_with_retry 逻辑一致）。

    Args:
        uploaded_files: Streamlit UploadedFile 对象列表。
        db_path:        DuckDB 数据库路径。
        progress_callback: 可选进度回调 (current, total, message)。

    Returns:
        (summary_msg, DuckDBWrapper, merged_df)
    """
    if not uploaded_files:
        raise RuntimeError("未提供任何 CSV 文件")

    dfs = []
    loaded_names = []
    failed_names = []
    total = len(uploaded_files)

    for idx, uploaded in enumerate(uploaded_files):
        fname = uploaded.name
        if progress_callback:
            progress_callback(idx, total, f"正在读取: {fname}")
        try:
            # 先读取字节内容用于检测
            raw_bytes = uploaded.read()
            skip_n, best_enc = _detect_skiprows_and_encoding_from_bytes(raw_bytes)
            _log("DETECT", f"上传 {fname}: skip={skip_n}, enc={best_enc}")

            # 将字节流转为 StringIO（Seekable 以便 pd.read_csv 多次消费）
            import io
            text_stream = io.StringIO(raw_bytes.decode(best_enc, errors="replace"))

            df = pd.read_csv(
                text_stream,
                sep=CSV_DELIMITER,
                skiprows=skip_n,
                decimal=CSV_DECIMAL,
                on_bad_lines="skip",
            )
            df.columns = df.columns.str.strip()
            df = clean_dataframe(df)
            if df is not None and len(df) > 0:
                dfs.append(df)
                loaded_names.append(fname)
            else:
                failed_names.append(f"{fname} (空数据)")
        except Exception as e:
            failed_names.append(f"{fname} ({str(e)[:50]})")
            _log("SKIP", f"跳过上传文件 {fname}: {str(e)[:60]}")

    if not dfs:
        raise RuntimeError("所有上传的 CSV 均加载失败。")

    if progress_callback:
        progress_callback(total, total, "合并数据中...")
    full_df = merge_dataframes(dfs)

    if progress_callback:
        progress_callback(total, total, "写入数据库...")
    _safe_remove(db_path)

    # [安全保护] 从 Python 列表重建 DataFrame，彻底剥离所有扩展 dtype（如 StringDtype）
    # DuckDB 不支持 pandas 扩展类型，会导致 "Data type 'str' not recognized" 错误
    _safe_df = pd.DataFrame(
        {col: list(full_df[col]) for col in full_df.columns},
    )
    # 重建后字符串列会被推断为 object（标准 numpy），数值列保持 int64/float64

    con = duckdb.connect(db_path)
    try:
        con.execute(f"DROP TABLE IF EXISTS {DUCKDB_TABLE_NAME}")
        con.execute(f"CREATE TABLE {DUCKDB_TABLE_NAME} AS SELECT * FROM _safe_df")
        row_count = con.execute(f"SELECT COUNT(*) FROM {DUCKDB_TABLE_NAME}").fetchone()[0]
    finally:
        con.close()

    summary = (
        f"已加载 **{len(loaded_names)}** 个上传文件，"
        f"共 **{row_count:,}** 条工单"
    )
    if failed_names:
        summary += f"\n\n(!) 跳过 {len(failed_names)} 个文件"

    _log("LOAD", summary.replace("**", "").replace("\n", " | "))

    db = DuckDBWrapper(db_path, full_df)
    return summary, db, full_df


def _parse_date_from_filename(fname: str) -> Optional[date]:
    """
    从文件名提取日期。
    格式: RechercheExport_YYYY-MM-DD_HH_MM_SS.fff.csv
    返回 date 对象或 None。
    """
    m = re.search(r"(\d{4}-\d{2}-\d{2})", fname)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y-%m-%d").date()
    except ValueError:
        return None


def scan_csv_files(
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
) -> List[Tuple[str, float, str, date]]:
    """
    扫描所有 CSV 文件，返回按日期匹配的文件列表。
    每条记录: (完整路径, 修改时间戳, 文件名, 文件中提取的日期)
    按修改时间降序排列。

    Args:
        start_date: 起始日期（含），None 表示不限。
        end_date:   结束日期（含），None 表示不限。

    Returns:
        匹配的 CSV 文件列表。
    """
    candidates: List[Tuple[str, float, str, date]] = []
    scan_dirs = _get_scan_directories()

    for sd in scan_dirs:
        if not os.path.isdir(sd):
            continue
        try:
            for f in glob.glob(os.path.join(sd, CSV_PATTERN)):
                try:
                    mtime = os.path.getmtime(f)
                except OSError:
                    continue
                fname = os.path.basename(f)
                file_date = _parse_date_from_filename(fname)
                if file_date is None:
                    # 无法解析日期时默认包含
                    candidates.append((f, mtime, fname, date.today()))
                    continue
                # 日期筛选
                if start_date and file_date < start_date:
                    continue
                if end_date and file_date > end_date:
                    continue
                candidates.append((f, mtime, fname, file_date))
        except (FileNotFoundError, PermissionError):
            continue

    if not candidates:
        return []

    # 按修改时间降序
    candidates.sort(key=lambda x: x[1], reverse=True)
    return candidates


def find_all_csvs() -> List[Tuple[str, float, str, date]]:
    """扫描全部 CSV（不限日期），按修改时间降序"""
    return scan_csv_files(start_date=None, end_date=None)


# ============================================================
# 2. CSV 读取（带 OneDrive 锁重试）
# ============================================================

def _detect_skiprows_and_encoding(csv_path: str) -> Tuple[int, str]:
    """
    自动检测 CSV 文件应从第几行开始读取，并确定正确的编码。

    保时捷 CSV 格式特殊：文件开头可能有元数据行（如 "VERTRAULICH"），
    真正的表头行以分隔符分隔多列。需要跳过这些前置行。

    策略：
    1. 用 latin-1 读取前 20 行
    2. 找到第一行含分隔符且拆分后 > 1 列的行 = 表头行
    3. 表头行之前的所有行 = skiprows

    Returns:
        (skiprows, encoding): 跳过的行数和检测到的编码。
    """
    # 先检测编码
    for enc in [CSV_ENCODING] + CSV_FALLBACK_ENCODINGS:
        try:
            with open(csv_path, "r", encoding=enc) as fh:
                raw_lines = [fh.readline() for _ in range(20)]
            break
        except (UnicodeDecodeError, UnicodeError):
            continue
    else:
        enc = "latin-1"
        with open(csv_path, "r", encoding=enc) as fh:
            raw_lines = [fh.readline() for _ in range(20)]

    # 找真正的表头行
    for i, line in enumerate(raw_lines):
        stripped = line.strip()
        if not stripped:
            continue
        parts = stripped.split(CSV_DELIMITER)
        if len(parts) >= 5:
            # 这是真正的表头行
            return i, enc

    # 回退：不跳过任何行
    return 0, enc


def read_csv_with_retry(
    csv_path: str,
    max_retries: int = CSV_RETRY_MAX,
    retry_delay: float = 0,
) -> pd.DataFrame:
    """
    从磁盘读取一个 CSV 文件，带 OneDrive 锁重试机制。

    优化要点：
    - 跳过编码链式尝试（_detect_skiprows_and_encoding 已准确定位编码）
    - 仅当 OneDrive 锁时才重试，非锁错误直接报错
    - 去掉低效的 low_memory=False

    Args:
        csv_path:    CSV 文件完整路径。
        max_retries: 最大重试次数（仅针对锁错误）。
        retry_delay: 基础重试间隔（秒），0 表示不等待。

    Returns:
        pandas DataFrame（列名已去空格）。

    Raises:
        RuntimeError: 所有重试耗尽后仍然失败。
    """
    fname = os.path.basename(csv_path)

    # 一次性检测 skiprows 和编码
    skip_n, best_enc = _detect_skiprows_and_encoding(csv_path)
    _log("DETECT", f"{fname}: skip={skip_n}, enc={best_enc}")

    for attempt in range(1, max_retries + 1):
        try:
            df = pd.read_csv(
                csv_path,
                sep=CSV_DELIMITER,
                encoding=best_enc,
                decimal=CSV_DECIMAL,
                skiprows=skip_n,
                on_bad_lines="skip",
                # low_memory=False 去掉（默认同行为，避免混淆）
            )

            if df is None or len(df) == 0:
                raise ValueError("读取后无数据")
            if len(df.columns) == 0:
                raise ValueError("未能解析出任何列")

            # 标准化列名（去除首尾空格）
            df.columns = df.columns.str.strip()

            _log("PARSE", f"{fname}: {len(df)} 行, {len(df.columns)} 列")
            return df

        except (PermissionError, OSError) as e:
            if attempt < max_retries:
                _log("LOCK", f"{fname} 被锁定, 重试 {attempt}/{max_retries}")
                if retry_delay:
                    time.sleep(retry_delay * attempt)
            else:
                raise RuntimeError(
                    f"CSV 文件被 OneDrive 锁定，重试 {max_retries} 次后仍失败: {fname}"
                ) from e

        except Exception as e:
            # 非锁错误，不再重试（直接失败）
            raise RuntimeError(
                f"CSV 读取失败: {fname} — {str(e)[:300]}"
            ) from e

    # 理论上不可达
    raise RuntimeError(f"未知错误: {fname}")


# ============================================================
# 3. 数据清洗
# ============================================================

def _is_string_like(dtype) -> bool:
    """判断 dtype 是否为字符串类型（兼容 pandas 1.x 和 2.x）"""
    return dtype == object or str(dtype) in ("object", "string", "str")


def _sample_looks_numeric(col_str) -> bool:
    """
    快速采样检测：取前 100 个非空值，检查是否大多像数字。
    用于跳过长文本列避免昂贵的全量 regex 操作。
    """
    sample = col_str.dropna().head(100).astype(str)
    if len(sample) < 5:
        return False
    # 跳过明显是文本的列（平均长度 > 50）
    if sample.str.len().mean() > 50:
        return False
    # 分词数 > 2 的基本是文本
    if sample.str.split().str.len().mean() > 2:
        return False
    # 检查原始值中数字字符占比（避免将 "Person_0" 误转为 0）
    cleaned = sample.str.replace(r"[^\d.\-]", "", regex=True)
    non_empty = cleaned.str.len() > 0
    if non_empty.mean() < 0.3:
        return False
    digit_ratio = cleaned.str.len() / sample.str.len().clip(lower=1)
    if digit_ratio[non_empty].mean() < 0.5:
        return False
    return True


def _try_numeric_conversion(col_series):
    """
    尝试将 Series 转换为数值。
    策略（按开销递增）：
    1. 直接 pd.to_numeric(coerce)
    2. 替换逗号为小数点后再试
    3. 去除非数字字符后再试
    4. 处理 Excel ="value" 格式后重试
    返回 (converted_series, success_ratio) 或 (None, 0)
    """
    col_str = col_series.astype(str)

    # 策略 1: 直接转换
    converted = pd.to_numeric(col_str, errors="coerce")
    ratio = converted.notna().mean()
    if ratio > 0.3:
        return converted, ratio

    # 策略 2: 德式逗号 → 小数点
    cleaned = col_str.str.replace(",", ".", regex=False)
    converted = pd.to_numeric(cleaned, errors="coerce")
    ratio = converted.notna().mean()
    if ratio > 0.3:
        return converted, ratio

    # 策略 3: 去除非数字字符（仅对局部采样做，确认值得后再全量）
    if not _sample_looks_numeric(col_str):
        return None, 0
    cleaned = col_str.str.replace(r"[^\d.\-]", "", regex=True)
    converted = pd.to_numeric(cleaned, errors="coerce")
    ratio = converted.notna().mean()
    if ratio > 0.3:
        return converted, ratio

    # 策略 4: 处理 Excel ="value" 格式
    if col_str.str.contains(r'^="', na=False).any():
        cleaned = col_str.str.replace(r'^="(.*)"$', r"\1", regex=True)
        converted = pd.to_numeric(cleaned, errors="coerce")
        ratio = converted.notna().mean()
        if ratio > 0.3:
            return converted, ratio

    return None, 0


def clean_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    """
    通用数据清洗：
    - [修复] 所有 StringDtype 强制转为 object（DuckDB 1.5.2 不兼容 StringDtype）
    - 智能数值列转换（按开销递增的多级策略）
    - 自动识别时间列并转换为 datetime
    - 跳过明显是长文本的列

    Args:
        df: 原始 DataFrame。

    Returns:
        清洗后的 DataFrame（就地修改，也返回同一引用）。
    """
    if df is None or len(df) == 0:
        return df

    # [Bug2修复] 将所有 StringDtype / string 类型的列强制转为 object
    # DuckDB 不支持 pandas StringDtype，会导致 "Data type 'str' not recognized" 错误
    for col in df.columns:
        try:
            dtype_str = str(df[col].dtype)
            # 兼容 pandas 1.x/2.x 的各种 StringDtype 表示形式
            if (pd.api.types.is_string_dtype(df[col].dtype)
                    or dtype_str in ("string", "str", "string[python]", "string[pyarrow]",
                                     "StringDtype", "string[object]")):
                df[col] = df[col].astype(object)
        except Exception:
            pass

    # --- 数值列智能转换（从快到慢逐步尝试）---
    for col in df.columns:
        if not _is_string_like(df[col].dtype):
            continue
        converted, _ = _try_numeric_conversion(df[col])
        if converted is not None:
            df[col] = converted

    # --- 时间列自动转换（只对非 datetime 的列做）---
    time_keywords = ["time", "date", "ts", "timestamp"]
    time_columns = [
        c for c in df.columns
        if any(k in c.lower() for k in time_keywords)
    ]
    for col in time_columns:
        # 已转换好的跳过
        if not _is_string_like(df[col].dtype):
            continue
        try:
            # 快速采样检查：前 20 行包含日期特征？
            sample = df[col].dropna().astype(str).head(20)
            if len(sample) < 2:
                continue
            # 简单启发：包含 "-" 或 ":" 的可能是日期
            if not sample.str.contains(r"[-/:T]", regex=True).any():
                continue
            df[col] = pd.to_datetime(df[col], errors="coerce", dayfirst=True)
        except Exception:
            pass

    return df


def merge_dataframes(dfs: List[pd.DataFrame]) -> pd.DataFrame:
    """
    合并多个 DataFrame。
    如果列不完全相同：
    - 取所有列的并集
    - 缺失的列填充 NaN
    - 去重（基于 Number 列，如果有的话）

    Args:
        dfs: DataFrame 列表。

    Returns:
        合并后的单一 DataFrame。
    """
    if not dfs:
        return pd.DataFrame()

    if len(dfs) == 1:
        return dfs[0].copy()

    try:
        merged = pd.concat(dfs, ignore_index=True, sort=False)

        # 基于工单编号去重
        if "Number" in merged.columns:
            before = len(merged)
            merged = merged.drop_duplicates(subset=["Number"], keep="last")
            after = len(merged)
            if before != after:
                _log("DEDUP", f"去重: {before} → {after} (移除 {before - after} 条)")

        return merged
    except Exception as e:
        _log("MERGE", f"合并失败: {e}，返回拼接结果")
        return pd.concat(dfs, ignore_index=True, sort=False)


# ============================================================
# 4. 加载数据到 DuckDB
# ============================================================

def _safe_remove(path: str):
    """安全删除文件，忽略所有异常"""
    try:
        if os.path.exists(path):
            os.remove(path)
    except Exception:
        pass


def load_csvs_to_duckdb(
    csv_paths: List[str],
    db_path: str,
    progress_callback: callable = None,
) -> Tuple[str, "DuckDBWrapper", pd.DataFrame]:
    """
    批量读取 CSV 文件并写入 DuckDB。
    作为单个 DuckDBWrapper 返回，供 LangChain Agent 使用。

    优化：直接使用已加载的 DataFrame，避免 DuckDB 重复读取写回。

    Args:
        csv_paths: CSV 文件路径列表。
        db_path:   DuckDB 数据库文件路径。
        progress_callback: 可选进度回调 (current, total, message)。

    Returns:
        (summary_msg, DuckDBWrapper, merged_df)

    Raises:
        RuntimeError: 所有 CSV 加载失败。
    """
    if not csv_paths:
        raise RuntimeError("未提供任何 CSV 文件路径")

    dfs = []
    loaded_files = []
    failed_files = []
    total = len(csv_paths)

    for idx, fp in enumerate(csv_paths):
        fname = os.path.basename(fp)
        if progress_callback:
            progress_callback(idx, total, f"正在读取: {fname}")
        try:
            df = read_csv_with_retry(fp)
            df = clean_dataframe(df)
            if df is not None and len(df) > 0:
                dfs.append(df)
                loaded_files.append(fname)
            else:
                failed_files.append(f"{fname} (空数据)")
        except Exception as e:
            failed_files.append(f"{fname} ({str(e)[:50]})")
            _log("SKIP", f"跳过 {fname}: {str(e)[:60]}")

    if not dfs:
        raise RuntimeError(
            f"所有 CSV 加载均失败。"
            + (f" 失败列表: {failed_files}" if failed_files else "")
        )

    # 合并所有 DataFrame
    if progress_callback:
        progress_callback(total, total, "合并数据中...")
    full_df = merge_dataframes(dfs)

    # 写入 DuckDB（仅用于 SQL Agent，直接使用 full_df 做 DuckDBWrapper）
    if progress_callback:
        progress_callback(total, total, "写入数据库...")
    _safe_remove(db_path)

    # [安全保护] 从 Python 列表重建 DataFrame，彻底剥离所有扩展 dtype（如 StringDtype）
    # DuckDB 不支持 pandas 扩展类型，会导致 "Data type 'str' not recognized" 错误
    _safe_df = pd.DataFrame(
        {col: list(full_df[col]) for col in full_df.columns},
    )

    con = duckdb.connect(db_path)
    try:
        con.execute(f"DROP TABLE IF EXISTS {DUCKDB_TABLE_NAME}")
        con.execute(f"CREATE TABLE {DUCKDB_TABLE_NAME} AS SELECT * FROM _safe_df")
        row_count = con.execute(f"SELECT COUNT(*) FROM {DUCKDB_TABLE_NAME}").fetchone()[0]
    finally:
        con.close()

    summary = (
        f"已加载 **{len(loaded_files)}** 个文件，"
        f"共 **{row_count:,}** 条工单"
    )
    if failed_files:
        summary += f"\n\n(!) 跳过 {len(failed_files)} 个文件"

    _log("LOAD", summary.replace("**", "").replace("\n", " | "))

    # 直接使用 full_df 创建 DuckDBWrapper（避免从 DuckDB 读回）
    db = DuckDBWrapper(db_path, full_df)
    return summary, db, full_df


# ============================================================
# 5. DuckDBWrapper — LangChain SQL Agent 兼容接口
# ============================================================

class DuckDBWrapper:
    """
    纯 DuckDB 原生包装器，完全绕过 SQLAlchemy。
    提供 LangChain create_sql_agent 所需的所有接口：
      - run(command, fetch)      → 执行 SQL 并返回字符串
      - run_no_throw(command)    → 不抛异常的 run
      - get_table_info(names)    → 返回表结构 DDL + 样本行
      - get_usable_table_names() → 返回可用表名列表
      - dialect                  → "duckdb"
      - close()                  → 关闭连接

    特性：
    - 自动重连（防止意外关闭）
    - 只读安全（LangChain 层面的保护由 prompt 实现）
    - Windows 路径兼容
    """

    def __init__(self, db_path: str, sample_df: Optional[pd.DataFrame] = None):
        self._db_path = db_path
        self._table_names = [DUCKDB_TABLE_NAME]
        self._closed = False
        try:
            self._con = duckdb.connect(db_path, read_only=False)
        except Exception:
            # 兜底：内存数据库
            self._con = duckdb.connect(":memory:")
        self._table_info = self._build_table_info()

        # 附加样本行（帮助 LLM 理解数据）
        if sample_df is not None and len(sample_df) > 0:
            try:
                sample_rows = sample_df.head(3).to_string(max_colwidth=40)
                self._table_info += (
                    "\n\n/* 样本行 (前 3 行):\n" + sample_rows + "\n*/"
                )
            except Exception:
                pass

    # ---------- 连接管理 ----------

    def _ensure_open(self):
        """如果连接已关闭，自动重新打开"""
        if self._closed:
            try:
                self._con = duckdb.connect(self._db_path, read_only=False)
                self._closed = False
            except Exception:
                self._con = duckdb.connect(":memory:")
                self._closed = False

    def close(self):
        """安全关闭连接"""
        try:
            if hasattr(self, "_con") and not self._closed:
                self._con.close()
                self._closed = True
        except Exception:
            self._closed = True

    def __del__(self):
        self.close()

    # ---------- 表信息 ----------

    def _build_table_info(self) -> str:
        """构建 CREATE TABLE DDL 字符串（供 LLM 理解表结构）"""
        try:
            self._ensure_open()
            rows = self._con.execute(
                f"PRAGMA table_info('{DUCKDB_TABLE_NAME}')"
            ).fetchall()
            if not rows:
                return f"CREATE TABLE {DUCKDB_TABLE_NAME} ();"
            lines = [f"CREATE TABLE {DUCKDB_TABLE_NAME} ("]
            col_defs = []
            for row in rows:
                _cid, name, col_type, notnull, dflt_value, pk = row
                parts = [f'  "{name}" {col_type}']
                if notnull:
                    parts.append("NOT NULL")
                if pk:
                    parts.append("PRIMARY KEY")
                if dflt_value is not None:
                    parts.append(f"DEFAULT {dflt_value}")
                col_defs.append(" ".join(parts))
            lines.append(",\n".join(col_defs))
            lines.append(");")
            return "\n".join(lines)
        except Exception as e:
            return f"-- PRAGMA table_info 失败: {e}"

    def get_table_info(self, table_names=None) -> str:
        """返回表结构信息（LangChain 接口）"""
        return self._table_info

    def get_usable_table_names(self) -> List[str]:
        """返回可用表名列表（LangChain 接口）"""
        return list(self._table_names)

    @property
    def dialect(self) -> str:
        """数据库方言"""
        return "duckdb"

    # ---------- SQL 执行 ----------

    def run(self, command: str, fetch: str = "all") -> str:
        """
        执行 SQL 并返回文本结果（LangChain 接口）。

        Args:
            command: SQL 语句。
            fetch:   返回模式，固定为 "all"。

        Returns:
            格式化后的查询结果字符串。
        """
        try:
            self._ensure_open()
            result = self._con.execute(command)
            rows = result.fetchall()

            # 无结果集的语句（如 INSERT，实际不会出现）
            if result.description is None:
                rc = getattr(result, "rowcount", None)
                base = "命令执行成功"
                return base + (f"，影响 {rc} 行" if rc else "")

            if not rows:
                return "查询返回 0 行。"

            # 格式化输出
            cols = [desc[0] for desc in result.description]
            lines = [", ".join(cols)]
            for row in rows[:DUCKDB_MAX_DISPLAY_ROWS]:
                vals = [str(v) if v is not None else "NULL" for v in row]
                lines.append(", ".join(vals))
            if len(rows) > DUCKDB_MAX_DISPLAY_ROWS:
                lines.append(
                    f"\n... (共 {len(rows):,} 行，仅显示前 {DUCKDB_MAX_DISPLAY_ROWS} 行)"
                )
            return "\n".join(lines)

        except Exception as e:
            return f"SQL 执行错误: {type(e).__name__}: {str(e)[:300]}"

    def run_no_throw(self, command: str, fetch: str = "all") -> str:
        """不抛异常的 run（LangChain 接口）"""
        try:
            return self.run(command, fetch)
        except Exception as e:
            return f"Error: {type(e).__name__}: {str(e)[:300]}"


# ============================================================
# 6. 便捷函数：关闭全局数据库连接
# ============================================================

def close_global_connection():
    """
    关闭 session state 中的 DuckDBWrapper 连接（如果需要的话）。
    通常由主程序在需要重新加载时调用。
    """
    try:
        if "db" in st.session_state:
            try:
                st.session_state.db.close()
            except Exception:
                pass
            del st.session_state.db
    except Exception:
        pass
