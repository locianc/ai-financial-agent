
"""本机网络环境适配模块。

import 本模块即生效（对 socket.getaddrinfo 打补丁，进程内有效）：
1. NO_PROXY="*"：绕过本机不稳定的 Windows 系统代理（127.0.0.1:7897），改直连；
2. 强制 IPv4：本机 IPv6 链路到东方财富连接被重置，IPv4 路径正常；
3. 东方财富各接口固定 IP：本网络下 Azure Traffic Manager 返回的 IP 多数被重置，
   61.129.129.48（push2delay 服务器）实测稳定可达。将固定 IP 排在解析结果
   首位优先尝试，不可达时自动回退到 DNS 结果：
   - push2（实时行情）：72.push2.eastmoney.com
   - push2his（历史K线）：*.push2his.eastmoney.com

说明：仅作用于本机本进程，不改动任何系统配置；本机之外的网络无需此适配。
"""

import os
import socket

os.environ.setdefault("NO_PROXY", "*")

_ORIG_GETADDRINFO = socket.getaddrinfo
_PINNED_IP = "61.129.129.48"


def _patched_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    """本地化 DNS 解析：强制 IPv4，并将东方财富主机固定到实测可达的 IP。"""
    if family == socket.AF_UNSPEC:
        family = socket.AF_INET
    is_eastmoney = (
        host.endswith(".push2.eastmoney.com")
        or host.endswith(".push2his.eastmoney.com")
    )
    if is_eastmoney:
        pinned = _ORIG_GETADDRINFO(_PINNED_IP, port, family, type, proto, flags)
        return pinned + _ORIG_GETADDRINFO(host, port, family, type, proto, flags)
    return _ORIG_GETADDRINFO(host, port, family, type, proto, flags)


socket.getaddrinfo = _patched_getaddrinfo
