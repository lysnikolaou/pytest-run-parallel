import ast
import functools
import inspect

try:
    # added in hypothesis 6.131.0
    from hypothesis import is_hypothesis_test
except ImportError:
    try:
        # hypothesis versions < 6.131.0
        from hypothesis.internal.detection import is_hypothesis_test
    except ImportError:
        # hypothesis isn't installed
        def is_hypothesis_test(fn):
            return False


THREAD_UNSAFE_FIXTURES = {
    "capsys",
    "monkeypatch",
    "recwarn",
}


BLOCKLIST = {
    ("pytest", "warns"),
    ("pytest", "deprecated_call"),
    ("_pytest.recwarn", "warns"),
    ("_pytest.recwarn", "deprecated_call"),
    ("warnings", "catch_warnings"),
    ("unittest.mock", "*"),
    ("mock", "*"),
    ("ctypes", "*"),
}


class ThreadUnsafeNodeVisitor(ast.NodeVisitor):
    def __init__(self, fn, skip_set, level=0):
        self.thread_unsafe = False
        self.thread_unsafe_reason = None
        self.blocklist = BLOCKLIST | skip_set
        self.module_blocklist = {mod for mod, func in self.blocklist if func == "*"}
        self.function_blocklist = {
            (mod, func) for mod, func in self.blocklist if func != "*"
        }

        modules = {mod.split(".")[0] for mod, _ in self.blocklist}
        modules |= {mod for mod, _ in self.blocklist}

        self.fn = fn
        self.skip_set = skip_set
        self.level = level
        self.modules_aliases = {}
        self.func_aliases = {}
        for var_name in getattr(fn, "__globals__", {}):
            value = fn.__globals__[var_name]
            if inspect.ismodule(value) and value.__name__ in modules:
                self.modules_aliases[var_name] = value.__name__
            elif inspect.isfunction(value):
                if value.__module__ in modules:
                    self.func_aliases[var_name] = (value.__module__, value.__name__)
                    continue

                all_parents = self._create_all_parent_modules(value.__module__)
                for parent in all_parents:
                    if parent in modules:
                        self.func_aliases[var_name] = (parent, value.__name__)
                        break

        super().__init__()

    def _create_all_parent_modules(self, module_name):
        all_parent_modules = set()
        parent, dot, _ = module_name.rpartition(".")
        while dot:
            all_parent_modules.add(parent)
            parent, dot, _ = parent.rpartition(".")
        return all_parent_modules

    def _is_module_blocklisted(self, module_name):
        # fast path
        if module_name in self.module_blocklist:
            return True

        # try parent modules
        all_parents = self._create_all_parent_modules(module_name)
        if any(parent in self.module_blocklist for parent in all_parents):
            return True
        return False

    def _is_function_blocklisted(self, module_name, func_name):
        # Whole module is blocked
        if self._is_module_blocklisted(module_name):
            return True

        # Function is blocked
        if (module_name, func_name) in self.function_blocklist:
            return True

        return False

    def _recursive_analyze_attribute(self, node):
        current = node
        while isinstance(current.value, ast.Attribute):
            current = current.value
        if not isinstance(current.value, ast.Name):
            return
        id = current.value.id

        def _get_child_fn(mod, node):
            if isinstance(node.value, ast.Attribute):
                submod = _get_child_fn(mod, node.value)
                return getattr(submod, node.attr, None)

            if not isinstance(node.value, ast.Name):
                return None
            return getattr(mod, node.attr, None)

        if id in getattr(self.fn, "__globals__", {}):
            mod = self.fn.__globals__[id]
            child_fn = _get_child_fn(mod, node)
            if child_fn is not None and callable(child_fn):
                self.thread_unsafe, self.thread_unsafe_reason = (
                    identify_thread_unsafe_nodes(
                        child_fn, self.skip_set, self.level + 1
                    )
                )

    def _build_attribute_chain(self, node):
        chain = []
        current = node

        while isinstance(current, ast.Attribute):
            chain.insert(0, current.attr)
            current = current.value

        if isinstance(current, ast.Name):
            chain.insert(0, current.id)

        return chain

    def _visit_attribute_call(self, node):
        if isinstance(node.value, ast.Name):
            real_mod = node.value.id
            if real_mod in self.modules_aliases:
                real_mod = self.modules_aliases[real_mod]
            if self._is_function_blocklisted(real_mod, node.attr):
                self.thread_unsafe = True
                self.thread_unsafe_reason = (
                    "calls thread-unsafe function: " f"{real_mod}.{node.attr}"
                )
            elif self.level < 2:
                self._recursive_analyze_attribute(node)
        elif isinstance(node.value, ast.Attribute):
            chain = self._build_attribute_chain(node)
            module_part = ".".join(chain[:-1])
            func_part = chain[-1]
            if self._is_function_blocklisted(module_part, func_part):
                self.thread_unsafe = True
                self.thread_unsafe_reason = (
                    f"calls thread-unsafe function: {'.'.join(chain)}"
                )
            elif self.level < 2:
                self._recursive_analyze_attribute(node)

    def _recursive_analyze_name(self, node):
        if node.id in getattr(self.fn, "__globals__", {}):
            child_fn = self.fn.__globals__[node.id]
            if callable(child_fn):
                self.thread_unsafe, self.thread_unsafe_reason = (
                    identify_thread_unsafe_nodes(
                        child_fn, self.skip_set, self.level + 1
                    )
                )

    def _visit_name_call(self, node):
        if node.id in self.func_aliases:
            if self._is_function_blocklisted(*self.func_aliases[node.id]):
                self.thread_unsafe = True
                self.thread_unsafe_reason = f"calls thread-unsafe function: {node.id}"
                return

        if self.level < 2:
            self._recursive_analyze_name(node)

    def visit_Call(self, node):
        if isinstance(node.func, ast.Attribute):
            self._visit_attribute_call(node.func)
        elif isinstance(node.func, ast.Name):
            self._visit_name_call(node.func)
        self.generic_visit(node)

    def visit_Assign(self, node):
        if len(node.targets) == 1:
            name_node = node.targets[0]
            value_node = node.value
            if getattr(name_node, "id", None) == "__thread_safe__" and not bool(
                value_node.value
            ):
                self.thread_unsafe = True
                self.thread_unsafe_reason = (
                    f"calls thread-unsafe function: {self.fn.__name__} "
                    "(inferred via func.__thread_safe__ == False)"
                )
                return

        self.generic_visit(node)

    def visit(self, node):
        if self.thread_unsafe:
            return
        return super().visit(node)


def _identify_thread_unsafe_nodes(fn, skip_set, level=0):
    if is_hypothesis_test(fn):
        return True, "uses hypothesis"

    try:
        src = inspect.getsource(fn)
    except Exception:
        return False, None

    try:
        tree = ast.parse(src.lstrip())
    except Exception:
        return False, None

    visitor = ThreadUnsafeNodeVisitor(fn, skip_set, level=level)
    visitor.visit(tree)
    return visitor.thread_unsafe, visitor.thread_unsafe_reason


cached_thread_unsafe_identify = functools.lru_cache(_identify_thread_unsafe_nodes)


def identify_thread_unsafe_nodes(fn, skip_set, level=0):
    try:
        return cached_thread_unsafe_identify(fn, skip_set, level=level)
    except TypeError:
        return _identify_thread_unsafe_nodes(fn, skip_set, level=level)
