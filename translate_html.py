# -*- coding: utf-8 -*-
"""
HTML 批量翻译核心引擎（DeepSeek API）

功能概览：
- 遍历目录下 .html/.htm，用 BeautifulSoup 提取文本节点与可翻译属性
- 跳过 script/style/pre/code 等标签
- 跳过无源语言字符、过短、纯数字符号的片段（省钱）
- 批内去重：同一批里重复文本只发一次 API
- 结果缓存：cache/ 目录按 md5 存，命中不花钱
- 多线程并发翻译
- 回写阶段按文件刷新进度条 0→100%

对外主入口：
    run_translation(input_dir, output_dir, api_key, ...)
"""

import os
import re
import json
import time
import hashlib
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

from bs4 import BeautifulSoup
from openai import OpenAI

# ---------------- 可调常量 ----------------
DEFAULT_BATCH = 30          # 每批片段数
DEFAULT_WORKERS = 6         # 并发线程
MIN_FRAGMENT_LEN = 2        # 短于这个长度直接跳过，不发 API

SKIP_TAGS = {"script", "style", "pre", "code", "noscript", "svg", "math"}
TRANSLATE_ATTRS = ("alt", "title", "placeholder", "aria-label")

CACHE_DIR = "cache"

# ---------------- 语言表 ----------------
# 值格式：(提示词里用的名字, 用于字符检测的脚本名元组)
LANGUAGES = {
    "俄语":       ("俄语",       ("cyrillic",)),
    "英语":       ("英语",       ("latin",)),
    "简体中文":   ("简体中文",   ("cjk",)),
    "繁体中文":   ("繁体中文",   ("cjk",)),
    "日语":       ("日语",       ("cjk", "kana")),
    "韩语":       ("韩语",       ("hangul",)),
    "德语":       ("德语",       ("latin",)),
    "法语":       ("法语",       ("latin",)),
    "西班牙语":   ("西班牙语",   ("latin",)),
    "葡萄牙语":   ("葡萄牙语",   ("latin",)),
    "意大利语":   ("意大利语",   ("latin",)),
    "阿拉伯语":   ("阿拉伯语",   ("arabic",)),
    "泰语":       ("泰语",       ("thai",)),
    "越南语":     ("越南语",     ("latin",)),
}

_CHAR_RANGES = {
    "cyrillic": [(0x0400, 0x04FF), (0x0500, 0x052F)],
    "latin":    [(0x0041, 0x005A), (0x0061, 0x007A),
                 (0x00C0, 0x024F)],
    "cjk":      [(0x4E00, 0x9FFF), (0x3400, 0x4DBF)],
    "kana":     [(0x3040, 0x309F), (0x30A0, 0x30FF)],
    "hangul":   [(0xAC00, 0xD7AF), (0x1100, 0x11FF)],
    "arabic":   [(0x0600, 0x06FF), (0x0750, 0x077F)],
    "thai":     [(0x0E00, 0x0E7F)],
}

# ---------------- 全局统计 ----------------
STATS = {
    "cache_hit": 0,
    "api_call": 0,
    "saved_chars": 0,
    "translated_chars": 0,
}
_stats_lock = threading.Lock()


# ---------------- 工具函数 ----------------
def _md5(s: str) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest()


def _in_ranges(ch: str, ranges) -> bool:
    o = ord(ch)
    for lo, hi in ranges:
        if lo <= o <= hi:
            return True
    return False


def _has_script(text: str, scripts) -> bool:
    for ch in text:
        for sc in scripts:
            if _in_ranges(ch, _CHAR_RANGES.get(sc, [])):
                return True
    return False


def _should_skip(text: str, src_scripts) -> bool:
    """是否跳过这个片段（不发 API）"""
    t = text.strip()
    if len(t) < MIN_FRAGMENT_LEN:
        return True
    # 纯数字/标点/空白
    if not re.search(r"[A-Za-z\u00C0-\u024F\u0400-\u04FF\u4E00-\u9FFF"
                     r"\u3040-\u30FF\uAC00-\uD7AF\u0600-\u06FF\u0E00-\u0E7F]", t):
        return True
    # 没有源语言字符
    if src_scripts and not _has_script(t, src_scripts):
        return True
    return False


def _system_prompt(src_lang: str, dst_lang: str) -> str:
    return (
        f"你是专业的网页本地化翻译。把用户给的 HTML 文本片段从「{src_lang}」"
        f"翻译成「{dst_lang}」。\n"
        "要求：\n"
        "1. 只输出译文本身，不要解释、不要引号、不要 Markdown。\n"
        "2. 严格保持与输入相同的行数，一行输入对应一行输出。\n"
        "3. 保留原文中的 HTML 实体、变量占位符（如 {0}、%s、{{name}}）不变。\n"
        "4. 保留原有的首尾空白与标点风格。\n"
        "5. 如果某行是地名/品牌/网址等专有名词，可保留原文。"
    )


def _cache_path(key: str) -> str:
    os.makedirs(CACHE_DIR, exist_ok=True)
    return os.path.join(CACHE_DIR, key + ".json")


def _cache_get(key: str):
    p = _cache_path(key)
    if os.path.exists(p):
        try:
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None
    return None


def _cache_set(key: str, value):
    p = _cache_path(key)
    try:
        with open(p, "w", encoding="utf-8") as f:
            json.dump(value, f, ensure_ascii=False)
    except Exception:
        pass


# ---------------- API 调用 ----------------
def _translate_uniq(client, uniq_texts, src_lang, dst_lang, model, src_scripts):
    """
    翻译一批去重后的文本。返回 dict: 原文 -> 译文
    分段数不符时递归对半拆。
    """
    result = {}
    if not uniq_texts:
        return result

    # 先查缓存
    todo = []
    for t in uniq_texts:
        key = _md5(f"{src_lang}|{dst_lang}|{model}|{t}")
        cached = _cache_get(key)
        if cached is not None:
            result[t] = cached
            with _stats_lock:
                STATS["cache_hit"] += 1
                STATS["saved_chars"] += len(t)
        else:
            todo.append((t, key))

    if not todo:
        return result

    # 组装多行文本
    lines = [t for t, _ in todo]
    payload = "\n".join(lines)

    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": _system_prompt(src_lang, dst_lang)},
                {"role": "user", "content": payload},
            ],
            temperature=1.3,
        )
        out = resp.choices[0].message.content or ""
    except Exception as e:
        # API 失败：兜底返回原文，不写缓存（避免污染）
        for t, _ in todo:
            result[t] = t
        raise e

    out_lines = out.split("\n")

    # 行数匹配
    if len(out_lines) == len(lines):
        for (t, key), tr in zip(todo, out_lines):
            result[t] = tr
            _cache_set(key, tr)
            with _stats_lock:
                STATS["api_call"] += 1
                STATS["translated_chars"] += len(t)
    else:
        # 对半拆，递归
        if len(lines) == 1:
            # 单条还错，兜底原文，不写缓存
            t, _ = todo[0]
            result[t] = t
        else:
            mid = len(todo) // 2
            left = todo[:mid]
            right = todo[mid:]
            result.update(_translate_uniq(
                client, [t for t, _ in left], src_lang, dst_lang, model, src_scripts))
            result.update(_translate_uniq(
                client, [t for t, _ in right], src_lang, dst_lang, model, src_scripts))

    return result


def _translate_batch(client, texts, src_lang, dst_lang, model, src_scripts):
    """批内去重后翻译。返回 dict: 原文 -> 译文"""
    uniq = list(dict.fromkeys(texts))
    mapping = _translate_uniq(client, uniq, src_lang, dst_lang, model, src_scripts)
    return {t: mapping.get(t, t) for t in texts}


# ---------------- HTML 处理 ----------------
def _collect_nodes(soup):
    """返回 [(node, kind)]，kind: 'text' 或 'attr'"""
    items = []
    for tag in soup.find_all(string=True):
        parent = tag.parent
        if parent is None:
            continue
        if parent.name and parent.name.lower() in SKIP_TAGS:
            continue
        items.append((tag, "text"))

    for el in soup.find_all(True):
        if el.name and el.name.lower() in SKIP_TAGS:
            continue
        for attr in TRANSLATE_ATTRS:
            if el.has_attr(attr) and isinstance(el[attr], str) and el[attr].strip():
                items.append((el, ("attr", attr)))
    return items


def _process_one_file(path, client, src_lang, dst_lang, model, batch_size, src_scripts):
    """翻译单个 HTML 文件，返回翻译后的 HTML 字符串"""
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        html = f.read()

    soup = BeautifulSoup(html, "lxml")
    nodes = _collect_nodes(soup)

    # 收集需要翻译的文本
    texts = []
    for node, kind in nodes:
        if kind == "text":
            s = str(node)
            if not _should_skip(s, src_scripts):
                texts.append(s)
        else:
            _, attr = kind
            s = node[attr]
            if not _should_skip(s, src_scripts):
                texts.append(s)

    # 分批
    result_map = {}
    for i in range(0, len(texts), batch_size):
        chunk = texts[i:i + batch_size]
        m = _translate_batch(client, chunk, src_lang, dst_lang, model, src_scripts)
        result_map.update(m)

    # 回写
    for node, kind in nodes:
        if kind == "text":
            s = str(node)
            if s in result_map:
                node.replace_with(result_map[s])
        else:
            _, attr = kind
            s = node[attr]
            if s in result_map:
                node[attr] = result_map[s]

    return str(soup)


# ---------------- 主入口 ----------------
def run_translation(
    input_dir,
    output_dir,
    api_key,
    on_progress=None,
    glossary_path=None,
    stop_flag=None,
    only_files=None,
    src_lang="俄语",
    dst_lang="简体中文",
    model="deepseek-chat",
    batch_size=DEFAULT_BATCH,
    workers=DEFAULT_WORKERS,
    base_url="https://api.deepseek.com",
    skip_existing=True,
):
    """
    批量翻译 input_dir 下的 HTML 到 output_dir。

    on_progress 回调：
      - 日志： on_progress("文本")
      - 进度： on_progress(cur, total, filename)
    """

    def log(msg):
        if on_progress:
            try:
                on_progress(msg)
            except Exception:
                pass

    def prog(cur, total, name):
        if on_progress:
            try:
                on_progress(cur, total, name)
            except Exception:
                pass

    # 重置统计
    with _stats_lock:
        for k in STATS:
            STATS[k] = 0

    os.makedirs(output_dir, exist_ok=True)
    os.makedirs(CACHE_DIR, exist_ok=True)

    # 确定源语言脚本（用于跳过无源语言字符）
    src_scripts = ()
    if src_lang in LANGUAGES:
        src_scripts = LANGUAGES[src_lang][1]
    else:
        log(f"⚠ 「{src_lang}」不在内置语言表，跳过字符检测（所有片段都发 API）")

    if glossary_path:
        log(f"术语表已配置：{glossary_path}（当前引擎暂未启用，预留接口）")

    # 收集文件
    if only_files:
        files = [f for f in only_files if os.path.isfile(f)]
    else:
        files = []
        for name in sorted(os.listdir(input_dir)):
            if name.lower().endswith((".html", ".htm")):
                files.append(os.path.join(input_dir, name))

    if not files:
        log("没有找到 HTML 文件")
        return

    total = len(files)
    log(f"共 {total} 个文件，开始翻译（{src_lang} -> {dst_lang}）")

    client = OpenAI(api_key=api_key, base_url=base_url)

    # 跳过未改动文件
    tasks = []
    for path in files:
        name = os.path.basename(path)
        out_path = os.path.join(output_dir, name)
        if skip_existing and os.path.exists(out_path):
            if os.path.getmtime(out_path) >= os.path.getmtime(path):
                log(f"跳过（未改动）：{name}")
                continue
        tasks.append((path, out_path))

    if not tasks:
        log("所有文件都已翻译且未改动，无需处理")
        prog(total, total, "完成")
        return

    log(f"实际待翻译 {len(tasks)} 个文件")

    done = 0
    done_lock = threading.Lock()
    stop = stop_flag

    def worker(path, out_path):
        nonlocal done
        if stop is not None and stop.is_set():
            return
        name = os.path.basename(path)
        try:
            log(f"开始：{name}")
            new_html = _process_one_file(
                path, client, src_lang, dst_lang, model, batch_size, src_scripts)
            with open(out_path, "w", encoding="utf-8") as f:
                f.write(new_html)
            log(f"完成：{name}")
        except Exception as e:
            log(f"失败：{name} —— {e}")
        finally:
            with done_lock:
                done += 1
                prog(done, len(tasks), name)

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = [ex.submit(worker, p, o) for p, o in tasks]
        for _ in as_completed(futures):
            pass

    # 汇总
    log("—— 统计 ——")
    log(f"缓存命中：{STATS['cache_hit']} 段")
    log(f"实际调用：{STATS['api_call']} 段")
    log(f"翻译字符：{STATS['translated_chars']}")
    log(f"节省字符：{STATS['saved_chars']}")
    log("全部结束")
