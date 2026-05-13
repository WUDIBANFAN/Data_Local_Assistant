#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
================================================================================
main.py — Streamlit 主入口（极简编排）
保时捷测试工单智能分析系统 | 模块化重构版 v4.0
================================================================================
运行: streamlit run main.py

架构:
  config.py      → 全局配置
  data_loader.py → CSV 扫描/读取/清洗/DuckDB 写入
  visualizer.py  → 所有图表渲染
  ai_query.py    → LLM + SQL Agent
  utils.py       → 日志/导出/周报/异常处理

所有模块通过显式 import 组合，无循环依赖。
================================================================================
"""

import os
import sys
import traceback
from datetime import datetime, date, timedelta

import streamlit as st
import pandas as pd

# ---- 项目模块 ----
from config import DB_PATH
from data_loader import (
    scan_csv_files,
    find_all_csvs,
    load_csvs_to_duckdb,
    load_uploaded_files_to_duckdb,
    close_global_connection,
)
from visualizer import (
    render_overview_cards,
    render_full_dashboard,
)
from ai_query import (
    init_llm,
    create_sql_agent_with_llm,
    safe_invoke_agent,
    EXAMPLE_QUESTIONS,
    chat_with_llm,
)
from utils import (
    log_info,
    export_csv_bytes,
    generate_weekly_report,
    render_file_info,
    safe_print_traceback,
)


# ============================================================
# 页面配置（必须为第一个 Streamlit 命令）
# ============================================================
st.set_page_config(
    page_title="保时捷测试工单智能分析系统",
    page_icon="🏎️",
    layout="wide",
)


# ============================================================
# 辅助：初始化或刷新 session state
# ============================================================
def _init_session():
    """确保 session state 关键字段已初始化"""
    defaults = {
        "df": None,
        "db": None,
        "llm": None,
        "llm_error": "",
        "_csv_paths": [],
        "_csv_summary": "",
        "_question": "",
        "_upload_source": "",  # "onedrive" 或 "upload"
        "_right_nav_open": False,
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v


# ============================================================
# 侧边栏：时间范围选择 + 加载按钮
# ============================================================
def _render_sidebar_loader():
    """侧边栏顶部：时间范围选择器 + 加载 CSV 按钮 + 拖拽上传"""
    st.sidebar.header("📂 数据加载")

    # ---- 方式 A：从 OneDrive 目录加载 ----
    all_csvs = find_all_csvs()
    has_onedrive_files = bool(all_csvs)

    if has_onedrive_files:
        csv_dates = sorted({d for _, _, _, d in all_csvs if d})
        min_d = csv_dates[0] if csv_dates else None
        max_d = csv_dates[-1] if csv_dates else None

        if min_d is None:
            st.sidebar.warning("无法从文件名识别日期")
            return None, None, False, []

        # 默认加载最新 7 天
        default_start = max_d - timedelta(days=6)
        if default_start < min_d:
            default_start = min_d

        col_a, col_b = st.sidebar.columns(2)
        with col_a:
            start_date = st.date_input(
                "开始日期",
                value=default_start,
                min_value=min_d,
                max_value=max_d,
                key="sidebar_start",
            )
        with col_b:
            end_date = st.date_input(
                "结束日期",
                value=max_d,
                min_value=min_d,
                max_value=max_d,
                key="sidebar_end",
            )

        if start_date > end_date:
            start_date, end_date = end_date, start_date

        st.sidebar.caption(
            f"数据范围: {min_d} ~ {max_d}  |  共 {len(all_csvs)} 个文件"
        )

        load_clicked = st.sidebar.button(
            "📥 加载选中时间段",
            type="primary",
            use_container_width=True,
        )
    else:
        st.sidebar.warning("OneDrive 目录未找到 CSV 文件")
        start_date, end_date, load_clicked = None, None, False

    st.sidebar.markdown("---")
    st.sidebar.markdown("**或** 拖拽上传 CSV 文件：")

    # ---- 方式 B：拖拽上传 CSV 文件 ----
    uploaded_files = st.sidebar.file_uploader(
        "拖拽 CSV 文件到此处",
        type=["csv"],
        accept_multiple_files=True,
        key="csv_uploader",
        help="支持同时上传多个 CSV 文件（文件名不限）",
    )

    return start_date, end_date, load_clicked, uploaded_files


# ============================================================
# 侧边栏：数据筛选器
# ============================================================
def _render_sidebar_filters(df: pd.DataFrame) -> pd.DataFrame:
    """侧边栏：状态/功能模块/负责人筛选器，返回筛选后的 DataFrame"""
    st.sidebar.markdown("---")
    st.sidebar.header("🔎 数据筛选")

    filtered = df.copy()

    # 日期范围
    if "Date" in filtered.columns:
        try:
            dates = pd.to_datetime(filtered["Date"], errors="coerce").dropna()
            if len(dates) > 0:
                min_date = dates.min().date()
                max_date = dates.max().date()
                date_range = st.sidebar.date_input(
                    "日期范围",
                    value=(min_date, max_date),
                    min_value=min_date,
                    max_value=max_date,
                    key="filter_date",
                )
                if len(date_range) == 2:
                    sd, ed = date_range
                    filtered = filtered[
                        (dates.dt.date >= sd) & (dates.dt.date <= ed)
                    ]
        except Exception:
            pass

    # 状态筛选
    if "Status" in filtered.columns:
        try:
            opts = ["全部"] + sorted(filtered["Status"].dropna().unique().tolist())
            sel = st.sidebar.selectbox("工单状态", opts, key="filter_status")
            if sel != "全部":
                filtered = filtered[filtered["Status"] == sel]
        except Exception:
            pass

    # 功能模块筛选
    func_col = (
        "Functionality.1" if "Functionality.1" in filtered.columns
        else "Functionality"
    )
    if func_col in filtered.columns:
        try:
            opts = ["全部"] + sorted(filtered[func_col].dropna().unique().tolist())
            sel = st.sidebar.selectbox("功能模块", opts, key="filter_func")
            if sel != "全部":
                filtered = filtered[filtered[func_col] == sel]
        except Exception:
            pass

    # 负责人筛选
    if "Responsible Problem Solver" in filtered.columns:
        try:
            opts = ["全部"] + sorted(
                filtered["Responsible Problem Solver"].dropna().unique().tolist()
            )
            sel = st.sidebar.selectbox("负责人", opts, key="filter_person")
            if sel != "全部":
                filtered = filtered[filtered["Responsible Problem Solver"] == sel]
        except Exception:
            pass

    st.sidebar.markdown(f"**筛选后**: {len(filtered):,} 条工单")
    return filtered


# ============================================================
# Tab: 可视化仪表盘
# ============================================================
def _render_tab_dashboard(filtered_df: pd.DataFrame):
    render_full_dashboard(filtered_df)


# ============================================================
# Tab: 智能查询（SQL查询 + AI对话 双模式）
# ============================================================
def _render_tab_query(filtered_df: pd.DataFrame):
    llm = st.session_state.llm
    db = st.session_state.db

    if llm is None:
        err = st.session_state.get("llm_error", "未知错误")
        st.warning(f"⚠️ 大模型未初始化: {err}")
        st.info("请检查 .env 文件中的 LLM_API_KEY 配置")
        return

    # 子 Tab 切换：SQL 查询 / AI 对话
    sub_tab1, sub_tab2 = st.tabs(["🔍 SQL 数据查询", "💬 AI 对话"])

    # ==================== 子 Tab 1: SQL 数据查询 ====================
    with sub_tab1:
        st.markdown("用中文提问，AI 自动生成 SQL 查询数据库。")

        if db is None:
            st.warning("⚠️ 数据库未加载，请先加载 CSV 数据")
            return

        # 示例问题
        with st.expander("💡 点击查看示例问题", expanded=False):
            for eq in EXAMPLE_QUESTIONS:
                if st.button(eq, key=f"eq_{eq[:20]}", use_container_width=True):
                    st.session_state._question = eq
                    st.session_state.query_input = eq
                    st.rerun()

        # 输入框
        question = st.text_input(
            "输入你的问题",
            value=st.session_state.get("_question", ""),
            placeholder="例如：统计本周各状态的工单数量",
            key="query_input",
        )

        if st.button("🔍 执行查询", type="primary", disabled=not question):
            st.session_state._question = ""
            with st.spinner("🤖 AI 正在分析..."):
                agent, error = create_sql_agent_with_llm(llm, db)
                if agent:
                    response = safe_invoke_agent(agent, question)
                    st.markdown("#### 📋 查询结果")
                    st.write(response.get("output", "无输出"))

                    steps = response.get("intermediate_steps", [])
                    if steps:
                        for step in steps:
                            if isinstance(step, tuple) and len(step) >= 2:
                                step_name, step_detail = step[0], step[1]
                                if step_name == "生成 SQL" or "sql" in str(step_name).lower():
                                    with st.expander("🔍 查看生成的 SQL"):
                                        st.code(str(step_detail), language="sql")
                else:
                    st.error(f"创建 Agent 失败: {error}")

    # ==================== 子 Tab 2: AI 自由对话 ====================
    with sub_tab2:
        st.markdown("与 AI 自由对话，提问数据分析建议、业务概念解释等。")

        # 初始化聊天历史
        if "_chat_messages" not in st.session_state:
            st.session_state._chat_messages = []

        # 清空对话按钮
        if st.session_state._chat_messages:
            if st.button("🗑️ 清空对话", key="clear_chat"):
                st.session_state._chat_messages = []
                st.rerun()

        # 渲染历史消息
        chat_container = st.container()
        with chat_container:
            for msg in st.session_state._chat_messages:
                role = msg["role"]
                avatar = "🤖" if role == "assistant" else "🧑"
                with st.chat_message(role, avatar=avatar):
                    st.markdown(msg["content"])

        # 输入框（用 st.chat_input，固定在底部）
        if prompt := st.chat_input("输入你的问题..."):
            # 用户消息
            st.session_state._chat_messages.append({"role": "user", "content": prompt})

            with chat_container:
                with st.chat_message("user", avatar="🧑"):
                    st.markdown(prompt)

                with st.chat_message("assistant", avatar="🤖"):
                    with st.spinner("思考中..."):
                        reply = chat_with_llm(
                            llm, st.session_state.df,
                            st.session_state._chat_messages,
                        )
                    st.markdown(reply)
                    st.session_state._chat_messages.append({"role": "assistant", "content": reply})


# ============================================================
# Tab: 原始数据
# ============================================================
def _render_tab_data(filtered_df: pd.DataFrame):
    st.markdown("### 📋 原始数据")
    st.dataframe(filtered_df, use_container_width=True, height=500)

    # CSV 导出
    csv_bytes = export_csv_bytes(filtered_df)
    st.download_button(
        label="📥 下载筛选结果为 CSV",
        data=csv_bytes,
        file_name=f"tickets_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
        mime="text/csv",
    )


# ============================================================
# Tab: 周报
# ============================================================
def _render_tab_report(filtered_df: pd.DataFrame):
    if st.button("📝 一键生成本周报告", type="primary", use_container_width=True):
        generate_weekly_report(filtered_df, st.session_state.llm)
    else:
        st.info("点击上方按钮，自动生成本周工单分析报告")


# ============================================================
# 右侧可收起导航栏（摸鱼专区）— 纯 Streamlit 原生实现
# ============================================================

def _render_right_nav_content():
    """渲染右侧导航栏内容（摸鱼链接列表）。"""
    nav_links = [
        ("🍔", "今天吃什么", "https://yanweb.top/moyu/eat/"),
        ("🐱", "圈小猫", "https://yanweb.top/moyu/catch-the-cat/"),
        ("💣", "扫雷", "https://yanweb.top/moyu/saolei/"),
        ("🎮", "2048", "https://yanweb.top/moyu/2048/"),
        ("⚫", "五子棋", "https://www.yanweb.top/moyu/five-in-a-row/"),
        ("🔢", "数独", "https://www.yanweb.top/moyu/Sudoku/"),
    ]
    st.markdown("#### 🐟 摸鱼专区")
    st.divider()
    for icon, name, url in nav_links:
        st.markdown(f"[{icon} {name}]({url})")


# ============================================================
# 主入口
# ============================================================
def main():
    _init_session()

    # ========== 右侧可收起导航栏（摸鱼专区）==========
    # 用 st.popover 实现浮动面板，纯 Streamlit 原生组件，无 JS
    _col_title, _col_toggle = st.columns([24, 1])
    with _col_title:
        st.title("🏎️ 保时捷测试工单智能分析系统")
    with _col_toggle:
        with st.popover("🐟", use_container_width=True):
            _render_right_nav_content()

    # ========== 1. 加载数据 ==========
    start_d, end_d, load_clicked, uploaded_files = _render_sidebar_loader()

    # ---- 优先处理上传文件 ----
    if uploaded_files:
        needs_upload_load = (
            st.session_state._upload_source != "upload"
            or st.session_state.df is None
        )
        if needs_upload_load:
            try:
                # [修复] 先关闭旧的 DuckDBWrapper 连接，再写入 db 文件
                # 否则 Windows 上 DuckDB 文件被占用，_safe_remove 会失败
                close_global_connection()
                with st.spinner(f"📂 正在加载 {len(uploaded_files)} 个上传文件..."):
                    progress_bar = st.progress(0, text="准备加载...")

                    def _on_progress(current, total, msg):
                        pct = min(current / total, 1.0) if total > 0 else 0
                        progress_bar.progress(pct, text=msg)

                    summary, db, df = load_uploaded_files_to_duckdb(
                        uploaded_files, DB_PATH,
                        progress_callback=_on_progress,
                    )
                    progress_bar.empty()

                st.session_state._csv_paths = []
                st.session_state._csv_summary = summary
                st.session_state.db = db
                st.session_state.df = df
                st.session_state._upload_source = "upload"
                st.success(f"✅ {summary}")
                log_info("MAIN", f"上传加载完成: {len(df):,} 条工单")
            except Exception as e:
                st.error(f"⛔ 上传文件加载失败: {str(e)[:300]}")
                if st.session_state.df is not None:
                    st.warning("⚠️ 显示缓存数据")
                else:
                    st.stop()

    # ---- OneDrive 目录加载 ----
    elif start_d is not None:
        needs_load = load_clicked or st.session_state.df is None
        if needs_load:
            try:
                csv_list = scan_csv_files(start_date=start_d, end_date=end_d)
                if not csv_list:
                    st.warning(f"⚠️ {start_d} ~ {end_d} 区间内无 CSV 文件")
                    if st.session_state.df is not None:
                        st.info("显示上次加载的数据")
                    elif st.session_state._upload_source == "upload":
                        st.info("显示上次上传的数据")
                    else:
                        st.stop()
                else:
                    csv_paths = [fp for fp, _, _, _ in csv_list]
                    log_info("MAIN", f"加载 {len(csv_paths)} 个文件: {start_d} ~ {end_d}")

                    # [修复] 先关闭旧 DuckDBWrapper 连接，再重建 db 文件
                    close_global_connection()

                    with st.spinner(f"📂 正在加载 {start_d} ~ {end_d} 的 CSV..."):
                        progress_bar = st.progress(0, text="准备加载...")
                        total_files = len(csv_paths)

                        def _on_progress(current, total, msg):
                            pct = min(current / total, 1.0) if total > 0 else 0
                            progress_bar.progress(pct, text=msg)

                        summary, db, df = load_csvs_to_duckdb(
                            csv_paths, DB_PATH,
                            progress_callback=_on_progress,
                        )
                        progress_bar.empty()

                    st.session_state._csv_paths = csv_paths
                    st.session_state._csv_summary = summary
                    st.session_state.db = db
                    st.session_state.df = df
                    st.session_state._upload_source = "onedrive"

                    st.success(f"✅ {summary}")
                    log_info("MAIN", f"加载完成: {len(df):,} 条工单")

            except RuntimeError as e:
                st.error(f"⛔ 加载失败: {str(e)}")
                st.info("💡 请确认 OneDrive 同步完成后再试")
                if st.session_state.df is not None:
                    st.warning("⚠️ 显示上次缓存数据")
                else:
                    st.stop()

            except Exception as e:
                st.error(f"⛔ 未知错误: {str(e)[:300]}")
                tb = safe_print_traceback()
                try:
                    import sys as _s
                    print(tb, file=_s.stderr, flush=True)
                except Exception:
                    pass
                if st.session_state.df is not None:
                    st.warning("⚠️ 显示缓存数据")
                else:
                    st.stop()

    # 获取已加载的数据
    db = st.session_state.db
    df = st.session_state.df

    if df is None or len(df) == 0:
        st.stop()

    # 初始化 LLM（首次）
    if st.session_state.llm is None and "_llm_init_done" not in st.session_state:
        st.session_state.llm, st.session_state.llm_error = init_llm()
        st.session_state._llm_init_done = True

    # 文件信息
    upload_source = st.session_state.get("_upload_source", "")
    csv_paths = st.session_state.get("_csv_paths", [])
    current_path = csv_paths[0] if csv_paths else upload_source
    render_file_info(current_path, df)

    # ========== 2. 筛选器 ==========
    filtered_df = _render_sidebar_filters(df)

    # ========== 3. 概览卡片 ==========
    st.markdown("---")
    st.subheader("📊 数据概览")
    render_overview_cards(filtered_df)

    # ========== 4. Tab 布局 ==========
    tab1, tab2, tab3, tab4 = st.tabs([
        "📈 可视化仪表盘",
        "💬 智能查询",
        "📋 原始数据",
        "📝 周报",
    ])

    with tab1:
        _render_tab_dashboard(filtered_df)
    with tab2:
        _render_tab_query(filtered_df)
    with tab3:
        _render_tab_data(filtered_df)
    with tab4:
        # 周报基于全量数据，不受侧边栏筛选影响
        _render_tab_report(df)


if __name__ == "__main__":
    main()
