# DVD光驱助手（com.fnos.dvdmgr）

飞牛 fnOS（FnOS）第三方应用：**自动识别并驱动 USB DVD 光驱**，插入 USB 光驱并放入光盘后自动挂载，并提供 Web 窗口管理界面。

> 作者：Kinyo · [github.com/kisba007/fnos-dvdmgr](https://github.com/kisba007/fnos-dvdmgr)

## 功能特性

- **自动识别**：udev 规则 + 轮询双通道，即时发现 USB 光驱（`/dev/sr*`）热插拔，区分 USB / SATA 接口。
- **自动挂载**：放入光盘后自动挂载（ISO9660 / UDF，默认只读），挂载到**「我的文件 → dvdmgr」**文件夹，打开文件管理即可看到光盘内容，体验类似 U 盘。
- **窗口化管理**：桌面图标以内嵌窗口打开，列出现有光驱（型号、接口、介质、文件系统、挂载点、状态），支持**挂载 / 卸载 / 弹出**。
- **可配置**：自动挂载开关、仅 USB 光驱、只读模式、挂载基目录，均可通过界面即时调整并持久化。
- **诊断能力**：`/api/diag` 接口 + 运行日志（`var/dvdmgr.log`），便于排查光驱识别问题。
- **无人值守**：光盘取出自动卸载；拔出光驱自动清理。

## 安装方式

`dist/com.fnos.dvdmgr-1.0.3.fpk` 为现成安装包，上传到飞牛 NAS，在**应用中心 → 手动安装**中选择即可。支持arm和x86设备。

如需自行打包（本仓库已内置 fnpack，可在任意 Linux 上执行）：

```bash
bash build.sh          # 自检 + fnpack 打包，产物在 dist/
```

> 本应用以 root 权限安装到系统分区（`install_type=root`），用于监听光驱热插拔、挂载光盘与发送弹出指令，属系统级工具。第三方应用无法提交到官方商店，仅供个人 NAS 手动安装使用。

## 使用

1. 插入 USB DVD 光驱，放入光盘。
2. 数秒内自动挂载，默认挂到**「我的文件 → dvdmgr → <设备名>」**。
   <img width="1104" height="640" alt="1702" src="https://github.com/user-attachments/assets/25f4c7a8-ed61-4f40-a065-0c67eb09fc42" />

3. 打开桌面 **DVD光驱助手** 窗口：查看光驱状态、手动挂载/卸载/弹出、调整自动挂载策略。
   <img width="1100" height="638" alt="1701" src="https://github.com/user-attachments/assets/a3dbf2c1-bc0e-41f2-a9a1-9e98fa1a313d" />


## 配置

默认配置（`/vol[x]/@appconf/com.fnos.dvdmgr/config.json`，也可在 Web 界面修改）：

```json
{
    "port": 8085,
    "mount_base": "",
    "auto_mount": true,
    "usb_only": true,
    "readonly": true
}
```

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `port` | 8085 | Web 管理端口（需与 manifest `service_port` 一致） |
| `mount_base` | 自动探测 | 挂载基目录，留空时守护进程自动挂到「我的文件 → dvdmgr」 |
| `auto_mount` | `true` | 是否自动挂载 |
| `usb_only` | `true` | 仅自动挂载 USB 接口光驱（内部 SATA 光驱仅展示，可手动挂载） |
| `readonly` | `true` | 光盘以只读方式挂载（推荐，避免写入介质损坏） |

## 诊断排障

若光驱未被检测到，访问管理窗口的诊断接口（把 `192.168.x.x` 换成 NAS IP）：

```
http://192.168.x.x:8085/api/diag
```

重点看 `dev_sr` / `sys_sr`（系统是否识别出 `/dev/sr*`）与 `dmesg_optical`（内核 USB/光驱日志）。`dev_sr` 为空通常意味着硬件/供电/驱动层面未识别（换 USB 口、换线、确认供电）。

## 技术要点

- **udev 规则**（安装时写入 `/etc/udev/rules.d/99-dvdmgr.rules`）：匹配 `sr*` 光驱，`add/change/remove` 触发 `dvdmgr.py --udev ...` 即时感知；守护进程以 2 秒周期轮询兜底。
- **设备枚举**：`lsblk -J` 读取 rom 类型设备，`TRAN=usb` 判定 USB 接口；`blockdev --getsize64` 判断托盘内是否有介质；兼容不同 lsblk 版本并做 sysfs 兜底。
- **挂载**：`mount -t auto -o ro,noatime /dev/srN <base>/srN`。
- **弹出**：优先 `eject` 命令，缺失时通过 `ioctl(CDROMEJECT)` 实现，无额外依赖。
- **安全**：Web 端口监听 `0.0.0.0:8085`（FnOS 桌面内嵌窗口所需），请勿在不可信网络暴露；建议在飞牛防火墙仅放行可信网段。
  
## 说明

- 卸载应用时，会先卸载所有由本应用挂载的光驱，并移除 udev 规则。
- 光盘介质通常为只读文件系统（ISO9660/UDF）；若为可写介质（CD-RW/DVD-RW）且关闭只读模式，请自行注意数据安全。
- 本应用为第三方个人打包，与飞牛官方无关。
