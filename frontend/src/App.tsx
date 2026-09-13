import { Result } from "antd";
import { Route, Routes } from "react-router-dom";

import BasicLayout from "./layouts/BasicLayout";
import Dashboard from "./pages/Dashboard";
import NewAudit from "./pages/NewAudit";
import TaskDetail from "./pages/TaskDetail";

/** 路由：/ 仪表盘 · /new 新建审计 · /audits/:id 任务详情 · * 404。 */
export default function App() {
  return (
    <Routes>
      <Route element={<BasicLayout />}>
        <Route path="/" element={<Dashboard />} />
        <Route path="/new" element={<NewAudit />} />
        <Route path="/audits/:id" element={<TaskDetail />} />
        <Route
          path="*"
          element={<Result status="404" title="404" subTitle="页面不存在" />}
        />
      </Route>
    </Routes>
  );
}
