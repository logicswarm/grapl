import base64
import hmac
import inspect
import json
import logging
import os
import sys
import traceback
import uuid

from base64 import b64decode
from hashlib import sha1
from typing import *

import boto3
from botocore.client import BaseClient
from chalice import Chalice, Response
from github import Github
from grapl_analyzerlib.grapl_client import GraphClient
import pydgraph
from grapl_analyzerlib.schemas import *
from grapl_analyzerlib.schemas.lens_node_schema import LensSchema
from grapl_analyzerlib.schemas.risk_node_schema import RiskSchema
from grapl_analyzerlib.schemas.schema_builder import ManyToMany
from grapl_analyzerlib.grapl_client import (
    GraphClient,
    MasterGraphClient,
    LocalMasterGraphClient,
)

T = TypeVar("T")

IS_LOCAL = bool(os.environ.get("IS_LOCAL", False))

if IS_LOCAL:
    JWT_SECRET = str(uuid.uuid4())
    os.environ["BUCKET_PREFIX"] = "local-grapl"
else:
    JWT_SECRET_ID = os.environ["JWT_SECRET_ID"]

    client = boto3.client("secretsmanager")

    JWT_SECRET = client.get_secret_value(SecretId=JWT_SECRET_ID,)["SecretString"]

ORIGIN = (
    "https://" + os.environ["BUCKET_PREFIX"] + "engagement-ux-bucket.s3.amazonaws.com"
)
ORIGIN_OVERRIDE = os.environ.get("ORIGIN_OVERRIDE", None)

GRAPL_LOG_LEVEL = os.getenv("GRAPL_LOG_LEVEL")
LEVEL = "ERROR" if GRAPL_LOG_LEVEL is None else GRAPL_LOG_LEVEL
logging.basicConfig(stream=sys.stdout, level=LEVEL)
LOGGER = logging.getLogger("grapl-model-plugin-deployer")

app = Chalice(app_name="model-plugin-deployer")


def into_list(t: Union[T, List[T]]) -> List[T]:
    if isinstance(t, list):
        return t
    return [t]


def check_jwt(headers):
    encoded_jwt = None
    for cookie in headers.get("Cookie", "").split(";"):
        if "grapl_jwt=" in cookie:
            encoded_jwt = cookie.split("grapl_jwt=")[1].strip()

    if not encoded_jwt:
        return False

    try:
        jwt.decode(encoded_jwt, JWT_SECRET, algorithms=["HS256"])
        return True
    except Exception as e:
        LOGGER.error(e)
        return False


def verify_payload(payload_body, key, signature):
    new_signature = "sha1=" + hmac.new(key, payload_body, sha1).hexdigest()
    return new_signature == signature


def set_schema(client, schema: str) -> None:
    op = pydgraph.Operation(schema=schema)
    client.alter(op)


def format_schemas(schema_defs) -> str:
    schemas = "\n\n".join([schema.to_schema_str() for schema in schema_defs])

    types = "\n\n".join([schema.generate_type() for schema in schema_defs])

    return "\n".join(
        ["  # Type Definitions", types, "\n  # Schema Definitions", schemas,]
    )


def provision_mg(mclient: GraphClient, schemas: List[NodeSchema]) -> None:
    mg_schema_str = format_schemas(schemas)
    set_schema(mclient, mg_schema_str)


def get_s3_client() -> Any:
    if IS_LOCAL:
        return boto3.client(
            "s3",
            endpoint_url="http://s3:9000",
            aws_access_key_id="minioadmin",
            aws_secret_access_key="minioadmin",
        )
    else:
        return boto3.client("s3")


def git_walker(repo, directory, f):
    f(directory)
    for path in into_list(repo.get_contents(directory.path)):
        if path.path == directory.path:
            return
        inner_directories = into_list(repo.get_contents(path.path))
        for inner_directory in inner_directories:
            git_walker(repo, inner_directory, f)


def is_schema(plugin_name: str, schema_cls):
    if (
        plugin_name == "NodeSchema" or plugin_name == "AnyNodeSchema"
    ):  # This is the base class
        return False
    return (
        hasattr(schema_cls, "self_type")
        and hasattr(schema_cls, "generate_type")
        and hasattr(schema_cls, "to_schema_str")
    )


def get_schema_objects() -> Dict[str, NodeSchema]:
    clsmembers = inspect.getmembers(sys.modules[__name__], inspect.isclass)
    return {an[0]: an[1]() for an in clsmembers if is_schema(an[0], an[1])}


def provision_schemas(mclient, raw_schemas):
    # For every schema, exec the schema
    for raw_schema in raw_schemas:
        exec(raw_schema, globals())

    # Now fetch the schemas back from memory
    schemas = list(get_schema_objects().values())

    schemas = list(set(schemas) - builtin_nodes)
    LOGGER.info(f"deploying schemas: {[s.self_type() for s in schemas]}")

    provision_mg(mclient, schemas)


def upload_plugin(s3_client: BaseClient, key: str, contents: str) -> None:
    plugin_bucket = os.environ["BUCKET_PREFIX"] + "-model-plugins-bucket"

    plugin_parts = key.split("/")
    plugin_name = plugin_parts[0]
    plugin_key = "/".join(plugin_parts[1:])

    try:
        s3_client.put_object(
            Body=contents,
            Bucket=plugin_bucket,
            Key=plugin_name
            + "/"
            + base64.encodebytes((plugin_key.encode("utf8"))).decode(),
        )
    except Exception:
        LOGGER.error(f"Failed to put_object to s3 {key} {traceback.format_exc()}")


def respond(err, res=None, headers=None):
    LOGGER.info(f"responding, origin: {app.current_request.headers.get('origin', '')}")
    if not headers:
        headers = {}

    if IS_LOCAL:
        override = app.current_request.headers.get("origin", "")
        LOGGER.info(f"overriding origin with {override}")
    else:
        override = ORIGIN_OVERRIDE

    return Response(
        body={"error": err} if err else json.dumps({"success": res}),
        status_code=400 if err else 200,
        headers={
            "Access-Control-Allow-Origin": override or ORIGIN,
            "Access-Control-Allow-Credentials": "true",
            "Content-Type": "application/json",
            "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
            "X-Requested-With": "*",
            "Access-Control-Allow-Headers": "Content-Type, Access-Control-Allow-Headers, Authorization, X-Requested-With",
            **headers,
        },
    )


def requires_auth(path):
    if not IS_LOCAL:
        path = "/{proxy+}" + path

    def route_wrapper(route_fn):
        @app.route(path, methods=["OPTIONS", "POST"])
        def inner_route():
            if app.current_request.method == "OPTIONS":
                return respond(None, {})

            if not IS_LOCAL:  # For now, disable authentication locally
                if not check_jwt(app.current_request.headers):
                    LOGGER.warning("not logged in")
                    return respond("Must log in")
            try:
                return route_fn()
            except Exception as e:
                LOGGER.error(e)
                return respond("Unexpected Error")

        return inner_route

    return route_wrapper


def no_auth(path):
    if not IS_LOCAL:
        path = "/{proxy+}" + path

    def route_wrapper(route_fn):
        @app.route(path, methods=["OPTIONS", "POST"])
        def inner_route():
            if app.current_request.method == "OPTIONS":
                return respond(None, {})
            try:
                return route_fn()
            except Exception as e:
                LOGGER.error(e)
                return respond("Unexpected Error")

        return inner_route

    return route_wrapper


def upload_plugins(s3_client, plugin_files: Dict[str, str]):
    raw_schemas = [
        contents
        for path, contents in plugin_files.items()
        if path.endswith("schema.py") or path.endswith("schemas.py")
    ]

    provision_schemas(
        LocalMasterGraphClient() if IS_LOCAL else MasterGraphClient(), raw_schemas,
    )

    # TODO: Handle new reverse edges
    for path, contents in plugin_files.items():
        upload_plugin(s3_client, path, contents)


builtin_nodes = {
    "Asset",
    "File",
    "IpAddress",
    "IpConnection",
    "IpPort",
    "Lens",
    "NetworkConnection",
    "ProcessInboundConnection",
    "ProcessOutboundConnection",
    "Process",
    "Risk",
}


@no_auth("/gitWebhook")
def webhook():
    shared_secret = os.environ["GITHUB_SHARED_SECRET"]
    access_token = os.environ["GITHUB_ACCESS_TOKEN"]

    signature = app.current_request.headers["X-Hub-Signature"]

    assert verify_payload(
        app.current_request.body.encode("utf8"), shared_secret.encode(), signature
    )

    repo_name = app.current_request.json_body["repository"]["full_name"]
    if body["ref"] != "refs/heads/master":
        return

    g = Github(access_token)

    repo = g.get_repo(repo_name)

    plugin_folders = repo.get_contents("model_plugins")
    # Upload every single file and folder, within 'plugins', to Grapl

    plugin_paths = []
    for plugin_folder in plugin_folders:
        git_walker(repo, plugin_folder, lambda file: plugin_paths.append(file))

    plugin_files = {}
    for path in plugin_paths:
        if not path.content:
            continue

        file_contents = b64decode(path.content).decode()
        plugin_files[path.path] = file_contents

    upload_plugins(get_s3_client(), plugin_files)
    return respond(None, {})


# We expect a body of:
"""
"plugins": {
    "<plugin_path>": "<plugin_contents>",
}
"""


@requires_auth("/deploy")
def deploy():
    LOGGER.info("/deploy")
    request = app.current_request
    plugins = request.json_body.get("plugins", {})

    LOGGER.info(f"Deploying {request.json_body}")
    upload_plugins(get_s3_client(), plugins)
    LOGGER.info("uploaded plugins")
    return respond(None, {"Success": True})


def get_plugin_list(s3: BaseClient):
    plugin_bucket = os.environ["BUCKET_PREFIX"] + "-model-plugins-bucket"

    list_response = s3.list_objects_v2(Bucket=plugin_bucket)

    if not list_response.get("Contents"):
        return []

    plugin_names = set()
    for response in list_response["Contents"]:
        key = response["Key"]
        plugin_name = key.split("/")[0]
        plugin_names.add(plugin_name)
    return [plugin_name for plugin_name in plugin_names if plugin_name != "__init__.py"]


@requires_auth("/listModelPlugins")
def list_model_plugins():
    LOGGER.info("/listModelPlugins")
    plugin_names = []
    try:
        plugin_names = get_plugin_list(get_s3_client())
    except Exception as e:
        LOGGER.error("failed with %s", traceback.format_exc())
        return respond({"Failed": "Failed"})

    LOGGER.info("plugin_names: %s", plugin_names)
    return respond(None, {"plugin_list": plugin_names})


def delete_plugin(s3_client, plugin_name):
    plugin_bucket = os.environ["BUCKET_PREFIX"] + "-model-plugins-bucket"

    list_response = s3_client.list_objects_v2(Bucket=plugin_bucket, Prefix=plugin_name,)

    if not list_response.get("Contents"):
        return []

    plugin_names = set()
    for response in list_response["Contents"]:
        s3_client.delete_object(Bucket=plugin_bucket, Key=response["Key"])


@requires_auth("/deleteModelPlugin")
def delete_model_plugin():
    try:
        LOGGER.info("/deleteModelPlugin")
        request = app.current_request
        plugins_to_delete = request.json_body.get("plugins_to_delete", [])

        s3_client = get_s3_client()
        for plugin_name in plugins_to_delete:
            delete_plugin(s3_client, plugin_name)
    except Exception as e:
        LOGGER.error(traceback.format_exc())
        return respond("deleteModelPlugin: Server Error")

    return respond(None, {"Success": "Deleted plugins"})


@app.route("/{proxy+}", methods=["OPTIONS", "POST"])
def nop_route():
    LOGGER.info("routing: ", app.current_request.context["path"])
    LOGGER.info(vars(app.current_request))

    try:
        path = app.current_request.context["path"]
        if path == "/prod/gitWebhook":
            return webhook()
        if path == "/prod/deploy":
            return deploy()
        if path == "/prod/listModelPlugins":
            return list_model_plugins()
        if path == "/prod/deleteModelPlugin":
            return delete_model_plugin()

        return respond("InvalidPath")
    except Exception:
        LOGGER.error(traceback.format_exc())
        return respond("Route Server Error")
