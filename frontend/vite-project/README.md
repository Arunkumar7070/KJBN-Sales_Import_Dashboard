# KJBN Sales Operations — Web App

React + TypeScript + Tailwind CSS (v4) front end for the KJBN FastAPI backend,
built from the Stitch project "KJBN Sales Operations Dashboard".

## Run

```bash
cp .env.example .env      # set VITE_API_BASE_URL if the backend isn't on 127.0.0.1:8000
npm install
npm run dev               # http://localhost:5173
npm run build             # type-check + production build into dist/
```

The backend must be running and must allow this origin in `CORS_ALLOWED_ORIGINS`
(its default already allows `http://localhost:5173` and `http://127.0.0.1:5173`).
The dev server is pinned to port 5173 for that reason.

## Pages

| Route | View | API |
|---|---|---|
| `/` | Analytics Dashboard: KPIs, daily revenue trend (weekends shaded), store revenue with return rates (+ all-stores return health dialog), payment split, top 10 products | `GET /api/v1/analytics/dashboard` (current + previous period) |
| `/ingestion` | Data Ingestion: sales tab (multi-file upload with progress, batch history with error CSVs) and store catalog tab (`?tab=stores`) | `POST /api/v1/imports`, `GET /api/v1/imports`, `POST /api/v1/stores` |
| `/transactions` | Transaction Explorer: SKU/store/date/returns filters (kept in the URL), paging | `GET /api/v1/transactions`, `GET /api/v1/stores` |
| `/stores` | Store Management: store cards with all-time revenue; "Import Store Catalog" links to the ingestion page's catalog tab | `GET /api/v1/stores` |

## Conventions

- **API base URL:** every request goes through `src/api/client.ts`, built on `VITE_API_BASE_URL`.
- **Money:** the API sends integer paise. `formatINR` converts to rupees with integer
  arithmetic, never floating point, e.g. `482465000` → `₹48,24,650.00`.
- **Time:** all timestamps are shown in IST (`Asia/Kolkata`). Date filters are IST
  calendar days, as the backend expects.
- **Request states:** each request has loading, empty and error states. Errors show
  the backend's message and its `X-Request-ID`, with a Retry button.
- **Live updates:** QUEUED/PROCESSING batches refresh every 3 s until they finish.
