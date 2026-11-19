import copy
import io
import json
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock

from scripts import deploy_fc as fc


class FakeFC:
    def __init__(self, environment=None, role=None):
        self.function = NS(function_name=fc.FUNCTION, runtime="python3.12", handler="index.handler", cpu=0.35,
                           timeout=60, environment_variables=environment or {}, role=role, code_checksum=None, state="Active", last_update_status="Successful")
        self.triggers = []
        self.updates = []
        self.trigger_updates = []
        self.invocations = []
        self.probe = {"status": "ok", "ossList": True, "marketReachable": True}

    def get_function(self, name, request):
        assert name == fc.FUNCTION
        return NS(body=copy.deepcopy(self.function))

    def list_triggers(self, name, request):
        assert name == fc.FUNCTION
        return NS(body=NS(triggers=self.triggers, next_token=None))

    def create_trigger(self, name, request):
        assert name == fc.FUNCTION
        body = request.body
        self.triggers.append(NS(trigger_name=body.trigger_name, trigger_type=body.trigger_type,
                                trigger_config=body.trigger_config, qualifier=body.qualifier, status="OK"))

    def update_trigger(self, name, trigger_name, request):
        assert name == fc.FUNCTION and trigger_name == fc.TIMER
        self.trigger_updates.append(request.body)
        self.triggers[0].trigger_config = request.body.trigger_config

    def update_function_with_options(self, name, request, headers, runtime):
        assert name == fc.FUNCTION
        body = request.body
        self.updates.append(body)
        self.function.code_checksum = body.code.checksum
        for field in ("role", "runtime", "handler", "timeout", "instance_concurrency", "internet_access", "environment_variables"):
            value = getattr(body, field)
            if value is not None:
                setattr(self.function, field, value)

    def invoke_function_with_options(self, name, request, headers, runtime):
        assert name == fc.FUNCTION and headers.x_fc_invocation_type == "Sync"
        action = json.loads(request.body.read())["action"]
        self.invocations.append(action)
        probe = self.probe if action == "verify_dependencies" else {"status": "ok"}
        return NS(status_code=200, body=io.BytesIO(json.dumps({
            "service": fc.FUNCTION, "nodeMajor": 20, "codeVersion": "0" * 40, **probe,
        }).encode()))


class DeployTests(unittest.TestCase):
    def run_deploy(self, target, env=None, lookup=lambda: {}):
        return fc.deploy(target, lookup, b"archive", "0" * 40, env=env or {}, sleep=lambda _: None)

    def test_crc64(self):
        self.assertEqual(fc.crc64(b"123456789"), "11051210869376104954")

    def test_existing_config_is_preserved_and_timer_is_idempotent(self):
        config = {"OSS_CONTENT_BUCKET": "own-bucket", "OSS_CONTENT_REGION": "oss-cn-beijing", "UNRELATED": "retain"}
        target = FakeFC(config, "existing-role")
        lookup = Mock(side_effect=AssertionError("should not read existing website config"))
        self.run_deploy(target, lookup=lookup)
        self.run_deploy(target, lookup=lookup)
        lookup.assert_not_called()
        self.assertEqual(target.function.role, "existing-role")
        self.assertEqual(target.function.environment_variables, config)
        self.assertTrue(all(update.environment_variables is None and update.role is None for update in target.updates))
        self.assertEqual(target.function.handler, "index.handler")
        self.assertEqual(target.function.instance_concurrency, 1)
        self.assertTrue(target.function.internet_access)
        self.assertEqual(target.function.timeout, 75)
        self.assertEqual(target.invocations, ["health", "verify_dependencies"] * 2)
        self.assertEqual(len(target.triggers), 1)
        self.assertEqual(json.loads(target.triggers[0].trigger_config), fc.TRIGGER_CONFIG)
        self.assertEqual(target.trigger_updates, [])

    def test_only_storage_config_is_inherited_and_explicit_role_is_assigned(self):
        target = FakeFC({"UNRELATED": "retain"})
        self.run_deploy(target, {"MARKET_REVIEW_ROLE_ARN": "acs:ram::123:role/review-oss"}, lookup=lambda: {
            "OSS_CONTENT_BUCKET": "archive-bucket", "OSS_CONTENT_REGION": "oss-cn-beijing",
            "PRIVATE_RESOURCE_ROOT": "/archive", "OSS_CONTENT_ENDPOINT": "https://oss-cn-beijing-internal.aliyuncs.com",
            "UNRELATED": "never copied",
        })
        self.assertEqual(target.function.role, "acs:ram::123:role/review-oss")
        self.assertEqual(target.function.environment_variables, {
            "OSS_CONTENT_BUCKET": "archive-bucket", "OSS_CONTENT_REGION": "oss-cn-beijing",
            "PRIVATE_RESOURCE_ROOT": "/archive", "UNRELATED": "retain",
        })

    def test_partial_storage_does_not_mix_mismatched_buckets(self):
        env, _ = fc.config_environment({}, {"OSS_CONTENT_BUCKET": "custom"}, lambda: {
            "OSS_CONTENT_BUCKET": "archive", "OSS_CONTENT_REGION": "oss-cn-beijing",
        })
        self.assertEqual(env, {"OSS_CONTENT_BUCKET": "custom"})

    def test_unavailable_dependencies_leave_timer_inactive_and_report_code_deployed(self):
        target = FakeFC()
        target.probe = {"status": "blocked", "code": "REVIEW_ROLE_REQUIRED"}
        with self.assertRaisesRegex(fc.DeployError, "Code deployed.*REVIEW_ROLE_REQUIRED"):
            self.run_deploy(target)
        self.assertEqual(target.triggers, [])
        self.assertEqual(target.function.runtime, fc.RUNTIME)
        self.assertEqual(target.invocations, ["health", "verify_dependencies"])

    def test_existing_disabled_timer_is_enabled_only_after_probe(self):
        target = FakeFC()
        target.triggers.append(NS(trigger_name=fc.TIMER, trigger_type="timer",
                                   trigger_config=json.dumps({"enable": False}), qualifier="LATEST", status="OK"))
        self.run_deploy(target)
        self.assertEqual(len(target.trigger_updates), 1)
        self.assertEqual(json.loads(target.trigger_updates[0].trigger_config), fc.TRIGGER_CONFIG)

    def test_http_or_conflicting_timer_blocks_before_writing(self):
        for trigger_type, qualifier in [("http", None), ("timer", "review-prod")]:
            with self.subTest(trigger_type=trigger_type):
                target = FakeFC()
                target.triggers.append(NS(trigger_name=fc.TIMER, trigger_type=trigger_type,
                    trigger_config=json.dumps({"enable": True}), qualifier=qualifier, status="OK"))
                with self.assertRaises(fc.DeployError):
                    self.run_deploy(target)
                self.assertEqual(target.updates, [])
