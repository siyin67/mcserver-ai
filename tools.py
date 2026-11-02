# -*- coding: utf-8 -*-
"""
tools.py —— 工具执行器与硬编码安全层
================================================================
职责：
1. 作为全工程唯一的配置入口，统一从 config.json（缺失时自动从 config.example.json
   生成一份）读取参数；
2. 对外提供两个工具函数 execute_rcon() / execute_ssh()，供 agent_core 动态调用；
3. 在执行前做“绝对黑名单硬拦截 + 玩家权限校验”（纵深防御，不依赖 AI 的自觉）；
4. 提供 send_tellraw() / say()，把结果用游戏内聊天消息的方式反馈给玩家；
5. 支持双运行模式（config.json 的 read_mode）：
   - "local"：execute_ssh() 直接用 subprocess 在本机跑命令，全程不碰网络、不加载 paramiko；
   - "ssh"  ：才按需 import paramiko 建立连接，在远端主机上执行。

安全设计原则（非常重要）：
- AI 只是“提议者”，真正的执行闸门在 Python 这一层；
- 任何命令在发往服务器之前，都必须先过 find_blocked_keyword() 与 check_permission()；
- 玩家身份由程序注入，绝不接受 AI 自行指定，防止 AI 被玩家话术诱导而“冒充管理员”提权；
- 黑名单匹配做了去空白处理，防止用 “k i l l @ a” 这类拆字绕过。

配置约定（本项目不含任何个人化硬编码）：
- 本文件里没有服务器地址、密码、玩家名、机器人名；
- 所有可调参数都在 config.json：连接信息在 ssh / rcon 段，
  超时与长度上限在 runtime 段，机器人名字在 bot_name，权限在 permissions 段；
- 某个 runtime 项没写时回退到下面的 RUNTIME_DEFAULTS 默认值。
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ！！刻意不在顶层 import paramiko ！！
# 本地模式（read_mode = "local"）下整个进程都不应该把它拉进内存：
# 只有在真的要建立 SSH 连接时，才由 load_paramiko() 按需导入。

# mcrcon 两种模式都要用（RCON 始终走网络），正常导入即可
try:
    from mcrcon import MCRcon
except ImportError:  # pragma: no cover
    MCRcon = None  # type: ignore[assignment]

log = logging.getLogger("tools")

# ---------------------------------------------------------------- 路径与常量
# 配置文件默认与代码同目录；容器里可以用 MC_CONFIG_PATH 指到挂载点（如 /data/config.json）
CONFIG_PATH = Path(
    os.environ.get("MC_CONFIG_PATH") or Path(__file__).resolve().with_name("config.json")
).expanduser()
EXAMPLE_CONFIG_PATH = Path(__file__).resolve().with_name("config.example.json")

PLAYER_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_]{1,16}$")   # Java 版玩家名规则
ANSI_PATTERN = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")          # 控制台颜色控制符
WHITESPACE_PATTERN = re.compile(r"\s+")

DEFAULT_BOT_NAME = "MC管家"     # 仅当 config.json 里 bot_name / trigger_keyword 都缺失时使用

# 运行模式：决定日志怎么读、系统命令在哪里执行
READ_MODE_SSH = "ssh"       # 连远端主机：paramiko + tail -F / exec_command
READ_MODE_LOCAL = "local"   # 就在本机：open() 读文件 / subprocess 跑命令，完全不加载 paramiko
READ_MODES = (READ_MODE_SSH, READ_MODE_LOCAL)
_READ_MODE_ALIASES = {      # 容忍几种常见写法
    "ssh": READ_MODE_SSH, "remote": READ_MODE_SSH,
    "local": READ_MODE_LOCAL, "本地": READ_MODE_LOCAL, "本机": READ_MODE_LOCAL,
}

# runtime 段的默认值：config.json 里缺哪一项就用哪一项
RUNTIME_DEFAULTS: Dict[str, Any] = {
    "max_tool_rounds": 3,          # 单条消息最多几轮“思考 → 调工具”
    "request_timeout": 60,         # 单次 API 请求超时（秒）
    "max_tokens": 800,             # 单次回复最大 token
    "temperature": 0.3,            # 采样温度，管家场景要稳
    "command_max_chars": 500,      # 单条 RCON / SSH 命令最大长度
    "tellraw_max_chars": 160,      # 单条游戏内消息最大长度
    "ssh_output_max_chars": 1200,  # 回传给 AI 的 SSH 输出最大长度（控 token）
    "rcon_timeout": 10,            # RCON 连接超时（秒）
    "ssh_connect_timeout": 10,     # SSH 连接超时（秒）
    "ssh_command_timeout": 20,     # SSH 单条命令超时（秒）
    "tail_channel_timeout": 1.0,   # tail 通道读超时，决定 Ctrl+C 响应速度
    "reconnect_delay": 5.0,        # 断线重连间隔（秒）
    "dedupe_window": 3.0,          # 重复日志行去重时间窗（秒）
    "local_poll_interval": 0.3,    # 本地模式轮询日志文件的间隔（秒）
}

# 配置缓存（避免每收到一条聊天就重复读盘）
_config_lock = threading.Lock()
_config_cache: Dict[str, Any] = {}
_config_loaded = False

# 黑名单正则缓存：keyword -> 编译好的正则
_regex_cache: Dict[str, re.Pattern] = {}


# ================================================================ 一、配置读取
def load_config(path: Optional[str] = None, force: bool = False) -> Dict[str, Any]:
    """
    读取并缓存 config.json；文件不存在时自动从 config.example.json 复制一份。

    :param path:  可选的自定义配置文件路径（默认取本文件同目录下的 config.json）
    :param force: 为 True 时强制重新读盘（改了配置想热加载时用）
    :raises RuntimeError: 文件不存在 / 不是合法 JSON / 顶层不是对象
    """
    global _config_loaded

    with _config_lock:
        if _config_loaded and not force and path is None:
            return _config_cache

        target = Path(path).expanduser() if path else CONFIG_PATH

        # 首次运行：把示例配置复制成真实配置，让程序能直接跑起来
        if not target.exists() and target == CONFIG_PATH and EXAMPLE_CONFIG_PATH.exists():
            try:
                shutil.copyfile(EXAMPLE_CONFIG_PATH, target)
                log.warning(
                    "未找到 config.json，已从 config.example.json 复制一份 → %s\n"
                    "        请填写其中的 API Key、SSH 与 RCON 信息后再使用。", target,
                )
            except OSError as exc:
                raise RuntimeError(
                    f"config.json 不存在，且自动生成失败：{exc}\n"
                    f"        请手动把 config.example.json 复制为 config.json 并填写。"
                ) from None

        try:
            with target.open("r", encoding="utf-8") as fp:
                data = json.load(fp)
        except FileNotFoundError:
            raise RuntimeError(
                f"找不到配置文件：{target}\n"
                f"        请把 config.example.json 复制为 config.json 并填写。"
            ) from None
        except json.JSONDecodeError as exc:
            # 配置文件被打错一个逗号是很常见的坑，这里报出精确行列，方便你自己修
            raise RuntimeError(
                f"config.json 不是合法 JSON（第 {exc.lineno} 行第 {exc.colno} 列）：{exc.msg}"
            ) from None
        except OSError as exc:
            raise RuntimeError(f"读取配置文件失败：{exc}") from None

        if not isinstance(data, dict):
            raise RuntimeError("config.json 的顶层必须是 JSON 对象 {...}")

        _config_cache.clear()
        _config_cache.update(data)
        _config_loaded = True
        return _config_cache


def get_runtime(cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    取运行参数（超时、长度上限等），与 RUNTIME_DEFAULTS 合并；
    类型不符时打一条警告并回退默认值，绝不让坏配置把程序搞崩。
    """
    cfg = cfg if cfg is not None else load_config()
    runtime = cfg.get("runtime") or {}
    merged: Dict[str, Any] = dict(RUNTIME_DEFAULTS)

    if isinstance(runtime, dict):
        for key, default in RUNTIME_DEFAULTS.items():
            if key not in runtime:
                continue
            value = runtime[key]
            try:
                merged[key] = type(default)(value)
            except (TypeError, ValueError):
                log.warning("runtime.%s 的值 %r 非法，已回退默认值 %r", key, value, default)
    elif runtime:
        log.warning("config.json 的 runtime 段必须是对象，已全部使用默认值")

    return merged


def get_read_mode(cfg: Optional[Dict[str, Any]] = None) -> str:
    """
    读取 read_mode：决定日志监听与系统命令执行的形态。

    - "ssh"  ：连远端主机（paramiko + tail -F / exec_command）
    - "local"：就在本机（open() 读日志 / subprocess 跑命令）

    缺省或写错时一律回退 "ssh"，保证不填 read_mode 的旧配置行为完全不变。
    """
    cfg = cfg if cfg is not None else load_config()
    raw = cfg.get("read_mode")
    text = str(raw).strip().lower() if raw is not None else ""
    if not text:
        return READ_MODE_SSH

    mode = _READ_MODE_ALIASES.get(text, text)
    if mode not in READ_MODES:
        log.warning("read_mode 只能是 \"ssh\" 或 \"local\"，收到 %r，已按 ssh 处理", raw)
        return READ_MODE_SSH
    return mode


def get_log_path(cfg: Optional[Dict[str, Any]] = None) -> str:
    """
    取要监听的日志路径（两种模式共用同一套解析规则）：

    - local 模式：优先 local_log_path，退而用 ssh.remote_log_path（兼容只配了一处的人）；
    - ssh 模式  ：用 ssh.remote_log_path（远端主机上的路径）。
    """
    cfg = cfg if cfg is not None else load_config()
    ssh_cfg = cfg.get("ssh") or {}

    if get_read_mode(cfg) == READ_MODE_LOCAL:
        path = cfg.get("local_log_path") or ssh_cfg.get("remote_log_path")
    else:
        path = ssh_cfg.get("remote_log_path")
    return str(path or "").strip()


def load_paramiko() -> Any:
    """
    按需导入 paramiko —— 这是“极致按需加载”的核心：

    - 本地模式的整条链路（logger 读文件、execute_ssh 跑 subprocess）都不会调用它，
      所以 `import paramiko` 永远不会执行，进程里也不会出现这个模块；
    - 只有 ssh 模式真的要建连接时才走到这里，缺依赖时给出可操作的提示。
    """
    try:
        import paramiko  # noqa: PLC0415  —— 函数内导入是刻意为之
    except ImportError:  # pragma: no cover
        raise RuntimeError(
            "当前是 ssh 模式，但没有安装 paramiko。请先执行：pip install -r requirements.txt"
        ) from None
    return paramiko


def get_bot_name(cfg: Optional[Dict[str, Any]] = None) -> str:
    """机器人的名字：优先 bot_name，其次从触发词里去掉 @ 推断。"""
    cfg = cfg if cfg is not None else load_config()
    name = str(cfg.get("bot_name") or "").strip()
    if name:
        return name
    trigger = str(cfg.get("trigger_keyword") or "").strip()
    if trigger:
        return trigger.lstrip("@").strip() or DEFAULT_BOT_NAME
    return DEFAULT_BOT_NAME


def get_trigger(cfg: Optional[Dict[str, Any]] = None) -> str:
    """触发词：优先 trigger_keyword，缺省时用 “@机器人名”。"""
    cfg = cfg if cfg is not None else load_config()
    trigger = str(cfg.get("trigger_keyword") or "").strip()
    return trigger or f"@{get_bot_name(cfg)}"


def get_bot_prefix(cfg: Optional[Dict[str, Any]] = None) -> str:
    """消息前缀，形如 “[名字] ”，随 bot_name 变化，不再硬编码在文案里。"""
    return f"[{get_bot_name(cfg)}] "


def get_permissions(cfg: Optional[Dict[str, Any]] = None) -> Dict[str, List[str]]:
    """
    把 config.json 里的 permissions 取出来并规范化成三个字符串列表。
    缺字段 / 写成字符串等异常情况都能兜住，绝不让 KeyError 冒到主循环里。
    """
    cfg = cfg if cfg is not None else load_config()
    perms = cfg.get("permissions") or {}

    def _as_list(key: str) -> List[str]:
        value = perms.get(key) or []
        if isinstance(value, str):          # 容忍写成单个字符串
            value = [value]
        if not isinstance(value, (list, tuple)):
            return []
        return [str(item).strip() for item in value if str(item).strip()]

    return {
        "op_whitelist": _as_list("op_whitelist"),
        "normal_player_allow": [item.lower() for item in _as_list("normal_player_allow")],
        "absolute_block": _as_list("absolute_block"),
    }


def is_op_player(player: str, cfg: Optional[Dict[str, Any]] = None) -> bool:
    """判断玩家是否在 op_whitelist 里（管理权限唯一来源）。"""
    return str(player or "").strip() in get_permissions(cfg)["op_whitelist"]


# ================================================================ 二、安全拦截
def _keyword_regex(keyword: str) -> re.Pattern:
    """
    为黑名单词生成正则：
    - 词内空格允许任意空白（"kill @a" 也能匹配 "kill@a"）；
    - 前后做单词边界，避免 "stop" 误伤 "stopwatch"、"op" 误伤 "stop"。
    """
    rx = _regex_cache.get(keyword)
    if rx is None:
        tokens = [re.escape(token) for token in keyword.split()]
        body = r"\s*".join(tokens) if tokens else re.escape(keyword)
        rx = re.compile(r"(?<![A-Za-z0-9_])" + body + r"(?![A-Za-z0-9_])", re.IGNORECASE)
        _regex_cache[keyword] = rx
    return rx


# 配置黑名单之外的“兜底”规则：即使忘了写进 config.json，这类操作也一律禁止
_EXTRA_BLOCK_PATTERNS: List[re.Pattern] = [
    # 任何选择器形式的批量抹杀：kill @p / kill @s / kill @r / kill @e[type=...]
    re.compile(r"(?<![A-Za-z0-9_])kill\s*@[a-z]", re.IGNORECASE),
]


def find_blocked_keyword(text: Any) -> Optional[str]:
    """
    检查文本是否命中绝对黑名单。

    :return: 命中的关键字（便于日志与提示），未命中返回 None
    """
    if text is None:
        return None
    raw = str(text)
    if not raw.strip():
        return None

    perms = get_permissions()
    # 去掉所有空白再查一遍，防止 “k i l l @ a” 这种拆字绕过
    flat = WHITESPACE_PATTERN.sub("", raw)

    for keyword in perms["absolute_block"]:
        rx = _keyword_regex(keyword)
        if rx.search(raw) or rx.search(flat):
            return keyword

    for rx in _EXTRA_BLOCK_PATTERNS:
        matched = rx.search(raw) or rx.search(flat)
        if matched:
            return matched.group(0)

    return None


def _normalize_command(command: Any) -> str:
    """去掉命令开头的斜杠和多余空白，统一成 RCON 可直接下发的形式。"""
    cmd = str(command or "").strip()
    while cmd.startswith("/"):
        cmd = cmd[1:].lstrip()
    return cmd


def _base_command(command: str) -> str:
    """取命令的第一个词（子命令根），并去掉 minecraft: 命名空间前缀。"""
    parts = (command or "").strip().split()
    if not parts:
        return ""
    return parts[0].lower().split(":")[-1]


def check_permission(player: str, command: str, cfg: Optional[Dict[str, Any]] = None) -> Tuple[bool, str]:
    """
    权限矩阵硬校验（AI 说允许不算，这里才算）：

    - 玩家在 op_whitelist 里   → 允许（前提是没命中黑名单）；
    - 其他人                  → 只允许 normal_player_allow 里的指令根。

    :return: (是否允许, 原因说明)
    """
    perms = get_permissions(cfg)
    player = str(player or "").strip()

    if player in perms["op_whitelist"]:
        return True, f"玩家 {player} 在 op_whitelist 中，拥有管理权限"

    base = _base_command(command)
    if base and base in perms["normal_player_allow"]:
        return True, f"指令「{base}」属于所有玩家可用范围（normal_player_allow）"

    return False, (
        f"玩家 {player} 不在 op_whitelist 中，"
        f"且指令根「{base or '空'}」不在 normal_player_allow 允许范围内"
    )


# ================================================================ 三、基础工具
def _one_line(text: Any, limit: int) -> str:
    """把任意文本压成单行并截断，避免把换行 / 超长内容塞进 tellraw。"""
    flat = WHITESPACE_PATTERN.sub(" ", str(text or "")).strip()
    return flat[:limit]


def _safe_player(player: Any) -> str:
    """校验玩家名（同时是 tellraw 目标），非法则返回空串，从根源上杜绝指令注入。"""
    name = str(player or "").strip()
    return name if PLAYER_NAME_PATTERN.match(name) else ""


def _rcon_exec(command: str, cfg: Optional[Dict[str, Any]] = None) -> Tuple[bool, str]:
    """
    执行一条 RCON 指令：**每次用完立即断开**，不常驻连接、不占资源。

    :return: (是否成功, 回显内容或错误信息)
    """
    if MCRcon is None:
        return False, "未安装 mcrcon，请先执行：pip install -r requirements.txt"

    cfg = cfg if cfg is not None else load_config()
    rcon_cfg = cfg.get("rcon") or {}
    host = str(rcon_cfg.get("host") or "").strip()
    password = str(rcon_cfg.get("password") or "")
    try:
        port = int(rcon_cfg.get("port") or 25575)
    except (TypeError, ValueError):
        port = 25575
    timeout = int(get_runtime(cfg)["rcon_timeout"])

    if not host or not password:
        return False, "config.json 里的 rcon.host / rcon.password 没有配置"

    try:
        # 兼容不同版本的 mcrcon（老版本没有 timeout 参数）
        try:
            client = MCRcon(host, password, port=port, timeout=timeout)
        except TypeError:
            client = MCRcon(host, password, port=port)
        with client:                      # with 语句：退出时自动断开
            output = client.command(command) or ""
        return True, output.strip()
    except Exception as exc:             # 网络错误、密码错误、被防火墙拦……都在这里兜住
        log.error("RCON 执行失败（%s）：%s: %s", command[:80], type(exc).__name__, exc)
        return False, f"{type(exc).__name__}: {exc}"


def send_tellraw(player: str, text: str, color: str = "white") -> bool:
    """
    通过 RCON 给玩家发一条游戏内聊天提示（tellraw）。

    :param player: 玩家名；传 "@a" 表示全服广播
    :param color:  Minecraft 颜色名：white / green / red / yellow / gold / aqua ...
    """
    target = "@a" if str(player).strip() == "@a" else _safe_player(player)
    if not target:
        log.error("tellraw 目标玩家名非法，消息已丢弃：%r", player)
        return False

    limit = int(get_runtime()["tellraw_max_chars"])
    # 手写 JSON 很容易被引号 / 反斜杠坑死，统一交给 json.dumps 转义
    payload = json.dumps(
        {"text": _one_line(text, limit), "color": color},
        ensure_ascii=False,
    )
    ok, _ = _rcon_exec(f"tellraw {target} {payload}")
    return ok


def say(player: str, text: str, color: str = "white") -> bool:
    """
    带机器人署名地给玩家发消息：自动加上 “[机器人名] ”前缀。

    前缀来自 config.json 的 bot_name，所以代码里不再出现硬编码的机器人名。
    """
    prefix = get_bot_prefix()
    body = str(text or "").strip()
    head = prefix.strip()
    if body.startswith(head):                 # 文案里已经带了前缀就不重复加
        body = body[len(head):].strip()
    return send_tellraw(player, prefix + body, color)


# ================================================================ 四、工具一：RCON
def execute_rcon(command: str, player: str = "") -> str:
    """
    在 Minecraft 服务端执行一条控制台指令（AI 通过 Function Calling 调用它）。

    ★ 本函数**不再向游戏里发送任何消息**（没有 feedback、没有 tellraw）：
      它只做「校验 → 执行 → 原样返回真实结果」，
      至于怎么向玩家解释，由 agent_core 在整轮对话结束后统一发言。
      这样同一件事只会出现在聊天栏一次，杜绝“工具说一句、模型再说一句”的刷屏。

    :param command: 要执行的指令原文，例如 "list"、"give Steve diamond 1"
    :param player:  发起请求的玩家（由程序注入，AI 不可信）
    :return:        给 AI 看的执行结果字符串（会被放进 tool 消息里）
    """
    cfg = load_config()
    runtime = get_runtime(cfg)

    player = _safe_player(player)
    if not player:
        return "错误：缺少合法的玩家名，已拒绝执行（防止越权操作）。"

    cmd = _normalize_command(command)
    if not cmd:
        return "错误：command 为空，未执行任何操作。"

    max_chars = int(runtime["command_max_chars"])
    if len(cmd) > max_chars:
        return f"拒绝：指令长度超过 {max_chars} 字符上限，未执行。"

    # ---- 第一道闸门：绝对黑名单（AI 返回的指令再检查一次）----
    blocked = find_blocked_keyword(cmd)
    if blocked:
        log.warning("【硬拦截·RCON】玩家=%s 关键字=%s 指令=%s", player, blocked, cmd[:120])
        return (
            f"拦截：指令包含绝对禁止的关键字「{blocked}」，已拒绝执行，服务器未受任何影响。"
            f"请如实告诉玩家这条操作被系统禁止，不要换一种写法重试。"
        )

    # ---- 第二道闸门：权限矩阵 ----
    allowed, reason = check_permission(player, cmd, cfg)
    if not allowed:
        log.warning("【权限拒绝·RCON】玩家=%s 指令=%s", player, cmd[:120])
        return f"拒绝：{reason}。请如实告诉玩家他没有这个权限，不要重试。"

    # ---- 真正执行 ----
    ok, output = _rcon_exec(cmd, cfg)
    if not ok:
        log.error("【RCON 失败】玩家=%s 指令=%s 原因=%s", player, cmd, output)
        return f"执行失败：{output}。请如实告诉玩家这次没执行成功。"

    log.info("【RCON 成功】玩家=%s 指令=%s 回显=%s", player, cmd, output[:200])
    return f"执行成功。RCON 回显：{output or '(无回显)'}"


# ================================================================ 五、工具二：SSH
# SSH 命令额外禁止的“词”：在服务器所在主机上跑 shell 属于高危动作，宁可错杀不可放过
_SSH_FORBIDDEN_WORDS: List[str] = [
    "rm", "mkfs", "dd", "shutdown", "reboot", "poweroff", "halt", "init",
    "kill", "killall", "pkill", "su", "sudo", "passwd", "chmod", "chown", "chattr",
    "crontab", "mount", "umount", "iptables", "sysctl",
    "curl", "wget", "ssh", "scp", "sftp", "nc", "ncat", "telnet",
    "eval", "exec", "bash", "sh", "zsh", "perl", "python", "python3",
    "apt", "apt-get", "pkg", "pm", "fastboot", "adb",
]

# 词边界用 (?<![-\w]) 而不是 \b，这样 "du -sh" 里的 "-sh" 不会被误判成 sh
_SSH_TERM_TEMPLATE = r"(?<![-\w]){}(?![-\w])"

# 管道 / 重定向 / 命令串联等 shell 语法：一律不许，避免“一条命令变一整套操作”
_SSH_FORBIDDEN_SYNTAX: List[str] = [
    r";", r"&", r"\|", r"`", r"\$\(", r">", r"<", r"\n", r"\$\{",
    r"screen\s+-X",     # 用 screen 远程掐掉服务器会话
    r"tmux\s+kill",
]

_SSH_BLOCK_REGEXES: List[re.Pattern] = (
    [re.compile(_SSH_TERM_TEMPLATE.format(re.escape(word)), re.IGNORECASE)
     for word in _SSH_FORBIDDEN_WORDS]
    + [re.compile(pattern, re.IGNORECASE) for pattern in _SSH_FORBIDDEN_SYNTAX]
)


def _match_ssh_forbidden(command: str) -> Optional[str]:
    """返回 SSH 命令里命中的危险片段，未命中返回 None。"""
    for rx in _SSH_BLOCK_REGEXES:
        matched = rx.search(command or "")
        if matched:
            return matched.group(0)
    return None


def _clean_shell_output(text: str, limit: int) -> str:
    """清理 SSH 输出：去控制符、压缩空行、截断长度。"""
    cleaned = ANSI_PATTERN.sub("", str(text or "")).replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in cleaned.split("\n")]
    # 连续空行压缩成一行
    compact: List[str] = []
    for line in lines:
        if line or (compact and compact[-1]):
            compact.append(line)
    result = "\n".join(compact).strip()
    if len(result) > limit:
        result = result[:limit] + "\n……（输出过长，已截断）"
    return result


def _run_local_command(command: str, timeout: int) -> Tuple[int, str, str]:
    """
    local 模式：直接用 subprocess 在本机执行，**完全绕开 SSH / 网络**。

    - shell=True：命令按本机默认 shell 解释（Linux/macOS 是 sh，Windows 是 cmd）；
    - stdin=DEVNULL：不给交互输入，避免命令卡在等密码 / 等确认；
    - 超时会强杀子进程，不会把监听线程一起拖死。

    :return: (退出码, 标准输出, 标准错误)
    :raises RuntimeError: 超时 / 无法启动子进程
    """
    try:
        completed = subprocess.run(
            command,
            shell=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"命令执行超过 {timeout} 秒，已强制结束") from None
    except OSError as exc:
        raise RuntimeError(f"无法启动本机子进程：{exc}") from None

    return int(completed.returncode), completed.stdout or "", completed.stderr or ""


def _run_ssh_command(
    command: str,
    runtime: Dict[str, Any],
    ssh_cfg: Dict[str, Any],
) -> Tuple[int, str, str]:
    """
    ssh 模式：按需导入 paramiko，连上远端主机执行一条命令，用完立刻断开。

    :return: (退出码, 标准输出, 标准错误)
    :raises RuntimeError: 未装 paramiko / 连接信息缺失 / 连接或执行失败
    """
    paramiko = load_paramiko()          # ← 只有走到这里，paramiko 才会被 import

    host = str(ssh_cfg.get("host") or "").strip()
    username = str(ssh_cfg.get("username") or "").strip()
    password = str(ssh_cfg.get("password") or "")
    try:
        port = int(ssh_cfg.get("port") or 22)
    except (TypeError, ValueError):
        port = 22
    if not host or not username:
        raise RuntimeError("config.json 里的 ssh.host / ssh.username 没有配置")

    connect_timeout = int(runtime["ssh_connect_timeout"])
    command_timeout = int(runtime["ssh_command_timeout"])

    client = None
    try:
        client = paramiko.SSHClient()
        # 内网一次性信任主机指纹；要更严格可换成 load_system_host_keys()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(
            hostname=host,
            port=port,
            username=username,
            password=password,
            timeout=connect_timeout,
            banner_timeout=connect_timeout,
            auth_timeout=connect_timeout,
            look_for_keys=False,   # 强制走密码认证，不受本机密钥干扰
            allow_agent=False,
        )
        _stdin, stdout, stderr = client.exec_command(command, timeout=command_timeout)
        out_text = stdout.read().decode("utf-8", "replace")
        err_text = stderr.read().decode("utf-8", "replace")
        exit_code = int(stdout.channel.recv_exit_status())
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError(f"SSH 连接或执行失败：{type(exc).__name__}: {exc}") from None
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:
                pass

    return exit_code, out_text, err_text


def execute_ssh(command: str, player: str = "") -> str:
    """
    在“服务器所在的主机”上执行一条只读 shell 命令（查看进程、磁盘、内存等）。

    执行位置由 config.json 的 read_mode 决定：
    - local：本机 subprocess 执行，不联网、不加载 paramiko；
    - ssh  ：按需加载 paramiko 连远端主机执行。
    三道安全闸门（权限 + 黑名单 + shell 危险词）两种模式完全一致。

    ★ 和 execute_rcon 一样，本函数**不向游戏发送任何消息**，只返回真实输出，
      由 agent_core 统一把结果讲给玩家听。

    :param command: 要执行的 shell 命令，例如 "du -sh ~/server"、"df -h"
    :param player:  发起请求的玩家（由程序注入）
    :return:        给 AI 看的执行结果字符串
    """
    cfg = load_config()
    runtime = get_runtime(cfg)
    ssh_cfg = cfg.get("ssh") or {}

    player = _safe_player(player)
    if not player:
        return "错误：缺少合法的玩家名，已拒绝执行。"

    raw = str(command or "").strip()
    if not raw:
        return "错误：command 为空，未执行任何操作。"

    max_chars = int(runtime["command_max_chars"])
    if len(raw) > max_chars:
        return f"拒绝：SSH 命令长度超过 {max_chars} 字符上限，未执行。"

    # ---- 闸门 0：SSH 是最高危工具，只对 op_whitelist 开放 ----
    if not is_op_player(player, cfg):
        log.warning("【权限拒绝·SSH】非白名单玩家 %s 试图执行：%s", player, raw[:120])
        return (
            f"拒绝：主机命令仅限 op_whitelist 玩家使用，玩家 {player} 无权调用。"
            f"请如实告诉玩家这是管理员专属能力。"
        )

    # ---- 闸门 1：绝对黑名单 ----
    blocked = find_blocked_keyword(raw)
    if blocked:
        log.warning("【硬拦截·SSH】玩家=%s 关键字=%s 命令=%s", player, blocked, raw[:120])
        return (
            f"拦截：SSH 命令包含绝对禁止的关键字「{blocked}」，已拒绝执行。"
            f"请如实告诉玩家这条操作被系统禁止，不要重试。"
        )

    # ---- 闸门 2：shell 危险词 / 危险语法 ----
    hit = _match_ssh_forbidden(raw)
    if hit:
        log.warning("【硬拦截·SSH】玩家=%s 危险片段=%s 命令=%s", player, hit, raw[:120])
        return (
            f"拦截：SSH 命令包含危险片段「{hit}」，已拒绝执行。"
            f"请如实告诉玩家这条命令有破坏性，不要换写法重试。"
        )

    # ---- 闸门 3：执行。local 走 subprocess，ssh 才按需 import paramiko ----
    mode = get_read_mode(cfg)
    is_local = mode == READ_MODE_LOCAL
    where = "本机" if is_local else "远端主机"

    try:
        if is_local:
            log.info("【本机执行】玩家=%s 命令=%s", player, raw)
            exit_code, out_text, err_text = _run_local_command(raw, int(runtime["ssh_command_timeout"]))
        else:
            log.info("【SSH 执行】玩家=%s 命令=%s", player, raw)
            exit_code, out_text, err_text = _run_ssh_command(raw, runtime, ssh_cfg)
    except RuntimeError as exc:            # 已有可读中文说明的失败
        log.error("%s执行失败（%s）：%s", where, raw[:80], exc)
        return f"{where}执行失败：{exc}"
    except Exception as exc:               # 兜底：任何没预料到的异常都不许冒到 AI 循环里
        log.error("%s执行失败（%s）：%s: %s", where, raw[:80], type(exc).__name__, exc)
        return f"{where}执行失败：{type(exc).__name__}: {exc}"

    log.info("【%s 成功】玩家=%s 命令=%s 退出码=%s", "本机" if is_local else "SSH", player, raw, exit_code)

    limit = int(runtime["ssh_output_max_chars"])
    output = _clean_shell_output(out_text, limit)
    error_output = _clean_shell_output(err_text, limit)

    pieces = [f"命令执行完成（{where}），退出码 {exit_code}。"]
    if output:
        pieces.append(f"标准输出：\n{output}")
    if error_output:
        pieces.append(f"标准错误：\n{error_output}")
    if not output and not error_output:
        pieces.append("没有任何输出。")
    return "\n".join(pieces)
