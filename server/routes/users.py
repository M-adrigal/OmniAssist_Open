from fastapi import APIRouter, HTTPException, Request
from server.models import UserCreateRequest, UserUpdateRequest, UserResponse
from server.database import list_users, create_user, update_user, delete_user, get_user_by_id, get_user_by_public_id
from server.routes.auth import require_permission

router = APIRouter(prefix="/api/users", tags=["users"])


@router.get("", response_model=list[UserResponse])
def get_users(request: Request):
    require_permission(request, "users", "read")
    users = list_users()
    return [UserResponse(**u) for u in users]


@router.post("", response_model=UserResponse)
def create_user_api(req: UserCreateRequest, request: Request):
    require_permission(request, "users", "write")

    if not req.username.strip():
        raise HTTPException(status_code=400, detail="用户名不能为空")
    if len(req.password) < 6:
        raise HTTPException(status_code=400, detail="密码长度不能少于6位")

    try:
        user = create_user(
            username=req.username.strip(),
            password=req.password,
            user_type=req.user_type,
            description=req.description.strip(),
        )
        return UserResponse(**user)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.put("/{public_id}", response_model=UserResponse)
def update_user_api(public_id: str, req: UserUpdateRequest, request: Request):
    require_permission(request, "users", "write")

    existing = get_user_by_public_id(public_id)
    if not existing:
        raise HTTPException(status_code=404, detail="用户不存在")
    db_id = existing["id"]

    kwargs = {}
    if req.password is not None:
        if len(req.password) < 6:
            raise HTTPException(status_code=400, detail="密码长度不能少于6位")
        kwargs["password"] = req.password
    if req.user_type is not None:
        kwargs["user_type"] = req.user_type
    if req.description is not None:
        kwargs["description"] = req.description.strip()

    user = update_user(db_id, **kwargs)
    return UserResponse(**user)


@router.delete("/{public_id}")
def delete_user_api(public_id: str, request: Request, keep_files: bool = False):
    current_user = require_permission(request, "users", "delete")

    if public_id == current_user["id"]:
        raise HTTPException(status_code=400, detail="不能删除自己的账号")

    target = get_user_by_public_id(public_id)
    if not target:
        raise HTTPException(status_code=404, detail="用户不存在")

    if not delete_user(target["id"], keep_files=keep_files):
        raise HTTPException(status_code=404, detail="用户不存在")

    return {"message": "用户已删除"}