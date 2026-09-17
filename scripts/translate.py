#!/usr/bin/env python3
"""
将 GitHub 仓库中的 .md 文件翻译为中文，覆盖原文件。

策略：
1. 从 cache_file（本仓库 cache/<gitee_repo>.json）读取翻译缓存
2. 遍历 source_dir（GitHub 端）所有 .md 文件，计算内容 hash
3. hash 命中缓存  ->  直接写回缓存的翻译内容，零 API 调用
4. hash 未命中    ->  调用腾讯云机器翻译 API 翻译
5. 更新缓存，写回 cache_file；本次配额消耗写入 quota_out（供 finalize 汇总）

腾讯云 TMT API 限制：
- 单次请求最大 6000 字节（UTF-8）
- QPS 限制 5/秒
- 每月免费 500 万字符
"""
import argparse
import hashlib
import hmac
import json
import os
import re
import sys
import time
from pathlib import Path

# 单次 API 请求最大字节（留余量）
MAX_CHUNK_BYTES = 4000
# QPS 控制（秒）
API_INTERVAL = 0.25
# 腾讯云机器翻译每月免费额度：500 万字符
MONTHLY_FREE_QUOTA = 500_000
# 安全阈值：剩余低于此值停止翻译（避免超额）
QUOTA_SAFETY_THRESHOLD = 100_000

# 专有名词术语表默认值（glossary.txt 不存在时回落使用）
DEFAULT_GLOSSARY = [
    'Music Assistant Server',
    'Music Assistant',
    'Home Assistant',
]


def _load_glossary() -> list:
    """从 scripts 同级的 glossary.txt 加载术语表，文件不存在时用默认值"""
    glossary_path = Path(__file__).resolve().parent.parent / 'glossary.txt'
    terms = []
    if glossary_path.exists():
        for line in glossary_path.read_text(encoding='utf-8').splitlines():
            line = line.strip()
            if line and not line.startswith('#'):
                terms.append(line)
    return terms if terms else DEFAULT_GLOSSARY


GLOSSARY = _load_glossary()
# 预排序：长词在前，避免短词先匹配截断长词（如 "Music Assistant" 不会破坏 "Music Assistant Server"）
_GLOSSARY_SORTED = sorted(GLOSSARY, key=len, reverse=True)


def _load_translate_files() -> list:
    """从 scripts 同级的 translate-files.txt 加载要翻译的文件名列表
    文件不存在时用默认值 [README.md]
    """
    files_path = Path(__file__).resolve().parent.parent / 'translate-files.txt'
    filenames = []
    if files_path.exists():
        for line in files_path.read_text(encoding='utf-8').splitlines():
            line = line.strip()
            if line and not line.startswith('#'):
                filenames.append(line)
    return filenames if filenames else ['README.md']


TRANSLATE_FILES = _load_translate_files()
_TRANSLATE_FILES_LOWER = set(f.lower() for f in TRANSLATE_FILES)


def _load_translation_map() -> dict:
    """从 scripts 同级的 translation-map.txt 加载翻译映射表 {源词: 目标词}
    用于纠正翻译 API 的系统性误译（如 Documentation 误译为"文件"应为"文档"）
    """
    map_path = Path(__file__).resolve().parent.parent / 'translation-map.txt'
    mapping = {}
    if map_path.exists():
        for line in map_path.read_text(encoding='utf-8').splitlines():
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            if '=>' in line:
                source, target = line.split('=>', 1)
                source, target = source.strip(), target.strip()
                if source and target:
                    mapping[source] = target
    return mapping


TRANSLATION_MAP = _load_translation_map()
# 预排序：长源词在前，避免短词先匹配截断长词
_TRANSLATION_MAP_SORTED = sorted(TRANSLATION_MAP.items(), key=lambda x: len(x[0]), reverse=True)
# 预编译正则：\b 词边界防止匹配代码标识符子串（如 supervisor_add_on_repository），
# re.IGNORECASE 覆盖句首大写（如 Repository/Manager），re.escape 防止源词含正则元字符
_TRANSLATION_MAP_PATTERNS = [
    (re.compile(r'\b' + re.escape(source) + r'\b', re.IGNORECASE), target)
    for source, target in _TRANSLATION_MAP_SORTED
]


def call_deepl_api(text: str, api_key: str) -> tuple:
    """
    调用 DeepL 翻译 API
    返回 (translated_text, used_chars)
    used_chars = 输入字符数（DeepL 按输入字符计费）
    API Key 以 :fx 结尾为免费版，用 api-free.deepl.com；否则用 api.deepl.com
    """
    import requests

    if not api_key:
        raise Exception("DeepL API Key is empty. Check DEEPL_API_KEY secret in GitHub Actions.")

    if api_key.endswith(':fx'):
        endpoint = "https://api-free.deepl.com/v2/translate"
    else:
        endpoint = "https://api.deepl.com/v2/translate"

    resp = requests.post(
        endpoint,
        data={
            "text": text,
            "source_lang": "EN",
            "target_lang": "ZH",
        },
        headers={
            "Authorization": f"DeepL-Auth-Key {api_key}",
        },
        timeout=30
    )

    if resp.status_code != 200:
        raise Exception(f"DeepL API HTTP {resp.status_code}: {resp.text[:200]}")

    result = resp.json()

    if "message" in result:
        raise Exception(f"DeepL API error: {result['message']}")

    if "translations" not in result or not result["translations"]:
        raise Exception(f"Unexpected response: {result}")

    translated_text = result["translations"][0]["text"]
    used_chars = len(text)
    return translated_text, used_chars


PLACEHOLDER_RE = re.compile(r'XPLHX\d+XPLHX')
# 零宽空格，用作链接 text 与占位符的分隔符。
# 占位符 XPLHX{idx}XPLHX 以字母 X 开头/结尾（词字符），链接 text 紧挨占位符时
# \b 词边界无法匹配 text 首尾的词。插入 \u200b（非词字符）使 \b 正确匹配。
# 还原后需清理（translate_markdown 末尾 .replace(_LINK_SEP, '')）。
_LINK_SEP = '\u200b'


def _extract_protected(text: str) -> tuple:
    """
    提取 markdown 中不应被翻译的结构，替换为占位符 XPLHX{idx}XPLHX。
    保护范围：代码块、行内代码、HTML 注释、图片、专有名词、链接语法骨架、
    链接引用定义、裸 URL、HTML 标签、加粗、标题。
    链接 [text](url) 的 text 部分留给翻译，仅保护 [ 和 ](url) 语法骨架。
    返回 (替换后的文本, 占位符映射 {占位符: 原内容})。
    顺序很重要：先匹配内层结构（代码、图片、术语），再匹配外层（链接），避免嵌套冲突。
    """
    placeholders = {}
    counter = [0]

    def _make_ph(original):
        idx = counter[0]
        counter[0] += 1
        ph = f"XPLHX{idx}XPLHX"
        placeholders[ph] = original
        return ph

    def _protect(m):
        return _make_ph(m.group())

    # 1. 代码块、行内代码、HTML 注释、图片（整体保护，不翻译）
    #    引用图片 ![alt][ref] 必须先于引用链接保护，否则嵌套 [![alt][ref]][website]
    #    里内层 ] 会使步骤 3b 的 [^\]]* 无法匹配外层，导致 [website] 被翻译失效。
    text = re.sub(r'```[\s\S]*?```', _protect, text)
    text = re.sub(r'`[^`]*`', _protect, text)
    text = re.sub(r'<!--[\s\S]*?-->', _protect, text)
    text = re.sub(r'!\[[^\]]*\]\([^)]*(?:\s+"[^"]*")?\)', _protect, text)
    text = re.sub(r'!\[[^\]]*\]\[[^\]]*\]', _protect, text)

    # 2. 专有名词术语表保护（在链接拆分前，确保链接 text 里的术语也被保护不翻译）
    for term in _GLOSSARY_SORTED:
        if term in text:
            text = text.replace(term, _make_ph(term))

    # 3a. 行内链接 [text](url)：保护 [ 和 ](url) 语法骨架，text 留给翻译 API
    def _protect_link(m):
        link_text = m.group(1)
        url_part = m.group(2)
        return f'{_make_ph("[")}{link_text}{_make_ph(f"]{url_part}")}'
    text = re.sub(r'\[([^\]]*)\](\([^)]*(?:\s+"[^"]*")?\))', _protect_link, text)

    # 3b. 引用链接 [text][ref]：保护 [ 和 ][ref] 语法骨架，text 留给翻译 API
    # ref 部分必须保护不翻译，否则与引用定义 [ref]: url 不匹配导致链接失效
    def _protect_ref_link(m):
        link_text = m.group(1)
        ref_part = m.group(2)
        return f'{_make_ph("[")}{link_text}{_make_ph(f"]{ref_part}")}'
    text = re.sub(r'\[([^\]]*)\](\[[^\]]*\])', _protect_ref_link, text)

    # 4. 链接引用定义、裸 URL、HTML 标签、加粗、标题（整体保护）
    text = re.sub(r'^\[[^\]]*\]:\s*\S+(?:\s+"[^"]*")?\s*$', _protect, text, flags=re.MULTILINE)
    text = re.sub(r'https?://[^\s)\]\)]+', _protect, text)
    text = re.sub(r'<[^>]+>', _protect, text)
    text = re.sub(r'\*\*', _protect, text)
    text = re.sub(r'(?m)^#{1,6}\s', _protect, text)

    # 5. 翻译映射表保护（在 URL/引用定义/链接骨架保护之后，避免映射词替换破坏 URL）
    # 占位符 XPLHX{idx}XPLHX 以字母 X 开头（词字符），正文词紧挨占位符时 \b 无法匹配。
    # 临时在所有占位符前后插入 _LINK_SEP（零宽空格，非词字符），使 \b 能正确匹配。
    # 映射表执行后立即去掉 _LINK_SEP，避免它被翻译 API 转为空格污染链接 text。
    text = PLACEHOLDER_RE.sub(f'{_LINK_SEP}\\g<0>{_LINK_SEP}', text)
    for pattern, target in _TRANSLATION_MAP_PATTERNS:
        text = pattern.sub(lambda m: _make_ph(target), text)
    text = text.replace(_LINK_SEP, '')

    return text, placeholders


def _restore_placeholders(text: str, placeholders: dict) -> str:
    """
    将占位符替换回原始内容。
    用模糊匹配 XPLHX\\s*\\d+\\s*XPLHX 还原，容忍翻译 API 在占位符内插入空格
    （如 XPLHX26XPLHX → XPLHX 26XPLHX）。
    按 idx 降序还原：外层占位符（idx 大）先还原，其 original 可能含内层占位符
    （idx 小），内层占位符后还原，从而正确处理嵌套结构（如图片嵌套在链接里）。
    """
    if not placeholders:
        return text
    # 模糊匹配：容忍翻译 API 在 XPLHX 和数字间插入空格
    ph_fuzzy = re.compile(r'XPLHX\s*(\d+)\s*XPLHX', re.IGNORECASE)
    # 按 idx 降序排序，处理嵌套
    sorted_ph = sorted(placeholders.items(), key=lambda x: int(x[0][5:-5]), reverse=True)
    ph_by_idx = {int(ph[5:-5]): original for ph, original in sorted_ph}

    def _restore(m):
        idx = int(m.group(1))
        return ph_by_idx.get(idx, m.group())

    # 循环还原：外层占位符还原后可能含内层占位符（被翻译 API 加空格），需要再次匹配
    prev = None
    while text != prev:
        prev = text
        text = ph_fuzzy.sub(_restore, text)
    return text


def split_markdown(text: str) -> list:
    """
    按段落切分文本（含占位符），让翻译 API 看到完整句子上下文。
    占位符 XPLHX\\d+XPLHX 是不认识的文本，翻译 API 通常保留不动。
    超长文本按字节长度切分（<= MAX_CHUNK_BYTES）。
    
    之前的策略是按占位符切分，占位符之间的短文本独立翻译，
    导致翻译 API 缺乏上下文（如 " for " → " 为 "，". For the " → ".为 "）。
    改为整段送翻译 API，翻译 API 能看到完整句子，翻译质量更好。
    """
    return split_by_length(text)


def split_by_length(text: str) -> list:
    """按字节长度切分普通文本，尽量在段落边界切分"""
    if not text:
        return []
    if len(text.encode('utf-8')) <= MAX_CHUNK_BYTES:
        return [{'type': 'text', 'content': text}]

    # 按段落切分（保留分隔符）
    paragraphs = re.split(r'(\n\s*\n)', text)
    chunks = []
    current = ''

    for p in paragraphs:
        candidate = current + p
        if len(candidate.encode('utf-8')) > MAX_CHUNK_BYTES:
            if current:
                chunks.append({'type': 'text', 'content': current})
                current = ''
            # 单个段落超过限制，按行切分
            if len(p.encode('utf-8')) > MAX_CHUNK_BYTES:
                lines = p.split('\n')
                line_buf = ''
                for line in lines:
                    if len((line_buf + line + '\n').encode('utf-8')) > MAX_CHUNK_BYTES:
                        if line_buf:
                            chunks.append({'type': 'text', 'content': line_buf})
                        # 单行超长时按字节切分，避免超过 API 限制
                        line_content = line + '\n'
                        if len(line_content.encode('utf-8')) > MAX_CHUNK_BYTES:
                            encoded = line_content.encode('utf-8')
                            for j in range(0, len(encoded), MAX_CHUNK_BYTES):
                                chunk_str = encoded[j:j+MAX_CHUNK_BYTES].decode('utf-8', errors='ignore')
                                if chunk_str:
                                    chunks.append({'type': 'text', 'content': chunk_str})
                            line_buf = ''
                        else:
                            line_buf = line_content
                    else:
                        line_buf += line + '\n'
                if line_buf:
                    current = line_buf
            else:
                current = p
        else:
            current = candidate

    if current:
        chunks.append({'type': 'text', 'content': current})

    return chunks


def translate_markdown(content: str, api_key: str, quota_used: int) -> tuple:
    """
    翻译整个 markdown 文件
    返回 (translated_content, used_chars, quota_exceeded, failed_count)
    failed_count > 0 表示有 chunk 翻译失败，不应写入缓存
    """
    protected, placeholders = _extract_protected(content)
    chunks = split_markdown(protected)
    result = []
    total_used = 0
    quota_exceeded = False
    failed_count = 0

    for i, chunk in enumerate(chunks):
        if quota_exceeded:
            # 额度用尽，剩余 chunks 原样保留
            result.append(chunk['content'])
            continue

        if chunk['type'] == 'code':
            # 代码块原样保留
            result.append(chunk['content'])
        else:
            text = chunk['content']
            if not text.strip():
                result.append(text)
                continue

            # 预检：估算本次消耗（输入字符 + 预估输出）
            est_usage = len(text) * 2  # 预估输出与输入等长
            remaining = MONTHLY_FREE_QUOTA - quota_used - total_used
            if remaining < QUOTA_SAFETY_THRESHOLD + est_usage:
                print(f"    chunk {i+1}/{len(chunks)}: SKIP - quota low (used {quota_used + total_used}/{MONTHLY_FREE_QUOTA}, remaining {remaining})")
                quota_exceeded = True
                result.append(text)
                continue

            try:
                translated, used = call_deepl_api(text, api_key)
                result.append(translated)
                total_used += used
                print(f"    chunk {i+1}/{len(chunks)}: {len(text)} chars -> translated (used {used}, total {quota_used + total_used})")
            except Exception as e:
                print(f"    chunk {i+1}/{len(chunks)}: FAILED - {e}")
                failed_count += 1
                # 失败时保留原文
                result.append(text)
            # QPS 控制
            time.sleep(API_INTERVAL)

    translated = ''.join(result)
    # 中英混排加空格：在中文与英文/数字边界统一为单个空格，压缩 DeepL 产生的多余空格
    # 在占位符还原前执行，代码块/URL 等占位符区域不受影响
    translated = re.sub(r'([\u4e00-\u9fff]) *([a-zA-Z0-9])', r'\1 \2', translated)
    translated = re.sub(r'([a-zA-Z0-9]) *([\u4e00-\u9fff])', r'\1 \2', translated)
    final = _restore_placeholders(translated, placeholders)
    # 清理链接 text 与占位符之间插入的零宽空格分隔符
    final = final.replace(_LINK_SEP, '')
    # 清理翻译 API 拆开占位符产生的残留 XPLHX 片段及其周围多余空格
    final = re.sub(r'\s*XPLHX\s*', ' ', final, flags=re.IGNORECASE)
    # 修复加粗内部前后空格（** text ** → **text**）
    def _strip_bold(m):
        return f'**{m.group(1).strip()}**'
    final = re.sub(r'\*\*(.+?)\*\*', _strip_bold, final)
    # 修复加粗未闭合：按行检查 ** 是否成对，奇数个时去掉该行最后一个 **（CommonMark 加粗不跨行）
    def _fix_bold_per_line(line):
        if line.count('**') % 2 == 1:
            idx = line.rfind('**')
            return line[:idx] + line[idx + 2:]
        return line
    final = '\n'.join(_fix_bold_per_line(ln) for ln in final.split('\n'))
    # 修复标题多空格：##  关于 → ## 关于
    final = re.sub(r'(?m)^(#{1,6})\s{2,}', r'\1 ', final)
    # 修复链接 text 前后多余空格：[ text ](url) → [text](url)，[ text ][ref] → [text][ref]
    final = re.sub(r'\[\s*([^\[\]]+?)\s*\]\(', r'[\1](', final)
    final = re.sub(r'\[\s*([^\[\]]+?)\s*\]\[', r'[\1][', final)
    # 去掉中文字符之间的多余空格（映射表占位符还原后中文词间可能残留空格）
    final = re.sub(r'([\u4e00-\u9fff]) (?=[\u4e00-\u9fff])', r'\1', final)
    # 去掉中文标点前后的多余空格
    final = re.sub(r' ([，。！？；：）」】])', r'\1', final)
    final = re.sub(r'([，。！？；：（「【]) ', r'\1', final)
    return final, total_used, quota_exceeded, failed_count


def main():
    parser = argparse.ArgumentParser(description='Translate .md files to Chinese')
    parser.add_argument('--source-dir', required=True,
                        help='GitHub 仓库目录（英文原版）')
    parser.add_argument('--cache-file', required=True,
                        help='缓存文件路径（本仓库 cache/<gitee_repo>.json）')
    parser.add_argument('--quota-used', type=int, default=0,
                        help='全局已用配额（当月，由 prepare 阶段传入）')
    parser.add_argument('--quota-out',
                        help='写入本次消耗配额的文件路径（供 finalize 汇总）')
    parser.add_argument('--api-key', default=os.environ.get('DEEPL_API_KEY'),
                        help='DeepL API Key（免费版以 :fx 结尾）')
    parser.add_argument('--force', action='store_true',
                        help='强制重新翻译，忽略缓存（用于刷新历史错乱的翻译版本）')
    args = parser.parse_args()

    if args.force:
        print("::warning::--force enabled, ignoring cache and re-translating all files.")

    source_dir = Path(args.source_dir).resolve()
    cache_path = Path(args.cache_file).resolve()

    # 读取缓存
    # 结构：{rel_path: {"hash": "...", "content": "翻译后内容"}}
    if cache_path.exists():
        try:
            with open(cache_path, 'r', encoding='utf-8') as f:
                cache = json.load(f)
        except Exception:
            cache = {}
    else:
        cache = {}

    # 配额（从 prepare 传入的全局值，不再嵌在缓存里）
    current_month = time.strftime('%Y-%m')
    quota_used = args.quota_used
    quota_remaining = MONTHLY_FREE_QUOTA - quota_used

    print(f"=== Quota ({current_month}) ===")
    print(f"  Used:      {quota_used}")
    print(f"  Remaining: {quota_remaining}")
    print(f"  Free:      {MONTHLY_FREE_QUOTA}")
    print(f"  Safety:    {QUOTA_SAFETY_THRESHOLD}")
    print(f"Cache entries: {len(cache)}")

    # 全局额度检查：如果已用尽，直接跳过所有翻译
    global_quota_exceeded = (quota_remaining < QUOTA_SAFETY_THRESHOLD)
    if global_quota_exceeded:
        print(f"\n::warning::Translation quota exceeded this month. Skipping all translations. Will sync English version only.")
        print(f"  Used {quota_used}/{MONTHLY_FREE_QUOTA} chars in {current_month}.")

    # 翻译根目录和一级子目录下的目标文件（不区分大小写）
    # 不递归更深层目录，避免大仓库遍历数千个 .md 文件
    md_files = []
    for p in source_dir.glob('*.md'):
        if p.name.lower() in _TRANSLATE_FILES_LOWER:
            md_files.append(p)
    for p in source_dir.glob('*/*.md'):
        if p.name.lower() in _TRANSLATE_FILES_LOWER:
            md_files.append(p)

    print(f"Found {len(md_files)} files to translate ({', '.join(TRANSLATE_FILES)} in root + subdirs)")

    translated_count = 0
    reused_count = 0
    skipped_count = 0
    failed_count = 0
    quota_skipped_count = 0
    total_quota_used_this_run = 0

    for md_file in md_files:
        rel_path = str(md_file.relative_to(source_dir)).replace('\\', '/')

        try:
            content = md_file.read_text(encoding='utf-8')
        except Exception as e:
            print(f"  SKIP (read failed): {rel_path} - {e}")
            skipped_count += 1
            continue

        content_hash = hashlib.sha256(content.encode('utf-8')).hexdigest()

        # 检查缓存（--force 时跳过缓存，总是重新翻译）
        # 缓存结构：{rel_path: {"hash": "...", "content": "..."}}
        if not args.force:
            entry = cache.get(rel_path)
            if isinstance(entry, dict) and entry.get('hash') == content_hash:
                cached_content = entry.get('content', '')
                if cached_content:
                    md_file.write_text(cached_content, encoding='utf-8')
                    reused_count += 1
                    print(f"  REUSED: {rel_path}")
                    continue

        # 需要翻译
        # 跳过空文件
        if not content.strip():
            skipped_count += 1
            print(f"  SKIP (empty): {rel_path}")
            continue

        # 跳过已经是中文的文件（首次同步 Gitee 端已有中文版本）
        # 启发式：如果超过 30% 是中文字符，跳过
        chinese_chars = len(re.findall(r'[\u4e00-\u9fff]', content))
        total_chars = len(content)
        if total_chars > 0 and chinese_chars / total_chars > 0.3:
            # 已经是中文，更新缓存但不翻译
            cache[rel_path] = {'hash': content_hash, 'content': content}
            reused_count += 1
            print(f"  REUSED (already Chinese): {rel_path}")
            continue

        # 全局额度检查
        if global_quota_exceeded:
            # 额度已用尽，保留英文原文，不更新缓存（下次额度恢复后重新翻译）
            quota_skipped_count += 1
            print(f"  SKIP (quota exceeded): {rel_path}")
            continue

        print(f"  TRANSLATING: {rel_path} ({len(content)} chars)...")
        try:
            translated, used, quota_exceeded, chunk_failed = translate_markdown(
                content, args.api_key,
                quota_used + total_quota_used_this_run
            )
            md_file.write_text(translated, encoding='utf-8')
            total_quota_used_this_run += used

            if chunk_failed > 0:
                # 有 chunk 失败，不写入缓存，下次重新翻译
                failed_count += 1
                print(f"  FAILED: {rel_path} - {chunk_failed} chunks failed, not cached (will retry next run)")
            else:
                # 全部成功，写入缓存
                cache[rel_path] = {'hash': content_hash, 'content': translated}
                translated_count += 1
                print(f"  OK: {rel_path} - all chunks translated, cached")

            if quota_exceeded:
                # 翻译中额度用尽，标记全局
                global_quota_exceeded = True
                print(f"  -> Quota exceeded during translation. Remaining files will be skipped.")
        except Exception as e:
            print(f"  FAILED: {rel_path} - {e}")
            failed_count += 1

    # 写入缓存到 cache_file
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    with open(cache_path, 'w', encoding='utf-8') as f:
        json.dump(cache, f, indent=2, ensure_ascii=False)

    # 写入本次配额消耗（供 finalize 汇总全局配额）
    if args.quota_out:
        with open(args.quota_out, 'w', encoding='utf-8') as f:
            f.write(str(total_quota_used_this_run))

    print(f"\n=== Summary ===")
    print(f"Translated:     {translated_count}")
    print(f"Reused:         {reused_count}")
    print(f"Skipped:        {skipped_count}")
    print(f"Failed:         {failed_count}")
    print(f"Quota skipped:  {quota_skipped_count}")
    print(f"Quota used this run: {total_quota_used_this_run}")
    print(f"Quota total {current_month}: {quota_used + total_quota_used_this_run}/{MONTHLY_FREE_QUOTA}")

    if quota_skipped_count > 0:
        print(f"\n::warning::Translation quota exceeded. {quota_skipped_count} files not translated (kept English). Will retry next month.")


if __name__ == '__main__':
    main()
