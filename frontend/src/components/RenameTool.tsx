/**
 * 符号重命名工具面板（W30 卡D）：safe-rename（W28-B POST /api/rename）的前端变现。
 * 交互强制两段式：
 * 1. 「生成预览」→ renameSymbol(apply:false) dry-run，展示替换点数/影响文件数 + 逐文件 unified diff（不落盘）；
 * 2. 预览成功后「确认应用（写入源码）」才可用 → renameSymbol(apply:true)，服务端 all-or-nothing。
 * 错误语义：400/401 展示服务端中文 detail；409 额外提示 all-or-nothing（未写入任何文件）；
 * 任何请求失败仅在本面板内提示，不崩页面。
 * language 不由前端指定（透传缺省，后端取 "python"）。
 */

import { Alert, Button, Card, Flex, Input, Space, Typography } from "antd";
import { useState } from "react";

import { ApiError, renameSymbol } from "../api/client";
import type { RenameResult } from "../api/client";

const MONO_FONT = "Consolas, 'SFMono-Regular', Menlo, monospace";

/** 面板内错误状态：status 用于区分 409 的额外提示（0 = 非 ApiError 的兜底）。 */
interface RenameError {
  status: number;
  detail: string;
}

export function RenameTool() {
  const [sourcePath, setSourcePath] = useState("");
  const [oldName, setOldName] = useState("");
  const [newName, setNewName] = useState("");
  const [previewing, setPreviewing] = useState(false);
  const [applying, setApplying] = useState(false);
  const [preview, setPreview] = useState<RenameResult | null>(null);
  const [applied, setApplied] = useState<RenameResult | null>(null);
  const [error, setError] = useState<RenameError | null>(null);

  const formReady =
    sourcePath.trim() !== "" && oldName.trim() !== "" && newName.trim() !== "";

  /** 表单变更后旧预览/旧结果即失效：一并清空，保证「先预览才能应用」。 */
  const resetResults = () => {
    setPreview(null);
    setApplied(null);
    setError(null);
  };

  const toError = (err: unknown): RenameError =>
    err instanceof ApiError
      ? { status: err.status, detail: err.detail }
      : { status: 0, detail: `重命名请求失败：${String(err)}` };

  const handlePreview = async () => {
    if (!formReady || previewing || applying) return;
    setPreviewing(true);
    resetResults();
    try {
      const result = await renameSymbol({
        source_path: sourcePath.trim(),
        old_name: oldName.trim(),
        new_name: newName.trim(),
        apply: false,
      });
      setPreview(result);
    } catch (err) {
      setError(toError(err));
    } finally {
      setPreviewing(false);
    }
  };

  const handleApply = async () => {
    if (preview === null || applying || previewing) return;
    setApplying(true);
    setError(null);
    try {
      const result = await renameSymbol({
        source_path: sourcePath.trim(),
        old_name: oldName.trim(),
        new_name: newName.trim(),
        apply: true,
      });
      setApplied(result);
    } catch (err) {
      setError(toError(err));
    } finally {
      setApplying(false);
    }
  };

  return (
    <Card title="符号重命名（safe-rename）" style={{ maxWidth: 960 }}>
      <Flex vertical gap={12}>
        <Typography.Paragraph type="secondary" style={{ marginBottom: 0 }}>
          先「生成预览」（dry-run，不落盘）核对 diff，再「确认应用」写入源码；应用为
          all-or-nothing：任一文件复检失败则整体不写入。
        </Typography.Paragraph>

        <Flex vertical gap={4}>
          <Typography.Text strong>源路径</Typography.Text>
          <Input
            placeholder="服务端白名单内的 .py 文件或目录（例如 D:\demo\my-project）"
            value={sourcePath}
            onChange={(e) => {
              setSourcePath(e.target.value);
              resetResults();
            }}
            allowClear
          />
        </Flex>

        <Flex vertical gap={4}>
          <Typography.Text strong>旧名</Typography.Text>
          <Input
            placeholder="现名（合法 Python 标识符）"
            value={oldName}
            onChange={(e) => {
              setOldName(e.target.value);
              resetResults();
            }}
            allowClear
          />
        </Flex>

        <Flex vertical gap={4}>
          <Typography.Text strong>新名</Typography.Text>
          <Input
            placeholder="新名（合法 Python 标识符，且不同于旧名）"
            value={newName}
            onChange={(e) => {
              setNewName(e.target.value);
              resetResults();
            }}
            allowClear
          />
        </Flex>

        <Space size={12} wrap>
          <Button
            type="primary"
            disabled={!formReady || previewing}
            loading={previewing}
            onClick={() => void handlePreview()}
          >
            生成预览
          </Button>
          <Button
            danger
            type="primary"
            disabled={preview === null || applying}
            loading={applying}
            onClick={() => void handleApply()}
          >
            确认应用（写入源码）
          </Button>
        </Space>

        {error && (
          <Alert
            type="error"
            showIcon
            message="重命名请求失败"
            description={
              <Flex vertical gap={4}>
                <span>{error.detail}</span>
                {error.status === 409 && (
                  <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                    all-or-nothing：未写入任何文件，源码保持原样，可重新生成预览后再试。
                  </Typography.Text>
                )}
              </Flex>
            }
          />
        )}

        {applied && (
          <Alert
            type="success"
            showIcon
            message={`已写入 ${applied.files.length} 文件`}
            description={
              <span>重命名已落盘（applied=true）：{applied.files.join("、")}</span>
            }
          />
        )}

        {preview && (
          <Flex vertical gap={8} data-testid="rename-preview">
            <Space size={16} wrap>
              <Typography.Text strong>
                替换点 {preview.replace_points} 处
              </Typography.Text>
              <Typography.Text strong>
                影响文件 {preview.files.length} 个
              </Typography.Text>
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                以下为 dry-run 预览，尚未写入源码
              </Typography.Text>
            </Space>
            {Object.entries(preview.diffs ?? {}).map(([file, diff]) => (
              <div key={file}>
                <Typography.Text code style={{ fontFamily: MONO_FONT, fontSize: 12.5 }}>
                  {file}
                </Typography.Text>
                <pre
                  style={{
                    margin: "4px 0 0",
                    padding: 12,
                    background: "#f8fafc",
                    border: "1px solid #e2e8f0",
                    borderRadius: 6,
                    fontFamily: MONO_FONT,
                    fontSize: 12,
                    lineHeight: 1.6,
                    overflowX: "auto",
                    whiteSpace: "pre-wrap",
                    wordBreak: "break-all",
                  }}
                >
                  {diff}
                </pre>
              </div>
            ))}
          </Flex>
        )}
      </Flex>
    </Card>
  );
}

export default RenameTool;
