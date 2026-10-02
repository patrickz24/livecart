import { Navigate, type RouteObject } from "react-router";

import { RequireAuth } from "../auth/RequireAuth";
import { LoginPage } from "../features/auth/LoginPage";
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
    ],
  },
  { path: "*", element: <NotFoundPage /> },
];
