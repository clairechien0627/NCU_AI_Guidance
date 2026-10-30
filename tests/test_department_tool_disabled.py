import ast
import unittest
from pathlib import Path


TOOLS_PATH = (
    Path(__file__).resolve().parents[1]
    / "backend"
    / "app"
    / "services"
    / "tools.py"
)


class DepartmentToolDisabledTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tree = ast.parse(TOOLS_PATH.read_text(encoding="utf-8"))

    def test_department_info_is_not_in_dispatch_map(self):
        tool_map = next(
            node.value
            for node in self.tree.body
            if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "_TOOL_MAP" for target in node.targets)
        )
        keys = {
            key.value
            for key in tool_map.keys
            if isinstance(key, ast.Constant) and isinstance(key.value, str)
        }
        self.assertNotIn("get_dept_info", keys)

    def test_department_info_is_not_advertised_to_agent(self):
        tools_list = next(
            node.value
            for node in self.tree.body
            if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "TOOLS" for target in node.targets)
        )
        advertised_names = {
            node.value
            for node in ast.walk(tools_list)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        }
        self.assertNotIn("get_dept_info", advertised_names)


if __name__ == "__main__":
    unittest.main()
