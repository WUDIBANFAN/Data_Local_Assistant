#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
================================================================================
保时捷测试工单智能分析系统 (Porsche Test Ticket Intelligent Analysis System)
================================================================================
技术栈: DuckDB + LangChain + Streamlit + Plotly
数据源: OneDrive 同步的 CSV 文件 (RechercheExport_*.csv)
运行方式: python -m streamlit run ticket_analyzer.py

版本: 3.0.0 - 完整仪表盘 + 自动加载 + NL2SQL + 周报
================================================================================
"""

# ============================================================
# 依赖导入 (Import Dependencies)
# ============================================================
import os
import re
import io
import glob
import time
import atexit
import tempfile
import traceback
from datetime import datetime, timedelta, date
from pathlib import Path

import streamlit as st
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import duckdb
from dotenv import load_dotenv

# LangChain 相关导入 (带兼容性处理)
try:
    from langchain_openai import ChatOpenAI
    LANGCHAIN_OPENAI_AVAILABLE = True
except ImportError:
    LANGCHAIN_OPENAI_AVAILABLE = False

try:
    from langchain_core.tools import Tool
    LANGCHAIN_SQL_AVAILABLE = True
except ImportError:
    LANGCHAIN_SQL_AVAILABLE = False


# ============================================================
# 配置区域 - 仅需修改这里 ↓↓↓
# ============================================================

# ----- CSV 数据目录 (硬编码) -----
CSV_DIR = (
    r"C:\Users\GRHYSFH\OneDrive - Dr. Ing. h.c. F. Porsche AG"
    r"\Sinan_ Infotainment@MLBevo China - Cross - Validation and Verification"
    r"\09_Error Management\CSV"
)

# ----- 项目代码目录 (硬编码) -----
PROJECT_DIR = (
    r"C:\Users\GRHYSFH\OneDrive - Dr. Ing. h.c. F. Porsche AG"
    r"\Desktop\Data_Local_Assistant"
)

# ----- CSV 解析配置 -----
CSV_ENCODING = 'utf-8-sig'         # 德国保时捷 CSV 通常带 BOM
CSV_DELIMITER = ';'                # 德式 CSV 用分号分隔
CSV_DECIMAL = ','                  # 德式小数点用逗号

# ----- 缓存配置 -----
CACHE_DIR = os.path.join(PROJECT_DIR, "CSV_DATA_CACHE")
CACHE_META_FILE = os.path.join(CACHE_DIR, "cache_meta.json")

# ============================================================
# 配置区域 - 仅需修改这里 ↑↑↑
# ============================================================


# ============================================================
# 全局变量 (Global Variables)
# ============================================================
DB_PATH = os.path.join(tempfile.gettempdir(), "porsche_tickets_analysis.db")

# 加载 .env 文件
env_path = os.path.join(PROJECT_DIR, ".env")
if os.path.exists(env_path):
    load_dotenv(env_path)
else:
    load_dotenv()

# 清理临时数据库
def _cleanup_db():
    try:
        if os.path.exists(DB_PATH):
            time.sleep(0.1)
            os.remove(DB_PATH)
    except Exception:
        pass

atexit.register(_cleanup_db)


# ============================================================
# 辅助函数: 终端日志输出
# ============================================================
_LOG_FILE = None

def log_info(tag: str, message: str):
    global _LOG_FILE
    timestamp = datetime.now().strftime("%H:%M:%S.%f")[:12]
    line = f"[{timestamp}] [{tag}] {message}"

    if _LOG_FILE is None:
        os.makedirs(CACHE_DIR, exist_ok=True)
        log_path = os.path.join(CACHE_DIR, "load_progress.txt")
        _LOG_FILE = open(log_path, 'w', encoding='utf-8')

    _LOG_FILE.write(line + "\n")
    _LOG_FILE.flush()

    try:
        import sys as _sys
        _sys.__stdout__.write(line + "\n")
        _sys.__stdout__.flush()
    except Exception:
        pass


# ============================================================
# 函数: 扫描目录，找到最新的 CSV 文件
# ============================================================
def find_latest_csv():
    """
    扫描 CSV 目录及子目录(01-12)，按修改时间取最新的 CSV。
    返回 (full_path, mtime, fname) 或 None。
    """
    candidates = []
    scan_dirs = [CSV_DIR] + [
        os.path.join(CSV_DIR, f"{m:02d}")
        for m in range(1, 13)
        if os.path.isdir(os.path.join(CSV_DIR, f"{m:02d}"))
    ]

    for sd in scan_dirs:
        try:
            for f in glob.glob(os.path.join(sd, "RechercheExport_*.csv")):
                try:
                    mtime = os.path.getmtime(f)
                    candidates.append((f, mtime, os.path.basename(f)))
                except OSError:
                    continue
        except (FileNotFoundError, PermissionError):
            continue

    if not candidates:
        return None

    # 按修改时间降序
    candidates.sort(key=lambda x: x[1], reverse=True)
    return candidates[0]


# ============================================================
# 函数: 从磁盘读取 CSV（带 OneDrive 锁重试）
# ============================================================
def read_csv_with_retry(csv_path: str, max_retries: int = 3, retry_delay: float = 0):
    """
    从磁盘读取 CSV 文件到 DataFrame。
    优化：先小样检测编码和 skiprows，再用正确编码一次读完。
    """
    fname = os.path.basename(csv_path)

    # ---- 快速编码检测 + 跳行检测 ----
    skip_rows = 0
    best_enc = 'utf-8-sig'
    for enc in ['utf-8-sig', 'utf-8', 'latin-1', 'gbk']:
        try:
            with open(csv_path, 'r', encoding=enc) as fh:
                lines = [fh.readline() for _ in range(20)]
            best_enc = enc
            # 找表头行
            for i, line in enumerate(lines):
                parts = line.strip().split(CSV_DELIMITER)
                if len(parts) >= 5:
                    skip_rows = i
                    break
            break
        except (UnicodeDecodeError, UnicodeError):
            continue

    log_info("DETECT", f"{fname}: skip={skip_rows}, enc={best_enc}")

    # ---- 正式读取 ----
    for attempt in range(1, max_retries + 1):
        try:
            df = pd.read_csv(
                csv_path,
                sep=CSV_DELIMITER,
                encoding=best_enc,
                decimal=',',
                skiprows=skip_rows,
                on_bad_lines='skip',
            )

            if df is None or len(df) == 0:
                raise ValueError("读取后无数据")
            if len(df.columns) == 0:
                raise ValueError("未能解析出任何列")

            df.columns = df.columns.str.strip()
            log_info("PARSE", f"{fname}: {len(df)} 行, {len(df.columns)} 列")
            return df

        except (PermissionError, OSError) as e:
            if attempt < max_retries:
                log_info("LOCK", f"文件被锁定，重试 ({attempt}/{max_retries}): {e}")
                if retry_delay:
                    time.sleep(retry_delay * attempt)
            else:
                raise RuntimeError(f"CSV 文件被 OneDrive 锁定: {e}")
        except Exception as e:
            # 非锁错误直接报错，不重试
            raise RuntimeError(f"CSV 读取失败 {fname}: {str(e)[:200]}")


# ============================================================
# 函数: 数据清洗（时间/数值转换）
# ============================================================
def _is_string_like(dtype) -> bool:
    """判断 dtype 是否为字符串类型（兼容 pandas 1.x 和 2.x）"""
    return dtype == object or str(dtype) in ("object", "string", "str")


def _sample_looks_numeric(col_str):
    """
    快速采样检测：取前 100 个非空值，检查是否大多像数字。
    用于跳过长文本列避免昂贵的全量 regex 操作。
    """
    sample = col_str.dropna().head(100).astype(str)
    if len(sample) < 5:
        return False
    if sample.str.len().mean() > 50:
        return False
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
    3. 去除非数字字符后再试（仅当采样表明值得）
    4. 处理 Excel ="value" 格式后重试
    """
    col_str = col_series.astype(str)

    # 策略 1: 直接转换
    converted = pd.to_numeric(col_str, errors='coerce')
    ratio = converted.notna().mean()
    if ratio > 0.3:
        return converted, ratio

    # 策略 2: 德式逗号 → 小数点
    cleaned = col_str.str.replace(',', '.', regex=False)
    converted = pd.to_numeric(cleaned, errors='coerce')
    ratio = converted.notna().mean()
    if ratio > 0.3:
        return converted, ratio

    # 策略 3: 去除非数字字符（仅采样确认后）
    if not _sample_looks_numeric(col_str):
        return None, 0
    cleaned = col_str.str.replace(r"[^\d.\-]", "", regex=True)
    converted = pd.to_numeric(cleaned, errors='coerce')
    ratio = converted.notna().mean()
    if ratio > 0.3:
        return converted, ratio

    # 策略 4: 处理 Excel ="value"
    if col_str.str.contains(r'^="', na=False).any():
        cleaned = col_str.str.replace(r'^="(.*)"$', r'\1', regex=True)
        converted = pd.to_numeric(cleaned, errors='coerce')
        ratio = converted.notna().mean()
        if ratio > 0.3:
            return converted, ratio

    return None, 0


def clean_dataframe(df):
    """通用数据清洗：智能数值列转换 + 时间列"""
    if df is None or len(df) == 0:
        return df

    # 数值列智能转换（从快到慢逐步尝试）
    for col in df.columns:
        if not _is_string_like(df[col].dtype):
            continue
        converted, _ = _try_numeric_conversion(df[col])
        if converted is not None:
            df[col] = converted

    # 时间列识别转换（只对仍为字符串的列做）
    time_columns = [c for c in df.columns
                    if any(k in c.lower() for k in ['time', 'date', 'ts', 'timestamp'])]
    for col in time_columns:
        if not _is_string_like(df[col].dtype):
            continue
        try:
            sample = df[col].dropna().astype(str).head(20)
            if len(sample) < 2:
                continue
            if not sample.str.contains(r"[-/:T]", regex=True).any():
                continue
            df[col] = pd.to_datetime(df[col], errors='coerce', dayfirst=True)
        except Exception:
            pass

    return df


# ============================================================
# 函数: 加载最新 CSV 到 DuckDB
# ============================================================
def load_latest_csv(db_path: str):
    """
    自动扫描目录找最新 CSV，读取并写入 DuckDB。
    返回 (csv_path, db, df)。
    """
    latest = find_latest_csv()
    if latest is None:
        raise RuntimeError(f"在 {CSV_DIR} 目录中未找到 RechercheExport_*.csv 文件")

    csv_path, mtime, fname = latest
    mtime_str = datetime.fromtimestamp(mtime).strftime('%Y-%m-%d %H:%M:%S')
    log_info("MAIN", f"最新文件: {fname} | {mtime_str}")

    # 读取 CSV
    with st.spinner(f"📂 正在读取 {fname}..."):
        df = read_csv_with_retry(csv_path)
        df = clean_dataframe(df)

    if df is None or len(df) == 0:
        raise RuntimeError("CSV 读取失败，无有效数据")

    # 清理旧数据库
    if os.path.exists(db_path):
        try:
            os.remove(db_path)
        except Exception:
            pass

    # 用 DuckDB 原生 API 写入
    con = duckdb.connect(db_path)
    con.execute("DROP TABLE IF EXISTS tickets")
    con.execute("CREATE TABLE tickets AS SELECT * FROM df")
    row_count = con.execute("SELECT COUNT(*) FROM tickets").fetchone()[0]
    con.close()

    log_info("LOAD", f"DuckDB: {row_count:,} 行")
    db = DuckDBWrapper(db_path, df)
    return csv_path, db, df


# ============================================================
# DuckDBWrapper: DuckDB 原生 SQL 执行器（LangChain Agent 兼容）
# ============================================================
class DuckDBWrapper:
    """
    纯 DuckDB 原生包装器，完全绕过 SQLAlchemy。
    提供 LangChain create_sql_agent 所需的接口。
    """

    def __init__(self, db_path: str, sample_df: pd.DataFrame = None):
        self._db_path = db_path
        self._con = duckdb.connect(db_path, read_only=False)
        self._table_names = ['tickets']
        self._closed = False
        self._table_info = self._build_table_info()

        if sample_df is not None and len(sample_df) > 0:
            try:
                sample_rows = sample_df.head(3).to_string(max_colwidth=40)
                self._table_info += (
                    "\n\n/* Sample rows (first 3):\n" + sample_rows + "\n*/"
                )
            except Exception:
                pass

    def _ensure_open(self):
        if self._closed:
            self._con = duckdb.connect(self._db_path, read_only=False)
            self._closed = False

    def _build_table_info(self) -> str:
        try:
            self._ensure_open()
            rows = self._con.execute("PRAGMA table_info('tickets')").fetchall()
            if not rows:
                return "CREATE TABLE tickets ();"
            lines = ["CREATE TABLE tickets ("]
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

    def run(self, command: str, fetch: str = "all") -> str:
        try:
            self._ensure_open()
            result = self._con.execute(command)
            rows = result.fetchall()
            if result.description is None:
                row_count = getattr(result, 'rowcount', None)
                return f"命令执行成功" + (f"，影响 {row_count} 行" if row_count else "")
            if not rows:
                return "查询返回 0 行。"
            cols = [desc[0] for desc in result.description]
            max_display = 100
            lines = [", ".join(cols)]
            for row in rows[:max_display]:
                lines.append(", ".join(str(v) if v is not None else "NULL" for v in row))
            if len(rows) > max_display:
                lines.append(f"\n... (共 {len(rows):,} 行，仅显示前 {max_display} 行)")
            return "\n".join(lines)
        except Exception as e:
            return f"SQL 执行错误: {type(e).__name__}: {str(e)[:300]}"

    def run_no_throw(self, command: str, fetch: str = "all") -> str:
        try:
            return self.run(command, fetch)
        except Exception as e:
            return f"Error: {type(e).__name__}: {str(e)[:300]}"

    def get_usable_table_names(self):
        return list(self._table_names)

    def get_table_info(self, table_names=None):
        return self._table_info

    @property
    def dialect(self) -> str:
        return "duckdb"

    def close(self):
        try:
            if hasattr(self, '_con') and not self._closed:
                self._con.close()
                self._closed = True
        except Exception:
            pass

    def __del__(self):
        self.close()


# ============================================================
# 函数: 初始化大模型
# ============================================================
def init_llm():
    api_key = os.getenv("LLM_API_KEY") or ""
    if not api_key:
        return None, "未配置 API Key，请在 .env 文件中设置 LLM_API_KEY"

    base_url = os.getenv("OPENAI_BASE_URL") or ""

    try:
        llm = ChatOpenAI(
            model=os.getenv("LLM_MODEL") or "gpt-3.5-turbo",
            temperature=float(os.getenv("LLM_TEMPERATURE", 0)),
            max_tokens=int(os.getenv("LLM_MAX_TOKENS", 2000)),
            api_key=api_key,
            base_url=base_url if base_url else None,
        )
        return llm, None
    except Exception as e:
        return None, f"初始化大模型失败: {str(e)}"


# ============================================================
# 函数: 创建 LangChain SQL Agent
# ============================================================
import re as _re

_SQL_EXTRACT_RE = _re.compile(
    r"(?:```sql\s*|```\s*)((?:SELECT|select|WITH|with)\b.*?)(?:```|\Z)",
    _re.DOTALL,
)


class _SimpleSQLAgent:
    """
    轻量级 SQL Agent：直接调用 LLM + DuckDBWrapper。
    不依赖 LangChain ReAct Agent 框架 / SQLAlchemy / SQLDatabase。
    兼容 agent.invoke({"input": "..."}) 接口。
    """

    def __init__(self, llm, db):
        self._llm = llm
        self._db = db

        # 获取表名
        try:
            table_name = db.get_usable_table_names()[0]
        except Exception:
            table_name = "tickets"

        self._system = f"""你是一个保时捷测试工单数据库的 SQL 专家。

数据库表名: {table_name}
表结构:
- Number: 工单编号 (整数)
- "Change-TS problem": 工单变更时间戳
- "Date": 工单创建日期
- "Short Text": 问题标题
- "Problem Description": 问题详细描述
- "Functionality": 功能模块编号
- "Functionality.1": 功能模块全称
- "Status": 工单状态
- "Responsible Problem Solver": 负责人
- "SW (causing)": 软件版本
- "HW (causing)": 硬件版本
- "Fault frequency": 故障频率
- "Rating": 严重等级 (1-6)
- "Country": 国家
- "Project": 项目

规则:
1. 只能生成 SELECT 查询
2. 带空格的列名必须用双引号括起来
3. 使用 /* ... */ 中文注释解释逻辑

请直接输出 DuckDB SQL，用 ```sql ... ``` 包裹。"""

    def invoke(self, inputs: dict) -> dict:
        question = inputs.get("input", "")
        if not question:
            return {"output": "问题不能为空", "intermediate_steps": []}
        steps = []
        try:
            prompt = f"{self._system}\n\n用户问题: {question}\n\nSQL 查询:"
            response = self._llm.invoke(prompt)
            llm_text = response.content if hasattr(response, "content") else str(response)
            steps.append(("LLM", llm_text))

            sql = self._extract_sql(llm_text)
            if not sql:
                return {"output": f"AI 未生成有效 SQL:\n{llm_text[:500]}", "intermediate_steps": steps}
            steps.append(("SQL", sql))

            result = self._db.run(sql)
            steps.append(("结果", result))
            return {"output": result, "intermediate_steps": steps}
        except Exception as e:
            return {"output": f"失败: {type(e).__name__}: {str(e)[:300]}", "intermediate_steps": steps}

    @staticmethod
    def _extract_sql(text: str) -> str:
        m = _SQL_EXTRACT_RE.search(text)
        if m:
            return m.group(1).strip()
        lines = text.strip().split("\n")
        sql_lines = []
        in_sql = False
        for line in lines:
            stripped = line.strip()
            if _re.match(r"^(SELECT|select|WITH|with)\b", stripped):
                in_sql = True
            if in_sql:
                sql_lines.append(stripped)
        return "\n".join(sql_lines) if sql_lines else ""


def create_sql_agent_with_llm(llm, db):
    """
    创建 SQL Agent：直接 LLM + DuckDBWrapper，无 SQLAlchemy。
    返回兼容 invoke({"input": "..."}) 的 Agent 对象。
    """
    if not LANGCHAIN_SQL_AVAILABLE:
        return None, "LangChain 模块未完整安装"
    if llm is None:
        return None, "LLM 未初始化"
    if db is None:
        return None, "数据库未加载"
    try:
        return _SimpleSQLAgent(llm, db), None
    except Exception as e:
        return None, f"创建 Agent 失败: {str(e)}"


# ============================================================
# 图表生成函数
# ============================================================

def render_overview_cards(df):
    """概览卡片：总数、今日新增、未解决、平均严重等级"""
    col1, col2, col3, col4 = st.columns(4)

    with col1:
        st.metric("总工单数", f"{len(df):,}")

    with col2:
        today_count = 0
        if 'Date' in df.columns:
            today_str = date.today().strftime('%Y-%m-%d')
            try:
                today_count = len(df[df['Date'].astype(str).str.contains(today_str, na=False)])
            except Exception:
                pass
        st.metric("今日新增", f"{today_count:,}")

    with col3:
        open_count = 0
        if 'Status' in df.columns:
            try:
                open_count = len(
                    df[df['Status'].astype(str).str.contains(
                        'under way|open|taken over', case=False, na=False
                    )]
                )
            except Exception:
                pass
        st.metric("未解决工单", f"{open_count:,}")

    with col4:
        avg_rating = None
        if 'Rating' in df.columns:
            try:
                avg_rating = pd.to_numeric(df['Rating'], errors='coerce').mean()
            except Exception:
                pass
        st.metric(
            "平均严重等级",
            f"{avg_rating:.2f}" if pd.notna(avg_rating) else "N/A"
        )


def render_time_trend_chart(df):
    """时间趋势图：每日新增工单 + 7日移动平均"""
    if 'Date' not in df.columns:
        st.info("缺少 Date 列，无法生成时间趋势图")
        return

    try:
        daily = df['Date'].dropna().value_counts().sort_index()
        if len(daily) == 0:
            st.info("无有效日期数据")
            return

        daily_df = pd.DataFrame({
            '日期': daily.index,
            '新增工单': daily.values
        }).sort_values('日期')

        daily_df['7日移动平均'] = daily_df['新增工单'].rolling(window=7, min_periods=1).mean()

        fig = make_subplots(specs=[[{"secondary_y": False}]])

        fig.add_trace(
            go.Bar(name='每日新增', x=daily_df['日期'], y=daily_df['新增工单'],
                   marker_color='#4a90d9', opacity=0.7),
        )
        fig.add_trace(
            go.Scatter(name='7日移动平均', x=daily_df['日期'], y=daily_df['7日移动平均'],
                       line=dict(color='#e74c3c', width=3), mode='lines'),
        )

        fig.update_layout(
            title='📈 每日新增工单趋势 (含7日移动平均)',
            xaxis_title='日期',
            yaxis_title='工单数量',
            hovermode='x unified',
            height=400,
        )
        st.plotly_chart(fig, use_container_width=True)
    except Exception as e:
        st.warning(f"时间趋势图生成失败: {str(e)[:80]}")


def render_status_pie(df):
    """工单状态饼图"""
    if 'Status' not in df.columns:
        return
    try:
        status_counts = df['Status'].value_counts()
        fig = px.pie(
            values=status_counts.values,
            names=status_counts.index,
            title='📊 工单状态分布',
            color_discrete_sequence=px.colors.qualitative.Set3,
        )
        fig.update_traces(textposition='inside', textinfo='percent+label')
        st.plotly_chart(fig, use_container_width=True)
    except Exception as e:
        st.warning(f"状态图失败: {str(e)[:80]}")


def render_functionality_chart(df):
    """Top10 问题最多的功能模块"""
    col = 'Functionality.1' if 'Functionality.1' in df.columns else 'Functionality'
    if col not in df.columns:
        return
    try:
        top_func = df[col].value_counts().head(10)
        fig = px.bar(
            x=top_func.values,
            y=top_func.index,
            orientation='h',
            title='🔝 Top10 问题最多功能模块',
            labels={'x': '工单数量', 'y': '功能模块'},
            color=top_func.values,
            color_continuous_scale='Blues',
        )
        fig.update_layout(height=400, yaxis={'categoryorder': 'total ascending'})
        st.plotly_chart(fig, use_container_width=True)
    except Exception as e:
        st.warning(f"功能模块图失败: {str(e)[:80]}")


def render_person_chart(df):
    """Top10 负责人工作量"""
    if 'Responsible Problem Solver' not in df.columns:
        return
    try:
        top_person = df['Responsible Problem Solver'].value_counts().head(10)
        fig = px.bar(
            x=top_person.values,
            y=top_person.index,
            orientation='h',
            title='👤 Top10 负责人工作量',
            labels={'x': '工单数量', 'y': '负责人'},
            color=top_person.values,
            color_continuous_scale='Greens',
        )
        fig.update_layout(height=400, yaxis={'categoryorder': 'total ascending'})
        st.plotly_chart(fig, use_container_width=True)
    except Exception as e:
        st.warning(f"负责人图失败: {str(e)[:80]}")


def render_sw_hw_charts(df):
    """软件/硬件版本分布"""
    col1, col2 = st.columns(2)
    with col1:
        if 'SW (causing)' in df.columns:
            try:
                sw_counts = df['SW (causing)'].value_counts().head(10)
                fig = px.bar(
                    x=sw_counts.index, y=sw_counts.values,
                    title='💾 软件版本分布 (Top10)',
                    labels={'x': '软件版本', 'y': '工单数量'},
                    color=sw_counts.values,
                    color_continuous_scale='Reds',
                )
                st.plotly_chart(fig, use_container_width=True)
            except Exception as e:
                st.info(f"SW 图不可用: {str(e)[:50]}")
    with col2:
        if 'HW (causing)' in df.columns:
            try:
                hw_counts = df['HW (causing)'].value_counts().head(10)
                fig = px.bar(
                    x=hw_counts.index, y=hw_counts.values,
                    title='🔧 硬件版本分布 (Top10)',
                    labels={'x': '硬件版本', 'y': '工单数量'},
                    color=hw_counts.values,
                    color_continuous_scale='Purples',
                )
                st.plotly_chart(fig, use_container_width=True)
            except Exception as e:
                st.info(f"HW 图不可用: {str(e)[:50]}")


def render_rating_chart(df):
    """严重等级分布"""
    if 'Rating' not in df.columns:
        return
    try:
        rating_vals = pd.to_numeric(df['Rating'], errors='coerce').dropna()
        if len(rating_vals) == 0:
            return
        rating_counts = rating_vals.value_counts().sort_index()
        fig = px.bar(
            x=rating_counts.index, y=rating_counts.values,
            title='⚠️ 严重等级分布 (1=最严重)',
            labels={'x': '严重等级', 'y': '工单数量'},
            color=rating_counts.index,
            color_continuous_scale='RdYlGn_r',
        )
        fig.update_layout(xaxis=dict(tickmode='linear', dtick=1))
        st.plotly_chart(fig, use_container_width=True)
    except Exception as e:
        st.warning(f"严重等级图失败: {str(e)[:80]}")


def render_fault_frequency(df):
    """故障频率分布"""
    if 'Fault frequency' not in df.columns:
        return
    try:
        freq_counts = df['Fault frequency'].value_counts()
        fig = px.pie(
            values=freq_counts.values,
            names=freq_counts.index,
            title='🔄 故障频率分布',
            color_discrete_sequence=px.colors.qualitative.Pastel,
        )
        fig.update_traces(textposition='inside', textinfo='percent+label')
        st.plotly_chart(fig, use_container_width=True)
    except Exception:
        pass


# ============================================================
# 函数: 一键生成周报
# ============================================================
def generate_weekly_report(df, llm=None):
    """生成当周工单分析报告"""
    today = date.today()
    monday = today - timedelta(days=today.weekday())
    sunday = monday + timedelta(days=6)

    st.subheader(f"📝 本周工单报告 ({monday} ~ {sunday})")

    # 筛选本周数据
    week_df = df.copy()
    if 'Date' in week_df.columns:
        try:
            dates = pd.to_datetime(week_df['Date'], errors='coerce')
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

    with col1:
        if 'Status' in week_df.columns:
            open_w = len(
                week_df[week_df['Status'].astype(str).str.contains(
                    'under way|open', case=False, na=False
                )]
            )
            st.metric("处理中", open_w)
        if 'Date' in week_df.columns:
            today_w = len(
                week_df[week_df['Date'].astype(str).str.contains(
                    today.strftime('%Y-%m-%d'), na=False
                )]
            )
            st.metric("今日新增", today_w)

    with col2:
        if 'Functionality.1' in week_df.columns:
            top_func = week_df['Functionality.1'].value_counts().head(3)
            st.write("**🔝 Top3 问题模块**")
            for name, cnt in top_func.items():
                st.write(f"- {name}: {cnt}")
        elif 'Functionality' in week_df.columns:
            top_func = week_df['Functionality'].value_counts().head(3)
            st.write("**🔝 Top3 问题模块**")
            for name, cnt in top_func.items():
                st.write(f"- {name}: {cnt}")

    with col3:
        if 'Responsible Problem Solver' in week_df.columns:
            top_p = week_df['Responsible Problem Solver'].value_counts().head(3)
            st.write("**👤 Top3 负责人**")
            for name, cnt in top_p.items():
                st.write(f"- {name}: {cnt}")

    # 用 LLM 生成文字总结
    if llm is not None:
        try:
            status_summary = ""
            if 'Status' in week_df.columns:
                status_summary = week_df['Status'].value_counts().to_string()

            func_summary = ""
            if 'Functionality.1' in week_df.columns:
                func_summary = week_df['Functionality.1'].value_counts().head(5).to_string()

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
# 主函数: Streamlit 应用入口
# ============================================================
def main():
    """保时捷测试工单智能分析系统 - 主入口"""
    st.set_page_config(
        page_title="保时捷测试工单智能分析系统",
        page_icon="🏎️",
        layout="wide",
    )

    st.title("🏎️ 保时捷测试工单智能分析系统")

    # ==================== 1. 数据加载 ====================
    # 检查是否需要重新加载
    latest = find_latest_csv()
    current_csv_path = st.session_state.get('_csv_path')

    needs_reload = False
    if latest is None:
        st.error(f"⛔ 在目录中未找到 CSV 文件:\n`{CSV_DIR}`")
        st.info("请确保 OneDrive 已同步，且文件位于上述目录下")
        st.stop()

    latest_path, latest_mtime, latest_name = latest
    if current_csv_path != latest_path:
        needs_reload = True

    if needs_reload or st.session_state.get('df') is None:
        try:
            csv_path, db, df = load_latest_csv(DB_PATH)
            st.session_state._csv_path = csv_path
            st.session_state.db = db
            st.session_state.df = df

            st.success(
                f"✅ 已加载最新 CSV: **{os.path.basename(csv_path)}** "
                f"| {len(df):,} 条工单"
            )
        except RuntimeError as e:
            st.error(f"⛔ {str(e)}")
            st.info("💡 请确认 OneDrive 同步完成后再刷新页面")
            if st.session_state.get('df') is not None:
                st.warning("⚠️ 显示上一次成功加载的缓存数据")
                df = st.session_state.df
                db = st.session_state.db
            else:
                st.stop()
        except Exception as e:
            st.error(f"⛔ 加载失败: {str(e)}")
            # 用 format_exc() 代替 print_exc()，避免 Streamlit 中 sys.stdout 不可用导致 OSError
            try:
                import sys
                print(traceback.format_exc(), file=sys.stderr, flush=True)
            except Exception:
                pass
            if st.session_state.get('df') is not None:
                df = st.session_state.df
                db = st.session_state.db
                st.warning("⚠️ 显示缓存数据")
            else:
                st.stop()

    db = st.session_state.db
    df = st.session_state.df

    if df is None:
        st.stop()

    # 初始化大模型
    if 'llm' not in st.session_state:
        st.session_state.llm, st.session_state.llm_error = init_llm()
    llm = st.session_state.llm

    # 当前文件信息
    current_path = st.session_state.get('_csv_path', '')
    with st.expander(f"📄 当前数据文件", expanded=False):
        st.write(f"**文件**: {os.path.basename(current_path)}")
        st.write(f"**路径**: {current_path}")
        st.write(f"**数据量**: {len(df):,} 行, {len(df.columns)} 列")
        if st.button("🔄 刷新数据", type="secondary"):
            st.cache_data.clear()
            st.rerun()

    # ==================== 2. 数据筛选器（侧边栏） ====================
    st.sidebar.header("🔎 数据筛选")

    filtered_df = df.copy()

    # 日期范围筛选
    if 'Date' in filtered_df.columns:
        try:
            dates = pd.to_datetime(filtered_df['Date'], errors='coerce').dropna()
            if len(dates) > 0:
                min_date = dates.min().date()
                max_date = dates.max().date()
                date_range = st.sidebar.date_input(
                    "日期范围",
                    value=(min_date, max_date),
                    min_value=min_date,
                    max_value=max_date,
                )
                if len(date_range) == 2:
                    start_d, end_d = date_range
                    filtered_df = filtered_df[
                        (dates.dt.date >= start_d) & (dates.dt.date <= end_d)
                    ]
        except Exception:
            pass

    # 状态筛选
    if 'Status' in filtered_df.columns:
        try:
            status_options = ['全部'] + sorted(filtered_df['Status'].dropna().unique().tolist())
            selected_status = st.sidebar.selectbox("工单状态", status_options)
            if selected_status != '全部':
                filtered_df = filtered_df[filtered_df['Status'] == selected_status]
        except Exception:
            pass

    # 功能模块筛选
    func_col = 'Functionality.1' if 'Functionality.1' in filtered_df.columns else 'Functionality'
    if func_col in filtered_df.columns:
        try:
            func_options = ['全部'] + sorted(filtered_df[func_col].dropna().unique().tolist())
            selected_func = st.sidebar.selectbox("功能模块", func_options)
            if selected_func != '全部':
                filtered_df = filtered_df[filtered_df[func_col] == selected_func]
        except Exception:
            pass

    # 负责人筛选
    if 'Responsible Problem Solver' in filtered_df.columns:
        try:
            person_options = ['全部'] + sorted(
                filtered_df['Responsible Problem Solver'].dropna().unique().tolist()
            )
            selected_person = st.sidebar.selectbox("负责人", person_options)
            if selected_person != '全部':
                filtered_df = filtered_df[filtered_df['Responsible Problem Solver'] == selected_person]
        except Exception:
            pass

    st.sidebar.markdown(f"**筛选后**: {len(filtered_df):,} 条工单")

    # ==================== 3. 概览卡片 ====================
    st.markdown("---")
    st.subheader("📊 数据概览")
    render_overview_cards(filtered_df)

    # ==================== 4. Tab 布局 ====================
    tab_titles = ["📈 可视化仪表盘", "💬 智能查询", "📋 原始数据", "📝 周报"]
    tab_viz, tab_query, tab_data, tab_report = st.tabs(tab_titles)

    # ----- Tab: 可视化仪表盘 -----
    with tab_viz:
        # 时间趋势
        render_time_trend_chart(filtered_df)

        # 两列布局：状态饼图 + 故障频率
        col_left, col_right = st.columns(2)
        with col_left:
            render_status_pie(filtered_df)
        with col_right:
            render_fault_frequency(filtered_df)

        # 功能模块 + 负责人
        col_left, col_right = st.columns(2)
        with col_left:
            render_functionality_chart(filtered_df)
        with col_right:
            render_person_chart(filtered_df)

        # 软硬件版本
        render_sw_hw_charts(filtered_df)

        # 严重等级
        render_rating_chart(filtered_df)

    # ----- Tab: 智能查询 -----
    with tab_query:
        st.markdown("### 💬 智能 SQL 查询")
        st.markdown("用中文提问，AI 自动生成 SQL 查询数据。")

        if llm is None:
            st.warning(f"⚠️ 大模型未初始化: {st.session_state.get('llm_error', '未知错误')}")
        else:
            # 示例问题
            with st.expander("💡 点击查看示例问题", expanded=False):
                example_questions = [
                    "统计各状态的工单数量",
                    "本月新增了多少个工单？",
                    "软件版本 5045 对应的问题有哪些？",
                    "严重等级 1 的工单有多少个？",
                    "功能模块 Speech - general 有多少个未解决工单？",
                    "谁负责的工单最多？",
                    "近 7 天每天新增多少工单？",
                ]
                for eq in example_questions:
                    if st.button(eq, key=f"eq_{eq}", use_container_width=True):
                        st.session_state._question = eq

            # 输入框
            question = st.text_input(
                "输入你的问题",
                value=st.session_state.get('_question', ''),
                placeholder="例如：统计本周各状态的工单数量",
                key="query_input",
            )

            if st.button("🔍 执行查询", type="primary") and question:
                st.session_state._question = ""
                with st.spinner("🤖 AI 正在分析..."):
                    agent, error = create_sql_agent_with_llm(llm, db)
                    if agent:
                        try:
                            response = agent.invoke({"input": question})
                            st.markdown("#### 📋 AI 回答")
                            st.write(response.get('output', '无输出'))

                            intermediate_steps = response.get('intermediate_steps', [])
                            if intermediate_steps:
                                for step in intermediate_steps:
                                    if isinstance(step, tuple) and len(step) >= 2:
                                        action, observation = step[0], step[1]
                                        if hasattr(action, 'tool') and 'sql' in str(action.tool).lower():
                                            with st.expander("查看生成的 SQL"):
                                                st.code(
                                                    action.tool_input if hasattr(action, 'tool_input') else str(action),
                                                    language="sql",
                                                )
                        except Exception as e:
                            st.error(f"查询失败: {str(e)}")
                            st.info("💡 建议换一种表达方式重试")
                    else:
                        st.error(f"创建 Agent 失败: {error}")

    # ----- Tab: 原始数据 -----
    with tab_data:
        st.markdown("### 📋 原始数据")
        st.dataframe(filtered_df, use_container_width=True, height=500)

        # CSV 导出
        csv = filtered_df.to_csv(index=False).encode('utf-8-sig')
        st.download_button(
            label="📥 下载筛选结果为 CSV",
            data=csv,
            file_name=f"tickets_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
            mime="text/csv",
        )

    # ----- Tab: 周报 -----
    with tab_report:
        if st.button("📝 一键生成本周报告", type="primary", use_container_width=True):
            generate_weekly_report(filtered_df, llm)
        else:
            st.info("点击上方按钮，自动生成本周工单分析报告")


if __name__ == "__main__":
    main()
