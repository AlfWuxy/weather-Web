# -*- coding: utf-8 -*-
"""模板展开证明：证明模板重组没有改变任何渲染结果。

P5 只允许一种模板改动：把逐字相同的片段移到 templates/partials/，原位置换成一行 include。
本脚本把每个 include 按 Jinja 的规则原样展开，展开结果必须与基线分支上的模板源码逐字相同。
这一证明与数据和分支无关，覆盖模板里所有 if/for 分支，行为锁走不到的分支也在内。

允许的 include 写法（必须独占一行，从第 1 列开始）:
    {% include 'partials/xxx.html' %}
        片段原样插入。Jinja 会去掉片段文件末尾的一个换行，这一行自己的换行保留。
    {% filter indent(N, first=True) %}{% include 'partials/xxx.html' %}{% endfilter %}
        片段每一行加 N 个空格（空行除外），用于同一段代码在两处缩进不同的情况。
        这种写法的片段不得含任何 Jinja 语法，因为 indent 作用在渲染结果上。

片段的限制：扩展名 .html（与引用方的自动转义一致）；不得含 set、macro、block、extends、
import、from 等改变作用域的标签，不得含空白控制符（{%- -%} {{- -}}）；只能 include 其他片段。

用法:
    python scripts/template_expand_check.py --base origin/main
"""
import argparse
import re
import subprocess
import sys

from jinja2.filters import do_indent

PARTIAL_DIR = 'templates/partials/'
INCLUDE_RE = re.compile(r"^\{% include '(partials/[\w./-]+\.html)' %\}$")
INDENT_RE = re.compile(
    r"^\{% filter indent\((\d+), first=True\) %\}\{% include '(partials/[\w./-]+\.html)' %\}\{% endfilter %\}$"
)
FORBIDDEN_TAGS = re.compile(r'\{%-?\s*(set|macro|call|block|extends|import|from|filter|with)\b')
WHITESPACE_CONTROL = re.compile(r'\{%-|-%\}|\{\{-|-\}\}|\{#-|-#\}')
JINJA_SYNTAX = re.compile(r'\{\{|\{%|\{#')
NEWLINE = re.compile(r'\r\n|\r|\n')


def _git(*args):
    return subprocess.run(['git', *args], check=True, capture_output=True).stdout


def _jinja_source(raw):
    """按 Jinja 词法器的做法规范源码：统一换行为 \\n，去掉末尾的一个换行。"""
    lines = NEWLINE.split(raw)
    if lines[-1] == '':
        del lines[-1]
    return '\n'.join(lines)


def _read_head(path):
    with open(path, encoding='utf-8', newline='') as fh:
        return fh.read()


def _check_partial(path, text, errors):
    for pattern, what in ((FORBIDDEN_TAGS, '改变作用域的标签'), (WHITESPACE_CONTROL, '空白控制符')):
        match = pattern.search(text)
        if match:
            errors.append(f'{path}: 片段含{what} {match.group(0)!r}')


def expand(path, errors, stack=()):
    """返回模板展开全部片段后的 Jinja 源码（已规范换行、已去掉末尾换行）。"""
    if path in stack:
        errors.append(f'{path}: 片段循环引用 {" -> ".join(stack)}')
        return ''
    source = _jinja_source(_read_head(path))
    out = []
    for line in source.split('\n'):
        plain = INCLUDE_RE.match(line)
        indented = INDENT_RE.match(line)
        if plain or indented:
            name = (plain or indented).groups()[-1]
            partial_path = 'templates/' + name
            try:
                raw = _read_head(partial_path)
            except FileNotFoundError:
                errors.append(f'{path}: 引用的片段不存在 {partial_path}')
                continue
            _check_partial(partial_path, raw, errors)
            body = expand(partial_path, errors, stack + (path,))
            if indented:
                if JINJA_SYNTAX.search(body):
                    errors.append(f'{partial_path}: indent 写法的片段不得含 Jinja 语法')
                body = do_indent(body, int(indented.group(1)), first=True)
            out.append(body)
        elif re.search(r'\{%-?\s*include\b', line) and re.search(r'partials/', line) and path.startswith(PARTIAL_DIR):
            errors.append(f'{path}: include 写法不符合要求: {line.strip()}')
        else:
            out.append(line)
    return '\n'.join(out)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--base', required=True, help='基线提交，例如 origin/main')
    args = parser.parse_args()

    # 与分叉点比较工作区（含未提交和未跟踪的文件），本地和 CI 用法一致
    fork = _git('merge-base', args.base, 'HEAD').decode().strip()
    changed = _git('diff', '--name-status', '--no-renames', fork, '--', 'templates').decode()
    untracked = _git('ls-files', '--others', '--exclude-standard', 'templates').decode().split()
    changed += ''.join(f'A\t{path}\n' for path in untracked)
    errors = []
    checked = 0
    new_partials = []
    for row in changed.splitlines():
        status, path = row.split('\t', 1)
        if not path.endswith('.html'):
            continue
        if status == 'D':
            errors.append(f'{path}: 模板被删除')
            continue
        if status == 'A':
            if not path.startswith(PARTIAL_DIR):
                errors.append(f'{path}: 新增模板只能是 {PARTIAL_DIR} 下的片段')
            else:
                new_partials.append(path)
            continue
        base_source = _jinja_source(_git('show', f'{fork}:{path}').decode('utf-8'))
        expanded = expand(path, errors)
        checked += 1
        if expanded != base_source:
            a, b = base_source.split('\n'), expanded.split('\n')
            first = next((i for i, (x, y) in enumerate(zip(a, b)) if x != y), min(len(a), len(b)))
            errors.append(f'{path}: 展开后与基线不同，第 {first + 1} 行起\n'
                          f'    基线: {a[first] if first < len(a) else "<结束>"!r}\n'
                          f'    展开: {b[first] if first < len(b) else "<结束>"!r}')

    # 新片段必须被引用，且本身满足片段限制
    all_templates = _git('ls-files', 'templates').decode().split() + untracked
    referenced = set()
    for path in all_templates:
        if path.endswith('.html'):
            for line in _jinja_source(_read_head(path)).split('\n'):
                match = INCLUDE_RE.match(line) or INDENT_RE.match(line)
                if match:
                    referenced.add('templates/' + match.groups()[-1])
    for path in new_partials:
        _check_partial(path, _read_head(path), errors)
        if path not in referenced:
            errors.append(f'{path}: 新片段没有被任何模板引用')

    for error in errors:
        print(f'[不通过] {error}')
    print(f'展开证明：检查 {checked} 个改动的模板、{len(new_partials)} 个新片段，'
          f'{"全部与基线逐字相同" if not errors else f"{len(errors)} 处问题"}')
    return 1 if errors else 0


if __name__ == '__main__':
    sys.exit(main())
