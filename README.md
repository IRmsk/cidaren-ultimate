# cidaren-ultimate

**词达人自动刷题整合版（v2.6.1）**：融合 5 个开源仓库的精华，一个项目跑通班级任务 + 自学任务。纯 Python，Web 控制台 + CLI 双入口，手机 Termux 可直接运行。

```
├── main.py              # 入口：Web 控制台 / CLI（check / resume / class / study）
├── requirements.txt     # 仅 flask + requests + qrcode，无重型依赖
├── .env.example         # 配置模板（不含任何真实密钥）
├── cidaren/
│   ├── config.py        # 配置管理（.env + 环境变量）
│   ├── crypto.py        # 接口加解密：sign 签名 + jv 解密（三张表合并）
│   ├── api.py           # API 客户端：班级 + 自学双套 + 查词/转原型接口
│   ├── bank.py          # 三库合一答案库：题库缓存 + 短语库 + 单词库（自学习）
│   ├── ai.py            # LLM 客户端（OpenAI 兼容，按题型定制 prompt）
│   ├── login.py         # 微信扫码登录（免 Fiddler 抓包）
│   ├── solver.py        # 全题型答题引擎（19 种题型）
│   ├── runner.py        # 任务执行器（循环刷满分、卡住检测、重置重刷）
│   ├── ocr.py           # 图片题干/选项识别（LLM / tesseract / 百度 OCR）
│   ├── report.py        # 测试报告：每题判分与来源统计
│   ├── mock_server.py   # 本地模拟服务端（无账号也能跑 E2E 测试）
│   ├── web.py           # Web 控制台（配置/任务/日志/扫码/报告）
│   └── data/            # 内置词表 + 本地答案库（自动累积，越用越准）
└── tests/               # 28 项单测（核心逻辑 + mock E2E）
```

## 快速开始

```bash
pip install -r requirements.txt
python main.py            # 打开 http://localhost:5000
```

在配置面板二选一登录：
1. **微信扫码登录**（一键，无需抓包）
2. 手动填 `USERTOKEN`（Fiddler 抓包；`ABC`/`AUTH_V` 可选）

然后可选配置 LLM（OpenAI 兼容：DeepSeek / Kimi / 豆包 / 本地 Ollama），保存后即可在任务面板「启动 / 循环」。

## 各仓库精华与整合对照

| 仓库 | 贡献 | 整合位置 |
|---|---|---|
| [LangQi99/cidaren](https://github.com/LangQi99/cidaren) | Flask Web 控制台、题库指纹缓存、LLM 兜底 prompt、班级+自学双套 API、签到 | `web.py` / `bank.py` / `ai.py` / `api.py` / `runner.py` |
| [ularch/Easy_Cidaren](https://github.com/ularch/Easy_Cidaren) | 全题型规则匹配（11~73 共 19 种）、答案库自学习、jv 解密表、词表短语反查、mode 73 五级候选池、mode 32 模板空位对齐 | `solver.py` / `bank.py` / `crypto.py` |
| [xm2284/Cidaren-AI](https://github.com/xm2284/Cidaren-AI) | AI 优先答题、空白试探纠错、任务单词列表喂 AI、循环重刷闭环、分数卡住检测、8 个 bug 修复 | `solver.py` / `runner.py` |
| [Rain-shadow/cdr](https://github.com/Rain-shadow/cdr) | 微信扫码登录、题型判定文档（mode 11-54 策略）、查词比对判定思路 | `login.py` / `solver.py` |
| [github123666/cidaren](https://github.com/github123666/cidaren) | 任务过期过滤、token 校验流程、自建任务处理 | `runner.py` / `login.py` |

## 答题策略（答案来源优先级）

```
题库指纹缓存 -> 语义兜底(mode+中文短语) -> 短语/单词答案库 -> 离线释义词典 -> 规则匹配(查词比对) -> [AI 优先] -> AI 二次兜底 -> 空白试探(服务端纠错) -> [LLM 兜底] -> 跳过
```

- **内置词表**：随项目附带题目指纹 1918 条、短语答案库 212 条、单词答案库 371 条、全局词表 388 条、离线释义词典 14771 词（ECDICT 开源词典，MIT 协议，`cidaren/data/`）
- **题库语义兜底（v2.6）**：32/73 型题的题干下划线占位、选项顺序重刷时可能变化导致指纹失配——按「题型 + 中文短语」语义匹配兜底，跨题型不误命中
- **不盲猜**：题库/AI/规则都答不上时不做瞎猜，直接空白试探让服务端返回标准答案；拿不到就跳过，宁缺毋滥
- **AI 答对自动上传题库**：没见过的题 AI 答对后，答案自动写入本地题库（题目指纹 + 短语/单词库），越用越准
- **AI 二次兜底（v2.6.1）**：AI 首次超时/限速失败、或规则判错且无纠错时，AI 带选项词池再组一次答案，减少丢分
- 判错后自动把 `answer_corrects` 学习进本地答案库
- `AI_PRIORITY=1`：先问大模型，答不上再走规则/空白试探（快，正确率高）
- `AI_PRIORITY=0`：先规则匹配，大模型仅兜底（省 API 额度）

## 版本历史

| 版本 | 内容 |
|---|---|
| v1.1 | 基础整合：5 仓库精华合体，Web 控制台 + CLI |
| v1.2 | 微信扫码登录，免抓包 |
| v2.0 | OCR 读图 / 断点恢复 / 测试报告 / mock server E2E / LLM 429 限速暂停 / 防检测时长上报 |
| v2.1 | Web 全面注释 + 移动端适配 |
| v2.2 | 离线释义词典（ECDICT 14771 词，英译汉题型兜底） |
| v2.2.1 | 字母数识别：无 w_lens 时从题干下划线推断每空长度 |
| v2.3 | 「测试 AI 连接」按钮 + max_tokens 类型 bug 修复 |
| v2.4 | 日志面板美化：状态徽章（对/学/答/跳）、来源徽章（AI/规则/纠错） |
| v2.5 | 轮报汇总（答对/首错/跳过 + 来源分布）、SKIP 明细显示具体题目、卡住/放弃原因 |
| v2.5.1 | 首错统计口径修正（FIX 先错后纠不再显示为"答错 0"） |
| v2.6 | 题库语义兜底：按中文短语匹配，重刷命中率大幅提升 |
| v2.6.1 | AI 二次兜底（rule 判错后带词池重试）+ 报告排除学习卡片噪音 |

## Web 控制台功能

- **任务面板**：班级/自学任务一键启动、循环、停止；进度 + 分数实时刷新
- **日志面板**：结构化徽章渲染——`对`(绿)/`学`(橙，纠错入库)/`答`(琥珀，答题中)/`跳`(红，跳过)；来源 `AI/规则/纠错/题库`；轮报显示每轮答对/首错/跳过与来源分布
- **测试报告**：每题判分与来源统计（cache/rule/ai/blank/skip + 明细），学习卡片不计入正确率
- **配置面板**：扫码登录、LLM/OCR 配置、「测试 AI 连接」按钮
- **移动端**：手机浏览器直接可用（900px 断点：配置单列、表格转卡片、日志全屏）

## CLI 用法

```bash
python main.py check                            # 校验 token
python main.py resume                           # 断点恢复：刷完所有未满分班级任务
python main.py class <task_id> <release_id>     # 刷单个班级任务
python main.py study <task_id> <list_id> [course_id] [task_type] [grade]
```

## Termux / 手机运行

纯 Python + POSIX 兼容（文件锁走 fcntl），可在 Termux 直接跑：

```bash
pkg install python
pip install flask requests qrcode
python main.py
```

- 手机浏览器访问 `http://127.0.0.1:5000` 即可打开控制台（Termux 与 Android 共享网络栈）
- 想让电脑/局域网访问：`CIDAREN_HOST=0.0.0.0 python main.py`（需手机与电脑同一 Wi-Fi，注意安全）
- 端口自定义：`CIDAREN_PORT=8080 python main.py`

## 配置项

| 变量 | 说明 |
|---|---|
| `USERTOKEN` | 词达人请求头 usertoken（必填） |
| `ABC` / `AUTH_V` | 请求头字段（可选） |
| `COURSE_ID` / `STUDY_GRADE` | 自学任务课程与难度（1 快速 / 2 普通 / 3 完整 / 4 超级困难） |
| `LLM_URL` / `LLM_KEY` / `LLM_MODEL` / `LLM_MAX_TOKENS` | OpenAI 兼容 LLM 配置（留空则不调用 AI） |
| `AI_PRIORITY` | 1 = AI 优先，0 = 规则优先 |
| `DELAY_PER_QUESTION` | 答题间隔秒数（≥2 防风控） |
| `MAX_ROUNDS` | 未满分最大重刷轮数 |
| `QUESTION_TIMEOUT` | 单题超时秒数，超时强制跳过（防卡死） |
| `USE_WORD_API` | 1 = 查词接口辅助；0 = 禁用（弱网提速） |
| `OCR_MODE` | off / llm / tesseract / baidu（图片题读图） |
| `BAIDU_OCR_AK` / `BAIDU_OCR_SK` | 百度 OCR 凭据（OCR_MODE=baidu 时填） |

## 测试

```bash
python -m unittest tests.test_core tests.test_e2e_mock   # 28 项，含 mock 服务端 E2E
```

## 免责声明

本项目仅供学习交流，请勿用于商业用途。词达人接口随时可能调整，脚本可能在任何时间失效。使用本脚本造成的一切后果由使用者自行承担；如学校将词达人任务计入平时成绩，请自行权衡学术诚信风险。
