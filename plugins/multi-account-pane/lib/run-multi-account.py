"""Runs the multi-account plugin's own command line (its lib/ma.py) with the arguments the pane passes.

The pane holds no account logic of its own: each refresh and each button runs this script, which finds the
multi-account plugin installed beside the pane and runs it. It looks in two places only, both next to this plugin:

    <pane>/../multi-account/lib/ma.py               the repository layout, plugins/multi-account beside this one
    <pane>/../../multi-account/<version>/lib/ma.py  Claude Code's plugin cache, the newest version (1.0.10 after 1.0.9)
"""
import json
import os
import runpy
import sys

PANE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def version(name):
    try:
        return tuple(int(x) for x in name.split("."))
    except ValueError:
        return (-1,)


def find():
    beside = os.path.join(os.path.dirname(PANE), "multi-account", "lib", "ma.py")
    if os.path.isfile(beside):
        return beside
    cache = os.path.join(os.path.dirname(os.path.dirname(PANE)), "multi-account")
    try:
        names = sorted(os.listdir(cache), key=version, reverse=True)
    except OSError:
        names = []
    for name in names:
        path = os.path.join(cache, name, "lib", "ma.py")
        if os.path.isfile(path):
            return path
    return None


path = find()
if not path:
    print(json.dumps({"error": "the multi-account plugin is not installed"}))
    sys.exit(3)
sys.argv = [path] + sys.argv[1:]
sys.path.insert(0, os.path.dirname(path))
runpy.run_path(path, run_name="__main__")
