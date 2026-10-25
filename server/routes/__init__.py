from .chat import router as chat_router
from .sessions import router as sessions_router
from .config import router as config_router
from .models import router as models_router
from .files import router as files_router
from .upload import router as upload_router
from .auth import router as auth_router
from .users import router as users_router
from .skills import router as skills_router
from .skill_upload import router as skill_upload_router
from .approval import router as approval_router
from .usage import router as usage_router
from .audit import router as audit_router
from .memory import router as memory_router

# 注意顺序：config_router 含 /api/config 前缀的旧接口（GET/PUT /api/config），
# models_router 是同前缀下的新增子路径（/models、/providers、/my-models…），
# 两者路径不重叠，顺序不影响匹配。
# skill_upload_router 必须排在 skills_router 之前：skills 有参数化路径
# GET /{skill_id}（int），会先把 /trash 这类字面路径吃掉并报 422。
routers = [chat_router, sessions_router, config_router, models_router, files_router,
           upload_router, auth_router, users_router, skill_upload_router, skills_router,
           approval_router, usage_router, audit_router, memory_router]
