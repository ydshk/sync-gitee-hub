#!/usr/bin/env python3
"""
将 GitHub 仓库中的 .md 文件翻译为中文，覆盖原文件。

策略：
1. 从 translated_dir（Gitee 端）读取 .translation-cache.json
2. 遍历 source_dir（GitHub 端）所有 .md 文件，计算内容 hash
3. hash 命中缓存  ->  从 Gitee 端复制已翻译版本，零 API 调用
4. hash 未命中    ->  调用腾讯云机器翻译 API 翻译
5. 更新缓存，写入 source_dir/.translation-cache.json

腾讯云 TMT API 限制：
- 单次请求最大 6000 字节（UTF-8）
- QPS 限制 5/秒
- 每月免费 500 万字符
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path

# 单次 API 请求最大字节（留余量）
MAX_CHUNK_BYTES = 4000
# QPS 控制（秒）
API_INTERVAL = 0.25
# 腾讯云机器翻译每月免费额度：500 万字符
MONTHLY_FREE_QUOTA = 5_000_000
# 安全阈值：剩余低于此值停止翻译（避免超额）
QUOTA_SAFETY_THRESHOLD = 100_000


def call_tencent_api(text: str, secret_id: str, secret_key: str) -> tuple:
    """
    调用腾讯云机器翻译 API（单次请求，文本必须 < 6000 字节）
    返回 (translated_text, used_chars)
    used_chars = 输入字符数 + 输出字符数（腾讯云按 total 字符计费）
    """
    from tencentcloud.common import credential
    from tencentcloud.tmt.v20180321 import tmt_client, models

    cred = credential.Credential(secret_id, secret_key)
    client = tmt_client.TmtClient(cred, "ap-beijing")

    req = models.TextTranslateRequest()
    req.SourceText = text
    req.Source = "en"
    req.Target = "zh"
    req.ProjectId = 0

    resp = client.TextTranslate(req)
    used_chars = len(text) + len(resp.TargetText)
    return resp.TargetText, used_chars


def split_markdown(text: str) -> list:
    """
    按 markdown 结构切分：
    - 代码块（```...```）整块保留，不翻译
    - 行内代码（`...`）保留，不翻译
    - 其他文本按字节长度切分（<= MAX_CHUNK_BYTES）
    """
    chunks = []
    # 匹配代码块和行内代码
    pattern = re.compile(r'```[\s\S]*?```|`[^`]*`', re.MULTILINE)
    last_end = 0

    for m in pattern.finditer(text):
        # 代码块之前的普通文本
        if m.start() > last_end:
            chunks.extend(split_by_length(text[last_end:m.start()]))
        # 代码块本身
        chunks.append({
            'type': 'code',
            'content': m.group()
        })
        last_end = m.end()

    # 剩余文本
    if last_end < len(text):
        chunks.extend(split_by_length(text[last_end:]))

    return chunks


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
                        line_buf = line + '\n'
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


def translate_markdown(content: str, secret_id: str, secret_key: str, quota_used: int) -> tuple:
    """
    翻译整个 markdown 文件
    返回 (translated_content, used_chars, quota_exceeded)
    quota_exceeded=True 表示翻译中额度用尽，剩余 chunks 保留原文
    """
    chunks = split_markdown(content)
    result = []
    total_used = 0
    quota_exceeded = False

    for i, chunk in enumerate(chunks):
        if quota_exceeded:
            # 额度用尽，剩余 chunks 原样保留
            result.append(chunk['content'] if chunk['type'] == 'code' else chunk['content'])
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
                translated, used = call_tencent_api(text, secret_id, secret_key)
                result.append(translated)
                total_used += used
                print(f"    chunk {i+1}/{len(chunks)}: {len(text)} chars -> translated (used {used}, total {quota_used + total_used})")
            except Exception as e:
                print(f"    chunk {i+1}/{len(chunks)}: FAILED - {e}")
                # 失败时保留原文
                result.append(text)
            # QPS 控制
            time.sleep(API_INTERVAL)

    return ''.join(result), total_used, quota_exceeded


def main():
    parser = argparse.ArgumentParser(description='Translate .md files to Chinese')
    parser.add_argument('--source-dir', required=True,
                        help='GitHub 仓库目录（英文原版）')
    parser.add_argument('--translated-dir', required=True,
                        help='Gitee 仓库目录（已翻译中文版本）')
    parser.add_argument('--secret-id', default=os.environ.get('TENCENT_SECRET_ID'))
    parser.add_argument('--secret-key', default=os.environ.get('TENCENT_SECRET_KEY'))
    args = parser.parse_args()

    source_dir = Path(args.source_dir).resolve()
    translated_dir = Path(args.translated_dir).resolve()

    # 读取缓存（含配额计数器）
    cache_path = translated_dir / '.translation-cache.json'
    if cache_path.exists():
        try:
            with open(cache_path, 'r', encoding='utf-8') as f:
                cache = json.load(f)
        except Exception:
            cache = {}
    else:
        cache = {}

    # 配额计数器单独存储在 __quota__ 字段
    # 结构：{"2026-09": 123456, "2026-10": 7890, ...} 按月记录
    quota_by_month = cache.pop('__quota__', {})
    if not isinstance(quota_by_month, dict):
        quota_by_month = {}

    # 当前年月
    current_month = time.strftime('%Y-%m')
    quota_used = quota_by_month.get(current_month, 0)
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

    # 遍历所有 .md 文件（排除 .git 目录）
    md_files = []
    for p in source_dir.rglob('*.md'):
        if '.git' in p.parts:
            continue
        md_files.append(p)

    print(f"Found {len(md_files)} .md files in source dir")

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

        # 检查缓存
        if cache.get(rel_path) == content_hash:
            # 文件未变化，从 Gitee 端复制已翻译版本
            gitee_file = translated_dir / rel_path
            if gitee_file.exists():
                shutil.copy2(gitee_file, md_file)
                reused_count += 1
                print(f"  REUSED: {rel_path}")
                continue
            # 缓存命中但 Gitee 端文件不存在（异常），继续翻译

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
            cache[rel_path] = content_hash
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
            translated, used, quota_exceeded = translate_markdown(
                content, args.secret_id, args.secret_key,
                quota_used + total_quota_used_this_run
            )
            md_file.write_text(translated, encoding='utf-8')
            cache[rel_path] = content_hash
            translated_count += 1
            total_quota_used_this_run += used

            if quota_exceeded:
                # 翻译中额度用尽，标记全局
                global_quota_exceeded = True
                print(f"  -> Quota exceeded during translation. Remaining files will be skipped.")
        except Exception as e:
            print(f"  FAILED: {rel_path} - {e}")
            failed_count += 1

    # 更新配额计数器（当前月累加本次使用量）
    quota_by_month[current_month] = quota_used + total_quota_used_this_run

    # 把配额数据放回缓存
    cache['__quota__'] = quota_by_month

    # 写入缓存到 source_dir
    cache_out = source_dir / '.translation-cache.json'
    with open(cache_out, 'w', encoding='utf-8') as f:
        json.dump(cache, f, indent=2, ensure_ascii=False)

    print(f"\n=== Summary ===")
    print(f"Translated:     {translated_count}")
    print(f"Reused:         {reused_count}")
    print(f"Skipped:        {skipped_count}")
    print(f"Failed:         {failed_count}")
    print(f"Quota skipped:  {quota_skipped_count}")
    print(f"Quota used this run: {total_quota_used_this_run}")
    print(f"Quota total {current_month}: {quota_used + total_quota_used_this_run}/{MONTHLY_FREE_QUOTA}")

    # 翻译失败不中断整个 workflow（已翻译的部分仍要推送）
    # 但额度用尽时输出 warning 提醒
    if quota_skipped_count > 0:
        print(f"\n::warning::Translation quota exceeded. {quota_skipped_count} files not translated (kept English). Will retry next month.")


if __name__ == '__main__':
    main()
