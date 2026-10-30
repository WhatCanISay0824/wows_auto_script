"""仓库测试包。

显式使用普通包（而非命名空间包），避免被 site-packages 里同名的 `tests` 包遮蔽，
从而允许 `from tests.helpers import ...` 在 pytest 与直接运行时都可用。
"""
