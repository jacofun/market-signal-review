"""Deploy and verify the existing Singapore review event function."""
import base64
import io
import json
import os
from pathlib import Path
import re
import sys
import time

from alibabacloud_fc20230330.client import Client
from alibabacloud_fc20230330 import models
from alibabacloud_tea_openapi.models import Config
from alibabacloud_tea_util.models import RuntimeOptions

REGION = "ap-southeast-1"
FUNCTION = "market-signal-review"
RUNTIME = "nodejs20"
HANDLER = "index.handler"
TIMER = "market-research-review-v4"
TRIGGER_CONFIG = {"cronExpression": "@every 5m", "enable": True, "payload": "drain-market-signal-reviews"}
ARCHIVE_FUNCTION = "fc-blog-authenticator-web"
ARCHIVE_REGION = "cn-beijing"
STORAGE_KEYS = ("OSS_CONTENT_BUCKET", "OSS_CONTENT_REGION", "PRIVATE_RESOURCE_ROOT")
PRESERVED_FIELDS = ("role", "cpu", "memory_size", "disk_size", "log_config", "vpc_config", "layers", "disable_inject_credentials")


class DeployError(Exception):
    pass


def log(event, **data):
    print(json.dumps({"event": event, **data}), flush=True)


def crc64(data):
    polynomial = 0xC96C5795D7870F42
    table = []
    for byte in range(256):
        value = byte
        for _ in range(8):
            value = (value >> 1) ^ (polynomial if value & 1 else 0)
        table.append(value)
    checksum = 0xFFFFFFFFFFFFFFFF
    for byte in data:
        checksum = table[(checksum ^ byte) & 0xFF] ^ (checksum >> 8)
    return str(checksum ^ 0xFFFFFFFFFFFFFFFF)


def safe_code(error):
    value = getattr(error, "code", None)
    return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,79}", value) else "FC_API_ERROR"


def call(operation, method, *args):
    try:
        return method(*args)
    except Exception as error:
        raise DeployError(f"{operation} failed ({safe_code(error)}).") from None


def codec(value):
    return value.to_map() if hasattr(value, "to_map") else value


def config_environment(os_environment, current, archive_lookup):
    """Fill unset storage settings from trusted config, preserving console overrides."""
    base = dict(current or {})
    explicit_role = os_environment.get("MARKET_REVIEW_ROLE_ARN", "").strip()
    configured = {key: base.get(key) or os_environment.get(key) for key in STORAGE_KEYS}
    missing_storage = any(not configured[key] for key in STORAGE_KEYS[:2])
    inherited = {}
    if missing_storage:
        try:
            inherited = archive_lookup()
            bucket_matches = not configured["OSS_CONTENT_BUCKET"] or configured["OSS_CONTENT_BUCKET"] == inherited.get("OSS_CONTENT_BUCKET")
            region = configured["OSS_CONTENT_REGION"]
            archive_region = inherited.get("OSS_CONTENT_REGION", "")
            region_matches = not region or region.removeprefix("oss-") == archive_region.removeprefix("oss-")
            if not bucket_matches or not region_matches:
                inherited = {}
                log("archive_config_conflict")
        except Exception as error:
            log("archive_config_lookup_failed", code=safe_code(error))
    for key in STORAGE_KEYS:
        value = os_environment.get(key) or inherited.get(key)
        if value and not base.get(key):
            base[key] = value.strip()
    if os_environment.get("OSS_CONTENT_ENDPOINT") and not base.get("OSS_CONTENT_ENDPOINT"):
        base["OSS_CONTENT_ENDPOINT"] = os_environment["OSS_CONTENT_ENDPOINT"].strip()
    if explicit_role and not re.fullmatch(r"acs:ram::[0-9]{1,32}:role/[A-Za-z0-9_+=,.@-]{1,64}", explicit_role):
        raise DeployError("MARKET_REVIEW_ROLE_ARN is invalid.")
    return base, explicit_role


def timer(client):
    cursor = None
    selected = None
    for _ in range(10):
        response = call("ListTriggers", client.list_triggers, FUNCTION, models.ListTriggersRequest(limit=50, next_token=cursor)).body
        for trigger in response.triggers or []:
            if trigger.trigger_type == "http":
                raise DeployError("The review event function has an HTTP trigger. Remove it before deploying.")
            if trigger.trigger_name == TIMER:
                selected = trigger
        next_cursor = response.next_token
        if not next_cursor:
            return selected
        if next_cursor == cursor:
            break
        cursor = next_cursor
    raise DeployError("Could not verify all FC triggers.")


def trigger_config(trigger):
    if trigger is None:
        return None
    if trigger.trigger_type != "timer" or trigger.qualifier not in (None, "", "LATEST", "$LATEST"):
        raise DeployError("The managed trigger name is used by a different trigger type or qualifier.")
    try:
        config = json.loads(trigger.trigger_config)
        return {key: config.get(key) for key in TRIGGER_CONFIG}
    except (ValueError, TypeError, AttributeError):
        return None


def verify_timer(client, *, sleep=time.sleep):
    old = timer(client)
    expected = json.dumps(TRIGGER_CONFIG, separators=(",", ":"))
    if old is None:
        params = models.CreateTriggerInput(trigger_name=TIMER, trigger_type="timer", trigger_config=expected)
        call("CreateTrigger", client.create_trigger, FUNCTION, models.CreateTriggerRequest(body=params))
    elif trigger_config(old) != TRIGGER_CONFIG:
        params = models.UpdateTriggerInput(trigger_config=expected)
        call("UpdateTrigger", client.update_trigger, FUNCTION, TIMER, models.UpdateTriggerRequest(body=params))
    for _ in range(10):
        current = timer(client)
        if trigger_config(current) == TRIGGER_CONFIG and current.status in (None, "", "OK"):
            log("review_timer_verified", triggerName=TIMER, interval="5m", enabled=True)
            return
        sleep(2)
    raise DeployError("Live timer data did not confirm an enabled 5-minute trigger.")


def invoke(client, action, commit):
    response = call("InvokeFunction", client.invoke_function_with_options,
        FUNCTION, models.InvokeFunctionRequest(body=io.BytesIO(json.dumps({"action": action}).encode())),
        models.InvokeFunctionHeaders(x_fc_invocation_type="Sync"),
        RuntimeOptions(connect_timeout=10_000, read_timeout=50_000, autoretry=False))
    body = response.body.read(4097) if hasattr(response.body, "read") else response.body
    try:
        result = json.loads(body) if isinstance(body, (str, bytes)) and len(body) <= 4096 else {}
        if (response.status_code != 200 or result.get("service") != FUNCTION or result.get("nodeMajor") != 20
                or result.get("codeVersion") != commit):
            raise ValueError()
    except (ValueError, AttributeError):
        raise DeployError(f"Live {action} invocation did not return the expected Node.js and commit version.") from None
    return result


def deploy(client, archive_lookup, package, commit, *, env=os.environ, sleep=time.sleep):
    current = call("GetFunction", client.get_function, FUNCTION, models.GetFunctionRequest()).body
    if current.function_name != FUNCTION or not current.runtime or current.runtime.startswith("custom"):
        raise DeployError("Expected the existing market-signal-review managed event function in Singapore.")
    trigger_config(timer(client))
    previous_env = current.environment_variables or {}
    environment, requested_role = config_environment(env, previous_env, archive_lookup)
    role = requested_role if requested_role and not current.role else None
    timeout = max(75, current.timeout or 0)
    checksum = crc64(package)
    body = models.UpdateFunctionInput(
        runtime=RUNTIME, handler=HANDLER, timeout=timeout, internet_access=True, instance_concurrency=1,
        role=role, environment_variables=environment if environment != previous_env else None,
        code=models.InputCodeLocation(zip_file=base64.b64encode(package).decode("ascii"), checksum=checksum),
    )
    call("UpdateFunction", client.update_function_with_options, FUNCTION, models.UpdateFunctionRequest(body=body), {},
        RuntimeOptions(connect_timeout=10_000, read_timeout=120_000, autoretry=False))
    for _ in range(36):
        updated = call("GetFunction readback", client.get_function, FUNCTION, models.GetFunctionRequest()).body
        if updated.state == "Failed" or updated.last_update_status == "Failed":
            raise DeployError("FC reports a failed deployment. Inspect the function console.")
        if (updated.state in (None, "", "Active") and updated.last_update_status in (None, "", "Successful")
                and str(updated.code_checksum) == checksum):
            break
        sleep(5)
    else:
        raise DeployError("FC code checksum and active state did not converge.")
    if (updated.runtime != RUNTIME or updated.handler != HANDLER or updated.timeout != timeout or
            updated.instance_concurrency != 1 or not updated.internet_access or (updated.environment_variables or {}) != environment):
        raise DeployError("Live runtime, handler, resources or environment did not match the deployment.")
    for key in PRESERVED_FIELDS:
        expected = (role or current.role) if key == "role" else codec(getattr(current, key, None))
        if codec(getattr(updated, key, None)) != expected:
            raise DeployError(f"FC configuration changed unexpectedly ({key}).")
    health = invoke(client, "health", commit)
    if health.get("status") != "ok":
        raise DeployError("Live health invocation failed.")
    log("review_code_verified", function=FUNCTION, region=REGION, sourceCommit=commit, codeChecksum=checksum, runtime=RUNTIME, handler=HANDLER)

    probe = invoke(client, "verify_dependencies", commit)
    if probe.get("status") != "ok" or probe.get("ossList") is not True or probe.get("marketReachable") is not True:
        allowed_codes = {"REVIEW_ROLE_REQUIRED", "REVIEW_OSS_CONFIG_REQUIRED", "REVIEW_OSS_CONFIG_INVALID", "REVIEW_OSS_PUBLIC_ENDPOINT_REQUIRED",
                         "REVIEW_OSS_LIST_FAILED", "REVIEW_MARKET_UNREACHABLE"}
        code = probe.get("code")
        hint = probe.get("ossCode")
        if hint in {"AccessDenied", "NoSuchBucket", "InvalidAccessKeyId", "InvalidSecurityToken", "SecurityTokenExpired", "SignatureDoesNotMatch"}:
            log("review_dependency_failed", code=code if code in allowed_codes else "REVIEW_PROBE_FAILED", ossCode=hint, httpStatus=probe.get("httpStatus"))
        raise DeployError(f"Code deployed, but review dependencies are blocked ({code if code in allowed_codes else 'REVIEW_PROBE_FAILED'}).")
    log("review_dependencies_verified", ossList=True, marketReachable=True)
    verify_timer(client, sleep=sleep)


def main():
    keys = [os.environ.get(key, "").strip() for key in ("ALIYUN_FC_ACCESS_KEY_ID", "ALIYUN_FC_ACCESS_KEY_SECRET")]
    if not all(keys):
        raise DeployError("Configure ALIYUN_FC_ACCESS_KEY_ID and ALIYUN_FC_ACCESS_KEY_SECRET in GitHub.")
    commit = os.environ.get("GITHUB_SHA", "")
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise DeployError("GITHUB_SHA is missing or malformed.")
    package = (Path(__file__).resolve().parents[1] / "dist" / "market-signal-review.zip").read_bytes()
    if not 0 < len(package) <= 20 * 1024 * 1024:
        raise DeployError("Invalid deployment ZIP size.")

    def fc_client(region):
        return Client(Config(access_key_id=keys[0], access_key_secret=keys[1], endpoint=f"fcv3.{region}.aliyuncs.com",
                            connect_timeout=10_000, read_timeout=30_000))

    def archive_lookup():
        function = fc_client(ARCHIVE_REGION).get_function(ARCHIVE_FUNCTION, models.GetFunctionRequest()).body
        settings = function.environment_variables or {}
        return {key: settings[key] for key in STORAGE_KEYS if settings.get(key)}

    deploy(fc_client(REGION), archive_lookup, package, commit)


if __name__ == "__main__":
    try:
        main()
    except DeployError as error:
        print(f"::error::{error}", file=sys.stderr)
        sys.exit(1)
    except Exception as error:
        print(f"::error::Deployment failed ({safe_code(error)}).", file=sys.stderr)
        sys.exit(1)
