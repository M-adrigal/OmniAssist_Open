import json
import os
import re
import datetime

from llm import LLMClient
from tools import ToolRegistry


TOOL_BUILDER_SYSTEM_PROMPT = """# Tool 构建器系统提示

你是一个智能工具生成器。用户会用自然语言描述一个想要创建的工具，你需要将其转化为一个标准的工具定义 JSON 对象。

## 输出要求

1. 严格输出一个 JSON 对象，不要包含任何额外的解释、注释或 Markdown 标记。
2. JSON 必须包含以下字段：
   - name (string)：工具的唯一英文标识名，使用下划线命名法 (snake_case)。
   - description (string)：用中文简要描述工具的功能，让大模型能理解何时该调用它。
   - parameters (object)：符合 JSON Schema 规范的参数定义，必须包含 type: "object"、properties 和 required 字段。
   - execution_mode (string)：执行模式，根据工具性质选择：
     * "local_execution"：适用于需要精确计算、数学运算、数据处理、编码转换等场景。工具会通过生成并执行 Python 代码来得到精确结果。
     * "llm_simulated"：适用于信息查询、知识问答、内容生成、模拟预测等场景。工具通过 LLM 模拟返回结果。
     * "http_request"：适用于需要调用外部真实 API 获取数据的场景（如天气查询、股票查询、新闻获取等）。工具会发起真实的 HTTP 请求获取数据。
   - execution_prompt (string)：执行提示词模板。
      * 对于 local_execution 模式：提示词应指导如何编写 Python 代码来完成计算，要求将最终结果赋值给变量 `result`，并说明只需输出纯 Python 代码。
      * 对于 llm_simulated 模式：提示词应指导 LLM 如何模拟出合理的返回结果。
      * 对于 http_request 模式：提示词应指导 LLM 如何解析 API 返回的原始数据，将其转化为用户友好的自然语言回复。提示词中可使用 {api_response} 占位符代表 API 返回的原始数据。
      * 提示词中必须包含 {params} 占位符，在实际调用时会被替换为真实的参数键值对。
    - execution_code (string)：仅 local_execution 模式需要。直接给出可执行的 Python 代码，代码中可直接使用参数名作为变量（如 num1、num2、operation），将计算结果赋值给变量 result。不要包含 ``` 标记或任何解释文字。
    - http_config (object)：仅 http_request 模式需要。包含以下字段：
      * url (string)：API 地址模板，可使用 {参数名} 占位符，如 "https://api.weather.com/v1/current?city={city}"
      * method (string)：HTTP 请求方法，通常为 "GET" 或 "POST"
      * headers (object)：请求头，可使用 {参数名} 占位符，如 {"Authorization": "Bearer {api_key}"}
    - output_dir (string)：可选字段。如果工具会生成文件（Word、Excel、PDF、CSV、图片等），必须提供此字段。所有输出文件统一放在 `document_output/` 目录下，按文件类型使用子目录，格式为 `document_output/{type}_output`，例如：
     * Word文档 → `document_output/word_output`
     * Excel文件 → `document_output/excel_output`
     * PDF文件 → `document_output/pdf_output`
     * CSV文件 → `document_output/csv_output`
     * 图片文件 → `document_output/image_output`
     execution_code 中必须使用此 output_dir 作为文件保存路径，并使用 `os.makedirs(output_dir, exist_ok=True)` 确保目录存在。
    - dependencies (list)：可选字段。工具依赖的 pip 包列表，如 ["python-docx", "openpyxl"]。
3. 工具名称要简洁、表达功能核心，参数设计要合理、必要且最小化。
4. 如果用户描述模糊或缺少必要信息，请在参数和描述中做出合理推断，但不要额外询问。

## 执行模式选择指南

- 涉及精确数学计算（加减乘除、幂运算、三角函数等）→ local_execution
- 涉及数据处理、排序、统计、单位转换 → local_execution
- 涉及编码解码、格式化转换 → local_execution
- 涉及文件生成、文档输出（如 Word、Excel、PDF 等）→ local_execution
- 涉及需要实时外部数据的查询（天气、股票、汇率、新闻等）→ http_request
- 涉及信息查询、知识问答、内容生成 → llm_simulated
- 涉及内容生成、翻译、摘要 → llm_simulated

## 输出示例

用户输入："帮我创建一个可以查询任意城市实时天气的工具"

输出：
{
  "name": "get_weather",
  "description": "查询指定城市的实时天气情况",
  "parameters": {
    "type": "object",
    "properties": {
      "city": {
        "type": "string",
        "description": "城市名称，例如北京、上海、纽约"
      }
    },
    "required": ["city"]
  },
  "execution_mode": "http_request",
  "execution_prompt": "请根据以下API返回的天气数据，提取关键信息（天气状况、温度、湿度、风力等）并用自然语言回复用户。\nAPI返回数据：{api_response}\n用户查询的城市：{params}",
  "http_config": {
    "url": "https://api.weather.com/v1/current?city={city}",
    "method": "GET",
    "headers": {}
  }
}

用户输入："帮我创建一个计算器工具，可以进行加减乘除"

输出：
{
  "name": "simple_calculator",
  "description": "执行基本的加减乘除数学运算",
  "parameters": {
    "type": "object",
    "properties": {
      "operation": {
        "type": "string",
        "description": "运算类型",
        "enum": ["add", "subtract", "multiply", "divide"]
      },
      "num1": {
        "type": "number",
        "description": "第一个操作数"
      },
      "num2": {
        "type": "number",
        "description": "第二个操作数"
      }
    },
    "required": ["operation", "num1", "num2"]
  },
  "execution_mode": "local_execution",
  "execution_prompt": "请编写Python代码来执行数学运算。参数为：{params}。请根据operation的值执行对应运算（add为加、subtract为减、multiply为乘、divide为除），将计算结果赋值给变量result。只输出纯Python代码，不要包含任何解释或markdown标记。",
  "execution_code": "if operation == 'add':\n    result = num1 + num2\nelif operation == 'subtract':\n    result = num1 - num2\nelif operation == 'multiply':\n    result = num1 * num2\nelif operation == 'divide':\n    if num2 == 0:\n        result = '错误：除数不能为零'\n    else:\n        result = num1 / num2"
}

现在，请等待用户输入工具描述。"""


TOOL_REPAIR_PROMPT = """# Tool 修复器

你是一个工具修复专家。用户会提供一个**已有工具**的完整 JSON 定义，以及对该工具问题的描述。你需要**仅修复用户指出的具体问题**，输出修复后的完整工具 JSON。

## 核心原则

1. **保持工具名称不变**：name 字段必须与原始工具完全一致，不要改名。
2. **保持核心功能不变**：description 应保持原意，只修正不准确的描述。
3. **保持参数结构不变**：parameters 的 properties 和 required 应尽量保持，除非修复需要调整。
4. **只修改问题相关部分**：用户说哪里有问题就修哪里，不要画蛇添足。

## 常见修复场景

### 场景1：执行模式错误
如果工具用了 llm_simulated 但实际需要精确计算（如日期转换、数学运算），应改为 local_execution 并提供 execution_code。
如果工具需要调用真实 API 获取数据，应改为 http_request 并提供 http_config。

### 场景2：返回结果不准确
如果是 local_execution 模式，检查并修正 execution_code 中的算法逻辑。
如果是 llm_simulated 模式，优化 execution_prompt 使模拟结果更合理。

### 场景3：参数设计不合理
调整 parameters 定义，使参数更符合实际使用需求。

### 场景4：缺少依赖声明
如果 local_execution 模式的 execution_code 使用了第三方库（如 python-docx、openpyxl、reportlab、Pillow、pandas、matplotlib 等），必须添加 dependencies 字段声明这些包。

## 输出要求

1. 严格输出一个**完整的** JSON 对象，必须包含所有字段：name、description、parameters、execution_mode、execution_prompt。
2. 如果是 local_execution 模式，还必须包含 execution_code 字段。如果使用了第三方库，还必须包含 dependencies 字段。
3. 如果是 http_request 模式，还必须包含 http_config 字段。
4. 不要只输出修改的部分，要输出修复后的完整工具定义。
5. 不要包含任何额外的解释、注释或 Markdown 标记。

现在，请等待用户提供原始工具定义和修复要求。"""


TOOL_ANALYZER_PROMPT = """# Tool 需求分析器

你是一个工具需求分析器。用户描述了一个想要创建的工具，你需要分析这个需求，判断是否需要用户提供额外信息才能完成工具创建。

## 分析规则

1. 如果工具需要调用外部 API（如天气、股票、汇率、新闻等实时数据），**优先尝试自主确定 API 细节**：
   - 利用你的知识储备，优先选择免费公开的 API（如 wttr.in 用于天气、api.exchangerate-api.com 用于汇率等）。
   - 只有在确实无法确定 API 端点、或该服务必须使用 API 密钥且用户未提供时，才向用户提问。
   - 提问时要精准、具体，告诉用户需要什么以及如何获取（如注册地址）。

2. 如果工具是纯计算、纯本地处理（如计算器、编码转换、文件生成、文档输出、邮件发送），则不需要额外信息，可以直接生成。

3. 如果用户描述不够清晰（如只说"创建一个工具"），也需要追问具体功能。

4. 文件生成类工具（Word、Excel、PDF、CSV、图片等）属于 local_execution 模式，可以直接生成，无需询问用户。

## 输出格式

严格输出一个 JSON 对象：

如果需要询问用户，输出：
{
  "need_info": true,
  "questions": ["问题1", "问题2", ...],
  "suggested_params": ["建议的参数列表"]
}

如果不需要额外信息，可以直接生成，输出：
{
  "need_info": false,
  "execution_mode": "local_execution 或 llm_simulated 或 http_request",
  "reason": "简要说明为什么选择这个模式"
}

现在，请等待用户输入工具描述。"""


TOOL_SMART_GENERATOR_PROMPT = """# 智能工具生成器

你是一个智能工具生成器。用户会用自然语言描述一个想要创建的工具，你需要**尽最大努力自主完成工具的创建**，尽量减少对用户的提问。

## 核心原则

1. **自主推断优先**：利用你的知识储备，尽可能自主确定 API 地址、请求方式、参数格式、响应解析等所有细节。
2. **优先使用免费公开 API**：优先选择无需 API 密钥的免费公开 API。以下是已知可用的免费 API：
   - 天气：wttr.in，格式 https://wttr.in/{city}?format=j1（返回 JSON，无需密钥）
   - 汇率：api.exchangerate-api.com/v4/latest/{base_currency}（无需密钥）
   - IP 查询：ipapi.co/{ip}/json/（无需密钥）
   - 随机事实：uselessfacts.jsph.pl/random.json?language=en（无需密钥）
   - 名言：api.quotable.io/random（无需密钥）
   - 国家信息：restcountries.com/v3.1/name/{country}（无需密钥）
   - 大学列表：universities.hipolabs.com/search?name={name}（无需密钥）
   - 猫咪图片：api.thecatapi.com/v1/images/search（无需密钥）
   - 狗狗图片：dog.ceo/api/breeds/image/random（无需密钥）
3. **最少提问原则**：只有在确实无法确定关键信息时才向用户提问，且每次只问最关键的问题。
4. **智能解析**：对于 API 返回的数据，你应能自主设计 execution_prompt 中的解析逻辑，无需用户描述返回格式。
5. **参数设计合理**：工具的参数应尽量简单，让用户只需提供最自然的输入（如城市名、日期等），技术细节由工具内部处理。
6. **execution_mode 严格限制**：execution_mode 字段只能是以下三个值之一，不得使用其他任何名称：
   - "local_execution"：需要执行 Python 代码来完成任务的场景（如计算、文件生成、数据处理、编码转换等）。**此模式必须提供 execution_code 字段**，包含可直接执行的 Python 代码。
   - "http_request"：需要调用外部 API 获取数据的场景（如天气、汇率、新闻等）。**此模式必须提供 http_config 字段**。
   - "llm_simulated"：纯知识问答、内容生成等不需要代码执行也不需要外部 API 的场景。
7. **output_dir 字段（文件输出类工具专用）**：如果工具会生成文件（Word、Excel、PDF、CSV、图片等），必须包含 `output_dir` 字段。所有输出文件统一放在 `document_output/` 目录下，按文件类型使用子目录，格式为 `document_output/{type}_output`，例如：
   - Word文档 → `document_output/word_output`
   - Excel文件 → `document_output/excel_output`
   - PDF文件 → `document_output/pdf_output`
   - CSV文件 → `document_output/csv_output`
   - 图片文件 → `document_output/image_output`
   execution_code 中必须使用此 output_dir 作为文件保存路径，并使用 `os.makedirs(output_dir, exist_ok=True)` 确保目录存在。

## 输出格式

### 情况1：可以自主完成

如果你能自主确定所有细节，直接输出完整的工具 JSON。

**local_execution 模式示例（Word 文档生成）：**

{
  "success": true,
  "tool": {
    "name": "generate_word_document",
    "description": "将文本内容生成Word文档并保存到指定路径",
    "parameters": {
      "type": "object",
      "properties": {
        "content": {
          "type": "string",
          "description": "要写入文档的文本内容"
        },
        "title": {
          "type": "string",
          "description": "文档标题"
        }
      },
      "required": ["content", "title"]
    },
    "execution_mode": "local_execution",
    "execution_prompt": "请编写Python代码，使用python-docx库生成Word文档。参数为：{params}。将生成的文档保存到指定路径，并将保存路径赋值给变量result。只输出纯Python代码。",
    "execution_code": "from docx import Document\\nimport os\\n\\noutput_dir = os.path.join('document_output', 'word_output')\\nos.makedirs(output_dir, exist_ok=True)\\nfilepath = os.path.join(output_dir, f'{title}.docx')\\n\\ndoc = Document()\\ndoc.add_heading(title, level=1)\\ndoc.add_paragraph(content)\\ndoc.save(filepath)\\nresult = f'文档已保存至: {filepath}'",
    "dependencies": ["python-docx"],
    "output_dir": "document_output/word_output"
  }
}

**http_request 模式示例（天气查询）：**

{
  "success": true,
  "tool": {
    "name": "get_weather",
    "description": "查询指定城市的实时天气情况",
    "parameters": {
      "type": "object",
      "properties": {
        "city": {
          "type": "string",
          "description": "城市名称，例如北京、上海、纽约"
        }
      },
      "required": ["city"]
    },
    "execution_mode": "http_request",
    "execution_prompt": "请根据以下API返回的天气数据，提取关键信息（天气状况、温度、湿度、风力等）并用自然语言回复用户。\\nAPI返回数据：{api_response}\\n用户查询的城市：{params}",
    "http_config": {
      "url": "https://wttr.in/{city}?format=j1",
      "method": "GET",
      "headers": {}
    }
  }
}

### 情况2：需要用户提供信息

如果确实需要用户提供某些关键信息（如 API 密钥），输出：

{
  "success": false,
  "need_info": true,
  "reason": "该天气服务需要API密钥才能使用",
  "questions": [
    "请提供您的 OpenWeatherMap API 密钥（可在 https://openweathermap.org/api 免费注册获取）"
  ],
  "partial_tool": {
    "name": "get_weather",
    "description": "查询指定城市的实时天气情况",
    "parameters": {
      "type": "object",
      "properties": {
        "city": {"type": "string", "description": "城市名称"},
        "api_key": {"type": "string", "description": "OpenWeatherMap API密钥"}
      },
      "required": ["city", "api_key"]
    },
    "execution_mode": "http_request",
    "execution_prompt": "请根据以下API返回的天气数据，提取关键信息并用自然语言回复用户。\\nAPI返回数据：{api_response}\\n用户查询的城市：{params}",
    "http_config": {
      "url": "https://api.openweathermap.org/data/2.5/weather?q={city}&appid={api_key}&units=metric&lang=zh_cn",
      "method": "GET",
      "headers": {}
    }
  }
}

注意：partial_tool 中应包含你已经能确定的所有字段，方便后续补全。

现在，请等待用户输入工具描述。"""


class ToolBuilder:
    """Tool 构建器：将自然语言描述转化为底座可用的工具定义"""

    def __init__(self, llm_client: LLMClient):
        """初始化 ToolBuilder

        Args:
            llm_client: LLMClient 实例，用于调用大模型生成工具定义
        """
        self.llm_client = llm_client

    def _extract_json(self, text: str) -> str:
        """从 LLM 返回的文本中提取 JSON 字符串

        处理可能包裹在 ```json ... ``` 代码块中的 JSON，
        以及前后可能存在的多余空白字符。

        Args:
            text: LLM 返回的原始文本

        Returns:
            str: 提取出的纯 JSON 字符串
        """
        text = text.strip()

        json_block_pattern = r'```(?:json)?\s*\n?(.*?)\n?```'
        match = re.search(json_block_pattern, text, re.DOTALL)
        if match:
            return match.group(1).strip()

        return text

    def analyze_requirements(self, user_description: str) -> dict:
        """分析用户需求，判断是否需要额外信息

        Args:
            user_description: 用户的工具描述

        Returns:
            dict: 包含 need_info、questions 等字段的分析结果

        Raises:
            ValueError: 当 LLM 返回无法解析时抛出
        """
        messages = [
            {"role": "system", "content": TOOL_ANALYZER_PROMPT},
            {"role": "user", "content": user_description}
        ]

        response = self.llm_client.chat(messages, temperature=0.2)
        raw_content = response.get("content", "")

        if not raw_content:
            raise ValueError("LLM 返回了空内容，无法分析需求")

        json_str = self._extract_json(raw_content)

        try:
            result = json.loads(json_str)
        except json.JSONDecodeError as e:
            raise ValueError(f"无法解析需求分析结果：{e}\n原始内容：{raw_content}")

        return result

    def smart_generate(self, user_description: str) -> dict:
        """智能生成工具：优先自主完成所有技术细节，尽量减少用户提问

        与 analyze_requirements + generate_tool 的两步流程不同，
        本方法尝试一次性生成完整的工具定义（包括 http_config 等细节），
        利用 LLM 的知识储备自主确定 API 端点、请求方式、响应解析等。

        Args:
            user_description: 用户的工具描述

        Returns:
            dict: 包含 success 字段的结果。
                  成功时：{"success": true, "tool": {...完整工具JSON...}}
                  需用户输入时：{"success": false, "need_info": true, "questions": [...], "partial_tool": {...}}

        Raises:
            ValueError: 当 LLM 返回无法解析时抛出
        """
        messages = [
            {"role": "system", "content": TOOL_SMART_GENERATOR_PROMPT},
            {"role": "user", "content": user_description}
        ]

        response = self.llm_client.chat(messages, temperature=0.2)
        raw_content = response.get("content", "")

        if not raw_content:
            raise ValueError("LLM 返回了空内容，无法智能生成工具")

        json_str = self._extract_json(raw_content)

        try:
            result = json.loads(json_str)
        except json.JSONDecodeError as e:
            raise ValueError(f"无法解析智能生成结果：{e}\n原始内容：{raw_content}")

        if not isinstance(result, dict):
            raise ValueError(f"智能生成返回的不是 JSON 对象，而是 {type(result).__name__}")

        if "success" not in result:
            result["success"] = False
            result["need_info"] = True
            result["questions"] = ["请更详细地描述您需要的工具功能"]
            result["reason"] = "无法确定生成结果"

        return result

    def generate_tool(self, user_description: str) -> dict:
        """根据用户的自然语言描述生成工具 JSON

        Args:
            user_description: 用户的描述，如"创建一个能计算两数之和的工具"

        Returns:
            dict: 包含 name、description、parameters、execution_mode、execution_prompt 的工具定义

        Raises:
            ValueError: 当 LLM 返回的内容无法解析为合法 JSON 时抛出
        """
        messages = [
            {"role": "system", "content": TOOL_BUILDER_SYSTEM_PROMPT},
            {"role": "user", "content": user_description}
        ]

        response = self.llm_client.chat(messages, temperature=0.2)
        raw_content = response.get("content", "")

        if not raw_content:
            raise ValueError("LLM 返回了空内容，无法生成工具定义")

        json_str = self._extract_json(raw_content)

        try:
            tool_json = json.loads(json_str)
        except json.JSONDecodeError as e:
            raise ValueError(f"无法解析 LLM 返回的 JSON：{e}\n原始内容：{raw_content}")

        if not isinstance(tool_json, dict):
            raise ValueError(f"LLM 返回的 JSON 不是一个对象，而是 {type(tool_json).__name__}")

        return tool_json

    def repair_tool(self, original_tool: dict, repair_description: str) -> dict:
        """修复已有工具，仅修改用户指出的问题

        使用创建工具的完整 Prompt 确保输出格式规范，但用户消息指明这是修复任务。

        Args:
            original_tool: 原始工具的完整 JSON 定义
            repair_description: 用户对问题的描述

        Returns:
            dict: 修复后的完整工具 JSON

        Raises:
            ValueError: 当 LLM 返回的内容无法解析时抛出
        """
        user_message = (
            f"你需要修复一个已有工具，而不是创建新工具。\n\n"
            f"原始工具定义：\n{json.dumps(original_tool, ensure_ascii=False, indent=2)}\n\n"
            f"需要修复的问题：{repair_description}\n\n"
            f"重要：修复后的工具名称必须保持为 '{original_tool.get('name', '')}'，不要改名。"
            f"请输出修复后的完整工具 JSON，包含所有必须字段。"
        )

        messages = [
            {"role": "system", "content": TOOL_BUILDER_SYSTEM_PROMPT},
            {"role": "user", "content": user_message}
        ]

        response = self.llm_client.chat(messages, temperature=0.2)
        raw_content = response.get("content", "")

        if not raw_content:
            raise ValueError("LLM 返回了空内容，无法修复工具")

        json_str = self._extract_json(raw_content)

        try:
            tool_json = json.loads(json_str)
        except json.JSONDecodeError as e:
            raise ValueError(f"无法解析 LLM 返回的 JSON：{e}\n原始内容：{raw_content}")

        if not isinstance(tool_json, dict):
            raise ValueError(f"LLM 返回的 JSON 不是一个对象，而是 {type(tool_json).__name__}")

        return tool_json

    def validate_tool_json(self, tool_json: dict) -> tuple:
        """校验生成的 JSON 是否包含所有必须字段，格式是否正确

        Args:
            tool_json: 待校验的工具定义字典

        Returns:
            tuple: (is_valid: bool, message: str)
                   - (True, "") 表示校验通过
                   - (False, "错误原因") 表示校验失败
        """
        required_fields = ["name", "description", "parameters", "execution_mode", "execution_prompt"]
        for field in required_fields:
            if field not in tool_json:
                return False, f"缺少必须字段：'{field}'"
            if not tool_json[field]:
                return False, f"字段 '{field}' 不能为空"

        name = tool_json["name"]
        if not isinstance(name, str):
            return False, f"字段 'name' 必须是字符串类型，当前为 {type(name).__name__}"

        snake_case_pattern = r'^[a-z][a-z0-9]*(_[a-z0-9]+)*$'
        if not re.match(snake_case_pattern, name):
            return False, f"工具名称 '{name}' 不符合 snake_case 命名规范"

        parameters = tool_json["parameters"]
        if not isinstance(parameters, dict):
            return False, f"字段 'parameters' 必须是字典类型，当前为 {type(parameters).__name__}"

        if parameters.get("type") != "object":
            return False, "parameters 中缺少 'type' 字段或其值不是 'object'"

        if "properties" not in parameters:
            return False, "parameters 中缺少 'properties' 字段"

        properties = parameters["properties"]
        if not isinstance(properties, dict):
            return False, f"parameters.properties 必须是字典类型，当前为 {type(properties).__name__}"

        if "required" in parameters:
            required = parameters["required"]
            if not isinstance(required, list):
                return False, f"parameters.required 必须是列表类型，当前为 {type(required).__name__}"
            for req_param in required:
                if req_param not in properties:
                    return False, f"required 中的参数 '{req_param}' 在 properties 中未定义"

        execution_mode = tool_json["execution_mode"]
        if not isinstance(execution_mode, str):
            return False, f"字段 'execution_mode' 必须是字符串类型"

        _mode_aliases = {
            "code_interpreter": "local_execution",
            "code": "local_execution",
            "python": "local_execution",
            "local": "local_execution",
            "simulated": "llm_simulated",
            "llm": "llm_simulated",
            "simulate": "llm_simulated",
            "http": "http_request",
            "api": "http_request",
            "request": "http_request",
        }
        if execution_mode in _mode_aliases:
            tool_json["execution_mode"] = _mode_aliases[execution_mode]
            execution_mode = tool_json["execution_mode"]

        if execution_mode not in ("llm_simulated", "local_execution", "http_request"):
            return False, f"字段 'execution_mode' 的值必须是 'llm_simulated'、'local_execution' 或 'http_request'，当前为 '{execution_mode}'"

        if execution_mode == "local_execution":
            execution_code = tool_json.get("execution_code", "")
            if not execution_code or not isinstance(execution_code, str):
                return False, "local_execution 模式必须提供 'execution_code' 字段且不能为空"

        if execution_mode == "http_request":
            http_config = tool_json.get("http_config")
            if not http_config or not isinstance(http_config, dict):
                return False, "http_request 模式必须提供 'http_config' 字段且为字典类型"
            if "url" not in http_config or not http_config["url"]:
                return False, "http_config 中必须包含 'url' 字段且不能为空"
            if "method" not in http_config:
                http_config["method"] = "GET"

        execution_prompt = tool_json["execution_prompt"]
        if not isinstance(execution_prompt, str):
            return False, f"字段 'execution_prompt' 必须是字符串类型"

        if "{params}" not in execution_prompt and "{" not in execution_prompt:
            param_count = len(properties)
            if param_count > 0:
                return False, "execution_prompt 中缺少参数占位符（如 {参数名}）"

        return True, ""

    @staticmethod
    def _infer_category(tool_json: dict) -> str:
        """根据工具定义推断分类

        Args:
            tool_json: 工具定义字典

        Returns:
            str: 分类名称
        """
        mode = tool_json.get("execution_mode", "")
        name = tool_json.get("name", "").lower()
        desc = tool_json.get("description", "").lower()
        output_dir = tool_json.get("output_dir", "")

        if output_dir or any(kw in name for kw in ["save_to_", "generate_", "export_", "create_"]):
            if "word" in output_dir or "word" in name:
                return "document"
            if "excel" in output_dir or "excel" in name:
                return "document"
            if "pdf" in output_dir or "pdf" in name:
                return "document"
            if "ppt" in output_dir or "ppt" in name:
                return "document"
            return "document"

        weather_kw = ["weather", "天气", "qweather", "forecast"]
        if any(kw in name for kw in weather_kw):
            return "weather"

        web_kw = ["web", "fetch", "http", "url", "网页", "抓取"]
        if any(kw in name for kw in web_kw):
            return "web"

        if mode == "http_request":
            return "web"

        return "utility"

    @staticmethod
    def _extract_keywords(tool_json: dict) -> list:
        """从工具定义中提取关键词

        Args:
            tool_json: 工具定义字典

        Returns:
            list: 关键词列表
        """
        keywords = set()
        desc = tool_json.get("description", "")
        name = tool_json.get("name", "")

        for word in re.findall(r'[\u4e00-\u9fff]{2,}', desc):
            if len(word) <= 4:
                keywords.add(word)

        params = tool_json.get("parameters", {}).get("properties", {})
        for param_name in params:
            keywords.add(param_name)

        return list(keywords)[:15]

    def save_tool_to_file(self, tool_json: dict, tools_dir: str = None,
                          registry=None) -> str:
        """将生成的 Tool JSON 保存到指定目录下的文件中

        文件名格式为 {tool_name}.json，同时自动添加 created_at 时间戳。
        如果提供了 registry，会自动同步工具清单。

        Args:
            tool_json: 工具定义字典
            tools_dir: 保存目录路径，默认为当前文件所在目录下的 agent_tools/
            registry: 可选的 ToolRegistry 实例，用于自动同步清单

        Returns:
            str: 保存的文件完整路径

        Raises:
            ValueError: 当 tool_json 校验不通过时抛出
            IOError: 当目录创建或文件写入失败时抛出
        """
        if tools_dir is None:
            base_dir = os.path.dirname(os.path.abspath(__file__))
            tools_dir = os.path.join(base_dir, "agent_tools")

        valid, msg = self.validate_tool_json(tool_json)
        if not valid:
            raise ValueError(f"Tool JSON 校验不通过，无法保存：{msg}")

        os.makedirs(tools_dir, exist_ok=True)

        tool_json["created_at"] = datetime.datetime.now().isoformat()

        filename = f"{tool_json['name']}.json"
        filepath = os.path.join(tools_dir, filename)

        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(tool_json, f, ensure_ascii=False, indent=2)

        if registry is not None:
            category = self._infer_category(tool_json)
            keywords = self._extract_keywords(tool_json)
            registry.add_to_manifest(
                name=tool_json["name"],
                description=tool_json.get("description", ""),
                category=category,
                keywords=keywords,
                file=filename,
            )

        print(f"[保存 Tool] {tool_json['name']} → {filepath}")
        return filepath


def create_simulated_executor(tool_name: str, execution_prompt: str, llm_client: LLMClient):
    """创建一个用于 LLM 模拟执行的函数

    当工具被调用时，该函数会将 execution_prompt 中的 {参数名} 占位符替换为实际值，
    然后请求 LLM 生成模拟结果。

    Args:
        tool_name: 工具名称，用于错误信息标识
        execution_prompt: 执行提示词模板，包含 {参数名} 占位符
        llm_client: LLMClient 实例，用于调用大模型

    Returns:
        callable: 一个可调用的函数，接收 **kwargs 参数，返回字符串结果
    """

    def executor(**kwargs) -> str:
        """模拟执行函数

        Args:
            **kwargs: 工具调用时传入的参数键值对

        Returns:
            str: LLM 模拟执行的结果文本
        """
        prompt = execution_prompt
        for key, value in kwargs.items():
            placeholder = "{" + key + "}"
            prompt = prompt.replace(placeholder, str(value))

        messages = [
            {"role": "user", "content": prompt}
        ]

        try:
            response = llm_client.chat(messages, temperature=0.3)
            return response.get("content", "")
        except Exception as e:
            return f"[工具 '{tool_name}' 模拟执行失败] 错误信息：{str(e)}"

    return executor


def create_local_executor(tool_name: str, execution_code: str, dependencies: list = None, sandbox=None):
    """创建一个用于本地执行 Python 代码的函数

    工具代码在沙箱子进程中执行，与主服务完全隔离：
    - 依赖安装在独立的 venv 中，不污染宿主环境
    - 子进程崩溃不影响主服务
    - 30 秒超时保护，防止死循环

    Args:
        tool_name: 工具名称，用于错误信息标识
        execution_code: 预生成的 Python 代码，可直接使用参数名作为变量
        dependencies: 需要安装的 pip 包列表，如 ["python-docx", "openpyxl"]
        sandbox: 可选的共享 ToolSandbox 实例，为 None 时自动创建

    Returns:
        callable: 一个可调用的函数，接收 **kwargs 参数，返回字符串结果
    """
    from sandbox import ToolSandbox

    if sandbox is None:
        try:
            sandbox = ToolSandbox()
        except Exception as e:
            print(f"[警告] 工具 '{tool_name}' 沙箱创建失败: {e}")
            sandbox = None
    _sandbox = sandbox

    def executor(**kwargs) -> str:
        if _sandbox is None:
            return f"[工具 '{tool_name}' 执行失败] 沙箱环境不可用，工具执行功能暂不可用"
        user_id = kwargs.pop('_user_id', None)
        try:
            return _sandbox.execute(execution_code, kwargs, user_id=user_id)
        except Exception as e:
            return f"[工具 '{tool_name}' 执行异常] {type(e).__name__}: {str(e)}"

    def _wrapped_executor(**kwargs) -> str:
        result = executor(**kwargs)
        if _sandbox is not None and "ModuleNotFoundError" in result and dependencies:
            import re as _re
            match = _re.search(r"No module named '(\w+)'", result)
            if match:
                missing = match.group(1)
                print(f"[工具 '{tool_name}'] 检测到缺失依赖 '{missing}'，正在自动安装...")
                if _sandbox.install([missing]):
                    print(f"[工具 '{tool_name}'] 依赖安装完成，重试执行...")
                    return executor(**kwargs)
                else:
                    print(f"[工具 '{tool_name}'] 自动安装失败")
        if result.startswith("[沙箱执行失败]") or result.startswith("[沙箱执行超时]") or result.startswith("[沙箱异常]"):
            return result
        return result

    return _wrapped_executor


_SSRF_BLOCKED_HOSTS = {
    "127.0.0.1", "0.0.0.0", "169.254.169.254",
    "localhost", "metadata.google.internal",
}
_SSRF_BLOCKED_NETWORKS = [
    (0x0A000000, 0xFF000000),
    (0xAC100000, 0xFFF00000),
    (0xC0A80000, 0xFFFF0000),
    (0x7F000000, 0xFF000000),
    (0xA9FE0000, 0xFFFF0000),
]


def _validate_url(url: str) -> tuple:
    import ipaddress
    import urllib.parse

    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return False, f"不支持的协议: {parsed.scheme}，仅允许 http/https"

    host = parsed.hostname
    if not host:
        return False, "URL 中无法解析主机名"

    if host.lower() in _SSRF_BLOCKED_HOSTS:
        return False, f"禁止访问目标主机: {host}"

    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        addr = None

    if addr is not None:
        if addr.is_loopback or addr.is_link_local or addr.is_multicast or addr.is_unspecified:
            return False, f"禁止访问目标地址: {host}"
        if addr.is_private:
            return False, f"禁止访问内网地址: {host}"
        addr_int = int(addr)
        for network, mask in _SSRF_BLOCKED_NETWORKS:
            if addr_int & mask == network:
                return False, f"禁止访问目标地址: {host}"

    return True, ""


def create_http_executor(tool_name: str, http_config: dict,
                         execution_prompt: str, llm_client: LLMClient,
                         response_formatter: str = None):
    """创建一个用于发起 HTTP 请求并解析结果的执行函数

    Args:
        tool_name: 工具名称
        http_config: HTTP 配置，包含 url、method、headers
        execution_prompt: 执行提示词模板，用于指导 LLM 解析 API 返回数据
        llm_client: LLMClient 实例，用于解析 API 返回数据
        response_formatter: 可选的 Python 格式化函数代码，直接格式化 API 响应，绕过 LLM

    Returns:
        callable: 一个可调用的函数，接收 **kwargs 参数，返回字符串结果
    """
    import urllib.request
    import urllib.error
    import urllib.parse
    import gzip
    import re

    def executor(**kwargs) -> str:
        url = http_config.get("url", "")
        method = http_config.get("method", "GET").upper()
        headers = http_config.get("headers", {})

        for key, value in kwargs.items():
            placeholder = "{" + key + "}"
            encoded_value = urllib.parse.quote(str(value), safe='')
            url = url.replace(placeholder, encoded_value)
            headers = {
                k: v.replace("{" + key + "}", str(value))
                for k, v in headers.items()
            }

        url = re.sub(r'&[^&=?]*\{[^}]*\}', '', url)
        url = re.sub(r'\{[^}]*\}', '', url)

        valid, err_msg = _validate_url(url)
        if not valid:
            return f"[工具 '{tool_name}' SSRF防护] {err_msg}"

        try:
            req = urllib.request.Request(url, method=method)
            for k, v in headers.items():
                req.add_header(k, v)

            with urllib.request.urlopen(req, timeout=10) as resp:
                raw_bytes = resp.read()
                try:
                    raw_data = gzip.decompress(raw_bytes).decode('utf-8')
                except (gzip.BadGzipFile, OSError):
                    raw_data = raw_bytes.decode('utf-8')
        except urllib.error.HTTPError as e:
            return f"[工具 '{tool_name}' HTTP请求失败] HTTP {e.code}: {e.reason}"
        except Exception as e:
            return f"[工具 '{tool_name}' HTTP请求失败] 错误信息：{str(e)}"

        if response_formatter:
            try:
                local_vars = {"raw_data": raw_data, "kwargs": kwargs, "json": __import__("json")}
                exec(response_formatter, {"__builtins__": {}}, local_vars)
                return local_vars.get("result", raw_data)
            except Exception as e:
                pass

        prompt = execution_prompt
        prompt = prompt.replace("{api_response}", raw_data)
        params_str = ", ".join(f"{k}={v}" for k, v in kwargs.items())
        prompt = prompt.replace("{params}", params_str)

        messages = [
            {"role": "user", "content": prompt}
        ]

        try:
            response = llm_client.chat(messages, temperature=0.3)
            return response.get("content", raw_data)
        except Exception as e:
            return raw_data

    return executor


def _extract_code_block(text: str) -> str:
    """从 LLM 返回的文本中提取 Python 代码块

    Args:
        text: LLM 返回的原始文本

    Returns:
        str: 提取出的纯 Python 代码
    """
    text = text.strip()

    pattern = r'```(?:python)?\s*\n?(.*?)\n?```'
    match = re.search(pattern, text, re.DOTALL)
    if match:
        return match.group(1).strip()

    return text


if __name__ == "__main__":
    from llm import LLMClient
    from tools import ToolRegistry

    llm_client = LLMClient()

    builder = ToolBuilder(llm_client)

    user_input = "帮我创建一个可以查询任意城市实时天气的工具"

    tool_json = builder.generate_tool(user_input)
    print("生成的工具 JSON：")
    print(json.dumps(tool_json, ensure_ascii=False, indent=2))

    valid, msg = builder.validate_tool_json(tool_json)
    print(f"校验结果: {'通过' if valid else '失败'} - {msg}")

    if valid:
        filepath = builder.save_tool_to_file(tool_json, tools_dir="agent_tools")
        print(f"Tool 已保存到文件：{filepath}")

    registry = ToolRegistry()
    registry.load_tools_from_dir(
        tools_dir="agent_tools",
        func_factory=lambda name, prompt: create_simulated_executor(name, prompt, llm_client)
    )

    available = ToolRegistry.list_available_tools("agent_tools")
    print(f"\n当前可用的 Tool：")
    for name, desc in available:
        print(f"  - {name}: {desc}")
