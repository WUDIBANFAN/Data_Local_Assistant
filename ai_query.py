#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
================================================================================
ai_query.py — NL2SQL / 智能查询模块
保时捷测试工单智能分析系统 | 模块化重构版 v4.0
================================================================================
职责：
 - 初始化 LLM（DeepSeek V4 Pro / OpenAI 兼容接口）
 - 创建 SQL Agent（直接调用 LLM + DuckDBWrapper，不依赖 SQLAlchemy）
 - 封装自然语言查询执行
 - 严格只读 SQL

架构说明：
  本模块**完全绕过** SQLAlchemy 和 LangChain 的 SQLDatabaseToolkit。
  直接复用 DuckDBWrapper 已有连接执行 SQL，避免多连接与 pg_catalog 兼容性问题。
================================================================================
"""

import re
from typing import Optional, Tuple

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

from config import get_llm_config, is_env_configured, DUCKDB_TABLE_NAME

# ---- LangChain 导入（v1.2+ 兼容） ----
LANGCHAIN_OPENAI_AVAILABLE = False
LANGCHAIN_SQL_AVAILABLE = False

try:
    from langchain_openai import ChatOpenAI
    LANGCHAIN_OPENAI_AVAILABLE = True
except ImportError:
    pass

try:
    from langchain_core.tools import Tool
    LANGCHAIN_SQL_AVAILABLE = True
except ImportError:
    pass


# ============================================================
# 1. LLM 初始化（DeepSeek V4 Pro）
# ============================================================

def init_llm():
    """
    初始化 LLM 实例（兼容 DeepSeek / OpenAI 等 OpenAI 兼容接口）。
    Returns:
        (llm, error_message): 成功时 error_message 为 None。
    """
    if not LANGCHAIN_OPENAI_AVAILABLE:
        return None, "langchain-openai 未安装，请运行: pip install langchain-openai"

    cfg = get_llm_config()

    if not cfg["api_key"]:
        return None, "未配置 LLM_API_KEY。请在 .env 文件中设置 LLM_API_KEY=your-key"

    try:
        llm_kwargs = {
            "model": cfg["model"],
            "temperature": cfg["temperature"],
            "max_tokens": cfg["max_tokens"],
            "api_key": cfg["api_key"],
        }
        if cfg["base_url"]:
            llm_kwargs["base_url"] = cfg["base_url"]

        llm = ChatOpenAI(**llm_kwargs)
        return llm, None

    except Exception as e:
        return None, f"初始化 LLM 失败: {type(e).__name__}: {str(e)[:200]}"


# ============================================================
# 2. Schema 获取（直接使用 DuckDBWrapper，不依赖 SQLAlchemy）
# ============================================================

def _build_schema_string(db) -> str:
    """从 DuckDBWrapper 获取表结构 DDL（给 LLM 提供上下文）。"""
    # 优先用 DuckDBWrapper 自带的 get_table_info
    try:
        return db.get_table_info()
    except Exception:
        pass

    # fallback：直接从 DuckDBWrapper 的 connection 查 information_schema
    try:
        tables = db.get_usable_table_names()
        lines = []
        for tname in tables:
            sql = (
                f"SELECT column_name || ' ' || data_type AS col "
                f"FROM information_schema.columns WHERE table_name = '{tname}' "
                f"ORDER BY ordinal_position"
            )
            col_rows = db.run(sql)
            ddl = f"CREATE TABLE {tname} (\n"
            for row_str in col_rows.split("\n")[1:]:
                if row_str.strip():
                    ddl += f"  {row_str.strip()},\n"
            ddl = ddl.rstrip(",\n") + "\n)"
            lines.append(ddl)
        return "\n\n".join(lines)
    except Exception:
        return f"表: {DUCKDB_TABLE_NAME}"


# ============================================================
# 3. SQL Agent 封装（直接调用 LLM + DuckDBWrapper，无 SQLAlchemy）
# ============================================================

SQL_EXTRACT_RE = re.compile(
    r"(?:```sql\s*|```\s*)((?:SELECT|select|WITH|with)\b.*?)(?:```|\Z)",
    re.DOTALL,
)


class _SimpleSQLAgent:
    """
    轻量级 SQL Agent：
    - 不依赖 LangChain ReAct Agent 框架
    - 不依赖 SQLAlchemy / SQLDatabase
    - 直接通过 DuckDBWrapper 已有连接执行 SQL
    - 兼容 agent.invoke({"input": "..."}) 接口
    """

    def __init__(self, llm, db):
        self._llm = llm
        self._db = db
        self._schema = _build_schema_string(db)

        # 从 DuckDBWrapper 获取真实表名
        try:
            table_name = db.get_usable_table_names()[0]
        except Exception:
            table_name = DUCKDB_TABLE_NAME

        self._system = f"""你是一个保时捷测试工单数据库的 SQL 专家。请根据用户的问题生成 DuckDB SQL 查询。

数据库表名: {table_name}

表结构:
- Number: 工单编号 (整数)
- "Change-TS problem": 工单变更时间戳
- "Date": 工单创建日期
- "Short Text": 问题标题 (简短描述)
- "Problem Description": 问题详细描述
- "Functionality": 功能模块编号 (如 SY 0000007, CF 0000001)
- "Functionality.1": 功能模块全称 (如 System - download, Car-Functions - Performance App)
- "Status": 工单状态 (如 under way, Release OK, taken over 等)
- "Responsible Problem Solver": 负责人姓名
- "SW (causing)": 问题软件版本 (如 5045)
- "HW (causing)": 问题硬件版本 (如 H06)
- "Fault frequency": 故障频率 (One-Off, Repeatedly, Constant)
- "Rating": 严重等级 (1-6, 数字越小越严重)
- "Country": 国家/地区
- "Project": 项目编号

重要规则:
1. 只能生成 SELECT 查询，严格禁止 INSERT/UPDATE/DELETE/DROP/ALTER/TRUNCATE
2. 带空格或特殊字符的列名必须用双引号括起来，如 "Change-TS problem"
3. 使用中文注释解释你的查询逻辑（用 /* ... */ 格式）
4. 始终优先使用 "Change-TS problem" 作为时间相关查询的列
5. DuckDB 不支持 PostgreSQL 特有函数，请使用标准 SQL
6. COUNT(*) 时用 AS cnt 作为别名

请直接输出 DuckDB SQL 查询，用 ```sql ... ``` 包裹。"""

    def invoke(self, inputs: dict) -> dict:
        """兼容 AgentExecutor.invoke({"input": "..."}) 接口。"""
        question = inputs.get("input", "")
        if not question:
            return {"output": "问题不能为空", "intermediate_steps": []}

        steps = []
        try:
            # Step 1: LLM 生成 SQL
            prompt = f"{self._system}\n\n用户问题: {question}\n\nSQL 查询:"
            llm_response = self._llm.invoke(prompt)
            llm_text = llm_response.content if hasattr(llm_response, "content") else str(llm_response)
            steps.append(("LLM 推理", llm_text))

            # Step 2: 从 LLM 回复中提取 SQL
            sql = self._extract_sql(llm_text)
            if not sql:
                return {
                    "output": f"AI 未能生成有效 SQL。回复原文:\n{llm_text[:500]}",
                    "intermediate_steps": steps,
                }

            steps.append(("生成 SQL", sql))

            # Step 3: 通过 DuckDBWrapper 执行 SQL
            result = self._db.run(sql)
            steps.append(("执行结果", result))

            return {"output": result, "intermediate_steps": steps}

        except Exception as e:
            return {"output": f"查询执行失败: {type(e).__name__}: {str(e)[:300]}", "intermediate_steps": steps}

    @staticmethod
    def _extract_sql(text: str) -> str:
        """
        从 LLM 回复中提取纯 SQL。
        彻底清除所有 Markdown 代码块标记（```sql / ```），确保输出纯文本。
        """
        sql = ""

        # 策略1：正则提取 ```sql ... ``` 代码块中的内容
        m = SQL_EXTRACT_RE.search(text)
        if m:
            sql = m.group(1).strip()

        # 策略2：回退 — 逐行提取，遇到 ``` 立即停止
        if not sql:
            lines = text.strip().split("\n")
            sql_lines = []
            in_sql = False
            for line in lines:
                stripped = line.strip()
                if re.match(r"^(SELECT|select|WITH|with)\b", stripped):
                    in_sql = True
                if in_sql:
                    # [修复] 遇到 Markdown 代码块结束标记则停止提取
                    if stripped.startswith("```"):
                        break
                    sql_lines.append(stripped)
            if sql_lines:
                sql = "\n".join(sql_lines)

        # [修复] 兜底清洗：彻底移除任何残留的 Markdown 标记
        sql = re.sub(r"^```\w*\s*", "", sql)
        sql = re.sub(r"```\s*$", "", sql)
        sql = sql.strip()

        return sql


# ============================================================
# 4. 工厂函数（兼容原有 API）
# ============================================================

def create_sql_agent_with_llm(llm, db):
    """
    创建 SQL Agent（直接调用 LLM + DuckDBWrapper，无 SQLAlchemy）。

    Args:
        llm: ChatOpenAI 实例。
        db:  DuckDBWrapper 实例（复用其已有连接执行 SQL）。

    Returns:
        (agent, error_message)
        agent 是一个类 AgentExecutor 对象，支持 invoke({"input": "..."})。
    """
    if not LANGCHAIN_SQL_AVAILABLE:
        return None, "langchain-community 未完整安装"

    if llm is None:
        return None, "LLM 未初始化，无法创建 Agent"

    if db is None:
        return None, "数据库未加载，无法创建 Agent"

    try:
        agent = _SimpleSQLAgent(llm, db)
        return agent, None
    except Exception as e:
        return None, f"创建 SQL Agent 失败: {type(e).__name__}: {str(e)[:200]}"


# ============================================================
# 5. 示例问题 & 安全执行
# ============================================================

EXAMPLE_QUESTIONS = [
    "统计各状态的工单数量",
    "本月新增了多少个工单？",
    "软件版本 5045 对应的问题有哪些？",
    "严重等级 1 的工单有多少个？",
    "功能模块 Speech - general 有多少个未解决工单？",
    "谁负责的工单最多？",
    "近 7 天每天新增多少工单？",
]


def safe_invoke_agent(agent, question: str) -> dict:
    """
    安全调用 agent.invoke，统一异常处理。

    Args:
        agent:    _SimpleSQLAgent 或兼容 AgentExecutor 实例。
        question: 用户自然语言问题。

    Returns:
        包含 'output' 和 'intermediate_steps' 的 dict。
    """
    try:
        return agent.invoke({"input": question})
    except Exception as e:
        return {
            "output": f"查询执行失败: {type(e).__name__}: {str(e)[:300]}",
            "intermediate_steps": [],
        }


# ============================================================
# 6. AI 自由对话功能（不限于 SQL 查询，支持通用问答）
# ============================================================

CHAT_SYSTEM_PROMPT = """你是保时捷测试工单智能分析系统的 AI 助手。你可以：
1. 回答关于测试工单数据的问题（统计数据、趋势分析等）
2. 解释工单状态、功能模块、严重等级等业务概念
3. 提供数据分析建议和排查思路
4. 用中文友好地与用户交流

以下是当前加载的数据概况，请据此回答用户问题：
{data_context}

注意事项：
- 回答时引用数据请注明来源（基于当前加载的工单数据）
- 如果用户的问题涉及具体数值查询，建议他使用左侧的「SQL 查询」模式
- 保持专业但友好的语气"""


def _build_data_context(df) -> str:
    """从 DataFrame 构建数据摘要文本，作为 LLM 对话的上下文。"""
    if df is None or len(df) == 0:
        return "当前没有加载数据。"

    lines = [f"- 总工单数: {len(df):,}"]

    # 常用字段统计
    for col, label in [
        ("Status", "工单状态"),
        ("Functionality.1", "功能模块"),
        ("Responsible Problem Solver", "负责人"),
        ("Fault frequency", "故障频率"),
        ("Rating", "严重等级"),
    ]:
        if col in df.columns:
            vc = df[col].dropna().value_counts()
            if len(vc) > 0:
                top = vc.head(10).to_dict()
                items = ", ".join(f"{k}: {v}" for k, v in top.items())
                lines.append(f"- {label} Top {len(top)}: {items}")

    # 时间范围
    for time_col in ["Change-TS problem", "Date"]:
        if time_col in df.columns:
            try:
                dates = pd.to_datetime(df[time_col], errors="coerce").dropna()
                if len(dates) > 0:
                    lines.append(f"- {time_col} 范围: {dates.min()} ~ {dates.max()}")
                    break
            except Exception:
                pass

    # 列名列表（帮助 LLM 了解可用字段）
    lines.append(f"- 所有字段: {', '.join(df.columns.tolist())}")

    return "\n".join(lines)


def chat_with_llm(llm, df, messages: list) -> str:
    """
    调用 LLM 进行自由对话，自动注入数据上下文。

    Args:
        llm: ChatOpenAI 实例。
        df:  已加载的 DataFrame（用于构建数据摘要上下文）。
        messages: 对话历史列表，格式为 [{"role": "user"|"assistant", "content": "..."}]。

    Returns:
        AI 回复文本。
    """
    try:
        data_context = _build_data_context(df)
        system_msg = CHAT_SYSTEM_PROMPT.format(data_context=data_context)

        # 构建 LangChain 消息列表
        from langchain_core.messages import SystemMessage, HumanMessage, AIMessage

        lc_messages = [SystemMessage(content=system_msg)]
        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if role == "user":
                lc_messages.append(HumanMessage(content=content))
            elif role == "assistant":
                lc_messages.append(AIMessage(content=content))

        response = llm.invoke(lc_messages)
        return response.content if hasattr(response, "content") else str(response)
    except Exception as e:
        return f"对话出错: {type(e).__name__}: {str(e)[:300]}"
