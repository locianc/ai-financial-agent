"""Phase 20A.1：项目基础测试 fixture。

tests/test_store.py 的用例声明 `tmp: str` 参数（临时目录路径字符串），
但 pytest 没有内置名为 tmp 的 fixture（内置的是 tmp_path / tmpdir），
项目此前也没有任何 conftest 定义它，导致 pytest 收集该文件时报
`fixture 'tmp' not found`（该文件直接运行时由自身 main() 用
tempfile.TemporaryDirectory 提供 tmp，故直接运行不受影响）。

此 conftest 补齐该基础 fixture，使 pytest 与直接运行两种模式都可用。
"""

from __future__ import annotations

import tempfile
from typing import Iterator

import pytest


@pytest.fixture
def tmp() -> Iterator[str]:
    """临时目录路径字符串，契约与 tests/test_store.py 声明的 `tmp: str` 一致。"""
    with tempfile.TemporaryDirectory(prefix="test_store_") as tmp_dir:
        yield tmp_dir
