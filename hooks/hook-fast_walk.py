from pathlib import Path

# __init__.py has to sit next to the .pyd.
module_collection_mode = {
    "fast_walk": "py",
}


hiddenimports = ["fast_walk"]

binaries = []
datas = []


try:
    import fast_walk as module

    shim = Path(module.__file__).resolve()
    extension = getattr(module, "EXTENSION_PATH", None)
    if not extension:
        matches = sorted(shim.parent.glob("fast_walk*.pyd"))
        extension = matches[0] if matches else None
except ImportError:
    extension = Path("fast_walk/target/release/fast_walk.dll").resolve()
    shim = Path("fast_walk/__init__.py").resolve()

if extension and Path(extension).is_file():
    binaries.append((str(extension), "fast_walk"))
if shim.is_file():
    datas.append((str(shim), "fast_walk"))
