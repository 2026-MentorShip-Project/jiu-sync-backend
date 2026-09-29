"""build context 排除 secret(add-pgbouncer-and-celery-worker design.md D6)。

以真正的 `docker build` 驗證 repo 的 `.dockerignore`:在暫存目錄放 repo 的 `.dockerignore`
與代表性檔案(假 `.env` 等,不碰 repo 根目錄真正的 `.env`),用只做 `COPY . /ctx` 的
`FROM scratch` Dockerfile 建置並以 `--output type=local` 匯出,檢查實際進入 build
context 的檔案。不產生 image,不需拉取 base image。
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCKERIGNORE = REPO_ROOT / ".dockerignore"

# 不可進入 build context 的檔案(secret、本機環境、版控資料)
MUST_EXCLUDE = [
    ".env",
    ".env.prod",
    ".env.example",
    "apps/events/.env",
    ".venv/bin/python",
    ".git/config",
    # 本機 terraform state 可能含 secret(已 gitignore,但一般 docker build . 仍會打包)
    "infra/terraform/terraform.tfstate",
    "infra/terraform/terraform.tfstate.backup",
    "infra/terraform/.terraform/providers/placeholder",
    "infra/terraform/secrets.tfvars",
    "infra/terraform/prod.auto.tfvars",
    # 本機開發資料
    "db.sqlite3",
]
# 根目錄 Dockerfile 的 `uv sync` 與執行期需要的檔案
MUST_INCLUDE = [
    "Dockerfile",
    "pyproject.toml",
    "uv.lock",
    ".python-version",
    "manage.py",
    "config/__init__.py",
    "config/settings/prod.py",
    "apps/__init__.py",
    "apps/events/models.py",
]

CONTEXT_PROBE_DOCKERFILE = "FROM scratch\nCOPY . /ctx\n"


@pytest.fixture(scope="module")
def context_files(tmp_path_factory):
    docker = shutil.which("docker")
    if docker is None:
        # CI(ubuntu-latest)一定有 docker;在 CI 缺 docker 代表環境變了,不可默默 skip。
        if os.environ.get("CI"):
            pytest.fail("CI 環境缺少 docker CLI,無法驗證 build context")
        pytest.skip("docker CLI 不存在,無法以 docker build 驗證 build context")
    root = tmp_path_factory.mktemp("build_context")
    ctx = root / "ctx"
    ctx.mkdir()
    # 沒有 .dockerignore 時照樣建置:排除類斷言會失敗,反映真實洩漏行為
    if DOCKERIGNORE.is_file():
        shutil.copy(DOCKERIGNORE, ctx / ".dockerignore")
    for rel in MUST_EXCLUDE + MUST_INCLUDE:
        f = ctx / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("FAKE_SECRET=not-a-real-value\n")
    # probe Dockerfile 放在 context 外,避免它自己影響 context 內容
    probe = root / "probe.Dockerfile"
    probe.write_text(CONTEXT_PROBE_DOCKERFILE)
    out = root / "out"

    result = subprocess.run(
        [docker, "build", "-q", "-f", str(probe), "--output", f"type=local,dest={out}", str(ctx)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    base = out / "ctx"
    return {p.relative_to(base).as_posix() for p in base.rglob("*") if p.is_file()}


@pytest.mark.parametrize("rel", MUST_EXCLUDE)
def test_secret_and_local_files_are_not_in_build_context(context_files, rel):
    assert rel not in context_files


@pytest.mark.parametrize("rel", MUST_INCLUDE)
def test_files_needed_by_dockerfile_stay_in_build_context(context_files, rel):
    assert rel in context_files
