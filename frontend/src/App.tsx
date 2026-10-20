import { useCallback, useEffect, useRef, useState, type FormEvent, type ReactNode } from "react";
import { CircleMarker, MapContainer, Polyline, TileLayer, Tooltip } from "react-leaflet";
import { Area, AreaChart, CartesianGrid, ResponsiveContainer, Tooltip as ChartTooltip, XAxis, YAxis } from "recharts";
import { ApiError, api, withFallback } from "./api";
import { enqueueDecision, getQueuedDecisions, removeQueuedDecision, type QueuedDecision } from "./offlineQueue";

type SupplyRisk = { score: number; coverage_days: number | null; days_stock_left?: number | null; risk_days?: number | null; days_to_next_safe_window?: number | null; level: string; explanation?: string };
type Stock = { item_id: number; item: string; unit: string; quantity: number; average_daily_demand_7d?: number | null; risk: SupplyRisk };
type Post = { id: number; name: string; altitude_m: number; location: { longitude: number; latitude: number }; stocks: Stock[]; risk_score: number; risk?: { days_to_next_safe_window?: number | null; explanation?: string; critical_items?: number } };
type Depot = { id: number; name: string; altitude_m: number; location: [number, number] };
type Vehicle = { id: number; name: string; vehicle_type: string; capacity_kg: number; available: boolean };
type Plan = { id: number; post_id: number; depot_id: number; item_id: number; quantity: number; vehicle_id?: number | null; vehicle_name?: string | null; primary_route?: RouteOption | null; backup_route?: RouteOption | null; departure_at?: string | null; explanation?: string | null; risk?: SupplyRisk | null; requires_commander_approval?: boolean; status: string; created_at: string };
type Alert = { id: number | null; post_id: number | null; item_id: number | null; severity: string; message: string; suggestion: string; created_at: string; resolved: boolean };
type Item = { id: number; name: string; unit: string };
type Page = "overview" | "posts" | "routes" | "simulator" | "plans" | "audit";
type ForecastPoint = { date: string; quantity: number };
type Dashboard = { summary: { posts: number; depots: number; active_alerts: number; active_plans: number; available_vehicles: number }; posts: Post[] };
type CachedOperations = { savedAt: string; summary: Dashboard["summary"]; posts: Post[]; alerts: Alert[]; plans: Plan[] };
type RouteOption = { distance_km: number; estimated_hours: number; risk_score: number | null; path: number[][]; segments: { id: number | null; name: string; closure_risk?: number; risk_score?: number }[] };
type RouteResponse = { primary_route?: RouteOption; backup_route?: RouteOption | null; path?: number[][]; distance_km?: number; estimated_hours?: number; risk_score?: number | null; segments?: RouteOption["segments"] };

const depots: Depot[] = [
  { id: 1, name: "Depot North", altitude_m: 2800, location: [34.2, 77.12] },
  { id: 2, name: "Depot Central", altitude_m: 2450, location: [34.14, 77.28] },
  { id: 3, name: "Depot South", altitude_m: 2200, location: [34.08, 77.43] },
];
const items: Item[] = [
  { id: 1, name: "Fuel", unit: "litres" }, { id: 2, name: "Rations", unit: "packs" },
  { id: 3, name: "Medical kits", unit: "kits" }, { id: 4, name: "Winter clothing", unit: "sets" },
  { id: 5, name: "Spare parts", unit: "units" },
];
const seedVehicles: Vehicle[] = Array.from({ length: 10 }, (_, index) => ({
  id: index + 1, name: `Demo Vehicle ${String(index + 1).padStart(2, "0")}`,
  vehicle_type: index < 6 ? "Cargo truck" : "Utility 4x4", capacity_kg: index < 6 ? 5000 : 1200, available: true,
}));
const seedPosts: Post[] = [
  [1, "Post Alpha", 1650, 77.17, 34.18, 78], [2, "Post Bravo", 2100, 77.23, 34.23, 49],
  [3, "Post Charlie", 2550, 77.31, 34.19, 63], [4, "Post Delta", 2900, 77.37, 34.14, 84],
  [5, "Post Echo", 3300, 77.27, 34.1, 55], [6, "Post Foxtrot", 3650, 77.19, 34.07, 42],
  [7, "Post Golf", 4050, 77.36, 34.06, 91], [8, "Post Hotel", 4400, 77.45, 34.12, 72],
].map(([id, name, altitude_m, longitude, latitude, risk_score]) => ({
  id: Number(id), name: String(name), altitude_m: Number(altitude_m),
  location: { longitude: Number(longitude), latitude: Number(latitude) }, risk_score: Number(risk_score),
  stocks: items.map((item, index) => {
    const quantity = Math.max(4, 180 - Number(risk_score) + index * 12);
    return { item_id: item.id, item: item.name, unit: item.unit, quantity, average_daily_demand_7d: index + 4, risk: { score: Number(risk_score), coverage_days: Math.round(quantity / (index + 4)), days_stock_left: Math.round(quantity / (index + 4)), risk_days: Number(risk_score) >= 70 ? 2.5 : -4, level: Number(risk_score) >= 70 ? "high" : "medium", explanation: "Synthetic stock coverage estimate for prototype display." } };
  }),
}));
const seedAlerts: Alert[] = [
  { id: 1, post_id: 4, item_id: 1, severity: "high", message: "Post Delta has 4 days of Fuel coverage.", suggestion: "Review the forecast and propose a replenishment plan.", created_at: new Date().toISOString(), resolved: false },
  { id: 2, post_id: 7, item_id: 4, severity: "high", message: "Post Golf has low Winter clothing stock.", suggestion: "Schedule a resupply before the next cold-weather window.", created_at: new Date().toISOString(), resolved: false },
  { id: 3, post_id: 8, item_id: 5, severity: "moderate", message: "Road segment Demo Road 4 is closed.", suggestion: "Use the alternate route and confirm access before dispatch.", created_at: new Date().toISOString(), resolved: false },
];
const seedPlans: Plan[] = [
  { id: 101, post_id: 7, depot_id: 2, item_id: 4, quantity: 120, status: "proposed", created_at: new Date().toISOString() },
  { id: 102, post_id: 4, depot_id: 1, item_id: 1, quantity: 450, status: "proposed", created_at: new Date().toISOString() },
  { id: 103, post_id: 8, depot_id: 3, item_id: 5, quantity: 36, status: "approved", created_at: new Date().toISOString() },
];
const seedForecast: ForecastPoint[] = Array.from({ length: 14 }, (_, index) => ({
  date: `Day ${index + 1}`, quantity: Math.round(12 + 2.5 * Math.sin(index / 2) + index * 0.35),
}));
const seedRoadSegments = Array.from({ length: 8 }, (_, index) => ({ id: index + 1, name: `Demo Road ${index + 1}`, status: index === 3 ? "closed" : "open" }));
const CACHE_KEY = "purva-operations-cache";

function readOperationsCache(): CachedOperations | null {
  try {
    const value = localStorage.getItem(CACHE_KEY);
    if (!value) return null;
    const cache = JSON.parse(value) as CachedOperations;
    return cache.savedAt && Array.isArray(cache.posts) && Array.isArray(cache.alerts) && Array.isArray(cache.plans) ? cache : null;
  } catch { return null; }
}

function saveOperationsCache(cache: CachedOperations) {
  try { localStorage.setItem(CACHE_KEY, JSON.stringify(cache)); } catch { /* Cache is optional when storage is full or disabled. */ }
}

function updateCachedPlans(plans: Plan[]): string | null {
  const cache = readOperationsCache();
  if (!cache) return null;
  const savedAt = new Date().toISOString();
  const updated = { ...cache, savedAt, plans };
  saveOperationsCache(updated);
  return savedAt;
}

function toMapPath(path: number[][] | undefined): [number, number][] {
  return Array.isArray(path) ? path.filter((point) => point.length >= 2).map(([longitude, latitude]) => [latitude, longitude]) : [];
}

type AuthProfile = { username: string; role: string };

function readSavedSession(): { token: string; tokenType: string; user: AuthProfile } | null {
  const token = localStorage.getItem("purva-token");
  const savedProfile = localStorage.getItem("purva-auth-profile");
  if (!token || token === "prototype-demo-token" || !savedProfile) {
    ["purva-token", "purva-token-type", "purva-user", "purva-role", "purva-auth-profile"].forEach((key) => localStorage.removeItem(key));
    return null;
  }
  try {
    const user = JSON.parse(savedProfile) as AuthProfile;
    if (!user.username || !user.role) throw new Error("Incomplete saved profile");
    return { token, tokenType: localStorage.getItem("purva-token-type") || "bearer", user };
  } catch {
    ["purva-token", "purva-token-type", "purva-user", "purva-role", "purva-auth-profile"].forEach((key) => localStorage.removeItem(key));
    return null;
  }
}

function Icon({ name, size = 18 }: { name: string; size?: number }) {
  const paths: Record<string, string> = {
    grid: "M3 3h7v7H3z M14 3h7v7h-7z M3 14h7v7H3z M14 14h7v7h-7z",
    pin: "M20 10c0 5-8 12-8 12S4 15 4 10a8 8 0 1 1 16 0Z M12 10a2.5 2.5 0 1 0 0-5 2.5 2.5 0 0 0 0 5Z",
    route: "M5 6a2 2 0 1 0 0-4 2 2 0 0 0 0 4Zm14 16a2 2 0 1 0 0-4 2 2 0 0 0 0 4ZM5 4h7a4 4 0 0 1 4 4v8a4 4 0 0 0 4 4",
    sliders: "M4 21v-7m0-4V3m8 18v-9m0-4V3m8 18v-5m0-4V3M2 14h4m4-6h4m4 8h4",
    clipboard: "M9 5H5a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2V7a2 2 0 0 0-2-2h-4 M9 3h6v4H9z M8 12h8m-8 4h8",
    shield: "M12 22s8-4 8-11V5l-8-3-8 3v6c0 7 8 11 8 11Zm-3-11 2 2 4-4",
    bell: "M18 8a6 6 0 0 0-12 0c0 7-3 7-3 9h18c0-2-3-2-3-9m-8 12h4",
    search: "m20 20-4.3-4.3M18 10.5a7.5 7.5 0 1 1-15 0 7.5 7.5 0 0 1 15 0Z",
    arrow: "M5 12h14m-6-6 6 6-6 6", chevron: "m9 18 6-6-6-6",
    cloud: "M20 16.2A4.5 4.5 0 0 0 18 7.7 6 6 0 0 0 6.3 9 4 4 0 0 0 6 17h13",
    menu: "M4 6h16M4 12h16M4 18h16", check: "m5 12 4 4L19 6", close: "M18 6 6 18M6 6l12 12",
  };
  return <svg aria-hidden="true" width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><path d={paths[name] || paths.grid} /></svg>;
}

function riskClass(risk: string | number) {
  const score = typeof risk === "number" ? risk : Number(risk);
  return score >= 70 || String(risk).toLowerCase() === "high" || String(risk).toLowerCase() === "critical" ? "risk-high" : score >= 35 || String(risk).toLowerCase() === "medium" ? "risk-medium" : "risk-low";
}
function statusClass(status: string) { return status === "proposed" ? "status-pending" : status === "approved" ? "status-approved" : "status-neutral"; }
function findName<T extends { id: number; name: string }>(records: T[], id: number, fallback: string) { return records.find((record) => record.id === id)?.name || fallback; }

export default function App() {
  const [savedSession] = useState(readSavedSession);
  const [savedCache] = useState(readOperationsCache);
  const [token, setToken] = useState(savedSession?.token || null);
  const [username, setUsername] = useState(savedSession?.user.username || "");
  const [role, setRole] = useState(savedSession?.user.role || "");
  const [page, setPage] = useState<Page>("overview");
  const [selectedPost, setSelectedPost] = useState<Post | null>(null);
  const [isOnline, setIsOnline] = useState(navigator.onLine);
  const [queue, setQueue] = useState<QueuedDecision[]>([]);
  const [posts, setPosts] = useState(savedCache?.posts || seedPosts);
  const [alerts, setAlerts] = useState(savedCache?.alerts || seedAlerts);
  const [plans, setPlans] = useState(savedCache?.plans || seedPlans);
  const [summary, setSummary] = useState(savedCache?.summary || { posts: 8, depots: 3, active_alerts: 3, active_plans: 3, available_vehicles: 7 });
  const [cacheTimestamp, setCacheTimestamp] = useState(savedCache?.savedAt || "");
  const [isStaleCache, setIsStaleCache] = useState(Boolean(savedCache && !navigator.onLine));
  const [apiStatus, setApiStatus] = useState<"loading" | "live" | "demo">("loading");
  const [mobileNav, setMobileNav] = useState(false);
  const [notice, setNotice] = useState("");
  const failedSyncKey = useRef("");
  const refreshQueue = useCallback(async () => {
    try { setQueue(await getQueuedDecisions()); } catch { setQueue([]); }
  }, []);

  useEffect(() => {
    const online = () => { failedSyncKey.current = ""; setIsOnline(true); };
    const offline = () => { setIsOnline(false); setIsStaleCache(true); };
    window.addEventListener("online", online);
    window.addEventListener("offline", offline);
    void refreshQueue();
    if ("serviceWorker" in navigator && import.meta.env.PROD) void navigator.serviceWorker.register("/sw.js");
    return () => { window.removeEventListener("online", online); window.removeEventListener("offline", offline); };
  }, [refreshQueue]);

  useEffect(() => {
    if (!token || !isOnline) {
      if (!isOnline && savedCache) setIsStaleCache(true);
      return;
    }
    let active = true;
    let refreshing = false;
    const refresh = async () => {
      if (refreshing) return;
      refreshing = true;
      try {
        const [dashboard, postData, alertData, planData] = await Promise.all([
          api<Dashboard>("/api/dashboard"),
          api<Post[]>("/api/posts"),
          api<Alert[]>("/api/alerts"),
          api<Plan[]>("/api/plans"),
        ]);
        if (!active) return;
        const nextSummary = dashboard.summary;
        const nextPosts = postData.length ? postData : dashboard.posts;
        const cache: CachedOperations = {
          savedAt: new Date().toISOString(),
          summary: nextSummary,
          posts: nextPosts,
          alerts: alertData,
          plans: planData,
        };
        setSummary(nextSummary);
        setPosts(nextPosts);
        setAlerts(alertData);
        setPlans(planData);
        saveOperationsCache(cache);
        setCacheTimestamp(cache.savedAt);
        setIsStaleCache(false);
        setApiStatus("live");
      } catch {
        if (!active) return;
        setApiStatus("demo");
        if (savedCache) {
          setSummary(savedCache.summary);
          setPosts(savedCache.posts);
          setAlerts(savedCache.alerts);
          setPlans(savedCache.plans);
          setCacheTimestamp(savedCache.savedAt);
          setIsStaleCache(true);
        }
      } finally {
        refreshing = false;
      }
    };
    void refresh();
    const interval = window.setInterval(() => { void refresh(); }, 5000);
    return () => {
      active = false;
      window.clearInterval(interval);
    };
  }, [token, isOnline]);

  useEffect(() => {
    if (!isOnline && savedCache) setIsStaleCache(true);
  }, [isOnline, savedCache]);

  useEffect(() => {
    if (!token || !isOnline || !queue.length) return;
    const syncKey = queue.map((action) => action.id).join(",");
    if (failedSyncKey.current === syncKey) return;
    (async () => {
      for (const [index, action] of queue.entries()) {
        try {
          await api(`/api/plans/${action.planId}/decision`, { method: "POST", body: JSON.stringify({ decision: action.decision, comment: action.note }) });
          await removeQueuedDecision(action.id);
          setPlans((current) => current.map((plan) => plan.id === Number(action.planId) ? { ...plan, status: action.decision } : plan));
          const cachedPlans = readOperationsCache()?.plans;
          if (cachedPlans) {
            const savedAt = updateCachedPlans(cachedPlans.map((plan) => plan.id === Number(action.planId) ? { ...plan, status: action.decision } : plan));
            if (savedAt) setCacheTimestamp(savedAt);
          }
        } catch {
          failedSyncKey.current = queue.slice(index).map((pending) => pending.id).join(",");
          break;
        }
      }
      await refreshQueue();
    })();
  }, [isOnline, token, queue, refreshQueue]);

  const onLogin = (accessToken: string, tokenType: string, user: AuthProfile) => {
    localStorage.setItem("purva-token", accessToken);
    localStorage.setItem("purva-token-type", tokenType);
    localStorage.setItem("purva-auth-profile", JSON.stringify(user));
    localStorage.setItem("purva-user", user.username);
    localStorage.setItem("purva-role", user.role);
    setUsername(user.username);
    setRole(user.role);
    setToken(accessToken);
  };
  const logout = () => {
    ["purva-token", "purva-token-type", "purva-user", "purva-role", "purva-auth-profile"].forEach((key) => localStorage.removeItem(key));
    setToken(null);
    setUsername("");
    setRole("");
  };
  if (!token || !username || !role) return <Login onLogin={onLogin} />;

  const navigate = (next: Page) => { setPage(next); setSelectedPost(null); setMobileNav(false); };
  const decide = async (plan: Plan, decision: "approved" | "rejected") => {
    const action: QueuedDecision = { id: crypto.randomUUID(), planId: String(plan.id), decision, note: "", createdAt: new Date().toISOString() };
    if (!isOnline) {
      await enqueueDecision(action); await refreshQueue();
      setNotice("Approval saved offline and will sync when you’re back online.");
      return;
    }
    try {
      await api(`/api/plans/${plan.id}/decision`, { method: "POST", body: JSON.stringify({ decision, comment: "" }) });
      const updatedPlans = plans.map((item) => item.id === plan.id ? { ...item, status: decision } : item);
      setPlans(updatedPlans);
      const savedAt = updateCachedPlans(updatedPlans);
      if (savedAt) setCacheTimestamp(savedAt);
      setNotice(`Plan ${decision}.`);
    } catch {
      await enqueueDecision(action); await refreshQueue();
      setNotice("Could not reach the server. Decision queued for sync.");
    }
  };
  const navigation: { id: Page; label: string; icon: string }[] = [
    { id: "overview", label: "Overview", icon: "grid" }, { id: "posts", label: "Posts & forecasts", icon: "pin" },
    { id: "routes", label: "Route planner", icon: "route" }, { id: "simulator", label: "What-if simulator", icon: "sliders" },
    { id: "plans", label: "Plans & approvals", icon: "clipboard" }, ...(role === "admin" ? [{ id: "audit" as Page, label: "Audit log", icon: "shield" }] : []),
  ];

  return <div className="app-shell">
    <aside className={`sidebar ${mobileNav ? "sidebar-open" : ""}`}>
      <div className="brand"><div className="brand-mark">pd<span>•</span></div><div><b>purva drishti</b><small>LOGISTICS INTELLIGENCE</small></div><button className="nav-close" onClick={() => setMobileNav(false)} aria-label="Close navigation"><Icon name="close" /></button></div>
      <div className="workspace-label">WORKSPACE</div>
      <nav>{navigation.map((item) => <button key={item.id} onClick={() => navigate(item.id)} className={`nav-item ${page === item.id && !selectedPost ? "active" : ""}`}><Icon name={item.icon} /><span>{item.label}</span>{item.id === "plans" && <span className="nav-count">{plans.filter((plan) => plan.status === "proposed").length}</span>}</button>)}</nav>
      <div className="sidebar-bottom"><div className="help-card"><div className="help-icon"><Icon name="cloud" size={17} /></div><b>Supply forecasting, made clear.</b><p>Stock, demand and route signals to support logistics planning.</p><button onClick={() => setNotice("Forecasts compare recent item consumption with available stock and route conditions.")}>Methodology <Icon name="arrow" size={14} /></button></div><div className="user-card"><div className="avatar">{username.slice(0, 2).toUpperCase()}</div><div><b>{username}</b><span>{role}</span></div><button className="logout-button" onClick={logout} title="Sign out">↗</button></div></div>
    </aside>
    {mobileNav && <button className="nav-scrim" onClick={() => setMobileNav(false)} aria-label="Close navigation" />}
    <main className="main-area">
      <header className="topbar"><button className="mobile-menu" onClick={() => setMobileNav(true)} aria-label="Open navigation"><Icon name="menu" /></button><div className="breadcrumb">Logistics Network <span>/</span> <b>{selectedPost?.name || navigation.find((item) => item.id === page)?.label}</b></div><div className="topbar-actions"><div className="sync-status"><i className={isOnline && apiStatus !== "demo" ? "dot-green" : "dot-amber"} />{!isOnline ? "Working offline" : apiStatus === "live" ? "All systems operational" : apiStatus === "loading" ? "Connecting to API" : "Synthetic data active"}{queue.length > 0 && <span className="queue-pill">{queue.length} queued</span>}</div><button className="icon-button" aria-label="Notifications" onClick={() => setNotice("No new logistics alerts.")}><Icon name="bell" /></button><div className="top-avatar">{username.slice(0, 2).toUpperCase()}</div></div></header>
      {!isOnline && <div className="offline-banner"><Icon name="cloud" size={16} /><span>You’re offline. Approval actions are saved on this device and will sync automatically.</span>{queue.length > 0 && <b>{queue.length} pending</b>}</div>}
      {notice && <div className="toast"><span>{notice}</span><button onClick={() => setNotice("")} aria-label="Dismiss"><Icon name="close" size={15} /></button></div>}
      <div className="page-content">
        <div className="prototype-banner"><span>✳</span> Prototype - synthetic data <i /> Fictional depots, posts, inventory and routes</div>
        {isStaleCache && <div className="stale-cache-banner"><Icon name="cloud" size={15} /><span>Showing stale cached operations data from {cacheTimestamp ? new Date(cacheTimestamp).toLocaleString() : "the last successful sync"}.</span></div>}
        {selectedPost ? <PostDetail post={selectedPost} onBack={() => setSelectedPost(null)} /> :
          page === "overview" ? <Overview posts={posts} alerts={alerts} summary={summary} onSelectPost={setSelectedPost} onNavigate={navigate} apiStatus={apiStatus} /> :
          page === "posts" ? <Posts posts={posts} onSelectPost={setSelectedPost} /> :
          page === "routes" ? <RoutePlanner posts={posts} /> :
          page === "simulator" ? <Simulator posts={posts} /> :
          page === "plans" ? <Plans plans={plans} posts={posts} role={role} onDecision={decide} onCreated={(plan) => {
            const updated = [plan, ...plans];
            setPlans(updated);
            const savedAt = updateCachedPlans(updated);
            if (savedAt) setCacheTimestamp(savedAt);
          }} /> :
          <AuditPage />}
      </div>
    </main>
  </div>;
}

function Login({ onLogin }: { onLogin: (token: string, tokenType: string, user: AuthProfile) => void }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setLoading(true);
    setError("");
    const body = new URLSearchParams({ username, password });
    try {
      const result = await api<{ access_token: string; token_type: string; user: AuthProfile }>("/api/auth/token", { method: "POST", body });
      if (!result.access_token || !result.token_type || !result.user?.username || !result.user?.role) {
        setError("The server returned an incomplete sign-in response. Contact your administrator.");
      } else {
        onLogin(result.access_token, result.token_type, result.user);
      }
    } catch (requestError) {
      setError(requestError instanceof ApiError && (requestError.status === 401 || requestError.status === 403)
        ? "Those credentials could not be verified. Check your details and try again."
        : "Login is unavailable because the authentication service could not be reached. Check your connection and try again.");
    } finally { setLoading(false); }
  };
  return <div className="login-page"><div className="login-left"><div className="login-brand"><div className="brand-mark">pd<span>•</span></div><div><b>purva drishti</b><small>LOGISTICS INTELLIGENCE</small></div></div><div className="login-message"><div className="eyebrow">SUPPLY READINESS</div><h1>See what’s needed.<br /><em>Plan what moves.</em></h1><p>Forecast stock requirements. Coordinate dispatch. Keep remote posts supplied.</p><div className="login-stat-row"><div><b>14d</b><span>demand forecast</span></div><div><b>8</b><span>fictional posts</span></div><div><b>3</b><span>fictional depots</span></div></div></div><div className="login-footer">PURVA DRISHTI <span>·</span> SYNTHETIC MILITARY LOGISTICS PROTOTYPE</div></div><section className="login-form-panel"><div className="login-form-wrap"><div className="mobile-login-logo"><div className="brand-mark">pd<span>•</span></div><b>purva drishti</b></div><div className="eyebrow">AUTHORISED ACCESS</div><h2>Sign in to logistics workspace</h2><p className="form-subtitle">Use your assigned account to review stock and dispatch plans.</p><form onSubmit={submit}><label htmlFor="username">Username</label><input id="username" autoComplete="username" placeholder="Enter your username" value={username} onChange={(event) => setUsername(event.target.value)} required /><label htmlFor="password">Password</label><input id="password" type="password" autoComplete="current-password" placeholder="Enter your password" value={password} onChange={(event) => setPassword(event.target.value)} required /><div className="form-options"><span className="check-label">Credentials are verified by the operations API.</span></div>{error && <div className="form-error" role="alert">{error}</div>}<button className="primary-button login-submit" disabled={loading}>{loading ? "Signing in…" : "Sign in"} <Icon name="arrow" size={16} /></button></form><div className="login-note"><span>✳</span> Prototype - synthetic data. All records and map locations are fictional.</div><div className="login-privacy">Authorised logistics workspace <span>·</span> Synthetic demo environment</div></div></section></div>;
}

function PageHeading({ eyebrow, title, description, action }: { eyebrow: string; title: string; description: string; action?: ReactNode }) {
  return <div className="page-heading"><div><div className="eyebrow">{eyebrow}</div><h1>{title}</h1><p>{description}</p></div>{action}</div>;
}

function Overview({ posts, alerts, summary, onSelectPost, onNavigate, apiStatus }: { posts: Post[]; alerts: Alert[]; summary: Dashboard["summary"]; onSelectPost: (post: Post) => void; onNavigate: (page: Page) => void; apiStatus: string }) {
  const highRisk = posts.filter((post) => post.risk_score >= 70).length;
  const lowStock = posts.reduce((count, post) => count + post.stocks.filter((stock) => stock.risk.score >= 70).length, 0);
  return <><PageHeading eyebrow="SUPPLY NETWORK · LIVE READINESS" title="Logistics overview" description="Inventory, demand and route readiness across the fictional forward supply network." action={<button className="outline-button" onClick={() => onNavigate("simulator")}><Icon name="sliders" size={16} /> Run a scenario</button>} />
    {apiStatus === "demo" && <div className="api-note">API unavailable — displaying synthetic logistics data.</div>}
    <div className="metric-grid"><MetricCard label="Posts monitored" value={String(summary.posts).padStart(2, "0")} trend={`${summary.depots} supply depots`} icon="pin" tone="mint" /><MetricCard label="Posts with high stock risk" value={String(highRisk).padStart(2, "0")} trend="Review replenishment needs" icon="shield" tone="sand" /><MetricCard label="Items flagged for resupply" value={String(lowStock).padStart(2, "0")} trend="Across all monitored posts" icon="grid" tone="lilac" /><MetricCard label="Active dispatch plans" value={String(summary.active_plans).padStart(2, "0")} trend={`${summary.available_vehicles} vehicles available`} icon="clipboard" tone="peach" /></div>
    <div className="overview-grid"><section className="panel map-panel"><div className="panel-header"><div><h2>Fictional high-altitude network</h2><p>Supply depots, monitored posts and sample road segments</p></div><button className="subtle-button" onClick={() => onNavigate("posts")}>View posts <Icon name="arrow" size={14} /></button></div><div className="map-wrap"><MapContainer center={[34.16, 77.29]} zoom={8} scrollWheelZoom={false} zoomControl={false} className="field-map"><TileLayer attribution='&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>' url="https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png" /><Polyline positions={posts.slice(0, 5).map((post) => [post.location.latitude, post.location.longitude] as [number, number])} pathOptions={{ color: "#647c6e", weight: 2, dashArray: "5 8", opacity: 0.55 }} />{depots.map((depot) => <CircleMarker key={`depot-${depot.id}`} center={depot.location} radius={7} pathOptions={{ color: "#fff", fillColor: "#476b58", fillOpacity: 1, weight: 2 }}><Tooltip>{depot.name} · {depot.altitude_m}m</Tooltip></CircleMarker>)}{posts.map((post) => <CircleMarker key={post.id} center={[post.location.latitude, post.location.longitude]} radius={post.risk_score >= 70 ? 8 : 6} pathOptions={{ color: "#fff", fillColor: post.risk_score >= 70 ? "#d96b5e" : post.risk_score >= 35 ? "#e4a166" : "#75a68c", fillOpacity: 0.9, weight: 2 }} eventHandlers={{ click: () => onSelectPost(post) }}><Tooltip>{post.name} · {post.altitude_m}m · risk {post.risk_score}</Tooltip></CircleMarker>)}</MapContainer><div className="map-legend"><span><i className="legend-dot critical-dot" />High stock risk</span><span><i className="legend-dot high-dot" />Watch</span><span><i className="legend-dot safe-dot" />Stable</span></div><div className="map-attribution">Illustrative network · OpenStreetMap base map</div></div><div className="map-footer"><span><i className="live-dot" /> Inventory refreshed from API</span><button onClick={() => onNavigate("routes")}>Open route planner <Icon name="arrow" size={14} /></button></div></section>
      <section className="panel alerts-panel"><div className="panel-header"><div><h2>Supply & route alerts</h2><p>Stock coverage and access updates</p></div><span className="alert-count">{String(alerts.length).padStart(2, "0")}</span></div><div className="alert-list">{alerts.slice(0, 3).map((alert, index) => <button key={`${alert.id}-${index}`} className="alert-item" onClick={() => { const post = posts.find((entry) => entry.id === alert.post_id); if (post) onSelectPost(post); }}><div className={`alert-marker ${alert.severity === "high" || alert.severity === "severe" ? "critical" : "watch"}`}><span /></div><div className="alert-copy"><div className="alert-head"><b>{alert.message}</b><time>{new Date(alert.created_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}</time></div><p>{alert.suggestion}</p><span className={`alert-level ${alert.severity === "high" || alert.severity === "severe" ? "critical" : "watch"}`}>{alert.severity}</span></div><Icon name="chevron" size={17} /></button>)}</div><button className="alerts-all" onClick={() => onNavigate("posts")}>View stock positions <Icon name="arrow" size={14} /></button></section></div>
    <section className="panel posts-panel"><div className="panel-header"><div><h2>Monitored posts</h2><p>Altitude, inventory exposure and supply coverage</p></div><button className="subtle-button" onClick={() => onNavigate("posts")}>All posts <Icon name="arrow" size={14} /></button></div><div className="table-scroll"><table><thead><tr><th>POST</th><th>ALTITUDE</th><th>STOCK RISK</th><th>LOWEST COVERAGE ITEM</th><th>ON-HAND STOCK</th><th>DEMAND / DAY</th><th></th></tr></thead><tbody>{posts.slice(0, 5).map((post) => { const stock = [...post.stocks].sort((a, b) => (a.risk.coverage_days ?? Infinity) - (b.risk.coverage_days ?? Infinity))[0]; return <tr key={post.id} onClick={() => onSelectPost(post)}><td><span className="post-dot" />{post.name}</td><td>{post.altitude_m.toLocaleString()} m</td><td><span className={`risk-pill ${riskClass(post.risk_score)}`}><i />{Math.round(post.risk_score)} / 100</span></td><td>{stock?.item || "—"}</td><td>{stock ? `${Math.round(stock.quantity)} ${stock.unit}` : "—"}</td><td>{stock?.risk.coverage_days ? `${(stock.quantity / stock.risk.coverage_days).toFixed(1)} ${stock.unit}` : "—"}</td><td><Icon name="chevron" size={16} /></td></tr>; })}</tbody></table></div></section>
    <div className="methodology-strip"><div className="method-icon"><Icon name="cloud" size={18} /></div><div><b>Methodology</b><p>Collect data → Integrate and clean → Forecast demand → Check supply risk → Plan route → Officer approves → Dispatch and learn.</p></div><button onClick={() => onSelectPost(posts[0])}>Inspect a post <Icon name="arrow" size={14} /></button></div>
  </>;
}

function MetricCard({ label, value, trend, icon, tone }: { label: string; value: string; trend: string; icon: string; tone: string }) {
  return <div className="metric-card"><div className="metric-top"><span>{label}</span><div className={`metric-icon ${tone}`}><Icon name={icon} size={18} /></div></div><div className="metric-value">{value}</div><div className="metric-trend"><span className="trend-line">↗</span>{trend}</div></div>;
}

function Posts({ posts, onSelectPost }: { posts: Post[]; onSelectPost: (post: Post) => void }) {
  const [search, setSearch] = useState("");
  const [filter, setFilter] = useState("All risk levels");
  const filtered = posts.filter((post) => post.name.toLowerCase().includes(search.toLowerCase()) && (filter === "All risk levels" || (filter === "High" ? post.risk_score >= 70 : filter === "Moderate" ? post.risk_score >= 35 && post.risk_score < 70 : post.risk_score < 35)));
  return <><PageHeading eyebrow="INVENTORY & DEMAND" title="Posts & forecasts" description="Review fictional post inventory, daily demand and predicted stock coverage." /><div className="filter-row"><div className="search-field"><Icon name="search" size={17} /><input placeholder="Search Post Alpha–Hotel…" value={search} onChange={(event) => setSearch(event.target.value)} /></div><select value={filter} onChange={(event) => setFilter(event.target.value)}><option>All risk levels</option><option>High</option><option>Moderate</option><option>Low</option></select><span className="result-count">{filtered.length} posts</span></div><div className="post-card-grid">{filtered.map((post) => { const stock = [...post.stocks].sort((a, b) => (a.risk.coverage_days ?? Infinity) - (b.risk.coverage_days ?? Infinity))[0]; return <button className="post-card panel" key={post.id} onClick={() => onSelectPost(post)}><div className="post-card-top"><span className={`risk-pill ${riskClass(post.risk_score)}`}><i />Risk {Math.round(post.risk_score)}</span><Icon name="arrow" size={17} /></div><h2>{post.name}</h2><p>Fictional post · elevation {post.altitude_m.toLocaleString()} m</p><div className="post-card-stats"><div><span>LOWEST STOCK</span><b>{stock?.item || "—"}</b></div><div><span>COVERAGE</span><b>{stock?.risk.coverage_days ?? "—"} <small>days</small></b></div></div><div className="post-card-foot"><span>{post.stocks.length} supply categories tracked</span><span>View details <Icon name="arrow" size={13} /></span></div></button>; })}</div></>;
}

function PostDetail({ post, onBack }: { post: Post; onBack: () => void }) {
  const [itemId, setItemId] = useState(post.stocks[0]?.item_id || 1);
  const [forecastData, setForecastData] = useState(seedForecast);
  const [prophetData, setProphetData] = useState<ForecastPoint[] | null>(null);
  const [mape, setMape] = useState<number | null>(12.4);
  const [prophetMape, setProphetMape] = useState<number | null>(null);
  const [forecastSource, setForecastSource] = useState("synthetic");
  const selectedItem = post.stocks.find((stock) => stock.item_id === itemId);
  useEffect(() => {
    withFallback(api<{ forecast: ForecastPoint[]; mape: number | null; prophet_baseline: ForecastPoint[] | null; prophet_mape: number | null }>(`/api/posts/${post.id}/forecast?item_id=${itemId}`), null).then((data) => {
      if (data?.forecast) {
        setForecastData(data.forecast);
        setProphetData(data.prophet_baseline);
        setMape(data.mape);
        setProphetMape(data.prophet_mape);
        setForecastSource("live");
      }
    });
  }, [post.id, itemId]);
  const chartData = forecastData.map((entry) => ({ ...entry, prophet: prophetData?.find((point) => point.date === entry.date)?.quantity }));
  return <><button className="back-link" onClick={onBack}>← Back to monitored posts</button><PageHeading eyebrow="SUPPLY POST · INVENTORY PROFILE" title={post.name} description={`Fictional logistics post · elevation ${post.altitude_m.toLocaleString()} m · synthetic map position`} action={<span className={`risk-pill ${riskClass(post.risk_score)}`}><i />Risk score {Math.round(post.risk_score)}</span>} /><div className="detail-grid"><section className="panel chart-panel"><div className="panel-header"><div><h2>Daily demand forecast</h2><p>Expected item consumption · next 14 days</p></div><select aria-label="Forecast item" value={itemId} onChange={(event) => setItemId(Number(event.target.value))}>{post.stocks.map((stock) => <option key={stock.item_id} value={stock.item_id}>{stock.item}</option>)}</select></div><div className="chart-legend"><span><i className="chart-key-forecast" />XGBoost / primary</span>{prophetData && <span><i className="chart-key-observed" />Prophet baseline</span>}</div><div className="chart-holder"><ResponsiveContainer width="100%" height="100%"><AreaChart data={chartData} margin={{ top: 8, right: 9, left: -20, bottom: 0 }}><defs><linearGradient id="fillForecast" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stopColor="#6d9a7e" stopOpacity={0.23} /><stop offset="100%" stopColor="#6d9a7e" stopOpacity={0.02} /></linearGradient></defs><CartesianGrid stroke="#edf0ec" vertical={false} /><XAxis dataKey="date" tickLine={false} axisLine={false} interval={2} tick={{ fill: "#87918a", fontSize: 10 }} /><YAxis tickLine={false} axisLine={false} tick={{ fill: "#87918a", fontSize: 11 }} /><ChartTooltip /><Area type="monotone" dataKey="quantity" stroke="#315a49" strokeWidth={2} fill="url(#fillForecast)" name="Primary forecast" /><Area type="monotone" dataKey="prophet" stroke="#bd8756" strokeWidth={2} strokeDasharray="5 4" fill="none" name="Prophet baseline" connectNulls /></AreaChart></ResponsiveContainer></div><div className="chart-footnote"><span>Current stock <b>{selectedItem ? `${selectedItem.quantity.toFixed(0)} ${selectedItem.unit}` : "—"}</b></span><span>MAPE: Primary {mape == null ? "—" : `${mape.toFixed(1)}%`} · Prophet {prophetMape == null ? "—" : `${prophetMape.toFixed(1)}%`}</span></div></section><section className="panel risk-panel"><div className="panel-header"><div><h2>Stock risk assessment</h2><p>Coverage for selected item</p></div></div><div className="risk-score"><div className={`risk-score-number ${riskClass(selectedItem?.risk.score || 0)}`}>{Math.round(selectedItem?.risk.score || post.risk_score)}<small>/100</small></div><div><span className={`risk-pill ${riskClass(selectedItem?.risk.score || 0)}`}><i />{selectedItem?.risk.level || "medium"} risk</span><p>{selectedItem?.risk.coverage_days ?? selectedItem?.risk.days_stock_left ?? "—"} days stock cover</p></div></div><div className="risk-factors"><h3>Supply position</h3><div><span>Selected item</span><b>{selectedItem?.item || "—"}</b></div><div><span>On-hand stock</span><b>{selectedItem ? `${selectedItem.quantity.toFixed(1)} ${selectedItem.unit}` : "—"}</b></div><div><span>Daily demand</span><b>{selectedItem?.average_daily_demand_7d != null ? `${selectedItem.average_daily_demand_7d.toFixed(2)} ${selectedItem.unit}` : "Not available"}</b></div><div><span>Stock coverage</span><b>{selectedItem?.risk.days_stock_left ?? selectedItem?.risk.coverage_days ?? "—"} days</b></div><div><span>Days to safe window</span><b>{selectedItem?.risk.days_to_next_safe_window ?? post.risk?.days_to_next_safe_window ?? "—"} days</b></div><div><span>Risk window delta</span><b>{selectedItem?.risk.risk_days == null ? "—" : `${selectedItem.risk.risk_days} days`}</b></div><div><span>Altitude</span><b>{post.altitude_m.toLocaleString()} m</b></div>{(selectedItem?.risk.explanation || post.risk?.explanation) && <p className="risk-explanation">{selectedItem?.risk.explanation || post.risk?.explanation}</p>}</div><div className="mape-box"><div><b>Forecast performance</b><span>MAPE</span></div><strong>{mape == null ? "—" : mape.toFixed(1)}<small>{mape == null ? "" : "%"}</small></strong><p>Primary model MAPE · Prophet {prophetMape == null ? "—" : `${prophetMape.toFixed(1)}%`}</p></div></section></div><div className="methodology-strip"><div className="method-icon"><Icon name="shield" size={18} /></div><div><b>Prototype supply-planning guidance</b><p>Inventory and demand values are synthetic. Confirm stock and route availability with the relevant logistics team before dispatch.</p></div></div></>;
}

function RoutePlanner({ posts }: { posts: Post[] }) {
  const [depotId, setDepotId] = useState(1);
  const [postId, setPostId] = useState(posts[0]?.id || 1);
  const [route, setRoute] = useState("primary");
  const [routes, setRoutes] = useState<{ primary: RouteOption; backup: RouteOption | null } | null>(null);
  const [message, setMessage] = useState("Loading route options…");
  const [loading, setLoading] = useState(false);
  const selectedPost = posts.find((post) => post.id === postId) || posts[0];
  const fetchRoutes = async () => {
    setLoading(true);
    try {
      const response = await api<RouteResponse>("/api/routes?" + new URLSearchParams({ depot_id: String(depotId), post_id: String(postId) }));
      const primary = response.primary_route || (response.path ? {
        path: response.path, segments: response.segments || [], distance_km: response.distance_km || 0,
        estimated_hours: response.estimated_hours || 0, risk_score: response.risk_score ?? null,
      } : null);
      if (!primary) throw new Error("The route service returned no primary route.");
      setRoutes({ primary, backup: response.backup_route || null });
      setMessage(response.backup_route ? "Primary and backup routes loaded from the operations API." : "Primary route loaded. The API did not return a backup route.");
    } catch (error) {
      setRoutes(null);
      setMessage(error instanceof Error ? `Route API error: ${error.message}` : "Route API error: Unable to load route options.");
    }
    finally { setLoading(false); }
  };
  useEffect(() => { void fetchRoutes(); }, [depotId, postId]);
  const primary = routes?.primary;
  const backup = routes?.backup;
  const mapPrimary = toMapPath(primary?.path) as [number, number][];
  const mapBackup = toMapPath(backup?.path) as [number, number][];
  const selectedRoute = route === "backup" && backup ? backup : primary;
  const namesFor = (option: RouteOption | null) => option?.segments?.map((segment) => segment.name).filter(Boolean).join(" → ") || "Route segments unavailable";
  const renderOption = (id: "primary" | "backup", option: RouteOption | null) => (
    <button className={`route-option panel ${route === id ? "route-selected" : ""} ${!option ? "route-option-disabled" : ""}`} key={id} onClick={() => option && setRoute(id)} disabled={!option}>
      <div className={`route-choice ${id === "primary" ? "primary-route" : "backup-route"}`}><span /></div>
      <div className="route-option-body"><div className="route-option-title"><b>{id === "primary" ? "Primary route" : "Backup route"}</b>{id === "primary" && option && <span className="recommended">RECOMMENDED</span>}</div>
        {option ? <><p>{namesFor(option)}</p><div className="route-meta"><b>{option.estimated_hours.toFixed(1)} hr</b><span>{option.distance_km.toFixed(1)} km</span></div><div className="route-condition condition-good">● Route risk {option.risk_score ?? "not scored"}</div></> : <p>No backup route was returned for this depot and post.</p>}
      </div>
    </button>
  );
  return <><PageHeading eyebrow="DISPATCH PLANNING" title="Route planner" description="Compare API-provided primary and backup logistics routes between depots and forward posts." action={<button className="primary-button" onClick={() => void fetchRoutes()} disabled={loading}>{loading ? "Refreshing…" : "Refresh routes"} <Icon name="arrow" size={15} /></button>} /><div className="route-controls panel"><label>DEPOT<select value={depotId} onChange={(event) => { setDepotId(Number(event.target.value)); setRoutes(null); }}><option value={1}>Depot North</option><option value={2}>Depot Central</option><option value={3}>Depot South</option></select></label><Icon name="arrow" size={18} /><label>DESTINATION POST<select value={postId} onChange={(event) => { setPostId(Number(event.target.value)); setRoutes(null); }}>{posts.map((post) => <option key={post.id} value={post.id}>{post.name}</option>)}</select></label><div className="route-live"><i className="live-dot" /> Fictional network · {depots.find((depot) => depot.id === depotId)?.altitude_m} m depot / {selectedPost?.altitude_m} m post</div></div>{message && <div className="api-note">{message}</div>}<div className="route-results"><div className="panel route-map-panel"><div className="panel-header"><div><h2>Recommended routes</h2><p>Only route geometry returned by the API is drawn</p></div><span className="route-tag">{backup ? "2 ROUTES" : "1 ROUTE"}</span></div><div className="map-wrap route-map"><MapContainer center={[34.16, 77.29]} zoom={8} scrollWheelZoom={false} className="field-map"><TileLayer attribution="&copy; OpenStreetMap" url="https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png" />{mapPrimary.length > 0 && <Polyline positions={mapPrimary} pathOptions={{ color: route === "primary" ? "#315a49" : "#bdc0a8", weight: route === "primary" ? 5 : 3, dashArray: route === "primary" ? undefined : "7 6" }} />}{mapBackup.length > 0 && <Polyline positions={mapBackup} pathOptions={{ color: route === "backup" ? "#bc8757" : "#bdc0a8", weight: route === "backup" ? 5 : 3, dashArray: route === "backup" ? undefined : "7 6", opacity: route === "backup" ? 1 : 0.65 }} />}<CircleMarker center={depots.find((depot) => depot.id === depotId)?.location || depots[0].location} radius={7} pathOptions={{ color: "#fff", fillColor: "#355c4c", fillOpacity: 1, weight: 2 }} /><CircleMarker center={[selectedPost.location.latitude, selectedPost.location.longitude]} radius={8} pathOptions={{ color: "#fff", fillColor: "#cd8064", fillOpacity: 1, weight: 2 }}><Tooltip>{selectedPost.name}</Tooltip></CircleMarker></MapContainer><div className="map-attribution">Illustrative map · route geometry from operations API</div></div></div><div className="route-option-list">{renderOption("primary", primary || null)}{renderOption("backup", backup || null)}</div></div><div className="route-disclaimer"><Icon name="cloud" size={17} /><span>{selectedRoute ? "Route data is returned by the API. Confirm vehicle access and altitude capability before dispatch." : "Waiting for route API data. No synthetic route is shown as an API result."}</span></div></>;
}

function Simulator({ posts }: { posts: Post[] }) {
  const [postId, setPostId] = useState(posts[0]?.id || 1);
  const [depotId, setDepotId] = useState(1);
  const [itemId, setItemId] = useState(1);
  const [additional, setAdditional] = useState(80);
  const [multiplier, setMultiplier] = useState(1.2);
  const [roadClosure, setRoadClosure] = useState(false);
  const [roadSegments, setRoadSegments] = useState(seedRoadSegments.filter((segment) => segment.status === "open"));
  const [roadSegmentId, setRoadSegmentId] = useState<number | "">("");
  const [closureDays, setClosureDays] = useState(3);
  const [result, setResult] = useState<{
    current_stock?: number;
    projected_stock?: number;
    daily_demand?: number;
    current_risk?: SupplyRisk;
    projected_risk?: SupplyRisk;
    projected_days_to_next_safe_window?: number;
    road_closure_days?: number;
    road_closure_impact?: string;
    route_impact?: {
      available: boolean;
      baseline_distance_km: number;
      alternate_distance_km: number | null;
      distance_change_km: number | null;
    } | null;
  } | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => {
    let active = true;
    api<RouteResponse>("/api/routes?" + new URLSearchParams({ depot_id: String(depotId), post_id: String(postId) })).then((response) => {
      if (!active) return;
      const segments = [...(response.primary_route?.segments || response.segments || []), ...(response.backup_route?.segments || [])]
        .filter((segment) => segment.id !== null)
        .filter((segment, index, all) => all.findIndex((entry) => entry.id === segment.id) === index)
        .map((segment) => ({ id: segment.id as number, name: segment.name, status: "open" }));
      if (segments.length) {
        setRoadSegments(segments);
        setRoadSegmentId((current) => segments.some((segment) => segment.id === current) ? current : segments[0].id);
      } else {
        setRoadSegments([]);
        setRoadSegmentId("");
      }
    }).catch(() => {
      if (active) setError("Route API is unavailable; road-segment choices are based on the fictional seed data.");
    });
    return () => { active = false; };
  }, [depotId, postId]);
  const run = async () => {
    setBusy(true);
    setError("");
    setResult(null);
    if (roadClosure && roadSegmentId === "") {
      setError("Choose a depot and an open road segment before simulating a closure.");
      setBusy(false);
      return;
    }
    try {
      const payload: { post_id: number; item_id: number; additional_quantity: number; demand_multiplier: number; road_closure: boolean; road_closure_days: number; depot_id?: number; road_segment_id?: number } = {
        post_id: postId, item_id: itemId, additional_quantity: additional, demand_multiplier: multiplier, road_closure: roadClosure, road_closure_days: closureDays,
      };
      if (roadClosure) {
        payload.depot_id = depotId;
        payload.road_segment_id = Number(roadSegmentId);
      }
      const data = await api<NonNullable<typeof result>>("/api/what-if", { method: "POST", body: JSON.stringify(payload) });
      setResult(data);
    } catch (requestError) {
      setError(requestError instanceof Error ? `Scenario API error: ${requestError.message}` : "Scenario API error: unable to run the scenario.");
    } finally { setBusy(false); }
  };
  const post = posts.find((entry) => entry.id === postId) || posts[0];
  const selectedItem = items.find((entry) => entry.id === itemId);

  return (
    <>
      <PageHeading
        eyebrow="LOGISTICS SCENARIO PLANNING"
        title="What-if simulator"
        description="Test how replenishment, demand changes and a road closure affect supply coverage."
      />
      <div className="simulator-layout">
        <section className="panel simulator-controls">
          <div className="panel-header">
            <div><h2>Set your scenario</h2><p>Adjust fictional supply and route conditions.</p></div>
          </div>
          <label className="control-label">FORWARD POST
            <select value={postId} onChange={(event) => setPostId(Number(event.target.value))}>
              {posts.map((entry) => <option key={entry.id} value={entry.id}>{entry.name}</option>)}
            </select>
          </label>
          <label className="control-label">DEPOT
            <select value={depotId} onChange={(event) => setDepotId(Number(event.target.value))}>
              {depots.map((depot) => <option key={depot.id} value={depot.id}>{depot.name}</option>)}
            </select>
          </label>
          <label className="control-label">SUPPLY ITEM
            <select value={itemId} onChange={(event) => setItemId(Number(event.target.value))}>
              {items.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}
            </select>
          </label>
          <div className="slider-control">
            <div><label htmlFor="stock-slider">Additional stock</label><b>{additional} <small>{selectedItem?.unit}</small></b></div>
            <input id="stock-slider" type="range" min="0" max="500" step="10" value={additional} onChange={(event) => setAdditional(Number(event.target.value))} />
            <div className="range-labels"><span>0</span><span>500</span></div>
          </div>
          <div className="slider-control">
            <div><label htmlFor="demand-slider">Demand multiplier</label><b>{multiplier.toFixed(1)} <small>×</small></b></div>
            <input id="demand-slider" type="range" min="0.5" max="3" step="0.1" value={multiplier} onChange={(event) => setMultiplier(Number(event.target.value))} />
            <div className="range-labels"><span>0.5×</span><span>3×</span></div>
          </div>
          <label className="closure-toggle">
            <input type="checkbox" checked={roadClosure} onChange={(event) => { setRoadClosure(event.target.checked); setResult(null); }} />
            <span>Model a road closure</span>
          </label>
          {roadClosure && <>
            <label className="control-label">ROAD SEGMENT
              <select value={roadSegmentId} onChange={(event) => setRoadSegmentId(Number(event.target.value))}>
                <option value="" disabled>Select an open segment</option>
                {roadSegments.map((segment) => <option key={segment.id} value={segment.id}>{segment.name}</option>)}
              </select>
            </label>
            <div className="slider-control">
              <div><label htmlFor="closure-days">Closure duration</label><b>{closureDays} <small>days</small></b></div>
              <input id="closure-days" type="range" min="1" max="14" step="1" value={closureDays} onChange={(event) => setClosureDays(Number(event.target.value))} />
              <div className="range-labels"><span>1 day</span><span>14 days</span></div>
            </div>
          </>}
          {error && <div className="form-error scenario-error" role="alert">{error}</div>}
          <button className="primary-button run-scenario" onClick={() => void run()} disabled={busy}>
            {busy ? "Running scenario…" : "Run scenario"} <Icon name="arrow" size={15} />
          </button>
          <p className="scenario-note">Exploratory inventory planning only. No dispatch is created by this simulation.</p>
        </section>
        <section className={`panel scenario-result ${result ? "result-active" : ""}`}>
          <div className="panel-header">
            <div><h2>Scenario outcome</h2><p>{result ? `${post.name} · ${selectedItem?.name}` : "Run a scenario to view projected supply coverage."}</p></div>
            <div className="scenario-icon"><Icon name="sliders" /></div>
          </div>
          {result ? <>
            <div className="result-risk">
              <span>Projected stock risk</span>
              <b className={riskClass(result.projected_risk?.score || 0)}>
                <i />{result.projected_risk?.level || "unknown"} · {result.projected_risk?.score ?? "—"}/100
              </b>
            </div>
            <div className="result-impact">
              <div><span>Current on-hand</span><b>{result.current_stock?.toFixed(1)} {selectedItem?.unit}</b></div>
              <div><span>Projected on-hand</span><b>{result.projected_stock?.toFixed(1)} {selectedItem?.unit}</b></div>
              <div><span>Daily demand</span><b>{result.daily_demand?.toFixed(2)} {selectedItem?.unit}/day</b></div>
              {result.road_closure_days ? <div><span>Road closure</span><b>{result.road_closure_days} days · {result.road_closure_impact}</b></div> : null}
              {result.projected_days_to_next_safe_window != null && <div><span>Projected safe window</span><b>{result.projected_days_to_next_safe_window} days</b></div>}
              {result.route_impact && <div><span>Backup route</span><b>{result.route_impact.available ? `${result.route_impact.alternate_distance_km} km · ${result.route_impact.distance_change_km} km change` : "No alternate route available"}</b></div>}
            </div>
            {result.projected_risk?.explanation && <div className="result-callout">{result.projected_risk.explanation}</div>}
          </> : <div className="empty-scenario"><div className="empty-icon">↗</div><b>Model your supply plan</b><p>Choose a depot, post and item, then adjust incoming stock, demand and road access to explore supply coverage.</p></div>}
        </section>
      </div>
    </>
  );
}

function Plans({ plans, posts, role, onDecision, onCreated }: { plans: Plan[]; posts: Post[]; role: string; onDecision: (plan: Plan, decision: "approved" | "rejected") => void; onCreated: (plan: Plan) => void }) {
  const [filter, setFilter] = useState("All plans");
  const [message, setMessage] = useState("");
  const [postId, setPostId] = useState(posts[0]?.id || 1);
  const [itemId, setItemId] = useState(1);
  const [depotId, setDepotId] = useState(1);
  const [quantity, setQuantity] = useState(100);
  const [vehicles, setVehicles] = useState(seedVehicles);
  const [vehicleId, setVehicleId] = useState(seedVehicles[0].id);
  const [vehicleSource, setVehicleSource] = useState("seeded synthetic vehicles");
  const [routes, setRoutes] = useState<{ primary: RouteOption; backup: RouteOption | null } | null>(null);
  const [routeMessage, setRouteMessage] = useState("Loading route options…");
  const [departureAt, setDepartureAt] = useState(() => {
    const date = new Date(Date.now() + 60 * 60 * 1000);
    date.setMinutes(date.getMinutes() - date.getTimezoneOffset());
    return date.toISOString().slice(0, 16);
  });
  const [explanation, setExplanation] = useState("");
  const [showForm, setShowForm] = useState(false);
  const filtered = plans.filter((plan) => filter === "All plans" || plan.status === (filter === "Awaiting approval" ? "proposed" : filter.toLowerCase()));
  const pending = plans.filter((plan) => plan.status === "proposed").length;
  useEffect(() => {
    let active = true;
    api<Vehicle[]>("/api/vehicles").then((data) => {
      if (!active || !Array.isArray(data)) return;
      const available = data.filter((vehicle) => vehicle.available);
      setVehicles(available);
      setVehicleId(available[0]?.id ?? 0);
      setVehicleSource("available vehicles from API");
    }).catch(() => {
      if (active) setVehicleSource("synthetic seed list (vehicle API unavailable)");
    });
    return () => { active = false; };
  }, []);
  useEffect(() => {
    let active = true;
    setRoutes(null);
    setRouteMessage("Loading current primary and backup routes…");
    api<RouteResponse>("/api/routes?" + new URLSearchParams({ depot_id: String(depotId), post_id: String(postId) })).then((response) => {
      if (!active) return;
      const primary = response.primary_route || (response.path ? {
        path: response.path, segments: response.segments || [], distance_km: response.distance_km || 0,
        estimated_hours: response.estimated_hours || 0, risk_score: response.risk_score ?? null,
      } : null);
      if (!primary) throw new Error("The route API returned no primary route.");
      setRoutes({ primary, backup: response.backup_route || null });
      setRouteMessage(response.backup_route ? "Routes refreshed from API for the selected depot and post." : "Primary route loaded; no backup route was returned.");
    }).catch((error: unknown) => {
      if (active) setRouteMessage(error instanceof Error ? `Route lookup failed: ${error.message}` : "Route lookup failed.");
    });
    return () => { active = false; };
  }, [depotId, postId]);
  const createPlan = async (event: FormEvent) => {
    event.preventDefault(); setMessage("");
    if (!routes?.primary) { setMessage("A current primary route is required before a dispatch plan can be submitted."); return; }
    if (!vehicleId) { setMessage("Select an available vehicle before submitting this dispatch."); return; }
    if (!explanation.trim()) { setMessage("Add a short explanation for the proposed dispatch."); return; }
    try {
      const payload = {
        post_id: postId,
        depot_id: depotId,
        item_id: itemId,
        quantity,
        vehicle_id: vehicleId,
        primary_route: routes.primary,
        backup_route: routes.backup,
        departure_at: new Date(departureAt).toISOString(),
        explanation: explanation.trim(),
      };
      const created = await api<Plan>("/api/plans", { method: "POST", body: JSON.stringify(payload) });
      if (!created.id || !created.status || !created.created_at || created.vehicle_id !== vehicleId || !created.primary_route) {
        setMessage("The API did not confirm a complete dispatch plan. Nothing was added to the list.");
        return;
      }
      onCreated(created);
      setMessage(`Dispatch plan ${created.id} was confirmed by the API and added to the list.`);
      setShowForm(false);
      setExplanation("");
    } catch (requestError) { setMessage(requestError instanceof Error ? `Plan creation failed: ${requestError.message}` : "Plan creation failed. No plan was added."); }
  };
  return <><PageHeading eyebrow="DISPATCH COORDINATION" title="Plans & approvals" description="Review proposed replenishment dispatches from depots to forward posts." action={<button className="primary-button" onClick={() => setShowForm((shown) => !shown)}>+ New dispatch</button>} />{message && <div className="api-note">{message}</div>}{showForm && <form className="panel dispatch-form" onSubmit={createPlan}><label>POST<select value={postId} onChange={(event) => setPostId(Number(event.target.value))}>{posts.map((post) => <option key={post.id} value={post.id}>{post.name}</option>)}</select></label><label>DEPOT<select value={depotId} onChange={(event) => setDepotId(Number(event.target.value))}>{depots.map((depot) => <option key={depot.id} value={depot.id}>{depot.name}</option>)}</select></label><label>ITEM<select value={itemId} onChange={(event) => setItemId(Number(event.target.value))}>{items.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}</select></label><label>QUANTITY<input min="1" type="number" value={quantity} onChange={(event) => setQuantity(Number(event.target.value))} /></label><label>AVAILABLE VEHICLE<select value={vehicleId} onChange={(event) => setVehicleId(Number(event.target.value))}><option value={0} disabled>Select a vehicle</option>{vehicles.map((vehicle) => <option key={vehicle.id} value={vehicle.id}>{vehicle.name} · {vehicle.vehicle_type} · {vehicle.capacity_kg.toLocaleString()} kg</option>)}</select><small className="field-hint">{vehicleSource}</small></label><label>DEPARTURE TIME<input type="datetime-local" value={departureAt} onChange={(event) => setDepartureAt(event.target.value)} required /></label><label className="dispatch-explanation">DISPATCH EXPLANATION<textarea maxLength={2000} rows={3} value={explanation} onChange={(event) => setExplanation(event.target.value)} placeholder="Explain the supply need and why this dispatch is proposed." required /></label><div className="route-preview"><b>Route choice sent with plan</b><span>{routeMessage}</span>{routes?.primary && <span>Primary · {routes.primary.distance_km.toFixed(1)} km / {routes.primary.estimated_hours.toFixed(1)} hr · {routes.primary.segments.map((segment) => segment.name).join(" → ")}</span>}{routes?.backup && <span>Backup · {routes.backup.distance_km.toFixed(1)} km / {routes.backup.estimated_hours.toFixed(1)} hr · {routes.backup.segments.map((segment) => segment.name).join(" → ")}</span>}</div><button className="primary-button" type="submit" disabled={!routes?.primary || !vehicleId}>Submit dispatch plan</button></form>}{vehicleSource.includes("unavailable") && <div className="api-note">Backend inspection found no vehicle-list endpoint. Vehicle choices are synthetic seed IDs; validate availability with the server before dispatch.</div>}<div className="plan-summary"><div className="panel"><span>AWAITING REVIEW</span><b>{String(pending).padStart(2, "0")}</b><small>Proposed dispatches</small></div><div className="panel"><span>APPROVED PLANS</span><b>{String(plans.filter((plan) => plan.status === "approved").length).padStart(2, "0")}</b><small>Ready for operations</small></div><div className="panel"><span>ITEM CATEGORIES</span><b>05</b><small>Fuel, rations, medical, clothing, parts</small></div></div><div className="plans-toolbar"><div><h2>Dispatch plans</h2><p>Supply movements proposed by logistics planners</p></div><select value={filter} onChange={(event) => setFilter(event.target.value)}><option>All plans</option><option>Awaiting approval</option><option>Approved</option><option>Rejected</option></select></div><div className="plan-list">{filtered.map((plan) => <article className="panel plan-row" key={plan.id}><div className="plan-kind-icon"><Icon name="clipboard" /></div><div className="plan-main"><h3>{findName(items, plan.item_id, "Supply item")} · {plan.quantity} {items.find((item) => item.id === plan.item_id)?.unit || "units"}</h3><p><Icon name="pin" size={13} />{findName(depots, plan.depot_id, "Depot")} → {findName(posts, plan.post_id, "Forward post")}</p><div className="plan-time">Proposed {new Date(plan.created_at).toLocaleString()}</div>{plan.vehicle_name && <div className="plan-time">Vehicle {plan.vehicle_name} · departs {plan.departure_at ? new Date(plan.departure_at).toLocaleString() : "time not set"}</div>}{plan.explanation && <p className="plan-explanation">{plan.explanation}</p>}{plan.primary_route && <div className="plan-time">Primary {plan.primary_route.distance_km.toFixed(1)} km / {plan.primary_route.estimated_hours.toFixed(1)} hr · Backup {plan.backup_route ? `${plan.backup_route.distance_km.toFixed(1)} km / ${plan.backup_route.estimated_hours.toFixed(1)} hr` : "not available"}</div>}{plan.risk && <><div className="plan-time">Supply risk {plan.risk.score.toFixed(1)}/100 · {plan.risk.level}{plan.risk.coverage_days != null ? ` · ${plan.risk.coverage_days} days coverage` : ""}{plan.risk.risk_days != null ? ` · ${plan.risk.risk_days} risk days` : ""}{plan.requires_commander_approval ? " · commander review required" : ""}</div>{plan.risk.explanation && <p className="plan-explanation">{plan.risk.explanation}</p>}</>}</div><span className={`plan-status ${statusClass(plan.status)}`}><i />{plan.status}</span>{plan.status === "proposed" && (role === "commander" || role === "admin") && <div className="plan-actions"><button className="reject-button" onClick={() => onDecision(plan, "rejected")}>Decline</button><button className="approve-button" onClick={() => onDecision(plan, "approved")}><Icon name="check" size={14} /> Approve</button></div>}</article>)}</div><div className="approval-note"><Icon name="cloud" size={16} /><span>Approvals made offline are stored on this device and synced when connected.</span></div></>;
}

function AuditPage() {
  const [entries, setEntries] = useState<{ id: number; action: string; username: string | null; details: string; created_at: string }[]>([]);
  const [retraining, setRetraining] = useState(false);
  const [message, setMessage] = useState("");
  useEffect(() => { withFallback(api<typeof entries>("/api/audit"), []).then((data) => setEntries(data || [])); }, []);
  const retrain = async () => {
    setRetraining(true); setMessage("");
    try { await api("/api/ml/retrain", { method: "POST" }); setMessage("Demand model retraining request submitted."); }
    catch { setMessage("Model retraining API is unavailable. No training job was started."); }
    finally { setRetraining(false); }
  };
  return <><PageHeading eyebrow="ADMINISTRATION" title="Audit log" description="Review authenticated user actions and model activity." action={<div className="audit-actions"><button className="outline-button" onClick={() => window.print()}>Export log ↓</button><button className="primary-button" onClick={retrain} disabled={retraining}>{retraining ? "Submitting…" : "Retrain model"}</button></div>} />{message && <div className="api-note">{message}</div>}<div className="audit-summary"><div className="panel"><span>RECORDED EVENTS</span><b>{entries.length}</b></div><div className="panel"><span>AUTHENTICATED ACTORS</span><b>{new Set(entries.map((entry) => entry.username).filter(Boolean)).size}</b></div><div className="panel"><span>DATA SOURCE</span><b className="audit-date">Operations API</b></div></div><section className="panel audit-panel"><div className="panel-header"><div><h2>Recent activity</h2><p>Authenticated actions recorded by the server</p></div><span className="audit-live"><i className="live-dot" /> Read-only</span></div><div className="table-scroll"><table><thead><tr><th>EVENT ID</th><th>ACTION</th><th>USER</th><th>DETAILS</th><th>TIME</th></tr></thead><tbody>{entries.map((entry) => <tr key={entry.id}><td className="audit-id">{entry.id}</td><td>{entry.action}</td><td>{entry.username || "System"}</td><td>{entry.details}</td><td>{new Date(entry.created_at).toLocaleString()}</td></tr>)}</tbody></table></div></section><div className="methodology-strip"><div className="method-icon"><Icon name="shield" size={18} /></div><div><b>Audit integrity</b><p>Activity entries are retrieved from the authenticated operations API and cannot be edited here.</p></div></div></>;
}
