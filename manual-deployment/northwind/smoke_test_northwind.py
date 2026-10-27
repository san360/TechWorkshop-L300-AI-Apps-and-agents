"""Live smoke checks for the deployed Northwind bundle; incurs model usage."""

import json
from pathlib import Path

from deploy_northwind import (
    AzureApi, Config, DOCUMENTS, MEMORY_API, SEARCH_API, knowledge_sources,
    load_config_defaults, write_json,
)


ROOT = Path(__file__).resolve().parent
QUESTION = (
    "My mom is turning 65 and lives with me. Can I add her to my Northwind plan, "
    "and which of my two options is better for her?"
)


def response_text(response):
    return "\n".join(
        content["text"]
        for item in response.get("output", [])
        if item.get("type") == "message"
        for content in item.get("content", [])
        if content.get("type") == "output_text"
    )


def invoke(api, endpoint, version, prompt, output, label, *, tools_required=False):
    body = {
        "agent_reference": {
            "type": "agent_reference",
            "name": version["name"],
            "version": version["version"],
        },
        "input": prompt,
        "max_output_tokens": 4000,
    }
    if tools_required:
        body["tool_choice"] = "required"
    result = api.request(
        "POST", f"{endpoint}/openai/v1/responses", "https://ai.azure.com/", body,
        retry_permissions=True,
    )
    write_json(output / f"{label}-response.json", result)
    if result.get("status") != "completed" or not response_text(result).strip():
        raise RuntimeError(f"{label} did not complete successfully. See its saved response.")
    return result


def main():
    output = ROOT / "output"
    manifest = json.loads((output / "deployment-manifest.json").read_text(encoding="utf-8"))
    config = (
        Config(**manifest["configuration"])
        if "configuration" in manifest else load_config_defaults(ROOT)
    )
    if manifest.get("status") != "deployed" or manifest["target"] != config.project_id:
        raise RuntimeError("A successful deployment manifest for this bundle's target is required.")
    api = AzureApi(config)
    endpoint = manifest["project_endpoint"]
    search = api.search("POST", "indexes/northwind-eligibility/docs/search", {
        "search": '"LA-1"', "queryType": "simple", "top": 3,
        "select": "id,title,content,source_document,url",
    })
    write_json(output / "direct-search-check.json", search)
    if {item["source_document"] for item in search["value"]} != set(DOCUMENTS):
        raise RuntimeError("Direct Search did not return both policy documents.")
    print("Direct Search: both source documents returned.")
    retrieval = api.search("POST", "knowledgebases/northwind-eligibility-kb/retrieve", {
        "messages": [{"role": "user", "content": [{"type": "text", "text": QUESTION}]}],
        "knowledgeSourceParams": [{
            "knowledgeSourceName": source["name"], "kind": "searchIndex",
            "alwaysQuerySource": True, "includeReferences": True,
        } for source in knowledge_sources()],
        "includeActivity": True,
    })
    write_json(output / "knowledge-base-check.json", retrieval)
    serialized = json.dumps(retrieval)
    if not retrieval.get("response") or not all(name in serialized for name in DOCUMENTS):
        raise RuntimeError("Knowledge-base retrieval did not expose both source documents.")
    print("Knowledge base: both knowledge sources retrieved.")
    baseline = invoke(
        api, endpoint, manifest["agents"]["without-knowledge"], QUESTION,
        output, "without-knowledge",
    )
    if any(item.get("type") in {"mcp_call", "azure_ai_search_call"}
           for item in baseline.get("output", [])):
        raise RuntimeError("The baseline unexpectedly invoked a knowledge tool.")
    print("Ungrounded eligibility version: completed without retrieval tools.")
    grounded = invoke(
        api, endpoint, manifest["agents"]["with-knowledge"],
        QUESTION + " Use the Northwind knowledge-base tool and cite both source documents.",
        output, "with-knowledge", tools_required=True,
    )
    calls = [item for item in grounded.get("output", []) if item.get("type") == "mcp_call"]
    if not calls or not any(
        item.get("name") == "knowledge_base_retrieve" and not item.get("error")
        for item in calls
    ):
        raise RuntimeError("The grounded agent did not successfully invoke knowledge-base MCP retrieval.")
    text = response_text(grounded)
    if "Health Plus" not in text or "Standard" not in text:
        raise RuntimeError("The grounded answer did not distinguish Health Plus from Standard.")
    print("Grounded eligibility version: knowledge-base MCP call and plan-specific answer completed.")
    store = api.request(
        "GET", f"{endpoint}/memory_stores/memory-store?api-version={MEMORY_API}",
        "https://ai.azure.com/", retry_permissions=True,
    )
    if store["definition"]["options"]["chat_summary_enabled"]:
        raise RuntimeError("Memory store unexpectedly enables conversation summarization.")
    invoke(
        api, endpoint, manifest["agents"]["memory"],
        "Briefly introduce yourself. Do not use web search or store new preferences for this check.",
        output, "memory-agent",
    )
    print("Memory agent: completed; memory store is configured and conversation summaries are disabled.")
    write_json(output / "live-verification.json", {
        "status": "passed", "search_api": SEARCH_API,
        "document_count": 2, "knowledge_source_count": 2,
        "agent_versions": manifest["agents"],
        "checks": ["direct_search", "knowledge_base", "ungrounded_agent",
                   "grounded_agent_mcp", "memory_agent"],
    })
    print(f"Live verification: {output / 'live-verification.json'}")


if __name__ == "__main__":
    main()
