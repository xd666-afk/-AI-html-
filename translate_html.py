# -*- coding: utf-8 -*-
"""
HTML 批量翻译核心（v2 编号对齐版 + 批次回调）

v2 关键改动：
1. 批次对齐从 <|SEP|> 分隔符改为「数字编号」对齐。
2. 行数不符时只补翻缺失的那几条，不再整批逐条重试。
3. prompt 加硬约束：显式写死 N、禁止合并/拆分/空行、保留占位符。
4. _should_skip 修复 ranges=None 边界。
5. stop_flag 兼容 threading.Event 与 callable。

v2.1 改动：
- 去掉「[翻译] 批次进度 x/y」日志刷屏，改为 on_batch(done, total) 回调。

v2.2 改动：
- on_fragment 改在「翻译阶段」按批次完成度触发（之前误放在回写阶段，导致进度条
  整个翻译过程不动、回写时瞬间跑满）。
- 映射关系：frag_done = round(frag_total * done_batches / total_batches)。
- 回写阶段不再触发 on_fragment（瞬间完成，无意义）。
"""

import os
import re
import json
import hashlib
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from bs4 import BeautifulSoup, NavigableString, Comment

# ============================== 配置区 ==============================

API_URL = "https://api.deepseek.com/chat/completions"
MODEL_NAME = "deepseek-chat"

DEFAULT_BATCH = 30
DEFAULT_WORKERS = 12
TEMPERATURE = 0.2
MAX_RETRY = 3
TIMEOUT = 120

LANGUAGES = {
    "auto": "自动检测",
    "zh-CN": "简体中文",
    "zh-TW": "繁体中文",
    "en": "英语",
    "ja": "日语",
    "ko": "韩语",
    "fr": "法语",
    "de": "德语",
    "es": "西班牙语",
    "pt": "葡萄牙语",
    "ru": "俄语",
    "it": "意大利语",
    "ar": "阿拉伯语",
    "th": "泰语",
}

SKIP_TAGS = {
    "script", "style", "code", "pre", "kbd", "samp", "var",
    "noscript", "template", "svg", "math", "iframe",
}

TRANSLATE_ATTRS = ["title", "alt", "placeholder", "aria-label", "content"]

_ITEM_RE = re.compile(r"^\s*(\d+)\s*[.、:：]\s?(.*)$")


# ============================== 工具函数 ==============================

def _log(cb, msg):
    if cb:
        try:
            cb(msg)
        except Exception:
            pass


def _md5(s: str) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest()


def _snippet(s: str, n: int = 60) -> str:
    s = s.replace("\n", " ")
    return s if len(s) <= n else s[:n] + "..."


def _is_stopped(stop_flag) -> bool:
    """兼容 threading.Event 与 callable 两种 stop_flag。"""
    if stop_flag is None:
        return False
    if callable(stop_flag):
        try:
            return bool(stop_flag())
        except Exception:
            return False
    if hasattr(stop_flag, "is_set"):
        try:
            return bool(stop_flag.is_set())
        except Exception:
            return False
    return False


def load_glossary(path):
    """术语表：每行 `原文=译文` 或 Tab 分隔。# 开头为注释。"""
    if not path or not os.path.isfile(path):
        return {}
    g = {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.rstrip("\n").rstrip("\r")
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    k, v = line.split("=", 1)
                elif "\t" in line:
                    k, v = line.split("\t", 1)
                else:
                    continue
                k = k.strip()
                v = v.strip()
                if k:
                    g[k] = v
    except Exception as e:
        print(f"[术语表] 读取失败：{e}")
    return g


# ============================== 缓存 ==============================

class Cache:
    """缓存：md5(json{s,d,t}) -> 译文。"""

    def __init__(self, cache_dir):
        self.cache_dir = cache_dir
        self._lock = threading.Lock()
        os.makedirs(cache_dir, exist_ok=True)

    def _path(self, src_lang, dst_lang, text):
        key = _md5(json.dumps(
            {"s": src_lang, "d": dst_lang, "t": text},
            ensure_ascii=False, sort_keys=True
        ))
        return os.path.join(self.cache_dir, key + ".txt")

    def get(self, src_lang, dst_lang, text):
        p = self._path(src_lang, dst_lang, text)
        if os.path.isfile(p):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    return f.read()
            except Exception:
                return None
        return None

    def set(self, src_lang, dst_lang, text, translated):
        p = self._path(src_lang, dst_lang, text)
        with self._lock:
            try:
                with open(p, "w", encoding="utf-8") as f:
                    f.write(translated)
            except Exception:
                pass


# ============================== 跳过规则 ==============================

def _should_skip(text, src_lang):
    if not text:
        return True
    s = text.strip()
    if not s:
        return True
    if re.fullmatch(r"[\d\s\W_]+", s, flags=re.UNICODE):
        return True
    if not re.search(r"[A-Za-z\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af"
                     r"\u0400-\u04ff\u0600-\u06ff\u0e00-\u0e7f]", s):
        return True
    if len(s) == 1 and not s.isalnum():
        return True
    if re.fullmatch(r"https?://\S+", s):
        return True
    if re.fullmatch(r"[\w.\-+]+@[\w.\-]+\.\w+", s):
        return True
    if re.fullmatch(r"var\(--[\w-]+\)", s):
        return True
    return False


# ============================== HTML 片段提取/回写 ==============================

def _collect_fragments_from_soup(soup, src_lang):
    frags = []
    for s in soup.find_all(string=True):
        if isinstance(s, Comment):
            continue
        parent = s.parent
        if parent is None:
            continue
        if parent.name and parent.name.lower() in SKIP_TAGS:
            continue
        txt = str(s)
        if _should_skip(txt, src_lang):
            continue
        frags.append((s, txt))

    for tag in soup.find_all(True):
        if tag.name and tag.name.lower() in SKIP_TAGS:
            continue
        for attr in TRANSLATE_ATTRS:
            if tag.has_attr(attr):
                val = tag.get(attr)
                if isinstance(val, list):
                    val = " ".join(val)
                if not isinstance(val, str):
                    continue
                if _should_skip(val, src_lang):
                    continue
                frags.append(((tag, attr), val))

    return frags


def _replace_soup_fragments(frags, translated_list):
    for (node, original), translated in zip(frags, translated_list):
        if translated is None:
            continue
        if isinstance(node, NavigableString):
            node.replace_with(NavigableString(translated))
        else:
            tag, attr = node
            tag[attr] = translated


# ============================== API 调用 ==============================

def _build_prompt(texts, src_lang, dst_lang, glossary):
    src_name = LANGUAGES.get(src_lang, src_lang)
    dst_name = LANGUAGES.get(dst_lang, dst_lang)
    n = len(texts)

    gloss_hint = ""
    if glossary:
        hits = []
        for k, v in glossary.items():
            for t in texts:
                if k in t:
                    hits.append(f"- {k} → {v}")
                    break
            if len(hits) >= 30:
                break
        if hits:
            gloss_hint = "\n必须严格遵守以下术语对照：\n" + "\n".join(hits) + "\n"

    numbered = "\n".join(f"{i+1}. {t}" for i, t in enumerate(texts))

    base = (
        f"你是专业翻译。请把下面的 {n} 条文本从 {src_name} 翻译成 {dst_name}。\n"
        f"{gloss_hint}"
        "\n严格规则：\n"
        f"1) 一共必须有且仅有 {n} 行输出，每行以 `<序号>. ` 开头（如 `1. `、`2. `），序号从 1 到 {n}；\n"
        "2) 禁止合并、拆分、增删、空行；序号必须连续且与输入一一对应；\n"
        "3) 保留原文中的 HTML 标签、占位符（如 {0}、%s、%d、$1、{{name}}）、转义字符、变量名；\n"
        "4) 不要输出解释、不要加代码块标记、不要加引号包裹；\n"
        "5) 若某条无需翻译（纯符号/URL/代码等），原样输出。\n"
        f"\n原文（共 {n} 条）：\n{numbered}\n"
    )
    return base


def _parse_numbered(raw, expected_n):
    result = [None] * expected_n
    if not raw:
        return result
    for line in raw.splitlines():
        m = _ITEM_RE.match(line)
        if not m:
            continue
        idx = int(m.group(1))
        if 1 <= idx <= expected_n:
            if result[idx - 1] is None:
                result[idx - 1] = m.group(2).rstrip()
    return result


def _call_api(api_key, prompt, stop_flag=None):
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": MODEL_NAME,
        "messages": [
            {"role": "system", "content": "你是专业翻译，严格按编号逐行输出。"},
            {"role": "user", "content": prompt},
        ],
        "temperature": TEMPERATURE,
        "stream": False,
    }
    last_err = None
    for attempt in range(1, MAX_RETRY + 1):
        if _is_stopped(stop_flag):
            raise RuntimeError("已停止")
        try:
            r = requests.post(API_URL, headers=headers, json=payload, timeout=TIMEOUT)
            if r.status_code != 200:
                last_err = f"HTTP {r.status_code}: {r.text[:200]}"
                time.sleep(1.5 * attempt)
                continue
            data = r.json()
            content = data["choices"][0]["message"]["content"]
            return content
        except Exception as e:
            last_err = str(e)
            time.sleep(1.5 * attempt)
    raise RuntimeError(f"API 调用失败：{last_err}")


# ============================== 单批翻译 ==============================

def _translate_batch(api_key, batch_texts, src_lang, dst_lang, glossary, log_cb, stop_flag):
    n = len(batch_texts)
    result = [None] * n

    prompt = _build_prompt(batch_texts, src_lang, dst_lang, glossary)
    raw = _call_api(api_key, prompt, stop_flag)
    parsed = _parse_numbered(raw, n)
    got = 0
    for i in range(n):
        if parsed[i] is not None:
            result[i] = parsed[i]
            got += 1

    if got == n:
        return result

    missing = [i for i in range(n) if result[i] is None]
    _log(log_cb, f"    缺 {len(missing)} 条，补翻")

    for _ in range(2):
        if not missing:
            break
        if _is_stopped(stop_flag):
            break
        sub_texts = [batch_texts[i] for i in missing]
        sub_prompt = _build_prompt(sub_texts, src_lang, dst_lang, glossary)
        try:
            raw2 = _call_api(api_key, sub_prompt, stop_flag)
            parsed2 = _parse_numbered(raw2, len(sub_texts))
        except Exception as e:
            _log(log_cb, f"    补翻失败：{e}")
            break
        still_missing = []
        for j, orig_i in enumerate(missing):
            if parsed2[j] is not None:
                result[orig_i] = parsed2[j]
            else:
                still_missing.append(orig_i)
        missing = still_missing
        if missing:
            _log(log_cb, f"    仍缺 {len(missing)} 条，再补")

    for i in range(n):
        if result[i] is None:
            result[i] = batch_texts[i]

    return result


# ============================== 缓存+翻译唯一片段 ==============================

def _translate_uniq(api_key, uniq_texts, src_lang, dst_lang,
                    glossary, cache, batch_size, workers,
                    log_cb, stop_flag, on_batch=None):
    """返回 {原文: 译文}。
    on_batch(done, total) 每完成一批回调一次（含失败回退的批）。
    """
    result_map = {}
    pending = []

    for t in uniq_texts:
        if _is_stopped(stop_flag):
            break
        c = cache.get(src_lang, dst_lang, t)
        if c is not None:
            result_map[t] = c
        else:
            pending.append(t)

    _log(log_cb, f"[翻译] 唯一片段 {len(uniq_texts)} 条，缓存命中 {len(result_map)}，待翻 {len(pending)}")

    if not pending:
        if on_batch:
            try:
                on_batch(1, 1)
            except Exception:
                pass
        return result_map

    batches = [pending[i:i + batch_size] for i in range(0, len(pending), batch_size)]
    total_batches = len(batches)
    _log(log_cb, f"[翻译] 分 {total_batches} 批，并发 {workers}")

    # 先推 0/total，让进度条立刻有东西显示
    if on_batch:
        try:
            on_batch(0, total_batches)
        except Exception:
            pass

    def _work(batch):
        try:
            return batch, _translate_batch(
                api_key, batch, src_lang, dst_lang, glossary, log_cb, stop_flag
            )
        except Exception as e:
            _log(log_cb, f"[翻译] 批次失败：{e}，回退原文")
            return batch, list(batch)

    done_cnt = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_work, b): b for b in batches}
        for fut in as_completed(futs):
            if _is_stopped(stop_flag):
                break
            batch, translated = fut.result()
            for src_t, dst_t in zip(batch, translated):
                result_map[src_t] = dst_t
                cache.set(src_lang, dst_lang, src_t, dst_t)
            done_cnt += 1
            # —— v2.1：不再刷日志，改为回调进度条 ——
            if on_batch:
                try:
                    on_batch(done_cnt, total_batches)
                except Exception:
                    pass

    for t in pending:
        if t not in result_map:
            result_map[t] = t

    return result_map


# ============================== 主入口 ==============================

def run_translation(input_dir, output_dir, cache_dir,
                    api_key, src_lang, dst_lang, glossary_path,
                    batch_size, workers,
                    on_file=None, on_fragment=None, on_batch=None,
                    log_cb=None, stop_flag=None):
    """主入口。

    回调签名：
        on_file(total, done, fname)          —— 文件级进度（fname 可为 ""）
        on_fragment(total, done, fname)      —— 片段级进度（三参数，总、已、当前文件）
        on_batch(done, total)                —— 批次级进度

    v2.2 行为：
      - on_fragment 在「翻译阶段」按批次完成度映射触发：done = round(frag_total * done_b / total_b)。
      - 回写阶段不再触发 on_fragment（瞬间完成，无意义）。
    """
    os.makedirs(output_dir, exist_ok=True)
    os.makedirs(cache_dir, exist_ok=True)

    glossary = load_glossary(glossary_path)
    if glossary:
        _log(log_cb, f"[术语表] 载入 {len(glossary)} 条")

    cache = Cache(cache_dir)

    files = []
    for root, _, names in os.walk(input_dir):
        for n in names:
            if n.lower().endswith((".html", ".htm")):
                files.append(os.path.join(root, n))
    files.sort()
    total_files = len(files)
    _log(log_cb, f"[扫描] 发现 {total_files} 个 HTML 文件")
    if total_files == 0:
        _log(log_cb, "[扫描] 没有可翻译文件")
        if on_file:
            on_file(0, 0, "")
        if on_fragment:
            on_fragment(0, 0, "")
        return

    file_data = []
    uniq_set = []
    uniq_seen = set()

    for idx, path in enumerate(files):
        if _is_stopped(stop_flag):
            _log(log_cb, "[停止] 用户中止（扫描阶段）")
            return
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                html = f.read()
        except Exception as e:
            _log(log_cb, f"[读取失败] {os.path.basename(path)}: {e}")
            continue

        try:
            soup = BeautifulSoup(html, "html.parser")
        except Exception as e:
            _log(log_cb, f"[解析失败] {os.path.basename(path)}: {e}")
            continue

        frags = _collect_fragments_from_soup(soup, src_lang)
        file_data.append((path, soup, frags))

        for _, txt in frags:
            if txt not in uniq_seen:
                uniq_seen.add(txt)
                uniq_set.append(txt)

        if (idx + 1) % 20 == 0 or idx + 1 == total_files:
            _log(log_cb, f"[扫描] {idx+1}/{total_files}")

    # 片段总数（含重复），后面映射进度用
    frag_total = sum(len(f[2]) for f in file_data)
    _log(log_cb, f"[去重] 片段总数 {frag_total}，唯一 {len(uniq_set)}")

    if not uniq_set:
        for path, soup, _ in file_data:
            rel = os.path.relpath(path, input_dir)
            out_path = os.path.join(output_dir, rel)
            os.makedirs(os.path.dirname(out_path), exist_ok=True)
            with open(out_path, "w", encoding="utf-8") as f:
                f.write(str(soup))
        if on_file:
            on_file(total_files, total_files, "")
        if on_fragment:
            on_fragment(frag_total, frag_total, "")
        _log(log_cb, "[完成] 无片段可翻，已复制全部文件")
        return

    # —— v2.2：把片段进度挂到批次进度上 ——
    # 每完成一批，按 done_b / total_b 线性映射到 frag_total。
    # 提前推一个 0/frag_total，让进度条一开始就显示总数。
    if on_fragment and frag_total > 0:
        try:
            on_fragment(frag_total, 0, "")
        except Exception:
            pass

    def _wrapped_on_batch(done_b, total_b):
        if on_batch:
            try:
                on_batch(done_b, total_b)
            except Exception:
                pass
        if on_fragment and frag_total > 0 and total_b > 0:
            mapped = int(round(frag_total * done_b / total_b))
            if mapped > frag_total:
                mapped = frag_total
            try:
                on_fragment(frag_total, mapped, "")
            except Exception:
                pass

    result_map = _translate_uniq(
        api_key, uniq_set, src_lang, dst_lang,
        glossary, cache, batch_size, workers,
        log_cb, stop_flag,
        on_batch=_wrapped_on_batch,
    )

    # 翻译阶段结束，明确推到 100%
    if on_fragment and frag_total > 0:
        try:
            on_fragment(frag_total, frag_total, "")
        except Exception:
            pass

    # —— 回写阶段：不再触发 on_fragment（瞬间完成，进度条已 100%）——
    for i, (path, soup, frags) in enumerate(file_data):
        if _is_stopped(stop_flag):
            _log(log_cb, "[停止] 用户中止（回写阶段）")
            return

        fname = os.path.basename(path)
        if on_file:
            on_file(total_files, i, fname)

        translated_list = []
        for _, txt in frags:
            t = result_map.get(txt, txt)
            translated_list.append(t)

        _replace_soup_fragments(frags, translated_list)

        rel = os.path.relpath(path, input_dir)
        out_path = os.path.join(output_dir, rel)
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        try:
            with open(out_path, "w", encoding="utf-8") as f:
                f.write(str(soup))
        except Exception as e:
            _log(log_cb, f"[写出失败] {fname}: {e}")

    if on_file:
        on_file(total_files, total_files, "")
    _log(log_cb, f"[完成] 共处理 {total_files} 个文件")


# ============================== 单文件翻译（可选） ==============================

def translate_single_file(src_file, dst_file, cache_dir,
                          api_key, src_lang, dst_lang, glossary_path,
                          batch_size, workers,
                          log_cb=None, stop_flag=None):
    import tempfile
    import shutil
    tmp_in = tempfile.mkdtemp(prefix="tr_in_")
    tmp_out = tempfile.mkdtemp(prefix="tr_out_")
    try:
        shutil.copy2(src_file, os.path.join(tmp_in, os.path.basename(src_file)))
        run_translation(
            tmp_in, tmp_out, cache_dir,
            api_key, src_lang, dst_lang, glossary_path,
            batch_size, workers,
            on_file=None, on_fragment=None, log_cb=log_cb,
            stop_flag=stop_flag
        )
        produced = os.path.join(tmp_out, os.path.basename(src_file))
        os.makedirs(os.path.dirname(dst_file), exist_ok=True)
        if os.path.isfile(produced):
            shutil.copy2(produced, dst_file)
    finally:
        shutil.rmtree(tmp_in, ignore_errors=True)
        shutil.rmtree(tmp_out, ignore_errors=True)


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 4:
        print("用法：python translate_html.py <input_dir> <output_dir> <api_key>")
        sys.exit(1)
    run_translation(
        sys.argv[1], sys.argv[2], os.path.join(os.path.dirname(__file__), "cache"),
        sys.argv[3],
        "auto", "zh-CN", None,
        DEFAULT_BATCH, DEFAULT_WORKERS,
        log_cb=print
    )
