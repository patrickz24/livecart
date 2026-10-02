import { Navigate, type RouteObject } from "react-router";

import { RequireAuth } from "../auth/RequireAuth";
import { LoginPage } from "../features/auth/LoginPage";
import { LiveSessionPage } from "../features/live/LiveSessionPage";
import { OrderDetailPage } from "../features/orders/OrderDetailPage";
import { EditSessionPage, NewSessionPage } from "../features/sessions/SessionFormPage";
import { SessionListPage } from "../features/sessions/SessionListPage";
import { Layout } from "./Layout";
import { NotFoundPage } from "./NotFoundPage";

/** Every route of the dashboard; the browser router in main.tsx, a memory router in tests. */
export const routes: RouteObject[] = [
  { path: "/login", element: <LoginPage /> },
  {
    element: (
      <RequireAuth>
        <Layout />
      </RequireAuth>
    ),
    children: [
      { index: true, element: <Navigate to="/sessions" replace /> },
      { path: "sessions", element: <SessionListPage /> },
      { path: "sessions/new", element: <NewSessionPage /> },
      { path: "sessions/:sessionId/edit", element: <EditSessionPage /> },
      { path: "sessions/:sessionId/live", element: <LiveSessionPage /> },
      { path: "orders/:reference", element: <OrderDetailPage /> },
    ],
  },
  { path: "*", element: <NotFoundPage /> },
];
