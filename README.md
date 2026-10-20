# Purva Drishti

Purva Drishti is a Smart India Hackathon 2026 prototype for predictive logistics
and forward supply chains (SIH26251). It demonstrates how stock history,
forecasts, road conditions and weather can inform replenishment plans.

> **Prototype - synthetic data.** Every post, depot, vehicle, stock reading,
   consumption value and weather event is made up. This project is a demo, not
   an operational logistics system.

## What the prototype includes

- PostGIS database seeded with 3 fictional depots, 8 fictional posts, 5 supply
  items, 10 vehicles, daily demand history, synthetic weather and road events.
- FastAPI endpoints protected by JWT and role checks for `planner`,
  `commander` and `admin`.
- XGBoost demand forecasting, a Prophet baseline, 14-day forecasts and MAPE.
  If the optional ML packages are unavailable, the API explicitly labels the
  seasonal fallback in its forecast response.
- Supply-risk explanation based on `days to next safe window - days of stock`.
- NetworkX / GeoPandas synthetic routes with a primary and backup. OSMnx
  downloads are disabled by default; enable them only when you have internet
  access and want to query OpenStreetMap.
- Alert rules, a what-if endpoint, dispatch plans and commander decisions.
- MQTT synthetic stock publisher, Leaflet maps, Recharts, PWA shell caching,
  stale-data display and an IndexedDB queue for offline approval decisions.

## Requirements

- Windows 10/11 with PowerShell (commands below use PowerShell syntax)
- Docker Desktop with Docker Compose
- Python 3.12 (recommended; use `py -0p` to list installed Python versions)
- Node.js 20 or newer and npm
- Internet access for initial dependency/image downloads and online map tiles

The ML/GIS Python packages are large and may take several minutes to install.
For a full ML environment, use Python 3.12 rather than an older Python version.

## Fast start: Docker backend + Vite frontend

Run commands from the project root unless a step says otherwise.

### 1. Create your local environment file

```powershell
Copy-Item .env.example .env
```

The example is for local-only development. Change `POSTGRES_PASSWORD`,
`JWT_SECRET_KEY`, and the three demo passwords in `.env` before using this
prototype beyond your own machine. You can create a random JWT key with:

```powershell
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Paste the printed value into `.env` as `JWT_SECRET_KEY=...`. Do not commit `.env`.

### 2. Build and start PostGIS, Mosquitto and the API

```powershell
docker compose up -d --build
docker compose ps
docker compose logs -f backend
```

The backend container creates the database tables and synthetic demo records
before it starts the API. Press `Ctrl+C` to stop following logs; containers keep
running. Check health and interactive API docs:

- API health: <http://localhost:8000/health>
- Swagger API docs: <http://localhost:8000/docs>

### 3. Install and start the frontend

Open a second PowerShell terminal at the project root:

```powershell
Set-Location frontend
npm ci
npm run dev
```

Open the Vite URL printed in the terminal (normally
<http://localhost:5173>). Keep the Vite terminal open while using the app.
The browser must be online for map tiles; the app shell, last-loaded dashboard
data and queued approval actions are available offline.

The service worker is enabled for production builds. To test the PWA and offline
cache locally, stop the dev server and run:

```powershell
npm run build
npm run preview
```

Open the preview URL printed by Vite, load the dashboard once, then use the
browser's offline mode to test the cached view and queued commander actions.

### 4. Sign in with a seeded demo role

The local seed creates these accounts. The passwords shown are defaults; if you
changed the matching `DEMO_*_PASSWORD` values in `.env`, use your changed
passwords.

| Role | Username | Default password |
| --- | --- | --- |
| Planner | `planner` | `planner-demo-password` |
| Commander | `commander` | `commander-demo-password` |
| Admin | `admin` | `admin-demo-password` |

Only a commander can approve/reject proposed plans. Only an admin can open the
audit page or retrain forecast models. These are public demo credentials, not
secure production accounts.

### 5. Run the synthetic stock simulator

Open a third PowerShell terminal at the project root. The default simulator
publishes 10 fake stock readings for Post Alpha / Fuel:

```powershell
py -3.12 -m venv simulator\.venv
.\simulator\.venv\Scripts\Activate.ps1
python -m pip install -r simulator\requirements.txt
python simulator\publish_stock.py --post-id 1 --item-id 1 --count 10 --interval 2
```

To publish continuously, pass `--count 0`. Use `--post-id` 1 through 8 and
`--item-id` 1 through 5. The simulator sends only synthetic readings to
`purva/stock/{post_id}`.

## Manual backend setup (without the API container)

Use this option for debugging Python code locally. Do not run this local API at
the same time as the Compose backend, because both use port 8000.

### 1. Start only the database and MQTT broker

From the project root, create `.env` as described above, then run:

```powershell
docker compose up -d database mosquitto
docker compose ps
```

### 2. Create a Python virtual environment and install all backend dependencies

```powershell
Set-Location backend
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements-ml.txt
```

`requirements-ml.txt` includes the base API dependencies, XGBoost, Prophet,
OSMnx and GeoPandas.

If PowerShell blocks virtual-environment activation, run this once in the
current terminal and then retry activation:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
```

### 3. Seed the database and start the API

```powershell
python -m app.seed
python -m uvicorn app.main:app --reload
```

Open <http://localhost:8000/docs> to explore the API. The seed script is safe
to rerun for this local demo: it creates demo users if missing and skips the
main dataset if it already exists.

### 4. Start the frontend and simulator

Use separate terminals at the project root:

```powershell
Set-Location frontend
npm ci
npm run dev
```

```powershell
Set-Location backend
.\.venv\Scripts\Activate.ps1
Set-Location ..
python simulator\publish_stock.py --post-id 1 --item-id 1 --count 10
```

## Useful API endpoints

All endpoints except `/health` and `/api/auth/token` require
`Authorization: Bearer <access_token>`. Use the Swagger UI at
<http://localhost:8000/docs> to log in and try requests.

| Method | Endpoint | Purpose |
| --- | --- | --- |
| `POST` | `/api/auth/token` | Sign in; form fields `username` and `password` |
| `GET` | `/api/dashboard` | Summary counts, post stock and weather |
| `GET` | `/api/posts` | Stock, demand and supply risk per post |
| `GET` | `/api/posts/{id}/forecast?item_id=1` | 14-day forecast, Prophet baseline and MAPE |
| `POST` | `/api/ml/retrain` | Retrain and persist available XGBoost models (admin) |
| `GET` | `/api/routes?depot_id=1&post_id=1` | Primary and backup route |
| `GET` | `/api/alerts` | Active stock, road and weather alerts |
| `POST` | `/api/what-if` | Compare current/projected risk and a simulated road closure |
| `GET`, `POST` | `/api/plans` | List or propose a dispatch plan |
| `POST` | `/api/plans/{id}/decision` | Commander approval or rejection |
| `GET` | `/api/audit` | Recent audit entries (admin) |

## Tests and build checks

Run the focused Python test suite from the project root:

```powershell
Set-Location backend
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-dev.txt
pytest tests -v
```

Build and audit the frontend:

```powershell
Set-Location frontend
npm ci
npm run build
npm audit --audit-level=moderate
```

## Stop and reset

Stop containers while keeping the synthetic database and MQTT volumes:

```powershell
docker compose down
```

Start them again with:

```powershell
docker compose up -d
```

To delete all local Compose data and re-seed an empty database, run:

```powershell
docker compose down -v
docker compose up -d --build
```

`docker compose down -v` permanently deletes the local database and MQTT
volumes. Do not use it if you need to keep local data.

## Security and deployment notes

- Use only made-up demo data. Do not enter real, classified or sensitive data.
- `.env.example`, demo passwords, anonymous local Mosquitto and HTTP are for
  local development only. Never reuse the example secrets in a deployment.
- For deployment, use strong environment-injected secrets, TLS/HTTPS at a
  trusted reverse proxy, restricted MQTT access with authentication/TLS,
  least-privilege database access, production password management, and a
  specific `CORS_ORIGINS` allow-list.
- The demo map coordinates are public map coordinates, but every named post and
  logistics record is synthetic. OSMnx is disabled by default. Enabling it
  downloads public road-network data; its results are not guaranteed to
  represent current or operational road access.
- The project is an educational prototype and requires independent review
  before any real-world use.
