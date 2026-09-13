import ReactDOM from "react-dom/client";
import { ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import dayjs from "dayjs";
import "dayjs/locale/zh-cn";
import { BrowserRouter } from "react-router-dom";

import App from "./App";
import "./index.css";

dayjs.locale("zh-cn");

// 品牌主色与 web/index.html 演示页一致（--primary: #0f766e）
const THEME = {
  token: {
    colorPrimary: "#0f766e",
  },
};

ReactDOM.createRoot(document.getElementById("root") as HTMLElement).render(
  <ConfigProvider locale={zhCN} theme={THEME}>
    <BrowserRouter>
      <App />
    </BrowserRouter>
  </ConfigProvider>,
);
