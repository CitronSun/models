class PromptRuntime:
    def __init__(self, local_store: LocalPromptStore, zip_store: PromptZipStore):
        self.local_store = local_store
        self.zip_store = zip_store

        self._lock = threading.Lock()
        self._active_registry_by_proj: dict[str, PromptRegistry] = {}
        self._active_info_by_proj: dict[str, dict] = {}

    # -----------------------------
    # Mode logic (env + API override)
    # -----------------------------
    def get_effective_mode(self) -> str:
        cfg = self.local_store.read_config()
        override = cfg.get("mode_override")
        if override in ("local", "remote"):
            return override
        return PROMPT_MODE_ENV if PROMPT_MODE_ENV in ("local", "remote") else "remote"

    # -----------------------------
    # helpers: per-proj config access
    # -----------------------------
    def _get_proj_cfg(self, cfg: dict, proj: str) -> dict:
        projects = cfg.setdefault("projects", {})
        return projects.setdefault(proj, {})

    def _set_active_registry(self, proj: str, prompt_data: dict, source: str, extra: dict):
        reg = PromptRegistry(prompt_data)
        with self._lock:
            self._active_registry_by_proj[proj] = reg
            self._active_info_by_proj[proj] = {"source": source, "updated_at": _now_utc_iso(), **extra}

    # -----------------------------
    # Init per proj
    # -----------------------------
    def initialize_proj(self, proj: str):
        """
        Initialize ONE project. No default proj is assumed.
        Should be called at startup for each known proj, or via admin.
        """
        if not proj:
            raise ValueError("proj is required")

        mode = self.get_effective_mode()
        cfg = self.local_store.read_config()
        pcfg = self._get_proj_cfg(cfg, proj)

        if mode == "remote":
            self.zip_store.refresh_if_needed()

            active_ver = pcfg.get("active_ver")
            if isinstance(active_ver, str) and active_ver.startswith("v"):
                try:
                    bundle = self.zip_store.get_by_proj_ver(proj, active_ver)
                    self._set_active_registry(
                        proj,
                        bundle.data,
                        source="remote-ver",
                        extra={"zip_id": bundle.zip_id, "proj": proj, "prompt_ver": bundle.ver, "locked": bool(pcfg.get("locked", False))}
                    )
                    return
                except Exception as e:
                    print(f"[runtime] {proj} config active_ver load failed: {e}")

            # fallback to remote latest for this proj
            try:
                bundle = self.zip_store.get_latest(proj)
                self._set_active_registry(
                    proj,
                    bundle.data,
                    source="remote-latest",
                    extra={"zip_id": bundle.zip_id, "proj": proj, "prompt_ver": bundle.ver, "locked": bool(pcfg.get("locked", False))}
                )
                pcfg["active_ver"] = bundle.ver
                pcfg.setdefault("locked", False)
                pcfg["last_zip_id"] = bundle.zip_id
                pcfg["updated_at"] = _now_utc_iso()
                self.local_store.write_config(cfg)
                return
            except Exception as e:
                print(f"[runtime] {proj} remote latest load failed: {e}")

        # local fallback per proj（建议你本地也做成按 proj 的 default，或统一 default）
        default_data = self.local_store.load_default_prompt(proj=proj)  # 如果你不想改 LocalPromptStore，就先忽略 proj 参数
        self._set_active_registry(proj, default_data, source="default", extra={"proj": proj, "prompt_ver": None, "zip_id": None, "locked": bool(pcfg.get("locked", False))})

    # -----------------------------
    # business usage: MUST pass proj
    # -----------------------------
    def get_registry(self, proj: str) -> PromptRegistry:
        if not proj:
            raise ValueError("proj is required")

        with self._lock:
            reg = self._active_registry_by_proj.get(proj)
        if reg is None:
            raise RuntimeError(f"Prompt registry for proj={proj} not initialized")
        return reg

    def status(self) -> dict:
        cfg = self.local_store.read_config()
        with self._lock:
            info = {k: dict(v) for k, v in self._active_info_by_proj.items()}
        return {
            "effective_mode": self.get_effective_mode(),
            "mode_override": cfg.get("mode_override"),
            "env_mode": PROMPT_MODE_ENV,
            "projects_config": cfg.get("projects", {}),
            "active_by_proj": info,
        }

    # -----------------------------
    # Admin: switch/unlock per proj
    # -----------------------------
    def switch_to_version(self, proj: str, ver: str, lock: bool = True):
        if not proj:
            raise ValueError("proj is required")
        if not ver:
            raise ValueError("ver is required")

        self.zip_store.refresh_if_needed()
        bundle = self.zip_store.get_by_proj_ver(proj, ver)

        self._set_active_registry(
            proj,
            bundle.data,
            source="admin-switch",
            extra={"zip_id": bundle.zip_id, "proj": proj, "prompt_ver": bundle.ver, "locked": bool(lock)}
        )

        cfg = self.local_store.read_config()
        pcfg = self._get_proj_cfg(cfg, proj)
        pcfg["active_ver"] = ver
        pcfg["locked"] = bool(lock)
        pcfg["last_zip_id"] = bundle.zip_id
        pcfg["updated_at"] = _now_utc_iso()
        self.local_store.write_config(cfg)

    def unlock_follow_latest(self, proj: str):
        if not proj:
            raise ValueError("proj is required")

        cfg = self.local_store.read_config()
        pcfg = self._get_proj_cfg(cfg, proj)
        pcfg["locked"] = False
        self.local_store.write_config(cfg)

        mode = self.get_effective_mode()
        if mode == "remote":
            self.zip_store.refresh_if_needed()
            try:
                bundle = self.zip_store.get_latest(proj)
                self._set_active_registry(
                    proj,
                    bundle.data,
                    source="unlock-latest",
                    extra={"zip_id": bundle.zip_id, "proj": proj, "prompt_ver": bundle.ver, "locked": False}
                )
                cfg = self.local_store.read_config()
                pcfg = self._get_proj_cfg(cfg, proj)
                pcfg["active_ver"] = bundle.ver
                pcfg["last_zip_id"] = bundle.zip_id
                pcfg["updated_at"] = _now_utc_iso()
                self.local_store.write_config(cfg)
                return
            except Exception as e:
                print(f"[runtime] {proj} unlock remote latest failed: {e}")

        default_data = self.local_store.load_default_prompt(proj=proj)
        self._set_active_registry(proj, default_data, source="default", extra={"zip_id": None, "proj": proj, "prompt_ver": None, "locked": False})

    # -----------------------------
    # Background refresh (scheduler)
    # -----------------------------
    def refresh_background(self):
        mode = self.get_effective_mode()
        if mode != "remote":
            return

        cfg = self.local_store.read_config()
        changed = self.zip_store.refresh_if_needed()
        if not changed:
            return

        # zip changed; for each proj, update if NOT locked
        for proj in self.zip_store.list_projects():
            pcfg = self._get_proj_cfg(cfg, proj)
            if bool(pcfg.get("locked", False)):
                continue
            try:
                bundle = self.zip_store.get_latest(proj)
                self._set_active_registry(
                    proj,
                    bundle.data,
                    source="bg-remote-latest",
                    extra={"zip_id": bundle.zip_id, "proj": proj, "prompt_ver": bundle.ver, "locked": False}
                )
                pcfg["active_ver"] = bundle.ver
                pcfg["last_zip_id"] = bundle.zip_id
                pcfg["updated_at"] = _now_utc_iso()
            except Exception as e:
                print(f"[runtime] bg refresh {proj} latest failed: {e}")

        self.local_store.write_config(cfg)




import json
import re
import threading
import time
from dataclasses import dataclass
from io import BytesIO
from typing import Dict, List, Optional, Tuple

from zipfile import ZipFile

from packaging.version import Version  # pip install packaging


# -----------------------------
# Zip path matchers
# -----------------------------
# matches:
# prompts/projects/proj1/v1.2.3/prompts.yml
# prompts/projects/anything/v10.0.1/prompts.yml
PROMPT_RE = re.compile(
    r"^prompts/projects/(?P<proj>[^/]+)/(?P<ver>v\d+\.\d+\.\d+)/prompts\.yml$"
)

# optional:
# pin.json or prompts/pin.json etc. adapt if your real file name differs
PIN_JSON_RE = re.compile(r"(^|.*/)(pin\.json)$")


# -----------------------------
# Bundle object returned to runtime
# -----------------------------
@dataclass(frozen=True)
class PromptBundle:
    zip_id: str
    proj: str
    ver: str
    data: dict


# -----------------------------
# PromptZipStore
# -----------------------------
class PromptZipStore:
    """
    Holds current latest zip bytes in memory and provides:
    - refresh_if_needed(): check latest zip id; download if changed (throttled)
    - available_projects(): list all projects found in zip
    - available_versions(proj): list all semantic versions for proj
    - get_by_proj_ver(proj, ver): parse and return prompts.yml (YAML -> dict)
    - get_latest(proj): parse latest semantic version prompts.yml for proj
    - optional: read pin.json inside zip (read-only)
    """

    def __init__(self, client, check_interval_s: int, yaml_loader):
        """
        client: ArtifactoryClient-like object:
            - find_latest_zip() -> (zip_name, zip_url)
            - download_zip(zip_url) -> bytes
        yaml_loader:
            - callable (text: str) -> dict
            Example: ruamel.yaml.YAML(typ="safe").load
        """
        self.client = client
        self.check_interval_s = int(check_interval_s)
        self._yaml_load = yaml_loader

        self._lock = threading.Lock()
        self._next_check_at = 0.0

        self._zip_id: Optional[str] = None
        self._zip_url: Optional[str] = None
        self._zip_bytes: Optional[bytes] = None

        # proj -> ver -> path_in_zip
        self._index: Dict[str, Dict[str, str]] = {}

        # (proj, ver) -> parsed YAML dict
        self._cache: Dict[Tuple[str, str], dict] = {}

        # optional metadata from pin.json in zip
        self._pin_json: Optional[dict] = None

    def current_zip_id(self) -> Optional[str]:
        with self._lock:
            return self._zip_id

    def refresh_if_needed(self) -> bool:
        """
        Returns True if refreshed (zip changed and downloaded), else False.
        This call is throttled by check_interval_s (to protect Artifactory).
        """
        now = time.time()
        with self._lock:
            if now < self._next_check_at:
                return False
            self._next_check_at = now + self.check_interval_s

        try:
            zip_name, zip_url = self.client.find_latest_zip()

            with self._lock:
                if self._zip_id == zip_name and self._zip_bytes is not None:
                    return False

            zip_bytes = self.client.download_zip(zip_url)

            # Build index in a local scope first (so we can atomically swap)
            idx: Dict[str, Dict[str, str]] = {}
            pin_obj: Optional[dict] = None

            with ZipFile(BytesIO(zip_bytes)) as zf:
                for name in zf.namelist():
                    m = PROMPT_RE.match(name)
                    if m:
                        proj = m.group("proj")
                        ver = m.group("ver")
                        idx.setdefault(proj, {})[ver] = name
                        continue

                    if PIN_JSON_RE.match(name):
                        try:
                            with zf.open(name) as f:
                                pin_obj = json.loads(f.read().decode("utf-8"))
                        except Exception:
                            pin_obj = None

            if not idx:
                raise RuntimeError("No prompts found: expected prompts/projects/<proj>/vX.Y.Z/prompts.yml inside zip")

            with self._lock:
                self._zip_id = zip_name
                self._zip_url = zip_url
                self._zip_bytes = zip_bytes
                self._index = idx
                self._cache = {}  # clear parsed cache after zip changes
                self._pin_json = pin_obj

            return True

        except Exception as e:
            # Do not break service; keep previous zip if any
            print(f"[zip-store] refresh failed: {e}")
            return False

    # -----------------------------
    # Discovery helpers
    # -----------------------------
    def available_projects(self) -> List[str]:
        with self._lock:
            return sorted(self._index.keys())

    def available_versions(self, proj: str) -> List[str]:
        if not proj:
            raise ValueError("proj is required")

        with self._lock:
            vers = list(self._index.get(proj, {}).keys())

        # semantic sort
        return sorted(vers, key=lambda v: Version(v.lstrip("v")))

    def get_latest_version(self, proj: str) -> str:
        if not proj:
            raise ValueError("proj is required")

        with self._lock:
            vers = list(self._index.get(proj, {}).keys())

        if not vers:
            raise KeyError(f"No versions found for proj={proj}")
        return max(vers, key=lambda v: Version(v.lstrip("v")))

    # -----------------------------
    # Get prompt data
    # -----------------------------
    def get_by_proj_ver(self, proj: str, ver: str) -> PromptBundle:
        """
        Parse and return the prompts.yml for a given (proj, ver).
        """
        if not proj:
            raise ValueError("proj is required")
        if not ver:
            raise ValueError("ver is required")

        cache_key = (proj, ver)

        with self._lock:
            if self._zip_id is None or self._zip_bytes is None:
                raise RuntimeError("Zip not loaded yet (call refresh_if_needed or initialize first)")

            if cache_key in self._cache:
                return PromptBundle(zip_id=self._zip_id, proj=proj, ver=ver, data=self._cache[cache_key])

            path = self._index.get(proj, {}).get(ver)
            if not path:
                raise KeyError(f"Prompt proj={proj} ver={ver} not found in current zip {self._zip_id}")

            zip_id = self._zip_id
            zip_bytes = self._zip_bytes

        # parse outside lock for better concurrency
        with ZipFile(BytesIO(zip_bytes)) as zf:
            with zf.open(path) as f:
                text = f.read().decode("utf-8")
                data = self._yaml_load(text)

        if not isinstance(data, dict):
            raise ValueError("Prompt YAML must be a mapping/dict")

        with self._lock:
            self._cache[cache_key] = data

        return PromptBundle(zip_id=zip_id, proj=proj, ver=ver, data=data)

    def get_latest(self, proj: str) -> PromptBundle:
        """
        Return the latest prompt for a given proj by semantic version.
        """
        latest_ver = self.get_latest_version(proj)
        return self.get_by_proj_ver(proj, latest_ver)

    # -----------------------------
    # Optional pin.json
    # -----------------------------
    def get_pin_json(self) -> Optional[dict]:
        """
        Optional helper if you embed pin.json inside zip.
        Read-only (cannot write to artifactory in your constraint).
        """
        with self._lock:
            return self._pin_json



    # -----------------------------
    # Mode override controls
    # -----------------------------
    def _list_zip_projects_safe(self) -> list[str]:
        """
        Best-effort get project list from zip_store without breaking service.
        Supports both .list_projects() and .available_projects().
        """
        try:
            if hasattr(self.zip_store, "list_projects"):
                return list(self.zip_store.list_projects())
            if hasattr(self.zip_store, "available_projects"):
                return list(self.zip_store.available_projects())
        except Exception:
            pass
        return []

    def _iter_known_projects(self, cfg: dict) -> list[str]:
        """
        Decide which projects to (re)initialize when mode changes.
        Sources:
          1) config.json["projects"].keys()
          2) zip_store discovered project keys (if zip available)
        """
        projs = set()

        projects_cfg = cfg.get("projects", {})
        if isinstance(projects_cfg, dict):
            for p in projects_cfg.keys():
                if p:
                    projs.add(p)

        for p in self._list_zip_projects_safe():
            if p:
                projs.add(p)

        return sorted(projs)

    def _set_local_default_for_projects(self, projects: list[str]):
        """
        Immediately set active registries to local default for each project.
        """
        for proj in projects:
            try:
                default_data = self.local_store.load_default_prompt(proj=proj)  # proj REQUIRED
                # local mode: "locked" is not meaningful; keep config value if you want
                self._set_active_registry(
                    proj,
                    default_data,
                    source="default",
                    extra={"zip_id": None, "proj": proj, "prompt_ver": None, "locked": False},
                )
            except Exception as e:
                print(f"[runtime] local default set failed proj={proj}: {e}")

    def set_mode_override(self, mode: str):
        """
        Set runtime mode override (local/remote) in config.json and apply immediately.

        Strict proj rule:
        - We DO NOT assume any default project.
        - We apply to "known projects" only (from config + zip if available).
        """
        if mode not in ("local", "remote"):
            raise ValueError("mode must be local or remote")

        cfg = self.local_store.read_config()
        cfg["mode_override"] = mode
        cfg["updated_at"] = _now_utc_iso()
        self.local_store.write_config(cfg)

        projects = self._iter_known_projects(cfg)

        if mode == "local":
            # Immediately switch known projects to local defaults
            if projects:
                self._set_local_default_for_projects(projects)
            return

        # mode == "remote"
        # Try to initialize known projects from remote (remote-ver -> remote-latest -> default fallback)
        # Note: initialize_proj() internally calls zip_store.refresh_if_needed(), which is throttled.
        for proj in projects:
            try:
                self.initialize_proj(proj)
            except Exception as e:
                print(f"[runtime] set_mode_override(remote) init failed proj={proj}: {e}")

    def clear_mode_override(self):
        """
        Clear runtime mode override in config.json and apply behavior based on env PROMPT_MODE.

        Strict proj rule:
        - Reapply only for known projects (from config + zip if available).
        """
        cfg = self.local_store.read_config()
        cfg.pop("mode_override", None)
        cfg["updated_at"] = _now_utc_iso()
        self.local_store.write_config(cfg)

        effective = self.get_effective_mode()  # now follows env (since override removed)
        projects = self._iter_known_projects(cfg)

        if effective == "local":
            if projects:
                self._set_local_default_for_projects(projects)
            return

        # effective == "remote"
        for proj in projects:
            try:
                self.initialize_proj(proj)
            except Exception as e:
                print(f"[runtime] clear_mode_override(remote) init failed proj={proj}: {e}")



def apply_mode_for_proj(self, proj: str, mode: str, ver: str | None = None, lock: bool | None = None):
    """
    Apply mode for ONE project immediately (update active registry + config).

    mode:
      - "local": use local default.yml for this proj
      - "remote": use remote prompt for this proj (ver if provided else latest)

    ver:
      - only used when mode == "remote"
      - if None => latest

    lock:
      - only meaningful when mode == "remote"
      - if None:
          - when ver is provided => default True (stable)
          - when ver is None (latest) => default False (follow latest)
    """
    if not proj:
        raise ValueError("proj is required")
    if mode not in ("local", "remote"):
        raise ValueError("mode must be 'local' or 'remote'")

    cfg = self.local_store.read_config()
    pcfg = self._get_proj_cfg(cfg, proj)

    if mode == "local":
        # local default
        default_data = self.local_store.load_default_prompt(proj=proj)
        self._set_active_registry(
            proj,
            default_data,
            source="local-default",
            extra={"zip_id": None, "proj": proj, "prompt_ver": None, "locked": False},
        )

        # Update config: in local mode, remote selection is not meaningful
        pcfg["locked"] = False
        pcfg.pop("active_ver", None)
        pcfg.pop("last_zip_id", None)
        pcfg["updated_at"] = _now_utc_iso()
        self.local_store.write_config(cfg)
        return

    # mode == "remote"
    self.zip_store.refresh_if_needed()

    if ver:
        bundle = self.zip_store.get_by_proj_ver(proj, ver)
        effective_lock = True if lock is None else bool(lock)
        source = "remote-ver"
    else:
        bundle = self.zip_store.get_latest(proj)
        effective_lock = False if lock is None else bool(lock)
        source = "remote-latest"

    self._set_active_registry(
        proj,
        bundle.data,
        source=source,
        extra={"zip_id": bundle.zip_id, "proj": proj, "prompt_ver": bundle.ver, "locked": effective_lock},
    )

    # Persist to config.json
    pcfg["active_ver"] = bundle.ver
    pcfg["locked"] = effective_lock
    pcfg["last_zip_id"] = bundle.zip_id
    pcfg["updated_at"] = _now_utc_iso()
    self.local_store.write_config(cfg)

    def clear_mode_for_proj(self, proj: str):
        """
        Clear manual selection for ONE project and re-apply system default behavior.

        After clear:
          - No active_ver pin
          - No lock
          - Active registry is rebuilt according to effective mode:
              * remote  -> latest
              * local   -> default.yml
        """
        if not proj:
            raise ValueError("proj is required")

        # 1. Clear per-proj config
        cfg = self.local_store.read_config()
        pcfg = self._get_proj_cfg(cfg, proj)

        pcfg.pop("active_ver", None)
        pcfg.pop("last_zip_id", None)
        pcfg.pop("locked", None)
        pcfg["updated_at"] = _now_utc_iso()

        self.local_store.write_config(cfg)

        # 2. Re-initialize this project according to effective mode
        try:
            self.initialize_proj(proj)
        except Exception as e:
            # initialize_proj already has fallback to local default
            print(f"[runtime] clear_mode_for_proj init failed proj={proj}: {e}")


# middleware/token_context.py
from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from context.token_context import current_token

class TokenContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        token = request.app.state.token
        token_ctx = current_token.set(token)
        try:
            return await call_next(request)
        finally:
            current_token.reset(token_ctx)

# app.py
from fastapi import FastAPI
from middleware.token_context import TokenContextMiddleware

app = FastAPI()
app.add_middleware(TokenContextMiddleware)


from dataclasses import dataclass
from typing import Any
from .enums import ProjectEnum

@dataclass(frozen=True)
class RuntimeCtx:
    runtime: Any          # 如果你有 PromptRuntime 类型，可以替换 Any
    project: ProjectEnum


from fastapi import Request, HTTPException, Query
from .enums import ProjectEnum
from .runtime_ctx import RuntimeCtx

def _get_manager(request: Request):
    mgr = getattr(request.app.state, "project_manager", None)
    if mgr is None:
        raise HTTPException(status_code=500, detail="project_manager not initialized")
    return mgr

def get_runtime_by_project(request: Request, project: ProjectEnum) -> RuntimeCtx:
    mgr = _get_manager(request)
    runtime = mgr.get(project)
    if runtime is None:
        # 如果你的 mgr.get 一定不会 None，可以删掉这一段
        raise HTTPException(status_code=404, detail=f"runtime not found for project={project}")
    return RuntimeCtx(runtime=runtime, project=project)

def make_runtime_dep(project: ProjectEnum):
    """
    工厂：固定 project，不在 Swagger 暴露 project 参数
    """
    def _dep(request: Request) -> RuntimeCtx:
        return get_runtime_by_project(request, project)
    return _dep

def get_runtime_any_project(
    request: Request,
    project: ProjectEnum = Query(..., description="Project to use"),
) -> RuntimeCtx:
    """
    通用：暴露 project 下拉框（只有需要用户选择 project 的接口才用它）
    """
    return get_runtime_by_project(request, project)


from fastapi import APIRouter, Depends
from .enums import ProjectEnum
from .runtime_ctx import RuntimeCtx
from .runtime_deps import make_runtime_dep, get_runtime_any_project

router = APIRouter()

# 固定 project：Swagger 不显示 project 参数
get_runtime_main = make_runtime_dep(ProjectEnum.main_doc)
get_runtime_s2b = make_runtime_dep(ProjectEnum.s2b_doc)

@router.get("/main/do")
def do_main(ctx: RuntimeCtx = Depends(get_runtime_main)):
    # ctx.runtime / ctx.project 更清晰
    return {"project": ctx.project, "runtime_type": str(type(ctx.runtime))}

@router.get("/s2b/do")
def do_s2b(ctx: RuntimeCtx = Depends(get_runtime_s2b)):
    return {"project": ctx.project, "runtime_type": str(type(ctx.runtime))}

# 通用：Swagger 会出现 project 下拉框
@router.get("/any/do")
def do_any(ctx: RuntimeCtx = Depends(get_runtime_any_project)):
    return {"project": ctx.project, "runtime_type": str(type(ctx.runtime))}


from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse
import os, sys, time, platform, traceback, importlib, importlib.util
from pathlib import Path
import pkgutil

app = FastAPI()

def _safe_read_text(p: Path, limit: int = 4000) -> str:
    try:
        s = p.read_text(encoding="utf-8", errors="replace")
        return s[:limit]
    except Exception as e:
        return f"<read_error: {e}>"

def _import_probe(module_name: str):
    """Try to find spec and import a module; return detailed info."""
    info = {"module": module_name}
    try:
        spec = importlib.util.find_spec(module_name)
        info["find_spec"] = {
            "found": spec is not None,
            "origin": getattr(spec, "origin", None) if spec else None,
            "submodule_search_locations": (
                list(spec.submodule_search_locations) if spec and spec.submodule_search_locations else None
            ),
        }
    except Exception as e:
        info["find_spec_error"] = repr(e)

    try:
        m = importlib.import_module(module_name)
        info["import_ok"] = True
        info["module_file"] = getattr(m, "__file__", None)
        info["module_package"] = getattr(m, "__package__", None)
        info["module_path"] = getattr(m, "__path__", None)  # for packages
        # Optional: try to read first bytes of module file for sanity
        if info["module_file"]:
            p = Path(info["module_file"])
            info["module_file_exists"] = p.exists()
            info["module_file_head"] = _safe_read_text(p, limit=800) if p.exists() else None
    except Exception:
        info["import_ok"] = False
        info["traceback"] = traceback.format_exc()

    return info

@app.get("/__debug__/import")
def debug_import(
    probe: list[str] = Query(
        default=[
            # core probes
            "pkgtest",
            "pkgtest.mod_a",
            "pkgtest.subpkg",
            "pkgtest.subpkg.mod_b",
            # optional probes for your real folder name (replace later)
            # "myfolder",
            # "myfolder.some_module",
            # also probe a "doc" package name to see if Python thinks doc is a package
            "doc",
            "doc.pkgtest",
        ]
    ),
    list_dir: str = Query(default="/app/doc"),
    list_max: int = Query(default=200),
):
    now = time.time()
    cwd = os.getcwd()

    base = Path(list_dir)
    dir_listing = {"path": str(base), "exists": base.exists(), "is_dir": base.is_dir()}
    if base.exists() and base.is_dir():
        items = []
        for i, child in enumerate(sorted(base.iterdir(), key=lambda x: x.name)):
            if i >= list_max:
                items.append({"name": "...", "type": "truncated"})
                break
            items.append({
                "name": child.name,
                "type": "dir" if child.is_dir() else "file",
                "has_init": (child / "__init__.py").exists() if child.is_dir() else None,
            })
        dir_listing["items"] = items
    else:
        dir_listing["items"] = None

    # list importable top-level modules seen in current working dir ('.')
    top_level_modules = []
    try:
        for m in pkgutil.iter_modules([cwd]):
            top_level_modules.append(m.name)
    except Exception as e:
        top_level_modules = [f"<pkgutil_error: {e}>"]

    result = {
        "time": now,
        "python": {
            "version": sys.version,
            "executable": sys.executable,
            "platform": platform.platform(),
        },
        "process": {
            "cwd": cwd,
            "argv": sys.argv,
        },
        "sys_path": sys.path,
        "dir_listing": dir_listing,
        "pkgutil_top_level_in_cwd": sorted(top_level_modules)[:400],
        "probes": [],
    }

    for name in probe:
        result["probes"].append(_import_probe(name))

    return JSONResponse(result)