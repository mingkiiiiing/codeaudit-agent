"""mkdocs-gen-files 构建期脚本：把 docs/ 下的设计文档镜像进站点 design/ 命名空间。

为什么需要镜像：mkdocs 只允许 nav 引用 docs_dir 内的文件；本项目站点自有页面放在
docs-site/（docs_dir），而设计文档在仓库根 docs/。为避免内容复制出第二份造成漂移，
构建时把 docs/*.md 逐个生成到虚拟路径 design/<同名文件>：

- 源文件全部为 docs/ 平级兄弟，镜像后仍为 design/ 平级兄弟，文档互链相对关系不变；
- 不落盘，只存在于构建过程，仓库内不产生第二份 markdown；
- mkdocs build --strict 下镜像文件同样参与失效链接检查。
"""

from pathlib import Path

import mkdocs_gen_files

DOCS_SRC = Path("docs")       # 相对 mkdocs 工作目录（项目根）
DESIGN_PREFIX = "design"      # 站点内命名空间，与 mkdocs.yml 的 nav 对应

for src in sorted(DOCS_SRC.glob("*.md")):
    target = f"{DESIGN_PREFIX}/{src.name}"
    with mkdocs_gen_files.open(target, "w", encoding="utf-8") as f:
        f.write(src.read_text(encoding="utf-8"))
    mkdocs_gen_files.set_edit_path(target, src)
