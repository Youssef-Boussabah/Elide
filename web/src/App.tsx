import { useEffect } from "react";
import { AuthProvider, useAuth } from "./auth/AuthContext";
import { Loading } from "./components/ui";
import { Login } from "./pages/Login";
import { href, useRoute } from "./workspace/route";
import { Shell } from "./workspace/Shell";

function Redirect({ to }: { to: string }) {
  useEffect(() => {
    window.location.replace(to);
  }, [to]);
  return null;
}

function Root() {
  const { state } = useAuth();
  const route = useRoute();
  if (state.status === "loading") {
    return (
      <main className="p-8">
        <Loading label="Checking your session…" />
      </main>
    );
  }
  if (state.status === "signed-out") return <Login notice={state.notice} />;
  const ms = state.session.memberships;
  if (ms.length === 0) {
    return (
      <main className="p-8">
        <p>Your account has no active merchant membership. Ask a merchant owner for access.</p>
      </main>
    );
  }
  const current = ms.find((m) => m.merchant_id === route.merchant);
  if (!current) {
    const first = ms[0];
    if (ms.length === 1 && first) return <Redirect to={href(first.merchant_id, route.section)} />;
    return (
      <main className="mx-auto max-w-lg p-8">
        <h1 className="text-xl font-semibold">Choose a merchant</h1>
        <ul className="mt-4 space-y-2">
          {ms.map((m) => (
            <li key={m.merchant_id}>
              <a className="btn-secondary w-full justify-between" href={href(m.merchant_id, "overview")}>
                <span>{m.display_name}</span>
                <span className="text-xs text-slate-500">{m.role}</span>
              </a>
            </li>
          ))}
        </ul>
      </main>
    );
  }
  return <Shell session={state.session} membership={current} section={route.section} />;
}

export default function App() {
  return (
    <AuthProvider>
      <Root />
    </AuthProvider>
  );
}
