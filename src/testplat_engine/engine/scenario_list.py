"""有序列表投影与数量汇总，不包含表达式解释器。"""

from testplat_engine.engine.scenario_extract import SelectionError, at_path


def transform(source, *, operation: str, path: list):
    if not isinstance(source, list) or len(source) > 1024:
        raise SelectionError("列表来源必须是最多 1024 项的数组")
    values = [at_path(item, path) for item in source]
    if operation == "project":
        return values
    if not values or any(type(value) is not int or value <= 0 for value in values):
        raise SelectionError("数量必须为非空列表中的正整数")
    total = sum(values)
    if total > 2**53 - 1:
        raise SelectionError("总数量超出 JSON 安全整数范围")
    return total
