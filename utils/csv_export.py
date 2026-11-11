"""电子表格导出边界：历史记录和所有文本列统一防止公式解释。"""
import unicodedata

# 首个有效字符命中这些前缀时，Excel / WPS 会把单元格当公式。
_FORMULA_TRIGGERS = ('=', '+', '-', '@', '＝', '＋', '－', '＠')
# openpyxl 禁止写入 XML 1.0 的控制字符；保留制表符、换行和回车。
# str.translate 按 Unicode 码位索引，因此键必须是整数。
_ILLEGAL_XML_CONTROLS = dict.fromkeys(
    (*range(0x00, 0x09), 0x0B, 0x0C, *range(0x0E, 0x20)),
    None,
)


def spreadsheet_cell(value, *, strip_illegal_controls=False):
    if not isinstance(value, str):
        return value
    if strip_illegal_controls:
        value = value.translate(_ILLEGAL_XML_CONTROLS)
    # 前导空白、Cc/Cf（含 BOM）只用于判断，不从结果里删掉。
    significant = value
    while significant and (significant[0].isspace()
                           or unicodedata.category(significant[0]) in {'Cc', 'Cf'}):
        significant = significant[1:]
    return "'" + value if significant.startswith(_FORMULA_TRIGGERS) else value


def spreadsheet_frame(df):
    """返回副本：文本列按单元格消毒，数值列保持原来的 dtype。"""
    import pandas as pd

    framed = df.copy()
    for pos in range(framed.shape[1]):
        series = framed.iloc[:, pos]
        dtype = series.dtype
        if not (
            dtype == object
            or isinstance(dtype, (pd.StringDtype, pd.CategoricalDtype))
        ):
            continue
        # 分类列的 .map 不能产生原 categories 之外的字符串，而且按位置
        # 把 object 写回 category 会在后续 pandas 里直接报错。先换成 object。
        if isinstance(dtype, pd.CategoricalDtype):
            framed.isetitem(pos, series.astype(object))
            series = framed.iloc[:, pos]
        framed.iloc[:, pos] = series.astype(object).map(
            lambda v: spreadsheet_cell(v, strip_illegal_controls=True)
        )
    return framed
