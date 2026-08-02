import { createRoot } from "react-dom/client";
import App from "./App";
import "./styles.css";
import "./workflow-refresh.css";

const rootElement = document.getElementById("root");
if (!rootElement) throw new Error("Missing #root application mount point.");

createRoot(rootElement).render(<App />);
