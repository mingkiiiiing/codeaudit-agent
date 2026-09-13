import { Layout, Menu } from "antd";
import { Outlet, useLocation, useNavigate } from "react-router-dom";

const { Header, Content } = Layout;

const MENU_ITEMS = [
  { key: "dashboard", label: "仪表盘" },
  { key: "new", label: "新建审计" },
];

const GITHUB_URL = "https://github.com/mingkiiiiing/codeaudit-agent";

/** 全站布局：顶栏（品牌 + 主导航 + GitHub 外链）+ 居中内容区（max-width 1280）。 */
export default function BasicLayout() {
  const navigate = useNavigate();
  const { pathname } = useLocation();
  const selectedKey = pathname === "/new" ? "new" : pathname === "/" ? "dashboard" : "";

  return (
    <Layout style={{ minHeight: "100vh" }}>
      <Header
        style={{
          display: "flex",
          alignItems: "center",
          gap: 24,
          background: "#fff",
          borderBottom: "1px solid #e2e8f0",
          padding: "0 24px",
        }}
      >
        <div
          style={{
            color: "#0f766e",
            fontSize: 17,
            fontWeight: 700,
            whiteSpace: "nowrap",
            letterSpacing: 0.2,
          }}
        >
          CodeAudit 审计工作台
        </div>
        <Menu
          mode="horizontal"
          selectedKeys={selectedKey ? [selectedKey] : []}
          items={MENU_ITEMS}
          onClick={({ key }) => navigate(key === "new" ? "/new" : "/")}
          style={{ flex: 1, minWidth: 0, borderBottom: "none" }}
        />
        <a
          href={GITHUB_URL}
          target="_blank"
          rel="noreferrer"
          style={{ color: "#64748b", whiteSpace: "nowrap" }}
        >
          GitHub 仓库
        </a>
      </Header>
      <Content style={{ padding: "24px 24px 48px" }}>
        <div style={{ maxWidth: 1280, margin: "0 auto" }}>
          <Outlet />
        </div>
      </Content>
    </Layout>
  );
}
