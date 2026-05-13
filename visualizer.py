#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
================================================================================
visualizer.py — 可视化仪表盘模块
保时捷测试工单智能分析系统 | 模块化重构版 v4.0
================================================================================
职责：
 - 所有 Streamlit 图表渲染函数
 - 概览卡片（总数/今日新增/未解决/平均严重等级）
 - 时间趋势图、状态饼图、功能模块柱状图、负责人统计
 - 软硬件版本分布、严重等级、故障频率
================================================================================
"""

from datetime import date

import streamlit as st
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from plotly.subplots import make_subplots


# ============================================================
# 1. 概览卡片
# ============================================================

def render_overview_cards(df: pd.DataFrame) -> None:
    """
    顶部四列概览卡片。
    - 总工单数
    - 今日新增
    - 未解决工单
    - 平均严重等级
    """
    col1, col2, col3, col4 = st.columns(4)

    # 总工单数
    with col1:
        st.metric("总工单数", f"{len(df):,}")

    # 今日新增
    with col2:
        today_count = 0
        if "Date" in df.columns:
            today_str = date.today().strftime("%Y-%m-%d")
            try:
                today_count = len(
                    df[df["Date"].astype(str).str.contains(today_str, na=False)]
                )
            except Exception:
                pass
        st.metric("今日新增", f"{today_count:,}")

    # 未解决工单
    with col3:
        open_count = 0
        if "Status" in df.columns:
            try:
                open_count = len(
                    df[df["Status"].astype(str).str.contains(
                        "under way|open|taken over", case=False, na=False
                    )]
                )
            except Exception:
                pass
        st.metric("未解决工单", f"{open_count:,}")

    # 平均严重等级
    with col4:
        avg_rating = None
        if "Rating" in df.columns:
            try:
                avg_rating = pd.to_numeric(df["Rating"], errors="coerce").mean()
            except Exception:
                pass
        st.metric(
            "平均严重等级",
            f"{avg_rating:.2f}" if avg_rating is not None and pd.notna(avg_rating) else "N/A",
        )


# ============================================================
# 2. 时间趋势图
# ============================================================

def render_time_trend_chart(df: pd.DataFrame) -> None:
    """
    每日新增工单趋势（柱状图 + 7 日移动平均线）。
    """
    if "Date" not in df.columns:
        st.info("缺少 Date 列，无法生成时间趋势图")
        return

    try:
        daily = df["Date"].dropna().value_counts().sort_index()
        if len(daily) == 0:
            st.info("无有效日期数据")
            return

        daily_df = pd.DataFrame({
            "日期": daily.index,
            "新增工单": daily.values,
        }).sort_values("日期")

        # 7 日移动平均
        daily_df["7日移动平均"] = (
            daily_df["新增工单"].rolling(window=7, min_periods=1).mean()
        )

        fig = make_subplots(specs=[[{"secondary_y": False}]])

        fig.add_trace(
            go.Bar(
                name="每日新增",
                x=daily_df["日期"],
                y=daily_df["新增工单"],
                marker_color="#4a90d9",
                opacity=0.7,
            ),
        )
        fig.add_trace(
            go.Scatter(
                name="7日移动平均",
                x=daily_df["日期"],
                y=daily_df["7日移动平均"],
                line=dict(color="#e74c3c", width=3),
                mode="lines",
            ),
        )

        fig.update_layout(
            title="📈 每日新增工单趋势 (含7日移动平均)",
            xaxis_title="日期",
            yaxis_title="工单数量",
            hovermode="x unified",
            height=400,
        )
        st.plotly_chart(fig, use_container_width=True)

    except Exception as e:
        st.warning(f"时间趋势图生成失败: {str(e)[:80]}")


# ============================================================
# 3. 工单状态饼图
# ============================================================

def render_status_pie(df: pd.DataFrame) -> None:
    """各状态工单占比（饼图）"""
    if "Status" not in df.columns:
        return
    try:
        status_counts = df["Status"].value_counts()
        fig = px.pie(
            values=status_counts.values,
            names=status_counts.index,
            title="📊 工单状态分布",
            color_discrete_sequence=px.colors.qualitative.Set3,
        )
        fig.update_traces(textposition="inside", textinfo="percent+label")
        st.plotly_chart(fig, use_container_width=True)
    except Exception as e:
        st.warning(f"状态图失败: {str(e)[:80]}")


# ============================================================
# 4. 功能模块柱状图（Top10）
# ============================================================

def render_functionality_chart(df: pd.DataFrame) -> None:
    """Top10 问题最多功能模块（水平柱状图）"""
    func_col = "Functionality.1" if "Functionality.1" in df.columns else "Functionality"
    if func_col not in df.columns:
        return
    try:
        top = df[func_col].value_counts().head(10)
        fig = px.bar(
            x=top.values,
            y=top.index,
            orientation="h",
            title="🔝 Top10 问题最多功能模块",
            labels={"x": "工单数量", "y": "功能模块"},
            color=top.values,
            color_continuous_scale="Blues",
        )
        fig.update_layout(height=400, yaxis={"categoryorder": "total ascending"})
        st.plotly_chart(fig, use_container_width=True)
    except Exception as e:
        st.warning(f"功能模块图失败: {str(e)[:80]}")


# ============================================================
# 5. 负责人工作量图（Top10）
# ============================================================

def render_person_chart(df: pd.DataFrame) -> None:
    """Top10 负责人工单量（水平柱状图）"""
    if "Responsible Problem Solver" not in df.columns:
        return
    try:
        top = df["Responsible Problem Solver"].value_counts().head(10)
        fig = px.bar(
            x=top.values,
            y=top.index,
            orientation="h",
            title="👤 Top10 负责人工作量",
            labels={"x": "工单数量", "y": "负责人"},
            color=top.values,
            color_continuous_scale="Greens",
        )
        fig.update_layout(height=400, yaxis={"categoryorder": "total ascending"})
        st.plotly_chart(fig, use_container_width=True)
    except Exception as e:
        st.warning(f"负责人图失败: {str(e)[:80]}")


# ============================================================
# 6. 软件/硬件版本分布
# ============================================================

def render_sw_hw_charts(df: pd.DataFrame) -> None:
    """双列布局：软件版本 + 硬件版本 Top10"""
    col1, col2 = st.columns(2)

    with col1:
        if "SW (causing)" in df.columns:
            try:
                sw = df["SW (causing)"].value_counts().head(10)
                fig = px.bar(
                    x=sw.index,
                    y=sw.values,
                    title="💾 软件版本分布 (Top10)",
                    labels={"x": "软件版本", "y": "工单数量"},
                    color=sw.values,
                    color_continuous_scale="Reds",
                )
                st.plotly_chart(fig, use_container_width=True)
            except Exception as e:
                st.info(f"SW 图表不可用: {str(e)[:50]}")

    with col2:
        if "HW (causing)" in df.columns:
            try:
                hw = df["HW (causing)"].value_counts().head(10)
                fig = px.bar(
                    x=hw.index,
                    y=hw.values,
                    title="🔧 硬件版本分布 (Top10)",
                    labels={"x": "硬件版本", "y": "工单数量"},
                    color=hw.values,
                    color_continuous_scale="Purples",
                )
                st.plotly_chart(fig, use_container_width=True)
            except Exception as e:
                st.info(f"HW 图表不可用: {str(e)[:50]}")


# ============================================================
# 7. 严重等级分布
# ============================================================

def render_rating_chart(df: pd.DataFrame) -> None:
    """严重等级（1-6）柱状图，1=最严重"""
    if "Rating" not in df.columns:
        return
    try:
        vals = pd.to_numeric(df["Rating"], errors="coerce").dropna()
        if len(vals) == 0:
            return
        rating_counts = vals.value_counts().sort_index()
        fig = px.bar(
            x=rating_counts.index,
            y=rating_counts.values,
            title="⚠️ 严重等级分布 (1=最严重)",
            labels={"x": "严重等级", "y": "工单数量"},
            color=rating_counts.index,
            color_continuous_scale="RdYlGn_r",
        )
        fig.update_layout(xaxis=dict(tickmode="linear", dtick=1))
        st.plotly_chart(fig, use_container_width=True)
    except Exception as e:
        st.warning(f"严重等级图失败: {str(e)[:80]}")


# ============================================================
# 8. 故障频率分布
# ============================================================

def render_fault_frequency(df: pd.DataFrame) -> None:
    """故障频率（One-Off / Repeatedly / Constant）饼图"""
    if "Fault frequency" not in df.columns:
        return
    try:
        freq = df["Fault frequency"].value_counts()
        fig = px.pie(
            values=freq.values,
            names=freq.index,
            title="🔄 故障频率分布",
            color_discrete_sequence=px.colors.qualitative.Pastel,
        )
        fig.update_traces(textposition="inside", textinfo="percent+label")
        st.plotly_chart(fig, use_container_width=True)
    except Exception:
        pass


# ============================================================
# 9. 一键渲染所有仪表盘
# ============================================================

def render_full_dashboard(df: pd.DataFrame) -> None:
    """
    渲染完整仪表盘页面（按原布局顺序）。
    供 main.py 在 "可视化仪表盘" Tab 中调用。
    """
    # 时间趋势
    render_time_trend_chart(df)

    # 状态饼图 + 故障频率（双列）
    c1, c2 = st.columns(2)
    with c1:
        render_status_pie(df)
    with c2:
        render_fault_frequency(df)

    # 功能模块 + 负责人（双列）
    c1, c2 = st.columns(2)
    with c1:
        render_functionality_chart(df)
    with c2:
        render_person_chart(df)

    # 软硬件版本
    render_sw_hw_charts(df)

    # 严重等级
    render_rating_chart(df)
