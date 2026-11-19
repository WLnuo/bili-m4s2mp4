#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
bili_m4s2mp4.py —— B站缓存视频批量合并工具

把B站客户端缓存下来的 DASH 分离流（video.m4s + audio.m4s）无损合成 mp4，
不转码、不损画质、不删源文件，并可选同步弹幕。

只依赖 Python 3.7+ 标准库和系统里的 ffmpeg。

常用命令
    python bili_m4s2mp4.py --dry-run          # 预览，不写任何文件（建议先跑）
    python bili_m4s2mp4.py                    # 正式合并
    python bili_m4s2mp4.py --root "D:/cache"  # 指定缓存根目录
    python bili_m4s2mp4.py --list             # 只看发现哪些视频

参数说明见 README.md，或 python bili_m4s2mp4.py --help

退出码
    0 = 全部成功
    1 = 有失败/空间不足项
    2 = 环境不完整（缺 ffmpeg）或根目录不存在
    3 = 未找到任何缓存目录
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys

__version__ = "1.0.0"

ILLEGAL = re.compile(r'[\\/:*?"<>|\r\n\t]')
MAX_NAME = 150                  # Windows 单文件名建议上限
MAX_DIRNAME = 80                # 合集子文件夹名上限（留出 mp4 文件名空间）
DURATION_TOLERANCE = 3.0        # 秒
SPACE_MARGIN = 1.05             # 空间预留系数（muxing 开销）
MIN_VIDEO_SIZE = 10 * 1024      # video.m4s 最小合理体积；低于此值视为损坏/占位文件
MIN_AUDIO_SIZE = 1024           # audio.m4s 低于 1KB 视为占位，按无音轨处理
CHUNK = 1 << 20                 # 弹幕条数统计的读块大小（1MB）
NEEDLE = "<d p="                # 弹幕条目起始标记，用于统计条数
MAX_PATH_HINT = 255             # Windows 传统 MAX_PATH 上限，超过只告警不中断
WIN_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


# ---------------------------------------------------------------- 基础工具

def sanitize(name):
    """
    清理文件名：替换 Windows 非法字符、去首尾空格和点、规避保留设备名、限制长度。
    注意：Windows 下 "结尾的点" 和 "保留名" 都会导致创建失败，必须处理。
    """
    name = ILLEGAL.sub("_", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    if len(name) > MAX_NAME:
        name = name[:MAX_NAME].rstrip(" .")
    if not name:
        return "unnamed"
    # CON.mp4 / NUL.mp4 之类在 Windows 上无法创建
    if name.split(".")[0].upper() in WIN_RESERVED:
        name = "_" + name
    return name


def human_size(num):
    """字节数转人类可读。"""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(num) < 1024 or unit == "TB":
            return f"{num:.1f} {unit}" if unit != "B" else f"{num:.0f} {unit}"
        num /= 1024


def find_tools():
    """
    定位 ffmpeg / ffprobe。
    优先看 PATH，其次尝试 ffmpeg 同目录下的 ffprobe（常见于便携版）。
    返回 (ffmpeg, ffprobe 或 None)。
    缺 ffmpeg 直接抛 RuntimeError，由 main 统一转成退出码 2。
    """
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("未找到 ffmpeg，请确认已加入 PATH")

    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        # 便携版常把两者放在一起，PATH 只配了 ffmpeg
        d = os.path.dirname(ffmpeg)
        for cand in ("ffprobe.exe", "ffprobe"):
            guess = os.path.join(d, cand)
            if os.path.isfile(guess):
                ffprobe = guess
                break
    if not ffprobe:
        print("[警告] 未找到 ffprobe，将跳过合并后的时长校验（不影响合并结果）")
    return ffmpeg, ffprobe


def load_entry(path):
    """读 entry.json，失败返回 None。"""
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"  [跳过] entry.json 解析失败: {os.path.basename(os.path.dirname(path))} ({e})")
        return None


def build_name(entry, mode):
    """按命名模式从 entry.json 生成文件名（已 sanitize）。"""
    title = (entry.get("title") or "").strip()
    pd = entry.get("page_data") or {}
    part = (pd.get("part") or "").strip()
    page = pd.get("page")

    if mode == "title":
        raw = title or part
    elif mode == "part":
        raw = part or title
    else:  # both
        if part:
            raw = f"P{int(page):02d} {part}" if isinstance(page, int) else part
        else:
            raw = title
    return sanitize(raw)


def build_dir_name(entry, fallback):
    """
    生成子文件夹名：优先用 entry.json 的 title（合集里所有分段 title 相同）。
    title 缺失时退回视频目录名（avid），再兜底 "合集"。
    """
    title = (entry.get("title") or "").strip() if entry else ""
    if title:
        name = sanitize(title)
    else:
        fb = (fallback or "").strip()
        # 注意不能直接 sanitize(fallback)：空串会被 sanitize 成 "unnamed"
        name = sanitize(fb) if fb else ""
    if len(name) > MAX_DIRNAME:
        name = name[:MAX_DIRNAME].rstrip(" .")
    return name or "合集"


def candidate_subdir(topdir, top_name, segs):
    """
    只计算"如果建子文件夹，会叫什么/在哪"，不创建目录。
    用途：即使当前 --subdir 是 off/auto 而走了平铺，也要把这个可能已存在的
    子文件夹作为备选来源去探测，否则 --move 会找不到已有成品而重复合并。
    """
    if not segs:
        return None
    first = segs[0].entry if segs else None
    return os.path.join(topdir, build_dir_name(first, top_name))


def pick_collection_dir(topdir, top_name, segs, subdir, dry_run):
    """
    决定 --out top 时的实际输出目录。
    返回 (输出目录, 子文件夹名 或 None, 是否已存在)。

    默认 subdir=on：不论单集还是合集都建 <视频标题> 子文件夹
    （单集时子文件夹名与视频名相同，形成 230542581\\标题\\标题.mp4）。
    subdir=auto 保持"多分段才建"，subdir=off 完全平铺。
    目录已存在就复用（重跑时能找到上次的成品，不会另开 "(2)" 重复合并）。
    """
    if subdir == "off" or not segs:
        return topdir, None, True
    if subdir == "auto" and len(segs) < 2:
        return topdir, None, True

    first = segs[0].entry if segs else None
    sub = build_dir_name(first, top_name)
    target = os.path.join(topdir, sub)
    existed = os.path.isdir(target)      # 必须在 makedirs 之前判断
    if not dry_run:
        try:
            os.makedirs(target, exist_ok=True)
        except OSError as e:
            print(f"  [警告] 无法创建子文件夹 {sub}: {e}，改为平铺输出")
            return topdir, None, True
    return target, sub, existed


def probe_duration(ffprobe, path):
    """
    返回视频时长（秒）。
    ffprobe 不可用或解析失败返回 None（区别于"时长不符"）。
    调用方必须把 None 当作"无法校验"而不是"校验失败"，否则会误删成品。
    """
    if not ffprobe:
        return None
    try:
        r = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", path],
            capture_output=True, text=True, timeout=120,
            encoding="utf-8", errors="replace",
        )
        txt = r.stdout.strip()
        return float(txt) if txt else None
    except Exception:
        return None


# ---------------------------------------------------------------- 弹幕

def check_danmaku_xml(path):
    """
    校验是否为真正的B站弹幕 XML。
    下载失败时缓存里可能留下 HTML 错误页（"出错啦!- bilibili.com"），必须拦住。
    返回 (状态, 条数)，状态 in {"ok", "empty", "invalid", "missing"}

    头部读 2KB 判格式，条数按 1MB 分块流式统计 —— 大弹幕文件（几十万条）也不会
    整个读进内存。
    """
    if not os.path.isfile(path):
        return "missing", 0
    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            head = f.read(2048)
            if not head.lstrip().lower().startswith("<?xml"):
                return "invalid", 0
            if "<i>" not in head and "<i " not in head:
                return "invalid", 0
            if "chatserver" not in head:
                return "invalid", 0

            # 分块统计 "<d p=" 出现次数。
            # carry 只保留上一块末尾 NEEDLE_LEN-1 个字符：这样跨块被切断的标签
            # 能在下一块拼回来，而任何完整标签都不可能落在 carry 内（长度不够），
            # 因此不会重复计数。
            n = head.count(NEEDLE)
            carry = head[-(len(NEEDLE) - 1):]
            while True:
                chunk = f.read(CHUNK)
                if not chunk:
                    break
                window = carry + chunk
                n += window.count(NEEDLE)
                carry = window[-(len(NEEDLE) - 1):]
        return ("ok" if n > 0 else "empty"), n
    except Exception:
        return "invalid", 0


def sync_danmaku(cpath, out_dir, base_name, mode, dry_run):
    """
    复制弹幕到 mp4 旁边，重命名为 <视频名>.<ext>。
    返回状态: ok / exists / preview / none / empty / invalid / fail
    """
    if mode == "off":
        return None

    wanted = [("danmaku.xml", ".xml")]
    if mode == "both":
        wanted.append(("danmaku.pb", ".pb"))

    result = "ok"
    for src_name, ext in wanted:
        src = os.path.join(cpath, src_name)
        dst = os.path.join(out_dir, base_name + ext)

        if not os.path.isfile(src):
            if ext == ".xml":
                print(f"  [无弹幕] {base_name}{ext}")
                return "none"
            continue                       # pb 缺失不算问题

        if ext == ".xml":
            st, n = check_danmaku_xml(src)
            if st == "invalid":
                print(f"  [弹幕无效] {base_name}{ext}  (源文件是HTML错误页，非真弹幕，已跳过)")
                if result == "ok":
                    result = "invalid"
                continue
        else:
            st, n = "ok", 0

        # 目标已存在且大小一致 → 认为已同步
        if os.path.isfile(dst) and os.path.getsize(dst) == os.path.getsize(src):
            if result == "ok":
                result = "exists"
            continue

        if dry_run:
            if st == "empty":
                print(f"  [弹幕为空] {base_name}{ext}")
                if result == "ok":
                    result = "empty"
            else:
                label = f" ({n} 条)" if ext == ".xml" else ""
                print(f"  [弹幕预览] {base_name}{ext}{label}")
                if result == "ok":
                    result = "preview"
            continue

        try:
            shutil.copyfile(src, dst)
        except Exception as e:
            print(f"  [弹幕失败] {base_name}{ext}  {e}")
            return "fail"

        if ext == ".xml":
            if n == 0:
                print(f"  [弹幕为空] {base_name}{ext}  (已复制，该视频在B站本身无弹幕)")
                if result == "ok":
                    result = "empty"
            else:
                print(f"  [弹幕完成] {base_name}{ext}  ({n} 条)")
    return result


# ---------------------------------------------------------------- 缓存扫描

class Segment:
    """一个 c_xxx 分段目录的解析结果。"""

    __slots__ = ("cid", "cpath", "entry", "src_dir", "expect_sec")

    def __init__(self, cid, cpath, entry, src_dir, expect_sec):
        self.cid = cid
        self.cpath = cpath
        self.entry = entry
        self.src_dir = src_dir
        self.expect_sec = expect_sec


def pick_source_dir(cpath):
    """
    在分段目录里找出含 video.m4s 的清晰度目录。
    清晰度目录名是数字（80=1080P, 64=720P, 32=480P），必须取最大的，
    而不是 sorted() 的第一个 —— 否则会挑到最低画质。
    也兼容 video.m4s 直接放在 c_xxx 下的情况。
    返回 (路径, 错误原因)，成功时原因为 None。
    """
    candidates = []
    try:
        for sub in os.listdir(cpath):
            subp = os.path.join(cpath, sub)
            if os.path.isdir(subp) and os.path.isfile(os.path.join(subp, "video.m4s")):
                digits = sub if sub.isdigit() else "0"
                candidates.append((int(digits), subp))
    except OSError:
        return None, "目录不可读"

    if candidates:
        candidates.sort(key=lambda x: x[0], reverse=True)
        picked = candidates[0][1]
    elif os.path.isfile(os.path.join(cpath, "video.m4s")):
        picked = cpath
    else:
        return None, "未找到 video.m4s"

    # 体积校验：占位/损坏文件合并出来是坏片，不如不做
    try:
        size = os.path.getsize(os.path.join(picked, "video.m4s"))
    except OSError:
        return None, "video.m4s 不可读"
    if size < MIN_VIDEO_SIZE:
        return None, f"video.m4s 仅 {human_size(size)}，疑似损坏或未下载完"
    return picked, None


def scan_video_dirs(root):
    """
    扫描缓存根目录，返回 [(名称, 路径), ...]。
    自适应两种布局：
        root/116236176392460/c_xxx/...   （总目录）
        root/c_xxx/...                    （直接指向单个视频目录）
    """
    def cdirs(d):
        try:
            return sorted(x for x in os.listdir(d)
                          if x.startswith("c_") and os.path.isdir(os.path.join(d, x)))
        except OSError:
            return []

    if cdirs(root):
        return [(os.path.basename(os.path.normpath(root)) or root, root)]

    out = []
    try:
        for t in sorted(os.listdir(root)):
            p = os.path.join(root, t)
            if os.path.isdir(p) and cdirs(p):
                out.append((t, p))
    except OSError:
        pass
    return out


def iter_segments(topdir):
    """依次产出 topdir 下可用的 Segment（按集数 page 排序，合集才是课程顺序）。"""
    def cdirs():
        try:
            return sorted(x for x in os.listdir(topdir)
                          if x.startswith("c_") and os.path.isdir(os.path.join(topdir, x)))
        except OSError:
            return []

    # 先粗读每个分段的 page，避免按 cid 字符串排序导致的 01,02,04,03 错序
    def page_of(cd):
        try:
            with open(os.path.join(topdir, cd, "entry.json"), encoding="utf-8") as f:
                pd = (json.load(f).get("page_data") or {})
            p = pd.get("page")
            return (int(p) if isinstance(p, int) else 10 ** 6, cd)
        except Exception:
            return (10 ** 6, cd)

    for cd in sorted(cdirs(), key=page_of):
        cpath = os.path.join(topdir, cd)
        entry_path = os.path.join(cpath, "entry.json")

        if not os.path.isfile(entry_path):
            print(f"  [跳过] {cd}: 无 entry.json")
            yield None
            continue
        entry = load_entry(entry_path)
        if entry is None:
            yield None
            continue
        # 下载未完成的缓存不值得合并
        if entry.get("is_completed") is False:
            print(f"  [跳过] {cd}: 下载未完成")
            yield None
            continue

        src, why = pick_source_dir(cpath)
        if src is None:
            print(f"  [跳过] {cd}: {why}")
            yield None
            continue

        yield Segment(cd, cpath, entry, src,
                      (entry.get("total_time_milli") or 0) / 1000.0)


# ---------------------------------------------------------------- 合并

class SpaceGuard:
    """
    磁盘空间守卫：累计已预留的空间，避免批量跑一半才发现不够。
    free 会随合并进行而减少，这里按"初始可用 - 已预留"估算剩余。
    """

    def __init__(self, path):
        try:
            self.free = shutil.disk_usage(path).free
        except OSError:
            self.free = 0
        self.reserved = 0

    def try_reserve(self, need):
        """成功预留返回 True，空间不足返回 False。"""
        if self.free <= 0:          # 拿不到磁盘信息就不拦
            return True
        required = need * SPACE_MARGIN
        if self.free - self.reserved < required:
            return False
        self.reserved += required
        return True

    def release(self, need):
        """预留后任务失败/跳过，把额度还回去。"""
        if self.free > 0:
            self.reserved = max(0, self.reserved - need * SPACE_MARGIN)

    def remaining(self):
        return max(0, self.free - self.reserved)


def merge_one(ffmpeg, ffprobe, seg, out_dir, alt_dirs, name, dry_run, do_move, guard):
    """合并单个分段，返回状态字符串。alt_dirs 是其他可能已有该 mp4 的目录（按顺序探测）。"""
    v = os.path.join(seg.src_dir, "video.m4s")
    a = os.path.join(seg.src_dir, "audio.m4s")
    out = os.path.join(out_dir, name + ".mp4")

    # 音轨太小说明是占位文件，当作无音轨处理，否则合并出的片没声音
    has_audio = os.path.isfile(a) and os.path.getsize(a) >= MIN_AUDIO_SIZE
    need = os.path.getsize(v) + (os.path.getsize(a) if has_audio else 0)

    # 1) 目标已存在
    if os.path.isfile(out) and os.path.getsize(out) > 0:
        print(f"  [已存在] {name}.mp4")
        return "exists"

    # 2) 另一位置已有 → 提示或搬运
    alt = None
    for d in (alt_dirs or []):
        if not d or os.path.normcase(d) == os.path.normcase(out_dir):
            continue
        cand = os.path.join(d, name + ".mp4")
        if os.path.isfile(cand) and os.path.getsize(cand) > 0:
            alt = cand
            break

    if alt:
        try:
            common = os.path.commonpath([out_dir, os.path.dirname(alt)])
            rel = os.path.relpath(alt, os.path.dirname(common))
        except ValueError:
            rel = alt

        if do_move:
            if dry_run:
                print(f"  [预览] {name}.mp4   <将移动: {rel}>")
                return "preview"
            # 跨盘 move 会退化成复制+删除，先确认
            try:
                if os.stat(alt).st_dev != os.stat(out_dir).st_dev:
                    print(f"  [跨盘移动] {name}.mp4  源与目标不在同一分区，将执行复制后删除源")
                shutil.move(alt, out)
                print(f"  [已移动] {name}.mp4   (来自 {rel})")
                return "moved"
            except Exception as e:
                print(f"  [移动失败] {name}.mp4  {e}")
                return "fail"

        if dry_run:
            print(f"  [预览] {name}.mp4   <已存在(另一位置): {rel}>")
            return "preview"
        print(f"  [在另一位置] {name}.mp4  -> {rel}   (加 --move 可搬到目标位置)")
        return "elsewhere"

    # 3) 空间检查
    if not dry_run:
        if not guard.try_reserve(need):
            print(f"  [空间不足] {name}.mp4  需要 {human_size(need)}, "
                  f"剩余可用约 {human_size(guard.remaining())}")
            return "nospace"
    elif guard.free > 0 and guard.remaining() < need * SPACE_MARGIN:
        print(f"  [空间不足] {name}.mp4  需要 {human_size(need)}, "
              f"剩余可用约 {human_size(guard.remaining())}")
        return "nospace"

    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", v]
    if has_audio:
        cmd += ["-i", a]
    cmd += ["-c", "copy", out]

    # Windows 传统 MAX_PATH 是 260；超了 ffmpeg 会报错，提前提示便于用户改 --root
    if len(out) > MAX_PATH_HINT:
        print(f"  [警告] 输出路径 {len(out)} 字符，超过 Windows 255 限制，"
              f"建议用 --subdir off 或缩短 --root")

    if dry_run:
        print(f"  [预览] {name}.mp4" + ("" if has_audio else "  (无音轨)"))
        return "preview"

    r = subprocess.run(cmd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if r.returncode != 0:
        if os.path.isfile(out):
            os.remove(out)
        guard.release(need)
        print(f"  [失败] {name}.mp4\n         {r.stderr.strip()[:300]}")
        return "fail"

    # 4) 时长校验 —— ffprobe 缺失时跳过，绝不因无法校验而删除成品
    got = probe_duration(ffprobe, out)
    if got is None:
        print(f"  [完成] {name}.mp4  ({human_size(os.path.getsize(out))}, 未校验时长)")
        return "ok"

    if seg.expect_sec > 0 and abs(got - seg.expect_sec) > DURATION_TOLERANCE:
        os.remove(out)
        guard.release(need)
        print(f"  [校验失败] {name}.mp4  期望 {seg.expect_sec:.0f}s, "
              f"实际 {got:.0f}s (已删除)")
        return "fail"

    print(f"  [完成] {name}.mp4  ({human_size(os.path.getsize(out))})")
    return "ok"


# ---------------------------------------------------------------- 主流程

def main(argv=None):
    here = os.path.dirname(os.path.abspath(__file__)) if "__file__" in globals() else os.getcwd()
    ap = argparse.ArgumentParser(
        description="B站缓存视频批量合并：把 DASH 缓存的 video.m4s + audio.m4s 无损合成 mp4",
        epilog="示例: python bili_m4s2mp4.py --root \"D:/bilibili/cache\" --dry-run",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--root", default=here,
                    help="缓存根目录，可指向总目录或单个视频目录（默认脚本所在目录）")
    ap.add_argument("--mode", choices=["title", "part", "both"], default="part",
                    help="文件命名: title=视频标题 / part=分P标题(默认) / both=带页码前缀")
    ap.add_argument("--out", choices=["top", "c"], default="top",
                    help="输出位置: top=视频目录下(默认) / c=各分段目录内")
    ap.add_argument("--danmaku", choices=["xml", "both", "off"], default="xml",
                    help="弹幕: xml=复制xml(默认) / both=xml+pb / off=不处理")
    ap.add_argument("--subdir", choices=["auto", "on", "off"], default="on",
                    help="子文件夹: on=总是建<视频标题>文件夹(默认) / "
                         "auto=仅合集建 / off=全部平铺")
    ap.add_argument("--move", action="store_true",
                    help="把另一位置的同名 mp4 搬到目标位置（同盘秒级，不重新编码）")
    ap.add_argument("--dry-run", action="store_true",
                    help="只预览将生成什么，不写任何文件、不建目录")
    ap.add_argument("--list", action="store_true",
                    help="只列出发现的缓存目录和分段数，不做检查")
    ap.add_argument("--version", action="version", version=f"bili_m4s2mp4 {__version__}")
    args = ap.parse_args(argv)

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    root = os.path.abspath(args.root)
    if not os.path.isdir(root):
        print(f"[错误] 目录不存在: {root}")
        return 2

    videos = scan_video_dirs(root)
    if not videos:
        print(f"[提示] 在 {root} 下未找到任何 c_ 缓存目录")
        print("       请确认 --root 指向B站缓存目录（含 c_xxx 子目录的那一层）")
        return 3

    if args.list:
        for name, p in videos:
            n = len([x for x in os.listdir(p)
                     if x.startswith("c_") and os.path.isdir(os.path.join(p, x))])
            print(f"{name}  ->  {p}   ({n} 个分段)")
        return 0

    try:
        ffmpeg, ffprobe = find_tools()
    except RuntimeError as e:
        print(f"[错误] {e}")
        print("       Windows: winget install Gyan.FFmpeg   或   scoop install ffmpeg")
        print("       macOS:   brew install ffmpeg")
        print("       Debian/Ubuntu: sudo apt install ffmpeg")
        return 2

    out_desc = "视频目录下 (top)" if args.out == "top" else "各分段目录内 (c)"
    guard = SpaceGuard(root)

    print(f"bili_m4s2mp4 v{__version__}")
    print(f"根目录: {root}")
    print(f"命名: {args.mode} | 输出: {out_desc} | 弹幕: {args.danmaku} | "
          f"子文件夹: {args.subdir} | "
          f"搬运: {'是' if args.move else '否'} | 预览: {'是' if args.dry_run else '否'}")
    print(f"可用空间: {human_size(guard.free)}\n")

    stats = dict(ok=0, fail=0, skip=0, exists=0, preview=0,
                 nospace=0, moved=0, elsewhere=0)
    dstats = dict(ok=0, exists=0, preview=0, none=0, empty=0, invalid=0, fail=0)
    used = {}       # 小写 (输出目录, 文件名) -> cid，Windows 大小写不敏感
    total = 0

    for top, topdir in videos:
        segs = list(iter_segments(topdir))
        valid = [s for s in segs if s]
        stats["skip"] += len(segs) - len(valid)

        # 子文件夹收纳：默认（--subdir on）单集和合集都建 <视频标题> 子文件夹
        # 用 dry_run 参数控制"只算路径不建目录"：--out c 时只把它当备选位置探测
        coll_dir, coll_name, coll_existed = pick_collection_dir(
            topdir, top, valid, args.subdir,
            dry_run=args.dry_run or args.out != "top",
        )
        # 无论当前是否收纳，都把"可能的子文件夹"纳入备选来源，
        # 这样 --subdir off --move 能把子文件夹里的成品搬出来，而不是重新合并
        probe_sub = candidate_subdir(topdir, top, valid)

        if args.out == "top":
            base_dir = coll_dir
            if coll_name:
                state = "已存在，复用" if coll_existed else "将新建"
                kind = "单集" if len(valid) < 2 else "合集"
                print(f"[{top}]  {len(valid)} 个分段({kind})  ->  {coll_dir}  ({state})")
            else:
                if args.subdir == "auto" and len(valid) < 2:
                    why = "单集，平铺"
                else:
                    why = "已关闭收纳，平铺"
                print(f"[{top}]  {len(valid)} 个分段  ->  {topdir}  ({why})")
        else:
            base_dir = topdir
            print(f"[{top}]  {len(valid)} 个分段  ->  各 c_ 目录")

        for seg in valid:
            name = build_name(seg.entry, args.mode)

            if args.out == "top":
                out_dir = base_dir
                alt_dirs = [seg.cpath, topdir, probe_sub]
            else:
                out_dir = seg.cpath
                alt_dirs = [topdir, probe_sub]
            # 去掉 None / 重复项，保持顺序
            seen = set()
            alt_dirs = [d for d in alt_dirs
                        if d and not (os.path.normcase(d) in seen
                                      or seen.add(os.path.normcase(d)))]

            # 同目录重名保护（小写化比较，兼容 Windows 大小写不敏感）
            key = (out_dir.lower(), (name + ".mp4").lower())
            if key in used:
                name = sanitize(f"{name}_{seg.cid}")
                print(f"  [重名] 自动改名 {name}.mp4（与 {used[key]} 冲突）")
            used[(out_dir.lower(), (name + ".mp4").lower())] = seg.cid

            total += 1
            res = merge_one(ffmpeg, ffprobe, seg, out_dir, alt_dirs,
                            name, args.dry_run, args.move, guard)
            stats[res] = stats.get(res, 0) + 1

            # 弹幕：独立于合并结果，mp4 已存在也会补
            if args.danmaku != "off":
                mp4_out = os.path.join(out_dir, name + ".mp4")

                if os.path.isfile(mp4_out) and os.path.getsize(mp4_out) > 0:
                    dm_dir = out_dir
                else:
                    dm_dir = next(
                        (d for d in alt_dirs
                         if d and os.path.isfile(os.path.join(d, name + ".mp4"))
                         and os.path.getsize(os.path.join(d, name + ".mp4")) > 0),
                        None,
                    )
                    if dm_dir is None and args.dry_run and res in ("preview", "elsewhere"):
                        dm_dir = out_dir

                if dm_dir:
                    st = sync_danmaku(seg.cpath, dm_dir, name, args.danmaku, args.dry_run)
                    if st:
                        dstats[st] = dstats.get(st, 0) + 1
                elif res in ("nospace", "fail"):
                    print(f"  [弹幕跳过] {name}.xml  (视频未生成)")
        print()

    # ---------------- 汇总
    print("=" * 60)
    print(f"共 {total} 个任务 | 成功 {stats['ok']} | 移动 {stats['moved']} | "
          f"预览 {stats['preview']} | 已存在 {stats['exists']}")
    print(f"在另一位置 {stats['elsewhere']} | 跳过 {stats['skip']} | "
          f"空间不足 {stats['nospace']} | 失败 {stats['fail']}")
    if stats["elsewhere"]:
        print("提示: 加 --move 可把已合并在另一位置的文件搬到当前目标位置")

    if args.danmaku != "off":
        done = dstats["ok"] + dstats["exists"]
        print(f"\n弹幕: 已就位 {done} (新增 {dstats['ok']}, 原有 {dstats['exists']}) | "
              f"预览 {dstats['preview']} | 无弹幕 {dstats['none']} | "
              f"空弹幕 {dstats['empty']}")
        if dstats["invalid"]:
            print(f"      无效弹幕 {dstats['invalid']} 个"
                  f"（源文件是HTML错误页，需回B站重新下载该分段）")
        if dstats["fail"]:
            print(f"      失败 {dstats['fail']}")

    bad = stats["fail"] or stats["nospace"] or dstats["fail"]
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
