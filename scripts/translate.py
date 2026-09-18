#!/usr/bin/env python3
"""
将 GitHub 仓库中的 .md 文件翻译为中文，覆盖原文件。

职责划分：
  本脚本（translate.py）：占位符保护、段落拆分、后处理、文件遍历
  translation-service：缓存查询 + 翻译（查缓存 → 命中返回 / 未命中调 API → 更新缓存）
  postprocess.py：后处理（还原占位符 + 格式修复）

流程：
  1. 按 markdown 段落拆分
  2. 每段：extract_protected → translation_service.translate() → postprocess.run()
  3. 拼接段落，写回文件

缓存目录由 --cache-dir 指定，物理位置在 sync-gitee-hub 仓库内（cache/<repo>/）。
"""
import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path

import postprocess

DEFAULT_GLOSSARY = [
    'Music Assistant Server',
    'Music Assistant',
    'Home Assistant',
]


def _load_glossary():
    glossary_path = Path(__file__).resolve().parent.parent / 'glossary.txt'
    terms = []
    if glossary_path.exists():
        for line in glossary_path.read_text(encoding='utf-8').splitlines():
            line = line.strip()
            if line and not line.startswith('#'):
                terms.append(line)
    return terms if terms else DEFAULT_GLOSSARY


GLOSSARY = _load_glossary()
_GLOSSARY_SORTED = sorted(GLOSSARY, key=len, reverse=True)


def _load_translation_map():
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


def _load_translate_files():
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

TRANSLATION_MAP = _load_translation_map()
_TRANSLATION_MAP_SORTED = sorted(TRANSLATION_MAP.items(), key=lambda x: len(x[0]), reverse=True)
_TRANSLATION_MAP_PATTERNS = [
    (re.compile(r'\b' + re.escape(source) + r'\b', re.IGNORECASE), target)
    for source, target in _TRANSLATION_MAP_SORTED
]

PLACEHOLDER_RE = re.compile(r'XPLHX\d+XPLHX')
_LINK_SEP = '\u200b'


def extract_protected(text):
    """提取不应翻译的结构，替换为占位符 XPLHX{idx}XPLHX"""
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

    text = re.sub(r'```[\s\S]*?```', _protect, text)
    text = re.sub(r'`[^`]*`', _protect, text)
    text = re.sub(r'<!--[\s\S]*?-->', _protect, text)
    text = re.sub(r'!\[[^\]]*\]\([^)]*(?:\s+"[^"]*")?\)', _protect, text)
    text = re.sub(r'!\[[^\]]*\]\[[^\]]*\]', _protect, text)

    for term in _GLOSSARY_SORTED:
        if term in text:
            text = text.replace(term, _make_ph(term))

    def _protect_link(m):
        return f'{_make_ph("[")}{m.group(1)}{_make_ph(f"]{m.group(2)}")}'
    text = re.sub(r'\[([^\]]*)\](\([^)]*(?:\s+"[^"]*")?\))', _protect_link, text)

    def _protect_ref_link(m):
        return f'{_make_ph("[")}{m.group(1)}{_make_ph(f"]{m.group(2)}")}'
    text = re.sub(r'\[([^\]]*)\](\[[^\]]*\])', _protect_ref_link, text)

    text = re.sub(r'^\[[^\]]*\]:\s*\S+(?:\s+"[^"]*")?\s*$', _protect, text, flags=re.MULTILINE)
    text = re.sub(r'https?://[^\s)\]\)]+', _protect, text)
    text = re.sub(r'<[^>]+>', _protect, text)
    text = re.sub(r'\*\*', _protect, text)
    text = re.sub(r'(?m)^#{1,6}\s', _protect, text)

    text = PLACEHOLDER_RE.sub(f'{_LINK_SEP}\\g<0>{_LINK_SEP}', text)
    for pattern, target in _TRANSLATION_MAP_PATTERNS:
        text = pattern.sub(lambda m: _make_ph(target), text)
    text = text.replace(_LINK_SEP, '')

    return text, placeholders


def split_by_paragraph(content):
    """按 markdown 段落拆分，代码块内空行不算分隔"""
    paragraphs = []
    current = []
    in_code_block = False
    for line in content.split('\n'):
        stripped = line.strip()
        if stripped.startswith('```'):
            in_code_block = not in_code_block
            current.append(line)
        elif in_code_block:
            current.append(line)
        elif stripped == '':
            if current:
                paragraphs.append('\n'.join(current))
                current = []
        else:
            current.append(line)
    if current:
        paragraphs.append('\n'.join(current))
    return paragraphs


def translate_markdown(content, translate_fn, cache_mgr, force=False):
    """
    翻译整个 markdown 文件内容
    translate_fn: translation-service 的 translate 函数
    返回 (translated_content, stats)
    """
    paragraphs = split_by_paragraph(content)
    results = []
    cache_hits = 0
    api_calls = 0

    for i, para in enumerate(paragraphs):
        if not para.strip():
            results.append(para)
            continue

        protected, placeholders = extract_protected(para)
        if not protected.strip():
            results.append(para)
            continue

        print(f"    para {i + 1}/{len(paragraphs)}: {len(para)} chars")
        api_result, from_cache = translate_fn(protected, cache_mgr, force)
        final = postprocess.run(api_result, placeholders)
        results.append(final)

        if from_cache:
            cache_hits += 1
        else:
            api_calls += 1

    stats = {'paragraphs': len(paragraphs), 'cache_hits': cache_hits, 'api_calls': api_calls}
    return '\n\n'.join(results), stats


def _file_sha(path):
    """计算文件内容的 SHA256"""
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def main():
    parser = argparse.ArgumentParser(description='Translate .md files to Chinese')
    parser.add_argument('--source-dir', required=True, help='源目录（英文原版）')
    parser.add_argument('--cache-dir', required=True, help='缓存目录路径')
    parser.add_argument('--service-dir', required=True, help='translation-service 目录路径')
    parser.add_argument('--api-key', default=os.environ.get('DEEPL_API_KEY'))
    parser.add_argument('--force', action='store_true', help='强制重译，忽略段落缓存')
    parser.add_argument('--sha-cache', default=None, help='逐文件 SHA 缓存路径（file_shas.json）')
    parser.add_argument('--no-sha-skip', action='store_true', help='跳过文件 SHA 对比，处理所有文件（段落缓存仍生效）')
    args = parser.parse_args()

    if args.force:
        print("::warning::--force enabled, ignoring paragraph cache.")

    # 加载 translation-service（缓存查询 + 翻译）
    service_path = Path(args.service_dir).resolve()
    sys.path.insert(0, str(service_path))
    from translator import translate as translate_fn
    from cache_manager import CacheManager

    api_key = args.api_key
    source_dir = Path(args.source_dir).resolve()
    cache_mgr = CacheManager(args.cache_dir).load()
    print(f"Cache: {cache_mgr.stats()}")

    # 加载逐文件 SHA 缓存
    sha_cache = {}
    sha_cache_path = Path(args.sha_cache) if args.sha_cache else None
    if sha_cache_path and sha_cache_path.exists():
        sha_cache = json.loads(sha_cache_path.read_text(encoding='utf-8'))

    def _translate(text, cache, force_flag):
        return translate_fn(text, api_key, cache, force_flag)

    translated_count = 0
    skipped_count = 0
    new_sha_cache = {}

    for md_file in sorted(source_dir.rglob('*')):
        if md_file.is_dir():
            continue
        if md_file.name.lower() not in _TRANSLATE_FILES_LOWER:
            continue
        if md_file.parent != source_dir and md_file.parent.parent != source_dir:
            continue

        rel_path = str(md_file.relative_to(source_dir))
        file_sha = _file_sha(md_file)
        new_sha_cache[rel_path] = file_sha

        if not args.force and not args.no_sha_skip and sha_cache.get(rel_path) == file_sha:
            print(f"\nSkipping (SHA unchanged): {rel_path}")
            skipped_count += 1
            continue

        print(f"\nTranslating: {rel_path}")
        content = md_file.read_text(encoding='utf-8')
        translated, stats = translate_markdown(content, _translate, cache_mgr, args.force)

        if translated != content:
            md_file.write_text(translated, encoding='utf-8')
            print(f"  Written ({len(translated)} chars) - {stats}")
        else:
            print(f"  Unchanged - {stats}")
        translated_count += 1

    cache_mgr.save()

    # 保存逐文件 SHA 缓存
    if sha_cache_path:
        sha_cache_path.parent.mkdir(parents=True, exist_ok=True)
        sha_cache_path.write_text(json.dumps(new_sha_cache, indent=2, sort_keys=True), encoding='utf-8')

    print(f"\nFinal stats: {cache_mgr.stats()}")
    print(f"Translated: {translated_count}, Skipped (SHA unchanged): {skipped_count}")

    # 输出 skipped 标记给 GitHub Actions
    github_output = os.environ.get('GITHUB_OUTPUT')
    if github_output:
        skipped = "true" if translated_count == 0 else "false"
        with open(github_output, 'a') as f:
            f.write(f"skipped={skipped}\n")


if __name__ == '__main__':
    main()
