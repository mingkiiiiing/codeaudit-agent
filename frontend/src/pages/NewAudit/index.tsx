import { useState } from "react";
import { useNavigate } from "react-router-dom";
import {
  Button,
  Card,
  Flex,
  Input,
  message,
  Radio,
  Space,
  Switch,
  Typography,
  Upload,
} from "antd";
import { InboxOutlined } from "@ant-design/icons";

import { ApiError, createAudit, uploadAuditZip } from "../../api/client";
import { fmtFileSize } from "../../utils/format";

type SourceKind = "path" | "zip";

/** 新建审计：本地路径 / 上传 zip 两种来源 + do_fix / do_tests 开关。 */
export default function NewAudit() {
  const navigate = useNavigate();
  const [messageApi, contextHolder] = message.useMessage();

  const [source, setSource] = useState<SourceKind>("path");
  const [path, setPath] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [doFix, setDoFix] = useState(false);
  const [doTests, setDoTests] = useState(false);
  const [submitting, setSubmitting] = useState(false);

  const ready = source === "path" ? path.trim().length > 0 : file !== null;

  const handleSubmit = async () => {
    if (!ready || submitting) return;
    setSubmitting(true);
    try {
      const result =
        source === "path"
          ? await createAudit({ source_path: path.trim(), do_fix: doFix, do_tests: doTests })
          : await uploadAuditZip(file as File, { do_fix: doFix, do_tests: doTests });
      messageApi.success("任务已创建，正在跳转 …");
      navigate(`/audits/${result.audit_id}`);
    } catch (err) {
      messageApi.error(err instanceof ApiError ? err.detail : `创建任务失败：${String(err)}`);
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Flex vertical gap={16}>
      {contextHolder}
      <Card title="新建审计任务">
        <Space direction="vertical" size="large" style={{ width: "100%" }}>
          <div>
            <Typography.Title level={5} style={{ marginTop: 0 }}>
              任务来源
            </Typography.Title>
            <Radio.Group
              value={source}
              onChange={(e) => setSource(e.target.value as SourceKind)}
              optionType="button"
              options={[
                { value: "path", label: "服务端本地路径" },
                { value: "zip", label: "上传 zip 包" },
              ]}
            />
          </div>

          {source === "path" ? (
            <div>
              <Typography.Paragraph type="secondary" style={{ marginBottom: 8 }}>
                待审计项目需位于服务端本地磁盘，路径不存在时服务端将拒绝创建。
              </Typography.Paragraph>
              <Input
                placeholder="例如：D:\demo\my-project"
                value={path}
                onChange={(e) => setPath(e.target.value)}
                allowClear
              />
            </div>
          ) : (
            <div>
              <Upload.Dragger
                accept=".zip"
                maxCount={1}
                beforeUpload={(f) => {
                  setFile(f);
                  return false; // 不自动上传，提交时随 FormData 发送
                }}
                onRemove={() => setFile(null)}
              >
                <p className="ant-upload-drag-icon">
                  <InboxOutlined />
                </p>
                <p className="ant-upload-text">点击或拖拽 zip 压缩包到此处</p>
                <p className="ant-upload-hint">仅支持 .zip，单文件最大 200MB</p>
              </Upload.Dragger>
              {file && (
                <Typography.Text type="secondary">
                  已选择：{file.name}（{fmtFileSize(file.size)}）
                </Typography.Text>
              )}
            </div>
          )}

          <Space size={48} wrap>
            <div>
              <Space size={8}>
                <Switch checked={doFix} onChange={setDoFix} />
                <Typography.Text strong>生成修复补丁（--fix）</Typography.Text>
              </Space>
              <Typography.Paragraph type="secondary" style={{ marginBottom: 0 }}>
                对检测出的可修复问题自动生成 diff 修复补丁
              </Typography.Paragraph>
            </div>
            <div>
              <Space size={8}>
                <Switch checked={doTests} onChange={setDoTests} />
                <Typography.Text strong>生成单元测试（--tests）</Typography.Text>
              </Space>
              <Typography.Paragraph type="secondary" style={{ marginBottom: 0 }}>
                为关键函数生成单元测试并运行验证
              </Typography.Paragraph>
            </div>
          </Space>

          <Button type="primary" disabled={!ready || submitting} onClick={() => void handleSubmit()}>
            开始审计
          </Button>
        </Space>
      </Card>
    </Flex>
  );
}
