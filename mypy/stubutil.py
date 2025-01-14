"""Utilities for mypy.stubgen, mypy.stubgenc, and mypy.stubdoc modules."""

import sys
import os.path
import inspect
import json
import pkgutil
import importlib
import subprocess
import re
from types import ModuleType
from contextlib import contextmanager

from typing import Optional, Tuple, List, Iterator, Union
from typing_extensions import overload


# Modules that may fail when imported, or that may have side effects.
NOT_IMPORTABLE_MODULES = {
    'tensorflow.tools.pip_package.setup',
}


class CantImport(Exception):
    def __init__(self, module: str, message: str):
        self.module = module
        self.message = message


def is_c_module(module: ModuleType) -> bool:
    if module.__dict__.get('__file__') is None:
        # Could be a namespace package. These must be handled through
        # introspection, since there is no source file.
        return True
    return os.path.splitext(module.__dict__['__file__'])[-1] in ['.so', '.pyd']


def default_py2_interpreter() -> str:
    """Find a system Python 2 interpreter.

    Return full path or exit if failed.
    """
    # TODO: Make this do something reasonable in Windows.
    for candidate in ('/usr/bin/python2', '/usr/bin/python'):
        if not os.path.exists(candidate):
            continue
        output = subprocess.check_output([candidate, '--version'],
                                         stderr=subprocess.STDOUT).strip()
        if b'Python 2' in output:
            return candidate
    raise SystemExit("Can't find a Python 2 interpreter -- "
                     "please use the --python-executable option")


def walk_packages(packages: List[str], verbose: bool = False) -> Iterator[str]:
    """Iterates through all packages and sub-packages in the given list.

    This uses runtime imports to find both Python and C modules. For Python packages
    we simply pass the __path__ attribute to pkgutil.walk_packages() to get the content
    of the package (all subpackages and modules).  However, packages in C extensions
    do not have this attribute, so we have to roll out our own logic: recursively find
    all modules imported in the package that have matching names.
    """
    for package_name in packages:
        if package_name in NOT_IMPORTABLE_MODULES:
            print('%s: Skipped (blacklisted)' % package_name)
            continue
        if verbose:
            print('Trying to import %r for runtime introspection' % package_name)
        try:
            package = importlib.import_module(package_name)
        except Exception:
            report_missing(package_name)
            continue
        yield package.__name__
        # get the path of the object (needed by pkgutil)
        path = getattr(package, '__path__', None)
        if path is None:
            # Object has no path; this means it's either a module inside a package
            # (and thus no sub-packages), or it could be a C extension package.
            if is_c_module(package):
                # This is a C extension module, now get the list of all sub-packages
                # using the inspect module
                subpackages = [package.__name__ + "." + name
                               for name, val in inspect.getmembers(package)
                               if inspect.ismodule(val)
                               and val.__name__ == package.__name__ + "." + name]
                # Recursively iterate through the subpackages
                for submodule in walk_packages(subpackages, verbose):
                    yield submodule
            # It's a module inside a package.  There's nothing else to walk/yield.
        else:
            all_packages = pkgutil.walk_packages(path, prefix=package.__name__ + ".",
                                                 onerror=lambda r: None)
            for importer, qualified_name, ispkg in all_packages:
                yield qualified_name


def find_module_path_and_all_py2(module: str,
                                 interpreter: str) -> Optional[Tuple[str,
                                                                     Optional[List[str]]]]:
    """Return tuple (module path, module __all__) for a Python 2 module.

    The path refers to the .py/.py[co] file. The second tuple item is
    None if the module doesn't define __all__.

    Raise CantImport if the module can't be imported, or exit if it's a C extension module.
    """
    cmd_template = '{interpreter} -c "%s"'.format(interpreter=interpreter)
    code = ("import importlib, json; mod = importlib.import_module('%s'); "
            "print(mod.__file__); print(json.dumps(getattr(mod, '__all__', None)))") % module
    try:
        output_bytes = subprocess.check_output(cmd_template % code, shell=True)
    except subprocess.CalledProcessError as e:
        raise CantImport(module, str(e))
    output = output_bytes.decode('ascii').strip().splitlines()
    module_path = output[0]
    if not module_path.endswith(('.py', '.pyc', '.pyo')):
        raise SystemExit('%s looks like a C module; they are not supported for Python 2' %
                         module)
    if module_path.endswith(('.pyc', '.pyo')):
        module_path = module_path[:-1]
    module_all = json.loads(output[1])
    return module_path, module_all


def find_module_path_and_all_py3(module: str,
                                 verbose: bool) -> Optional[Tuple[str, Optional[List[str]]]]:
    """Find module and determine __all__ for a Python 3 module.

    Return None if the module is a C module. Return (module_path, __all__) if
    it is a Python module. Raise CantImport if import failed.
    """
    if module in NOT_IMPORTABLE_MODULES:
        raise CantImport(module, '')

    # TODO: Support custom interpreters.
    if verbose:
        print('Trying to import %r for runtime introspection' % module)
    try:
        mod = importlib.import_module(module)
    except Exception as e:
        raise CantImport(module, str(e))
    if is_c_module(mod):
        return None
    module_all = getattr(mod, '__all__', None)
    if module_all is not None:
        module_all = list(module_all)
    return mod.__file__, module_all


@contextmanager
def generate_guarded(mod: str, target: str,
                     ignore_errors: bool = True, verbose: bool = False) -> Iterator[None]:
    """Ignore or report errors during stub generation.

    Optionally report success.
    """
    if verbose:
        print('Processing %s' % mod)
    try:
        yield
    except Exception as e:
        if not ignore_errors:
            raise e
        else:
            # --ignore-errors was passed
            print("Stub generation failed for", mod, file=sys.stderr)
    else:
        if verbose:
            print('Created %s' % target)


PY2_MODULES = {'cStringIO', 'urlparse', 'collections.UserDict'}


def report_missing(mod: str, message: Optional[str] = '', traceback: str = '') -> None:
    if message:
        message = ' with error: ' + message
    print('{}: Failed to import, skipping{}'.format(mod, message))
    m = re.search(r"ModuleNotFoundError: No module named '([^']*)'", traceback)
    if m:
        missing_module = m.group(1)
        if missing_module in PY2_MODULES:
            print('note: Try --py2 for Python 2 mode')


def fail_missing(mod: str) -> None:
    raise SystemExit("Can't find module '{}' (consider using --search-path)".format(mod))


@overload
def remove_misplaced_type_comments(source: bytes) -> bytes: ...


@overload
def remove_misplaced_type_comments(source: str) -> str: ...


def remove_misplaced_type_comments(source: Union[str, bytes]) -> Union[str, bytes]:
    """Remove comments from source that could be understood as misplaced type comments.

    Normal comments may look like misplaced type comments, and since they cause blocking
    parse errors, we want to avoid them.
    """
    if isinstance(source, bytes):
        # This gives us a 1-1 character code mapping, so it's roundtrippable.
        text = source.decode('latin1')
    else:
        text = source

    # Remove something that looks like a variable type comment but that's by itself
    # on a line, as it will often generate a parse error (unless it's # type: ignore).
    text = re.sub(r'^[ \t]*# +type: +["\'a-zA-Z_].*$', '', text, flags=re.MULTILINE)

    # Remove something that looks like a function type comment after docstring,
    # which will result in a parse error.
    text = re.sub(r'""" *\n[ \t\n]*# +type: +\(.*$', '"""\n', text, flags=re.MULTILINE)
    text = re.sub(r"''' *\n[ \t\n]*# +type: +\(.*$", "'''\n", text, flags=re.MULTILINE)

    # Remove something that looks like a badly formed function type comment.
    text = re.sub(r'^[ \t]*# +type: +\([^()]+(\)[ \t]*)?$', '', text, flags=re.MULTILINE)

    if isinstance(source, bytes):
        return text.encode('latin1')
    else:
        return text


def common_dir_prefix(paths: List[str]) -> str:
    if not paths:
        return '.'
    cur = os.path.dirname(paths[0])
    for path in paths[1:]:
        while True:
            path = os.path.dirname(path)
            if (cur + '/').startswith(path + '/'):
                cur = path
                break
    return cur or '.'
