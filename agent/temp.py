class PromptRuntime:
    def __init__(self, local_store, zip_store):
        self.local_store = local_store
        self.zip_store = zip_store
        self._lock = threading.Lock()

        # 注意：不再有默认 proj
        # 每个 proj 独立一套状态（互相独立）
        self._active_ver_by_proj: dict[str, str | None] = {}   # None => latest
        self._locked_by_proj: dict[str, bool] = {}             # 是否锁定（不随 refresh 自动变）
        # 你如果还有 mode_override（local/remote）也可以继续保留原样

    def get_active_version(self, proj: str) -> str:
        with self._lock:
            ver = self._active_ver_by_proj.get(proj)  # 可能 None 或不存在
            locked = self._locked_by_proj.get(proj, False)

        # 如果没指定版本（None 或不存在），就 follow latest
        if not ver:
            return self.zip_store.get_latest_version(proj)
        return ver

    def switch_to_version(self, proj: str, ver: str, lock: bool = True):
        # 先校验 zip 里确实存在这个 proj+ver
        _ = self.zip_store.get_prompt_path(proj, ver)

        with self._lock:
            self._active_ver_by_proj[proj] = ver
            self._locked_by_proj[proj] = lock

    def unlock_follow_latest(self, proj: str):
        with self._lock:
            self._active_ver_by_proj[proj] = None
            self._locked_by_proj[proj] = False

    def get_registry(self, proj: str) -> "PromptRegistry":
        """
        关键点：必须显式传 proj，否则不允许工作
        """
        if not proj:
            raise ValueError("proj is required")

        # 这里假设你已经判定 effective_mode == remote
        ver = self.get_active_version(proj)
        zip_path = self.zip_store.get_prompt_path(proj, ver)

        # 从 zip_bytes 里读取 zip_path，parse yml -> registry
        # （你原来的读取/缓存逻辑基本可复用，只是 cache key 要加上 (proj, ver)）
        return self._load_registry_from_zip(proj, ver, zip_path)