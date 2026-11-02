# mcserver-ai · Minecraft 服务器 AI 管家

> 在游戏聊天里 `@` 一下机器人，它就会读日志、想方案、调 RCON 执行。
> 一个跑在自己电脑上的「半自动服务器管家」：**玩家说人话 → 大模型做决策 → 程序做安全审查 → 服务器真的动了**。

默认人格名 `大肥鱼`（可在 `config.json` 的 `bot_name` 里改成任何名字）。

---

## 这是什么 / 不是什么

| 是 | 不是 |
| --- | --- |
| 一个 **本地运行** 的 Python 脚本，数据只经过你自己的电脑和 AI 接口 | 不是网易/微软的插件，服务器 **不需要装任何 mod** |
| 一个 **日志驱动** 的管家：靠 `tail -F latest.log` 读聊天，靠 RCON 执行 | 不是实时 tick 级插件，没有游戏内 UI，命令执行有秒级延迟 |
| 一个 **权限可控** 的值班机器人：谁能让它干什么，全写在配置文件里 | 不是「全权 AI」：危险指令在 Python 层就被钉死，模型没有绕过余地 |

---

## 特性

- **零侵入**：只要你的服务器开了 RCON、且日志能通过 SSH 读到，就能用；纯服务端、原版/插件服/模组服都行。
- **真正的安全闸门**：黑名单 + 权限矩阵写在 Python 里，AI 只负责「提议」，不负责「放行」。
- **不烧 token**：命中黑名单的请求在调用 AI 之前就被拦掉，一个 token 都不花。
- **可插拔工具**：内置 `execute_rcon`（服务器指令）与 `execute_ssh`（只读主机状态）两个工具，加新工具只需注册一个函数。
- **断线自愈**：SSH 掉线自动重连，单条消息异常不会带崩监听循环，日志轮转的重复行自动去重。
- **配置与代码完全分离**：超时、长度上限、采样温度、机器人名字、触发词全在 `config.json`，代码里没有魔数。
- **零个人配置入库**：仓库只带 `config.example.json`，真实配置被 `.gitignore` 挡在外面。

---

## 工作原理

```
                        ┌──────────────────────────────┐
   玩家在游戏里打字       │  <Steve> @大肥鱼 现在服务器有几个人  │
                        └──────────────┬───────────────┘
                                       │  写入 latest.log
                                       ▼
  ┌────────────────────────────────────────────────────────────┐
  │ logger.py                                                  │
  │   SSH + tail -F  ──►  正则 <玩家名> 消息  ──►  触发词匹配     │
  └───────────────────────────────┬────────────────────────────┘
                                  │  player, request
                                  ▼
  ┌────────────────────────────────────────────────────────────┐
  │ agent_core.py                                              │
  │   ① 前置黑名单扫描（命中 → 直接拒绝，不调 API）               │
  │   ② 组装 System Prompt（把 config.permissions 翻译成自然语言）│
  │   ③ 调大模型（Function Calling）                            │
  │   ④ 解析 tool_calls ──► 逐个 dispatch                       │
  └───────────────────────────────┬────────────────────────────┘
                                  │  execute_rcon / execute_ssh
                                  ▼
  ┌────────────────────────────────────────────────────────────┐
  │ tools.py  ← 真正的执行闸门                                  │
  │   ① 玩家名合法性校验   ② 绝对黑名单   ③ 权限矩阵              │
  │   ④ SSH 工具额外做危险词 / 危险语法拦截                       │
  │   → 通过才真正下发（RCON 用完即断 / SSH 用完即关）             │
  └───────────────────────────────┬────────────────────────────┘
                                  │
                                  ▼
                    服务器执行 → tellraw 把结果发回玩家聊天栏
```

一句话概括：**AI 负责「怎么帮」，程序负责「能不能帮」。**

---

## 目录结构

```
.
├── main.py              # 入口：控制台编码、日志格式、配置体检、启动监听
├── agent_core.py        # AI 决策：客户端、工具 schema、System Prompt、tool_calls 循环
├── logger.py            # SSH 监听：tail -F 日志、正则匹配、触发词识别、断线重连
├── tools.py             # 执行层：配置读取、黑名单、权限矩阵、RCON / SSH 工具
├── config.example.json  # 配置模板（真实配置 config.json 不入库）
├── requirements.txt     # 三个依赖：mcrcon / openai / paramiko
├── .gitignore
└── README.md
```

---

## 环境要求

- Python **3.9+**（开发与验证环境为 3.11）
- 一个开了 **RCON** 的 Minecraft 服务端（Java 版，`server.properties` 里 `enable-rcon=true`）
- 一个能用 **SSH 密码登录**、且能 `tail` 到服务器日志的远端主机（云服务器 / 树莓派 / 甚至 Android 手机上的 Termux 都行）
- 一个兼容 OpenAI 协议的 API Key（默认走 DeepSeek：`https://api.deepseek.com`）

---

## 快速开始

```bash
# 1. 装依赖
pip install -r requirements.txt

# 2. 生成自己的配置（首次运行 main.py 也会自动生成）
cp config.example.json config.json

# 3. 填三处必填项：deepseek_api_key / ssh.password / rcon.password
#    顺便把 ssh.host、rcon.host、op_whitelist 改成你自己的

# 4. 先做一次「体检」再启动（会打印脱敏后的配置摘要和问题清单）
python main.py

# 想看到每一条日志原文时加 -v
python main.py -v

# 单独调试某一环（不连服务器也能测 AI 链路）
python agent_core.py Steve "现在服务器有几个人"
python logger.py
```

> `config.json` 在 `.gitignore` 里，**永远不会被提交**。仓库里只有 `config.example.json`。

---

## 配置说明

<details open>
<summary><code>config.example.json</code> 全字段说明</summary>

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `bot_name` | string | 机器人名字，用于游戏内消息前缀 `[名字] ` |
| `trigger_keyword` | string | 触发词，玩家消息里包含它才会唤醒管家，如 `@大肥鱼` |
| `deepseek_api_key` | string | API 密钥，必须以 `sk-` 开头 |
| `deepseek_base_url` | string | OpenAI 兼容接口地址，默认 `https://api.deepseek.com` |
| `deepseek_model` | string | 模型名，默认 `deepseek-chat` |
| `ssh.host` / `ssh.port` | string / int | 日志所在主机的地址与 SSH 端口 |
| `ssh.username` / `ssh.password` | string | SSH 账号（密码认证，不支持密钥文件） |
| `ssh.remote_log_path` | string | 远端日志路径，如 `~/minecraft/logs/latest.log` |
| `rcon.host` / `rcon.port` | string / int | Minecraft RCON 地址与端口（默认 25575） |
| `rcon.password` | string | RCON 密码，与 `server.properties` 的 `rcon.password` 一致 |
| `permissions.op_whitelist` | string[] | 管理员名单：拥有全部权限，且唯一能调用 SSH 工具 |
| `permissions.normal_player_allow` | string[] | 普通玩家可用的指令根（如 `list`、`tps`） |
| `permissions.absolute_block` | string[] | 绝对黑名单：任何玩家、任何情况都不行 |
| `runtime.*` | number | 全部可调参数，见下表 |

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

</details>

---

## 安全模型：三道闸门

这是本项目最重要的部分。大模型会被玩家用话术诱导，所以**所有授权判断都不在模型手里**。

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
- 日志路径也做注入检查，`remote_log_path` 里有 `;` 直接报错拒绝启动。

### 已知边界（请务必知道）

- **AI 仍然可能执行「合法但你不想要」的指令**，例如 `gamerule keepInventory true`。
  不想要就把它写进 `absolute_block`，或在 `normal_player_allow` 里收紧。
- SSH 工具的能力等于**该账号在主机上的权限**，只给管理员开放，且默认只放行只读命令。
- 建议 **不要** 用 root 跑这个脚本，也不要给它 `sudo` 免密。

---

## 使用示例

**普通玩家：**

```
<Steve> @大肥鱼 现在服务器有几个人
[大肥鱼] 我去看看哦
[大肥鱼] 现在已经执行好啦。          ← execute_rcon: list
```

```
<Steve> @大肥鱼 给我 op
[大肥鱼] 这个请求包含禁用操作「op」，我不会做。     ← 闸门 1，未消耗任何 token
```

```
<Steve> @大肥鱼 帮我砍了服务器
[大肥鱼] 你的权限不足，这条指令我不能执行。          ← 闸门 2
```

**管理员：**

```
<Admin> @大肥鱼 磁盘还剩多少
[大肥鱼] 我这就去看
[大肥鱼] 我查到了磁盘占用情况。                     ← execute_ssh: df -h
```

---

## 部署

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

**Android（Termux）**：`pkg install python openssh` 后照常 `pip install -r requirements.txt`；
手机端只需保证 SSH 服务可连，脚本本身建议跑在更稳的机器上。

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

SSH 密码认证可能被 `sshd_config` 关掉了（`PasswordAuthentication yes`），或端口不对。
</details>

<details>
<summary><b>想换成别的大模型？</b></summary>

改 `deepseek_base_url` + `deepseek_model` 即可（任何 OpenAI 兼容接口都行：Kimi、Qwen、OneAPI 网关、Ollama…），只要它支持 Function Calling。
</details>

---

## 免责声明

本项目会**代替玩家在服务器上执行指令**。请自行评估风险：
先用 `op_whitelist` 圈定可信玩家、用 `absolute_block` 钉死危险操作、并对 SSH 账号做最小权限。
因配置不当造成的存档损坏、财产损失，作者不承担责任。

## License

[MIT](LICENSE) · 随便用，改了也欢迎提 PR。
