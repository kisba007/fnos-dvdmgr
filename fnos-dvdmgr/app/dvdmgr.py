#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DVD光驱助手 (dvdmgr) - 飞牛 fnOS 第三方应用
=================================================
自动识别并管理 USB DVD/光驱设备：
  * 检测 /dev/sr* 光驱（区分 USB / SATA）
  * 插入光盘后自动挂载（ISO9660 / UDF，只读）
  * 热插拔事件即时感知（udev 触发 + 轮询兜底）
  * 内置 HTTP 管理界面：查看光驱状态、挂载 / 卸载 / 弹出、配置

运行模式：
  --daemon [--pid-file PATH] [--var-dir PATH] [--etc-dir PATH]  后台守护
  --scan                                                        一次性输出设备 JSON
  --mount <srN> / --unmount <srN> / --eject <srN>               单次操作
  --unmount-all                                                 卸载本应用挂载的全部光驱
  --udev <add|remove> <srN>                                     udev 事件通知（触发即时扫描）
"""

import argparse
import json
import os
import re
import glob
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
DEFAULT_PORT = 8085
DEFAULT_MOUNT_BASE = "/media/dvd"
POLL_INTERVAL = 2.0          # 轮询周期（秒）
OPTICAL_READONLY_FS = {"iso9660", "udf", "vfat"}

# /proc/mounts 中本应用管理的挂载点前缀
MOUNT_LINE_RE = re.compile(r"^(/dev/sr[0-9]+)\s+(\S+)\s+(\S+)")

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
def _find_user_home():
    """探测飞牛用户「我的文件」目录：/vol*/<纯数字uid>，取 uid 最小（通常为 admin）"""
    homes = []
    for d in glob.glob("/vol*/[0-9]*"):
        if not os.path.isdir(d):
            continue
        try:
            uid = int(os.path.basename(d))
        except ValueError:
            continue
        homes.append((uid, d))
    if not homes:
        return None
    homes.sort()
    return homes[0][1]


def _default_mount_base():
    """默认挂载点：优先挂到用户「我的文件」下的 dvdmgr（文件管理主界面即可见，类似 U 盘）"""
    uh = _find_user_home()
    if uh:
        return os.path.join(uh, "dvdmgr")
    shares = (os.environ.get("TRIM_DATA_SHARE_PATHS") or "").strip()
    if shares:
        return shares.split(":")[0].strip()
    return DEFAULT_MOUNT_BASE


class Config:
    """加载 / 保存 etc 目录下的 config.json"""

    DEFAULTS = {
        "port": DEFAULT_PORT,
        "mount_base": DEFAULT_MOUNT_BASE,
        "auto_mount": True,
        "usb_only": True,
        "readonly": True,
    }

    def __init__(self, etc_dir):
        self.path = os.path.join(etc_dir, "config.json") if etc_dir else None
        self.data = dict(self.DEFAULTS)
        self.data["mount_base"] = _default_mount_base()
        if self.path and os.path.isfile(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                for k in self.DEFAULTS:
                    if k in loaded:
                        self.data[k] = loaded[k]
                # 若挂载点是默认值（旧默认 /media/dvd 或应用共享目录），
                # 视为未定制，自动切换到用户「我的文件」
                share = ((os.environ.get("TRIM_DATA_SHARE_PATHS") or "").strip()
                         .split(":")[0].strip() or "")
                mb = str(self.data.get("mount_base") or "").strip()
                if (not mb or os.path.normpath(mb) == DEFAULT_MOUNT_BASE
                        or (share and os.path.normpath(mb) == os.path.normpath(share))):
                    self.data["mount_base"] = _default_mount_base()
            except Exception:
                pass

    def get(self, key):
        return self.data.get(key, self.DEFAULTS.get(key))

    def save(self):
        if not self.path:
            return False
        try:
            d = os.path.dirname(self.path)
            if d:
                os.makedirs(d, exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
            return True
        except Exception:
            return False

    def update(self, patch):
        changed = False
        for k, v in patch.items():
            if k in self.DEFAULTS and v is not None:
                if self.data.get(k) != v:
                    self.data[k] = v
                    changed = True
        if changed:
            self.save()
        return changed


# ---------------------------------------------------------------------------
# 系统命令封装
# ---------------------------------------------------------------------------
def _run(cmd, timeout=30):
    """执行命令，返回 (rc, stdout, stderr)"""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except FileNotFoundError:
        return 127, "", "command not found: " + cmd[0]
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"
    except Exception as e:
        return 1, "", str(e)


# ---------------------------------------------------------------------------
# 运行日志
# ---------------------------------------------------------------------------
LOG_FILE = None


def set_log_file(path):
    global LOG_FILE
    LOG_FILE = path


def _log(msg):
    if not LOG_FILE:
        return
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%F %T')}] {msg}\n")
    except Exception:
        pass


def is_mounted(dev):
    """根据 /proc/mounts 判断 /dev/<dev> 是否已挂载，返回挂载点或 None"""
    target = f"/dev/{dev}"
    try:
        with open("/proc/mounts", "r", encoding="utf-8") as f:
            for line in f:
                src, mnt, _ = line.split(None, 2)
                if src == target:
                    return mnt
    except Exception:
        pass
    return None


def blockdev_size(dev):
    """返回设备块大小（字节）；无介质 / 失败返回 0"""
    rc, out, _ = _run(["blockdev", "--getsize64", f"/dev/{dev}"], timeout=10)
    if rc == 0 and out.isdigit():
        return int(out)
    return 0


def list_optical_devices():
    """
    使用 lsblk 枚举所有 rom（光驱）设备。
    返回列表，每项:
      {dev, is_usb, model, fs, label, size_bytes, hotplug}
    兼容不同 lsblk 版本：优先尝试完整列，失败则降级；单设备读取异常不整体放弃。
    """
    if not shutil.which("lsblk"):
        _log("警告: 系统无 lsblk，无法枚举光驱")
        return []

    attempts = [
        ["NAME", "TRAN", "TYPE", "FSTYPE", "LABEL", "SIZE", "RO", "HOTPLUG", "MODEL"],
        ["NAME", "TRAN", "TYPE", "FSTYPE", "LABEL", "SIZE", "RO", "HOTPLUG"],
        ["NAME", "TRAN", "TYPE", "FSTYPE", "LABEL", "SIZE", "RO"],
    ]
    data = None
    for cols in attempts:
        try:
            cmd = ["lsblk", "-J", "-b", "-o", ",".join(cols)]
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
            if p.returncode == 0 and p.stdout.strip():
                data = json.loads(p.stdout)
                break
            if p.returncode != 0 and p.stderr.strip():
                _log(f"lsblk 列 {cols} 失败: {p.stderr.strip()[:200]}")
        except Exception as e:
            _log(f"lsblk 列 {cols} 异常: {e}")

    if not data or not isinstance(data, dict):
        _log("警告: lsblk 未能返回有效数据，无法枚举光驱")
        return []

    result = []
    for blk in data.get("blockdevices", []):
        if blk.get("TYPE") != "rom":
            continue
        name = blk.get("NAME", "")
        trans = (blk.get("TRAN") or "").lower()
        result.append({
            "dev": name,
            "is_usb": trans == "usb",
            "trans": trans or "unknown",
            "model": (blk.get("MODEL") or "").strip() or "光驱",
            "fs": blk.get("FSTYPE") or "",
            "label": blk.get("LABEL") or "",
            "size_bytes": blk.get("SIZE") or 0,
            "hotplug": bool(blk.get("HOTPLUG")),
        })

    # 兜底：若 lsblk 未列出但 /sys 存在 sr* 设备，补充加入
    for sr in glob.glob("/sys/class/block/sr[0-9]*"):
        dev = os.path.basename(sr)
        if any(d["dev"] == dev for d in result):
            continue
        _log(f"兜底发现 {dev}（lsblk 未列出）")
        result.append({
            "dev": dev,
            "is_usb": True,
            "trans": "usb",
            "model": "光驱",
            "fs": "",
            "label": "",
            "size_bytes": 0,
            "hotplug": True,
        })
    return result


def has_media(dev):
    """光驱托盘内是否有光盘介质"""
    return blockdev_size(dev) > 0


# ---------------------------------------------------------------------------
# 挂载 / 卸载 / 弹出
# ---------------------------------------------------------------------------
def do_mount(dev, fstype, mount_base, readonly=True):
    """挂载光驱到 mount_base/<dev>，返回 (ok, message, mountpoint)"""
    mountpoint = os.path.join(mount_base, dev)
    os.makedirs(mountpoint, exist_ok=True)

    fstype = (fstype or "").lower()
    if fstype and fstype not in OPTICAL_READONLY_FS:
        # 未知 / 不常见文件系统，交给 auto 尝试
        fstype = "auto"
    cmd = ["mount"]
    if fstype:
        cmd += ["-t", fstype]
    opts = "ro,noatime" if readonly else "noatime"
    cmd += ["-o", opts, f"/dev/{dev}", mountpoint]

    rc, _, err = _run(cmd, timeout=60)
    if rc == 0:
        return True, "已挂载", mountpoint
    return False, f"挂载失败: {err}", mountpoint


def do_unmount(dev):
    """卸载光驱，返回 (ok, message)"""
    mnt = is_mounted(dev)
    if mnt:
        rc, _, err = _run(["umount", mnt], timeout=60)
        if rc != 0:
            return False, f"卸载失败: {err}"
    return True, "已卸载"


def do_eject(dev):
    """弹出光驱托盘（先卸载）"""
    ok, msg = do_unmount(dev)
    if not ok:
        return ok, msg
    # 1) 优先使用 eject 命令
    if shutil.which("eject"):
        rc, _, err = _run(["eject", f"/dev/{dev}"], timeout=30)
        if rc == 0:
            return True, "已弹出"
        return False, f"弹出失败: {err}"
    # 2) 无 eject 命令时通过 ioctl(CDROMEJECT) 弹出
    try:
        import fcntl
        fd = os.open(f"/dev/{dev}", os.O_RDONLY | os.O_NONBLOCK)
        try:
            fcntl.ioctl(fd, 0x5309)  # CDROMEJECT
        finally:
            os.close(fd)
        return True, "已弹出"
    except Exception as e:
        return False, f"弹出失败: {e}"


def unmount_all():
    """卸载所有 /dev/sr* 光驱挂载（不依赖具体挂载点位置）"""
    try:
        with open("/proc/mounts", "r", encoding="utf-8") as f:
            lines = f.readlines()
    except Exception:
        return
    for line in lines:
        m = MOUNT_LINE_RE.match(line.strip())
        if m:
            _, mnt, _ = m.groups()
            subprocess.run(["umount", mnt], capture_output=True, timeout=30)


# ---------------------------------------------------------------------------
# 设备状态聚合
# ---------------------------------------------------------------------------
def build_device_view(dev, mount_base):
    """为单个光驱构造完整视图（含实时挂载 / 介质状态）"""
    mnt = is_mounted(dev["dev"])
    media = has_media(dev["dev"])
    view = {
        "dev": dev["dev"],
        "is_usb": dev["is_usb"],
        "transport": dev["trans"],
        "model": dev["model"],
        "has_media": media,
        "fs": dev["fs"],
        "label": dev["label"],
        "size_bytes": dev["size_bytes"],
        "mounted": bool(mnt),
        "mountpoint": mnt or "",
        "expected_mountpoint": os.path.join(mount_base, dev["dev"]),
    }
    # 无介质时清理文件系统信息
    if not media:
        view["fs"] = ""
        view["label"] = ""
    return view


class DvdManager:
    """守护进程核心：轮询光驱 + 自动挂载 + 状态维护"""

    def __init__(self, config, var_dir):
        self.config = config
        self.var_dir = var_dir
        self.lock = threading.Lock()
        self._known = {}          # dev -> dict
        self._last_error = {}

    # -- 对外状态 ----------------------------------------------------------
    def snapshot(self):
        """返回所有光驱的视图列表"""
        devices = list_optical_devices()
        with self.lock:
            live = {d["dev"] for d in devices}
            # 清理已消失的设备
            for gone in [d for d in self._known if d not in live]:
                self._known.pop(gone, None)
            for d in devices:
                self._known[d["dev"]] = d

            mount_base = self.config.get("mount_base")
            views = []
            for dev in devices:
                view = build_device_view(dev, mount_base)
                view["auto_mount"] = self._should_auto_mount(dev)
                view["last_error"] = self._last_error.get(dev["dev"], "")
                views.append(view)
        views.sort(key=lambda v: v["dev"])
        return views

    def _should_auto_mount(self, dev):
        if not self.config.get("auto_mount"):
            return False
        if self.config.get("usb_only") and not dev["is_usb"]:
            return False
        return True

    # -- 单次操作 ----------------------------------------------------------
    def action(self, dev, action):
        """执行挂载 / 卸载 / 弹出。返回 (ok, message)"""
        match = [d for d in list_optical_devices() if d["dev"] == dev]
        if not match:
            return False, f"未找到光驱 {dev}"
        d = match[0]
        mount_base = self.config.get("mount_base")

        if action == "mount":
            if is_mounted(dev):
                return True, "已处于挂载状态"
            if not has_media(dev):
                return False, "光驱内无光盘"
            ok, msg, mnt = do_mount(dev, d["fs"], mount_base,
                                    self.config.get("readonly"))
            if ok:
                self._last_error.pop(dev, None)
            else:
                self._last_error[dev] = msg
            return ok, msg

        if action == "unmount":
            ok, msg = do_unmount(dev)
            if ok:
                self._last_error.pop(dev, None)
            return ok, msg

        if action == "eject":
            ok, msg = do_eject(dev)
            if ok:
                self._last_error.pop(dev, None)
            else:
                self._last_error[dev] = msg
            return ok, msg

        return False, f"未知操作: {action}"

    # -- 自动挂载轮询 ------------------------------------------------------
    def scan_once(self):
        """执行一次完整检测：新设备挂载、介质移除卸载、设备移除卸载"""
        devices = list_optical_devices()
        mount_base = self.config.get("mount_base")
        _log(f"扫描: 发现 {len(devices)} 台光驱 -> "
             f"{[d['dev'] for d in devices]}")

        with self.lock:
            live = {d["dev"] for d in devices}
            # 设备被拔出
            for dev in [d for d in self._known if d not in live]:
                if is_mounted(dev):
                    do_unmount(dev)
                self._known.pop(dev, None)
                self._last_error.pop(dev, None)

            for d in devices:
                dev = d["dev"]
                prev = self._known.get(dev)
                self._known[dev] = d

                media = has_media(dev)
                mnt = is_mounted(dev)

                # 介质被取出 -> 卸载
                if prev and prev.get("_had_media") and not media and mnt:
                    do_unmount(dev)
                d["_had_media"] = media

                # 自动挂载：有介质 + 未挂载 + 命中策略
                if media and not mnt and self._should_auto_mount(d):
                    ok, msg, _ = do_mount(dev, d["fs"], mount_base,
                                          self.config.get("readonly"))
                    if not ok:
                        self._last_error[dev] = msg

        self._signal_tail()

    def _signal_tail(self):
        """每次扫描后清空 udev 触发标记（供测试/调试）"""
        trigger = os.path.join(self.var_dir, "udev.trigger")
        try:
            if os.path.exists(trigger):
                os.unlink(trigger)
        except Exception:
            pass

    # -- 守护循环 ----------------------------------------------------------
    def run_forever(self, stop_event):
        while not stop_event.is_set():
            try:
                self.scan_once()
            except Exception:
                pass
            stop_event.wait(POLL_INTERVAL)


# ---------------------------------------------------------------------------
# HTTP 管理界面
# ---------------------------------------------------------------------------
def build_diag():
    """汇总系统侧诊断信息，供排障使用"""
    info = {
        "pid": os.getpid(),
        "euid": os.geteuid(),
        "is_root": os.geteuid() == 0,
        "python": sys.version.split()[0],
    }
    try:
        info["dev_sr"] = sorted(glob.glob("/dev/sr[0-9]*"))
    except Exception as e:
        info["dev_sr"] = [f"err:{e}"]
    try:
        info["sys_sr"] = sorted(glob.glob("/sys/class/block/sr[0-9]*"))
    except Exception as e:
        info["sys_sr"] = [f"err:{e}"]

    rc, out, _ = _run(["lsblk", "--version"], timeout=10)
    info["lsblk_version"] = out
    rc, out, err = _run(
        ["lsblk", "-o", "NAME,TRAN,TYPE,FSTYPE,LABEL,SIZE,MOUNTPOINT"], timeout=20)
    info["lsblk_raw_ok"] = (rc == 0)
    info["lsblk_raw"] = out if rc == 0 else f"rc={rc} err={err}"
    rc, out, _ = _run(["lsblk", "-J", "-b"], timeout=20)
    info["lsblk_json_ok"] = (rc == 0)
    info["lsblk_json"] = out if rc == 0 else "failed"

    rc, out, _ = _run(["dmesg", "-T"], timeout=10)
    lines = [l for l in out.splitlines()
             if re.search(r"usb|sr[0-9]|scsi|cdrom|dvd\b|cd \b|blkfront", l, re.I)]
    info["dmesg_optical"] = lines[-40:]
    info["dmesg_rc"] = rc

    try:
        with open("/proc/mounts", encoding="utf-8") as f:
            mnts = [l.strip() for l in f if l.startswith("/dev/sr")]
        info["mounted_sr"] = mnts
    except Exception as e:
        info["mounted_sr"] = [f"err:{e}"]
    return info


class DvdHandler(BaseHTTPRequestHandler):
    server_version = "dvdmgr/1.0"

    @property
    def mgr(self):
        return self.server.manager

    # ---- 工具 ------------------------------------------------------------
    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code, obj):
        self._send(code, obj)

    def log_message(self, fmt, *args):
        # 关闭默认访问日志，保持终端干净
        pass

    # ---- 路由 ------------------------------------------------------------
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path == "/" or path == "/index.html":
            self._send(200, INDEX_HTML, "text/html; charset=utf-8")
        elif path == "/api/devices":
            self._json(200, {"ok": True, "devices": self.mgr.snapshot(),
                             "config": self.mgr.config.data})
        elif path == "/api/config":
            self._json(200, {"ok": True, "config": self.mgr.config.data})
        elif path == "/api/diag":
            self._json(200, {"ok": True, "diag": build_diag()})
        else:
            self._json(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        try:
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b"{}"
            payload = json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            payload = {}

        if path == "/api/action":
            dev = payload.get("dev", "")
            action = payload.get("action", "")
            if not dev or not action:
                self._json(400, {"ok": False, "error": "缺少 dev/action 参数"})
                return
            ok, msg = self.mgr.action(dev, action)
            self._json(200, {"ok": ok, "message": msg})

        elif path == "/api/config":
            changed = self.mgr.config.update(payload)
            msg = "配置已保存" if changed else "配置无变化"
            self._json(200, {"ok": True, "message": msg,
                             "config": self.mgr.config.data})

        elif path == "/api/refresh":
            self.mgr.scan_once()
            self._json(200, {"ok": True, "devices": self.mgr.snapshot()})

        else:
            self._json(404, {"ok": False, "error": "not found"})


# ---------------------------------------------------------------------------
# 内嵌 Web 管理界面
# ---------------------------------------------------------------------------
INDEX_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>DVD光驱助手</title>
<style>
  :root{
    --bg:#0f1420; --panel:#161c2c; --panel2:#1d2540; --line:#26314f;
    --txt:#e6e9f0; --muted:#8a93a8; --accent:#4f8cff; --ok:#2ecc71; --warn:#f1c40f; --err:#e74c3c;
  }
  *{box-sizing:border-box;margin:0;padding:0}
  body{background:var(--bg);color:var(--txt);font-family:-apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;min-height:100vh;padding:24px}
  .wrap{max-width:980px;margin:0 auto}
  header{display:flex;align-items:center;gap:14px;margin-bottom:22px}
  header .logo{width:44px;height:44px;border-radius:10px;background:linear-gradient(135deg,#4f8cff,#7b5cff);display:flex;align-items:center;justify-content:center;font-size:22px}
  header h1{font-size:22px;font-weight:600}
  header .sub{color:var(--muted);font-size:13px;margin-top:2px}
  .card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:18px;margin-bottom:18px}
  .card h2{font-size:15px;font-weight:600;margin-bottom:14px;color:#cfe0ff}
  .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:14px}
  .field label{display:block;font-size:12px;color:var(--muted);margin-bottom:6px}
  .field .ctl{display:flex;align-items:center;gap:8px}
  input[type=text]{width:100%;background:var(--panel2);border:1px solid var(--line);color:var(--txt);border-radius:8px;padding:9px 10px;font-size:14px;outline:none}
  input[type=text]:focus{border-color:var(--accent)}
  .switch{position:relative;width:44px;height:24px;flex:none}
  .switch input{opacity:0;width:0;height:0}
  .slider{position:absolute;cursor:pointer;inset:0;background:#3a4460;border-radius:24px;transition:.2s}
  .slider:before{content:"";position:absolute;height:18px;width:18px;left:3px;top:3px;background:#fff;border-radius:50%;transition:.2s}
  .switch input:checked+.slider{background:var(--accent)}
  .switch input:checked+.slider:before{transform:translateX(20px)}
  .lbl{font-size:14px}
  table{width:100%;border-collapse:collapse}
  th{font-size:12px;color:var(--muted);text-align:left;padding:8px 10px;border-bottom:1px solid var(--line);font-weight:600;white-space:nowrap}
  td{padding:12px 10px;border-bottom:1px solid var(--line);font-size:14px;vertical-align:middle}
  tr:last-child td{border-bottom:none}
  .badge{display:inline-block;padding:2px 8px;border-radius:20px;font-size:11px;font-weight:600}
  .b-usb{background:#123a52;color:#5fd0ff}
  .b-sata{background:#33314d;color:#b39dff}
  .b-ok{background:#123c28;color:#4be39a}
  .b-no{background:#3a3f4d;color:#aab2c4}
  .b-err{background:#4a1f1f;color:#ff7a7a}
  .btn{background:var(--panel2);border:1px solid var(--line);color:var(--txt);padding:7px 14px;border-radius:8px;font-size:13px;cursor:pointer;transition:.15s;white-space:nowrap}
  .btn:hover{background:#27314f;border-color:#354a7a}
  .btn.primary{background:var(--accent);border-color:var(--accent);color:#fff}
  .btn.danger{border-color:#5c2a2a;color:#ff9c9c}
  .btn:disabled{opacity:.5;cursor:not-allowed}
  .mono{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:13px}
  .muted{color:var(--muted)}
  .actions{display:flex;gap:8px;justify-content:flex-end}
  .toolbar{display:flex;justify-content:space-between;align-items:center;margin-bottom:14px;gap:12px;flex-wrap:wrap}
  .toolbar .right{display:flex;gap:10px;align-items:center}
  .dot{width:8px;height:8px;border-radius:50%;background:var(--ok);display:inline-block;margin-right:6px}
  .toast{position:fixed;bottom:22px;right:22px;background:var(--panel2);border:1px solid var(--line);color:var(--txt);padding:12px 18px;border-radius:10px;font-size:14px;opacity:0;transition:.25s;transform:translateY(8px);z-index:99;max-width:70vw}
  .toast.show{opacity:1;transform:translateY(0)}
  .toast.ok{border-color:#1e6b42;color:#8cf0b8}
  .toast.err{border-color:#8a2f2f;color:#ffb3b3}
  .empty{color:var(--muted);text-align:center;padding:26px 0}
  .foot{color:var(--muted);font-size:12px;text-align:center;margin-top:8px}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div class="logo">💿</div>
    <div>
      <h1>DVD光驱助手</h1>
      <div class="sub">USB DVD / 光驱 自动识别 · 自动挂载 · 管理窗口</div>
    </div>
  </header>

  <div class="card">
    <div class="toolbar">
      <h2 style="margin:0">自动挂载设置</h2>
      <div class="right">
        <button class="btn" id="btnRefresh">刷新</button>
      </div>
    </div>
    <div class="grid">
      <div class="field">
        <label>自动挂载</label>
        <div class="ctl">
          <span class="switch"><input type="checkbox" id="autoMount"><span class="slider"></span></span>
          <span class="lbl">插入光盘后自动挂载</span>
        </div>
      </div>
      <div class="field">
        <label>仅 USB 光驱</label>
        <div class="ctl">
          <span class="switch"><input type="checkbox" id="usbOnly"><span class="slider"></span></span>
          <span class="lbl">仅自动挂载 USB 接口光驱</span>
        </div>
      </div>
      <div class="field">
        <label>只读挂载</label>
        <div class="ctl">
          <span class="switch"><input type="checkbox" id="readonly"><span class="slider"></span></span>
          <span class="lbl">光盘以只读方式挂载（推荐）</span>
        </div>
      </div>
      <div class="field">
        <label>挂载基目录</label>
        <input type="text" id="mountBase" spellcheck="false">
      </div>
    </div>
    <div style="margin-top:14px;text-align:right">
      <button class="btn primary" id="btnSave">保存配置</button>
    </div>
  </div>

  <div class="card">
    <h2><span class="dot" id="statusDot"></span>光驱设备列表</h2>
    <div id="devTableWrap">
      <div class="empty">正在加载…</div>
    </div>
  </div>

  <div class="foot">DVD光驱助手 · 飞牛 fnOS 第三方应用</div>
</div>

<div class="toast" id="toast"></div>

<script>
const $=s=>document.querySelector(s);
let cfg={auto_mount:true,usb_only:true,readonly:true,mount_base:"/media/dvd"};
let busyDev=new Set();
let toastTimer=null;

function toast(msg,type){
  const t=$("#toast");
  t.textContent=msg;
  t.className="toast show "+(type||"");
  clearTimeout(toastTimer);
  toastTimer=setTimeout(()=>t.className="toast",2600);
}
function badge(txt,cls){return `<span class="badge ${cls}">${txt}</span>`;}
function esc(s){return String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;"}[c]));}

async function load(){
  try{
    const r=await fetch("/api/devices");
    const data=await r.json();
    if(!data.ok) throw new Error(data.error||"接口错误");
    cfg=data.config;
    applyCfg();
    render(data.devices);
  }catch(e){
    $("#devTableWrap").innerHTML=`<div class="empty">加载失败：${esc(e.message)}</div>`;
  }
}
function applyCfg(){
  $("#autoMount").checked=!!cfg.auto_mount;
  $("#usbOnly").checked=!!cfg.usb_only;
  $("#readonly").checked=!!cfg.readonly;
  $("#mountBase").value=cfg.mount_base||"/media/dvd";
}
function fmtSize(b){
  if(!b) return "—";
  const u=["B","KB","MB","GB","TB"];
  let i=0;let v=b;
  while(v>=1024&&i<u.length-1){v/=1024;i++;}
  return v.toFixed(1)+" "+u[i];
}
function render(devices){
  const dot=$("#statusDot");
  dot.style.background=devices.length?"var(--ok)":"var(--muted)";
  const wrap=$("#devTableWrap");
  if(!devices.length){
    wrap.innerHTML=`<div class="empty">未检测到光驱设备。请连接 USB DVD 光驱。</div>`;
    return;
  }
  const rows=devices.map(d=>{
    const trans=d.is_usb?badge("USB",'b-usb'):badge((d.transport||'SATA').toUpperCase(),'b-sata');
    const media=d.has_media?badge("有光盘",'b-ok'):badge("无光盘",'b-no');
    const st=d.mounted
      ? badge("已挂载",'b-ok')
      : (d.last_error?badge("错误",'b-err'):badge("未挂载",'b-no'));
    const err=d.last_error?`<div class="muted mono" style="font-size:12px">${esc(d.last_error)}</div>`:"";
    const btn=(d.mounted
      ?`<button class="btn" onclick="act('${d.dev}','unmount')">卸载</button>`
      :`<button class="btn primary" onclick="act('${d.dev}','mount')" ${d.has_media?"":"disabled"}>挂载</button>`)
      +`<button class="btn" onclick="act('${d.dev}','eject')">弹出</button>`;
    const ops=d.mounted?`<span class="mono muted">${esc(d.mountpoint||"")}</span>`:(d.has_media?`${esc(d.fs||"未知")}`:`—`);
    return `<tr>
      <td class="mono">${d.dev}</td>
      <td>${trans}</td>
      <td>${esc(d.model)}</td>
      <td>${media} ${d.has_media?fmtSize(d.size_bytes):""}</td>
      <td>${esc(d.fs||"—")}</td>
      <td>${ops}${err}</td>
      <td>${st}</td>
      <td><div class="actions">${btn}</div></td>
    </tr>`;
  }).join("");
  wrap.innerHTML=`<table><thead><tr>
    <th>设备</th><th>接口</th><th>型号</th><th>介质</th><th>文件系统</th><th>挂载点</th><th>状态</th><th style="text-align:right">操作</th>
  </tr></thead><tbody>${rows}</tbody></table>`;
}

async function act(dev,action){
  if(busyDev.has(dev)) return;
  busyDev.add(dev);
  try{
    const r=await fetch("/api/action",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({dev,action})});
    const data=await r.json();
    toast(data.message||"完成",data.ok?"ok":"err");
  }catch(e){ toast("请求失败："+e.message,"err"); }
  finally{ busyDev.delete(dev); load(); }
}

$("#btnSave").addEventListener("click",async()=>{
  const body={auto_mount:$("#autoMount").checked,usb_only:$("#usbOnly").checked,
    readonly:$("#readonly").checked,mount_base:$("#mountBase").value.trim()||"/media/dvd"};
  try{
    const r=await fetch("/api/config",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
    const data=await r.json();
    toast(data.message||"已保存","ok");
  }catch(e){ toast("保存失败："+e.message,"err"); }
  load();
});
$("#btnRefresh").addEventListener("click",()=>{ load(); });

load();
setInterval(load,3000);
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# CLI 入口
# ---------------------------------------------------------------------------
def write_pid(pid_file):
    if pid_file:
        try:
            d = os.path.dirname(pid_file)
            if d:
                os.makedirs(d, exist_ok=True)
            with open(pid_file, "w", encoding="utf-8") as f:
                f.write(str(os.getpid()))
        except Exception:
            pass


def udev_signal(var_dir, action, dev):
    """udev 事件通知：在 var-dir 写入触发标记，并做一次即时扫描"""
    try:
        os.makedirs(var_dir, exist_ok=True)
        trigger = os.path.join(var_dir, "udev.trigger")
        with open(trigger, "w", encoding="utf-8") as f:
            f.write(f"{time.time()} {action} {dev}\n")
    except Exception:
        pass
    return 0


def cmd_daemon(args, config, var_dir):
    write_pid(args.pid_file)
    set_log_file(os.path.join(var_dir, "dvdmgr.log"))

    _log(f"守护进程启动 pid={os.getpid()} euid={os.geteuid()} "
         f"port={config.get('port')} mount_base={config.get('mount_base')}")

    mgr = DvdManager(config, var_dir)
    stop_event = threading.Event()

    def _shutdown(signum, frame):
        stop_event.set()
        # 优雅停止：卸载由本应用挂载的光驱
        try:
            unmount_all()
        except Exception:
            pass

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    # HTTP 服务
    port = int(config.get("port") or DEFAULT_PORT)
    server = ThreadingHTTPServer(("0.0.0.0", port), DvdHandler)
    server.manager = mgr
    http_thread = threading.Thread(target=server.serve_forever, daemon=True)
    http_thread.start()

    # 监测线程
    monitor = threading.Thread(target=mgr.run_forever, args=(stop_event,),
                               daemon=True)
    monitor.start()

    # 主线程等待停止信号
    while not stop_event.is_set():
        stop_event.wait(1)

    server.shutdown()
    try:
        if args.pid_file and os.path.exists(args.pid_file):
            os.unlink(args.pid_file)
    except Exception:
        pass
    return 0


def main():
    ap = argparse.ArgumentParser(prog="dvdmgr", description="DVD光驱助手")
    ap.add_argument("--daemon", action="store_true", help="以后台守护方式运行")
    ap.add_argument("--pid-file", default=os.environ.get("TRIM_PKGVAR", "/tmp/dvdmgr.pid"))
    ap.add_argument("--var-dir", default=os.environ.get("TRIM_PKGVAR", "/tmp/dvdmgr-var"))
    ap.add_argument("--etc-dir", default=os.environ.get("TRIM_PKGETC", "/tmp/dvdmgr-etc"))
    ap.add_argument("--udev", nargs=2, metavar=("ACTION", "DEV"),
                    help="udev 事件通知")
    ap.add_argument("--scan", action="store_true", help="输出设备列表")
    ap.add_argument("--unmount-all", action="store_true", help="卸载全部由本应用挂载的光驱")
    ap.add_argument("--mount", metavar="DEV", help="挂载指定光驱")
    ap.add_argument("--unmount", metavar="DEV", help="卸载指定光驱")
    ap.add_argument("--eject", metavar="DEV", help="弹出指定光驱")
    args = ap.parse_args()

    config = Config(args.etc_dir)

    # udev 通知：尽量精简、快速返回
    if args.udev:
        return udev_signal(args.var_dir, args.udev[0], args.udev[1])

    # 单次操作
    if args.unmount_all:
        unmount_all()
        print("ok")
        return 0

    if args.mount:
        dev = args.mount
        d = next((x for x in list_optical_devices() if x["dev"] == dev), None)
        if not d:
            print(f"未找到光驱 {dev}")
            return 1
        ok, msg, _ = do_mount(dev, d["fs"], config.get("mount_base"),
                              config.get("readonly"))
        print(msg)
        return 0 if ok else 1

    if args.unmount:
        ok, msg = do_unmount(args.unmount)
        print(msg)
        return 0 if ok else 1

    if args.eject:
        ok, msg = do_eject(args.eject)
        print(msg)
        return 0 if ok else 1

    # 一次性扫描
    if args.scan or not args.daemon:
        mgr = DvdManager(config, args.var_dir)
        views = mgr.snapshot()
        print(json.dumps({"ok": True, "devices": views}, ensure_ascii=False,
                         indent=2))
        return 0

    # 守护
    return cmd_daemon(args, config, args.var_dir)


if __name__ == "__main__":
    sys.exit(main())
