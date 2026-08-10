"""
并行工具执行器 — 将独立的工具调用在线程池中并行执行

设计原则：
- 三阶段输出：先发所有 tool_call → 并行执行 → 按序发 tool_result
- 线程安全：ToolRegistry/SandboxPool 均已保证线程安全
- 可降级：max_workers=0 时退化为串行

用法：
    executor = ParallelToolExecutor()
    results = executor.execute_batch(tool_calls, registry, user_id=user_id)
"""

import concurrent.futures
import json
import os

MAX_WORKERS = int(os.environ.get("PARALLEL_TOOL_MAX_WORKERS", "4"))


class ParallelToolExecutor:
    """并行工具执行器，用于批量执行独立的工具调用"""

    def __init__(self, max_workers: int = None):
        """初始化并行执行器

        Args:
            max_workers: 最大并发线程数，默认读取环境变量 PARALLEL_TOOL_MAX_WORKERS（默认 4）
                         设为 0 或 1 时退化为串行执行
        """
        self._max_workers = max_workers if max_workers is not None else MAX_WORKERS

    def execute_batch(self, tool_calls: list, registry, user_id: int = None) -> list:
        """并行执行一批工具调用

        Args:
            tool_calls: LLM 返回的 tool_calls 列表
            registry: ToolRegistry 实例
            user_id: 当前用户 ID

        Returns:
            list: 按原始顺序排列的结果列表，每项为
                  {"name", "arguments", "result", "error", "tool_call_id"}
        """
        if self._max_workers <= 0 or len(tool_calls) <= 1:
            return self._execute_sequential(tool_calls, registry, user_id)

        return self._execute_parallel(tool_calls, registry, user_id)

    def _execute_parallel(self, tool_calls, registry, user_id):
        """并行执行（ThreadPoolExecutor）"""
        results = [None] * len(tool_calls)

        with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(self._max_workers, len(tool_calls))
        ) as executor:
            futures = []
            for i, tc in enumerate(tool_calls):
                name = tc["function"]["name"]
                args = json.loads(tc["function"]["arguments"])
                f = executor.submit(self._execute_one, registry, name, args, user_id)
                futures.append((i, tc, f))

            for i, tc, f in futures:
                try:
                    result_str, error = f.result()
                except Exception as e:
                    result_str = f"工具执行错误: {str(e)}"
                    error = True

                results[i] = {
                    "name": tc["function"]["name"],
                    "arguments": json.loads(tc["function"]["arguments"]),
                    "result": result_str,
                    "error": error,
                    "tool_call_id": tc.get("id", ""),
                }

        return results

    @staticmethod
    def _execute_one(registry, name, args, user_id):
        """执行单个工具调用（线程安全）

        Args:
            registry: ToolRegistry 实例
            name: 工具名称
            args: 参数字典
            user_id: 用户 ID

        Returns:
            tuple: (result_str, error_flag)
        """
        try:
            result = registry.execute(name, args, user_id=user_id)
            if isinstance(result, dict):
                result_str = json.dumps(result, ensure_ascii=False)
            else:
                result_str = str(result)
            error = (
                result_str.startswith("Error")
                or result_str.startswith("[沙箱执行失败]")
                or result_str.startswith("[沙箱执行超时]")
                or result_str.startswith("[沙箱异常]")
                or result_str.startswith("[工具执行异常]")
            )
            return result_str, error
        except Exception as e:
            return f"工具执行错误: {str(e)}", True

    def _execute_sequential(self, tool_calls, registry, user_id):
        """串行执行（降级模式，当 max_workers <= 0 或仅 1 个工具调用时使用）"""
        results = []
        for tc in tool_calls:
            result_str, error = self._execute_one(
                registry,
                tc["function"]["name"],
                json.loads(tc["function"]["arguments"]),
                user_id,
            )
            results.append({
                "name": tc["function"]["name"],
                "arguments": json.loads(tc["function"]["arguments"]),
                "result": result_str,
                "error": error,
                "tool_call_id": tc.get("id", ""),
            })
        return results