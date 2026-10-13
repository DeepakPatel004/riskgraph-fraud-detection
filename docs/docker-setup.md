# Local Docker setup — Phase 7.1

Docker runs applications in isolated processes called **containers**. An
**image** is the packaged application used to create a container. Compose starts
the two related containers together. A **volume** stores data independently of
containers, so replacing a container does not lose the database.

Install [Docker Desktop for Windows](https://docs.docker.com/desktop/setup/install/windows-install/)
if necessary, follow its WSL 2 requirements, and start Docker Desktop with Linux
containers. Verify `docker version` shows both Client and Server and
`docker compose version` succeeds. Docker Desktop's installation and license
acceptance are separate from this project.

## Start and stop

Run these PowerShell commands from the RiskGraph repository root:

```powershell
docker compose build
docker compose run --rm --no-deps backend python -m scripts.init_db
docker compose up -d --wait
docker compose ps
Invoke-RestMethod http://127.0.0.1:8080/health
docker compose logs --tail 100 backend frontend
docker compose down
```

Open http://127.0.0.1:8080 while running. Initialization explicitly creates missing
tables using the existing script; startup never resets or initializes data.
Initialization can be repeated. `down` stops/removes this project's containers
and network but preserves `riskgraph-local_sqlite_data`. **Do not use `down -v`**
or remove this volume if you want to keep its records.

The Docker database is `/data/riskgraph.db`, completely separate from
`backend/riskgraph.db` on Windows. No host database is mounted or copied.
To change the frontend port for this shell: `$env:RISKGRAPH_HTTP_PORT='18080'`.
The examples then use port 18080 instead of 8080. Compose reads this variable
from the shell or root `.env`; it does not forward that file to the backend.
Keep real `.env` files private. `.env.example` contains only example settings.

## Communication and safeguards

The backend runs non-root Uvicorn with **one worker**, listening on port 8000
inside Docker only. Nginx serves the React production build as a non-root user
on port 8080. The host **port mapping** connects `127.0.0.1:8080` to that Nginx
port; it does not publish FastAPI. Both containers use read-only root filesystems
and temporary `/tmp` storage. Only the backend's `/data` volume is durable.

A **reverse proxy** forwards browser API requests to FastAPI using the internal
service name `backend:8000`. Nginx strips the existing browser `/backend/`
prefix, forwards direct `/api/` and `/health` routes too, and serves `index.html`
for React route refreshes. Vite's public API base is fixed to `/backend` at build
time. No secret environment files enter either build context. Nginx refreshes
Docker DNS so recreating the backend does not require hardcoding its IP.

The backend health check performs the existing SQL connectivity check. It does
**not** prove tables exist, Neptune works, or a model is available. Frontend
startup waits for backend health; the frontend check verifies static HTTP serving.
API errors pass through Nginx unchanged. Existing frontend error states show
unavailable dependencies rather than inventing results.

Neptune, scoring and graph population verification are explicitly disabled.
No worker starts. Transactions created through the legacy endpoint can persist
pending outbox jobs, but these are not proof of graph synchronization. There is
no public authentication: bind locally and use synthetic demo inputs only.
These settings are for restricted local development, not production security.

## Optional existing model bundle

The default backend installs core requirements only and starts without a model.
For an existing trusted bundle, enable the optional ML runtime and read-only mount:

```powershell
$env:RISKGRAPH_MODEL_DIR='./artifacts/fraud_classifier'
docker compose -f docker-compose.yml -f docker-compose.model.yml build
docker compose -f docker-compose.yml -f docker-compose.model.yml run --rm --no-deps backend python -m scripts.check_model_artifacts
docker compose -f docker-compose.yml -f docker-compose.model.yml up -d --wait
# Use the same -f arguments for logs, restart and down with this setup.
docker compose -f docker-compose.yml -f docker-compose.model.yml down
```

Alternatively leave `RISKGRAPH_MODEL_DIR` unset to use `./artifacts/fraud_classifier`.
The source must already exist; Compose will not silently create a missing folder.
Use forward slashes and quote Windows paths containing spaces. Allow Docker Desktop
to read that folder if prompted. Container UID 10001 needs read/traverse permission;
do not make artifacts world-writable. The mount is read-only and artifacts are
excluded from Git and image builds. Only load trusted bundles: preprocessing uses
Python serialization, which is unsafe for untrusted files.

The check opens the six required files and invokes the existing cached loader to
validate hashes, feature schema and exact saved ML dependency versions. Failure
is explicit; it never trains, downloads a model, or produces a fallback score.
The optional image installs the existing pinned ML requirements and libgomp
runtime; core builds do not need them. A readable bundle alone does not enable
scoring: real Neptune verification and the existing ingestion/freshness policies
still apply. Restart the backend after intentionally replacing a bundle.

The Linux XGBoost wheel also pulls a large NCCL runtime dependency, so the optional
image takes considerably longer to download and occupies more space. This does
not enable GPU training or change the saved classifier. The default image remains
small. Base-image tags and existing core dependency ranges can advance; this local
setup is not a fully locked production release.

## Existing volumes, migrations and worker

`create_all()` is not a schema migration. Before upgrading an older Docker database,
stop API and worker writers and back up the named volume using Docker Desktop's
volume export/backup tooling. Run the existing additive migrations when the older
schema lacks lease or graph-population columns:

```powershell
docker compose stop
docker compose run --rm --no-deps backend python -m scripts.migrate_graph_sync_leases
docker compose run --rm --no-deps backend python -m scripts.migrate_graph_population
docker compose up -d --wait
```

Do not copy an old host database into the volume casually. Review the existing
migration and scoring documentation first. Fresh volumes initialized above
already have the current schema.

Phase 7.2 must explicitly configure a verified Neptune deployment, install the
existing optional Neptune dependencies, and start `python -m app.workers.graph_sync_worker`
with the Neptune adapter and the same database volume/population identity. Preserve
the one-host ingestion gate and freshness checks. This phase does not supply that
worker service or AWS configuration. Never use `--allow-ephemeral` memory mode as
durable synchronization.

## Troubleshooting and verification

- Missing engine pipe: start Docker Desktop and wait until `docker version` shows
  a Server. `docker compose config --quiet` can validate YAML without an engine.
- Port busy: choose `RISKGRAPH_HTTP_PORT` above; do not publish backend port 8000.
- Startup or restart: wait for `docker compose up -d --wait` to succeed before
  checking API pages. A running container is not necessarily ready; temporary
  Nginx 502 responses during backend startup are expected. Retry the page after
  health recovers. Avoid running heavy image builds during timing-sensitive tests.
- Empty new environment: initialize the Docker database explicitly. An empty
  dashboard and no transactions/alerts are expected; host records are not imported.
- Database errors: inspect `docker compose logs backend`, ensure initialization
  completed, and check volume permissions. Do not delete the database to fix errors.
- Model unavailable: use the optional image, verify the directory and run the check.
  Normal API startup does not require model artifacts. Scoring remains unavailable
  until graph verification is complete, even with a valid model.
- Graph unavailable: expected until verified Neptune deployment; do not disable
  safety checks or substitute zero historical features.
- Registry/npm/pip errors: builds require network access to existing dependency
  registries; no AWS connection is needed. Retry after checking Docker networking.

Run the existing automated checks outside containers:

```powershell
cd backend
.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider
cd ../frontend
npm test
npm run build
```

For a Docker smoke check, verify `/health`, `/backend/health`, `/transactions`,
`/graph-explorer`, `/investigations` and `/backend/api/v1/dashboard/summary`.
Inspect browser connection, empty and unavailable states. Test persistence using
synthetic records in a **separate Compose project** (`-p riskgraph-phase71-test`),
restart its containers, and retrieve the same record. Always use that project flag
on its build/init/start/restart/down commands. Stop afterward without `-v`.

## Phase 7.1 verification

Verified locally with Docker Desktop 4.80.0, Engine 29.6.1 and Compose 5.1.4:

- Default backend, frontend and optional ML backend images built successfully
  (approximately 227 MB, 64 MB and 941 MB uncompressed, respectively).
- SQL health, Nginx proxy paths, SPA deep links, real empty dashboard responses,
  browser empty/error states and recovery after a restart worked.
- A synthetic `docker-persistence-71` transaction remained `12.34 USD`, UNSCORED
  and graph PENDING after restart and container replacement. It remains only in
  the retained `riskgraph-phase71-test_sqlite_data` test volume.
- The optional read-only bundle passed the existing loader's integrity checks.
  Scoring returned 503 both without ML and with ML installed; verified-Neptune
  restrictions remained intact and no scored record was persisted.
- Final backend suite: **553 passed** (two new artifact-check tests); frontend:
  **58 passed**; host and Docker frontend production builds succeeded.
- Host SQLite and all six model files retained their original SHA-256 hashes.
  No AWS/Neptune calls, training or dataset processing were performed.
- Test containers/networks were removed with `down`, retaining both test volumes.

An earlier run hit Windows pytest temporary-directory permissions in a new test;
that test now checks a nonexistent path without creating temporary files. Another
run hit an existing subprocess test's 30-second limit during the heavy optional
image build. The test passed alone and in the final full run after build completion.
The backend health timeout was increased to tolerate local Python startup under
load. The existing Starlette/httpx deprecation warning remains; no dependencies
were changed to address it in this phase.
