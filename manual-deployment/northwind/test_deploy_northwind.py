import copy
import dataclasses
import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import Mock, patch

import deploy_northwind as deployment


ROOT = Path(__file__).resolve().parent
TEST_CONFIG = deployment.Config(
    subscription="00000000-0000-4000-8000-000000000001",
    tenant="00000000-0000-4000-8000-000000000002",
    resource_group="rg-northwind-test",
    account="northwind-test-foundry",
    project="northwind-test-project",
)


def target_args():
    return [
        argument
        for field in ("subscription", "tenant", "resource_group", "account", "project")
        for argument in ("--" + field.replace("_", "-"), getattr(TEST_CONFIG, field))
    ]


class FakeAzure:
    """In-memory Azure contract fixture; never authenticates or sends traffic."""

    def __init__(self):
        self.config = TEST_CONFIG
        self.calls = []
        self.resources = {}
        self.agent_versions = {}
        self.memory = None
        self.documents = []
        self.embedding_dimensions = deployment.DIMENSIONS
        config = self.config
        self.project_endpoint = (
            f"https://{config.account}.services.ai.azure.com/api/projects/{config.project}"
        )
        self.resources[config.account_id] = {
            "identity": {"principalId": "11111111-1111-4111-8111-111111111111"},
            "properties": {"endpoints": {
                "OpenAI Language Model Instance API": f"https://{config.account}.openai.azure.com/"
            }},
        }
        self.resources[config.project_id] = {
            "identity": {"principalId": "22222222-2222-4222-8222-222222222222"},
            "properties": {"endpoints": {"AI Foundry API": self.project_endpoint}},
        }
        for name in (config.model, config.embedding_model):
            self.resources[f"{config.account_id}/deployments/{name}"] = {
                "properties": {"model": {"name": name}, "provisioningState": "Succeeded"}
            }

    def cli(self, *args):
        return {"id": self.config.subscription, "tenantId": self.config.tenant}

    def token(self, audience):
        return {"claims": {"oid": "33333333-3333-4333-8333-333333333333"}}

    def arm(self, method, resource_id, version, body=None, **kwargs):
        self.calls.append((method, resource_id, body))
        if method == "GET":
            result = self.resources.get(resource_id)
            if result is None and not kwargs.get("allow_missing"):
                raise AssertionError(f"Unexpected GET: {resource_id}")
            return copy.deepcopy(result)
        if method != "PUT":
            raise AssertionError(method)
        result = copy.deepcopy(body)
        result["id"] = resource_id
        result.setdefault("properties", {})["provisioningState"] = "Succeeded"
        if "sku" in result:
            result["sku"]["tier"] = "Basic" if result["sku"]["name"] == "basic" else "Standard"
        if result.get("identity"):
            result["identity"]["principalId"] = "44444444-4444-4444-8444-444444444444"
        if "/roleAssignments/" in resource_id:
            result["properties"]["scope"] = resource_id.split("/providers/Microsoft.Authorization/")[0]
        self.resources[resource_id] = result
        return copy.deepcopy(result)

    def request(self, method, url, audience=None, body=None, **kwargs):
        self.calls.append((method, url, body))
        if "/roleAssignments?" in url:
            return {"value": [
                copy.deepcopy(item) for key, item in self.resources.items()
                if "/roleAssignments/" in key
            ]}
        if ".blob.core.windows.net/" in url:
            assert method == "PUT" and kwargs["raw"]
            return {}
        if "/embeddings?" in url:
            return {"data": [{
                "index": number, "embedding": [0.1] * self.embedding_dimensions
            } for number, _ in enumerate(body["input"])]}
        if "/memory_stores" in url:
            if method == "GET":
                return copy.deepcopy(self.memory)
            self.memory = copy.deepcopy(body)
            return self.memory
        raise AssertionError(f"Unexpected request: {method} {url}")

    def search(self, method, path, body=None):
        self.calls.append((method, path, body))
        if path.endswith("/docs/index"):
            self.documents = copy.deepcopy(body["value"])
            return {"value": [
                {"key": item["id"], "status": True} for item in self.documents
            ]}
        if path.endswith("/docs/search"):
            return {"@odata.count": len(self.documents), "value": self.documents}
        if method != "PUT":
            raise AssertionError((method, path))
        return copy.deepcopy(body)

    def foundry(self, method, endpoint, path, body=None, **kwargs):
        self.calls.append((method, path, body))
        parts = path.split("/")
        name = parts[1]
        existing = self.agent_versions.setdefault(name, [])
        if method == "GET":
            if len(parts) == 4:
                return copy.deepcopy(next(item for item in existing if item["version"] == parts[3]))
            return {"data": copy.deepcopy(existing), "has_more": False}
        if method != "POST":
            raise AssertionError(method)
        result = copy.deepcopy(body)
        number = str(len(existing) + 1)
        result.update({"name": name, "version": number, "id": f"{name}:{number}"})
        # A service-added default must not cause endless new versions on reruns.
        result["definition"]["output_types"] = ["text"]
        for tool in result["definition"]["tools"]:
            if tool["type"] == "mcp":
                tool["allowed_tools"] = {"tool_names": tool["allowed_tools"]}
        existing.append(result)
        return copy.deepcopy(result)


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.config = TEST_CONFIG
        self.records = deployment.load_documents(ROOT / "documents", self.config)

    def test_exact_documents_and_provenance(self):
        self.assertEqual(len(self.records), 2)
        self.assertEqual({row["source_document"] for row in self.records}, set(deployment.DOCUMENTS))
        self.assertEqual(len({row["id"] for row in self.records}), 2)
        self.assertIn("Article III", self.records[1]["content"])
        self.assertIn("more than one-half", self.records[1]["content"])
        self.assertIn("LA-1", self.records[0]["content"])
        self.assertTrue(all(row["url"].startswith(self.config.blob_endpoint) for row in self.records))

    def test_deterministic_names_are_project_scoped(self):
        self.assertEqual(self.config.search, dataclasses.replace(self.config).search)
        self.assertNotEqual(self.config.search, dataclasses.replace(
            self.config, project="another-project"
        ).search)
        self.assertLessEqual(len(self.config.storage), 24)

    def test_real_kb_has_two_filtered_sources_and_model(self):
        sources = deployment.knowledge_sources()
        self.assertEqual(len(sources), 2)
        for source, filename in zip(sources, deployment.DOCUMENTS):
            self.assertEqual(source["kind"], "searchIndex")
            self.assertEqual(
                source["searchIndexParameters"]["baseFilter"], f"source_document eq '{filename}'"
            )
        kb = deployment.knowledge_base("https://example.openai.azure.com", self.config)
        self.assertEqual({row["name"] for row in kb["knowledgeSources"]}, {row["name"] for row in sources})
        self.assertEqual(kb["outputMode"], "extractiveData")
        self.assertEqual(kb["models"][0]["azureOpenAIParameters"]["deploymentId"], self.config.model)

    def test_vector_and_semantic_schema(self):
        index = deployment.index_definition("https://example.openai.azure.com", self.config)
        vector = next(field for field in index["fields"] if field["name"] == "content_vector")
        self.assertEqual(vector["dimensions"], 3072)
        self.assertFalse(vector["retrievable"])
        self.assertEqual(index["semantic"]["defaultConfiguration"], "northwind-semantic")
        self.assertNotIn("apiKey", index["vectorSearch"]["vectorizers"][0]["azureOpenAIParameters"])

    def test_agent_variants_and_user_requested_tool_values(self):
        payloads = deployment.agent_payloads(self.config)
        self.assertEqual(set(payloads), {"memory", "without-knowledge", "with-knowledge"})
        self.assertEqual(payloads["without-knowledge"]["definition"]["tools"], [])
        self.assertNotIn("Health Plus", payloads["without-knowledge"]["definition"]["instructions"])
        tools = payloads["with-knowledge"]["definition"]["tools"]
        self.assertEqual({tool["type"] for tool in tools}, {"azure_ai_search", "mcp"})
        direct = tools[0]["azure_ai_search"]["indexes"][0]
        self.assertEqual(direct["query_type"], "simple")
        self.assertEqual(direct["top_k"], 3)
        self.assertTrue(direct["project_connection_id"].startswith(self.config.project_id))
        self.assertEqual(tools[1]["allowed_tools"], ["knowledge_base_retrieve"])
        memory = payloads["memory"]["definition"]["tools"][1]
        self.assertEqual(memory["scope"], "{{$userId}}")
        self.assertEqual(memory["update_delay"], 30)

    def test_memory_is_preference_only(self):
        store = deployment.memory_store(self.config)
        options = store["definition"]["options"]
        self.assertFalse(options["chat_summary_enabled"])
        self.assertFalse(options["procedural_memory_enabled"])
        self.assertTrue(options["user_profile_enabled"])
        self.assertEqual(options["default_ttl_seconds"], 2592000)
        self.assertIn("Never retain health", options["user_profile_details"])

    def test_offline_default_never_constructs_azure_client(self):
        with tempfile.TemporaryDirectory() as temporary:
            with patch.object(deployment, "AzureApi", side_effect=AssertionError("Network forbidden")):
                result = deployment.main(["--output-dir", temporary, *target_args()])
            self.assertEqual(result, 0)
            plan = json.loads((Path(temporary) / "deployment-plan.json").read_text())
            self.assertEqual(plan["document_count"], 2)
            self.assertFalse((Path(temporary) / "deployment-manifest.json").exists())

    def test_full_deploy_contract_and_idempotent_rerun(self):
        api = FakeAzure()
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            manifest = {}
            deployment.deploy(api, self.config, self.records, directory, manifest)
            self.assertEqual(manifest["status"], "deployed")
            self.assertEqual(manifest["index_document_count"], 2)
            self.assertEqual(len(api.agent_versions["memory-agent"]), 1)
            self.assertEqual(len(api.agent_versions["eligibility-agent"]), 2)
            latest = api.agent_versions["eligibility-agent"][-1]
            self.assertEqual(latest["metadata"]["variant"], "with-knowledge")
            first_role_count = len(manifest["role_assignments"])
            deployment.deploy(api, self.config, self.records, directory, {})
            self.assertEqual(len(api.agent_versions["memory-agent"]), 1)
            self.assertEqual(len(api.agent_versions["eligibility-agent"]), 2)
            self.assertEqual(
                len([key for key in api.resources if "/roleAssignments/" in key]), first_role_count
            )
            private_storage = api.resources[self.config.storage_id]["properties"]
            self.assertFalse(private_storage["allowBlobPublicAccess"])
            self.assertFalse(private_storage["allowSharedKeyAccess"])

    def test_ungrounded_update_does_not_leave_ungrounded_latest(self):
        api = FakeAzure()
        payloads = deployment.agent_payloads(self.config)
        endpoint = api.project_endpoint
        baseline = copy.deepcopy(payloads["without-knowledge"])
        deployment.ensure_agent(api, endpoint, "eligibility-agent", baseline)
        deployment.ensure_agent(api, endpoint, "eligibility-agent", payloads["with-knowledge"], latest=True)
        baseline["definition"]["instructions"] += " Be concise."
        baseline["metadata"]["definition_sha256"] = deployment.digest(baseline["definition"])
        deployment.ensure_agent(api, endpoint, "eligibility-agent", baseline)
        restored = deployment.ensure_agent(
            api, endpoint, "eligibility-agent", payloads["with-knowledge"], latest=True
        )
        self.assertEqual(restored["version"], "4")
        self.assertEqual(api.agent_versions["eligibility-agent"][-1]["metadata"]["variant"], "with-knowledge")

    def test_unowned_resource_and_agent_are_refused(self):
        api = FakeAzure()
        api.resources[self.config.search_id] = {"tags": {}, "properties": {}}
        with self.assertRaisesRegex(RuntimeError, "unowned"):
            deployment.ensure_service(api, self.config.search_id, "2025-05-01", {})
        api.agent_versions["memory-agent"] = [{"metadata": {}}]
        with self.assertRaisesRegex(RuntimeError, "not owned"):
            deployment.ensure_agent(
                api, api.project_endpoint, "memory-agent", deployment.agent_payloads(self.config)["memory"]
            )

    def test_search_display_region_is_accepted_on_rerun(self):
        api = FakeAzure()
        api.resources[self.config.search_id] = {
            "id": self.config.search_id,
            "tags": {"deployment_package": deployment.PACKAGE},
            "location": "West Europe",
            "sku": {"name": "basic"},
            "properties": {"provisioningState": "Succeeded"},
        }
        deployment.ensure_service(api, self.config.search_id, "2025-05-01", {
            "location": "westeurope", "sku": {"name": "basic"}
        })

    def test_incompatible_existing_memory_is_never_deleted(self):
        api = FakeAzure()
        api.memory = deployment.memory_store(self.config)
        api.memory["definition"]["options"]["chat_summary_enabled"] = True
        with self.assertRaisesRegex(RuntimeError, "incompatible option"):
            deployment.ensure_memory(api, api.project_endpoint, self.config)
        self.assertTrue(all(method != "DELETE" for method, *_ in api.calls))

    def test_embedding_dimension_failure_prevents_agent_creation(self):
        api = FakeAzure()
        api.embedding_dimensions = 12
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(RuntimeError, "Invalid embedding shape"):
                deployment.deploy(api, self.config, self.records, Path(temporary), {})
        self.assertEqual(api.agent_versions, {})

    def test_extra_tool_is_not_hidden_by_service_default_tolerance(self):
        self.assertTrue(deployment.contains_expected(
            {"tools": [], "output_types": ["text"]}, {"tools": []}
        ))
        self.assertFalse(deployment.contains_expected(
            {"tools": [{"type": "web_search"}]}, {"tools": []}
        ))
        self.assertTrue(deployment.contains_expected(
            {"tool_names": ["knowledge_base_retrieve"]}, ["knowledge_base_retrieve"]
        ))
        self.assertFalse(deployment.contains_expected(
            {"tool_names": ["knowledge_base_retrieve", "another_tool"]}, ["knowledge_base_retrieve"]
        ))

    def test_partial_failure_is_persisted_and_lock_removed(self):
        with tempfile.TemporaryDirectory() as temporary:
            with patch.object(deployment, "AzureApi", side_effect=RuntimeError("Simulated auth failure")):
                self.assertEqual(deployment.main([
                    "--deploy", "--output-dir", temporary, *target_args()
                ]), 1)
            manifest = json.loads((Path(temporary) / "deployment-manifest.json").read_text())
            self.assertEqual(manifest["status"], "failed")
            self.assertIn("Simulated auth failure", manifest["error"])
            self.assertFalse((Path(temporary) / "deployment.lock").exists())


class HttpTests(unittest.TestCase):
    def client(self):
        api = deployment.AzureApi.__new__(deployment.AzureApi)
        api.config = deployment.Config()
        api.allowed_hosts = {"management.azure.com"}
        api.token = Mock(return_value={"value": "test-token"})
        api.opener = Mock()
        return api

    def test_foreign_endpoint_rejected_before_credentials(self):
        api = self.client()
        with self.assertRaisesRegex(RuntimeError, "unrecognized"):
            api.request("GET", "https://not-azure.example/collect")
        api.token.assert_not_called()
        api.opener.open.assert_not_called()

    def test_agent_post_is_not_retried_after_ambiguous_server_error(self):
        api = self.client()
        error = urllib.error.HTTPError(
            "https://management.azure.com/agents", 500, "Internal error", {}, io.BytesIO(b"failure")
        )
        api.opener.open.side_effect = error
        with self.assertRaisesRegex(RuntimeError, "HTTP 500"):
            api.request("POST", "https://management.azure.com/agents", body={})
        self.assertEqual(api.opener.open.call_count, 1)

    def test_storage_requests_include_required_date_and_version(self):
        api = self.client()
        api.allowed_hosts.add("example.blob.core.windows.net")
        response = Mock()
        response.read.return_value = b""
        api.opener.open.return_value.__enter__ = Mock(return_value=response)
        api.opener.open.return_value.__exit__ = Mock(return_value=False)
        api.request(
            "PUT", "https://example.blob.core.windows.net/container/file",
            "https://storage.azure.com/", raw=b"fixture",
            headers={"x-ms-version": "2023-11-03", "x-ms-blob-type": "BlockBlob"},
        )
        request = api.opener.open.call_args.args[0]
        self.assertIsNotNone(request.get_header("X-ms-date"))
        self.assertEqual(request.get_header("X-ms-version"), "2023-11-03")


if __name__ == "__main__":
    unittest.main()
