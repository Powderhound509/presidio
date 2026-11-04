# Install document jobs in existing Azure Government services

This runbook targets **already deployed** ASEv3, Synapse, ADF, Azure SQL DB,
ADLS Gen2, Cognitive Services, and ACR, plus explicitly permitted Storage
Queues. It does not create replacement service instances, networking
resources, identities, or application registrations. Cosmos DB is optional
and unused. The web frontend is a future consumer, not part of this sample.

**Before production activation:** use the supplied dependency lock, build/scan
the image with approved bases, validate SQL
concurrency against a disposable approved database, and execute Government
synthetic acceptance. An isolated Linux host generated `uv.lock` normally from
PyPI and completed the actual locked application suite across Python 3.10–3.14.
The original Windows host's PyPI artifact TLS problem was not bypassed:
generated artifacts were transferred with SHA-256 verification. Local results do not prove Azure
networking, identity, OCR quality, SQL semantics, ADF publication, or image
approval. No Azure resource has been deployed or changed.

Authoring evidence:

| Command / check | Observed result |
| --- | --- |
| `uv sync --locked --extra worker --group dev --python VERSION` followed by `python -m pytest -ra tests ../tests/test_document_tools.py` (Linux, package directory; separate environment per version) | 62 passed, 0 failed, 0 skipped on each Python 3.10.21, 3.11.16, 3.12.3, 3.13.15 and 3.14.7; actual project installed, not just dependency imports |
| `py -3.13 -m unittest discover -s docs\samples\ASE\synapse_worker\tests -q` (Windows, repository root, isolated dependency target) | 42 passed, no skips; includes XLSX annotation/redaction/sanitization, PDF/raster/Word, mocked OCR/SQL boundaries, child EOF/crash, lease-loss and wall-deadline cases |
| `py -3.13 -m unittest discover -s docs\samples\ASE\tests -p test_document_tools.py -q` | 20 passed, no skips |
| `ruff check src tests scripts examples`; `ruff format --check src tests scripts examples` (package directory) | Passed with Ruff 0.17.3; includes the Synapse notebook |
| `python -m build --wheel --installer uv` (Linux, locked project environment) | Passed with isolated hatchling 1.33.0; wheel includes all modules, console entry point and byte-identical shared helper |
| `uv build --project docs\samples\ASE\synapse_worker --wheel --offline --no-build-isolation` (Windows) | Passed; independently installed submission wheel imports without rendering dependencies |
| Unchanged worker Dockerfile, Linux/AMD64, pinned upstream uv and a disposable Python/ODBC test base | Built successfully; no image was published to ACR |
| Offline, read-only container with `/tmp` tmpfs, no network/credentials or exposed ports | `/health` 200, `/ready` 503, unknown path 404; UID 10001, ODBC Driver 18 present, Tesseract absent |
| Real packaged LibreOffice DOC conversion with synthetic input and injected detection, all three operations | Passed; DOCX artifacts produced, annotation retains/highlights source, redaction/sanitization remove source from visible text and all ZIP members; OCR not called |
| Microsoft ScriptDom 180.140.0 `TSql160Parser(true, SqlEngineType.SqlAzure)` on the final migration | Passed with 0 parser errors; syntax only, not database execution or concurrency |
| Python 3.10 syntax parsing; JSON/TOML/notebook parsing; PowerShell syntax; `git diff --check` | Passed; syntax checks are not execution in Python 3.10, Synapse, ADF or SQL |
| Ordinary `uv lock` (Linux); `uv lock --check --offline --project docs\samples\ASE\synapse_worker` (Windows) | Passed; same unmodified lock, SHA-256 `9ae28081c9e565ef511a1bf7172099973dd69d87b77c2513050757cb8a96d661` |
| Production image approval, real SQL concurrency and live Government acceptance | Not established; required before activation |

The locked Linux matrix used Pillow 12.3.0, openpyxl 3.1.5 and PyMuPDF 1.28.2.
Additional Windows checks used Pillow 12.1.0, openpyxl 3.1.5 and PyMuPDF 1.28.2,
all within the declared ranges; they are not a claim of full locked Windows
restoration. Earlier cached-PyMuPDF 1.26.5 results are superseded, not release
evidence. The Microsoft feed was only a recovery probe, not a retained source.

The container probe used Python 3.13.18 and LibreOffice 7.4.7.2. Its upstream
Linux/AMD64 inputs were
`python:3.13-slim-bookworm@sha256:70bda1e795ded2a684ef9815955e05829b14ee5bb5c3eb2a940648e88f10fce9`
and
`ghcr.io/astral-sh/uv:0.12.12@sha256:c5c7d1dfbdf123d4f6c01da3c50630c1de239651bd28a5cd671adb91e751ee9e`.
The Python derivative configured Microsoft's official Debian 12 ODBC
repository at build time. This was **test-only**, not a substitute for
approving/scanning the organization's production base and publishing through
the existing ACR. The temporary test container was stopped and removed.

## 1. Complete the existing-resource worksheet

Copy the JSON examples into the operator's approved configuration location.
These contain **non-secret identifiers only**. Never add keys, passwords,
client secrets, certificates, SAS URLs, exported tokens, credential-bearing
connections, or original filenames.

| Record | Required details |
| --- | --- |
| Cloud / execution | AzureUSGovernment subscription, tenant, region, installed Synapse Spark/runtime and JDBC versions; approved build/operator environment |
| Worker | Existing Linux ASE app/plan, capacity, Always On eligibility, outbound DNS/routes, scratch quota; existing UAMI resource/client/principal IDs or deliberately selected SAMI |
| Storage | Actual Blob, DFS and Queue Government endpoints, existing storage account kind, raw/result containers and queue names, network restrictions, retention/versioning/soft-delete policy |
| SQL | Actual Government hostname/database, Entra administrator, contained principal names/object IDs, migration version, RCSI setting and tested capacity |
| Presidio | Analyzer root HTTPS endpoint, resource URI, tenant, issuer/token version, app registration ID, enterprise-app object ID, app role ID/value, caller restrictions |
| OCR | Existing resource root endpoint, supported Read model/API, tier/image/page quotas, language support, token scope, private path, retention/deletion/data residency |
| ACR | Registry login hostname, authorization mode (classic RBAC or repository ABAC), repository, approved base/uv image digests, pull and publisher identities |
| Orchestration | Existing ADLS/SQL linked services, selected identity per connector, workspace SAMI path for Spark; approved schedule owner (ADF or Synapse, not both) |

Keep the API **resource service principal object ID** distinct from its app
registration object ID. UAMI client IDs select identity; principal IDs receive
permissions; resource IDs attach identity. None is a credential.

`config.example.json` defaults to paused processing and unvalidated OCR.
The `0.8` confidence is a **provisional fixture value**, not a calibrated
production threshold. API `2023-07-31` uses the Form Recognizer REST path;
`2024-11-30` uses the Document Intelligence path. Neither is assumed available
in your Government region. The adapter sends one raster per request with
`unicodeCodePoint` indexing and never follows redirects or fetches blob URLs.
If the resource cannot satisfy this contract, stop; do not silently select a
commercial endpoint or another service.

## 2. Prepare scoped permissions

Separate the operator/admin principal from runtime identities. Azure
Contributor does not imply database access, RBAC assignment authority, or
Entra application-role administration.

| Identity | Grant at the narrowest approved scope |
| --- | --- |
| Synapse workspace SAMI | Raw/result `Storage Blob Data Reader`; raw `Storage Blob Data Contributor` only if uploading; SQL contained Entra user in `document_submitter` |
| ADF / Synapse pipeline selected UAMI or SAMI | Required ADLS input access; SQL contained Entra user in `document_submitter`; credential-use rights for the chosen supported linked service |
| ASE worker/dispatcher identity | Raw `Storage Blob Data Reader`; result `Storage Blob Data Contributor`; work-queue `Storage Queue Data Contributor` (receive/update visibility/delete/send); OCR `Cognitive Services User`; Presidio API application role; SQL contained user in `document_worker` |
| Separate dispatcher if later split | Queue `Storage Queue Data Message Sender` plus its SQL dispatch rights; do not claim isolation while sharing the worker process and identity |
| ASE image-pull identity | Classic ACR `AcrPull`, or ABAC `Container Registry Repository Reader` with approved repository condition |
| Image publisher | Classic ACR `AcrPush`, or ABAC `Container Registry Repository Writer` with approved repository condition |
| Operations principal | Approved read-only SQL diagnostics; optionally `document_operator` for failed/dead-letter re-drive; no runtime DDL |
| Installer / permission admins | Approved object creation/schema permissions and ASE/ADF/Synapse publishing/configuration; scoped RBAC-assignment authority; authorized Entra app-role administrator; UAMI attachment rights (`Managed Identity Operator` or equivalent) |

Blob/queue scopes end in the specific container/queue. ADLS ACL traversal
may also be required when using ACLs. ACLs do not narrow broad RBAC grants.
The shared worker identity has the **union** of dispatch and consumer rights.
A receive-only Queue processor role must not be assumed to allow visibility
renewal. SQL management-plane roles do not grant database data access.

Expand `scripts\worksheet.example.json` to the complete matrix for your actual
principals. A permission administrator runs, from this directory:

```powershell
az cloud set --name AzureUSGovernment
az login --use-device-code
az account set --subscription APPROVED_SUBSCRIPTION_ID
.\scripts\permissions.ps1 -Worksheet C:\approved\document-permissions.json
# Review role_missing/app_role_missing with the responsible administrators.
.\scripts\permissions.ps1 -Worksheet C:\approved\document-permissions.json -Apply
```

The script checks before creating assignments. Recheck ABAC conditions and
inherited grants separately; an existing role with an incorrect condition
must be corrected by its administrator. Entra grants target the calling
identity service principal and the API resource service principal. Confirm
that the role permits application principals and is enabled. Preserve the
working tenant/issuer/audience/caller restrictions; do not change global
authentication settings to accommodate Spark.

For tenants using the DoD Graph endpoint, select the approved endpoint in
the worksheet deliberately. No secrets or raw tokens are script arguments.
Azure CLI acquires its own operator tokens internally. Do not run these
operator scripts as the worker.

## 3. Prove private paths from the actual runtimes

| Runtime | Required private destinations |
| --- | --- |
| Synapse Spark and selected pipeline integration runtime | ADLS Blob/DFS and SQL |
| ASE image-pull platform | ACR registry/data endpoints using the configured pull identity |
| ASE worker container | SQL, Blob, Queue, OCR and Presidio analyzer |
| Future browser | Approved web backend only, not private SQL/Storage/Presidio |

Verify hostname resolution, the expected private address, routes/firewalls,
TCP 443 (SQL 1433), certificate trust/SNI, identity acquisition, and API
authorization separately. From an approved Windows operator host, initial
probes include:

```powershell
Resolve-DnsName approvedsql.database.usgovcloudapi.net
Test-NetConnection approvedsql.database.usgovcloudapi.net -Port 1433
Resolve-DnsName approvedstorage.queue.core.usgovcloudapi.net
Test-NetConnection approvedstorage.queue.core.usgovcloudapi.net -Port 443
```

Repeat equivalent probes **inside the ASE image**, and from the actual Spark
and pipeline runtimes; operator/VM success is not sufficient. Make synthetic
authenticated requests using the selected identities, without printing
tokens, bodies or document content. Diagnose timeouts/DNS/TLS separately from
401 (audience/issuer/token) and 403 (roles/caller/data policy).

Blob/DFS private endpoints do not cover Queue; verify its separate approved
private endpoint/DNS. Synapse managed VNet is not the ASE VNet; self-hosted
integration runtime does not route Spark Python HTTP. Managed VNet selection
cannot be retrofitted to an existing workspace. The baseline intentionally
needs **no Synapse-to-ASE HTTP path**. Missing endpoints, capacity, content
inspection or isolation require approval; never expose services publicly,
disable TLS, or add an unapproved service as a workaround.

## 4. Build a complete image and deliver private packages

Use the approved builder, repositories and existing ACR. No build service is
introduced. In this directory:

```powershell
uv lock --check
uv sync --locked --extra worker --group dev
uv run pytest -q
uv run ruff check src tests scripts examples
uv build --wheel
```

Review and retain the supplied `uv.lock`; regenerate it with `uv lock` when
changing dependencies. It must be present in the image
context; `uv sync --locked` intentionally fails on missing/drifted locks.
Run with Python 3.10 through 3.14 in the approved validation matrix. Document
the actual versions, package licenses (including PyMuPDF and LibreOffice),
font policy, SBOM/scanning results, and image digests. Do not assume PyMuPDF
licensing suits every redistribution.

The initial image needs a **Debian-compatible, digest-pinned Python base**
with the approved Microsoft ODBC 18 APT repository/keyring configured, and
an approved uv image exposing `/uv`. Supply their existing registry
references; the Dockerfile does not create a repository or configure an
unreviewed signing source. Build from `docs\samples\ASE`:

```powershell
docker build --file synapse_worker\Dockerfile `
  --build-arg PYTHON_IMAGE=APPROVED_PYTHON_IMAGE_AT_SHA256_DIGEST `
  --build-arg UV_IMAGE=APPROVED_UV_IMAGE_AT_SHA256_DIGEST `
  --tag APPROVED_REGISTRY/approved/document-worker:RELEASE .
az acr login --name APPROVED_REGISTRY_NAME
docker push APPROVED_REGISTRY/approved/document-worker:RELEASE
```

Use Entra-backed publishing; never registry admin credentials or an
`--expose-token` copy/paste path. Record the pushed digest and deploy by
digest. All apt/uv downloads occur at **build time**. The entrypoint does not
install packages, models or OCR engines. Tesseract is not installed.
LibreOffice is packaged for legacy DOC conversion only.

Upload the built no-dependency wheel to Synapse workspace/pool packages
through approved private delivery. It is the same small Python package
without the `worker` extra: Spark does not import rendering/Azure OCR
dependencies. Verify the supported pool package mechanism, DEP restrictions,
JDBC SQL driver and runtime compatibility. Do not add `%pip` or public
startup downloads. Import `examples\synapse_document_jobs.ipynb`; its parameter
cell contains only non-secret object references. The accompanying
`examples\synapse_submit.py` is a source-export equivalent for existing
notebook publication workflows.

## 5. Install data-plane objects

An authorized installer, from the approved environment with ODBC 18 and the
worker development dependencies, runs:

```powershell
uv run python scripts\install.py --config C:\approved\worker.json `
  --principals C:\approved\document-principals.json
# After review/approval:
uv run python scripts\install.py --config C:\approved\worker.json `
  --principals C:\approved\document-principals.json --apply
```

Check-only mode requires existing containers/queue; missing objects fail
rather than claiming readiness. Apply creates only approved raw/result
containers, one queue, SQL schema/tables/procedures/roles and exact-SID
contained users. Existing principal SID mismatches stop installation.
It never creates a database, storage account, identity or service instance.
The CLI identity must already be authorized to make these changes.

`sql\001_jobs.sql` is a re-runnable v1 migration. Capture a database backup/
restore point under existing approved policy before migrations. Runtime
principals receive procedure execution, **not** direct table access,
`db_owner`, DDL or directory permissions. Exact-SID contained users avoid
runtime directory-name lookup. If the organization's SQL setup requires
server identity/directory lookup, resolve it with the SQL administrator,
never grant worker directory-management rights.

Version/table/index bootstrap runs in one transaction; individual object
checks permit recovery of a previously partial v1 install. Unknown version
rows stop installation. Submission enums use binary collation so an
insensitive database collation cannot accept values the worker rejects.
Validate execution, rollback and isolation against the approved database;
syntax validation alone cannot establish these properties.

### Job and publication contract

Inputs are immutable objects:
`RAW_CONTAINER/{document_uuid}/{version_uuid}.{extension}`.
Never use an original filename. Upload with overwrite disabled using the
approved identity/connector, then capture its ETag. Every submission includes
that exact ETag; download uses a conditional read. No arbitrary URL, parser,
shell option or filesystem path can enter the job.

SQL derives `owner_id` from the authenticated **database principal**.
Submission's unique key is owner + document + version + operation + language
+ policy. Conflicting ETags/extensions for an existing key fail. Jobs and
outbox commit in one transaction. Repeating submission returns the same
job ID. Queue body is exactly `1:{job_uuid}`, never a filename, document,
mapping, token or storage reference.

An ASE instance dispatches one outbox row and consumes one document at a
time. Queue TTL is explicitly `-1` (no expiry); visibility and SQL leases
default to 180 seconds, renewed every 60 seconds. Delivery is at-least-once,
not FIFO. SQL claim token + unexpired lease fence completion. Stale workers
cannot select a winning artifact. The dispatcher may republish after a
crash; duplicate active claims do not process concurrently under one fence.

Results use
`RESULT_CONTAINER/{job_uuid}/{claim_uuid}/document.ext`, optional
`annotations.json`, and `manifest.json`. Upload every required artifact,
then the manifest, then conditionally commit SQL `Complete`. Artifact
manifest contains sizes/hashes and safe capability/status metadata, not
source values. Do not expose a bare uploaded object as completed output.
Consumers read only the manifest selected by authorized SQL status.
Cross-store publication is not an ADLS/SQL transaction: failed/lost attempts
can leave private orphan objects requiring retention cleanup.

**Important authorization boundary:** workspace/factory MIs represent
trusted orchestration principals, not individual end users. They may submit
only approved storage references they are authorized to read. Opaque IDs
are not authorization. Do not expose this SQL client or shared MI directly
to untrusted users. The future web backend must maintain authenticated
tenant/user ownership and authorize upload/status/download for every job;
that frontend and its per-user authorization are not implemented here.

## 6. Configure the existing ASE worker app

Keep `processing_enabled=false` initially. Select the existing approved app
and Linux plan, attach the chosen identity, configure Always On, health path
`/health`, port 8080, scratch quota, instance restart/scale policy and private
outbound connectivity. `/ready` returns 503 while paused; `/health` confirms
the supervised loop is responsive, **not** that downstream services have
passed acceptance.

Use platform managed-identity ACR pulls. For example, after reviewing the
existing app's settings, an authorized operator can apply:

```powershell
az webapp config set --resource-group APPROVED_RG --name APPROVED_WORKER_APP `
  --generic-configurations '{"acrUseManagedIdentityCreds":true,"acrUserManagedIdentityID":"APPROVED_PULL_UAMI_CLIENT_ID"}'
az webapp config set --resource-group APPROVED_RG --name APPROVED_WORKER_APP `
  --linux-fx-version 'DOCKER|APPROVED_REGISTRY/approved/document-worker@sha256:APPROVED_DIGEST'
```

Omit the UAMI pull selector deliberately for an approved SAMI variant.
Do not pass registry usernames/passwords. Confirm the configuration syntax
for the deployed App Service container model before applying; sidecar-enabled
apps use their supported site-container configuration instead.

Deliver `worker.json` as approved application content/mount, or a reviewed
release-specific image configuration layer. `DOCUMENT_WORKER_CONFIG` names
that local file; configuration contains no credentials. The example embedded
in the image is **not** deployment configuration. Root endpoints, database
name, identity selector and policy are operator-controlled, never job inputs.
Unknown JSON fields (including secret fields) fail configuration validation.
SAMI selection is explicit (`identity=system`, empty `client_id`); failures
never trigger identity/user/password fallbacks.

Each job runs in a supervised subprocess; Linux uses a private process group,
3 GiB virtual-address-space limit and 600-second CPU limit. Default wall time
is 1800 seconds, configurable within 60–7200 seconds. A timeout/lost lease
kills processing and converter descendants; the parent removes private
scratch on normal failure/shutdown. A platform crash can leave scratch:
use ephemeral, access-restricted storage and approved restart cleanup/retention.
These are resource boundaries, **not a malware sandbox**. Windows containers
are a future alternative, not a validated deployment target for this image.

Activate only after the remaining acceptance gates pass: set the calibrated
OCR confidence/API, `ocr_validated=true`, then `processing_enabled=true` in
the approved config and restart the app. No Synapse call to its health
endpoint is required.

## 7. Publish orchestration artifacts

For Spark, use the workspace SAMI through the documented runtime-supported
path. Microsoft documents that **UAMI-linked-service connections are not
supported in Synapse Spark**; pipeline support does not change this.
Validate the actual identity for interactive and scheduled runs. A
deliberately authorized interactive user-passthrough variant needs separate
grants/documentation; unattended code must not fall back to it.

Interactive notebooks default to the interactive user's identity unless the
execution identity is explicitly changed to the workspace managed identity.
Batch and non-interactive notebook runs use the workspace identity. Selecting
the ADLS linked service below does **not** change the SQL execution identity;
verify both paths independently before submission.

`synapse_client` uses the runtime token library's named SQL resource `DW`
inside the JDBC adapter. Tokens stay inside the process/connection and are
never notebook parameters, printed values or pipeline outputs. Verify this
named-resource Government audience and selected execution identity in the
actual runtime; do not substitute a custom `api://` token request. The client
only executes SQL Submit/Status and never calls ASE/Queue/OCR.

For ADLS, the notebook calls `configure_storage` with the approved Government
DFS hostname and an existing **workspace-SAMI** linked service. It explicitly
selects OAuth and `LinkedServiceBasedTokenProvider`, not a SAS/key provider.
Configure this for each input/result account used. Confirm the linked service
authentication mode and actual runtime principal; a linked service backed by
a UAMI is not the supported Spark path.

For ADF/Synapse pipelines, select the existing SQL linked service with
explicit supported UAMI credential or deliberate SAMI authentication.
Do not configure SQL passwords/connection-string secrets. Publish
`examples\adf_sql_dataset.json` against that linked service and adapt
`examples\adf_pipeline.json` to the existing naming/publishing conventions.
The pipeline uses Lookup stored-procedure calls to receive returned job IDs,
bounded Until polling, and an explicit failure activity for failed/dead-letter
jobs. Submission success is **not** document-processing success.

Validate/import the pipeline JSON through the deployed factory's schema and
run a synthetic job before scheduling; the export has not been published
against a live factory here. Batch ingestion should enumerate a bounded
number of immutable ADLS versions, then invoke this child pipeline per
document with bounded concurrency. Use existing scheduling/watermarks rather
than introducing an event service. Designate one orchestrator per batch.

## 8. Synthetic acceptance and exact format policy

Start with `Contact alice@example.com.` and require the analyzer email span
8–25. The existing anonymizer API's independent synthetic test should still
produce `Contact <ANONYMIZED>.`; this worker uses its own explicit redaction/
label replacements rather than installing a remote anonymizer operator.

| Format | Supported initial subset / output |
| --- | --- |
| PDF | Native/scanned, unencrypted, 1–200 pages; 200 DPI rendering includes visible annotations/forms. OCR plus native character geometry; annotations preserve the source PDF, redaction/sanitization reconstruct image-only pages |
| TXT | UTF-8 (optional BOM), BOM-identified UTF-16 LE/BE; strict decode, no silent replacement or normalization; supported encoding/BOM/line endings retained; annotation adds a protected code-point offset/type sidecar |
| DOCX / DOC | Existing helper's supported body/tables/headers/footers and inline single-frame PNG/JPEG pictures; DOC requires packaged LibreOffice with macros disabled. Fields, tracked changes, footnotes/endnotes, equations, text boxes, floating/cropped/rotated drawings, embedded objects/macros and unsupported content fail explicitly |
| XLSX | Supported scalar strings, booleans and finite numbers; numbers only in `General` or `0` format. Dates/times, error cells, custom numeric formats, merged cells, formulas, rich text, macros, links/connections, drawings/images/charts, pivots/caches, validations, conditional formatting, protection and unsupported parts/features fail explicitly |
| PNG/JPEG | Single frame, flattened against white after EXIF orientation; newly encoded pixels, no source metadata; PNG stays PNG and JPEG stays JPEG |
| TIFF | Supported decoded raster frames, all processed, rebuilt RGB TIFF with lossless compression and no source metadata; no animation support for other codecs |

XLSX preflight inspects the original package before openpyxl. It rejects
formulas instead of trusting cached values or recalculating. It enumerates
stored cells, not a large sparse rectangle. Rebuilt outputs preserve cell
coordinates and unaffected supported scalar types; PII numeric cells become
literal text. All sheets/hidden rows/columns are scanned and rebuilt visible.
Redacted/pseudonymized sheets are named `Sheet 1`, etc.; annotation retains
source sheet names. Basic styles, row/column dimensions, print setup and
other original layout are intentionally not copied. Literal strings beginning
`=`, `+`, `-` or `@` remain text, never formulas.

| Limit | Exact initial bound |
| --- | --- |
| Input and each result document | 100 MiB |
| Package entries / expanded bytes | 1000 / 100 MiB |
| Characters per file / analyzer unit | 2,000,000 / 100,000 |
| XLSX sheets / stored cells / cell text | 100 / 100,000 / 32,767 code points |
| Image/rendered-page pixels | 25,000,000 |
| Total image/rendered pixels per file | 250,000,000 |
| PDF pages / TIFF frames | 200 |
| OCR raster operation deadline | 180 seconds, bounded retries for throttling/transient HTTP |
| Job attempts | 3 default; configurable 1–10 |

Oversized text units fail `analysis_block_limit` rather than splitting an
entity invisibly across chunks. All advertised format/operation combinations
must pass native-library tests before enabling the release. A lower OCR
tier/API limit wins. Confirm complete one-raster page coverage, Unicode/emoji
offsets, rotations, confidence at the exact threshold, malformed/missing
geometry, nonblank images with empty OCR, throttle/deadline handling and
language support. Calibrate confidence using representative synthetic printed
and handwritten fixtures; confidence alone does not prove PII coverage.
Labels are allocated in stable extraction order (PDF native blocks then OCR;
Word paragraph text then inline image references; worksheet/row/column;
image frame). Raster/native duplicates of the same value reuse labels.

Run synthetic format/mode acceptance with Tesseract absent, verify source
PII is removed from redacted text layers/pixels/package parts, preserve exact
unaffected values, verify all TIFF frames/hidden cells, and confirm complete
sidecar sets. Check alias reuse/reset, ambiguous aliases separate with
warnings, and label overflow failing closed.

Also test two workers, duplicate/out-of-order messages, lease expiry/renewal,
stale fencing, outbox publish/mark crashes, input mutation, partial artifact
uploads, SQL-completion crash, message-deletion crash, retries/poison and
restarts. Use an approved SQL database for transaction/isolation/concurrency
tests; Python mocks do not validate T-SQL locking. Require actual end-to-end
Synapse and scheduled ADF acceptance without direct ASE HTTP.

## 9. Operations, failure codes and retention

SQL states: `queued`, `running`, `retry_wait`, `succeeded`,
`succeeded_with_warnings`, `failed`, `dead_letter`.
The latter four are terminal. Unknown/ambiguous aliases produce
`succeeded_with_warnings`; they do not silently merge or block every file.
An accepted but undispatched job reports dispatch `pending`.

| Code family | Action |
| --- | --- |
| `invalid_*`, `unsupported_*`, `format_mismatch` | Correct the approved input/policy/config; do not bypass parser checks |
| `analysis_block_limit`, `*_limit`, `unsupported_numeric_format` | Split/reformat under the documented supported contract; do not silently truncate |
| `low_ocr_confidence`, `empty_ocr_nonblank_image`, `unlocated_*`, `label_render_overflow` | Review with synthetic coverage; no local OCR fallback or partial publication |
| `ocr_identity_failed`, `sql_connection_failed`, `analyzer_failed` | Separate identity/audience/permissions from routing/TLS; inspect safe operational status only |
| `ocr_throttled`, `ocr_deadline`, `storage_failed`, `job_deadline` | Bounded retry for eligible failures; review capacity and downstream health |
| `attempt_limit`, `processing_process_exited` | Diagnose restarts/resource limits; authorized re-drive only after correction |

Raw SDK exceptions/bodies, source text, mappings and tokens are suppressed.
Logs contain fixed codes, not documents. Use approved existing ASE/SQL/pipeline
operational views. Do not enable HTTP body tracing or dump connection tokens.
No new monitoring destination is required.

The SQL outbox reconciler republishes stale nonterminal jobs after ten
minutes. Explicit infinite Queue TTL avoids default seven-day expiry, but
requires retention/backlog operations. Retry waits are 30 seconds × attempt;
bare Storage Queue provides no automatic dead-lettering. SQL
`dead_letter` is authoritative, so no additional poison queue is needed.
Malformed/missing-job messages are diagnosed with fixed codes and discarded;
valid jobs are acknowledged only after terminal SQL state.

An authorized `document_operator` can re-drive corrected failed jobs:

```sql
EXEC docjobs.Redrive @job_id = '00000000-0000-0000-0000-000000000003';
```

Never re-drive successful/active jobs or change policy/input under an active
claim. Administrative diagnostics (only by approved operators):

```sql
SELECT status, COUNT_BIG(*) AS jobs FROM docjobs.Jobs GROUP BY status;
SELECT COUNT_BIG(*) AS pending_dispatch FROM docjobs.Outbox WHERE sent_at IS NULL;
SELECT COUNT_BIG(*) AS expired_claims FROM docjobs.Jobs
WHERE status = 'running' AND lease_until < SYSUTCDATETIME();
```

Agree finite retention periods **before real documents**. Use approved
storage lifecycle/operations processes covering raw objects, artifacts,
annotation sidecars, object versions/soft-deleted copies, and orphan attempt
prefixes. Never delete an active or SQL-selected winning attempt. Database
job/outbox retention must preserve idempotency for the required retry window.
Mappings stay job-local in memory and are recomputed from immutable input;
no reversible value map is returned or persisted. Review OCR service-side
retention/deletion, residency and access: private networking does not imply
zero retention. Preserve no sensitive scratch across restarts.

## 10. Upgrade and rollback

Pause new orchestration submissions and restart workers paused. Allow active
jobs to drain, or wait for their SQL/Queue leases to expire before changing
processing policy. Preserve all SQL/Queue/ADLS state. Record schema version,
prior image digest, package/pipeline/config versions and identity assignments.

Apply only reviewed backward-compatible migrations; deploy the new immutable
digest and updated approved pool wheel/artifacts, run a synthetic canary,
then resume producers/consumers. For schema-compatible rollback, restore the
prior image/config/pipeline/wheel and repeat the canary. Do not drop shared
tables/queues/containers, overwrite committed output, revert policy mid-job,
or restore a mapping from a different input. A breaking database rollback
needs its own approved migration/restore plan.

Recheck rights after identity changes. Recreated SAMIs receive new principals;
UAMIs are stable only while the same identity resource survives. Restore
explicit image-pull/downstream settings separately. Do not restore or invent
secret credentials.

## Future self-service web boundary

Use an existing ASE app/ACR image, Entra browser authorization-code/PKCE and
a bearer-protected backend, with backend MI for downstream calls. Proxy
uploads/downloads; do not give the browser SAS URLs or MI tokens. Enforce
authenticated tenant/user job ownership on every operation, bounded upload
quotas, retention and protected previews. Reuse this asynchronous job
contract behind the backend. Users need neither Synapse nor database access.
Untrusted-document inspection/isolation and per-user authorization are
release blockers until designed and verified using approved resources.

## Reference constraints

- [Synapse workspace managed VNet](https://learn.microsoft.com/azure/synapse-analytics/security/synapse-workspace-managed-vnet)
- [Synapse token library](https://learn.microsoft.com/azure/synapse-analytics/spark/apache-spark-secure-credentials-with-tokenlibrary)
- [Pipeline credentials and the Spark UAMI limitation](https://learn.microsoft.com/azure/data-factory/credentials)
- [Storage private endpoints](https://learn.microsoft.com/azure/storage/common/storage-private-endpoints)
- [Document Intelligence response spans and units](https://learn.microsoft.com/azure/ai-services/document-intelligence/concept/analyze-document-response)
- [App Service managed-identity ACR pulls](https://learn.microsoft.com/azure/app-service/configure-custom-container)
- [ACR RBAC/ABAC roles](https://learn.microsoft.com/azure/container-registry/container-registry-rbac-built-in-roles-directory-reference)
- [openpyxl limitations and security](https://openpyxl.readthedocs.io/en/stable/)

Commercial-cloud documentation is not proof of Government API/runtime parity.
