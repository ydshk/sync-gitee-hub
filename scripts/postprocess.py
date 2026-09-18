"""
后处理逻辑：对翻译 API 返回的结果应用格式修复。
独立模块，可单独迭代——改后处理不需重新调翻译 API。
"""
import re

_LINK_SEP = '\u200b'

PLACEHOLDER_RE = re.compile(r'XPLHX\d+XPLHX', re.IGNORECASE)


def restore_placeholders(text, placeholders):
    """将占位符替换回原始内容，容忍翻译 API 在占位符内插入空格"""
    if not placeholders:
        return text
    ph_fuzzy = re.compile(r'XPLHX\s*(\d+)\s*XPLHX', re.IGNORECASE)
    sorted_ph = sorted(placeholders.items(), key=lambda x: int(x[0][5:-5]), reverse=True)
    ph_by_idx = {int(ph[5:-5]): original for ph, original in sorted_ph}

    def _restore(m):
        idx = int(m.group(1))
        return ph_by_idx.get(idx, m.group())

    prev = None
    while text != prev:
        prev = text
        text = ph_fuzzy.sub(_restore, text)
    return text


def cjk_latin_spacing(text):
    """中英混排加空格，压缩 DeepL 产生的多余空格"""
    text = re.sub(r'([\u4e00-\u9fff]) *([a-zA-Z0-9])', r'\1 \2', text)
    text = re.sub(r'([a-zA-Z0-9]) *([\u4e00-\u9fff])', r'\1 \2', text)
    return text


def fix_bold(text):
    """修复加粗内部前后空格 + 去掉标点外加粗 + 按行修复奇数个 **"""
    def _strip_bold(m):
        return f'**{m.group(1).strip()}**'
    text = re.sub(r'\*\*(.+?)\*\*', _strip_bold, text)
    text = re.sub(r'\*\*([。，；！？])\*\*', r'\1', text)

    def _fix_bold_per_line(line):
        if line.count('**') % 2 == 0:
            return line
        idx = line.rfind('**')
        after = line[idx + 2:]
        if after.strip():
            return line + '**'
        return line[:idx] + line[idx + 2:]
    text = '\n'.join(_fix_bold_per_line(ln) for ln in text.split('\n'))
    text = re.sub(r'\*\* (?=\S)', '**', text)
    return text


def fix_not_untranslated(text):
    """将未翻译的 NOT（后接中文）替换为'不'"""
    return re.sub(r'\bNOT\s+([\u4e00-\u9fff])', r'不\1', text)


def fix_duplicate_cjk(text):
    """去掉重复的中文词（版本版本→版本，步骤步骤→步骤，问题的问题→问题）"""
    prev = None
    while text != prev:
        prev = text
        text = re.sub(r'([\u4e00-\u9fff]{2,})\1', r'\1', text)
        text = re.sub(r'([\u4e00-\u9fff]{2,})的\1', r'\1', text)
    return text


def fix_double_negation(text):
    """修复双重否定：不未→未，不不→不"""
    text = text.replace('不未', '未')
    text = re.sub(r'不不', '不', text)
    return text


def fix_title(text):
    """修复标题多空格：##  关于 → ## 关于"""
    return re.sub(r'(?m)^(#{1,6})\s{2,}', r'\1 ', text)


def fix_link(text):
    """修复链接 text 前后多余空格 + 去掉含中文链接文本末尾句号"""
    text = re.sub(r'\[\s*([^\[\]]+?)\s*\]\(', r'[\1](', text)
    text = re.sub(r'\[\s*([^\[\]]+?)\s*\]\[', r'[\1][', text)
    text = re.sub(r'\[([^\[\]]*[\u4e00-\u9fff][^\[\]]*?)\.\]\(', r'[\1](', text)
    text = re.sub(r'\[([^\[\]]*[\u4e00-\u9fff][^\[\]]*?)\.\]\[', r'[\1][', text)
    return text


def fix_cjk_space(text):
    """去掉中文字符之间的多余空格"""
    return re.sub(r'([\u4e00-\u9fff]) (?=[\u4e00-\u9fff])', r'\1', text)


def fix_punct_space(text):
    """去掉中文标点前后的多余空格"""
    text = re.sub(r' ([，。！？；：）」】（])', r'\1', text)
    text = re.sub(r'([，。！？；：（「【]) ', r'\1', text)
    return text


def run(translated, placeholders):
    """后处理主入口：还原占位符 + 全部格式修复"""
    translated = cjk_latin_spacing(translated)
    final = restore_placeholders(translated, placeholders)
    final = final.replace(_LINK_SEP, '')
    final = re.sub(r'\s*XPLHX\s*', ' ', final, flags=re.IGNORECASE)
    final = fix_not_untranslated(final)
    final = fix_bold(final)
    final = fix_title(final)
    final = fix_link(final)
    final = fix_cjk_space(final)
    final = fix_duplicate_cjk(final)
    final = fix_double_negation(final)
    final = fix_punct_space(final)
    return final