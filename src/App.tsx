import { Link, Route, Routes } from "react-router-dom";
import { ToastProvider } from "./components/Toast";
import LibraryPage from "./pages/Library";
import SettingsPage from "./pages/Settings";
import WorkbenchPage from "./pages/Workbench";

export default function App() {
  return (
    <ToastProvider>
      <Routes>
        <Route path="/" element={<LibraryPage />} />
        <Route path="/workbench/:documentId" element={<WorkbenchPage />} />
        <Route path="/settings" element={<SettingsPage />} />
        <Route
          path="*"
          element={
            <div className="flex h-screen items-center justify-center">
              <Link to="/" className="text-crimson-700">返回图书馆</Link>
            </div>
          }
        />
      </Routes>
    </ToastProvider>
  );
}
