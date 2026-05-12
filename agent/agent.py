import json


SYSTEM_PROMPT = """你是一个能使用工具的助手，可以根据情况调用工具，获得足够信息后直接给出答案。"""

SYSTEM_PROMPT_WITH_THOUGHT = """你是一个能使用工具的助手，可以根据情况调用工具，获得足够信息后直接给出答案。

重要：在每次回复中，请先输出你的思考过程（以"思考："开头），说明你当前的分析和决策理由，然后再决定是否调用工具或给出最终答案。"""


class SimpleAgent:
    """Agent 主循环类，处理多轮对话和工具调用"""

    def __init__(self, llm_client, tool_registry, max_history_rounds=10, show_thought=False):
        """初始化 SimpleAgent

        Args:
            llm_client: LLMClient 实例
            tool_registry: ToolRegistry 实例
            max_history_rounds: 最大保留的对话轮数（一轮=一问一答）
            show_thought: 是否显示思考过程
        """
        self.llm = llm_client
        self.tool_registry = tool_registry
        self.show_thought = show_thought
        self.max_history_rounds = max_history_rounds
        self.messages = []
        self._rebuild_system_message()

    def _rebuild_system_message(self):
        """根据 show_thought 状态重建系统消息"""
        prompt = SYSTEM_PROMPT_WITH_THOUGHT if self.show_thought else SYSTEM_PROMPT

        if self.messages and self.messages[0]["role"] == "system":
            self.messages[0] = {"role": "system", "content": prompt}
        else:
            self.messages.insert(0, {"role": "system", "content": prompt})

    def set_show_thought(self, enabled: bool):
        """设置是否显示思考过程

        Args:
            enabled: True 开启，False 关闭
        """
        self.show_thought = enabled
        self._rebuild_system_message()

    def reset(self):
        """重置对话上下文"""
        self._rebuild_system_message()
        self.messages = [self.messages[0]] if self.messages else [
            {"role": "system", "content": SYSTEM_PROMPT}
        ]

    def _trim_messages(self):
        """裁剪消息历史，保留最近的对话轮数"""
        if self.max_history_rounds == 0:
            return

        if len(self.messages) <= 1:
            return
        
        # 保留 system 消息
        system_msg = self.messages[0]
        history_msgs = self.messages[1:]
        
        # 每轮对话约2-4条消息（user + assistant + tool...），这里保守估计
        messages_per_round = 4
        max_keep = self.max_history_rounds * messages_per_round
        
        if len(history_msgs) > max_keep:
            # 保留最近的消息
            history_msgs = history_msgs[-max_keep:]
        
        self.messages = [system_msg] + history_msgs

    def run(self, user_input: str, max_iterations=10) -> str:
        """运行 Agent 主循环（单轮任务）

        Args:
            user_input (str): 用户输入
            max_iterations (int, optional): 最大迭代次数，默认为 10

        Returns:
            str: 最终回复
        """
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_input}
        ]
        
        tool_specs = self.tool_registry.get_all_openai_specs()
        
        for _ in range(max_iterations):
            response = self.llm.chat(messages, tools=tool_specs)
            
            if "tool_calls" not in response:
                return response["content"]
            
            messages.append(response)
            
            for tool_call in response["tool_calls"]:
                tool_name = tool_call["function"]["name"]
                tool_arguments = json.loads(tool_call["function"]["arguments"])
                
                print(f"[调用工具] {tool_name} 参数: {tool_arguments}")
                
                tool_result = self.tool_registry.execute(tool_name, tool_arguments)
                
                print(f"[工具结果] {tool_result}")
                
                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call["id"],
                    "name": tool_name,
                    "content": tool_result
                })
        
        return "已达到最大迭代次数，无法完成任务。"

    def chat(self, user_input: str, max_iterations=10) -> str:
        """多轮对话，保持上下文

        Args:
            user_input (str): 用户输入
            max_iterations (int, optional): 最大迭代次数，默认为 10

        Returns:
            str: 最终回复
        """
        self.messages.append({"role": "user", "content": user_input})
        
        self._trim_messages()
        
        tool_specs = self.tool_registry.get_all_openai_specs()
        
        for _ in range(max_iterations):
            response = self.llm.chat(self.messages, tools=tool_specs)
            
            if self.show_thought and response.get("content"):
                print(f"[思考] {response['content'].strip()}")
            
            if "tool_calls" not in response:
                self.messages.append(response)
                return response["content"]
            
            self.messages.append(response)
            
            for tool_call in response["tool_calls"]:
                tool_name = tool_call["function"]["name"]
                tool_arguments = json.loads(tool_call["function"]["arguments"])
                
                print(f"[调用工具] {tool_name} 参数: {tool_arguments}")
                
                tool_result = self.tool_registry.execute(tool_name, tool_arguments)
                
                print(f"[工具结果] {tool_result}")
                
                self.messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call["id"],
                    "name": tool_name,
                    "content": tool_result
                })
        
        return "已达到最大迭代次数，无法完成任务。"


if __name__ == "__main__":
    pass
