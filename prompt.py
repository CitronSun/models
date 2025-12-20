"""
A production-oriented (but still simple) Prompt Manager for FastAPI.

Key features:
- Prompt YAML schema: meta + tasks + system_prompt/user_prompt
- Default local prompt (default.yml) fallback if Artifactory is down
- Remote zip is downloaded into memory; no need to persist to disk
- Background scheduler checks Artifactory periodically (no per-request network checks)
- Admin APIs:
  - list versions (human-readable time)
  - switch to a specific version (pin/lock)
  - unlock to follow latest
  - set/clear local/remote mode override via API
  - status endpoint
- Thread-safe in-memory registry (rebuild only when needed)

Limitations (by design in your current stage):
- Without Redis/PVC/config-center, changes are per-pod only in multi-replica K8s.
"""

import os
import re
import json
import time
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Dict, Optional, Tuple, List, Any

import requests
from bs4 import BeautifulSoup
from zipfile import ZipFile
from ruamel.yaml import YAML
from jinja2 import Template

from fastapi import FastAPI, Depends, HTTPException, Header
from pydantic import BaseModel


# ---------------------------------------------------------------------
# Configuration (env-driven)
# ---------------------------------------------------------------------
APP_TZ = os.getenv("APP_TZ", "Asia/Yerevan")  # for human readable time display
PROMPTS_DIR = Path(os.getenv("PROMPTS_DIR", "./prompts"))
DEFAULT_PROMPT_PATH = Path(os.getenv("DEFAULT_PROMPT_PATH", str(PROMPTS_DIR / "default.yml")))
CONFIG_PATH = Path(os.getenv("PROMPT_CONFIG_PATH", str(PROMPTS_DIR / "config.json")))

# "remote" = use Artifactory; "local" = skip remote fetch
PROMPT_MODE_ENV = os.getenv("PROMPT_MODE", "remote").lower()

CHECK_INTERVAL_S = int(os.getenv("PROMPT_CHECK_INTERVAL_S", "60"))  # scheduler interval

ARTIFACTORY_LISTING_URL = os.getenv(
    "ARTIFACTORY_LISTING_URL",
    "https://your-artifactory/path/to/bundles/"
).rstrip("/") + "/"

# Admin auth (keep simple)
ADMIN_HEADER_NAME = os.getenv("PROMPT_ADMIN_HEADER", "X-Admin-Token")
ADMIN_TOKEN = os.getenv("PROMPT_ADMIN_TOKEN", "change-me")

# We assume prompts inside zip follow:
#   prompts/prompt_s2b_<unixtimestamp>.yml
# plus an optional pin file:
#   prompts/pin.json (optional)
PROMPT_RE = re.compile(r".*/?prompts/prompt_s2b_(\d+)\.yml$")
PIN_JSON_RE = re.compile(r".*/?prompts/pin\.json$")


# ---------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------
_yaml = YAML(typ="safe")


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ts_to_human(ts: int, tz_name: str = APP_TZ) -> Tuple[str, str]:
    """
    Convert unix timestamp to human-readable strings.
    Returns (utc_str, local_str).
    """
    # UTC
    dt_utc = datetime.fromtimestamp(ts, tz=timezone.utc)
    utc_s = dt_utc.strftime("%Y-%m-%d %H:%M:%S UTC")

    # Local time (zoneinfo is available in py>=3.9)
    try:
        from zoneinfo import ZoneInfo
        dt_local = dt_utc.astimezone(ZoneInfo(tz_name))
        local_s = dt_local.strftime(f"%Y-%m-%d %H:%M:%S {tz_name}")
    except Exception:
        # fallback if zoneinfo not available
        local_s = utc_s

    return utc_s, local_s


def atomic_write_json(path: Path, data: dict) -> None:
    """
    Safely write JSON with atomic replace to avoid partial writes.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_yaml_file(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"YAML file not found: {path}")
    with open(path, "r", encoding="utf-8") as f:
        data = _yaml.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"YAML must be a mapping/dict: {path}")
    return data


def render_jinja(template_str: str, **kwargs) -> str:
    """
    Render a Jinja2 template string with provided variables.
    """
    return Template(template_str).render(**kwargs)


# ---------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------
@dataclass(frozen=True)
class PromptBundle:
    """
    Represents a parsed prompt YAML from a specific version inside a zip.
    """
    zip_id: str
    prompt_ts: int
    data: dict


# ---------------------------------------------------------------------
# Prompt Registry
# ---------------------------------------------------------------------
class PromptRegistry:
    """
    A lightweight in-memory view over a prompt YAML.
    Your YAML schema:
      meta:
        name: ...
        version: ...
      tasks:
        <pipeline_task_name>:
          model: ...
          system_prompt: |
          user_prompt: |
    """
    def __init__(self, prompt_data: dict):
        self.meta = prompt_data.get("meta", {})
        self.tasks = prompt_data.get("tasks", {})

        if not isinstance(self.tasks, dict):
            raise ValueError("prompt_data.tasks must be a dict")

    def list_tasks(self) -> List[str]:
        return list(self.tasks.keys())

    def get_task(self, task_name: str) -> dict:
        if task_name not in self.tasks:
            raise KeyError(f"Task not found: {task_name}")
        task = self.tasks[task_name]
        if not isinstance(task, dict):
            raise ValueError(f"Task config must be dict: {task_name}")
        # Minimal required keys for your convention:
        if "system_prompt" not in task or "user_prompt" not in task:
            raise ValueError(f"Task '{task_name}' missing system_prompt/user_prompt")
        return task


# ---------------------------------------------------------------------
# Artifactory client (listing + latest zip selection + download)
# ---------------------------------------------------------------------
class ArtifactoryClient:
    """
    Responsible only for:
    - fetching the HTML listing page
    - finding the latest zip (based on filename sorting by default)
    - downloading the zip bytes
    """
    def __init__(self, listing_url: str):
        self.listing_url = listing_url.rstrip("/") + "/"

    def find_latest_zip(self) -> Tuple[str, str]:
        """
        Returns (zip_name, zip_url).
        NOTE: This uses max(filename) logic. If your naming is not sortable,
              replace this with parsing the listing "Last modified" column.
        """
        r = requests.get(self.listing_url, timeout=10)
        r.raise_for_status()

        soup = BeautifulSoup(r.text, "html.parser")
        zips: List[Tuple[str, str]] = []
        for a in soup.find_all("a"):
            href = a.get("href") or ""
            if href.endswith(".zip"):
                name = href.split("/")[-1]
                url = self.listing_url + href
                zips.append((name, url))

        if not zips:
            raise RuntimeError("No .zip found in Artifactory listing")

        # pick latest by name
        return max(zips, key=lambda x: x[0])

    def download_zip(self, zip_url: str) -> bytes:
        r = requests.get(zip_url, timeout=30)
        r.raise_for_status()
        return r.content


# ---------------------------------------------------------------------
# Zip prompt store (in-memory)
# ---------------------------------------------------------------------
class PromptZipStore:
    """
    Holds current latest zip bytes in memory and provides:
    - refresh_if_needed(): check latest zip id; download if changed
    - available_versions(): list all prompt timestamps found in zip
    - get_by_ts(ts): parse and return prompt yaml for that timestamp
    - optional: read pin.json inside zip (if you want a read-only pin config)
    """
    def __init__(self, client: ArtifactoryClient, check_interval_s: int):
        self.client = client
        self.check_interval_s = check_interval_s

        self._lock = threading.Lock()
        self._next_check_at = 0.0

        self._zip_id: Optional[str] = None
        self._zip_url: Optional[str] = None
        self._zip_bytes: Optional[bytes] = None

        self._index: Dict[int, str] = {}   # ts -> path_in_zip
        self._cache: Dict[int, dict] = {}  # ts -> parsed YAML dict

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
            idx: Dict[int, str] = {}
            pin_obj: Optional[dict] = None

            with ZipFile(BytesIO(zip_bytes)) as zf:
                for name in zf.namelist():
                    m = PROMPT_RE.match(name)
                    if m:
                        ts = int(m.group(1))
                        idx[ts] = name
                    elif PIN_JSON_RE.match(name):
                        # optional: read pin.json if present
                        try:
                            with zf.open(name) as f:
                                pin_obj = json.loads(f.read().decode("utf-8"))
                        except Exception:
                            pin_obj = None

            if not idx:
                raise RuntimeError("No prompts/prompt_s2b_<ts>.yml found inside zip")

            with self._lock:
                self._zip_id = zip_name
                self._zip_url = zip_url
                self._zip_bytes = zip_bytes
                self._index = idx
                self._cache = {}       # clear parsed cache after zip changes
                self._pin_json = pin_obj

            return True

        except Exception as e:
            # Do not break service; keep previous zip if any
            print(f"[zip-store] refresh failed: {e}")
            return False

    def available_versions(self) -> List[int]:
        """
        List all prompt versions (timestamps) available in current zip.
        """
        with self._lock:
            return sorted(self._index.keys())

    def get_by_ts(self, ts: int) -> PromptBundle:
        """
        Parse and return the YAML for a given prompt version timestamp.
        """
        with self._lock:
            if self._zip_id is None or self._zip_bytes is None:
                raise RuntimeError("Zip not loaded yet")
            if ts in self._cache:
                return PromptBundle(zip_id=self._zip_id, prompt_ts=ts, data=self._cache[ts])

            path = self._index.get(ts)
            if not path:
                raise KeyError(f"Prompt ts={ts} not found in current zip {self._zip_id}")
            zip_id = self._zip_id
            zip_bytes = self._zip_bytes

        # parse outside lock for better concurrency
        with ZipFile(BytesIO(zip_bytes)) as zf:
            with zf.open(path) as f:
                data = _yaml.load(f.read().decode("utf-8"))

        if not isinstance(data, dict):
            raise ValueError("Prompt YAML must be a mapping/dict")

        with self._lock:
            self._cache[ts] = data

        return PromptBundle(zip_id=zip_id, prompt_ts=ts, data=data)

    def get_latest(self) -> PromptBundle:
        """
        Return the latest prompt by timestamp (max ts).
        """
        with self._lock:
            if not self._index:
                raise RuntimeError("No prompt index; zip not loaded or empty")
            ts = max(self._index.keys())
        return self.get_by_ts(ts)

    def get_pin_json(self) -> Optional[dict]:
        """
        Optional helper if you embed pin.json inside zip.
        In your current constraint (can't write artifactory), this is read-only.
        """
        with self._lock:
            return self._pin_json


# ---------------------------------------------------------------------
# Local prompt store (default & active config)
# ---------------------------------------------------------------------
class LocalPromptStore:
    """
    Local storage is used for:
    - default prompt fallback (default.yml in image)
    - config.json storing current active selection (ts/locked/mode_override)
    In your current stage, local config is still useful for:
    - runtime switching via admin API
    - stable selection within a pod lifecycle
    """
    def __init__(self, prompts_dir: Path, default_path: Path, config_path: Path):
        self.prompts_dir = prompts_dir
        self.default_path = default_path
        self.config_path = config_path
        self.prompts_dir.mkdir(parents=True, exist_ok=True)

    def read_config(self) -> dict:
        return read_json(self.config_path)

    def write_config(self, cfg: dict) -> None:
        atomic_write_json(self.config_path, cfg)

    def load_default_prompt(self) -> dict:
        """
        Default prompt MUST exist in image for safe fallback.
        """
        return load_yaml_file(self.default_path)


# ---------------------------------------------------------------------
# Runtime manager (single in-memory active registry)
# ---------------------------------------------------------------------
class PromptRuntime:
    """
    Owns the active prompt registry used by business calls.

    The core rule:
    - NEVER do network or disk IO in request hot-path.
    - Only rebuild registry when:
      - service startup
      - admin switch
      - background refresh decides to update active prompt
    """
    def __init__(self, local_store: LocalPromptStore, zip_store: PromptZipStore):
        self.local_store = local_store
        self.zip_store = zip_store

        self._lock = threading.Lock()
        self._active_registry: Optional[PromptRegistry] = None
        self._active_info: dict = {}  # for status/debug

    # -----------------------------
    # Mode logic (env + API override)
    # -----------------------------
    def get_effective_mode(self) -> str:
        """
        Effective mode priority:
        1) config.json["mode_override"] if set to local/remote
        2) env PROMPT_MODE
        """
        cfg = self.local_store.read_config()
        override = cfg.get("mode_override")
        if override in ("local", "remote"):
            return override
        return PROMPT_MODE_ENV if PROMPT_MODE_ENV in ("local", "remote") else "remote"

    # -----------------------------
    # Build/replace active registry
    # -----------------------------
    def _set_active_registry(self, prompt_data: dict, source: str, extra: dict):
        """
        Atomically replace active registry.
        """
        reg = PromptRegistry(prompt_data)
        with self._lock:
            self._active_registry = reg
            self._active_info = {"source": source, "updated_at": _now_utc_iso(), **extra}

    def initialize(self):
        """
        Startup initialization strategy:
        1) Try to use config.json selection if possible (remote ts if available, else default)
        2) Try remote latest (if mode=remote)
        3) Fallback to local default.yml
        """
        mode = self.get_effective_mode()
        cfg = self.local_store.read_config()

        # 1) If config has active_ts and we're in remote mode, try loading that ts from zip
        if mode == "remote":
            # Try to refresh zip store once at startup
            self.zip_store.refresh_if_needed()

            active_ts = cfg.get("active_ts")
            if isinstance(active_ts, int):
                try:
                    bundle = self.zip_store.get_by_ts(active_ts)
                    self._set_active_registry(
                        bundle.data,
                        source="remote-ts",
                        extra={"zip_id": bundle.zip_id, "prompt_ts": bundle.prompt_ts, "locked": cfg.get("locked", False)}
                    )
                    return
                except Exception as e:
                    print(f"[runtime] config active_ts load failed: {e}")

            # 2) Otherwise try remote latest
            try:
                bundle = self.zip_store.get_latest()
                self._set_active_registry(
                    bundle.data,
                    source="remote-latest",
                    extra={"zip_id": bundle.zip_id, "prompt_ts": bundle.prompt_ts, "locked": cfg.get("locked", False)}
                )
                # update config to reflect what we used (unless locked explicitly)
                # we still record active_ts so admin can see status
                cfg["active_ts"] = bundle.prompt_ts
                cfg.setdefault("locked", False)
                cfg["last_zip_id"] = bundle.zip_id
                cfg["updated_at"] = _now_utc_iso()
                self.local_store.write_config(cfg)
                return
            except Exception as e:
                print(f"[runtime] remote latest load failed: {e}")

        # 3) Fallback to default
        default_data = self.local_store.load_default_prompt()
        self._set_active_registry(default_data, source="default", extra={"prompt_ts": None, "zip_id": None, "locked": cfg.get("locked", False)})

    def get_registry(self) -> PromptRegistry:
        """
        Get active registry for business usage.
        """
        with self._lock:
            if self._active_registry is None:
                raise RuntimeError("Prompt registry not initialized")
            return self._active_registry

    def status(self) -> dict:
        cfg = self.local_store.read_config()
        with self._lock:
            info = dict(self._active_info)
        return {
            "effective_mode": self.get_effective_mode(),
            "mode_override": cfg.get("mode_override"),
            "env_mode": PROMPT_MODE_ENV,
            "locked": cfg.get("locked", False),
            "config_active_ts": cfg.get("active_ts"),
            "config_last_zip_id": cfg.get("last_zip_id"),
            "active": info,
        }

    # -----------------------------
    # Admin controls (switch/lock/unlock)
    # -----------------------------
    def switch_to_ts(self, ts: int, lock: bool = True):
        """
        Switch active prompt to a specific remote version ts.
        - Requires remote mode or at least zip store loaded
        - lock=True means background refresh won't overwrite it automatically
        """
        # Ensure zip store is up-to-date at least once
        self.zip_store.refresh_if_needed()
        bundle = self.zip_store.get_by_ts(ts)

        # Swap active registry
        self._set_active_registry(
            bundle.data,
            source="admin-switch",
            extra={"zip_id": bundle.zip_id, "prompt_ts": bundle.prompt_ts, "locked": lock}
        )

        # Persist selection into config.json (pod-local)
        cfg = self.local_store.read_config()
        cfg["active_ts"] = ts
        cfg["locked"] = bool(lock)
        cfg["last_zip_id"] = bundle.zip_id
        cfg["updated_at"] = _now_utc_iso()
        self.local_store.write_config(cfg)

    def unlock_follow_latest(self):
        """
        Unlock and immediately follow latest remote prompt (if remote mode).
        If remote isn't available, fallback to default.
        """
        cfg = self.local_store.read_config()
        cfg["locked"] = False
        self.local_store.write_config(cfg)

        mode = self.get_effective_mode()
        if mode == "remote":
            self.zip_store.refresh_if_needed()
            try:
                bundle = self.zip_store.get_latest()
                self._set_active_registry(
                    bundle.data,
                    source="unlock-latest",
                    extra={"zip_id": bundle.zip_id, "prompt_ts": bundle.prompt_ts, "locked": False}
                )
                cfg = self.local_store.read_config()
                cfg["active_ts"] = bundle.prompt_ts
                cfg["last_zip_id"] = bundle.zip_id
                cfg["updated_at"] = _now_utc_iso()
                self.local_store.write_config(cfg)
                return
            except Exception as e:
                print(f"[runtime] unlock remote latest failed: {e}")

        # fallback
        default_data = self.local_store.load_default_prompt()
        self._set_active_registry(default_data, source="default", extra={"zip_id": None, "prompt_ts": None, "locked": False})

    def set_mode_override(self, mode: str):
        """
        Set runtime mode override (local/remote) in config.json.
        """
        if mode not in ("local", "remote"):
            raise ValueError("mode must be local or remote")
        cfg = self.local_store.read_config()
        cfg["mode_override"] = mode
        cfg["updated_at"] = _now_utc_iso()
        self.local_store.write_config(cfg)

        # If switching to remote, try to initialize remote latest immediately
        if mode == "remote":
            self.initialize()
        else:
            # local: set to default immediately to avoid remote dependency
            default_data = self.local_store.load_default_prompt()
            self._set_active_registry(default_data, source="default", extra={"zip_id": None, "prompt_ts": None, "locked": cfg.get("locked", False)})

    def clear_mode_override(self):
        cfg = self.local_store.read_config()
        cfg.pop("mode_override", None)
        cfg["updated_at"] = _now_utc_iso()
        self.local_store.write_config(cfg)
        # re-initialize based on env mode
        self.initialize()

    # -----------------------------
    # Background refresh (scheduler)
    # -----------------------------
    def refresh_background(self):
        """
        Called periodically by a background thread.

        Logic:
        - If effective_mode == local:
            do nothing (keep default/local)
        - If effective_mode == remote:
            refresh zip store (throttled)
            if NOT locked:
                update active to remote latest when zip changes
            if locked:
                do not auto overwrite active (keeps stable selected version)
        """
        mode = self.get_effective_mode()
        if mode != "remote":
            return

        cfg = self.local_store.read_config()
        locked = bool(cfg.get("locked", False))

        changed = self.zip_store.refresh_if_needed()
        if not changed:
            return

        # zip changed; only auto-update if not locked
        if locked:
            return

        try:
            bundle = self.zip_store.get_latest()
            self._set_active_registry(
                bundle.data,
                source="bg-remote-latest",
                extra={"zip_id": bundle.zip_id, "prompt_ts": bundle.prompt_ts, "locked": False}
            )
            cfg["active_ts"] = bundle.prompt_ts
            cfg["last_zip_id"] = bundle.zip_id
            cfg["updated_at"] = _now_utc_iso()
            self.local_store.write_config(cfg)
        except Exception as e:
            print(f"[runtime] bg refresh latest failed: {e}")


# ---------------------------------------------------------------------
# FastAPI + Admin auth
# ---------------------------------------------------------------------
app = FastAPI(title="Prompt Manager (YAML tasks + default fallback + remote zip)")

client = ArtifactoryClient(ARTIFACTORY_LISTING_URL)
zip_store = PromptZipStore(client, check_interval_s=CHECK_INTERVAL_S)
local_store = LocalPromptStore(PROMPTS_DIR, DEFAULT_PROMPT_PATH, CONFIG_PATH)
runtime = PromptRuntime(local_store, zip_store)


def require_admin(x_admin_token: Optional[str] = Header(default=None, alias=ADMIN_HEADER_NAME)):
    if x_admin_token != ADMIN_TOKEN:
        raise HTTPException(status_code=401, detail="Unauthorized (admin token missing/invalid)")


@app.on_event("startup")
def startup():
    """
    Startup:
    - Ensure prompts folder exists
    - Ensure default.yml exists (required)
    - Initialize active registry (remote if possible; else default fallback)
    - Start background scheduler thread for remote updates
    """
    PROMPTS_DIR.mkdir(parents=True, exist_ok=True)

    if not DEFAULT_PROMPT_PATH.exists():
        raise RuntimeError(
            f"default prompt not found: {DEFAULT_PROMPT_PATH}. "
            f"Please bake it into image or mount it."
        )

    # Initialize active registry once
    runtime.initialize()

    # Background scheduler: checks remote zip updates periodically
    def loop():
        while True:
            try:
                runtime.refresh_background()
            except Exception as e:
                print(f"[bg] refresh error: {e}")
            time.sleep(1)  # store has throttling; keep loop cheap

    t = threading.Thread(target=loop, daemon=True)
    t.start()


# ---------------------------------------------------------------------
# Business endpoint examples
# ---------------------------------------------------------------------
class LLMCallRequest(BaseModel):
    """
    Example request model for using a task prompt.
    You can pass any variables needed by the Jinja templates.
    """
    task_name: str
    variables: dict


@app.post("/llm/prepare")
def prepare_llm_call(req: LLMCallRequest):
    """
    Demonstration endpoint:
    - Fetch task prompt config from registry
    - Render system/user prompt with Jinja
    - Return prepared payload (model + prompts)
    (Replace with your real LLM call)
    """
    reg = runtime.get_registry()
    task = reg.get_task(req.task_name)

    model = task.get("model", "gpt-4.1")
    system_prompt = task["system_prompt"]
    user_prompt = render_jinja(task["user_prompt"], **req.variables)

    return {
        "model": model,
        "system_prompt": system_prompt,
        "user_prompt": user_prompt,
        "meta": reg.meta,
        "active_status": runtime.status()["active"],
    }


# ---------------------------------------------------------------------
# Admin endpoints
# ---------------------------------------------------------------------
@app.get("/admin/prompt/status", dependencies=[Depends(require_admin)])
def admin_status():
    """
    Show current mode/locked/config and active registry source.
    """
    return runtime.status()


@app.get("/admin/prompt/versions", dependencies=[Depends(require_admin)])
def admin_versions():
    """
    List all available prompt versions in current remote zip (human-readable).
    If remote zip is not loaded/available, return empty list (still safe).
    """
    # In remote mode we try to refresh (throttled)
    if runtime.get_effective_mode() == "remote":
        zip_store.refresh_if_needed()

    zip_id = zip_store.current_zip_id()
    versions = []
    try:
        for ts in zip_store.available_versions():
            utc_s, local_s = ts_to_human(ts)
            versions.append({
                "ts": ts,
                "time_utc": utc_s,
                "time_local": local_s,
                "filename": f"prompt_s2b_{ts}.yml"
            })
        versions.sort(key=lambda x: x["ts"], reverse=True)
    except Exception:
        versions = []

    return {"zip_id": zip_id, "count": len(versions), "versions": versions}


class SwitchRequest(BaseModel):
    ts: int
    lock: bool = True


@app.post("/admin/prompt/switch", dependencies=[Depends(require_admin)])
def admin_switch(req: SwitchRequest):
    """
    Switch active prompt to a specific version timestamp in remote zip.
    lock=True means background refresh won't overwrite it automatically.
    """
    if runtime.get_effective_mode() != "remote":
        raise HTTPException(status_code=400, detail="effective_mode is local; switch requires remote mode")

    try:
        runtime.switch_to_ts(req.ts, lock=req.lock)
        return {"ok": True, "status": runtime.status()}
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"switch failed: {e}")


@app.post("/admin/prompt/unlock", dependencies=[Depends(require_admin)])
def admin_unlock():
    """
    Unlock and follow remote latest (if possible), else fallback to default.
    """
    runtime.unlock_follow_latest()
    return {"ok": True, "status": runtime.status()}


class ModeRequest(BaseModel):
    mode: str  # "local" or "remote"


@app.post("/admin/prompt/mode", dependencies=[Depends(require_admin)])
def admin_set_mode(req: ModeRequest):
    """
    Set mode override in config.json.
    - local: stop using remote; active becomes default
    - remote: attempt remote initialization immediately
    """
    try:
        runtime.set_mode_override(req.mode.lower())
        return {"ok": True, "status": runtime.status()}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/admin/prompt/mode/clear", dependencies=[Depends(require_admin)])
def admin_clear_mode():
    """
    Clear mode override (falls back to env PROMPT_MODE).
    """
    runtime.clear_mode_override()
    return {"ok": True, "status": runtime.status()}