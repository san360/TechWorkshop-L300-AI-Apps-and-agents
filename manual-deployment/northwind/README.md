# Northwind: separate manual deployment

This bundle does not change or deploy the shopping application. **No Azure writes occur unless you explicitly pass `--deploy`.** Run it independently of the repository and `azd up`.

## What it creates

| Component | Configuration |
|---|---|
| Azure AI Search | New dedicated Standard service, managed identity, keyless access, semantic ranker, vector-enabled `northwind-eligibility` index |
| Source documents | Both supplied Markdown files in a new private, keyless Storage account/container; neither document is publicly readable |
| Indexed content | Two complete records, one per small document, with titles, original text, source filenames, private source URLs, and 3072-dimensional embeddings |
| Foundry IQ | Real `northwind-eligibility-kb` knowledge base with two filtered Search-index knowledge sources, one per document; query planning uses the existing chat model |
| Project connections | `northwind-search` (AAD) and `northwind-knowledge-base` (RemoteTool, ProjectManagedIdentity, Search audience) |
| `memory-store` | Existing model deployments, user-partitioned memory, 30-day TTL; only non-sensitive interaction preferences, with conversation summaries and procedural memory disabled |
| `memory-agent` | `gpt-5.4-mini`, low reasoning, web search, `memory_search_preview`, scope `{{$userId}}`, update delay 30 |
| `eligibility-agent`, without knowledge | Version with no tools or attached data; no policy outcomes embedded in its instructions |
| `eligibility-agent`, with knowledge | Version with the direct Azure AI Search tool (`simple`, top 3) **and** the actual knowledge-base MCP tool; created last so it is latest |

This deliberately reuses an existing Foundry account/project and model deployments. The verified deployment kept Foundry in Sweden Central and used **North Europe** with **Standard Search** after Azure rejected Basic and Standard Search in Sweden Central. Configure your own target in a gitignored local settings file; no tenant-specific target is embedded in the code. The script does not create another Foundry account, change model capacity, modify the six shopping agents, touch Cosmos DB, change the Container App, or edit application code.

The exported version numbers, principal/client IDs, blueprints, GUIDs, and connection to the other subscription are not deployment inputs. Azure creates new identities and assigns real version numbers. Both eligibility variants use the **same agent name**; the manifest records their exact versions.

## Prerequisites

- Python 3.10+ and Azure CLI. No pip packages, Docker, or azd extensions are required by the script.
- Sign in to the external directory with an account that has Owner, or equivalent resource-creation plus role-assignment permissions. The signed-in identity also needs Foundry data-plane access and project-connection management access on the existing account.
- The existing Foundry account **and project** must both have system-assigned managed identity enabled.
- Existing, ready `gpt-5.4-mini` and `text-embedding-3-large` deployments. The latter must support 3072 dimensions.
- Provider registrations for Microsoft.Search, Microsoft.Storage, and Microsoft.CognitiveServices, available Search quota for the selected tier, and regional support for the preview memory/knowledge-base features.
- HTTPS access to Azure management, the Foundry project/model endpoint, Search, and Blob Storage. This is a keyless, publicly reachable demo setup, **not** a private-endpoint deployment.

The script checks the explicit subscription and token tenant and never changes the Azure CLI default subscription. It fails instead of granting access to a different tenant or silently falling back to another model, region, tool, or plain index when a real knowledge base is unavailable.

## Run manually in PowerShell

Open PowerShell in this bundle's directory. For a fresh clone, copy `deployment-settings.example.json` to `deployment-settings.local.json` and replace its placeholder subscription, tenant, resource group, account, and project. If your private bundle already contains a populated local settings file, keep it.

The local settings file is gitignored. Missing fields can come from `AZURE_SUBSCRIPTION_ID`, `AZURE_TENANT_ID`, `AZURE_RESOURCE_GROUP`, `AZURE_AI_ACCOUNT_NAME`, `AZURE_AI_PROJECT_NAME`, and `AZURE_LOCATION`. Explicit command-line arguments override these defaults.

```powershell
az login --tenant "<your-tenant-id>"
python .\deploy_northwind.py
```

The second command is **offline**. Review `output\deployment-plan.json`, including the target, new resource names, documents, memory options, and agent definitions. Then explicitly deploy:

```powershell
python .\deploy_northwind.py --deploy
```

Allow time for Search provisioning and managed-identity/RBAC propagation. The script prints bounded retry messages. If a call fails, it exits nonzero and records `status: failed` in `output\deployment-manifest.json`; it does not pretend the deployment completed or delete partially created resources. Correct the reported issue and run the same command again.

Default deployment behavior (the Foundry target must be supplied):

```text
New resources: northeurope (Standard Search)
Foundry:       remains in its existing region
Model:         gpt-5.4-mini
Embeddings:    text-embedding-3-large (3072 dimensions)
```

Use `python .\deploy_northwind.py --help` for overrides, including `--location` and `--search-sku basic|standard`. Search names include the selected region and a deterministic project suffix; Storage names use the project suffix. If a global name is unavailable, select unique names using `--search-name` and `--storage-name`, then keep those same arguments on subsequent runs. The script does not silently change region or tier on capacity errors.

## Inspect and compare in Foundry

After a successful run, use `output\deployment-manifest.json`:

1. Open your configured project in Foundry. In the knowledge/data area, inspect `northwind-eligibility-kb` and its `northwind-enrollment-index` and `northwind-determinations` sources. These are Search-index sources over the two indexed documents, not scheduled Blob indexers.
2. Open `eligibility-agent` and select the manifest's `without-knowledge` version. Start a **new conversation** and ask the supplied question.
3. Select the manifest's `with-knowledge` version and start another **new conversation** with the same question. Inspect tool calls and citations to verify retrieval. Do not reuse the baseline conversation: that would leak grounded policy facts into the comparison.

Suggested prompt:

> My mom is turning 65 and lives with me. Can I add her to my Northwind plan, and which of my two options is better for her?

The source-backed conclusion is conditional eligibility for **Health Plus only**, never Standard, via parent -> LA-1 -> Schedule D-7 Article III. A complete answer must also cover the more-than-half maintenance test, no other group coverage, annual enrollment or the 60-day QLE window, shared principal residence with stated exceptions, and Article V evidence. The two fixtures do not establish Medicare eligibility, provide a complete benefits/cost comparison, or prove that this particular parent meets the conditions.

The ungrounded version has no source of those policy facts and should state that it cannot determine eligibility. It is not instructed to hallucinate for the demonstration. These documents are synthetic workshop material, not actual insurance advice.

The deployment script verifies indexed document IDs/count, indexing-item results, and read-back of each saved agent definition. It does **not** run inference. To perform the separate live smoke check after successful deployment, run `python .\smoke_test_northwind.py`; this incurs model usage and saves retrieval/agent responses and `output\live-verification.json`. The smoke check queries both knowledge sources, invokes both eligibility versions independently, checks an actual knowledge-base MCP call, and invokes the memory agent using a non-sensitive test prompt. Availability of a preview feature can still vary by tenant/region.

## Safety, reruns, and costs

- New Search and Storage resources carry `deployment_package=northwind-manual-v1`. Existing resources with those names but without the ownership tag are refused.
- Existing agents, project connections, or `memory-store` not owned by this package are refused rather than overwritten. Existing incompatible memory configuration is never fixed by deleting memories.
- Unchanged package-owned agent definitions are reused. The grounded eligibility version is kept latest. If a definition changes, a new immutable version is created, not an in-place replacement.
- Rerun against the same target, resource names, and output directory. Do not run concurrent deployments from multiple machines or output directories. A local `deployment.lock` prevents overlapping runs in one output directory; remove a stale lock only after confirming the earlier process is no longer active.
- If an agent-create request times out after the service accepted it, rerun: the version metadata allows discovery and reuse. POST creation requests are not blindly retried after ambiguous server/network failures.
- Source links point to **private blobs**. Citation text/source filenames remain available, but a bare browser URL is not an anonymous document download. Use an authenticated Storage browser or authorized download; the script does not generate public links or SAS credentials.
- `{{$userId}}` partitions memory by the caller's authenticated identity. A backend using one shared service identity must supply a trusted, authenticated per-user memory identity; never accept an arbitrary client-provided user ID.
- The memory restrictions are defense in depth, not a compliance guarantee. Do not submit actual health, financial, tax, identity, or credential data in this workshop.
- Web search uses Bing-backed processing outside Azure compliance/geographic boundaries and incurs separate charges. The eligibility agent has **no web tool**. Do not send private Northwind or user data to web search.
- Search is billed at the selected tier while it exists; Standard costs more than Basic. Semantic ranking, embeddings, knowledge-base query planning, agent inference, memory, web search, and Storage can also incur charges. This script makes no price estimate.
- There is no automatic teardown. After the workshop, delete only this package's owned resources, agent versions, connections, memory store, KB/sources/index, and newly created role assignments. A reused role assignment in the manifest might predate this package: do not remove permissions required by the existing project. **Do not delete the shared resource group**, the existing Foundry account/project/models, or the shopping application. Deleting a memory store permanently deletes its retained memories.

## Offline script tests

```powershell
python -m unittest -v test_deploy_northwind.py
```

## API references

- [Foundry IQ connection and MCP authentication](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/foundry-iq-connect)
- [Search-index knowledge sources](https://learn.microsoft.com/en-us/azure/search/agentic-knowledge-source-how-to-search-index)
- [Knowledge-base creation](https://learn.microsoft.com/en-us/azure/search/agentic-retrieval-how-to-create-knowledge-base)
- [Direct Azure AI Search tool](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/tools/ai-search)
- [Memory stores and scope](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/memory-usage)
- [Search management API](https://learn.microsoft.com/en-us/rest/api/searchmanagement/services/create-or-update?view=rest-searchmanagement-2025-05-01)

This package uses Search data-plane `2026-08-01-preview`, Foundry connection management `2025-10-01-preview`, memory `2025-11-15-preview`, and agent API `v1`. It uses the SDK's `/agents/{name}/versions` REST route to create immutable versions.
