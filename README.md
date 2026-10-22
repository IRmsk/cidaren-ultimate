# cidaren-ultimate

**词汇学习自动化框架（v2.7.0）**：融合 5 个开源项目的工程实践，多源答案融合 + 自学习题库 + Mock Server E2E 测试。纯 Python，Web 控制台 + CLI 双入口，Termux 可直接运行。

> 本项目为个人学习与工程研究用途，探索多源信息融合、自学习缓存、无凭证集成测试等技术模式。

```
├── main.py              # 入口：Web 控制台 / CLI
├── requirements.txt     # flask + requests + qrcode，无重型依赖
├── .env.example         # 配置模板（不含任何真实密钥）
├── core/                # 核心功能
│   ├── config.py        # 配置管理（.env + 环境变量）
│   ├── crypto.py        # 接口加解密：sign 签名 + jv 解密
│   ├── api.py          # API 客户端
│   ├── bank.py         # 三库合一答案库：题库缓存 + 短语库 + 单词库（自学习，跨进程文件锁）
│   ├── ai.py           # LLM 客户端（OpenAI 兼容，按题型定制 prompt）
│   ├── login.py        # 微信扫码登录
│   └── ocr.py          # 图片题干/选项识别（LLM / tesseract / 百度 OCR）
├── solver/              # 答题逻辑
│   ├── solver.py       # 全题型答题引擎（19 种题型，多源融合）
│   ├── runner.py       # 任务执行器（多轮重试、卡住检测、断点恢复、单题超时保护）
│   └── report.py       # 答题报告：每题判分与来源归因统计
├── web/                 # Web 控制台
│   └── app.py          # Flask 应用（配置/任务/日志/扫码/报告）
├── data/                # 内置词表 + 本地答案库（自动累积）
├── mock_server.py      # 本地模拟服务端（无账号也能跑 E2E 测试）
└── tests/               # 28 项测试（核心逻辑 + mock E2E）
```

## 快速开始

```bash
pip install -r requirements.txt
python main.py            # 打开 http://localhost:5000
```

在配置面板二选一登录：
1. **微信扫码登录**（一键，无需抓包）
2. 手动填 `USERTOKEN`

然后可选配置 LLM（OpenAI 兼容：DeepSeek / Kimi / 豆包 / 本地 Ollama），保存后即可在任务面板启动。

## 核心架构

### 多源答案融合链

```
题库指纹缓存 → 语义兜底 → 短语/单词答案库 → 离线释义词典 → 规则匹配 → [AI 优先] → AI 二次兜底 → 空白试探 → [LLM 兜底] → 跳过
```

- **题目指纹**：`mode::题型::题干::备注::选项前20字`，做缓存 key
- **语义兜底**：题干重刷时选项顺序变化导致指纹失配，按「题型 + 中文短语」语义匹配
- **不盲猜**：所有来源都答不上时不瞎猜，走空白试探让服务端返回标准答案；拿不到就跳过
- **自学习**：判错后自动把服务端返回的正确答案写进本地题库，越用越准
- **AI 二次兜底**：AI 首次超时/限速失败、或规则判错且无纠错时，带选项词池重试

### 任务执行器（健壮性设计）

- **单题超时保护**：`ThreadPoolExecutor` + `future.result(timeout=)`，超时强制跳过，永不卡死
- **多轮重试**：未达标则重置任务重新执行，最多 `MAX_ROUNDS` 轮
- **卡住检测**：连续 2 轮无进展则放弃，避免无效循环
- **断点恢复**：任务状态持久化，中断后可从断点继续

### 自学习答案库

- 四层存储：题目指纹缓存 → 短语库 → 单词库 → 离线词典（ECDICT 14771 词，MIT）
- **跨平台文件锁**：Unix `fcntl.flock` / Windows `msvcrt.locking`，同一 contextmanager 封装
- **原子写**：pid 临时文件 + `fsync` + `os.replace`，多进程安全

### Mock Server E2E 测试

最有价值的工程实践：用 Flask 实现完整的模拟服务端，固定题库覆盖所有题型，判分逻辑与真实服务端一致。测试时把 API 指向 mock，**无需真实账号即可跑完整流程**。

```bash
python -m pytest tests/ -v
```

测试覆盖：完整流程可达标、报告生成、答案库自学习生效。

## 各上游项目贡献

| 仓库 | 贡献 |
|---|---|
| LangQi99/cidaren | Flask Web 控制台、题库指纹缓存、LLM 兜底 prompt |
| ularch/Easy_Cidaren | 全题型规则匹配、答案库自学习、jv 解密表 |
| xm2284/Cidaren-AI | AI 优先答题、空白试探纠错、循环重试闭环 |
| Rain-shadow/cdr | 微信扫码登录、题型策略文档 |
| github123666/cidaren | 任务过滤、token 校验 |

## 配置项

| 变量 | 说明 | 默认 |
|---|---|---|
| `AI_PRIORITY` | 1=先AI后规则，0=先规则后AI | 1 |
| `MAX_ROUNDS` | 最大重试轮数 | 10 |
| `QUESTION_TIMEOUT` | 单题超时（秒） | 60 |
| `DELAY_PER_QUESTION` | 每题间隔（秒） | 2 |
| `LLM_URL` / `LLM_KEY` / `LLM_MODEL` | OpenAI 兼容 LLM 配置 | 空 |

## CLI 用法

```bash
python main.py check                            # 校验 token
python main.py resume                           # 断点恢复：完成所有未完成任务
python main.py class <task_id> <release_id>     # 单个班级任务
python main.py study <task_id> <list_id> [course_id] [task_type] [grade]
```

## Termux / 手机运行

纯 Python + POSIX 兼容，Termux 直接跑：

```bash
pkg install python
pip install flask requests qrcode
python main.py
```

## 版本历史

| 版本 | 内容 |
|---|---|
| v1.1 | 基础整合：5 仓库精华合体，Web 控制台 + CLI |
| v1.2 | 微信扫码登录，免抓包 |
| v2.0 | OCR 读图 / 断点恢复 / 测试报告 / mock server E2E / LLM 429 限速暂停 |
| v2.1 | Web 全面注释 + 移动端适配 |
| v2.2 | 离线释义词典（ECDICT 14771 词） |
| v2.2.1 | 字母数识别：无 w_lens 时从题干下划线推断每空长度 |
| v2.3 | 「测试 AI 连接」按钮 + max_tokens 类型修复 |
| v2.4 | 日志面板美化：状态徽章 + 来源徽章 |
| v2.5 | 轮报汇总 + 卡住/放弃原因明细 |
| v2.6 | 题库语义兜底：按中文短语匹配，重刷命中率提升 |
| v2.6.1 | AI 二次兜底 + 报告排除学习卡片噪音 |
| v2.6.2 | 重构目录结构：去掉嵌套 cidaren/ 文件夹，core/solver/web 直接放根目录 |
| v2.7.0 | 跳过已截止任务（修复 Cidaren-AI bug#3） |

## 免责声明

本项目仅供学习交流与技术研究，请勿用于商业用途。上游接口随时可能调整，脚本可能在任何时间失效。使用本项目造成的一切后果由使用者自行承担。
