# 🏎️ 保时捷测试工单智能分析系统

**模块化重构版 v4.0** | Streamlit + DuckDB + LangChain (OpenAI Compatible) + Plotly

---

## 📋 项目简介

本系统为保时捷（Porsche）Infotainment 部门 MLBevo China 团队开发的**测试工单智能分析与可视化平台**。数据来源于 OneDrive 同步的保时捷测试管理系统导出 CSV 文件（`RechercheExport_*.csv`），支持自动加载、清洗、可视化分析、AI 自然语言查询、AI 自由对话和一键周报生成等全流程功能。

### 核心亮点

| 亮点 | 说明 |
|------|------|
| 📂 双模式数据加载 | OneDrive 目录按时间段扫描 / 浏览器拖拽上传，自动处理 OneDrive 文件锁 |
| 🧹 智能数据清洗 | 自动检测编码（utf-8-sig/latin-1/gbk）、跳过元数据行、德式数字格式转换、时间列识别 |
| 📊 8 种交互式图表 | 基于 Plotly 的趋势图/状态饼图/模块排行/人员统计/SW-HW分布/严重等级/故障频率 |
| 💬 NL2SQL 智能查询 | 用中文提问，DeepSeek / OpenAI 等大模型自动生成 DuckDB SQL 并执行查询 |
| 🤖 AI 自由对话 | 多轮聊天界面，自动注入当前数据上下文，支持数据分析建议与业务咨询 |
| 📝 一键周报 | 本周统计汇总（总数/处理中/Top3模块/Top3负责人）+ AI 智能总结 |
| 🐟 摸鱼专区 | 右侧导航面板，内置今天吃什么/圈小猫/扫雷/2048/五子棋/数独快捷入口 |

---

## 📁 项目结构

```
Data_Local_Assistant/
├── main.py              # Streamlit 主入口（页面编排 / Session 管理 / 布局）
├── config.py            # 全局配置中心（路径 / 常量 / LLM 配置 / 环境变量）
├── data_loader.py       # 数据加载核心（CSV 扫描 / OneDrive 锁重试 / 清洗 / DuckDB 写入）
├── visualizer.py        # 可视化仪表盘（8 种 Plotly 交互式图表渲染函数）
├── ai_query.py          # AI 查询模块（NL2SQL Agent / AI 自由对话 / LLM 初始化）
├── utils.py             # 工具函数库（日志 / CSV 导出 / 周报生成 / 异常处理）
├── ticket_analyzer.py   # v3.0 旧版单文件版本（已弃用，保留作参考）
├── .env                 # 环境变量（API Key 等，需自行创建）
├── requirements.txt     # Python 依赖清单
├── csv_example/         # 本地测试用 CSV 示例文件
├── CSV_DATA_CACHE/      # 运行时缓存（日志 / 元数据 / Parquet 缓存）
└── Png/                 # 图片资源
```

### 模块职责与依赖关系

```
main.py (Streamlit 入口)
 ├──→ config.py        (全局配置，无依赖)
 ├──→ data_loader.py   (CSV → 清洗 → DuckDB)
 │     └──→ config.py
 ├──→ visualizer.py    (Plotly 图表)
 └──→ ai_query.py      (LLM + SQL Agent + 对话)
       └──→ config.py
 └──→ utils.py         (日志/导出/周报)
       └──→ config.py
```

所有模块通过显式 `import` 组合，**无循环依赖**。

---

## ✨ 功能详解

### 一、📂 数据加载模块 (`data_loader.py`)

#### 1.1 双模式数据源

| 模式 | 说明 | 适用场景 |
|------|------|----------|
| **OneDrive 目录加载** | 扫描 OneDrive 路径及月子目录 `01~12`，按文件名日期匹配，支持自定义时间范围 | 日常使用（默认） |
| **浏览器上传** | 通过 Streamlit `file_uploader` 拖拽多个 CSV 文件 | 无 OneDrive 权限 / 临时离线分析 |

两种模式共享同一套清洗、合并、写入流程。

#### 1.2 CSV 文件扫描 (`scan_csv_files`)

- 扫描目录：主 CSV 目录 + 示例目录 + 月子目录 `01` ~ `12`
- 匹配模式：`RechercheExport_*.csv`（保时捷标准导出格式）
- 日期提取：从文件名解析 `YYYY-MM-DD` 作为文件日期
- 支持按 `start_date` ~ `end_date` 时间范围筛选
- 结果按修改时间降序排列

#### 1.3 编码自动检测 (`_detect_skiprows_and_encoding`)

```
检测优先级: utf-8-sig → utf-8 → latin-1 → gbk
```

读取前 20 行逐一尝试解码，找到第一个成功的编码。同时跳过文件头部的元数据行（如 `"VERTRAULICH"` 机密标记），定位真正的列标题行。

#### 1.4 OneDrive 文件锁重试 (`read_csv_with_retry`)

当 OneDrive 正在同步文件时，读取会触发 `PermissionError`：
- 最大重试 **5 次**（可配置 `CSV_RETRY_MAX`）
- 重试间隔递增（基础间隔 × 重试次数）
- 非「锁错误」直接报错，不做无意义重试

#### 1.5 数据清洗引擎 (`clean_dataframe`)

多级智能清洗策略：

| 步骤 | 说明 |
|------|------|
| StringDtype 兼容 | 强制将 pandas `StringDtype` 转为 `object`（DuckDB 不兼容）|
| 数值列智能转换 | 4 级递进策略：直接转换 → 德式逗号→小数点 → 去除非数字字符 → Excel 公式格式 |
| 长文本保护 | 采样检测平均长度 > 50 或分词数 > 2 的列跳过数值化 |
| 时间列识别 | 含 time/date/ts 关键字的列自动转为 datetime（日优先）|

#### 1.6 数据合并与去重 (`merge_dataframes`)

- 使用 `pd.concat` 合并多文件 DataFrame
- 基于 `Number`（工单编号）列去重，保留最新记录
- 列不完全相同时取并集，缺失值填充 NaN

#### 1.7 DuckDB 写入安全保护

写入 DuckDB 前通过 **Python 原生列表重建 DataFrame**，彻底剥离所有 pandas 扩展类型（StringDtype、Int64 等），避免 `Data type 'str' not recognized` 错误。

同时在模块级禁用 `pd.set_option("future.infer_string", False)` 从源头防止 pandas 2.x 自动推断 StringDtype。

#### 1.8 DuckDBWrapper 类

纯 DuckDB 原生封装，**完全绕过 SQLAlchemy**，提供 LangChain Agent 所需接口：

```python
class DuckDBWrapper:
    run(command, fetch)          # 执行 SQL，返回文本结果
    run_no_throw(command)        # 不抛异常的 run
    get_table_info(names)        # 返回 CREATE TABLE DDL + 样本行
    get_usable_table_names()     # 返回可用表名列表
    dialect                      # "duckdb"
    close()                      # 安全关闭连接（含自动重连）
```

特性：
- 连接意外断开时自动重连
- Windows 长路径兼容
- 结果最多显示 100 行（可配置）

---

### 二、📊 可视化仪表盘 (`visualizer.py`)

提供 9 个渲染函数，全部基于 **Plotly** 交互式图表：

| # | 函数 | 图表类型 | 说明 |
|---|------|----------|------|
| 1 | `render_overview_cards` | Metric 卡片 | 总工单数 / 今日新增 / 未解决 / 平均严重等级 |
| 2 | `render_time_trend_chart` | 柱状图 + 折线叠加 | 每日新增工单趋势 + 7 日移动平均线 |
| 3 | `render_status_pie` | 饼图 | 各状态工单占比分布 |
| 4 | `render_functionality_chart` | 水平柱状图 | Top10 问题最多功能模块（Blues 渐变）|
| 5 | `render_person_chart` | 水平柱状图 | Top10 负责人工作量（Greens 渐变）|
| 6 | `render_sw_hw_charts` | 双列柱状图 | 软件/硬件版本分布（Reds/Purples）|
| 7 | `render_rating_chart` | 柱状图 | 严重等级分布（1=最严重，RdYlGn_r）|
| 8 | `render_fault_frequency` | 饼图 | 故障频率分布（One-Off/Repeatedly/Constant）|
| 9 | `render_full_dashboard` | 组合布局 | 按顺序调用上述 1~8，形成完整仪表盘 |

所有图表均包含异常捕获（`try/except`），字段缺失时优雅降级提示而非崩溃。

---

### 三、💬 AI 智能查询模块 (`ai_query.py`)

#### 3.1 LLM 初始化 (`init_llm`)

兼容多种 OpenAI 接口大模型：

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `LLM_API_KEY` | API 密钥 | 必填 |
| `LLM_TYPE` | 模型类型标识 | `deepseek` |
| `LLM_MODEL` | 模型名称 | `deepseek-chat` |
| `DEEPSEEK_BASE_URL` / `OPENAI_BASE_URL` | API 端点 | DeepSeek 官方 |
| `LLM_TEMPERATURE` | 温度参数 | `0`（确定性输出）|
| `LLM_MAX_TOKENS` | 最大 Token 数 | `2000` |

使用 `langchain-openai.ChatOpenAI`，天然兼容 DeepSeek、通义千问、本地 Ollama 等所有 OpenAI API 格式服务。

#### 3.2 NL2SQL 查询 — `_SimpleSQLAgent`

**轻量级自定义实现**，不依赖 LangChain ReAct Agent / SQLAlchemy：

```
用户输入自然语言问题
       ↓
  [Step 1] LLM 生成 DuckDB SQL（带表结构上下文）
       ↓
  [Step 2] 正则提取 ```sql ... ``` 代码块中的 SQL
       ↓
  [Step 3] 通过 DuckDBWrapper.execute() 执行 SQL
       ↓
  返回格式化的查询结果
```

安全约束：
- **只允许 SELECT 查询**（System Prompt 明确禁止 INSERT/UPDATE/DELETE/DROP/ALTER/TRUNCATE）
- 特殊列名（含空格或 `-`）自动加双引号
- 支持 SQL 展示中间步骤（用户可查看生成的原始 SQL）

#### 3.3 AI 自由对话 — `chat_with_llm`

独立于 NL2SQL 的通用聊天模式：

- **多轮对话**：维护完整的消息历史（`st.session_state._chat_messages`）
- **数据上下文注入**：自动构建当前数据的摘要统计（总数量、状态分布、Top 功能模块/负责人、时间范围、可用字段列表），作为 System Prompt 的一部分
- **LangChain 消息类型**：使用 `SystemMessage` / `HumanMessage` / `AIMessage` 构建标准对话链

#### 3.4 示例问题库

预置 7 条示例问题，点击即可填充输入框：

```python
EXAMPLE_QUESTIONS = [
    "统计各状态的工单数量",
    "本月新增了多少个工单？",
    "软件版本 5045 对应的问题有哪些？",
    "严重等级 1 的工单有多少个？",
    "功能模块 Speech - general 有多少个未解决工单？",
    "谁负责的工单最多？",
    "近 7 天每天新增多少工单？",
]
```

---

### 四、🛠️ 工具函数模块 (`utils.py`)

| 函数 | 说明 |
|------|------|
| `log_info(tag, message)` | 带时间戳的双通道日志（文件 + stderr）|
| `cleanup_temp_db(db_path)` | 安全删除临时 DuckDB 文件（进程退出时自动清理）|
| `export_csv_bytes(df)` | 导出 UTF-8-BOM 编码的 CSV 字节流（Excel 友好）|
| `generate_weekly_report(df, llm)` | 生成本周工单报告（统计卡片 + AI 总结）|
| `safe_result(func, *args, default)` | 通用安全调用装饰器（不崩溃）|
| `safe_print_traceback()` | 获取 traceback 字符串（兼容 Streamlit 环境）|
| `render_file_info(source, df)` | 可折叠的数据源信息面板 + 刷新按钮 |

周报功能详解：
- 自动计算本周周一~周日范围
- 优先使用 `"Change-TS problem"` 列匹配时间（比 `"Date"` 更可靠）
- 输出内容：本周总数 / 处理中 / 今日新增 / Top3 问题模块 / Top3 负责人
- 如已配置 LLM，额外生成一段 200 字以内的 AI 分析总结

---

### 五、🖥️ 主界面编排 (`main.py`)

#### 5.1 页面布局

```
┌─────────────────────────────────────────────────────┐
│  🏎️ 保时捷测试工单智能分析系统          [🐟摸鱼]    │  ← 标题栏 + 右侧 Popover
├──────────┬──────────────────────────────────────────┤
│ 侧边栏   │                                          │
│          │  📊 数据概览（4 列指标卡片区）            │
│ 📂 加载  │──────────────────────────────────────────│
│  ├ 日期  │  [📈仪表盘] [💬查询] [📋数据] [📝周报]  │  ← Tab 切换
│  ├ 上传  │                                          │
│          │  （Tab 内容区域）                          │
│ 🔍筛选  │                                          │
│  ├ 日期  │                                          │
│  ├ 状态  │                                          │
│  ├ 模块  │                                          │
│  └ 负责人 │                                          │
└──────────┴──────────────────────────────────────────┘
```

#### 5.2 侧边栏功能

**数据加载区**：
- OneDrive 模式：双日期选择器 + 「加载选中时间段」按钮 + 文件统计
- 上传模式：`file_uploader` 组件，支持多文件拖拽

**数据筛选区**（数据加载后显示）：
- 日期范围筛选
- 工单状态筛选（动态从数据中提取选项）
- 功能模块筛选（优先 `Functionality.1`，回退 `Functionality`）
- 负责人筛选
- 显示筛选后剩余条数

#### 5.3 四大 Tab

| Tab | 内容 | 关键组件 |
|-----|------|----------|
| 📈 **可视化仪表盘** | 8 种 Plotly 交互式图表 | `visualizer.render_full_dashboard()` |
| 💬 **智能查询** | NL2SQL 子 Tab + AI 对话子 Tab | `st.tabs()` 双子标签页 |
| 📋 **原始数据** | 可搜索表格 + CSV 导出按钮 | `st.dataframe()` + `st.download_button()` |
| 📝 **周报** | 一键生成按钮 / 本周报告展示 | `utils.generate_weekly_report()` |

#### 5.4 右侧摸鱼导航

使用 Streamlit 原生 `st.popover("🐟")` 组件实现的浮动面板，点击展开 6 个外部链接：

| 名称 | 链接 |
|------|------|
| 🍔 今天吃什么 | https://yanweb.top/moyu/eat/ |
| 🐱 圈小猫 | https://yanweb.top/moyu/catch-the-cat/ |
| 💣 扫雷 | https://yanweb.top/moyu/saolei/ |
| 🎮 2048 | https://yanweb.top/moyu/2048/ |
| ⚫ 五子棋 | https://www.yanweb.top/moyu/five-in-a-row/ |
| 🔢 数独 | https://www.yanweb.top/moyu/Sudoku/ |

#### 5.5 Session State 管理

| Key | 类型 | 说明 |
|-----|------|------|
| `df` | DataFrame | 当前加载的全量数据 |
| `db` | DuckDBWrapper | DuckDB 数据库连接实例 |
| `llm` | ChatOpenAI | 大语言模型实例 |
| `llm_error` | str | LLM 初始化错误信息 |
| `_csv_paths` | list[str] | 已加载的 CSV 文件路径列表 |
| `_csv_summary` | str | 加载结果摘要文字 |
| `_question` | str | SQL 查询输入框的当前值 |
| `_upload_source` | str | 数据来源标识（`"onedrive"` / `"upload"`）|
| `_chat_messages` | list[dict] | AI 对话历史消息列表 |
| `_right_nav_open` | bool | 右侧导航栏展开状态（预留）|

---

## ⚙️ 技术架构

### 数据流

```
OneDrive CSV 文件  /  浏览器上传
        │
        ▼
  data_loader.py
  ├─ scan_csv_files()     → 文件扫描 + 日期匹配
  ├─ read_csv_with_retry() → 编码检测 + 元数据行跳过 + 锁重试
  ├─ clean_dataframe()     → 数值转换 / 时间识别 / 类型修复
  ├─ merge_dataframes()    → 合并 + 去重
  └─ DuckDB 写入           → _safe_df (Python list 重建) → CREATE TABLE AS
        │
        ▼
  DuckDB 数据库 (.db 临时文件)
        │
   ┌────┴──────────────────────────────┐
   │              │                     │
   ▼              ▼                     ▼
visualizer.py  ai_query.py          utils.py
(Plotly 图表)  (LLM + SQL Agent)    (导出/周报)
```

### 技术栈

| 层级 | 技术 | 版本要求 |
|------|------|----------|
| Web UI | Streamlit | ≥ 1.28.0 |
| 数据处理 | Pandas | ≥ 2.0.0 |
| 数据库 | DuckDB | ≥ 0.9.0 |
| 可视化 | Plotly | ≥ 5.15.0 |
| AI / LLM | LangChain + langchain-openai | ≥ 0.2.0 / ≥ 0.1.0 |
| AI 接口 | OpenAI SDK | ≥ 1.0.0 |
| 环境变量 | python-dotenv | ≥ 1.0.0 |

### 设计原则

1. **模块化** — 每个文件单一职责，main.py 只做编排不含业务逻辑
2. **容错优先** — 所有 IO 操作均有 try/except，单个图表失败不影响整体
3. **Windows 兼容** — OneDrive 路径含空格/特殊字符、DuckDB 文件锁、编码问题均已处理
4. **无 SQLAlchemy** — DuckDBWrapper 直接封装原生 duckdb.connect()，避免 pg_catalog 兼容性问题
5. **渐进式加载** — 进度条实时反馈（扫描→读取→合并→写入）

---

## 🚀 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

推荐使用清华镜像加速：
```bash
pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
```

### 2. 配置环境变量

在项目根目录创建 `.env` 文件：

```ini
# 必填：大模型 API Key
LLM_API_KEY=sk-your-api-key-here

# 可选配置（有默认值）
LLM_TYPE=deepseek
LLM_MODEL=deepseek-chat
DEEPSEEK_BASE_URL=https://api.deepseek.com/v1
LLM_TEMPERATURE=0
LLM_MAX_TOKENS=2000
```

支持的 LLM 服务：

| 服务商 | LLM_TYPE | base_url 示例 |
|--------|----------|---------------|
| DeepSeek | `deepseek` | `https://api.deepseek.com/v1` |
| OpenAI | `openai` | `https://api.openai.com/v1` |
| 通义千问 | `qwen` | `https://dashscope.aliyuncs.com/compatible-mode/v1` |
| Ollama (本地) | `ollama` | `http://localhost:11434/v1` |

### 3. 启动系统

```bash
streamlit run main.py
```

浏览器会自动打开 `http://localhost:8501`

---

## ❓ 常见问题

**Q: 运行报 `No module named 'xxx'`？**
```bash
pip install -r requirements.txt
```

**Q: OneDrive 文件被锁定导致加载失败？**
系统会自动重试 5 次。如果仍然失败，请等待 OneDrive 同步完成后再刷新页面。

**Q: NL2SQL 查询无响应或报错？**
1. 检查 `.env` 中 `LLM_API_KEY` 是否正确
2. 确认网络可以访问对应的 LLM API 端点
3. 在终端查看详细错误日志

**Q: 报 `Data type 'str' not recognized` 错误？**
这是 pandas 2.x 的 StringDtype 与 DuckDB 不兼容导致的。v4.0 已通过以下三层防护解决：
1. 模块级 `pd.set_option("future.infer_string", False)`
2. `clean_dataframe` 中强制 StringDtype → object
3. DuckDB 写入前从 Python list 重建 DataFrame

如仍遇到此错误，请确认 `data_loader.py` 为最新版本后重启服务。

**Q: 如何切换为本地测试数据？**
将 CSV 文件放入 `csv_example/` 目录即可，系统会自动将其作为备用数据源扫描。

**Q: 页面显示空白？**
检查浏览器控制台（F12）是否有 JavaScript 错误。通常重启 Streamlit 服务即可解决。

---

## 📄 许可与说明

- 本系统仅供 **保时捷内部使用**，数据来源于保时捷测试管理系统
- 请勿将包含实际工单数据的截图/导出外传
- AI 查询功能需要联网调用 LLM API（产生相应费用）

---

*最后更新：2026 年 5 月 | 版本 v4.0*
