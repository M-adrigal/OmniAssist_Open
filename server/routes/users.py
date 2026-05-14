from fastapi import APIRouter, HTTPException, Request
from server.models import UserCreateRequest, UserUpdateRequest, UserResponse
from server.database import list_users, create_user, update_user, delete_user, get_user_by_id
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


@router.put("/{user_id}", response_model=UserResponse)
def update_user_api(user_id: int, req: UserUpdateRequest, request: Request):
    require_permission(request, "users", "write")

    existing = get_user_by_id(user_id)
    if not existing:
        raise HTTPException(status_code=404, detail="用户不存在")

    kwargs = {}
    if req.password is not None:
        if len(req.password) < 6:
            raise HTTPException(status_code=400, detail="密码长度不能少于6位")
        kwargs["password"] = req.password
    if req.user_type is not None:
        kwargs["user_type"] = req.user_type
    if req.description is not None:
        kwargs["description"] = req.description.strip()

    user = update_user(user_id, **kwargs)
    return UserResponse(**user)


@router.delete("/{user_id}")
def delete_user_api(user_id: int, request: Request):
    current_user = require_permission(request, "users", "delete")

    if user_id == current_user["id"]:
        raise HTTPException(status_code=400, detail="不能删除自己的账号")

    if not delete_user(user_id):
        raise HTTPException(status_code=404, detail="用户不存在")

    return {"message": "用户已删除"}