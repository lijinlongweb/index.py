import os
from copy import deepcopy
from inspect import signature
from typing import List, Dict, Any

from starlette.types import Scope, Receive, Send
from starlette.endpoints import Request, Response
from starlette.exceptions import HTTPException

from indexpy.responses import (
    JSONResponse,
    YAMLResponse,
    HTMLResponse,
)
from indexpy.applications import Index

from .schema import schema_parameters, schema_request_body, schema_response


class OpenAPI:
    def __init__(
        self,
        title: str,
        description: str,
        version: str,
        *,
        tags: Dict[str, Any] = {},
        template: str = "",
        media_type="yaml",
    ):
        """
        media_type: yaml or json
        """
        assert media_type in ("yaml", "json"), "media_type must in 'yaml' or 'json'"

        self.html_template = template
        self.media_type = media_type

        info = {"title": title, "description": description, "version": version}
        self.openapi = {
            "openapi": "3.0.0",
            "info": info,
            "paths": {},
            "tags": [
                {"name": tag_name, "description": tag_info.get("description", "")}
                for tag_name, tag_info in tags.items()
            ],
        }
        self.path2tag: Dict[str, List[str]] = {}
        for tag_name, tag_info in tags.items():
            for path in tag_info["paths"]:
                if path in self.path2tag:
                    self.path2tag[path].append(tag_name)
                else:
                    self.path2tag[path] = [tag_name]

    def _generate_paths(self) -> Dict[str, Any]:
        result = {}
        for view, path in Index().indexfile.get_views():
            if not hasattr(view, "HTTP"):
                continue
            viewclass = getattr(view, "HTTP")
            path_docs = self._generate_path(viewclass, path)
            if path_docs:
                result[path] = path_docs
        return result

    def _generate_path(self, viewclass: object, path: str) -> Dict[str, Any]:
        result = {}
        for method in viewclass.allowed_methods():  # type: ignore
            if method == "OPTIONS":
                continue
            method = method.lower()
            method_docs = self._generate_method(viewclass, path, method)
            if method_docs:
                result[method] = method_docs
        return result

    def _generate_method(
        self, viewclass: object, path: str, method: str
    ) -> Dict[str, Any]:
        sig = signature(getattr(viewclass, method))
        result: Dict[str, Any] = {}

        doc = getattr(viewclass, method).__doc__
        if isinstance(doc, str):
            doc = doc.strip()
            result.update(
                {
                    "summary": doc.splitlines()[0],
                    "description": "\n".join(doc.splitlines()[1:]).strip(),
                }
            )

        result["parameters"] = schema_parameters(
            None,
            sig.parameters.get("query").annotation  # type: ignore
            if sig.parameters.get("query")
            else None,
            sig.parameters.get("header").annotation  # type: ignore
            if sig.parameters.get("header")
            else None,
            sig.parameters.get("cookie").annotation  # type: ignore
            if sig.parameters.get("cookie")
            else None,
        )
        if not result["parameters"]:
            del result["parameters"]

        result["requestBody"] = schema_request_body(
            sig.parameters.get("body").annotation  # type: ignore
            if sig.parameters.get("body")
            else None
        )
        if not result["requestBody"]:
            del result["requestBody"]

        try:
            resps = getattr(getattr(viewclass, method), "__resps__")
        except AttributeError:
            pass
        else:
            result["responses"] = {}
            for status, content in resps.items():
                result["responses"][status] = {
                    "description": content["description"],
                }
                if content["model"] is not None:
                    result["responses"][status]["content"] = schema_response(
                        content["model"]
                    )
            if not result["responses"]:
                del result["responses"]

        if result and path in self.path2tag:  # has openapi docs, add tags
            result["tags"] = self.path2tag[path]

        return result

    def create_docs(self, request: Request) -> dict:
        openapi: dict = deepcopy(self.openapi)
        openapi["servers"] = [
            {
                "url": f"{request.url.scheme}://{request.url.netloc}",
                "description": "Current server",
            }
        ]
        openapi["paths"] = self._generate_paths()

        return openapi

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] not in ("/", "/get"):
            raise HTTPException(404)
        request = Request(scope, receive, send)

        if scope["path"] == "/get":
            handler = getattr(self, "docs")
        elif scope["path"] == "/":
            handler = getattr(self, "template")
        else:
            raise HTTPException(404)
        response = await handler(request)
        await response(scope, receive, send)

    async def template(self, request: Request) -> Response:
        if self.html_template:
            return HTMLResponse(self.html_template)

        with open(
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "template.html")
        ) as file:
            DEFAULT_TEMPLATE = file.read()
        return HTMLResponse(DEFAULT_TEMPLATE)

    async def docs(self, request: Request) -> Response:
        openapi = self.create_docs(request)
        media_type = request.query_params.get("type") or self.media_type

        if media_type == "json":
            return JSONResponse(openapi)
        return YAMLResponse(openapi)
