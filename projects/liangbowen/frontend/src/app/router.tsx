import type { InitialEntry } from "react-router";
import { createBrowserRouter, createMemoryRouter } from "react-router";

import { AuthProvider } from "../auth/AuthProvider";
import { LoginPage } from "../auth/LoginPage";
import { RequireRole } from "../auth/RequireRole";
import { TeacherDashboard } from "../assignments/TeacherDashboard";
import { AssignmentDetailPage } from "../assignments/AssignmentDetailPage";
import { ReportReviewPage } from "../reports/ReportReviewPage";
import { StudentAssignmentPage } from "../submissions/StudentAssignmentPage";
import { StudentHomePage } from "../submissions/StudentHomePage";
import { AccountManagementPage } from "../users/AccountManagementPage";
import { App } from "./App";
import { LandingRedirect, NotFoundPage } from "./RouteScreens";

const routes = [
  {
    element: (
      <AuthProvider>
        <App />
      </AuthProvider>
    ),
    children: [
      { path: "/", element: <LandingRedirect /> },
      { path: "/login", element: <LoginPage /> },
      {
        element: <RequireRole role="admin" />,
        children: [{ path: "/admin/users", element: <AccountManagementPage /> }],
      },
      {
        element: <RequireRole role="teacher" />,
        children: [
          { path: "/teacher", element: <TeacherDashboard /> },
          { path: "/teacher/users", element: <AccountManagementPage /> },
          { path: "/teacher/assignments/:id", element: <AssignmentDetailPage /> },
          { path: "/teacher/reports/:id", element: <ReportReviewPage /> },
        ],
      },
      {
        element: <RequireRole role="student" />,
        children: [
          { path: "/student", element: <StudentHomePage /> },
          { path: "/student/assignments/:id", element: <StudentAssignmentPage /> },
        ],
      },
      { path: "*", element: <NotFoundPage /> },
    ],
  },
];

export function createAppRouter(initialEntries?: InitialEntry[]) {
  return initialEntries === undefined
    ? createBrowserRouter(routes)
    : createMemoryRouter(routes, { initialEntries });
}
