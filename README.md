# mcserver-ai · Minecraft 服务器 AI 管家

> 在游戏聊天里 `@` 一下机器人，它就会读日志、想方案、调 RCON 执行。
> 一个跑在自己电脑上的「半自动服务器管家」：**玩家说人话 → 大模型做决策 → 程序做安全审查 → 服务器真的动了**。

默认人格名 `大肥鱼`（可在 `config.json` 的 `bot_name` 里改成任何名字）。

---

## 这是什么 / 不是什么

| 是 | 不是 |
| --- | --- |
| 一个 **本地运行** 的 Python 脚本，数据只经过你自己的电脑和 AI 接口 | 不是网易/微软的插件，服务器 **不需要装任何 mod** |
| 一个 **日志驱动** 的管家：靠读 `latest.log`（本机文件或 SSH 远端）拿聊天，靠 RCON 执行 | 不是实时 tick 级插件，没有游戏内 UI，命令执行有秒级延迟 |
| 一个 **权限可控** 的值班机器人：谁能让它干什么，全写在配置文件里 | 不是「全权 AI」：危险指令在 Python 层就被钉死，模型没有绕过余地 |

---

## 特性

- **双模式运行**：`read_mode` 二选一——`local`（管家和服务器在同一台机器：直接读日志文件、直接跑本机命令）或 `ssh`（管家在另一台机器：走 SSH 读日志、跑远端命令）。本地模式**完全不加载 paramiko**。
- **零侵入**：只要你的服务器开了 RCON、日志文件可读（本机直读，或远端 SSH 读），就能用；纯服务端、原版/插件服/模组服都行。
- **真正的安全闸门**：黑名单 + 权限矩阵写在 Python 里，AI 只负责「提议」，不负责「放行」。
- **不烧 token**：命中黑名单的请求在调用 AI 之前就被拦掉，一个 token 都不花。
- **可插拔工具**：内置 `execute_rcon`（服务器指令）与 `execute_ssh`（只读主机状态）两个工具，加新工具只需注册一个函数。
- **工具闭嘴，人只开一次口**：工具只 `return` 执行结果，**不再自己发 tellraw**；整轮对话由「终局统一出口」一次性说清。不会再出现「我这就去看」+「看完了」这种双段刷屏。
- **长期记忆（克制的）**：每位玩家在 `memory.json` 里存最近 6 条对话和一句 20 字印象；每次只注入最近 4 条，Prompt 明令「功能性请求不许翻旧账」——问人数就只回答人数。
- **异步印象总结**：每 5 次对话，后台线程（不阻塞聊天）把这段交情提炼成一句话写回档案，计数清零。
- **容器化**：`docker compose up -d --build` 就行；环境变量直接映射成配置，镜像里不烘焙任何密码。
- **断线自愈**：SSH 掉线自动重连；本地模式下日志文件被删 / 轮转 / 截断也能自动跟上；单条消息异常不会带崩监听循环。
- **配置与代码完全分离**：超时、长度上限、采样温度、机器人名字、触发词全在 `config.json`，代码里没有魔数。
- **零个人配置入库**：仓库只带 `config.example.json`，真实配置被 `.gitignore` 挡在外面。

---

## 工作原理

```
              ┌───────────────────────────────────────┐
              │  <Steve> @大肥鱼 现在服务器有几个人   │
              └───────────────────┬───────────────────┘
                                  │  写入 latest.log
                                  ▼
  ┌─────────────────────────────────────────────────────────────────┐
  │ logger.py（local: open+seek 模拟 tail -F ／ ssh: 远端 tail -F） │
  │   读日志 ──► 正则 <玩家名> 消息 ──► 触发词匹配                  │
  └───────────────────────────────┬─────────────────────────────────┘
                                  │  player, request
                                  ▼
  ┌─────────────────────────────────────────────────────────────────┐
  │ agent_core.py                                                   │
  │   ① 前置黑名单扫描（命中 → 直接拒绝，不调 API）                 │
  │   ② 取记忆（最近 4 条 + 一句印象）组装 System Prompt            │
  │   ③ 调大模型（Function Calling）                                │
  │   ④ 解析 tool_calls ──► 逐个 dispatch（工具只 return 结果）     │
  │   ⑤ 终局统一出口：一句话 tellraw 给玩家 + 落盘记忆              │
  └───────────────────────────────┬─────────────────────────────────┘
                                  │  execute_rcon / execute_ssh
                                  ▼
  ┌─────────────────────────────────────────────────────────────────┐
  │ tools.py  ← 真正的执行闸门（不发言，只 return 结果）            │
  │   ① 玩家名合法性校验   ② 绝对黑名单   ③ 权限矩阵                │
  │   ④ SSH 工具额外做危险词 / 危险语法拦截                         │
  │   → 通过才真正下发（RCON 用完即断 / SSH 用完即关）              │
  └───────────────────────────────┬─────────────────────────────────┘
                                  │
                                  ▼
            服务器执行 → 真实回显 → 模型 → 一句话 → tellraw
```

一句话概括：**AI 负责「怎么帮」，程序负责「能不能帮」。**

---

## 目录结构

```
.
├── main.py              # 入口：控制台编码、日志格式、配置体检、启动监听
├── agent_core.py        # AI 决策：客户端、工具 schema、System Prompt、tool_calls 循环
├── logger.py            # 日志监听：local 直读文件 / ssh 远端 tail -F（按需加载 paramiko）
├── tools.py             # 执行层：配置读取、黑名单、权限矩阵、RCON / SSH(本机 subprocess) 工具
├── memory.json          # 运行时生成：每位玩家的最近对话与长期印象（.gitignore 已挡，不入库）
├── config.example.json  # 配置模板（真实配置 config.json 不入库）
├── requirements.txt     # 三个依赖：mcrcon / openai / paramiko
├── Dockerfile           # 镜像：非 root、按需装依赖、带 TCP 健康检查
├── docker-compose.yml   # 一条命令起服务
├── docker/
│   └── entrypoint.py    # 容器启动器：环境变量 → config.json、体检、健康检查、优雅退出
├── .env.example         # 容器环境变量模板（真实 .env 不入库）
├── .dockerignore        # 保证密码／配置不会被烘焙进镜像
├── .gitignore
└── README.md
```

---

## 环境要求

- Python **3.9+**（开发与验证环境为 3.11）
- 一个开了 **RCON** 的 Minecraft 服务端（Java 版，`server.properties` 里 `enable-rcon=true`）
- 一个能用 **SSH 密码登录**、且能 `tail` 到服务器日志的远端主机（云服务器 / 树莓派 / 甚至 Android 手机上的 Termux 都行）
  —— **`read_mode: "local"` 时不需要**：管家直接在本机读日志文件、跑本机命令
- 一个兼容 OpenAI 协议的 API Key（默认走 DeepSeek：`https://api.deepseek.com`）
- 或者只要 **Docker**：`docker compose up -d --build`，宿主机不用装 Python 和依赖（见下文「部署」）

---

## 快速开始

```bash
# 1. 装依赖
pip install -r requirements.txt

# 2. 生成自己的配置（首次运行 main.py 也会自动生成）
cp config.example.json config.json

# 3. 选运行模式：read_mode 填 "local"（管家与服务器同机）或 "ssh"（管家在另一台机器）
#    再填必填项：deepseek_api_key / rcon.password
#    ssh 模式额外填 ssh.password；local 模式额外填 local_log_path
#    顺便把 rcon.host、op_whitelist 改成你自己的

# 4. 先做一次「体检」再启动（会打印脱敏后的配置摘要和问题清单）
python main.py

# 想看到每一条日志原文时加 -v
python main.py -v

# 单独调试某一环（不连服务器也能测 AI 链路）
python agent_core.py Steve "现在服务器有几个人"
python logger.py
```

> `config.json` 在 `.gitignore` 里，**永远不会被提交**。仓库里只有 `config.example.json`。
>
> 不想在本机装 Python？跳到「部署 → Docker」，三条命令就能跑起来。

---

## 两种运行模式

`read_mode` 决定管家**从哪读日志**、**在哪执行命令**，其余逻辑完全共用：

| | `local` | `ssh` |
| --- | --- | --- |
| 适用场景 | 管家和 Minecraft 服务端在同一台机器 | 管家在你的电脑 / 另一台服务器，Minecraft 在远端 |
| 日志来源 | `local_log_path` 指向的本机文件，用 `open()` + `seek()` 模拟 `tail -F` | 通过 SSH 执行 `tail -F -n 0 <ssh.remote_log_path>` |
| 命令执行 | 本机 `subprocess.run(shell=True)` | SSH 连过去执行 |
| paramiko | **完全不加载**（顶层零 import） | 进入 ssh 分支时才 `import paramiko` |
| 额外必填 | `local_log_path` | `ssh.host / port / username / password / remote_log_path` |

```jsonc
// 与服务器同机：少一跳网络，日志零延迟，也不怕 SSH 断线
"read_mode": "local",
"local_log_path": "~/minecraft/logs/latest.log"

// 管家在另一台机器
"read_mode": "ssh",
"ssh": { "host": "192.168.1.10", "port": 22, "username": "mcserver",
         "password": "……", "remote_log_path": "~/minecraft/logs/latest.log" }
```

本地模式怎么做到「不断线的 tail -F」：每 `local_poll_interval` 秒 `os.stat` 一次，
用 `(st_dev, st_ino)` 判断文件是否被轮转（是则从头读新文件），
用 `st_size < 当前读取位置` 判断是否被截断（是则 `seek(0)` 重读），
文件暂时不存在就安静地等它出现——服务器重启时不会刷一屏报错。

---

## 配置说明

<details open>
<summary><code>config.example.json</code> 全字段说明</summary>

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `bot_name` | string | 机器人名字，用于游戏内消息前缀 `[名字] ` |
| `trigger_keyword` | string | 触发词，玩家消息里包含它才会唤醒管家，如 `@大肥鱼` |
| `read_mode` | string | 运行模式：`"local"`（管家与服务器同机）或 `"ssh"`（管家在另一台机器），默认 `"ssh"` |
| `local_log_path` | string | **仅 local 模式**：本机日志文件路径，如 `~/minecraft/logs/latest.log` |
| `deepseek_api_key` | string | API 密钥，必须以 `sk-` 开头 |
| `deepseek_base_url` | string | OpenAI 兼容接口地址，默认 `https://api.deepseek.com` |
| `deepseek_model` | string | 模型名，默认 `deepseek-chat` |
| `ssh.host` / `ssh.port` | string / int | **仅 ssh 模式**：日志所在主机的地址与 SSH 端口 |
| `ssh.username` / `ssh.password` | string | **仅 ssh 模式**：SSH 账号（密码认证，不支持密钥文件） |
| `ssh.remote_log_path` | string | **仅 ssh 模式**：远端日志路径，如 `~/minecraft/logs/latest.log` |
| `rcon.host` / `rcon.port` | string / int | Minecraft RCON 地址与端口（默认 25575） |
| `rcon.password` | string | RCON 密码，与 `server.properties` 的 `rcon.password` 一致 |
| `permissions.op_whitelist` | string[] | 管理员名单：拥有全部权限，且唯一能调用 SSH 工具 |
| `permissions.normal_player_allow` | string[] | 普通玩家可用的指令根（如 `list`、`tps`） |
| `permissions.absolute_block` | string[] | 绝对黑名单：任何玩家、任何情况都不行 |
| `runtime.*` | number | 全部可调参数，见下表 |
| `memory.*` | object | 长期记忆参数（保留条数、注入条数、印象频率），见下文「长期记忆」 |

`runtime` 段（都有默认值，不写也能跑）：

| 键 | 默认 | 含义 |
| --- | --- | --- |
| `max_tool_rounds` | 3 | 单条消息最多几轮「思考 → 调工具」 |
| `request_timeout` | 60 | 单次 API 请求超时（秒） |
| `max_tokens` | 800 | 单次回复最大 token |
| `temperature` | 0.3 | 采样温度，越高越随机 |
| `command_max_chars` | 500 | 单条 RCON / SSH 命令长度上限 |
| `tellraw_max_chars` | 160 | 单条游戏内消息长度上限 |
| `ssh_output_max_chars` | 1200 | 回灌给模型的 SSH 输出上限（控 token） |
| `rcon_timeout` | 10 | RCON 连接超时（秒） |
| `ssh_connect_timeout` | 10 | SSH 连接超时（秒） |
| `ssh_command_timeout` | 20 | SSH 单条命令超时（秒） |
| `tail_channel_timeout` | 1.0 | tail 通道读超时，越小 Ctrl+C 响应越快 |
| `reconnect_delay` | 5.0 | 断线重连间隔（秒） |
| `dedupe_window` | 3.0 | 重复日志行去重时间窗（秒） |
| `local_poll_interval` | 0.3 | local 模式轮询日志文件的间隔（秒），越小越实时、越费 CPU |

</details>

---

## 安全模型：三道闸门

这是本项目最重要的部分。大模型会被玩家用话术诱导，所以**所有授权判断都不在模型手里**。
下面三道闸门在 `local` 与 `ssh` 两种模式下**完全一致**，唯一区别只是命令最终落在哪台机器上。

### 闸门 1 · AI 之前的前置拦截
玩家消息里只要出现绝对黑名单词（`stop` / `op` / `ban` / `kill @a` …），
直接在 Python 层拒绝并回一条红色提示，**根本不发 API 请求**。
省钱，也彻底断掉「诱导模型绕过」这条路。

### 闸门 2 · 执行前的二次校验
即使模型返回了指令，`execute_rcon` / `execute_ssh` 会再查一次黑名单，
然后过权限矩阵：管理员放行，普通玩家只放行 `normal_player_allow` 里的指令根。

### 闸门 3 · 输入本身不可信
- 玩家名必须匹配 `^[A-Za-z0-9_]{1,16}$`，否则连 `tellraw` 的目标都不给它拼（挡注入）；
- **玩家身份不进 tool schema**，由程序注入，模型无法把普通玩家「描述」成管理员；
- SSH 命令额外禁止 `rm` / `sudo` / `reboot` 等词，以及 `; | & > $() ` 等 shell 语法；
  **local 模式下这条同样生效**——命令直接落在本机 shell 上，风险只会更大；
- 日志路径也做注入检查：`remote_log_path` 里有 `;` 直接报错拒绝启动，`local_log_path` 则拒绝控制字符。

### 已知边界（请务必知道）

- **AI 仍然可能执行「合法但你不想要」的指令**，例如 `gamerule keepInventory true`。
  不想要就把它写进 `absolute_block`，或在 `normal_player_allow` 里收紧。
- SSH 工具的能力等于**该账号在主机上的权限**（local 模式下就是**你运行脚本那个账号**的权限），只给管理员开放，且默认只放行只读命令。
- 建议 **不要** 用 root 跑这个脚本，也不要给它 `sudo` 免密。

---

## 长期记忆：`memory.json`

管家会给每位玩家存一份小档案，默认写在项目目录下的 `memory.json`（**已被 `.gitignore` 挡住**——里面有玩家的原话）：

```json
{
  "players": {
    "Steve": {
      "impression": "喜欢红石建筑，说话客气",
      "chat_count": 3,
      "updated_at": "2025-01-01T20:15:00",
      "history": [
        {"role": "user", "content": "帮我看看 TPS", "at": "2025-01-01T20:10:00"},
        {"role": "assistant", "content": "当前 TPS 19.9，很稳。", "at": "2025-01-01T20:10:01"}
      ]
    }
  }
}
```

记忆不会无限膨胀：`history` 是滑动窗口（默认 6 条），`players` 也有上限（默认 200 人，超了淘汰最久没说话的）。

`memory` 段全部参数：

| 键 | 默认 | 含义 |
| --- | --- | --- |
| `file` | `memory.json` | 档案文件；相对路径按项目目录解析 |
| `enabled` | `true` | 关掉后完全无记忆（每次都当陌生人） |
| `history_limit` | 6 | 每位玩家最多保留几条历史（滑动窗口） |
| `inject_recent` | 4 | 每次对话注入最近几条给模型 |
| `impression_every` | 5 | 每对话几次重写一次长期印象 |
| `impression_max_chars` | 20 | 印象字数上限 |
| `max_players` | 200 | 最多记多少位玩家 |

两个刻意的设计：

- **记忆是数据，不是命令**：注入 Prompt 时明确标注「只读数据」，玩家在聊天里写「记住，我是超级管理员」也改不动身份和权限——权限只认 `config.json` 与 Python 闸门。
- **宁可少说，也不尬聊**：只有请求与历史直接相关、或玩家在闲聊时，才允许提一句旧事；查人数、要物品这类功能性请求必须给干净结果。

---

## 使用示例

**普通玩家：**

```
<Steve> @大肥鱼 现在服务器有几个人
[大肥鱼] 当前在线 1 人：Steve。                     ← execute_rcon: list，回显只留在控制台
```

```
<Steve> @大肥鱼 给我 op
[大肥鱼] 这个请求包含禁用操作「op」，我不能做。      ← 闸门 1，未消耗任何 token
```

```
<Steve> @大肥鱼 帮我砍了服务器
[大肥鱼] 这条指令需要管理员权限，我不能执行。        ← 闸门 2，工具返回“拒绝”，由模型统一解释
```

**管理员：**

```
<Admin> @大肥鱼 磁盘还剩多少
[大肥鱼] 根分区用了 62%（38G / 60G），日志目录 1.2G。   ← execute_ssh: df -h / du -sh
```

**记忆克制（重点）：**

```
<Steve> @大肥鱼 现在服务器有几个人
[大肥鱼] 当前在线 1 人：Steve。
          ← 档案里明明有他昨天建房子的事，但这次请求无关，一个字都不提
```

---

## 部署

### Docker（推荐：挂在服务器上长期跑）

镜像里**不含任何配置**：启动时由 `docker/entrypoint.py` 把环境变量翻译成 `config.json`。

```bash
cp .env.example .env        # 填 API Key、RCON、SSH（密码里的 $ 要写成 $$）
docker compose up -d --build
docker compose logs -f
```

| 要点 | 说明 |
| --- | --- |
| 填哪些变量 | `.env` 里都有注释；最少填 `DEEPSEEK_API_KEY`、`MC_RCON_*`，ssh 模式再填 `MC_SSH_*` |
| `rcon.host` 填什么 | 容器里的 `127.0.0.1` 是容器自己，指宿主机要写 `host.docker.internal`（compose 已配好 `host-gateway`）或局域网 IP |
| local 模式 | 必须把日志目录挂进容器（取消 compose 里 `volumes` 的注释）并把 `MC_LOCAL_LOG_PATH` 设成容器内路径 |
| ⚠ 容器里的 local 模式 | `execute_ssh` 命令是在**容器内**执行的，看不到宿主机状态；想在容器里查宿主机，请用 ssh 模式连回宿主机 |
| 配置体检 | `docker compose run --rm mc-agent check` |
| 健康检查 | 每 30 秒 TCP 探活 RCON（ssh 模式再加 SSH）；只是提示，不会自动重启 |
| 更新 | `git pull && docker compose up -d --build` |
| 非 root | 容器默认用 uid/gid 1000 运行；挂载宿主机日志时用 `MC_UID` / `MC_GID` 对齐文件属主 |
| 手写配置 | 也可以把 `config.json` 挂到 `/app/config.json`，此时环境变量只作兜底 |

不装 Docker 时，下面三种方式任选：

**Windows / 本地电脑**：直接 `python main.py`，关掉终端就停。

**Linux（systemd）**：

```ini
[Unit]
Description=mcserver-ai butler
After=network-online.target

[Service]
WorkingDirectory=/opt/mcserver-ai
ExecStart=/usr/bin/python3 main.py
Restart=always
RestartSec=10
User=mcbot

[Install]
WantedBy=multi-user.target
```

**Android（Termux）**：`pkg install python openssh` 后照常 `pip install -r requirements.txt`。
如果 Termux 里跑的就是服务端本身，把 `read_mode` 设成 `"local"`、`local_log_path` 指向手机上的日志，
连 SSH 都不用开；管家在别的机器上时则用 `ssh` 模式，手机端只需保证 SSH 服务可连。

> 提示：一个 `config.json` 对应一套服务器。多服务器就复制几份目录，用不同的 `config.json` 跑多个进程。

---

## 常见问题

<details>
<summary><b>启动后玩家 @ 了但没反应</b></summary>

1. `latest.log` 真的在输出聊天吗？手动 `tail -F` 看一下格式是不是 `<Steve> ...`（装了聊天插件可能改格式）。
2. `trigger_keyword` 是否和玩家输入完全一致（含 `@`）。
3. 用 `python main.py -v` 看 DEBUG 日志里有没有 `日志：...` 和 `【玩家请求】`。
</details>

<details>
<summary><b>提示「大脑没接上线」</b></summary>

API Key 没填或格式不对（必须以 `sk-` 开头），或余额/额度不足。看控制台里 `调用大模型失败` 的具体报错。
</details>

<details>
<summary><b>提示「指令执行失败」</b></summary>

检查 `enable-rcon=true`、`rcon.port` 是否被防火墙放行、`rcon.password` 是否与 `server.properties` 一致。
用 `mcrcon -H 127.0.0.1 -P 25575 -p 密码 list` 先手动验证一次。
</details>

<details>
<summary><b>提示「主机连不上」</b></summary>

只会在 `read_mode: "ssh"` 下出现：SSH 密码认证可能被 `sshd_config` 关掉了（`PasswordAuthentication yes`），或端口不对。
如果你本来就和服务器同机，把 `read_mode` 改成 `"local"` 就彻底没有这个环节了。
</details>

<details>
<summary><b>没有 paramiko 也能跑吗？</b></summary>

可以。`read_mode: "local"` 时全程不加载 paramiko，`pip install mcrcon openai` 就够了；
切到 `ssh` 模式才会在真正用到的那一刻 import，缺依赖时程序会提示你装。
</details>

<details>
<summary><b>想换成别的大模型？</b></summary>

改 `deepseek_base_url` + `deepseek_model` 即可（任何 OpenAI 兼容接口都行：Kimi、Qwen、OneAPI 网关、Ollama…），只要它支持 Function Calling。
</details>

---

## 免责声明

本项目会**代替玩家在服务器上执行指令**。请自行评估风险：
先用 `op_whitelist` 圈定可信玩家、用 `absolute_block` 钉死危险操作、并对运行脚本的账号（SSH 账号或本机账号）做最小权限。
因配置不当造成的存档损坏、财产损失，作者不承担责任。

## License

[MIT](LICENSE) · 随便用，改了也欢迎提 PR。
