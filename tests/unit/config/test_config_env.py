"""W12-A1（F7）新增用例：from_env 的 .env 自动加载语义（docs/17 §2）。

覆盖面：
- CWD/.env 自动发现生效；显式 env_file 参数生效；
- 真实进程环境变量永远优先于 .env（缺省只补环境缺失键）；
- GLM_* 三键只合并进本次取值、不落 os.environ（from_sources 口径不变）；
  CODEAUDIT_* 三键 setdefault 注入 os.environ（server 侧直接读环境）；
- 白名单外键不注入 os.environ；
- 解析格式变体：export 前缀 / 首尾引号剥离 / # 注释 / 空行 / 坏行静默跳过；
- 文件缺失静默跳过（自动发现与显式路径两口径）；
- _ENV_LOADED 标志：同进程至多实际加载一次（含显式 env_file 亦被跳过）。

隔离约定（防 os.environ 污染）：本目录 autouse fixture 每用例 delenv 全部白名单键
（monkeypatch 在用例结束后恢复用例前的原值，用例内被 setdefault 注入的键同样被
该机制清掉）并复位 audit.config._ENV_LOADED；CWD 一律 monkeypatch.chdir 钉在
tmp_path——绝不读到仓库根的真实 .env，全程零网络。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from audit import config as audit_config
from audit.config import DEFAULT_BASE_URL, DEFAULT_MODEL, AuditConfig

# 与 audit/config.py 的 _DOTENV_KEYS 白名单一致（断言用；白名单变化时此处同步）
_WHITELIST = (
    "GLM_API_KEY",
    "GLM_BASE_URL",
    "GLM_MODEL",
    "CODEAUDIT_MAX_RUNNING",
    "CODEAUDIT_MAX_PENDING",
    "CODEAUDIT_DB_PATH",
)


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch):
    """每用例：清空白名单键 + 复位 _ENV_LOADED 标志与 _DOTENV_CACHE 缓存
    （F7-R1 起两者同源，须一并复位），用例间/与既有用例零串扰。

    monkeypatch.delenv 会记录用例前的值并在 teardown 恢复——用例内被
    _load_dotenv 经 setdefault 写入 os.environ 的键不会泄漏出本目录。
    """
    for key in _WHITELIST:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(audit_config, "_ENV_LOADED", False)
    monkeypatch.setattr(audit_config, "_DOTENV_CACHE", None)


# ---------------------------------------------------------------- 自动发现与显式路径


def test_dotenv_autodiscovered_from_cwd(monkeypatch, tmp_path):
    """CWD/.env 自动发现：from_env 取到 .env 值。"""
    (tmp_path / ".env").write_text(
        "GLM_API_KEY=sk-from-dotenv\nGLM_MODEL=glm-dotenv-model\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    cfg = AuditConfig.from_env(source_path="C:/tmp/proj")
    assert cfg.api_key == "sk-from-dotenv"
    assert cfg.model == "glm-dotenv-model"
    assert cfg.llm_available


def test_glm_keys_not_injected_into_os_environ(monkeypatch, tmp_path):
    """GLM_* 三键只合并进本次 from_env 取值，不落 os.environ（不污染全局环境，
    from_sources 等其他环境消费者口径不变——否则存量 from_sources 用例会被
    开发机根目录真实 .env 打破）。"""
    (tmp_path / ".env").write_text(
        "GLM_API_KEY=sk-from-dotenv\nGLM_BASE_URL=https://dotenv.example/v1\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    assert AuditConfig.from_env().llm_available
    for key in ("GLM_API_KEY", "GLM_BASE_URL"):
        assert key not in os.environ
    # from_sources（CLI 口径）不读 os.environ——F7-R1（W27 卡 A）起它经
    # _DOTENV_CACHE 缓存取到 .env 值（dotenv 回落是 from_sources 自身的合法
    # 语义层，非 os.environ 污染；GLM_* 键全程未落 os.environ，上方已断言）
    cfg = AuditConfig.from_sources(search_root=tmp_path)
    assert cfg.api_key == "sk-from-dotenv"
    assert cfg.base_url == "https://dotenv.example/v1"


def test_explicit_env_file(monkeypatch, tmp_path):
    """显式 env_file 参数优先于自动发现：路径任意，不必叫 .env / 不必在 CWD。"""
    env_file = tmp_path / "custom.env"
    env_file.write_text("GLM_API_KEY=sk-explicit\n", encoding="utf-8")
    cwd_without_env = tmp_path / "cwd-without-env"
    cwd_without_env.mkdir()
    monkeypatch.chdir(cwd_without_env)  # CWD 无 .env，仅显式路径生效
    cfg = AuditConfig.from_env(source_path="x", env_file=str(env_file))
    assert cfg.api_key == "sk-explicit"


# ---------------------------------------------------------------- 优先级


def test_real_env_overrides_dotenv(monkeypatch, tmp_path):
    """关键语义：真实进程环境变量永远优先——.env 不得覆盖已存在的键。"""
    (tmp_path / ".env").write_text(
        "GLM_API_KEY=sk-from-dotenv\nGLM_MODEL=glm-dotenv\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GLM_API_KEY", "sk-from-shell")
    monkeypatch.setenv("GLM_MODEL", "glm-shell")
    cfg = AuditConfig.from_env()
    assert cfg.api_key == "sk-from-shell"
    assert cfg.model == "glm-shell"
    # os.environ 中的真实值未被 .env 改写
    assert os.environ["GLM_API_KEY"] == "sk-from-shell"
    assert os.environ["GLM_MODEL"] == "glm-shell"


def test_explicit_overrides_still_win(monkeypatch, tmp_path):
    """既有语义保持：显式 overrides（如 CLI/api 显式传参）仍高于一切。"""
    (tmp_path / ".env").write_text("GLM_MODEL=glm-dotenv\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    cfg = AuditConfig.from_env(source_path="x", model="glm-explicit")
    assert cfg.model == "glm-explicit"


# ---------------------------------------------------------------- 白名单


def test_non_whitelisted_keys_ignored(monkeypatch, tmp_path):
    """白名单外键一律忽略：不注入 os.environ（防 .env 注入无关变量）。"""
    (tmp_path / ".env").write_text(
        "GLM_MODEL=whitelisted-model\n"
        "NOT_ALLOWED=1\n"
        "SOME_OTHER_SECRET=boom\n"
        "GLM_API_KEY_EXTENDED=lookalike\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    cfg = AuditConfig.from_env()
    # 白名单内键生效（合并进本次取值）
    assert cfg.model == "whitelisted-model"
    # 白名单外键（含形似白名单的键）不注入 os.environ
    for key in ("NOT_ALLOWED", "SOME_OTHER_SECRET", "GLM_API_KEY_EXTENDED"):
        assert key not in os.environ


def test_codeaudit_keys_loaded_for_server_side(monkeypatch, tmp_path):
    """CODEAUDIT_* 白名单键注入 os.environ（server 侧运行期直接读）。"""
    (tmp_path / ".env").write_text(
        "CODEAUDIT_DB_PATH=D:/tmp/x.db\nCODEAUDIT_MAX_RUNNING=7\nCODEAUDIT_MAX_PENDING=9\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    AuditConfig.from_env()
    assert os.environ["CODEAUDIT_DB_PATH"] == "D:/tmp/x.db"
    assert os.environ["CODEAUDIT_MAX_RUNNING"] == "7"
    assert os.environ["CODEAUDIT_MAX_PENDING"] == "9"


# ---------------------------------------------------------------- 解析格式变体


def test_format_variants(monkeypatch, tmp_path):
    """解析变体：export 前缀（空格/Tab）、单双引号剥离、注释、空行、坏行静默跳过。"""
    (tmp_path / ".env").write_text(
        "# 整行注释应被跳过\n"
        "\n"
        "   \n"
        'GLM_MODEL="glm-dq"\n'
        "export GLM_BASE_URL='https://example.test/v1'\n"
        "export\tGLM_API_KEY=sk-exported\n"
        "CODEAUDIT_DB_PATH=D:/a=b/c.db\n"
        "  INDENTED_KEY = padded value  \n"
        "this line has no equals sign\n"
        "=empty-key-line\n"
        "#GLM_API_KEY=commented-out\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    cfg = AuditConfig.from_env()
    assert cfg.model == "glm-dq"
    assert cfg.base_url == "https://example.test/v1"
    assert cfg.api_key == "sk-exported"
    # 值内含 '='：按第一个 '=' 切分，其余属于值
    assert os.environ["CODEAUDIT_DB_PATH"] == "D:/a=b/c.db"
    # 非白名单键（含带缩进的合法格式行）不注入
    assert "INDENTED_KEY" not in os.environ


def test_parse_dotenv_line_edges():
    """解析器单测：各边界输入的 (key, value) / None 结果。"""
    parse = audit_config._parse_dotenv_line
    assert parse("KEY=value") == ("KEY", "value")
    assert parse("  KEY = value  ") == ("KEY", "value")
    assert parse('KEY="quoted"') == ("KEY", "quoted")
    assert parse("KEY='quoted'") == ("KEY", "quoted")
    assert parse("export KEY=v") == ("KEY", "v")
    assert parse("KEY=") == ("KEY", "")  # 空值合法（GLM_API_KEY 空 = 离线）
    assert parse("KEY=a=b") == ("KEY", "a=b")  # 值内 '=' 保留
    assert parse("") is None
    assert parse("   ") is None
    assert parse("# comment") is None
    assert parse("no-equals-line") is None
    assert parse("=no-key") is None
    assert parse("exportKEY=v") == ("exportKEY", "v")  # 无分隔不算 export 前缀


# ---------------------------------------------------------------- 文件缺失静默


def test_missing_dotenv_silent(monkeypatch, tmp_path):
    """CWD 无 .env：静默跳过，回落默认值，无异常无副作用。"""
    monkeypatch.chdir(tmp_path)  # tmp_path 下无 .env
    cfg = AuditConfig.from_env(source_path="x")
    assert cfg.api_key == ""
    assert not cfg.llm_available
    assert cfg.base_url == DEFAULT_BASE_URL
    assert cfg.model == DEFAULT_MODEL


def test_missing_explicit_env_file_silent(tmp_path):
    """显式 env_file 指向不存在的文件：同样静默跳过。"""
    cfg = AuditConfig.from_env(source_path="x", env_file=str(tmp_path / "nope.env"))
    assert not cfg.llm_available


# ---------------------------------------------------------------- 只加载一次


def test_env_loaded_only_once(monkeypatch, tmp_path):
    """_ENV_LOADED 标志：改写 .env 后不再重读（防覆盖用户运行中改的 env）。

    F7-R1（W27 卡 A）更新：不再重读的后续调用返回首次解析缓存（_DOTENV_CACHE），
    而非空 dict——取值口径与首次一致，且改写后的文件内容不会出现。
    """
    env = tmp_path / ".env"
    env.write_text("GLM_MODEL=first-model\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert AuditConfig.from_env().model == "first-model"
    # 文件改写 + 环境键清掉后：既不重读文件（second-model 不出现），也不回落
    # 默认值，而是返回首次加载缓存（修复前此处为 DEFAULT_MODEL——第二次取值
    # 口径与首次不一致，即 resume 重建丢 api_key 的根因）
    env.write_text("GLM_MODEL=second-model\n", encoding="utf-8")
    monkeypatch.delenv("GLM_MODEL", raising=False)
    assert AuditConfig.from_env().model == "first-model"
    assert "GLM_MODEL" not in os.environ
    assert audit_config._ENV_LOADED is True


def test_explicit_env_file_skipped_after_load(monkeypatch, tmp_path):
    """同进程只加载一次对显式 env_file 同样生效：已加载后显式路径不触发重读，
    后续调用返回首次加载缓存（F7-R1 前此处回落 DEFAULT_MODEL；显式文件内容
    other-model 两种实现下都不得出现）。"""
    (tmp_path / ".env").write_text("GLM_MODEL=auto-model\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert AuditConfig.from_env().model == "auto-model"
    other = tmp_path / "other.env"
    other.write_text("GLM_MODEL=other-model\n", encoding="utf-8")
    monkeypatch.delenv("GLM_MODEL", raising=False)
    cfg = AuditConfig.from_env(source_path="x", env_file=str(other))
    assert cfg.model == "auto-model"  # 首次缓存；other-model 未被注入（未重读）


def test_load_dotenv_missing_file_keeps_flag_unset(tmp_path, monkeypatch):
    """缺文件不算"实际加载"：不置 _ENV_LOADED，后续自动发现仍可生效。"""
    monkeypatch.chdir(tmp_path)  # 无 .env
    assert audit_config._load_dotenv(None) == {}
    assert audit_config._ENV_LOADED is False
    assert audit_config._DOTENV_CACHE is None  # F7-R1：失败路径同样不写缓存
    # 缺文件的那次尝试没有消费掉"只加载一次"额度：补上 .env 后 from_env 仍能发现
    (tmp_path / ".env").write_text("GLM_MODEL=later-model\n", encoding="utf-8")
    assert AuditConfig.from_env().model == "later-model"
    assert audit_config._ENV_LOADED is True


# ---------------------------------------------------------------- Path 口径


def test_env_file_accepts_path_object(tmp_path):
    """env_file 接受 Path 对象（签名 str | Path）。"""
    env_file: Path = tmp_path / "p.env"
    env_file.write_text("GLM_API_KEY=sk-path\n", encoding="utf-8")
    cfg = AuditConfig.from_env(source_path="x", env_file=env_file)
    assert cfg.api_key == "sk-path"


# ---------------------------------------------------------------- W16：from_sources 同样加载 .env


def test_from_sources_dotenv_autodiscovered(monkeypatch, tmp_path):
    """W16 修复（README 承诺对齐）：CLI 路径 from_sources 也自动加载 CWD/.env。

    此前仅 from_env 加载——CLI 填了 .env 仍被判"LLM 未配置"，与 README
    「cp .env 后无需 export 直接使用」不一致。
    """
    (tmp_path / ".env").write_text("GLM_API_KEY=sk-from-dotenv-cli\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    cfg = AuditConfig.from_sources(cli_overrides={"source_path": "C:/tmp/proj"})
    assert cfg.api_key == "sk-from-dotenv-cli"
    assert cfg.llm_available


def test_from_sources_real_env_still_beats_dotenv(monkeypatch, tmp_path):
    """真实进程环境变量仍优先于 .env（与 from_env 逐键优先级口径一致）。"""
    (tmp_path / ".env").write_text("GLM_API_KEY=sk-from-dotenv-cli\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GLM_API_KEY", "sk-real-env")
    cfg = AuditConfig.from_sources(cli_overrides={"source_path": "C:/tmp/proj"})
    assert cfg.api_key == "sk-real-env"


def test_from_sources_without_dotenv_unchanged(monkeypatch, tmp_path):
    """无 .env 时 from_sources 行为零变化（api_key 仍为空、llm_available False）。"""
    monkeypatch.chdir(tmp_path)
    cfg = AuditConfig.from_sources(cli_overrides={"source_path": "C:/tmp/proj"})
    assert cfg.api_key == ""
    assert not cfg.llm_available


# ---------------------------------------------------------------- F7-R1：同进程重复调用口径一致（W27 卡 A）


def test_repeated_from_env_same_api_key(monkeypatch, tmp_path):
    """F7-R1 修复主断言：同进程两次 from_env 的 api_key 一致。

    审计原复现（第七轮 P1）：修复前首次拿到 key、第二次为空——cli cmd_resume
    不带 --work-root 时前置 from_env 消耗进程首次加载，_rebuild_config_from_task_json
    的 from_env 拿到空 api_key，pipeline 走 FakeLLM，resume 静默降级纯规则模式。
    修复后第二次返回首次解析缓存（_DOTENV_CACHE 浅拷贝）。
    """
    (tmp_path / ".env").write_text("GLM_API_KEY=sk-f7r1-key\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    first = AuditConfig.from_env(source_path="x")
    second = AuditConfig.from_env(source_path="x")
    assert first.api_key == "sk-f7r1-key"
    assert second.api_key == "sk-f7r1-key"  # 修复前为 ""
    assert second.llm_available
    # 浅拷贝防污染：改写 _load_dotenv 返回值不得影响缓存与后续调用
    poisoned = audit_config._load_dotenv(None)
    poisoned["GLM_API_KEY"] = "polluted"
    assert AuditConfig.from_env().api_key == "sk-f7r1-key"


def test_from_sources_after_from_env_same_key(monkeypatch, tmp_path):
    """F7-R1 双入口回归：先 from_env 后 from_sources 同进程同样拿到 .env 值。

    from_sources 的 dotenv 回落（W16）在 _ENV_LOADED 置位后此前拿到空 dict
    （key 凭空丢失）；修复后经 _DOTENV_CACHE 与 from_env 口径一致。
    """
    (tmp_path / ".env").write_text("GLM_API_KEY=sk-dual-entry\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert AuditConfig.from_env().api_key == "sk-dual-entry"
    cfg = AuditConfig.from_sources(cli_overrides={"source_path": "C:/tmp/proj"})
    assert cfg.api_key == "sk-dual-entry"
    assert cfg.llm_available
