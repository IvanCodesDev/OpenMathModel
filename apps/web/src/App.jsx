import {
  ConfirmTaskScreen,
  DataScreen,
  ExperimentsScreen,
  MethodLibraryScreen,
  ModelPlanScreen,
  NewTaskScreen,
  PaperDetailScreen,
  PaperEditorScreen,
  PapersScreen,
  ProblemDetailScreen,
  ProblemLibraryScreen,
  ProjectsScreen,
  TaskCompleteScreen,
  TaskRunningScreen,
} from "./screens.jsx";

const routes = new Map([
  ["/", NewTaskScreen],
  ["/confirm", ConfirmTaskScreen],
  ["/task/running", TaskRunningScreen],
  ["/projects", ProjectsScreen],
  ["/workspace/data", DataScreen],
  ["/workspace/model-plan", ModelPlanScreen],
  ["/workspace/experiments", ExperimentsScreen],
  ["/workspace/paper-editor", PaperEditorScreen],
  ["/library/problems", ProblemLibraryScreen],
  ["/library/papers", PapersScreen],
  ["/library/methods", MethodLibraryScreen],
  ["/task/complete", TaskCompleteScreen],
  ["/library/problems/detail", ProblemDetailScreen],
  ["/library/papers/detail", PaperDetailScreen],
]);

function normalizePath(pathname) {
  if (pathname.length > 1 && pathname.endsWith("/")) return pathname.slice(0, -1);
  return pathname;
}

export default function App() {
  const Screen = routes.get(normalizePath(window.location.pathname)) ?? NewTaskScreen;
  return <Screen />;
}
