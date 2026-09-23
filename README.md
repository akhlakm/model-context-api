# MCA

Reusable Model Context API routers for Django Ninja and Pydantic applications.

Install optional integrations only when needed:

```bash
pip install model-context-api
pip install "model-context-api[ninja]"
pip install "model-context-api[mcp]"
```

Import adapter classes from `mca.pydantic`, `mca.ninja`, and `mca.mcp`; import
discovery models from `mca.models`. Routers receive a filesystem path to their
Markdown guides through `guides_dir` and optionally accept `title` and `version`
for discovery metadata. Route decorators can also receive `guides=[...]` to list
relevant guide names in operation discovery.
