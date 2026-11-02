# -*- coding: utf-8 -*-
"""
logger.py —— 日志监听模块（双模式：本机文件 / SSH 远端）
================================================================
职责：
1. 读 config.json 的 read_mode 决定监听方式；
2. local 模式：直接用标准库 open() + seek 模拟 `tail -F`，读本机日志文件；
3. ssh   模式：按需 import paramiko，远端执行 `tail -F -n 0 {path}` 读日志；
4. 用正则 `<玩家名> 消息` 匹配聊天，命中 trigger_keyword 就交给 AI。

极致按需加载（本文件的硬性约束）：
- **顶层绝不 import paramiko**：本地模式下整个进程里都不会出现这个模块，
  只有 ssh 模式真的建连接时才通过 tools.load_paramiko() 导入；
- 本地模式只依赖标准库（os / time / codecs），没有网络、没有 SSH。

健壮性设计：
- 断线（ssh）或日志文件轮转 / 被删（local）都会自动恢复；
- 单条消息处理失败不影响整个监听循环；
- 日志轮转导致的重复行会被去重，避免同一请求被执行两次。

配置说明（本文件不含任何个人化硬编码）：
- 模式：config.json 的 read_mode（"local" / "ssh"）；
- 路径：local 用 local_log_path，ssh 用 ssh.remote_log_path；
- 触发词：config.json 的 trigger_keyword；
- 轮询间隔/读超时/重连间隔/去重窗口：config.json 的 runtime 段。
"""

from __future__ import annotations

import codecs
import logging
import os
import re
import socket
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import agent_core
from tools import (
    READ_MODE_LOCAL,
    get_log_path,
    get_read_mode,
    get_runtime,
    get_trigger,
    load_config,
    load_paramiko,
    say,
)

log = logging.getLogger("logger")

# ---------------------------------------------------------------- 正则常量
# Minecraft 聊天行格式： [12:34:56] [Server thread/INFO]: <Steve> 你好
# 用 \s? 兼容 "<Steve>hello" 这种没空格的情况
CHAT_PATTERN = re.compile(r"<([^>]+)>\s?(.*)")
PLAYER_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_]{1,16}$")   # Java 版合法玩家名
ANSI_PATTERN = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")          # 控制台可能带回颜色控制符
UNSAFE_PATH_PATTERN = re.compile(r"[;&|`$<>(){}\[\]\s]")     # 拼 SSH 命令时的路径注入防护
LOCAL_PATH_BAD_PATTERN = re.compile(r"[\x00-\x1f]")          # 本地路径只挡不可见控制字符

# 单行日志缓冲上限：防止异常堆栈一直不换行导致内存被撑爆
_MAX_LINE_BUFFER = 1 << 20   # 1 MiB

# 去重状态（日志轮转、重连时会重复吐出同一行）
_last_line_text = ""
_last_line_at = 0.0


# ================================================================ 一、SSH 连接（仅 ssh 模式）
def _connect(ssh_cfg: Dict[str, Any], timeout: int = 10) -> Any:
    """
    建立 paramiko SSH 连接（密码认证）。

    注意：paramiko 是在 tools.load_paramiko() 里**函数内导入**的，
    本地模式永远不会走到这里，因此也永远不会触发这次 import。
    """
    paramiko = load_paramiko()

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
    拼出远端命令 tail -F -n 0 {path} 2>&1（仅 ssh 模式使用）。

    路径来自 config.json（你自己写的），但仍然做一次注入检查：
    万一配置里不小心带了 ; 或 | 之类的字符，宁可报错也不要执行任意代码。
    """
    path = str(remote_log_path or "").strip()
    if not path:
        raise RuntimeError("ssh 模式下 config.json 里的 ssh.remote_log_path 为空")
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

    刻意把每一行的处理都包在 try 里（由 _process_line 负责），
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


def _process_line(raw_line: str, cfg: Dict[str, Any], trigger: str) -> None:
    """清洗一行并交给 _handle_log_line；单行异常绝不冒到主循环。"""
    line = ANSI_PATTERN.sub("", raw_line).replace("\r", "").strip()
    if not line:
        return
    log.debug("日志：%s", line)
    try:
        _handle_log_line(line, cfg, trigger)
    except KeyboardInterrupt:
        raise
    except Exception:
        log.exception("处理日志行时出错：%s", line[:200])


# ================================================================ 三、模式一：SSH 远端 tail
def _tail_ssh_forever(client: Any, cfg: Dict[str, Any]) -> None:
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
    command = _build_tail_command(get_log_path(cfg) or ssh_cfg.get("remote_log_path"))

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
            _process_line(raw_line, cfg, trigger)


def _watch_ssh(cfg: Dict[str, Any]) -> None:
    """ssh 模式的常驻循环：连接 → tail → 断了就等一会儿重连。"""
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
            _tail_ssh_forever(client, cfg)
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


# ================================================================ 四、模式二：本地文件 tail -F
def _resolve_local_path(path: str) -> Path:
    """
    把配置里的本地日志路径解析成绝对路径。

    本地模式不走 shell，不存在命令注入问题，所以只拒绝不可见控制字符，
    放行中文、空格、Windows 盘符等正常路径。
    """
    raw = str(path or "").strip()
    if not raw:
        raise RuntimeError(
            "local 模式下没有配置日志路径：请填写 local_log_path"
            "（或沿用 ssh.remote_log_path，例如 ~/fabric/logs/latest.log）"
        )
    if LOCAL_PATH_BAD_PATTERN.search(raw):
        raise RuntimeError(f"日志路径含有不可见控制字符，已拒绝：{raw!r}")

    expanded = os.path.expandvars(os.path.expanduser(raw))
    return Path(expanded).expanduser().resolve()


def _open_local_file(path: Path, at_end: bool) -> Tuple[Any, Tuple[int, int]]:
    """
    打开日志文件（二进制，自己解码，避免文本模式的换行转换与 tell() 语义坑）。

    :param at_end: True 表示从文件末尾开始（等同 tail -n 0 -F）；
                   False 表示从头读（日志轮转后接上新的文件）。
    :return: (文件对象, (st_dev, st_ino) 用于识别轮转)
    """
    fp = open(path, "rb")
    try:
        stat = os.fstat(fp.fileno())
        if at_end:
            fp.seek(0, os.SEEK_END)
    except OSError:
        fp.close()
        raise
    return fp, (stat.st_dev, stat.st_ino)


def _close_quietly(fp: Optional[Any]) -> None:
    """关闭文件句柄，任何异常都吞掉（关闭失败不值得打断监听）。"""
    if fp is None:
        return
    try:
        fp.close()
    except Exception:
        pass


def _tail_local_forever(cfg: Dict[str, Any]) -> None:
    """
    本地模式的 `tail -F`：open() + 循环 read()。

    与 tail -F 对齐的三个行为：
    1. 启动时从文件末尾开始，不把历史日志当成新消息；
    2. 文件被轮转（inode 变化）或删除后重新创建时，自动接上新文件；
    3. 文件被截断（copytruncate 式轮转）时，回到文件开头继续读。
    """
    runtime = get_runtime(cfg)
    trigger = get_trigger(cfg)
    poll = max(0.05, float(runtime["local_poll_interval"]))
    retry = float(runtime["reconnect_delay"])
    path = _resolve_local_path(get_log_path(cfg))

    log.info("本地模式：监听日志文件 %s（轮询 %.2f 秒）", path, poll)

    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    buffer = ""
    fp: Optional[Any] = None
    file_key: Optional[Tuple[int, int]] = None

    while True:
        # ---- 文件没打开：尝试打开（首次从末尾，轮转后从头） ----
        if fp is None:
            try:
                fp, file_key = _open_local_file(path, at_end=file_key is None)
            except FileNotFoundError:
                log.warning("日志文件还不存在：%s（%.0f 秒后重试）", path, retry)
                time.sleep(retry)
                continue
            except OSError as exc:
                raise RuntimeError(f"打不开日志文件 {path}：{exc}") from None

            buffer = ""
            decoder = codecs.getincrementaldecoder("utf-8")("replace")
            log.info("已打开日志文件（当前 %d 字节），开始实时监听", fp.tell())

        time.sleep(poll)

        # ---- 每轮先确认文件还是不是原来那个 ----
        try:
            stat = os.stat(path)
        except FileNotFoundError:
            log.warning("日志文件不见了（可能正在轮转），等待重新出现……")
            _close_quietly(fp)
            fp = None
            continue
        except OSError as exc:
            log.error("读取文件状态失败：%s: %s", type(exc).__name__, exc)
            _close_quietly(fp)
            fp = None
            time.sleep(retry)
            continue

        if (stat.st_dev, stat.st_ino) != file_key:
            log.info("检测到日志轮转（文件已更换），重新打开新文件")
            _close_quietly(fp)
            fp = None
            continue

        try:
            position = fp.tell()
        except OSError:
            position = 0
        if stat.st_size < position:        # 文件被原地清空
            log.info("检测到日志被截断，从头继续读取")
            try:
                fp.seek(0)
            except OSError:
                _close_quietly(fp)
                fp = None
                continue
            decoder.reset()
            buffer = ""

        try:
            chunk = fp.read()
        except OSError as exc:
            log.error("读取日志失败：%s: %s", type(exc).__name__, exc)
            _close_quietly(fp)
            fp = None
            time.sleep(retry)
            continue

        if not chunk:
            continue                       # 这段时间没有新内容

        buffer += decoder.decode(chunk)
        while "\n" in buffer:
            raw_line, buffer = buffer.split("\n", 1)
            _process_line(raw_line, cfg, trigger)

        # 防御：极端情况下（超长单行）不让 buffer 无限增长
        if len(buffer) > _MAX_LINE_BUFFER:
            log.warning("单行日志超过 %d 字符，已丢弃前半段", _MAX_LINE_BUFFER)
            buffer = buffer[-_MAX_LINE_BUFFER:]


def _watch_local(cfg: Dict[str, Any]) -> None:
    """
    本地模式的常驻循环。

    文件暂时不存在（服务器还没启动 / 正在轮转）属于正常情况，继续重试；
    配置类错误（路径为空、路径非法）直接抛给 main.py，重试没有意义。
    """
    retry = float(get_runtime(cfg)["reconnect_delay"])

    while True:
        try:
            _tail_local_forever(cfg)       # 正常情况下不会返回
            return
        except KeyboardInterrupt:
            log.info("收到退出信号，正在停止本地日志监听……")
            raise
        except RuntimeError:
            raise
        except Exception as exc:
            log.error(
                "本地日志监听中断：%s: %s（%.0f 秒后重试）",
                type(exc).__name__, exc, retry,
            )
        try:
            time.sleep(retry)
        except KeyboardInterrupt:
            raise


# ================================================================ 五、入口
def start_watch(cfg: Optional[Dict[str, Any]] = None) -> None:
    """
    启动日志监听主循环，按 read_mode 自动选择实现：

    - local：本机 open() + seek 模拟 tail -F，全程不 import paramiko；
    - ssh  ：paramiko 远端 tail -F，断线自动重连。

    该函数会一直阻塞，直到收到 KeyboardInterrupt（由 main.py 捕获）。

    :param cfg: 配置字典；省略则从 config.json 读取
    """
    cfg = cfg if cfg is not None else load_config()
    mode = get_read_mode(cfg)

    log.info(
        "运行模式：%s —— %s",
        mode,
        "本机读取日志文件（不联网、不加载 paramiko）"
        if mode == READ_MODE_LOCAL
        else "SSH 远端读取日志",
    )

    if mode == READ_MODE_LOCAL:
        _watch_local(cfg)
    else:
        _watch_ssh(cfg)


# ================================================================ 六、单独运行
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
