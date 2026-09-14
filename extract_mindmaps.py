# -*- coding: utf-8 -*-
"""XMind → data/思维导图.json

把 7 张流程思维导图（.xmind，XMind Zen 格式 = zip + content.json）拍平成层级 JSON，
供 cockpit.py 的 _load_mindmaps() 读取，风格对齐 data/ 下的其它本地数据文件。

用法:
    python extract_mindmaps.py                # 默认扫 Claw/xmind-download
    python extract_mindmaps.py <xmind目录> [输出json]
"""
import zipfile
import json
import os
import sys
import datetime

DEFAULT_SRC = r"C:\Users\11430\WorkBuddy\Claw\xmind-download"
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "思维导图.json")

# 图的展示顺序：生产主线在前，职能流程在后
ORDER = ["生产流程", "采购流程", "研发流程", "建模流程", "生产库存管理", "产品返修流程", "品宣"]


def _notes(topic):
    """抽取节点备注文本（XMind notes.plain.content 或 html）。"""
    n = topic.get("notes")
    if not n:
        return ""
    if isinstance(n, dict):
        plain = (n.get("plain") or {}).get("content")
        if plain:
            return str(plain).strip()
    return ""


def _conv(topic):
    """递归转换一个 topic → {t:标题, n:备注, c:[子节点]}；空值不落键以压缩体积。"""
    node = {"t": str(topic.get("title") or "").strip()}
    nt = _notes(topic)
    if nt:
        node["n"] = nt
    kids = (topic.get("children") or {}).get("attached") or []
    if kids:
        node["c"] = [_conv(k) for k in kids]
    return node


def _count(node):
    return 1 + sum(_count(c) for c in node.get("c", []))


def _depth(node):
    return 1 + (max(_depth(c) for c in node["c"]) if node.get("c") else 0)


def build(src_dir):
    maps = []
    for fn in sorted(os.listdir(src_dir)):
        if not fn.lower().endswith(".xmind"):
            continue
        path = os.path.join(src_dir, fn)
        try:
            z = zipfile.ZipFile(path)
            sheets = json.loads(z.read("content.json").decode("utf-8"))
        except Exception as e:
            print("  [skip] %s: %s" % (fn, e))
            continue
        name = os.path.splitext(fn)[0]
        for s in sheets:
            root = _conv(s.get("rootTopic") or {})
            if not root.get("t"):
                continue
            maps.append({
                "file": name,
                "sheet": str(s.get("title") or ""),
                "root": root,
                "nodes": _count(root),
                "depth": _depth(root),
                "mtime": datetime.datetime.fromtimestamp(
                    os.path.getmtime(path)).strftime("%Y-%m-%d"),
            })

    def rank(m):
        try:
            return ORDER.index(m["file"])
        except ValueError:
            return len(ORDER)

    maps.sort(key=rank)
    return {
        "generated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source": src_dir,
        "count": len(maps),
        "nodes_total": sum(m["nodes"] for m in maps),
        "maps": maps,
    }


def main():
    src = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_SRC
    out = sys.argv[2] if len(sys.argv) > 2 else OUT
    model = build(src)
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(model, f, ensure_ascii=False, separators=(",", ":"))
    print("[ok] 已生成 %s" % out)
    print("     %d 张导图 · 共 %d 节点 · %d 字节" % (
        model["count"], model["nodes_total"], os.path.getsize(out)))
    for m in model["maps"]:
        print("     - %-8s %3d 节点  深 %d  (%s)" % (
            m["file"], m["nodes"], m["depth"], m["mtime"]))


if __name__ == "__main__":
    main()
