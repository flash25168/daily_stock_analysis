"""Shared implementation; source-specific entry points remain compatible."""
from typing import Any, Dict, Optional
import pandas as pd

def calc_market_stats(
    self,
    df: pd.DataFrame,
    ) -> Optional[Dict[str, Any]]:
    """从行情 DataFrame 计算涨跌统计。"""
    from .base import normalize_stock_code, is_bse_code, is_kc_cy_stock, is_st_stock
    import numpy as np

    df = df.copy()

    # 1. 提取基础比对数据：最新价、昨收
    # 兼容不同接口返回的列名 sina/em efinance tushare xtdata
    code_col = next((c for c in ['代码', '股票代码', 'ts_code','stock_code'] if c in df.columns), None)
    name_col = next((c for c in ['名称', '股票名称','name','name'] if c in df.columns), None)
    close_col = next((c for c in ['最新价', '最新价', 'close','lastPrice'] if c in df.columns), None)
    pre_close_col = next((c for c in ['昨收', '昨日收盘', 'pre_close','lastClose'] if c in df.columns), None)
    amount_col = next((c for c in ['成交额', '成交额', 'amount','amount'] if c in df.columns), None)

    limit_up_count = 0
    limit_down_count = 0
    up_count = 0
    down_count = 0
    flat_count = 0

    for code, name, current_price, pre_close, amount in zip(
        df[code_col], df[name_col], df[close_col], df[pre_close_col], df[amount_col]
    ):

        # 停牌过滤 efinance 的停牌数据有时候会缺失价格显示为 '-'，em 显示为none
        if pd.isna(current_price) or pd.isna(pre_close) or current_price in ['-'] or pre_close in ['-'] or amount == 0:
            continue

        # em、efinance 为str 需要转换为float
        current_price = float(current_price)
        pre_close = float(pre_close)

        # 获取去除前缀的纯数字代码
        pure_code = normalize_stock_code(str(code))

        # A. 确定每只股票的涨跌幅比例 (使用纯数字代码判断)
        if is_bse_code(pure_code):
            ratio = 0.30
        elif is_kc_cy_stock(pure_code): #pure_code.startswith(('688', '30')):
            ratio = 0.20
        elif is_st_stock(name): #'ST' in str_name:
            ratio = 0.05
        else:
            ratio = 0.10

        # B. 严格按照 A 股规则计算涨跌停价：昨收 * (1 ± 比例) -> 四舍五入保留2位小数
        limit_up_price = np.floor(pre_close * (1 + ratio) * 100 + 0.5) / 100.0
        limit_down_price = np.floor(pre_close * (1 - ratio) * 100 + 0.5) / 100.0

        limit_up_price_Tolerance = round(abs(pre_close * (1 + ratio) - limit_up_price), 10)
        limit_down_price_Tolerance = round(abs(pre_close * (1 - ratio) - limit_down_price), 10)

        # C. 精确比对
        if current_price > 0 :
            is_limit_up = (current_price > 0) and (abs(current_price - limit_up_price) <= limit_up_price_Tolerance)
            is_limit_down = (current_price > 0) and (abs(current_price - limit_down_price) <= limit_down_price_Tolerance)

            if is_limit_up:
                limit_up_count += 1
            if is_limit_down:
                limit_down_count += 1

            if current_price > pre_close:
                up_count += 1
            elif current_price < pre_close:
                down_count += 1
            else:
                flat_count += 1

    # 统计数量
    stats = {
        'up_count': up_count,
        'down_count': down_count,
        'flat_count': flat_count,
        'limit_up_count': limit_up_count,
        'limit_down_count': limit_down_count,
        'total_amount': 0.0,
    }

    # 成交额统计
    if amount_col and amount_col in df.columns:
        df[amount_col] = pd.to_numeric(df[amount_col], errors='coerce')
        stats['total_amount'] = (df[amount_col].sum() / 1e8)

    return stats
