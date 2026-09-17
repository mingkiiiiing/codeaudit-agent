"""configsecret 单测：.env / yaml 嵌套 / 占位豁免 / pyproject 跳过 / 无 key 文件零命中。"""

from __future__ import annotations

from audit.depcheck import configsecret
from audit.models import Category, IssueSource, Severity


# ---------------------------------------------------------------- 单文本扫描


class TestScanConfigText:
    def test_env_hit(self):
        text = "AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMIK7MDENGbPxRfiCYEXAMPLEKEY9a\nDB_PASSWORD=hunter2prod-secret\n"
        findings = configsecret.scan_config_text(text)
        assert len(findings) == 2
        assert findings[0][0] == 1 and findings[0][1] == "AWS_SECRET_ACCESS_KEY"
        assert findings[1][0] == 2 and findings[1][1] == "DB_PASSWORD"
        assert "hunter2prod-secret" not in findings[1][2]
        assert "********" in findings[1][2]

    def test_yaml_nested_hit(self):
        text = (
            "# 生产部署配置\n"
            "database:\n"
            "  host: db-prod.internal\n"
            "  password: hunter2prod-secret\n"
            "api:\n"
            "  secret_key: 9f8e7d6c5b4a3f2e1d0c9b8a7f6e5d4c\n"
        )
        findings = configsecret.scan_config_text(text)
        assert [(line, key) for line, key, _ in findings] == [(4, "password"), (6, "secret_key")]
        # 打码后保留 key 上下文
        assert findings[0][2] == "  password: ********"
        assert "9f8e7d6c" not in findings[1][2]

    def test_properties_and_ini_hit(self):
        text = "db.password=hunter2\n[credentials]\napi_key = abc123XYZ\n"
        findings = configsecret.scan_config_text(text)
        assert [(line, key) for line, key, _ in findings] == [(1, "db.password"), (3, "api_key")]

    def test_quoted_value_unmasked_context(self):
        findings = configsecret.scan_config_text('token: "abc123def"\n')
        assert findings[0][2] == 'token: "********"'


class TestPlaceholderExemption:
    def test_placeholder_values_exempt(self):
        text = "\n".join(
            [
                "PASSWORD=<your-password-here>",
                "SECRET=changeme",
                "API_KEY=xxx",
                "TOKEN=placeholder",
                "ACCESS_KEY=example",
                "PRIVATE_KEY=sample",
                "PASSWORD=test123",
                "SECRET=${DB_SECRET}",
                "TOKEN={{ vault_token }}",
                "PASSWORD=",
                "TOKEN=false",
            ]
        )
        assert configsecret.scan_config_text(text) == []

    def test_case_insensitive_prefixes(self):
        assert configsecret.is_placeholder("CHANGEME") is True
        assert configsecret.is_placeholder("<YOUR_TOKEN>") is True

    def test_real_values_not_placeholder(self):
        # 语料回归：值中段含 "example"（AWS 样例 key 形态）不算占位
        assert configsecret.is_placeholder("wJalrXUtnFEMIK7MDENGbPxRfiCYEXAMPLEKEY9a") is False
        assert configsecret.is_placeholder("hunter2prod-secret") is False


class TestKeyMatching:
    def test_sensitive_key_words(self):
        for key in ("password", "passwd", "PWD_SECRET", "token", "api_key", "apiKey",
                    "access_key", "private_key", "SECRET_KEY", "PASSWORD"):
            assert configsecret.SECRET_KEY_RE.search(key), key

    def test_non_sensitive_keys_zero_hit(self):
        # "db.pass" 不在契约正则（passw(or)?d）内，属非敏感 key（契约口径如此）
        text = "db.pass: short\nhost: db-prod.internal\nport: 5432\nusername: admin\nregion: us-east-1\n"
        assert configsecret.scan_config_text(text) == []

    def test_section_headers_ignored(self):
        assert configsecret.scan_config_text("[database]\n") == []


# ---------------------------------------------------------------- 文件级


class TestScanFiles:
    def test_env_file_hit(self, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text("DB_PASSWORD=hunter2prod-secret\n", encoding="utf-8")
        issues = configsecret.scan_config_file(env_file, rel_path=".env")
        assert len(issues) == 1
        issue = issues[0]
        assert issue.category == Category.SECURITY
        assert issue.severity == Severity.CRITICAL
        assert issue.line_start == 1 and issue.line_end == 1
        assert issue.confidence == 0.7
        assert issue.source == IssueSource.RULE
        assert any(e.startswith("rule:CFG-SECRET") for e in issue.evidence)
        assert "hunter2prod-secret" not in issue.code_snippet
        assert "hunter2prod-secret" not in issue.description  # 描述也不得泄露明文

    def test_env_variant_files(self, tmp_path):
        for name in (".env.local", ".env.production"):
            (tmp_path / name).write_text("SECRET=real-secret-value\n", encoding="utf-8")
        issues = configsecret.scan_source_root(tmp_path)
        assert {i.file for i in issues} == {".env.local", ".env.production"}

    def test_pyproject_skipped(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nauthors = [{email = "admin-password@example.com"}]\npassword = "x"\n',
            encoding="utf-8",
        )
        assert configsecret.scan_source_root(tmp_path) == []

    def test_toml_yaml_ini_properties_scanned(self, tmp_path):
        (tmp_path / "settings.toml").write_text('password = "hunter2"\n', encoding="utf-8")
        (tmp_path / "deploy.yaml").write_text("secret_key: abc123\n", encoding="utf-8")
        (tmp_path / "app.ini").write_text("token = abc123def\n", encoding="utf-8")
        (tmp_path / "jdbc.properties").write_text("db.password=p4ssw0rd\n", encoding="utf-8")
        issues = configsecret.scan_source_root(tmp_path)
        assert {i.file for i in issues} == {"settings.toml", "deploy.yaml", "app.ini", "jdbc.properties"}

    def test_no_key_file_zero_findings(self, tmp_path):
        (tmp_path / "config.yaml").write_text(
            "server:\n  host: 0.0.0.0\n  port: 8080\nlogging:\n  level: info\n",
            encoding="utf-8",
        )
        assert configsecret.scan_source_root(tmp_path) == []

    def test_node_modules_ignored(self, tmp_path):
        nested = tmp_path / "node_modules" / "pkg"
        nested.mkdir(parents=True)
        (nested / ".env").write_text("SECRET=leaked-value\n", encoding="utf-8")
        assert configsecret.scan_source_root(tmp_path) == []

    def test_unreadable_file_returns_empty(self, tmp_path):
        missing = tmp_path / "no-such.yaml"
        assert configsecret.scan_config_file(missing) == []
