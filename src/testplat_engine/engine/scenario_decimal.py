"""有界 Decimal 运算：中间结果精确，最终显式量化，JSON 输出为字符串。"""

import re
from decimal import (
    ROUND_DOWN,
    ROUND_HALF_EVEN,
    ROUND_HALF_UP,
    Context,
    Decimal,
    DecimalException,
    Inexact,
    localcontext,
)

from testplat_engine.engine.scenario_extract import SelectionError, at_path

ROUNDINGS = {"HALF_UP": ROUND_HALF_UP, "HALF_EVEN": ROUND_HALF_EVEN, "DOWN": ROUND_DOWN}
_LITERAL = re.compile(r"[+-]?[0-9]+(?:\.[0-9]+)?\Z")


class CalculationError(ValueError):
    """安全错误，不回显操作数。"""


def _bounded(value: Decimal) -> Decimal:
    if (
        not value.is_finite()
        or len(value.as_tuple().digits) > 64
        or value.as_tuple().exponent < -32
        or value.adjusted() > 32
    ):
        raise CalculationError("金额超出支持范围")
    return value


def _decimal(value) -> Decimal:
    if isinstance(value, str):
        if len(value) > 98 or not _LITERAL.fullmatch(value):
            raise CalculationError("金额必须为有限十进制数")
    elif type(value) not in (int, float):
        raise CalculationError("金额必须为十进制字符串或 JSON 数字")
    return _bounded(Decimal(str(value)))


def calculate(operands, *, operation: str, scale: int, rounding: str, path: list) -> str:
    if not isinstance(operands, list) or len(operands) > 1024:
        raise CalculationError("操作数必须是最多 1024 项的数组")
    if operation != "sum" and len(operands) < 2:
        raise CalculationError("加法与乘法至少需要两个操作数")
    try:
        # 独立上下文，避免调用方的精度、舍入、trap 设置改变执行结果。
        with localcontext(Context(prec=256, Emax=999, Emin=-999)) as context:
            context.traps[Inexact] = True
            values = [
                _decimal(at_path(value, path) if operation == "sum" else value)
                for value in operands
            ]
            result = Decimal(1 if operation == "multiply" else 0)
            for value in values:
                result = _bounded(result * value if operation == "multiply" else result + value)
            context.traps[Inexact] = False
            result = result.quantize(Decimal(1).scaleb(-scale), rounding=ROUNDINGS[rounding])
            _bounded(result)
            if result.is_zero():
                result = result.copy_abs()
            return format(result, "f")
    except (DecimalException, SelectionError, ValueError) as exc:
        raise CalculationError("金额操作数、路径或计算范围无效") from exc
