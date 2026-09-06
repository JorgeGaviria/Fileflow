"""Configuracion y perfiles de hardware.

Un perfil agrupa las decisiones que dependen de la maquina: que modelos se cargan,
con que runtime y si hay LLM. El resto del codigo no consulta el hardware nunca,
solo el perfil activo.

Ver docs/modelos-y-perfiles.md
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass, field, asdict
from pathlib import Path

APP_NAME = "fileflow"
CONFIG_FILENAME = "config.local.json"
DB_FILENAME = "index.db"


# ---------------------------------------------------------------------------
# Perfiles
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Profile:
    """Modelos y runtime para un nivel de hardware dado.

    text_dim e image_dim se declaran aqui para poder validar los vectores al
    leerlos del indice: si la dimension no cuadra, el indice es de otro perfil.
    """

    name: str
    text_model: str
    text_dim: int
    image_model: str
    image_dim: int
    runtime: str  # 'onnx' | 'torch'
    device: str  # 'cpu' | 'cuda'
    quantized: bool
    llm: str | None
    batch_size: int
    min_ram_gb: float


PROFILES: dict[str, Profile] = {
    "ligero": Profile(
        name="ligero",
        text_model="intfloat/multilingual-e5-small",
        text_dim=384,
        image_model="openai/clip-vit-base-patch32",
        image_dim=512,
        runtime="onnx",
        device="cpu",
        quantized=True,
        llm=None,
        batch_size=8,
        min_ram_gb=4.0,
    ),
    "equilibrado": Profile(
        name="equilibrado",
        text_model="intfloat/multilingual-e5-base",
        text_dim=768,
        image_model="google/siglip-base-patch16-224",
        image_dim=768,
        runtime="onnx",
        device="cpu",
        quantized=False,
        llm="qwen2.5:3b",
        batch_size=16,
        min_ram_gb=12.0,
    ),
    "potente": Profile(
        name="potente",
        text_model="BAAI/bge-m3",
        text_dim=1024,
        image_model="google/siglip-large-patch16-384",
        image_dim=1024,
        runtime="torch",
        device="cuda",
        quantized=False,
        llm="llama3.1:8b",
        batch_size=32,
        min_ram_gb=16.0,
    ),
}

DEFAULT_PROFILE = "ligero"


def detect_profile() -> str:
    """Preselecciona un perfil segun la maquina.

    Deliberadamente conservador: ante la duda elige el perfil mas ligero. Es
    preferible una aplicacion que va sobrada a una que ahoga el equipo.

    En el primer arranque definitivo esto se complementara con un benchmark
    real de throughput (ver docs/modelos-y-perfiles.md); por ahora se queda en
    una heuristica sobre RAM y presencia de CUDA.
    """
    ram_gb = _total_ram_gb()
    if _has_cuda() and ram_gb >= PROFILES["potente"].min_ram_gb:
        return "potente"
    if ram_gb >= PROFILES["equilibrado"].min_ram_gb:
        return "equilibrado"
    return "ligero"


def _total_ram_gb() -> float:
    """RAM total en GB, sin depender de psutil."""
    try:
        if os.name == "nt":
            import ctypes

            class MemoryStatusEx(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            stat = MemoryStatusEx()
            stat.dwLength = ctypes.sizeof(MemoryStatusEx)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
            return stat.ullTotalPhys / (1024**3)
        return (os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")) / (1024**3)
    except Exception:
        return 0.0


def _has_cuda() -> bool:
    """GPU NVIDIA presente. No garantiza que quepa el modelo, solo que hay GPU."""
    return shutil.which("nvidia-smi") is not None


# ---------------------------------------------------------------------------
# Configuracion de la aplicacion
# ---------------------------------------------------------------------------


@dataclass
class Thresholds:
    """Umbrales del motor de decision. Valores iniciales a calibrar contra el
    corpus de pruebas -- no son sagrados. Ver docs/motor-de-decision.md"""

    high: float = 0.75  # tau_alto: propuesta con confianza
    low: float = 0.45  # tau_bajo: por debajo va a /Unsorted
    margin: float = 0.08  # delta: diferencia minima entre el #1 y el #2
    smoothing_k: int = 10  # k de beta = n / (n + k)


@dataclass
class WatcherSettings:
    debounce_seconds: float = 2.0
    stability_checks: int = 2
    stability_interval: float = 1.0
    max_retries: int = 10


@dataclass
class AppConfig:
    """Configuracion local de esta maquina. NO se versiona: contiene rutas
    absolutas y el perfil de este equipo en concreto."""

    profile: str = DEFAULT_PROFILE
    data_dir: str = ""
    watched_dirs: list[str] = field(default_factory=list)
    thresholds: Thresholds = field(default_factory=Thresholds)
    watcher: WatcherSettings = field(default_factory=WatcherSettings)

    @property
    def profile_obj(self) -> Profile:
        return PROFILES[self.profile]

    @property
    def db_path(self) -> Path:
        return Path(self.data_dir) / DB_FILENAME

    # -- persistencia --------------------------------------------------------

    @classmethod
    def default_data_dir(cls) -> Path:
        """Directorio de datos: junto al proyecto en desarrollo, en LOCALAPPDATA
        cuando este instalado."""
        if os.name == "nt" and (local := os.environ.get("LOCALAPPDATA")):
            return Path(local) / "Fileflow"
        return Path.home() / f".{APP_NAME}"

    @classmethod
    def load(cls, path: Path | None = None) -> AppConfig:
        path = path or cls.default_data_dir() / CONFIG_FILENAME
        if not path.exists():
            cfg = cls(profile=detect_profile(), data_dir=str(cls.default_data_dir()))
            return cfg

        raw = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            profile=raw.get("profile", DEFAULT_PROFILE),
            data_dir=raw.get("data_dir", str(cls.default_data_dir())),
            watched_dirs=raw.get("watched_dirs", []),
            thresholds=Thresholds(**raw.get("thresholds", {})),
            watcher=WatcherSettings(**raw.get("watcher", {})),
        )

    def save(self, path: Path | None = None) -> Path:
        path = path or Path(self.data_dir) / CONFIG_FILENAME
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=2, ensure_ascii=False), encoding="utf-8")
        return path
