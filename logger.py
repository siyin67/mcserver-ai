# -*- coding: utf-8 -*-
"""
logger.py —— 日志监听模块（SSH 实时 tail）
================================================================
职责：
1. 从 config.json 读取 SSH 连接信息；
2. 用 paramiko 连上远端主机，执行 `tail -F -n 0 {remote_log_path}`；
3. 循环读取 stdout，用正则 `<玩家名> 消息` 匹配玩家聊天；
4. 若消息里包含 trigger_keyword，提取玩家名与请求内容，
   回调 agent_core.process_player_message() 交给 AI 处理。

健壮性设计：
- 断线自动重连（服务器重启、Wi-Fi 抖动都不会让管家失联）；
- 单条消息处理失败不影响整个监听循环；
- 日志轮转导致的重复行会被去重，避免同一请求被执行两次。

配置说明（本文件不含任何个人化硬编码）：
- 连接信息：config.json 的 ssh 段；
- 触发词：config.json 的 trigger_keyword；
- 重连间隔/读超时/去重窗口：config.json 的 runtime 段。
"""

from __future__ import annotations

import logging
import re
import socket
import time
from typing import Any, Dict, Optional

try:
    import paramiko
except ImportError:  # pragma: no cover
    paramiko = None  # type: ignore[assignment]

import agent_core
from tools import get_runtime, get_trigger, load_config, say

log = logging.getLogger("logger")

# ---------------------------------------------------------------- 正则常量
# Minecraft 聊天行格式： [12:34:56] [Server thread/INFO]: <Steve> 你好
# 用 \s? 兼容 "<Steve>hello" 这种没空格的情况
CHAT_PATTERN = re.compile(r"<([^>]+)>\s?(.*)")
PLAYER_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_]{1,16}$")   # Java 版合法玩家名
ANSI_PATTERN = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")          # 远端 SSH 可能带回颜色控制符
UNSAFE_PATH_PATTERN = re.compile(r"[;&|`$<>(){}\[\]\s]")     # 远端路径注入防护

# 去重状态（tail -F 在日志轮转、重连时会重复吐出同一行）
_last_line_text = ""
_last_line_at = 0.0


# ================================================================ 一、连接管理
def _connect(ssh_cfg: Dict[str, Any], timeout: int = 10) -> Any:
    """建立 paramiko SSH 连接（密码认证）。"""
    if paramiko is None:
        raise RuntimeError("未安装 paramiko，请先执行：pip install -r requirements.txt")

    host = str(ssh_cfg.get("host") or "").strip()
    username = str(ssh_cfg.get("username") or "").strip()
    password = str(ssh_cfg.get("password") or "")
    try:
        port = int(ssh_cfg.get("port") or 22)
    except (TypeError, ValueError):
        port = 22

    if not host or not username:
        raise RuntimeError("config.json 里的 ssh.host / ssh.username 没有配置")

    client = paramiko.SSHClient()
    # 内网场景：首次连接自动信任主机指纹。想更严格可以换成 load_system_host_keys()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        hostname=host,
        port=port,
        username=username,
        password=password,
        timeout=timeout,
        banner_timeout=timeout + 5,
        auth_timeout=timeout + 5,
        look_for_keys=False,   # 强制密码认证，避免被本机 SSH 密钥干扰
        allow_agent=False,
    )
    return client


def _build_tail_command(remote_log_path: str) -> str:
    """
    拼出远端命令 tail -F -n 0 {path} 2>&1。

    路径来自 config.json（你自己写的），但仍然做一次注入检查：
    万一配置里不小心带了 ; 或 | 之类的字符，宁可报错也不要执行任意代码。
    """
    path = str(remote_log_path or "").strip()
    if not path:
        raise RuntimeError("config.json 里的 ssh.remote_log_path 为空")
    if UNSAFE_PATH_PATTERN.search(path):
        raise RuntimeError(f"ssh.remote_log_path 含有不安全字符，已拒绝执行：{path!r}")
    # 2>&1：把 tail 的报错合并到 stdout，方便在同一个循环里看到
    return f"tail -F -n 0 {path} 2>&1"


# ================================================================ 二、消息处理
def _is_duplicated(line: str, window: float) -> bool:
    """
    tail -F 在日志轮转、重连时会重复吐出同一行。
    window 秒内完全相同的行视为重复，避免同一个请求被 AI 执行两次。
    """
    global _last_line_text, _last_line_at

    now = time.time()
    duplicated = line == _last_line_text and (now - _last_line_at) < window
    _last_line_text = line
    _last_line_at = now
    return duplicated


def _handle_log_line(line: str, cfg: Dict[str, Any], trigger: str) -> None:
    """
    处理一行日志：匹配玩家聊天 → 判断是否触发 → 交给 AI。

    刻意把每一行的处理都包在 try 里（由调用方 _tail_forever 负责），
    保证任何一条畸形日志都不会中断整个监听。
    """
    match = CHAT_PATTERN.search(line)
    if not match:
        return

    player = match.group(1).strip()
    message = match.group(2).strip()

    # 服务器自己的广播（如 "[Server]"）或带特殊字符的名字直接跳过，
    # 顺便避免把非法名字拼进 tellraw 目标造成指令注入
    if not PLAYER_NAME_PATTERN.match(player):
        log.debug("忽略非玩家聊天行：%s", line[:120])
        return

    if trigger not in message:
        return

    runtime = get_runtime(cfg)
    if _is_duplicated(line, float(runtime["dedupe_window"])):
        log.debug("跳过重复日志行：%s", line[:120])
        return

    # 把触发词之前的内容丢掉，只保留“请求内容”
    index = message.find(trigger)
    request = message[index + len(trigger):].strip(" ：:,，。!！?？\t")

    log.info("【玩家请求】%s：%s", player, request or "（空请求）")

    if not request:
        # 只喊了一声触发词，没说要干嘛：给个引导，不惊动 AI
        say(player, f"我在～ 想让我做什么？例如：{trigger} 现在服务器有几个人", "yellow")
        return

    try:
        agent_core.process_player_message(player, request, full_message=message)
    except Exception:                     # 单条请求失败不能让监听线程退出
        log.exception("处理玩家 %s 的请求时出现异常", player)
        say(player, "我这边处理出错了，请稍后再试。", "red")


# ================================================================ 三、主循环
def _tail_forever(client: Any, cfg: Dict[str, Any]) -> None:
    """
    在已建立的 SSH 连接上持续读取日志，直到连接断开 / 收到 Ctrl+C。

    说明：这里直接对 channel 做 recv() 而不是用 file 对象的 readline()，
    因为 channel 设了 settimeout 后，file 对象在超时后的缓冲状态不可靠，
    而原始 recv 可以安全地反复 try/except 继续读。
    """
    ssh_cfg = cfg.get("ssh") or {}
    runtime = get_runtime(cfg)
    trigger = get_trigger(cfg)
    channel_timeout = float(runtime["tail_channel_timeout"])
    command = _build_tail_command(ssh_cfg.get("remote_log_path"))

    log.info("远端命令：%s", command)
    _stdin, stdout, _stderr = client.exec_command(command)

    channel = stdout.channel
    channel.settimeout(channel_timeout)   # 周期唤醒，保证 Ctrl+C 能及时响应

    buffer = ""
    while True:
        try:
            chunk = channel.recv(65536)
        except socket.timeout:
            continue                       # 正常：这段时间没有新日志
        except KeyboardInterrupt:
            raise                          # 交给 main.py 优雅退出
        except Exception as exc:
            log.error("读取日志流失败：%s: %s", type(exc).__name__, exc)
            raise

        if not chunk:
            log.warning("远端 tail 已结束（连接被关闭）")
            return

        buffer += chunk.decode("utf-8", "replace")

        # 按行切分，最后一段可能是不完整的半行，留在 buffer 里等下一次
        while "\n" in buffer:
            raw_line, buffer = buffer.split("\n", 1)
            line = ANSI_PATTERN.sub("", raw_line).replace("\r", "").strip()
            if not line:
                continue
            log.debug("日志：%s", line)
            try:
                _handle_log_line(line, cfg, trigger)
            except KeyboardInterrupt:
                raise
            except Exception:
                # 单行处理异常不中断监听
                log.exception("处理日志行时出错：%s", line[:200])


def start_watch(cfg: Optional[Dict[str, Any]] = None) -> None:
    """
    启动日志监听主循环（断线自动重连）。

    该函数会一直阻塞，直到收到 KeyboardInterrupt（由 main.py 捕获）。

    :param cfg: 配置字典；省略则从 config.json 读取
    """
    cfg = cfg if cfg is not None else load_config()
    runtime = get_runtime(cfg)
    ssh_cfg = cfg.get("ssh") or {}
    host = ssh_cfg.get("host")
    port = ssh_cfg.get("port")
    username = ssh_cfg.get("username")
    reconnect_delay = float(runtime["reconnect_delay"])
    connect_timeout = int(runtime["ssh_connect_timeout"])

    while True:
        client = None
        try:
            log.info("正在连接 SSH：%s@%s:%s ……", username, host, port)
            client = _connect(ssh_cfg, connect_timeout)
            log.info("SSH 连接成功，开始实时监听服务器日志")
            _tail_forever(client, cfg)
        except KeyboardInterrupt:
            log.info("收到退出信号，正在关闭日志监听……")
            raise
        except Exception as exc:
            log.error(
                "日志监听中断：%s: %s（%.0f 秒后自动重连）",
                type(exc).__name__, exc, reconnect_delay,
            )
        finally:
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass

        # 重连前的等待；这里也可能被 Ctrl+C 打断，所以包起来
        try:
            time.sleep(reconnect_delay)
        except KeyboardInterrupt:
            log.info("收到退出信号，正在关闭日志监听……")
            raise


# ================================================================ 四、单独运行
if __name__ == "__main__":
    # 单独调试日志监听：python logger.py
    import sys

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
        datefmt="%H:%M:%S",
    )

    try:
        start_watch()
    except KeyboardInterrupt:
        print("\n[退出] 日志监听已停止。")
