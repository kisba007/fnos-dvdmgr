#!/usr/bin/env bash
# ============================================================================
# DVD光驱助手 - FPK 打包脚本
# 依赖：飞牛 fnOS 自带的 fnpack 工具（在 fnOS 上执行）。
#
# 用法：
#   在 fnOS 上：
#     1) 将本 dvdmgr-fpk 目录上传到 NAS（如 /vol1/1000/dvdmgr-fpk）
#     2) cd dvdmgr-fpk && bash build.sh
#     产物：dist/com.fnos.dvdmgr-1.0.0.tgz.fpk
#     3) 在飞牛「应用中心 -> 手动安装」中选择该 fpk 文件
# ============================================================================
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APPNAME="$(grep -E '^appname=' "$PROJECT_DIR/manifest" | cut -d= -f2- | tr -d ' ')"
VERSION="$(grep -E '^version=' "$PROJECT_DIR/manifest" | cut -d= -f2- | tr -d ' ')"
OUT_DIR="$PROJECT_DIR/dist"

echo "== 打包前自检 =="
# 1) 必须存在的关键文件
for f in manifest config/privilege config/resource wizard/install \
         ICON.PNG ICON_256.PNG app/ui/config app/dvdmgr.py cmd/main; do
    if [ ! -e "$PROJECT_DIR/$f" ]; then
        echo "[FAIL] 缺少 $f" >&2
        exit 1
    fi
done
echo "[OK] 关键文件齐全"

# 2) shell 脚本语法检查
for s in cmd/*; do
    bash -n "$s" || { echo "[FAIL] 语法错误: $s" >&2; exit 1; }
done
echo "[OK] cmd 脚本语法通过"

# 3) JSON 合法性检查
for j in config/privilege config/resource wizard/install app/ui/config; do
    python3 -c "import json,sys; json.load(open('$PROJECT_DIR/$j'))" \
        || { echo "[FAIL] JSON 错误: $j" >&2; exit 1; }
done
echo "[OK] JSON 配置合法"

# 4) Python 语法检查（用 ast.parse，不产生 __pycache__）
python3 - "$PROJECT_DIR/app/dvdmgr.py" <<'PY' \
    || { echo "[FAIL] dvdmgr.py 语法错误" >&2; exit 1; }
import ast, sys
with open(sys.argv[1], encoding="utf-8") as f:
    ast.parse(f.read())
PY
echo "[OK] dvdmgr.py 语法通过"

# 5) 图标尺寸检查
python3 - "$PROJECT_DIR" <<'PY'
import sys
from PIL import Image
root = sys.argv[1]
ok = True
for p, exp in [("ICON.PNG", 64), ("ICON_256.PNG", 256),
               ("app/ui/images/icon-64.png", 64),
               ("app/ui/images/icon-256.png", 256)]:
    im = Image.open(f"{root}/{p}")
    if im.size != (exp, exp):
        print(f"[FAIL] {p} 应为 {exp}x{exp}，实际 {im.size[0]}x{im.size[1]}")
        ok = False
print("[OK] 图标尺寸正确" if ok else "图标尺寸错误")
sys.exit(0 if ok else 1)
PY

echo
echo "== 使用 fnpack 打包 =="
mkdir -p "$OUT_DIR"

# 优先使用本地已下载的 fnpack，其次使用系统中的 fnpack
if [ -x "$PROJECT_DIR/tools/fnpack/fnpack" ]; then
    FN_PACK="$PROJECT_DIR/tools/fnpack/fnpack"
elif command -v fnpack >/dev/null 2>&1; then
    FN_PACK="fnpack"
else
    echo "[ERROR] 未找到 fnpack。" >&2
    echo "        下载 Linux 版: https://static2.fnnas.com/fnpack/fnpack-1.2.3-linux-amd64" >&2
    echo "        放入 tools/fnpack/fnpack 后重试，或确认在飞牛 fnOS 环境下执行。" >&2
    exit 1
fi

rm -f "$OUT_DIR/"*.fpk
# 确保 app 目录无 __pycache__ 等编译缓存混入安装包
rm -rf "$PROJECT_DIR/app/__pycache__"
cd "$PROJECT_DIR"
"$FN_PACK" build -d "$PROJECT_DIR"

# 收集产物到 dist/
FOUND="$(find "$PROJECT_DIR" -maxdepth 2 -name '*.fpk' -newer "$PROJECT_DIR/manifest" 2>/dev/null | head -n 5)"
if [ -z "$FOUND" ]; then
    echo "[ERROR] 未找到打包产物，请手动检查 fnpack 输出目录。" >&2
    exit 1
fi
for f in $FOUND; do
    if [ "$(dirname "$f")" != "$OUT_DIR" ]; then
        mv -f "$f" "$OUT_DIR/"
    fi
done

# 统一重命名为 <appname>-<version>.fpk，便于识别
PKG="$(find "$OUT_DIR" -maxdepth 1 -name '*.fpk' -print -quit)"
if [ -n "$PKG" ] && [ "$(basename "$PKG")" != "${APPNAME}-${VERSION}.fpk" ]; then
    mv -f "$PKG" "$OUT_DIR/${APPNAME}-${VERSION}.fpk"
fi

echo
echo "== 完成 =="
ls -lh "$OUT_DIR/"*.fpk
