# -*- coding: utf-8 -*-
"""typecheck.py —— 类型 gate 的静态检查器（纯标准库，零依赖）。

为什么自己写：本机没有 mypy / pyflakes，而「类型 gate」必须是程序化、确定性的。
本版检查三件事（v1 口径，够用且不误报）：

  1. **语法外的名称解析**：所有 ``Name`` 读取都必须能在（局部 → 闭包 → 模块 → 内置）
     作用域链里找到定义。这能抓住 8B 模型最常见的一类错误——写出调用了一个
     不存在的函数 / 变量名（拼错、漏 import、以为某个名字已经存在）。
  2. **公开接口的注解完备**：模块层与类层的函数，参数（除 ``self``/``cls``）与返回值
     都必须有类型注解。模型要新增函数时，这条把它从「随手写」拉到「可被静态检查」。
  3. **重复定义**：模块层同一个名字被 def 两次——典型症状是模型「追加」了同名函数
     而不是改原函数。

明确不做（写进契约，避免过度承诺）：不推断类型、不做跨文件解析、不做调用元数检查。
所以它叫「类型 gate 的 v1 近似」，不是 mypy 替代品。真实类型错误最终仍由测试 gate 兜底。
"""
from __future__ import annotations

import ast
import builtins
from typing import Any, Dict, List, Optional

#: 允许但不需要定义的名字（内置 + 模块级魔法名 + 方法首参）
ALLOWED_GLOBALS = set(dir(builtins)) | {
    "self", "cls", "__file__", "__name__", "__doc__", "__package__", "__spec__",
    "__loader__", "__builtins__", "__all__", "__annotations__", "__debug__",
    "True", "False", "None", "NotImplemented", "Ellipsis",
}


class _Scope:
    def __init__(self, kind: str, parent: Optional["_Scope"] = None) -> None:
        self.kind = kind            # module / class / function / comprehension
        self.parent = parent
        self.names: set = set()

    def chain(self) -> List["_Scope"]:
        out, s = [], self
        while s is not None:
            out.append(s)
            s = s.parent
        return out


def _closure_parent(scope: _Scope) -> Optional[_Scope]:
    """函数体的可见链不含外层类作用域（Python 语义）；模块/函数作用域原样返回。"""
    s: Optional[_Scope] = scope
    while s is not None and s.kind == "class":
        s = s.parent
    return s


def _bind_target(node: ast.AST, scope: _Scope) -> None:
    if isinstance(node, ast.Name):
        scope.names.add(node.id)
    elif isinstance(node, (ast.Tuple, ast.List)):
        for e in node.elts:
            _bind_target(e, scope)
    elif isinstance(node, ast.Starred):
        _bind_target(node.value, scope)
    # Attribute / Subscript 不是名字绑定，忽略


def _bind_args(args: ast.arguments, scope: _Scope) -> None:
    for a in list(getattr(args, "posonlyargs", [])) + list(args.args) + list(args.kwonlyargs):
        scope.names.add(a.arg)
    if args.vararg:
        scope.names.add(args.vararg.arg)
    if args.kwarg:
        scope.names.add(args.kwarg.arg)


class _Checker:
    def __init__(self, require_annotations: bool = True) -> None:
        self.require_annotations = require_annotations
        self.issues: List[Dict[str, Any]] = []
        self.scopes: Dict[int, _Scope] = {}
        self.module_scope = _Scope("module")
        self.module_level_defs: Dict[str, int] = {}

    # ---------------- 问题收集 ----------------
    def _add(self, type_: str, node: ast.AST, message: str, hint: str = "") -> None:
        self.issues.append({"type": type_, "message": message,
                            "line": getattr(node, "lineno", 0) or 0,
                            "col": getattr(node, "col_offset", 0) or 0,
                            "hint": hint})

    # ---------------- 第一遍：收集绑定 ----------------
    def collect(self, node: ast.AST, scope: _Scope, depth: int = 0) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if scope.kind == "module":
                prev = self.module_level_defs.get(node.name)
                if prev is not None:
                    self._add("duplicate_def", node,
                              "模块层重复定义 %s（第 %d 行已定义过一次）" % (node.name, prev),
                              "若要改已有函数，请改它本身，不要追加同名新函数")
                else:
                    self.module_level_defs[node.name] = node.lineno
            scope.names.add(node.name)
            inner = _Scope("function", parent=_closure_parent(scope))
            _bind_args(node.args, inner)
            self.scopes[id(node)] = inner
            for st in node.body:
                self.collect(st, inner, depth + 1)
            return
        if isinstance(node, ast.ClassDef):
            scope.names.add(node.name)
            cls_scope = _Scope("class", parent=scope)
            self.scopes[id(node)] = cls_scope
            for st in node.body:
                self.collect(st, cls_scope, depth + 1)
            return
        if isinstance(node, ast.Lambda):
            inner = _Scope("function", parent=_closure_parent(scope))
            _bind_args(node.args, inner)
            self.scopes[id(node)] = inner
            self.collect(node.body, inner, depth + 1)
            return
        if isinstance(node, ast.arguments):  # 兜底：不应被泛化递归到
            return
        if isinstance(node, (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)):
            comp = _Scope("comprehension", parent=scope)
            self.scopes[id(node)] = comp
            for gen in node.generators:
                _bind_target(gen.target, comp)      # ★ 推导式变量属于它自己的隐式作用域
                self.collect(gen.target, comp, depth)
                self.collect(gen.iter, scope, depth)
                for cond in gen.ifs:
                    self.collect(cond, comp, depth)
            if isinstance(node, ast.DictComp):
                self.collect(node.key, comp, depth)
                self.collect(node.value, comp, depth)
            else:
                self.collect(node.elt, comp, depth)
            return
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for a in node.names:
                scope.names.add(a.asname or a.name.split(".")[0])
            return
        if isinstance(node, ast.Assign):
            for t in node.targets:
                _bind_target(t, scope)
            for t in node.targets:
                self.collect(t, scope, depth)
            self.collect(node.value, scope, depth)
            return
        if isinstance(node, ast.AnnAssign):
            _bind_target(node.target, scope)
            self.collect(node.target, scope, depth)
            if node.value:
                self.collect(node.value, scope, depth)
            return
        if isinstance(node, ast.AugAssign):
            _bind_target(node.target, scope)
            return
        if isinstance(node, (ast.For, ast.AsyncFor)):
            _bind_target(node.target, scope)
            for st in list(node.body) + list(node.orelse):
                self.collect(st, scope, depth)
            return
        if isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                if item.optional_vars is not None:
                    _bind_target(item.optional_vars, scope)
            for st in node.body:
                self.collect(st, scope, depth)
            return
        if isinstance(node, ast.Try) or type(node).__name__ == "TryStar":
            for st in node.body:
                self.collect(st, scope, depth)
            for h in node.handlers:
                if h.name:
                    scope.names.add(h.name)
                for st in h.body:
                    self.collect(st, scope, depth)
            for st in node.orelse + node.finalbody:
                self.collect(st, scope, depth)
            return
        if isinstance(node, (ast.Global, ast.Nonlocal)):
            for n in node.names:
                scope.names.add(n)
            return
        if isinstance(node, ast.NamedExpr):
            _bind_target(node.target, scope)
            self.collect(node.value, scope, depth)
            return
        if isinstance(node, ast.Match):
            for case in node.cases:
                for sub in ast.walk(case.pattern):
                    if isinstance(sub, (ast.MatchAs, ast.MatchStar)) and sub.name:
                        scope.names.add(sub.name)
                    if isinstance(sub, ast.MatchMapping) and sub.rest:
                        scope.names.add(sub.rest)
                for st in case.body:
                    self.collect(st, scope, depth)
            return
        for child in ast.iter_child_nodes(node):
            self.collect(child, scope, depth)

    # ---------------- 第二遍：解析名字 ----------------
    def _resolve(self, name: str, scope: _Scope) -> bool:
        if name in ALLOWED_GLOBALS:
            return True
        for s in scope.chain():
            if name in s.names:
                return True
        return False

    def _check_ann(self, ann: Optional[ast.AST], scope: _Scope) -> None:
        if ann is None:
            return
        for sub in ast.walk(ann):
            if isinstance(sub, ast.Name) and not isinstance(sub.ctx, ast.Store):
                if not self._resolve(sub.id, scope):
                    self._add("undefined_name", sub,
                              "类型注解里的名字 %s 未定义" % sub.id,
                              "确认已 import，或名字拼写正确")

    def check_names(self, node: ast.AST, scope: _Scope) -> None:
        if isinstance(node, ast.Name):
            if isinstance(node.ctx, ast.Load) and not self._resolve(node.id, scope):
                self._add("undefined_name", node,
                          "名字 %s 未定义（未 import、未赋值、也不是内置）" % node.id,
                          "检查拼写，或把需要的名字 import 进来")
            return
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for dec in node.decorator_list:
                self.check_names(dec, scope)
            for d in list(node.args.defaults) + [d for d in node.args.kw_defaults if d]:
                self.check_names(d, scope)
            inner = self.scopes.get(id(node), scope)
            if self.require_annotations and scope.kind in ("module", "class"):
                for a in list(getattr(node.args, "posonlyargs", [])) + list(node.args.args) + list(node.args.kwonlyargs):
                    if a.arg in ("self", "cls"):
                        continue
                    if a.annotation is None:
                        self._add("missing_annotation", node if not a.lineno else a,
                                  "函数 %s 的参数 %s 缺少类型注解" % (node.name, a.arg),
                                  "给每个参数加类型注解，例如 %s: int" % a.arg)
                if node.returns is None:
                    self._add("missing_annotation", node,
                              "函数 %s 缺少返回值类型注解" % node.name,
                              "补上 -> 类型，例如 -> None / -> int")
            for a in list(getattr(node.args, "posonlyargs", [])) + list(node.args.args) + list(node.args.kwonlyargs):
                self._check_ann(a.annotation, scope)
            if node.args.vararg:
                self._check_ann(node.args.vararg.annotation, scope)
            if node.args.kwarg:
                self._check_ann(node.args.kwarg.annotation, scope)
            self._check_ann(node.returns, scope)
            for st in node.body:
                self.check_names(st, inner)
            return
        if isinstance(node, ast.Lambda):
            inner = self.scopes.get(id(node), scope)
            for d in list(node.args.defaults) + [d for d in node.args.kw_defaults if d]:
                self.check_names(d, scope)
            self.check_names(node.body, inner)
            return
        if isinstance(node, ast.ClassDef):
            for b in node.bases + [k.value for k in node.keywords] + node.decorator_list:
                self.check_names(b, scope)
            inner = self.scopes.get(id(node), scope)
            for st in node.body:
                self.check_names(st, inner)
            return
        if isinstance(node, (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)):
            comp = self.scopes.get(id(node), scope)
            for gen in node.generators:
                self.check_names(gen.iter, scope)
                self.check_names(gen.target, comp)
                for cond in gen.ifs:
                    self.check_names(cond, comp)
            if isinstance(node, ast.DictComp):
                self.check_names(node.key, comp)
                self.check_names(node.value, comp)
            else:
                self.check_names(node.elt, comp)
            return
        for child in ast.iter_child_nodes(node):
            self.check_names(child, scope)


_STMT_LIST_ATTRS = ("body", "orelse", "finalbody")


def _check_flow(body: List[ast.stmt], checker: "_Checker") -> None:
    '''★ 死代码 / 不可达代码（由 2026-09-27 qwen3:8b 真机跑暴露）。

    那次模型把新实现插到函数顶部，但**旧函数体没删**，于是出现：

        def average(nums: List[int]) -> float:
            """计算平均值。"""
            if not nums:
                return 0.0
            return sum(nums) / len(nums)      # ← 正常返回
            """计算平均值。"""               # ← 以下全部不可达
            raise NotImplementedError(...)

    语法、类型、测试三个 gate 全部通过（函数语义是对的，旧代码永不执行），但补丁是脏的。
    这类「能跑但留着死代码」只能由静态检查拦。
    '''
    seen_term = False
    for i, st in enumerate(body):
        if seen_term and not isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef,
                                            ast.ClassDef, ast.Pass)):
            checker._add("unreachable_code", st,
                         "前面的 return/raise 已经结束控制流，这段代码永远不会执行",
                         "把这段残留的旧代码删掉，或者把它并入上面的实现——不要留在函数里")
            break
        if isinstance(st, (ast.Return, ast.Raise)):
            seen_term = True
        elif (isinstance(st, ast.Expr) and isinstance(st.value, ast.Constant)
                and isinstance(st.value.value, str) and i > 0):
            checker._add("dead_string", st,
                         "这里有一句孤立的字符串（像是被遗下的文档字符串），不产生任何作用",
                         "删掉它，或改成注释")
        for child in ast.iter_child_nodes(st):
            for attr in _STMT_LIST_ATTRS:
                sub = getattr(child, attr, None)
                if isinstance(sub, list) and sub and isinstance(sub[0], ast.stmt):
                    _check_flow(sub, checker)
            for h in (getattr(child, "handlers", None) or []):
                if getattr(h, "body", None):
                    _check_flow(h.body, checker)
            for case in (getattr(child, "cases", None) or []):
                if getattr(case, "body", None):
                    _check_flow(case.body, checker)


def _walk_flow(tree: ast.AST, checker: "_Checker") -> None:
    """遍历模块/函数/类体，做死代码检查。"""
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", None)
            if isinstance(body, list) and body and isinstance(body[0], ast.stmt):
                _check_flow(body, checker)


def check_source(src: str, filename: str = "<target>",
                 require_annotations: bool = True) -> List[Dict[str, Any]]:
    """对源码做 v1 静态检查，返回问题列表（空列表 = 通过）。

    调用方需先保证语法正确（语法 gate 在前）；语法错时本函数抛 ``SyntaxError``。
    """
    tree = ast.parse(src, filename=filename)
    c = _Checker(require_annotations=require_annotations)
    for st in tree.body:
        c.collect(st, c.module_scope, 0)
    for st in tree.body:
        c.check_names(st, c.module_scope)

    # 保留性检查（与原始副本对比）在 gate 侧做；这里补死代码/不可达代码检查
    _walk_flow(tree, c)

    # 去重（同一位置同一类型只报一次）
    seen, out = set(), []
    for i in c.issues:
        key = (i["type"], i["line"], i["col"], i["message"])
        if key in seen:
            continue
        seen.add(key)
        out.append(i)
    return out


def check_file(path: str, require_annotations: bool = True) -> List[Dict[str, Any]]:
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        src = f.read()
    return check_source(src, filename=path, require_annotations=require_annotations)


# ============================================================
# 保留性检查（★ 由首次真机运行暴露的缺口）
# ============================================================
# 背景：2026-09-27 首次真机跑五节点时，模型为了消掉「名字 main 未定义」这个类型错误，
# 不是把 main() 加回来，而是把 `if __name__ == "__main__": main()` 连同 main() 一起删了——
# 三个 gate 全部通过，但文件里无关的入口代码被静默删除。
# 这类「改动合法、但删了不该删的东西」只有保留性检查能拦。
MODULE_SYMBOL_NOTE = "本单只做增量修改；不要删除原有函数/类/常量/入口，除非任务明确要求"


def module_symbols(src: str):
    """取模块层符号：顶层函数/类名、顶层赋值名，以及是否存在 __main__ 入口守卫。"""
    tree = ast.parse(src)
    names = set()
    has_main_guard = False
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    names.add(t.id)
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name):
                names.add(node.target.id)
        elif isinstance(node, ast.If):
            test = node.test
            if (isinstance(test, ast.Compare) and isinstance(test.left, ast.Name)
                    and test.left.id == "__name__"):
                has_main_guard = True
    return names, has_main_guard


def preservation_issues(orig_src: str, new_src: str) -> List[Dict[str, Any]]:
    """对比「改动前」与「改动后」的模块层符号，报出被删掉的东西。"""
    issues: List[Dict[str, Any]] = []
    try:
        old_names, old_guard = module_symbols(orig_src)
        new_names, new_guard = module_symbols(new_src)
    except SyntaxError:
        return issues          # 语法 gate 在前面已经处理
    for n in sorted(old_names - new_names):
        issues.append({"type": "removed_symbol", "line": 0, "col": 0,
                       "message": "原有顶层符号 %s 被删除" % n,
                       "hint": MODULE_SYMBOL_NOTE})
    if old_guard and not new_guard:
        issues.append({"type": "removed_main_guard", "line": 0, "col": 0,
                       "message": '原有的 `if __name__ == "__main__":` 入口被删除',
                       "hint": MODULE_SYMBOL_NOTE})
    return issues
