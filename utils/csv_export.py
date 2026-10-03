"""电子表格导出边界：历史记录和所有文本列统一防止公式解释。"""
import unicodedata


def spreadsheet_cell(value):
    if not isinstance(value, str):
        return value
    # 检查前导空白、控制字符和 BOM 后的首字符，原始内容完整保留。
    significant = value
    while significant and (significant[0].isspace()
                           or unicodedata.category(significant[0]) in {'Cc', 'Cf'}):
        significant = significant[1:]
    return "'" + value if significant.startswith(('=', '+', '-', '@')) else value
