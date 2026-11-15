#!/usr/bin/env python3
"""检查左侧领域导航的正式中文名、计数和折叠标记。"""
import re
import unittest
from pathlib import Path

import build

ROOT = Path(__file__).resolve().parent
DOCS = ROOT / "docs"

# 当前已发布目录。新增课程后若计数变化，需同步这里。
EXPECTED = {
    "A": ("计算机科学基础", 16, 16),
    "B": ("现代 C++", 16, 16),
    "C": ("Linux 系统编程", 16, 16),
    "D": ("网络与高并发", 16, 16),
    "E": ("并发与内存模型", 16, 16),
    "F": ("RPC 与通信", 16, 16),
    "G": ("数据库与存储", 16, 16),
    "H": ("分布式系统", 15, 15),
    "I": ("性能工程", 14, 14),
    "J": ("软件与生产工程", 15, 15),
    "K": ("LLM 工程基础", 6, 12),
    "L": ("Agent 与 Harness", 0, 18),
    "M": ("Backend + AI", 0, 10),
    "N": ("架构与技术领导力", 0, 12),
}

GROUP_RE = re.compile(
    r"<details(?P<open> open)?><summary>领域 (?P<letter>[A-Z])：(?P<name>.*?)（(?P<done>\d+)/(?P<total>\d+)）</summary>(?P<body>.*?)</details>",
    re.S,
)


def pages():
    found = [DOCS / "index.html"]
    found.extend(sorted((DOCS / "lessons").glob("*.html"), key=lambda p: int(p.stem)))
    return found


def groups(text):
    nav = text.split("<main>", 1)[0]
    return list(GROUP_RE.finditer(nav))


class DomainTitleTests(unittest.TestCase):
    def test_titles_match_meta_and_requested_a(self):
        self.assertEqual(list(build.DOMAIN_TITLES), list(EXPECTED))
        for letter, (name, _, _) in EXPECTED.items():
            self.assertEqual(build.DOMAIN_TITLES[letter], name)

    def test_summary_keeps_counts(self):
        self.assertEqual(build.domain_summary("A", 16, 16), "领域 A：计算机科学基础（16/16）")
        self.assertEqual(build.domain_summary("K", 0, 12), "领域 K：LLM 工程基础（0/12）")
        self.assertEqual(build.domain_summary("J", 4, 15), "领域 J：软件与生产工程（4/15）")
        self.assertEqual(build.domain_summary("B", 3, 9), "领域 B：现代 C++（3/9）")

    def test_unknown_letter_is_rejected(self):
        self.assertEqual(build.missing_domain_titles(["A", "A", "Z", "B"]), ["Z"])
        with self.assertRaises(SystemExit):
            build.domain_summary("Z", 0, 1)

    def test_generator_uses_domain_summary(self):
        src = (ROOT / "build.py").read_text(encoding="utf-8")
        self.assertIn("domain_summary(", src)
        self.assertNotIn("领域 {d}（", src)

    def test_every_page_has_named_groups_and_same_counts(self):
        html_pages = pages()
        self.assertEqual(len(html_pages), 163)
        for path in html_pages:
            text = path.read_text(encoding="utf-8")
            found = groups(text)
            self.assertEqual([m.group("letter") for m in found], list(EXPECTED), path.name)
            for m in found:
                letter = m.group("letter")
                name, done_n, total_n = EXPECTED[letter]
                self.assertEqual(m.group("name"), name, path.name)
                self.assertEqual(int(m.group("done")), done_n, path.name)
                self.assertEqual(int(m.group("total")), total_n, path.name)
                self.assertEqual(m.group(0).count("<summary>"), 1, path.name)
                body = m.group("body")
                links = re.findall(r'<li><a class="[^"]*" href="(?:\.\./)?lessons/\d+\.html">', body)
                todos = re.findall(r'<li class="todo">', body)
                items = re.findall(r"<li[ >]", body)
                self.assertEqual(len(links), done_n, f"{path.name} {letter} links")
                self.assertEqual(len(todos), total_n - done_n, f"{path.name} {letter} todos")
                self.assertEqual(len(items), total_n, f"{path.name} {letter} items")
                self.assertNotIn("<details", body, path.name)
            self.assertIsNone(re.search(r"<summary>领域 [A-Z]（", text), path.name)
            main = text.split("<main>", 1)[1]
            self.assertNotIn("领域 A：计算机科学基础", main)
            self.assertNotIn("：现代 C++", main)
            self.assertNotRegex(main, r"领域 [A-Z]：")

    def test_collapse_flags(self):
        index = groups((DOCS / "index.html").read_text(encoding="utf-8"))
        for m in index:
            done_n = int(m.group("done"))
            self.assertEqual(bool(m.group("open")), done_n > 0, m.group("letter"))
        for lesson, letter in (("1", "A"), ("33", "C"), ("145", "J")):
            found = groups((DOCS / "lessons" / f"{lesson}.html").read_text(encoding="utf-8"))
            opened = [m.group("letter") for m in found if m.group("open")]
            self.assertEqual(opened, [letter])
            current = [m.group("letter") for m in found if 'class="cur"' in m.group("body")]
            self.assertEqual(current, [letter])

    def test_lesson_meta_and_parenthetical_body_stay(self):
        lesson23 = (DOCS / "lessons" / "23.html").read_text(encoding="utf-8")
        lesson32 = (DOCS / "lessons" / "32.html").read_text(encoding="utf-8")
        lesson33 = (DOCS / "lessons" / "33.html").read_text(encoding="utf-8")
        self.assertIn("领域 B（C++）", lesson23.split("<main>", 1)[1])
        self.assertIn("领域 B（C++）", lesson32.split("<main>", 1)[1])
        self.assertIn("领域 C（C 语言）", lesson33.split("<main>", 1)[1])
        for text, number, letter in ((lesson23, 23, "B"), (lesson33, 33, "C")):
            main = text.split("<main>", 1)[1]
            self.assertIn(f"第 {number} 课 · 领域 {letter} · 阶段", main)
        self.assertIn('href="../files/23-b-stl.md"', lesson23)
        lesson33_main = lesson33.split("<main>", 1)[1]
        self.assertIn('href="../files/33-c-process.md"', lesson33_main)
        self.assertIn('href="../files/practice33.tar.gz"', lesson33_main)


if __name__ == "__main__":
    unittest.main()
