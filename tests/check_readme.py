"""Checks that the English and the Chinese README stay in step, and that the counts they
quote are the real ones. Nothing is called and nothing is written.

The total number of checks is compared in tests/run_all.py, which is the only place that
counts them.

Run: python tests/check_readme.py
"""
import re
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT / "tests"))
import planted_bugs  # noqa: E402

results = []


def check(name, cond):
    results.append(bool(cond))
    print("PASS" if cond else "FAIL", "-", name)


english = (PROJECT / "README.md").read_text(encoding="utf-8")
chinese = (PROJECT / "README.zh-CN.md").read_text(encoding="utf-8")


def tables(text):
    """Every markdown table, as the list of its rows without the header and the separator."""
    found, current = [], []
    for line in text.splitlines() + [""]:
        if line.startswith("|"):
            current.append(line)
        elif current:
            found.append(current[2:])
            current = []
    return found


def figures(text):
    """The figures in a piece of text, sorted: two or more digits, or a decimal, without
    thousands separators. Single digits are left out, because one language may spell them out."""
    cleaned = (found.replace(",", "") for found in re.findall(r"\d[\d,]*(?:\.\d+)?", text))
    return sorted(figure for figure in cleaned if len(figure) >= 2)


def commands(text):
    """The commands of the first bash block, without comments and blank lines."""
    block = re.search(r"```bash\n(.*?)```", text, re.S)
    lines = (re.sub(r"\s+#.*$", "", line).strip() for line in (block.group(1) if block else "").splitlines())
    return [line for line in lines if line]


def local_links(text):
    """Link targets that are files or folders in the repository."""
    return [target.split("#")[0] for target in re.findall(r"\]\(([^)\s]+)\)", text)
            if not target.startswith(("http://", "https://", "#"))]


check("each README links to the other language above its first section",
      "(README.zh-CN.md)" in english.split("\n## ")[0] and "(README.md)" in chinese.split("\n## ")[0])
check("both have the same number of sections and subsections",
      len(re.findall(r"^## ", english, re.M)) == len(re.findall(r"^## ", chinese, re.M)) > 5
      and len(re.findall(r"^### ", english, re.M)) == len(re.findall(r"^### ", chinese, re.M)))
shapes = [len(table) for table in tables(english)]
check("both have the same tables, with the same number of rows", shapes == [len(table) for table in tables(chinese)] and sum(shapes) > 40)
different = [(number, row + 1, figures(ours), figures(theirs))
             for number, (first, second) in enumerate(zip(tables(english), tables(chinese)), start=1)
             for row, (ours, theirs) in enumerate(zip(first, second)) if figures(ours) != figures(theirs)]
for number, row, ours, theirs in different:
    print(f"       table {number}, row {row}: English {ours} | Chinese {theirs}")
check("every table row holds the same figures in both languages", not different)
check("both list the same commands to run", commands(english) == commands(chinese) and len(commands(english)) > 8)
files = len(list((PROJECT / "tests").glob("check_*.py")))
quoted_files = re.findall(r"in (\d+) files", english) + re.findall(r"分布在 (\d+) 个文件", chinese)
check("the number of check files they quote is the real one", len(quoted_files) == 4 and set(quoted_files) == {str(files)})
planted = sum(len(bugs) for _, _, bugs in planted_bugs.PLANTED)
quoted_bugs = re.findall(r"(\d+) planted bugs", english) + re.findall(r"(\d+) 个故意埋的 bug", chinese)
check("the number of planted bugs they quote is the real one", len(quoted_bugs) == 4 and set(quoted_bugs) == {str(planted)})
check("neither uses the project's internal names", not re.search(r"the owner|Project 2|mandarin", english + chinese, re.I))
missing = [target for target in local_links(english) + local_links(chinese) if target and not (PROJECT / target).exists()]
check("every file or folder they link to exists", not missing and len(local_links(english)) == len(local_links(chinese)))

print(f"\n{sum(results)}/{len(results)} checks passed")
sys.exit(0 if all(results) else 1)
