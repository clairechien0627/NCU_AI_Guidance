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
GRAPH_SERVICE_PATH = TOOLS_PATH.with_name("graph_service.py")
INDEX_BUILDER_PATH = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "rag"
    / "build_vector_index.py"
)


def _function(tree: ast.Module, name: str) -> ast.FunctionDef:
    return next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == name
    )


class DepartmentToolDisabledTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tree = ast.parse(TOOLS_PATH.read_text(encoding="utf-8"))
        cls.graph_tree = ast.parse(GRAPH_SERVICE_PATH.read_text(encoding="utf-8"))
        cls.index_tree = ast.parse(INDEX_BUILDER_PATH.read_text(encoding="utf-8"))

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

    def test_direct_department_info_call_is_fail_closed(self):
        function = _function(self.tree, "tool_get_dept_info")
        constants = {
            node.value
            for node in ast.walk(function)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        }
        referenced_names = {
            node.id
            for node in ast.walk(function)
            if isinstance(node, ast.Name)
        }

        self.assertIn("get_dept_info_disabled", constants)
        self.assertNotIn("retriever", referenced_names)

    def test_department_name_resolution_does_not_use_department_vectors(self):
        function = _function(self.graph_tree, "_find_dept_like")
        constants = {
            node.value
            for node in ast.walk(function)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        }
        called_attributes = {
            node.attr
            for node in ast.walk(function)
            if isinstance(node, ast.Attribute)
        }

        self.assertNotIn("ncu_departments", constants)
        self.assertNotIn("query_points", called_attributes)

    def test_full_index_build_does_not_build_departments_collection(self):
        function = _function(self.index_tree, "main")
        called_functions = {
            node.func.id
            for node in ast.walk(function)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }

        self.assertNotIn("build_departments_collection", called_functions)


if __name__ == "__main__":
    unittest.main()
