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