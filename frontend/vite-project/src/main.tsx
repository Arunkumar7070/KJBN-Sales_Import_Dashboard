import { lazy, StrictMode, Suspense } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter, Route, Routes } from "react-router-dom";
import Layout from "./components/Layout";
import { Spinner } from "./components/ui";
import "./index.css";

// Pages load on demand; the charting library is only needed by the dashboard.
const DashboardPage = lazy(() => import("./pages/DashboardPage"));
const IngestionPage = lazy(() => import("./pages/IngestionPage"));
const TransactionsPage = lazy(() => import("./pages/TransactionsPage"));
const StoresPage = lazy(() => import("./pages/StoresPage"));
const NotFoundPage = lazy(() => import("./pages/NotFoundPage"));

function page(element: React.ReactNode) {
  return <Suspense fallback={<Spinner label="Loading…" />}>{element}</Suspense>;
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <BrowserRouter>
      <Routes>
        <Route element={<Layout />}>
          <Route index element={page(<DashboardPage />)} />
          <Route path="ingestion" element={page(<IngestionPage />)} />
          <Route path="transactions" element={page(<TransactionsPage />)} />
          <Route path="stores" element={page(<StoresPage />)} />
          <Route path="*" element={page(<NotFoundPage />)} />
        </Route>
      </Routes>
    </BrowserRouter>
  </StrictMode>,
);
