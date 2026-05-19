import os
import json
from fastapi import APIRouter, HTTPException, Request, Query
from server.models import ToolInfo, ToolCreate, ToolUpdate
from server.routes.auth import require_permission, get_current_user
from server.database import log_tool_operation, get_tool_operation_logs

router = APIRouter(prefix="/api/tools", tags=["tools"])


def get_tools_dir():
    base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.join(base_dir, "agent", "agent_tools")


def get_dependencies():
    try:
        from __main__ import get_tool_registry, get_tool_builder, get_llm_client, get_config, get_agent
    except ImportError:
        from server.main import get_tool_registry, get_tool_builder, get_llm_client, get_config, get_agent
    return get_tool_registry(), get_tool_builder(), get_llm_client(), get_config(), get_agent()


def _run_self_test(tool_json: dict, executor) -> dict:
    """运行工具自测并返回结构化结果

    Args:
        tool_json: 工具 JSON 定义
        executor: 工具执行函数

    Returns:
        dict: {"passed": bool, "message": str}
    """
    from agent.main import _self_test_tool
    import io

    old_stdout = None
    try:
        old_stdout = getattr(_self_test_tool, "__wrapped_stdout__", None)
    except Exception:
        pass

    try:
        import sys as _sys
        capture = io.StringIO()
        original_stdout = _sys.stdout
        _sys.stdout = capture
        try:
            _self_test_tool(tool_json, executor)
        finally:
            _sys.stdout = original_stdout
        output = capture.getvalue().strip()
    except Exception as e:
        return {"passed": False, "message": f"自测异常: {str(e)}"}

    if output:
        lines = output.split("\n")
        for line in lines:
            if "通过" in line:
                return {"passed": True, "message": output}
            if "失败" in line or "警告" in line:
                return {"passed": False, "message": output}
        return {"passed": True, "message": output}
    return {"passed": True, "message": "自测完成（无输出）"}


@router.get("", response_model=list[ToolInfo])
def list_tools():
    tools_dir = get_tools_dir()
    if not os.path.isdir(tools_dir):
        return []

    tools = []
    for filename in sorted(os.listdir(tools_dir)):
        if not filename.endswith(".json"):
            continue
        filepath = os.path.join(tools_dir, filename)
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                data = json.load(f)
            tools.append(ToolInfo(
                name=data.get("name", ""),
                description=data.get("description", ""),
                parameters=data.get("parameters", {}),
                execution_mode=data.get("execution_mode", ""),
                output_dir=data.get("output_dir"),
                dependencies=data.get("dependencies"),
            ))
        except (json.JSONDecodeError, IOError):
            continue

    return tools


@router.post("", response_model=dict)
def create_tool(body: ToolCreate, request: Request):
    user = require_permission(request, "tools", "write")
    registry, builder, llm, config, agent = get_dependencies()
    tools_dir = get_tools_dir()

    description = body.description.strip()
    if not description:
        raise HTTPException(status_code=400, detail="描述不能为空")

    existing = [(name, info.get("description", ""))
                for name, info in registry.list_tools().items()]

    from agent.main import _check_duplicate_tool
    dup_check = _check_duplicate_tool(description, existing, llm)
    if dup_check.get("is_duplicate"):
        log_tool_operation(
            tool_name=dup_check.get("matched_tool", ""),
            operation="create_duplicate",
            operator=user.get("username", ""),
            operator_id=user.get("id", 0),
            details=json.dumps({"description": description, "reason": dup_check.get("reason", "")}, ensure_ascii=False)
        )
        return {
            "success": False,
            "duplicate": True,
            "matched_tool": dup_check.get("matched_tool"),
            "reason": dup_check.get("reason"),
        }

    try:
        smart_result = builder.smart_generate(description)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"工具生成失败: {str(e)}")

    if not smart_result.get("success"):
        return {
            "success": False,
            "need_info": smart_result.get("need_info", False),
            "questions": smart_result.get("questions", []),
            "reason": smart_result.get("reason", ""),
        }

    tool_json = smart_result["tool"]
    valid, msg = builder.validate_tool_json(tool_json)
    if not valid:
        try:
            tool_json = builder.repair_tool(tool_json, f"校验失败原因：{msg}")
            tool_json["name"] = tool_json.get("name", "")
            valid, msg = builder.validate_tool_json(tool_json)
        except Exception:
            pass
        if not valid:
            raise HTTPException(status_code=500, detail=f"工具定义校验失败: {msg}")

    try:
        builder.save_tool_to_file(tool_json, tools_dir=tools_dir, registry=registry)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"保存失败: {str(e)}")

    from agent.main import _ensure_output_dir, _create_executor
    _ensure_output_dir(tool_json)

    executor = _create_executor(
        tool_json["name"], tool_json["execution_prompt"],
        tool_json["execution_mode"],
        tool_json.get("execution_code", ""),
        tool_json.get("http_config", {}),
        llm,
        tool_json.get("dependencies"),
        tool_json.get("response_formatter")
    )
    registry.register_tool(
        name=tool_json["name"],
        description=tool_json["description"],
        parameters=tool_json["parameters"],
        func=executor
    )

    self_test_result = _run_self_test(tool_json, executor)

    log_tool_operation(
        tool_name=tool_json["name"],
        operation="create",
        operator=user.get("username", ""),
        operator_id=user.get("id", 0),
        details=json.dumps({
            "description": tool_json.get("description", ""),
            "execution_mode": tool_json.get("execution_mode", ""),
            "dependencies": tool_json.get("dependencies", []),
            "self_test_passed": self_test_result.get("passed"),
        }, ensure_ascii=False)
    )

    agent.reset()

    return {
        "success": True,
        "tool": ToolInfo(
            name=tool_json["name"],
            description=tool_json["description"],
            parameters=tool_json["parameters"],
            execution_mode=tool_json["execution_mode"],
            output_dir=tool_json.get("output_dir"),
            dependencies=tool_json.get("dependencies"),
        ).model_dump(),
        "self_test": self_test_result,
    }


@router.put("/{tool_name}", response_model=dict)
def update_tool(tool_name: str, body: ToolUpdate, request: Request):
    user = require_permission(request, "tools", "write")
    registry, builder, llm, config, agent = get_dependencies()
    tools_dir = get_tools_dir()

    filepath = os.path.join(tools_dir, f"{tool_name}.json")
    if not os.path.isfile(filepath):
        raise HTTPException(status_code=404, detail=f"工具 '{tool_name}' 不存在")

    try:
        with open(filepath, "r", encoding="utf-8") as f:
            original_tool = json.load(f)
    except Exception:
        original_tool = {}

    try:
        tool_json = builder.repair_tool(original_tool, body.description)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"修复失败: {str(e)}")

    tool_json["name"] = tool_name
    valid, msg = builder.validate_tool_json(tool_json)
    if not valid:
        raise HTTPException(status_code=500, detail=f"修复后校验失败: {msg}")

    try:
        builder.save_tool_to_file(tool_json, tools_dir=tools_dir, registry=registry)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"保存失败: {str(e)}")

    registry.unregister_tool(tool_name)

    from agent.main import _ensure_output_dir, _create_executor
    _ensure_output_dir(tool_json)

    executor = _create_executor(
        tool_name, tool_json["execution_prompt"],
        tool_json["execution_mode"],
        tool_json.get("execution_code", ""),
        tool_json.get("http_config", {}),
        llm,
        tool_json.get("dependencies"),
        tool_json.get("response_formatter")
    )
    registry.register_tool(
        name=tool_name,
        description=tool_json["description"],
        parameters=tool_json["parameters"],
        func=executor
    )

    self_test_result = _run_self_test(tool_json, executor)

    log_tool_operation(
        tool_name=tool_name,
        operation="update",
        operator=user.get("username", ""),
        operator_id=user.get("id", 0),
        details=json.dumps({
            "repair_description": body.description,
            "execution_mode": tool_json.get("execution_mode", ""),
            "self_test_passed": self_test_result.get("passed"),
        }, ensure_ascii=False)
    )

    agent.reset()

    return {
        "success": True,
        "tool": ToolInfo(
            name=tool_name,
            description=tool_json["description"],
            parameters=tool_json["parameters"],
            execution_mode=tool_json["execution_mode"],
            output_dir=tool_json.get("output_dir"),
            dependencies=tool_json.get("dependencies"),
        ).model_dump(),
        "self_test": self_test_result,
    }


@router.delete("/{tool_name}", response_model=dict)
def delete_tool(tool_name: str, request: Request):
    user = require_permission(request, "tools", "delete")
    registry, _, _, _, _ = get_dependencies()
    tools_dir = get_tools_dir()

    filepath = os.path.join(tools_dir, f"{tool_name}.json")
    if not os.path.isfile(filepath):
        raise HTTPException(status_code=404, detail=f"工具 '{tool_name}' 不存在")

    tool_info = {}
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            tool_info = json.load(f)
    except Exception:
        pass

    os.remove(filepath)
    registry.unregister_tool(tool_name)
    registry.remove_from_manifest(tool_name)

    log_tool_operation(
        tool_name=tool_name,
        operation="delete",
        operator=user.get("username", ""),
        operator_id=user.get("id", 0),
        details=json.dumps({
            "description": tool_info.get("description", ""),
            "execution_mode": tool_info.get("execution_mode", ""),
        }, ensure_ascii=False)
    )

    return {"success": True, "message": f"工具 '{tool_name}' 已删除"}


@router.get("/logs", response_model=list[dict])
def query_tool_logs(
    request: Request,
    tool_name: str = Query(None, description="按工具名筛选"),
    operation: str = Query(None, description="按操作类型筛选（create/update/delete）"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    require_permission(request, "tools", "read")

    rows = get_tool_operation_logs(
        tool_name=tool_name,
        operation=operation,
        limit=limit,
        offset=offset
    )
    return rows