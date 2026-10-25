"""Scope for this fork's automated shortlist, independent of strategy scores."""
import re
from collections import Counter
from decimal import Decimal, InvalidOperation

MAINBOARD_PREFIXES = ('000', '001', '002', '003', '600', '601', '603', '605')
MAX_POSITION_YUAN = 10000
FEE_RESERVE_YUAN = 100
LOT_SIZE = 100
SCOPE_NOTICE = ('范围：仅沪深主板，排除创业板/科创板/北交所、ST及退市标记股票；'
                '单只预算≤10000元，按100股预留100元费用，参考股价≤99元。'
                '次日价格和实际费用变化后须重新核对，所有交易人工执行。')


def policy_descriptor():
    return {'version': 1, 'market': 'cn_mainboard', 'max_position_yuan': MAX_POSITION_YUAN,
            'fee_reserve_yuan': FEE_RESERVE_YUAN, 'lot_size': LOT_SIZE}


def is_mainboard(code):
    return isinstance(code, str) and bool(re.fullmatch(r'\d{6}', code)) and code.startswith(MAINBOARD_PREFIXES)


def price_value(raw):
    if isinstance(raw, bool):
        return None
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return value if value.is_finite() and value > 0 else None


def rejection_reason(code, name, price):
    if not is_mainboard(code):
        return 'outside_mainboard'
    if not isinstance(name, str) or not name.strip():
        return 'name_unknown'
    if re.search(r'ST|退', name, re.IGNORECASE):
        return 'risk_name'
    value = price_value(price)
    if value is None:
        return 'price_unknown'
    if value * LOT_SIZE + FEE_RESERVE_YUAN > MAX_POSITION_YUAN:
        return 'over_budget'
    return None


def filter_snapshot(frame):
    if not {'code', 'name', 'price'}.issubset(frame.columns):
        raise ValueError('Scope requires code, name and price')
    reasons = [rejection_reason(row.code, row['name'], row.price) for _, row in frame.iterrows()]
    result = frame.loc[[reason is None for reason in reasons]].copy()
    result.attrs['universe_audit'] = {'input_count': len(frame), 'eligible_count': len(result),
                                    'rejected_counts': dict(Counter(reason for reason in reasons if reason))}
    return result


def validate_payload(payload):
    """Recheck saved artifacts before fees or notifications, including old runs."""
    if payload.get('universe_policy') != policy_descriptor():
        raise ValueError('Missing or outdated screening scope')
    allowed = {}
    for strategy in payload.get('strategies', []):
        for row in strategy.get('picks', []):
            if rejection_reason(row.get('code'), row.get('name'), row.get('price')):
                raise ValueError('Ineligible screening pick')
            identity = (row['name'], price_value(row['price']))
            if row['code'] in allowed and allowed[row['code']] != identity:
                raise ValueError('Conflicting screening pick')
            allowed[row['code']] = identity
    seen = set()
    for row in payload.get('candidates', []):
        code = row.get('code')
        if (rejection_reason(code, row.get('name'), row.get('price')) or code in seen
                or allowed.get(code) != (row.get('name'), price_value(row.get('price')))):
            raise ValueError('Ineligible screening candidate')
        seen.add(code)
