"""README 結構測試(rewrite-readme design.md D1、D3、D4)。

只驗結構與連結有效性:檔案存在、D1 段落標題齊全且順序正確、相對連結指向的路徑存在、
Quick Start 引用的檔案存在。文字內容正確性由人工檢視(manual)。

`find_broken_links` 與標題清單設計成可重用,英文版 `README.en.md`(task 2.1)沿用同一套檢查。
"""

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
README_ZH = REPO_ROOT / "README.md"

# D1 第 2–9 段的 H2 標題(依序)。
# 第 1 段(標題)為唯一的 H1,另以 test_readme_zh_has_single_h1_title 檢查。
ZH_SECTION_HEADINGS = [
    "專案亮點",
    "技術棧",
    "系統架構",
    "Quick Start",
    "部署與可觀測性",
    "開發流程",
    "Branch 規範",
    "延伸文件",
]

QUICK_START_FILES = [".env.example", "docker-compose.yml"]

# Markdown 行內連結 [text](target) 或 ![alt](target);target 可帶 "title"。
_LINK_RE = re.compile(r"!?\[[^\]]*\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)")
_FENCE_RE = re.compile(r"^(```|~~~).*?^\1", re.DOTALL | re.MULTILINE)
_INLINE_CODE_RE = re.compile(r"`[^`\n]*`")
_SKIP_PREFIXES = ("http://", "https://", "mailto:", "#")


def _strip_code(markdown: str) -> str:
    """移除 fenced code block 與 inline code span,其中的內容不視為連結或標題。"""
    return _INLINE_CODE_RE.sub("", _FENCE_RE.sub("", markdown))


def link_targets(markdown: str) -> list[str]:
    """回傳 markdown 中所有行內連結的 target(略過程式碼內容)。"""
    return _LINK_RE.findall(_strip_code(markdown))


def section(markdown: str, heading: str) -> str:
    """回傳指定 H2 標題底下、到下一個 H2 之前的內容;找不到時回傳空字串。"""
    match = re.search(
        rf"^## {re.escape(heading)}[ \t]*$(.*?)(?=^## |\Z)", markdown, re.DOTALL | re.MULTILINE
    )
    return match.group(1) if match else ""


def find_broken_links(markdown: str, base_dir: Path) -> list[str]:
    """回傳 markdown 中指向不存在路徑的相對連結 target。

    略過外部連結(http/https/mailto)與頁內錨點(#anchor);`path#anchor` 只檢查 path。
    程式碼(fenced block 與 inline code)內的內容不視為連結。
    """
    broken = []
    for target in link_targets(markdown):
        if target.startswith(_SKIP_PREFIXES):
            continue
        path_part = target.split("#", 1)[0]
        if not path_part:
            continue
        if not (base_dir / path_part).exists():
            broken.append(target)
    return broken


def headings(markdown: str, level: int) -> list[str]:
    """回傳指定層級的 ATX 標題文字(略過程式碼區塊)。"""
    text = _strip_code(markdown)
    prefix = "#" * level + " "
    return [
        line[len(prefix) :].strip()
        for line in text.splitlines()
        if line.startswith(prefix)
    ]


# --- 連結檢查器本身(負向案例) ---


class TestFindBrokenLinks:
    def test_reports_missing_relative_path(self, tmp_path):
        (tmp_path / "exists.md").write_text("ok")
        md = "[ok](exists.md) [bad](missing.md) [bad dir](nope/)"
        assert find_broken_links(md, tmp_path) == ["missing.md", "nope/"]

    def test_skips_external_and_anchor_links(self, tmp_path):
        md = (
            "[a](https://example.com/missing.md) [b](http://example.com) "
            "[c](#section) [d](mailto:x@example.com)"
        )
        assert find_broken_links(md, tmp_path) == []

    def test_checks_path_part_of_path_with_anchor(self, tmp_path):
        (tmp_path / "doc.md").write_text("ok")
        md = "[ok](doc.md#intro) [bad](gone.md#intro)"
        assert find_broken_links(md, tmp_path) == ["gone.md#intro"]

    def test_existing_directory_and_image_links(self, tmp_path):
        (tmp_path / "docs").mkdir()
        (tmp_path / "img.png").write_bytes(b"")
        md = "[dir](docs/) ![pic](img.png) ![missing](none.png)"
        assert find_broken_links(md, tmp_path) == ["none.png"]

    def test_ignores_links_inside_code_fences(self, tmp_path):
        md = "```\n[not a link](missing.md)\n```\n"
        assert find_broken_links(md, tmp_path) == []

    def test_ignores_links_inside_tilde_fences_and_inline_code(self, tmp_path):
        md = "~~~\n[x](missing.md)\n~~~\nsee `[y](also-missing.md)` here"
        assert find_broken_links(md, tmp_path) == []

    def test_angle_bracket_target_and_title_suffix(self, tmp_path):
        (tmp_path / "doc.md").write_text("ok")
        md = '[a](<doc.md>) [b](doc.md "Title") [c](<gone.md>) [d](gone2.md "T")'
        assert find_broken_links(md, tmp_path) == ["gone.md", "gone2.md"]

    def test_empty_markdown(self, tmp_path):
        assert find_broken_links("", tmp_path) == []


class TestSection:
    def test_returns_content_until_next_h2(self):
        md = "## A\nfoo\n### sub\nbar\n## B\nbaz\n"
        assert section(md, "A") == "\nfoo\n### sub\nbar\n"
        assert section(md, "B") == "\nbaz\n"

    def test_missing_or_prefix_only_heading_returns_empty(self):
        assert section("## Quick Start Guide\nx\n", "Quick Start") == ""
        assert section("", "Quick Start") == ""


# --- README.md(繁中主檔) ---


@pytest.fixture(scope="module")
def readme_zh() -> str:
    assert README_ZH.is_file(), "README.md 不存在"
    return README_ZH.read_text(encoding="utf-8")


def test_readme_zh_exists():
    assert README_ZH.is_file()


def test_readme_zh_has_single_h1_title(readme_zh):
    assert len(headings(readme_zh, 1)) == 1


def test_readme_zh_has_d1_sections_in_order(readme_zh):
    h2 = headings(readme_zh, 2)
    found = []
    for expected in ZH_SECTION_HEADINGS:
        matches = [i for i, h in enumerate(h2) if h == expected]
        assert matches, f"README.md 缺少段落標題:{expected}"
        found.append(matches[0])
    assert found == sorted(found), f"段落順序與 D1 不符:{h2}"


def test_readme_zh_relative_links_resolve(readme_zh):
    assert find_broken_links(readme_zh, README_ZH.parent) == []


def test_readme_zh_links_architecture_doc(readme_zh):
    # D1 第 4 段:系統架構摘要需連到 docs/report/01-architecture.md
    assert "docs/report/01-architecture.md" in link_targets(section(readme_zh, "系統架構"))


@pytest.mark.parametrize("filename", QUICK_START_FILES)
def test_quick_start_referenced_files_exist(readme_zh, filename):
    assert filename in section(readme_zh, "Quick Start"), f"Quick Start 未提到 {filename}"
    assert (REPO_ROOT / filename).is_file()
