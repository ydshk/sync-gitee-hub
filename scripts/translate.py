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
MONTHLY_FREE_QUOTA = 5_000_000
# 安全阈值：剩余低于此值停止翻译（避免超额）
QUOTA_SAFETY_THRESHOLD = 100_000

# 专有名词术语表：这些词不翻译，保持原文
# 按长度降序匹配，避免短术语先匹配导致长术语被截断（如 "Music Assistant" 先匹配会破坏 "Music Assistant Server"）
GLOSSARY = [
    'Music Assistant Server',
    'Music Assistant',
    'Home Assistant',
]


def call_tencent_api(text: str, secret_id: str, secret_key: str) -> tuple:
    """
    调用腾讯云机器翻译 API（单次请求，文本必须 < 6000 字节）
    返回 (translated_text, used_chars)
    used_chars = 输入字符数 + 输出字符数（腾讯云按 total 字符计费）

    直接用 HTTP 请求，不依赖 SDK，避免类名变化导致的问题
    """
    import json
    import time
    import hashlib
    import hmac
    import requests

    # 腾讯云 API 3.0 签名算法
    service = "tmt"
    host = "tmt.tencentcloudapi.com"
    endpoint = f"https://{host}"

    # 1. 拼接规范请求串
    action = "TextTranslate"
    version = "2018-03-21"
    region = "ap-beijing"
    timestamp = int(time.time())
    date = time.strftime("%Y-%m-%d", time.gmtime(timestamp))

    payload = {
        "SourceText": text,
        "Source": "en",
        "Target": "zh",
        "ProjectId": 0,
    }
    payload_json = json.dumps(payload, separators=(',', ':'), ensure_ascii=False)

    # http request method
    http_method = "POST"
    canonical_uri = "/"
    canonical_querystring = ""
    canonical_headers = f"content-type:application/json; charset=utf-8\nhost:{host}\nx-tc-action:{action.lower()}\n"
    signed_headers = "content-type;host;x-tc-action"
    hashed_payload = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
    canonical_request = f"{http_method}\n{canonical_uri}\n{canonical_querystring}\n{canonical_headers}\n{signed_headers}\n{hashed_payload}"

    # 2. 拼签名串
    credential_scope = f"{date}/{service}/tc3_request"
    hashed_canonical_request = hashlib.sha256(canonical_request.encode("utf-8")).hexdigest()
    string_to_sign = f"TC3-HMAC-SHA256\n{timestamp}\n{credential_scope}\n{hashed_canonical_request}"

    # 3. 计算签名
    def _sign(key, msg):
        return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()

    secret_date = _sign(("TC3" + secret_key).encode("utf-8"), date)
    secret_service = _sign(secret_date, service)
    secret_signing = _sign(secret_service, "tc3_request")
    signature = hmac.new(secret_signing, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()

    # 4. Authorization header
    authorization = (
        f"TC3-HMAC-SHA256 "
        f"Credential={secret_id}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, "
        f"Signature={signature}"
    )

    headers = {
        "Authorization": authorization,
        "Content-Type": "application/json; charset=utf-8",
        "Host": host,
        "X-TC-Action": action,
        "X-TC-Timestamp": str(timestamp),
        "X-TC-Version": version,
        "X-TC-Region": region,
    }

    resp = requests.post(endpoint, data=payload_json.encode("utf-8"), headers=headers, timeout=30)
    result = resp.json()

    if "Response" in result and "Error" in result["Response"]:
        err = result["Response"]["Error"]
        raise Exception(f"Tencent API error: {err.get('Code', 'Unknown')} - {err.get('Message', '')}")

    if "Response" not in result or "TargetText" not in result["Response"]:
        raise Exception(f"Unexpected response: {result}")

    translated_text = result["Response"]["TargetText"]
    used_chars = len(text) + len(translated_text)
    return translated_text, used_chars


PLACEHOLDER_RE = re.compile(r'XPLHX\d+XPLHX')


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
    text = re.sub(r'```[\s\S]*?```', _protect, text)
    text = re.sub(r'`[^`]*`', _protect, text)
    text = re.sub(r'<!--[\s\S]*?-->', _protect, text)
    text = re.sub(r'!\[[^\]]*\]\([^)]*(?:\s+"[^"]*")?\)', _protect, text)

    # 2. 专有名词术语表保护（在链接拆分前，确保链接 text 里的术语也被保护不翻译）
    for term in sorted(GLOSSARY, key=len, reverse=True):
        if term in text:
            text = text.replace(term, _make_ph(term))

    # 3. 链接 [text](url)：保护 [ 和 ](url) 语法骨架，text 留给翻译 API
    def _protect_link(m):
        link_text = m.group(1)
        url_part = m.group(2)
        return f'{_make_ph("[")}{link_text}{_make_ph(f"]{url_part}")}'
    text = re.sub(r'\[([^\]]*)\](\([^)]*(?:\s+"[^"]*")?\))', _protect_link, text)

    # 4. 链接引用定义、裸 URL、HTML 标签、加粗、标题（整体保护）
    text = re.sub(r'^\[[^\]]*\]:\s*\S+(?:\s+"[^"]*")?\s*$', _protect, text, flags=re.MULTILINE)
    text = re.sub(r'https?://[^\s)\]\)]+', _protect, text)
    text = re.sub(r'<[^>]+>', _protect, text)
    text = re.sub(r'\*\*', _protect, text)
    text = re.sub(r'(?m)^#{1,6}\s', _protect, text)

    return text, placeholders


def _restore_placeholders(text: str, placeholders: dict) -> str:
    """
    将占位符替换回原始内容。
    按 idx 降序还原：外层占位符（idx 大）先还原，其 original 可能含内层占位符
    （idx 小），内层占位符后还原，从而正确处理嵌套结构（如图片嵌套在链接里）。
    """
    if not placeholders:
        return text
    sorted_ph = sorted(placeholders.items(), key=lambda x: int(x[0][5:-5]), reverse=True)
    for ph, original in sorted_ph:
        text = text.replace(ph, original)
    return text


def split_markdown(text: str) -> list:
    """
    按 markdown 结构切分：
    - 占位符（XPLHX\\d+XPLHX，由 _extract_protected 生成）作为 code chunk 不翻译
    - 其他文本按字节长度切分（<= MAX_CHUNK_BYTES）
    """
    chunks = []
    last_end = 0

    for m in PLACEHOLDER_RE.finditer(text):
        if m.start() > last_end:
            chunks.extend(split_by_length(text[last_end:m.start()]))
        chunks.append({'type': 'code', 'content': m.group()})
        last_end = m.end()

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
                translated, used = call_tencent_api(text, secret_id, secret_key)
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
    final = _restore_placeholders(translated, placeholders)
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
    parser.add_argument('--secret-id', default=os.environ.get('TENCENT_SECRET_ID'))
    parser.add_argument('--secret-key', default=os.environ.get('TENCENT_SECRET_KEY'))
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

    # 只翻译根目录的 README.md（不区分大小写），其他 .md 文件保持英文原版
    # 这样可以大幅节省翻译额度，同时保护其他文档不被改动
    md_files = []
    skipped_other_md = []
    for p in source_dir.rglob('*.md'):
        if '.git' in p.parts:
            continue
        rel_path_str = str(p.relative_to(source_dir)).replace('\\', '/')
        # 只翻译根目录的 README.md（不区分大小写）
        # README.md / readme.md / Readme.md 都识别
        if rel_path_str.lower() == 'readme.md':
            md_files.append(p)
        else:
            skipped_other_md.append(rel_path_str)

    print(f"Found {len(md_files)} README.md to translate (root only)")
    if skipped_other_md:
        print(f"Skipping {len(skipped_other_md)} other .md files (kept English):")
        for f in skipped_other_md[:10]:  # 只显示前10个避免日志过长
            print(f"  - {f}")
        if len(skipped_other_md) > 10:
            print(f"  ... and {len(skipped_other_md) - 10} more")

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
                content, args.secret_id, args.secret_key,
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
