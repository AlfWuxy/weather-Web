# -*- coding: utf-8 -*-
"""Security tests for analysis CSV export."""


def test_csv_safe_cell_neutralizes_formula_prefixes():
    """CSV cells with formula prefixes should be neutralized."""
    from blueprints.analysis import _csv_safe_cell

    assert _csv_safe_cell('=HYPERLINK("http://evil","x")').startswith("'")
    assert _csv_safe_cell('+1+1').startswith("'")
    assert _csv_safe_cell('-1+2').startswith("'")
    assert _csv_safe_cell('@cmd').startswith("'")
    assert _csv_safe_cell('normal_text') == 'normal_text'
