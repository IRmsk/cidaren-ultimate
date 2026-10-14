# cidaren-ultimate

**词达人自动刷题整合版**：融合 5 个开源仓库的精华，一个项目跑通班级任务 + 自学任务。

```
├── main.py              # 入口：Web 控制台 / CLI
├── requirements.txt     # 仅 flask + requests + qrcode，无重型依赖
├── .env.example         # 配置模板
├── cidaren/
│   ├── config.py        # 配置管理（.env + 环境变量）
│   ├── crypto.py        # 接口加解密：sign 签名 + jv 解密（三张表合并）
│   ├── api.py           # API 客户端：班级 + 自学双套 + 查词/转原型接口
│   ├── bank.py          # 三库合一答案库：题库缓存 + 短语库 + 单词库（自学习）
│   ├── ai.py            # LLM 客户端（OpenAI 兼容，按题型定制 prompt）
│   ├── login.py         # 微信扫码登录（免 Fiddler 抓包）
│   ├── solver.py        # 全题型答题引擎（19 种题型）
│   ├── runner.py        # 任务执行器（循环刷满分、卡住检测、重置重刷）
│   ├── web.py           # Web 控制台（配置/任务/日志/扫码/答案库统计）
│   └── data/            # 本地答案库（自动累积，越用越准）
└── tests/               # 核心逻辑单测
```

## 快速开始

```bash
pip install -r requirements.txt
python main.py            # 打开 http://localhost:5000
```

在配置面板二选一：
1. **微信扫码登录**（一键，无需抓包）
2. 手动填 `USERTOKEN`（Fiddler 抓包；`ABC`/`AUTH_V` 可选）

然后配置 LLM（可选，OpenAI 兼容：DeepSeek / Kimi / 豆包 / 本地 Ollama 都行），保存后即可在任务面板启动 / 循环。

## 各仓库精华与整合对照

| 仓库 | 贡献 | 整合位置 |
|---|---|---|
| [LangQi99/cidaren](https://github.com/LangQi99/cidaren) | Flask Web 控制台、题库指纹缓存、LLM 兜底 prompt、班级+自学双套 API、签到 | `web.py` / `bank.py` / `ai.py` / `api.py` / `runner.py` |
| [ularch/Easy_Cidaren](https://github.com/ularch/Easy_Cidaren) | 全题型规则匹配（11~73 共 19 种）、答案库自学习、jv 解密表、词表短语反查、mode 73 五级候选池、mode 32 模板空位对齐 | `solver.py` / `bank.py` / `crypto.py` |
| [xm2284/Cidaren-AI](https://github.com/xm2284/Cidaren-AI) | AI 优先答题、空白试探纠错、任务单词列表喂 AI、循环重刷闭环、分数卡住检测、8 个 bug 修复（已完成判定/过期过滤/网络重试/20001 选词后继续等） | `solver.py` / `runner.py` |
| [Rain-shadow/cdr](https://github.com/Rain-shadow/cdr) | 微信扫码登录、题型判定文档（mode 11-54 策略）、查词比对判定思路 | `login.py` / `solver.py` |
| [github123666/cidaren](https://github.com/github123666/cidaren) | 任务过期过滤、token 校验流程、自建任务处理 | `runner.py` / `login.py` |

## 答题策略（答案来源优先级）

```
题库指纹缓存 -> 短语/单词答案库 -> 规则匹配(查词比对) -> [AI 优先] -> 空白试探(服务端纠错) -> [LLM 兜底] -> 跳过
判错后自动把 answer_corrects 学习进本地答案库，越用越准
```

- `AI_PRIORITY=1`：先问大模型，答不上再走规则/空白试探（快，正确率高）
- `AI_PRIORITY=0`：先规则匹配，大模型仅兜底（省 API 额度）

## CLI 用法

```bash
python main.py check                            # 校验 token
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
| `LLM_URL` / `LLM_KEY` / `LLM_MODEL` / `LLM_MAX_TOKENS` | OpenAI 兼容 LLM 配置 |
| `AI_PRIORITY` | 1 = AI 优先，0 = 规则优先 |
| `DELAY_PER_QUESTION` | 答题间隔秒数（≥2 防风控） |
| `MAX_ROUNDS` | 未满分最大重刷轮数 |

## 免责声明

本项目仅供学习交流，请勿用于商业用途。词达人接口随时可能调整，脚本可能在任何时间失效。使用本脚本造成的一切后果由使用者自行承担；如学校将词达人任务计入平时成绩，请自行权衡学术诚信风险。
