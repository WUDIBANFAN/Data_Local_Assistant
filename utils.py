#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
================================================================================
utils.py — 工具函数模块
保时捷测试工单智能分析系统 | 模块化重构版 v4.0
================================================================================
职责：
 - 日志输出（终端 + 文件）
 - 数据库临时文件清理
 - CSV 导出（编码为 UTF-8-BOM 兼容 Excel）
 - 周报生成（统计 + LLM 总结）
 - 全局异常包装装饰器
================================================================================
"""

import os
import time
import atexit
import traceback
import sys as _sys
from datetime import datetime, timedelta, date
from typing import Optional, Callable

import streamlit as st
import pandas as pd

from config import (
    DB_PATH,
    CACHE_DIR,
    LOG_PATH,
)


# ============================================================
# 1. 日志系统
# ============================================================

_LOG_FILE = None


def _get_log_file():
    global _LOG_FILE
    if _LOG_FILE is None:
        os.makedirs(CACHE_DIR, exist_ok=True)
        _LOG_FILE = open(LOG_PATH, "w", encoding="utf-8")
    return _LOG_FILE


def log_info(tag: str, message: str) -> None:
    """
    输出时间戳日志到文件 + stderr。
    自动处理 Windows 编码和 Streamlit stdout 不可用问题。
    """
    ts = datetime.now().strftime("%H:%M:%S.%f")[:12]
    line = f"[{ts}] [{tag}] {message}"
    try:
        fh = _get_log_file()
        fh.write(line + "\n")
        fh.flush()
    except Exception:
        pass
    try:
        _sys.stderr.write(line + "\n")
        _sys.stderr.flush()
    except Exception:
        pass


# ============================================================
# 2. 数据库清理
# ============================================================

def cleanup_temp_db(db_path: str = DB_PATH) -> None:
    """安全删除临时 DuckDB 文件"""
    try:
        if os.path.exists(db_path):
            time.sleep(0.2)
            os.remove(db_path)
            log_info("CLEANUP", f"已删除临时数据库: {db_path}")
    except Exception as e:
        log_info("CLEANUP", f"清理数据库失败 (可忽略): {e}")


# 注册进程退出清理
atexit.register(cleanup_temp_db)


# ============================================================
# 3. CSV 导出
# ============================================================

def export_csv_bytes(df: pd.DataFrame) -> bytes:
    """
    将 DataFrame 导出为 UTF-8-BOM 编码的 CSV 字节流。
    BOM 确保 Excel 正确识别中文。

    Args:
        df: 要导出的 DataFrame。

    Returns:
        UTF-8-BOM 编码的字节。
    """
    return df.to_csv(index=False).encode("utf-8-sig")


# ============================================================
# 4. 安全异常处理（通用装饰器）
# ============================================================

def safe_result(func: Callable, *args, default=None, **kwargs):
    """
    安全调用函数，捕获所有异常，不崩溃。

    Args:
        func:    要调用的函数。
        *args:   位置参数。
        default: 异常时的默认返回值。
        **kwargs: 关键字参数。

    Returns:
        函数返回值或 default。
    """
    try:
        return func(*args, **kwargs)
    except Exception:
        return default


def safe_print_traceback() -> str:
    """
    获取当前异常的 traceback 字符串。
    不会因 stdout 不可用而崩溃（适用于 Streamlit 环境）。
    """
    try:
        return traceback.format_exc()
    except Exception:
        return "(无法获取 traceback)"


# ============================================================
# 5. 周报生成
# ============================================================

def generate_weekly_report(df: pd.DataFrame, llm=None) -> None:
    """
    生成本周工单分析报告。

    输出到 Streamlit UI（通过 st 写入），包括：
    - 本周工单总数
    - 处理中 / 今日新增
    - Top3 问题功能模块
    - Top3 负责人
    - LLM 生成的文字总结（如有 AI）

    Args:
        df:  全部或筛选后的工单 DataFrame。
        llm: ChatOpenAI 实例（可选，用于 AI 总结）。
    """
    today = date.today()
    monday = today - timedelta(days=today.weekday())
    sunday = monday + timedelta(days=6)

    st.subheader(f"📝 本周工单报告 ({monday} ~ {sunday})")

    # [修复] 筛选本周数据：优先用 "Change-TS problem"，其次用 "Date"
    # 原逻辑仅匹配 "Date"，但 "Date" 可能为空或格式不一致导致匹配为 0
    week_df = df.copy()
    time_col = None
    for candidate in ["Change-TS problem", "Date"]:
        if candidate in week_df.columns:
            try:
                parsed = pd.to_datetime(week_df[candidate], errors="coerce", dayfirst=True)
                if parsed.notna().sum() > 0:
                    time_col = candidate
                    dates = parsed
                    break
            except Exception:
                continue

    if time_col is not None:
        try:
            week_df = week_df[
                (dates.dt.date >= monday) & (dates.dt.date <= sunday)
            ]
        except Exception:
            pass

    total = len(week_df)
    st.metric("本周工单总数", f"{total:,}")

    if total == 0:
        st.info("本周暂无工单数据")
        return

    col1, col2, col3 = st.columns(3)

    # 列 1：处理中 + 今日新增
    with col1:
        if "Status" in week_df.columns:
            try:
                open_w = len(
                    week_df[week_df["Status"].astype(str).str.contains(
                        "under way|open", case=False, na=False
                    )]
                )
                st.metric("处理中", open_w)
            except Exception:
                pass
        if "Date" in week_df.columns:
            try:
                today_w = len(
                    week_df[week_df["Date"].astype(str).str.contains(
                        today.strftime("%Y-%m-%d"), na=False
                    )]
                )
                st.metric("今日新增", today_w)
            except Exception:
                pass

    # 列 2：Top3 功能模块
    with col2:
        func_col = (
            "Functionality.1" if "Functionality.1" in week_df.columns
            else "Functionality"
        )
        if func_col in week_df.columns:
            try:
                top_func = week_df[func_col].value_counts().head(3)
                st.write("**🔝 Top3 问题模块**")
                for name, cnt in top_func.items():
                    st.write(f"- {name}: {cnt}")
            except Exception:
                pass

    # 列 3：Top3 负责人
    with col3:
        if "Responsible Problem Solver" in week_df.columns:
            try:
                top_p = week_df["Responsible Problem Solver"].value_counts().head(3)
                st.write("**👤 Top3 负责人**")
                for name, cnt in top_p.items():
                    st.write(f"- {name}: {cnt}")
            except Exception:
                pass

    # LLM 总结
    if llm is not None:
        try:
            status_summary = ""
            if "Status" in week_df.columns:
                status_summary = week_df["Status"].value_counts().to_string()

            func_summary = ""
            if func_col in week_df.columns:
                func_summary = (
                    week_df[func_col].value_counts().head(5).to_string()
                )

            prompt = f"""基于以下本周工单数据，生成一份简洁的中文工单分析报告（200字以内）：

本周总数: {total}
状态分布:
{status_summary}

Top5功能模块:
{func_summary}

请包括：
1. 本周工单总体情况
2. 主要问题集中领域
3. 建议关注事项"""

            response = llm.invoke(prompt)
            st.markdown("#### 🤖 AI 分析总结")
            st.info(response.content)

        except Exception as e:
            st.warning(f"AI 总结生成失败: {str(e)[:80]}")


# ============================================================
# 6. 文件信息展示
# ============================================================

def render_file_info(source: str, df: pd.DataFrame) -> None:
    """
    在可折叠区域中显示当前数据文件信息 + 刷新按钮。

    Args:
        source: 数据来源（文件路径 或 "upload" 标记）。
        df:     当前 DataFrame。
    """
    with st.expander("📄 当前数据源", expanded=False):
        if source == "upload":
            st.write("**来源**: 📤 拖拽上传（CSV 文件）")
        elif source:
            st.write(f"**路径**: {source}")
        else:
            st.write("**来源**: N/A")
        st.write(f"**数据量**: {len(df):,} 行, {len(df.columns)} 列")
        if st.button("🔄 刷新数据", type="secondary", key="refresh_btn_fileinfo"):
            st.cache_data.clear()
            st.rerun()
