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