import os
import copy
import typing
import asyncio
import traceback
import importlib
from types import ModuleType

from starlette.types import Scope, Receive, Send, ASGIApp
from starlette.status import WS_1001_GOING_AWAY
from starlette.requests import URL, Request
from starlette.websockets import WebSocket, WebSocketClose
from starlette.routing import NoMatchFound
from starlette.responses import RedirectResponse
from starlette.middleware.errors import ServerErrorMiddleware
from starlette.middleware.wsgi import WSGIMiddleware
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.gzip import GZipMiddleware
from starlette.exceptions import HTTPException, ExceptionMiddleware

from .types import WSGIApp
from .utils import Singleton
from .config import config
from .responses import FileResponse, automatic
from .background import (
    BackgroundTasks,
    after_response_tasks_var,
    finished_response_tasks_var,
)


class Lifespan:
    def __init__(self) -> None:
        self.on_startup: typing.Dict[str, typing.Callable] = {}
        self.on_shutdown: typing.Dict[str, typing.Callable] = {}

    def on_event(self, event_type: str) -> typing.Callable:
        """Wrapper add_event_type"""

        def add_event_handler(func: typing.Callable) -> typing.Callable:
            self.add_event_handler(event_type, func)
            return func

        return add_event_handler

    def add_event_handler(self, event_type: str, func: typing.Callable) -> None:
        if event_type == "startup":
            self.on_startup[func.__qualname__] = func
        elif event_type == "shutdown":
            self.on_shutdown[func.__qualname__] = func
        else:
            raise ValueError("event_type must be in ('startup', 'shutdown')")

    async def startup(self) -> None:
        """
        Run any `.on_startup` event handlers.
        """
        for handler in self.on_startup.values():
            if asyncio.iscoroutinefunction(handler):
                await handler()
            else:
                handler()

    async def shutdown(self) -> None:
        """
        Run any `.on_shutdown` event handlers.
        """
        for handler in self.on_shutdown.values():
            if asyncio.iscoroutinefunction(handler):
                await handler()
            else:
                handler()

    async def lifespan(self, scope: Scope, receive: Receive, send: Send) -> None:
        """
        Handle ASGI lifespan messages, which allows us to manage application
        startup and shutdown events.
        """
        message = await receive()
        assert message["type"] == "lifespan.startup"

        try:
            await self.startup()
        except BaseException:
            msg = traceback.format_exc()
            await send({"type": "lifespan.startup.failed", "message": msg})
            raise

        await send({"type": "lifespan.startup.complete"})
        message = await receive()
        assert message["type"] == "lifespan.shutdown"
        await self.shutdown()
        await send({"type": "lifespan.shutdown.complete"})

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        assert scope["type"] == "lifespan"
        await self.lifespan(scope, receive, send)


class Mount:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self.apps: typing.Dict[str, ASGIApp] = {}

    def append(
        self, route: str, app: typing.Union[ASGIApp, WSGIApp], app_type: str
    ) -> None:
        assert app_type in ("asgi", "wsgi")
        if route != "":  # allow use "" to mount app
            assert route.startswith("/"), "prefix must be start with '/'"
            assert not route.endswith("/"), "prefix can't end with '/'"
        if app_type == "wsgi":
            app = WSGIMiddleware(app)
        self.apps.update({route: app})  # type: ignore

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        assert scope["type"] in ("http", "websocket", "lifespan")

        if scope["type"] in ("http", "websocket"):
            path = scope["path"]
            root_path = scope.get("root_path", "")

            # Call into a submounted app, if one exists.
            for path_prefix, app in self.apps.items():
                if not path.startswith(path_prefix + "/"):
                    continue
                if isinstance(app, WSGIMiddleware):
                    if scope["type"] != "http":
                        continue
                subscope = copy.deepcopy(scope)
                subscope["path"] = path[len(path_prefix) :]
                subscope["root_path"] = root_path + path_prefix
                await app(subscope, receive, send)
                return

        await self.app(scope, receive, send)


class Filepath:
    def __init__(self) -> None:
        self.lifespan = Lifespan()

    @classmethod
    def split_uri(cls, uri: str) -> typing.List[str]:
        """
        convert uri to file string corresponding to index.py
        """
        if uri.endswith("/"):
            uri += "index"

        filepath = uri[1:].strip(".")
        filepath = filepath.replace("-", "_")

        pathlist = filepath.split("/")
        pathlist.insert(0, "views")
        return pathlist

    @classmethod
    def get_path(
        cls, uri: str
    ) -> typing.Tuple[typing.Optional[str], typing.Optional[str]]:
        """
        translate uri to module name and file abspath

        if file not found, return None
        """
        pathlist = cls.split_uri(uri)
        abspath = os.path.join(config.path, *pathlist) + ".py"
        if not os.path.exists(abspath):
            return None, None
        return ".".join(pathlist), abspath

    @classmethod
    def get_views(cls) -> typing.Iterator[typing.Tuple[ModuleType, str]]:
        """
        return all (Module, uri)
        """
        views_path = os.path.join(config.path, "views")

        for root, _, files in os.walk(views_path):
            try:
                files.remove("index.py")
                files.insert(0, "index.py")
            except ValueError:  # file not exists
                pass

            for file in files:
                if not file.endswith(".py"):
                    continue
                if file == "__init__.py":
                    continue
                abspath = os.path.join(root, file)
                relpath = os.path.relpath(abspath, config.path).replace("\\", "/")

                uri = relpath[len("views") : -3]
                if uri.endswith("/index"):
                    uri = uri[:-5]

                module = cls.get_view(uri)

                yield module, uri

    @classmethod
    def get_view(cls, uri: str) -> ModuleType:
        module_name, filepath = cls.get_path(uri)
        if module_name is None or filepath is None:
            raise ModuleNotFoundError(uri)
        # # Not thread-safe, temporarily commented
        # spec = importlib.util.spec_from_file_location(module_name, filepath)
        # module = importlib.util.module_from_spec(spec)
        # sys.modules[module_name] = module
        # spec.loader.exec_module(module)  # type: ignore
        # return module
        return importlib.import_module(module_name)

    async def http(self, scope: Scope, receive: Receive, send: Send) -> None:
        request = Request(scope, receive)
        pathlist = self.split_uri(request.url.path)

        try:
            module = self.get_view(request.url.path)
        except ModuleNotFoundError:
            raise HTTPException(404)

        try:
            get_response = getattr(module, "HTTP")
        except AttributeError:
            raise HTTPException(404)

        try:
            # set background tasks contextvar
            after_response_tasks_token = after_response_tasks_var.set(BackgroundTasks())
            finished_response_tasks_token = finished_response_tasks_var.set(
                BackgroundTasks()
            )
            # call middleware
            for deep in range(len(pathlist), 0, -1):
                module = importlib.import_module(".".join(pathlist[:deep]))
                if not hasattr(module, "Middleware"):
                    continue
                get_response = getattr(module, "Middleware")(get_response)

            # get response
            response = await get_response(request)
            if isinstance(response, tuple):
                response = automatic(*response)
            else:
                response = automatic(response)
            response.background = after_response_tasks_var.get()
            await response(scope, receive, send)
        finally:
            after_response_tasks_var.reset(after_response_tasks_token)

            run_finished_response_tasks = finished_response_tasks_var.get()
            finished_response_tasks_var.reset(finished_response_tasks_token)
            await run_finished_response_tasks()

    async def websocket(self, scope: Scope, receive: Receive, send: Send) -> None:
        websocket = WebSocket(scope, receive=receive, send=send)

        try:
            module = self.get_view(websocket.url.path)
        except ModuleNotFoundError:
            await WebSocketClose(WS_1001_GOING_AWAY)(scope, receive, send)
            return

        try:
            handler = getattr(module, "Socket")
        except AttributeError:
            await WebSocketClose(WS_1001_GOING_AWAY)(scope, receive, send)
            return

        await handler(websocket)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        handler = getattr(self, scope["type"])

        if scope["type"] in ("http", "websocket"):
            url = URL(scope=scope)
            uri = url.path
            if uri.endswith("/index"):
                handler = RedirectResponse(
                    url.replace(path=f'{uri[:-len("/index")]}'), status_code=301,
                )
            elif not config.ALLOW_UNDERLINE and "_" in uri:  # Google SEO
                handler = RedirectResponse(
                    url.replace(path=f'{uri.replace("_", "-")}'), status_code=301,
                )
            elif uri == "/favicon.ico":
                handler = FileResponse("favicon.ico")

        await handler(scope, receive, send)


class Index(metaclass=Singleton):
    def __init__(self) -> None:
        self.app = Filepath()
        self.childapps = Mount(self.app)
        self.exception_middleware = ExceptionMiddleware(
            self.childapps, debug=config.DEBUG
        )
        self.error_middleware = ServerErrorMiddleware(
            self.exception_middleware, debug=config.DEBUG
        )
        self.asgiapp: ASGIApp = CORSMiddleware(
            self.error_middleware,
            allow_origins=config.CORS_ALLOW_ORIGINS,
            allow_methods=config.CORS_ALLOW_METHODS,
            allow_headers=config.CORS_ALLOW_HEADERS,
            allow_credentials=config.CORS_ALLOW_CREDENTIALS,
            allow_origin_regex=config.CORS_ALLOW_ORIGIN_REGEX,
            expose_headers=config.CORS_EXPOSE_HEADERS,
            max_age=config.CORS_MAX_AGE,
        )
        self.asgiapp = GZipMiddleware(self.asgiapp)

    def add_middleware(self, middleware_class: type, **kwargs: typing.Any) -> None:
        self.error_middleware.app = middleware_class(
            self.error_middleware.app, **kwargs
        )

    def add_exception_handler(
        self,
        exc_class_or_status_code: typing.Union[int, typing.Type[Exception]],
        handler: typing.Callable,
    ) -> None:
        if exc_class_or_status_code in (500, Exception):
            self.error_middleware.handler = handler
        else:
            self.exception_middleware.add_exception_handler(
                exc_class_or_status_code, handler
            )

    def exception_handler(
        self, exc_class_or_status_code: typing.Union[int, typing.Type[Exception]]
    ) -> typing.Callable:
        def decorator(func: typing.Callable) -> typing.Callable:
            self.add_exception_handler(exc_class_or_status_code, func)
            return func

        return decorator

    def add_event_handler(self, event_type: str, func: typing.Callable) -> None:
        self.app.lifespan.add_event_handler(event_type, func)

    def on_event(self, event_type: str) -> typing.Callable:
        return self.app.lifespan.on_event(event_type)

    def mount(
        self, route: str, app: typing.Union[ASGIApp, WSGIApp], app_type: str
    ) -> None:
        self.childapps.append(route, app, app_type)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        scope["app"] = self
        await self.asgiapp(scope, receive, send)
