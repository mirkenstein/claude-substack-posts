# ChromaDB Python 3.14 Patch

ChromaDB 1.5.x uses pydantic v1 which is incompatible with Python 3.14.
This affects both the local `.venv` and the `chroma-mcp` MCP server (which
runs in a uv-cached environment).

## The Error

```
pydantic.v1.errors.ConfigError: unable to infer type for attribute "chroma_server_nofile"
```

## Finding the config.py to patch

The file to patch is always `chromadb/config.py` inside the environment.

**Local venv:**
```bash
find .venv -name config.py -path '*/chromadb/*' 2>/dev/null
```

**chroma-mcp (uv cached):**
Check the error traceback for the path, e.g.:
```
~/.cache/uv/archive-v0/<hash>/lib64/python3.14/site-packages/chromadb/config.py
```

## The 4 Patches

### 1. Replace BaseSettings import (top of file, ~line 15-24)

**Before:**
```python
in_pydantic_v2 = False
try:
    from pydantic import BaseSettings
except ImportError:
    in_pydantic_v2 = True
    from pydantic.v1 import BaseSettings
    from pydantic.v1 import validator

if not in_pydantic_v2:
    from pydantic import validator  # type: ignore # noqa
```

**After:**
```python
in_pydantic_v2 = False
try:
    from pydantic_settings import BaseSettings
    in_pydantic_v2 = True
except ImportError:
    try:
        from pydantic import BaseSettings
    except ImportError:
        in_pydantic_v2 = True
        from pydantic.v1 import BaseSettings

if in_pydantic_v2:
    from pydantic import validator  # type: ignore # noqa
else:
    from pydantic import validator  # type: ignore # noqa
```

### 2. Move `chroma_server_nofile` BEFORE its @validator (~line 130)

**Before:**
```python
    @validator("chroma_server_nofile", pre=True, always=True, allow_reuse=True)
    def empty_str_to_none(cls, v: str) -> Optional[str]:
        ...
    chroma_server_nofile: Optional[int] = None
```

**After:**
```python
    chroma_server_nofile: Optional[int] = None

    @validator("chroma_server_nofile", pre=True, always=True, allow_reuse=True)
    def empty_str_to_none(cls, v: str) -> Optional[str]:
        ...
```

### 3. Add type annotations to 3 untyped fields

```python
# Find and replace these 3 lines:
chroma_coordinator_host = "localhost"     → chroma_coordinator_host: str = "localhost"
chroma_logservice_host = "localhost"      → chroma_logservice_host: str = "localhost"
chroma_logservice_port = 50052            → chroma_logservice_port: int = 50052
```

### 4. Add `extra = "allow"` to inner Config class

**Before:**
```python
    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
```

**After:**
```python
    class Config:
        extra = "allow"
        env_file = ".env"
        env_file_encoding = "utf-8"
```

## Also Required

`pydantic-settings` must be installed in the target environment.
For the uv cache, check if it's already there:

```bash
/path/to/uv/cache/bin/python -c "import pydantic_settings; print('OK')"
# If not:
uv pip install --python /path/to/uv/cache/bin/python pydantic-settings
```

## Verification

```bash
/path/to/python -c "import chromadb; print('OK:', chromadb.__version__)"
```

## Note

This patch is fragile — any `uv cache clean` or chroma-mcp update will
wipe it. The upstream fix is tracked at https://github.com/chroma-core/chroma/issues/5996
