#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""带模拟光驱数据的 dvdmgr 服务（仅用于界面预览/验证，无真实硬件）"""
import importlib.util
import os
import sys

BASE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app")
spec = importlib.util.spec_from_file_location("dvdmgr", os.path.join(BASE, "dvdmgr.py"))
m = importlib.util.module_from_spec(spec)
sys.modules["dvdmgr"] = m
spec.loader.exec_module(m)

MOUNTED = {}

def _lsblk():
    return [
        {"dev": "sr0", "is_usb": True, "trans": "usb", "model": "ASUS BW-16D1HT",
         "fs": "iso9660", "label": "MOVIE_2026", "size_bytes": 4680000000, "hotplug": True},
        {"dev": "sr1", "is_usb": False, "trans": "sata", "model": "HL-DT-ST DVDRAM GH24",
         "fs": "udf", "label": "BACKUP", "size_bytes": 4700000000, "hotplug": False},
    ]
m.list_optical_devices = _lsblk
m.has_media = lambda dev: True
m.is_mounted = lambda dev: MOUNTED.get(dev)
m.do_mount = lambda dev, fs, base, ro=True: (MOUNTED.update({dev: f"{base}/{dev}"}) or (True, "已挂载", f"{base}/{dev}"))
m.do_unmount = lambda dev: (MOUNTED.pop(dev, None) or (True, "已卸载"))
m.do_eject = lambda dev: (MOUNTED.pop(dev, None) or (True, "已弹出"))

ETC = "/tmp/dvdmgr-preview-etc"
VAR = "/tmp/dvdmgr-preview-var"
os.makedirs(ETC, exist_ok=True); os.makedirs(VAR, exist_ok=True)
with open(os.path.join(ETC, "config.json"), "w") as f:
    f.write('{"port":18089,"mount_base":"/media/dvd","auto_mount":true,"usb_only":true,"readonly":true}')

MOUNTED["sr0"] = "/media/dvd/sr0"  # 模拟 sr0 已挂载
cfg = m.Config(ETC)
mgr = m.DvdManager(cfg, VAR)
srv = m.ThreadingHTTPServer(("0.0.0.0", 18089), m.DvdHandler)
srv.manager = mgr
print("preview server on 18089")
srv.serve_forever()
