"""
执行基本的加减乘除及平方运算

Args:
    operation: 运算类型，可选 "add"（加）、"subtract"（减）、"multiply"（乘）、"divide"（除）、"square"（平方）
    num1: 第一个操作数，对于平方运算是要计算的数字
    num2: 第二个操作数，对于平方运算可忽略

Returns:
    str: 计算结果
"""
import json


def execute(operation: str, num1: float, num2: float = 0) -> str:
    if operation == "add":
        result = num1 + num2
    elif operation == "subtract":
        result = num1 - num2
    elif operation == "multiply":
        result = num1 * num2
    elif operation == "divide":
        if num2 == 0:
            return json.dumps({"error": "除数不能为零"}, ensure_ascii=False)
        result = num1 / num2
    elif operation == "square":
        result = num1 * num1
    else:
        return json.dumps({"error": f"不支持的运算类型: {operation}"}, ensure_ascii=False)

    return json.dumps({
        "operation": operation,
        "num1": num1,
        "num2": num2 if operation != "square" else None,
        "result": result
    }, ensure_ascii=False)