from __future__ import annotations

import ast
import importlib
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
CORE_DIR = ROOT / "core" / "core"
REPORT_JSON = ROOT / "reports" / "kernel_diagnostic_inventory.json"
REPORT_MD = ROOT / "reports" / "kernel_diagnostic_inventory.md"
ORCHESTRATOR_PATH = CORE_DIR / "orchestrator.py"
DIAGNOSTIC_CONTRACTS_PATH = CORE_DIR / "diagnostic_contracts.py"
SELF_DIAG_PATH = CORE_DIR / "self_diagnostic_module.py"

LIFECYCLE_METHODS = {"on_load", "on_unload", "before_task", "after_task", "finalize", "snapshot", "refresh", "run_diagnostics", "run_layer_diagnostics"}


def _src(node: ast.AST | None) -> str | None:
    if node is None:
        return None
    try:
        return ast.unparse(node)
    except Exception:
        return None


class FunctionAnalyzer(ast.NodeVisitor):
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.local_vars: set[str] = set()
        self.env_vars: set[str] = set()
        self.module_refs: set[str] = set()
        self.context_refs: set[str] = set()
        self.return_dict_keys: list[list[str]] = []
        self.block_counts = defaultdict(int)

    def visit_Assign(self, node: ast.Assign) -> Any:
        for target in node.targets:
            self._collect_target(target)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> Any:
        self._collect_target(node.target)
        self.generic_visit(node)

    def visit_AugAssign(self, node: ast.AugAssign) -> Any:
        self._collect_target(node.target)
        self.generic_visit(node)

    def visit_For(self, node: ast.For) -> Any:
        self.block_counts['for'] += 1
        self._collect_target(node.target)
        self.generic_visit(node)

    def visit_AsyncFor(self, node: ast.AsyncFor) -> Any:
        self.block_counts['async_for'] += 1
        self._collect_target(node.target)
        self.generic_visit(node)

    def visit_While(self, node: ast.While) -> Any:
        self.block_counts['while'] += 1
        self.generic_visit(node)

    def visit_If(self, node: ast.If) -> Any:
        self.block_counts['if'] += 1
        self.generic_visit(node)

    def visit_Try(self, node: ast.Try) -> Any:
        self.block_counts['try'] += 1
        self.generic_visit(node)

    def visit_With(self, node: ast.With) -> Any:
        self.block_counts['with'] += 1
        self.generic_visit(node)

    def visit_AsyncWith(self, node: ast.AsyncWith) -> Any:
        self.block_counts['async_with'] += 1
        self.generic_visit(node)

    def visit_Await(self, node: ast.Await) -> Any:
        self.block_counts['await'] += 1
        self.generic_visit(node)

    def visit_Return(self, node: ast.Return) -> Any:
        self.block_counts['return'] += 1
        if isinstance(node.value, ast.Dict):
            keys = []
            for key in node.value.keys:
                if isinstance(key, ast.Constant) and isinstance(key.value, str):
                    keys.append(key.value)
            if keys:
                self.return_dict_keys.append(keys)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> Any:
        call_name = _src(node.func)
        if call_name:
            self.calls.append(call_name)
        if isinstance(node.func, ast.Attribute) and node.func.attr == 'getenv' and isinstance(node.func.value, ast.Name) and node.func.value.id == 'os':
            if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                self.env_vars.add(node.args[0].value)
        if isinstance(node.func, ast.Attribute) and node.func.attr == 'get_module':
            if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                self.module_refs.add(node.args[0].value)
        if isinstance(node.func, ast.Attribute) and node.func.attr == 'get_context':
            if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                self.context_refs.add(node.args[0].value)
        self.generic_visit(node)

    def _collect_target(self, target: ast.AST) -> None:
        if isinstance(target, ast.Name):
            self.local_vars.add(target.id)
        elif isinstance(target, (ast.Tuple, ast.List)):
            for elt in target.elts:
                self._collect_target(elt)


def function_signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    args = []
    posonly = list(node.args.posonlyargs)
    regular = list(node.args.args)
    defaults = [None] * (len(posonly) + len(regular) - len(node.args.defaults)) + list(node.args.defaults)
    merged = list(zip(posonly + regular, defaults))
    for idx, (arg, default) in enumerate(merged):
        label = arg.arg
        if default is not None:
            label += f"={_src(default)}"
        args.append(label)
        if posonly and idx == len(posonly) - 1:
            args.append('/')
    if node.args.vararg:
        args.append('*' + node.args.vararg.arg)
    elif node.args.kwonlyargs:
        args.append('*')
    for arg, default in zip(node.args.kwonlyargs, node.args.kw_defaults):
        label = arg.arg
        if default is not None:
            label += f"={_src(default)}"
        args.append(label)
    if node.args.kwarg:
        args.append('**' + node.args.kwarg.arg)
    return f"({', '.join(args)})"


def analyze_class(path: Path, class_node: ast.ClassDef, source: str) -> dict[str, Any]:
    module_name_attr = None
    for stmt in class_node.body:
        if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name) and stmt.target.id == 'name':
            if isinstance(stmt.value, ast.Constant) and isinstance(stmt.value.value, str):
                module_name_attr = stmt.value.value
        elif isinstance(stmt, ast.Assign):
            for target in stmt.targets:
                if isinstance(target, ast.Name) and target.id == 'name' and isinstance(stmt.value, ast.Constant) and isinstance(stmt.value.value, str):
                    module_name_attr = stmt.value.value

    methods = []
    class_env_vars: set[str] = set()
    class_module_refs: set[str] = set()
    class_context_refs: set[str] = set()
    lifecycle = []
    for stmt in class_node.body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            analyzer = FunctionAnalyzer()
            analyzer.visit(stmt)
            method_info = {
                'name': stmt.name,
                'async': isinstance(stmt, ast.AsyncFunctionDef),
                'signature': function_signature(stmt),
                'line': stmt.lineno,
                'end_line': getattr(stmt, 'end_lineno', stmt.lineno),
                'local_vars': sorted(analyzer.local_vars),
                'env_vars': sorted(analyzer.env_vars),
                'module_refs': sorted(analyzer.module_refs),
                'context_refs': sorted(analyzer.context_refs),
                'calls': sorted(set(analyzer.calls)),
                'block_counts': dict(sorted(analyzer.block_counts.items())),
                'return_dict_keys': analyzer.return_dict_keys,
            }
            methods.append(method_info)
            class_env_vars.update(analyzer.env_vars)
            class_module_refs.update(analyzer.module_refs)
            class_context_refs.update(analyzer.context_refs)
            if stmt.name in LIFECYCLE_METHODS:
                lifecycle.append(stmt.name)

    return {
        'class_name': class_node.name,
        'module_name': module_name_attr,
        'file': str(path.relative_to(ROOT)),
        'line': class_node.lineno,
        'end_line': getattr(class_node, 'end_lineno', class_node.lineno),
        'lifecycle_methods': sorted(lifecycle),
        'env_vars': sorted(class_env_vars),
        'module_refs': sorted(class_module_refs),
        'context_refs': sorted(class_context_refs),
        'method_count': len(methods),
        'methods': methods,
    }


def parse_python_file(path: Path) -> dict[str, Any]:
    source = path.read_text(encoding='utf-8')
    tree = ast.parse(source)
    classes = []
    file_env_vars: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            info = analyze_class(path, node, source)
            classes.append(info)
            file_env_vars.update(info['env_vars'])
    return {
        'file': str(path.relative_to(ROOT)),
        'classes': classes,
        'env_vars': sorted(file_env_vars),
    }


def extract_registered_and_loaded_modules() -> dict[str, list[str]]:
    source = ORCHESTRATOR_PATH.read_text(encoding='utf-8')
    tree = ast.parse(source)
    registered = []
    loaded = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr == 'register' and isinstance(node.func.value, ast.Attribute) and node.func.value.attr == 'module_manager':
                if node.args:
                    registered.append(_src(node.args[0]))
            if node.func.attr == 'load' and isinstance(node.func.value, ast.Attribute) and node.func.value.attr == 'module_manager':
                if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                    loaded.append(node.args[0].value)
    return {'registered': registered, 'loaded': loaded}


def extract_contracts() -> dict[str, Any]:
    module = importlib.import_module('core.core.diagnostic_contracts')
    contracts = module.list_diagnostic_contracts()
    matrix = module.diagnostic_matrix()
    return {'contracts': contracts, 'matrix': matrix}


def build_interaction_edges(class_entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    edges = []
    for entry in class_entries:
        owner = entry.get('module_name') or entry['class_name']
        for method in entry['methods']:
            for ref in method['module_refs']:
                edges.append({
                    'from_module': owner,
                    'from_class': entry['class_name'],
                    'from_method': method['name'],
                    'type': 'get_module',
                    'target': ref,
                })
            for ref in method['context_refs']:
                edges.append({
                    'from_module': owner,
                    'from_class': entry['class_name'],
                    'from_method': method['name'],
                    'type': 'get_context',
                    'target': ref,
                })
    return edges


def collect_inventory() -> dict[str, Any]:
    files = sorted(CORE_DIR.glob('*.py'))
    parsed = [parse_python_file(path) for path in files]
    class_entries = [cls for item in parsed for cls in item['classes']]
    module_entries = [entry for entry in class_entries if entry.get('module_name')]
    registered_loaded = extract_registered_and_loaded_modules()
    contracts = extract_contracts()
    edges = build_interaction_edges(class_entries)
    env_usage = defaultdict(list)
    for entry in class_entries:
        owner = entry.get('module_name') or entry['class_name']
        for method in entry['methods']:
            for env in method['env_vars']:
                env_usage[env].append(f"{owner}.{method['name']}")

    return {
        'root': str(ROOT),
        'scope': {
            'core_dir': str(CORE_DIR.relative_to(ROOT)),
            'orchestrator': str(ORCHESTRATOR_PATH.relative_to(ROOT)),
            'self_diagnostic_module': str(SELF_DIAG_PATH.relative_to(ROOT)),
            'diagnostic_contracts': str(DIAGNOSTIC_CONTRACTS_PATH.relative_to(ROOT)),
        },
        'kernel_modules': {
            'registered': registered_loaded['registered'],
            'loaded': registered_loaded['loaded'],
            'registered_count': len(registered_loaded['registered']),
            'loaded_count': len(registered_loaded['loaded']),
        },
        'class_inventory': class_entries,
        'kernel_module_inventory': module_entries,
        'interaction_edges': edges,
        'env_usage': {key: sorted(value) for key, value in sorted(env_usage.items())},
        'diagnostic_contracts': contracts,
    }


def build_markdown(report: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append('# Kernel Diagnostic Inventory')
    lines.append('')
    km = report['kernel_modules']
    lines.append(f"Registered modules: {km['registered_count']}")
    lines.append(f"Loaded modules at boot path: {km['loaded_count']}")
    lines.append('')
    lines.append('## Registered Modules')
    for item in km['registered']:
        lines.append(f'- `{item}`')
    lines.append('')
    lines.append('## Loaded Modules')
    for item in km['loaded']:
        lines.append(f'- `{item}`')
    lines.append('')
    lines.append('## Diagnostic Layers')
    for contract in report['diagnostic_contracts']['contracts']:
        lines.append(f"- `{contract['layer']}`: {contract['metadata']['summary']}")
    lines.append('')
    lines.append('## Kernel Module Inventory')
    for module in sorted(report['kernel_module_inventory'], key=lambda x: (x.get('module_name') or '', x['class_name'])):
        owner = module.get('module_name') or module['class_name']
        lines.append(f"### {owner}")
        lines.append(f"- Class: `{module['class_name']}`")
        lines.append(f"- File: `{module['file']}`:{module['line']}")
        lines.append(f"- Lifecycle: {', '.join(module['lifecycle_methods']) or 'none'}")
        lines.append(f"- Methods: {module['method_count']}")
        if module['env_vars']:
            lines.append(f"- Env vars: {', '.join(f'`{item}`' for item in module['env_vars'])}")
        if module['module_refs']:
            lines.append(f"- get_module refs: {', '.join(f'`{item}`' for item in module['module_refs'])}")
        if module['context_refs']:
            lines.append(f"- get_context refs: {', '.join(f'`{item}`' for item in module['context_refs'])}")
        lines.append('- Methods detail:')
        for method in module['methods']:
            lines.append(f"  - `{method['name']}{method['signature']}` lines {method['line']}-{method['end_line']}")
            if method['local_vars']:
                lines.append(f"    locals: {', '.join(f'`{item}`' for item in method['local_vars'])}")
            if method['return_dict_keys']:
                keys = sorted({key for group in method['return_dict_keys'] for key in group})
                lines.append(f"    return keys: {', '.join(f'`{item}`' for item in keys)}")
        lines.append('')
    lines.append('## Interaction Edges')
    for edge in report['interaction_edges']:
        lines.append(f"- `{edge['from_module']}.{edge['from_method']}` -> {edge['type']}(`{edge['target']}`)")
    lines.append('')
    lines.append('## Env Usage')
    for env, users in report['env_usage'].items():
        lines.append(f"- `{env}`: {', '.join(f'`{item}`' for item in users)}")
    return '\n'.join(lines) + '\n'


def main() -> None:
    REPORT_JSON.parent.mkdir(parents=True, exist_ok=True)
    report = collect_inventory()
    REPORT_JSON.write_text(json.dumps(report, ensure_ascii=True, indent=2) + '\n', encoding='utf-8')
    REPORT_MD.write_text(build_markdown(report), encoding='utf-8')
    print(json.dumps({
        'json_report': str(REPORT_JSON),
        'md_report': str(REPORT_MD),
        'registered_modules': report['kernel_modules']['registered_count'],
        'loaded_modules': report['kernel_modules']['loaded_count'],
        'class_count': len(report['class_inventory']),
        'kernel_module_count': len(report['kernel_module_inventory']),
        'interaction_edges': len(report['interaction_edges']),
        'env_vars': len(report['env_usage']),
        'diagnostic_layers': len(report['diagnostic_contracts']['contracts']),
    }, ensure_ascii=True))


if __name__ == '__main__':
    main()
