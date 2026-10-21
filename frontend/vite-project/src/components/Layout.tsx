import { useEffect, useState } from "react";
import { NavLink, Outlet } from "react-router-dom";
import { API_BASE_URL, checkDatabaseHealth } from "../api/client";
import { formatIstClock } from "../lib/format";
import { Icon } from "./ui";

const NAV_ITEMS = [
  { to: "/", label: "Executive Analytics", icon: "analytics" },
  { to: "/ingestion", label: "Data Ingestion", icon: "cloud_sync" },
  { to: "/transactions", label: "Transaction Explorer", icon: "receipt_long" },
  { to: "/stores", label: "Store Management", icon: "storefront" },
];

const HEALTH_POLL_MS = 30_000;

type ApiHealth = "checking" | "up" | "down";

function useApiHealth(): ApiHealth {
  const [health, setHealth] = useState<ApiHealth>("checking");
  useEffect(() => {
    const controller = new AbortController();
    const check = () =>
      checkDatabaseHealth(controller.signal).then((ok) => {
        if (!controller.signal.aborted) setHealth(ok ? "up" : "down");
      });
    check();
    const timer = window.setInterval(check, HEALTH_POLL_MS);
    return () => {
      controller.abort();
      window.clearInterval(timer);
    };
  }, []);
  return health;
}

function useIstClock(): string {
  const [now, setNow] = useState(() => new Date());
  useEffect(() => {
    const timer = window.setInterval(() => setNow(new Date()), 1000);
    return () => window.clearInterval(timer);
  }, []);
  return formatIstClock(now);
}

const HEALTH_STYLE: Record<ApiHealth, { label: string; dot: string; text: string }> = {
  checking: { label: "Checking API…", dot: "bg-outline", text: "text-on-surface-variant" },
  up: { label: "API Connected", dot: "bg-tertiary-container", text: "text-tertiary" },
  down: { label: "API Unreachable", dot: "bg-error", text: "text-error" },
};

function ApiStatusPill({ health }: { health: ApiHealth }) {
  const style = HEALTH_STYLE[health];
  const host = API_BASE_URL.replace(/^https?:\/\//, "");
  return (
    <div
      className="hidden items-center gap-2 rounded-lg bg-surface-container-low px-3 py-1.5 md:flex"
      title={`${API_BASE_URL} — database health checked every 30s`}
    >
      <span className="relative flex h-2 w-2">
        {health === "up" && <span className={`absolute h-full w-full animate-ping rounded-full ${style.dot} opacity-50`} />}
        <span className={`relative h-2 w-2 rounded-full ${style.dot}`} />
      </span>
      <span className={`text-[11px] font-semibold tracking-[0.03em] ${style.text}`}>{style.label}</span>
      <span className="font-mono text-[11px] text-on-surface-variant">{host}</span>
    </div>
  );
}

function Brand() {
  return (
    <div className="flex items-center gap-2">
      <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-primary-container font-headline text-lg font-bold text-on-primary">
        K
      </div>
      <div className="flex flex-col">
        <span className="font-headline text-lg font-semibold leading-none tracking-[-0.01em] text-on-surface">KJBN</span>
        <span className="text-[11px] font-semibold uppercase tracking-wider text-on-surface-variant">Sales Dashboard</span>
      </div>
    </div>
  );
}

function Sidebar({ health }: { health: ApiHealth }) {
  return (
    <aside className="fixed inset-y-0 left-0 z-50 hidden w-64 flex-col justify-between bg-surface-container-lowest shadow-[0_1px_8px_rgba(0,0,0,0.04)] lg:flex">
      <div className="flex flex-col">
        <div className="flex h-16 items-center px-6">
          <Brand />
        </div>
        <div className="px-3 py-2">
          <div className="px-2 py-1 text-[11px] font-semibold uppercase tracking-wider text-on-surface-variant">Core Modules</div>
        </div>
        <nav className="flex flex-col gap-1 px-3" aria-label="Main">
          {NAV_ITEMS.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              end={item.to === "/"}
              className={({ isActive }) =>
                `flex items-center gap-2 rounded-lg px-3 py-2 text-sm font-semibold tracking-[0.01em] transition-colors ${
                  isActive
                    ? "bg-primary-container text-on-primary-container"
                    : "text-on-surface-variant hover:bg-surface-container hover:text-on-surface"
                }`
              }
            >
              <Icon name={item.icon} className="text-[18px]" />
              <span>{item.label}</span>
            </NavLink>
          ))}
        </nav>
      </div>
      <div className="p-3">
        <div className="flex flex-col gap-1.5 rounded-lg bg-surface-container-low p-3">
          <div className="flex items-center justify-between text-[11px] font-semibold uppercase tracking-wider">
            <span className="text-on-surface-variant">Database</span>
            <span className={HEALTH_STYLE[health].text}>
              {health === "up" ? "Online" : health === "down" ? "Offline" : "…"}
            </span>
          </div>
          <div className="h-1 w-full overflow-hidden rounded-full bg-surface-container-high">
            <div
              className={`h-full rounded-full transition-all ${
                health === "up" ? "w-full bg-tertiary-container" : health === "down" ? "w-full bg-error" : "w-1/3 animate-pulse bg-outline"
              }`}
            />
          </div>
        </div>
      </div>
    </aside>
  );
}

function TopBar({ health }: { health: ApiHealth }) {
  const clock = useIstClock();
  return (
    <header className="fixed left-0 right-0 top-0 z-40 flex h-16 items-center justify-between gap-3 bg-surface-container-lowest/90 px-4 shadow-[0_1px_8px_rgba(0,0,0,0.04)] backdrop-blur-xl sm:px-6 lg:left-64">
      <div className="flex min-w-0 items-center gap-3">
        <div className="lg:hidden">
          <Brand />
        </div>
        <ApiStatusPill health={health} />
      </div>
      {/* Below lg the sidebar is hidden, so the sections move up here. */}
      <nav className="flex items-center gap-1 overflow-x-auto rounded-lg bg-surface-container-low p-1 lg:hidden" aria-label="Sections">
        {NAV_ITEMS.map((item) => (
          <NavLink
            key={item.to}
            to={item.to}
            end={item.to === "/"}
            title={item.label}
            className={({ isActive }) =>
              `flex items-center gap-1 whitespace-nowrap rounded px-2 py-1 text-xs font-semibold transition-colors ${
                isActive ? "bg-primary-container text-on-primary-container" : "text-on-surface-variant hover:text-on-surface"
              }`
            }
          >
            <Icon name={item.icon} className="text-[16px]" />
            <span className="hidden md:inline">{item.label}</span>
          </NavLink>
        ))}
      </nav>
      <div className="flex items-center gap-4">
        <span className="hidden text-[11px] font-medium tracking-[0.03em] text-on-surface-variant xl:inline">
          KJBN Analytics Node
        </span>
        <div
          className="hidden items-center gap-1.5 rounded-lg bg-surface-container-low px-3 py-1.5 sm:flex"
          title="All times are shown in India Standard Time"
        >
          <Icon name="schedule" className="text-[16px] text-primary" />
          <span className="font-mono text-xs text-on-surface">{clock} IST</span>
        </div>
      </div>
    </header>
  );
}

export default function Layout() {
  const health = useApiHealth();
  return (
    <div className="min-h-screen">
      <Sidebar health={health} />
      <div className="flex min-h-screen flex-col lg:pl-64">
        <TopBar health={health} />
        <main className="w-full flex-1 px-4 pb-8 pt-22 sm:px-6">
          <Outlet />
        </main>
      </div>
    </div>
  );
}
