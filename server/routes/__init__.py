from .chat import router as chat_router
from .sessions import router as sessions_router
from .config import router as config_router
from .tools import router as tools_router
from .files import router as files_router
from .auth import router as auth_router
from .users import router as users_router

routers = [chat_router, sessions_router, config_router, tools_router, files_router, auth_router, users_router]