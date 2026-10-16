#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""dvdmgr 功能自测：以模拟光驱设备验证 API 与自动挂载逻辑（无需真实硬件）"""
import importlib.util
import json
import os
import sys
import threading
import urllib.request

# 加载被测试模块
BASE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app")
spec = importlib.util.spec_from_file_location("dvdmgr", os.path.join(BASE, "dvdmgr.py"))
m = importlib.util.module_from_spec(spec)
sys.modules["dvdmgr"] = m
spec.loader.exec_module(m)

# ---- 模拟底层命令 ---------------------------------------------------------
MOUNTED = {}          # dev -> mountpoint
EJECTED = set()
MOUNT_FAIL = set()

def _fake_lsblk():
    return [
        {"dev": "sr0", "is_usb": True, "trans": "usb", "model": "USB DVD Writer",
         "fs": "iso9660", "label": "MOVIE", "size_bytes": 5000000000,
         "hotplug": True},
        {"dev": "sr1", "is_usb": False, "trans": "sata", "model": "HL-DT-ST DVDRAM",
         "fs": "udf", "label": "DATA", "size_bytes": 8000000000,
         "hotplug": False},
    ]

m.list_optical_devices = _fake_lsblk
m.has_media = lambda dev: True

def _fake_is_mounted(dev):
    return MOUNTED.get(dev)

def _fake_mount(dev, fstype, base, readonly=True):
    if dev in MOUNT_FAIL:
        return False, f"模拟挂载失败 {dev}", os.path.join(base, dev)
    MOUNTED[dev] = os.path.join(base, dev)
    return True, "已挂载", MOUNTED[dev]

def _fake_unmount(dev):
    MOUNTED.pop(dev, None)
    return True, "已卸载"

def _fake_eject(dev):
    MOUNTED.pop(dev, None)
    EJECTED.add(dev)
    return True, "已弹出"

m.is_mounted = _fake_is_mounted
m.do_mount = _fake_mount
m.do_unmount = _fake_unmount
m.do_eject = _fake_eject

# ---- 启动 HTTP 服务 -------------------------------------------------------
import tempfile
etc = tempfile.mkdtemp(prefix="dvdmgr-etc-")
var = tempfile.mkdtemp(prefix="dvdmgr-var-")
cfg = m.Config(etc)
cfg.data.update({"port": 0})
cfg.save()

class Server(m.ThreadingHTTPServer):
    def __init__(self):
        super().__init__(("127.0.0.1", 0), m.DvdHandler)
        self.manager = m.DvdManager(cfg, var)

srv = Server()
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()

def api(path, payload=None):
    url = f"http://127.0.0.1:{port}{path}"
    if payload is None:
        with urllib.request.urlopen(url, timeout=5) as r:
            return r.status, json.loads(r.read().decode())
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=5) as r:
        return r.status, json.loads(r.read().decode())

passed = 0
def check(name, cond, detail=""):
    global passed
    if cond:
        passed += 1
        print(f"  [PASS] {name}")
    else:
        print(f"  [FAIL] {name}  {detail}")
        sys.exit(1)

print("== 设备列表 ==")
st, d = api("/api/devices")
check("GET /api/devices 返回 200", st == 200)
check("列出 2 台光驱", len(d["devices"]) == 2, str(d))
check("sr0 识别为 USB", d["devices"][0]["is_usb"] is True)
check("sr1 识别为 SATA", d["devices"][1]["is_usb"] is False)
check("sr0 有介质且未挂载", d["devices"][0]["has_media"] is True and d["devices"][0]["mounted"] is False)

print("== 挂载 / 卸载 / 弹出 ==")
st, d = api("/api/action", {"dev": "sr0", "action": "mount"})
check("挂载 sr0 成功", d["ok"] is True, str(d))
st, d = api("/api/devices")
check("sr0 已挂载", d["devices"][0]["mounted"] is True)
check("挂载点正确", d["devices"][0]["mountpoint"] == "/media/dvd/sr0", d["devices"][0]["mountpoint"])

st, d = api("/api/action", {"dev": "sr0", "action": "unmount"})
check("卸载 sr0 成功", d["ok"] is True)
st, d = api("/api/devices")
check("sr0 已卸载", d["devices"][0]["mounted"] is False)

st, d = api("/api/action", {"dev": "sr0", "action": "eject"})
check("弹出 sr0 成功", d["ok"] is True and "sr0" in EJECTED)

print("== 配置修改 ==")
st, d = api("/api/config", {"usb_only": False, "readonly": False, "mount_base": "/media/opt"})
check("保存配置成功", d["ok"] is True)
st, d = api("/api/devices")
check("配置已生效(mount_base)", d["config"]["mount_base"] == "/media/opt", str(d["config"]))
# 改回
api("/api/config", {"usb_only": True, "readonly": True, "mount_base": "/media/dvd"})

print("== 自动挂载轮询（usb_only=true 时仅挂载 sr0）==")
# 重置后调用 scan_once
MOUNTED.clear()
cfg.data["usb_only"] = True
mgr = srv.manager
mgr._known = {}
mgr.scan_once()
check("自动挂载 sr0(USB)", "sr0" in MOUNTED, str(MOUNTED))
check("不自动挂载 sr1(SATA)", "sr1" not in MOUNTED, str(MOUNTED))

print("== 自动挂载轮询（usb_only=false 时全部挂载）==")
MOUNTED.clear()
cfg.data["usb_only"] = False
mgr._known = {}
mgr.scan_once()
check("自动挂载 sr0", "sr0" in MOUNTED)
check("自动挂载 sr1", "sr1" in MOUNTED)

print("== 挂载失败记录 ==")
MOUNTED.clear()
MOUNT_FAIL.add("sr0")
cfg.data["usb_only"] = True
mgr._known = {}
mgr.scan_once()
st, d = api("/api/devices")
check("sr0 挂载失败且有错误信息", bool(d["devices"][0]["last_error"]), d["devices"][0]["last_error"])

srv.shutdown()
print(f"\n全部通过: {passed} 项")
