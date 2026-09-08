#!/usr/bin/env python3
"""
fronius2vim - Fronius Inverter to VictoriaMetrics Collector
Multi-inverter support with per-inverter MQTT toggles
"""

import asyncio
import json
import logging
import os
import re
import shutil
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import httpx
import paho.mqtt.client as mqtt
from fastapi import FastAPI, WebSocket
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse

# ---------------------------------------------------------------------------
# Configuration (env vars)
# ---------------------------------------------------------------------------
VICTORIAMETRICS_URL = os.getenv("VICTORIAMETRICS_URL", "http://172.20.204.22:8428")
REALTIME_INTERVAL = int(os.getenv("REALTIME_INTERVAL", "10"))
ENERGY_INTERVAL = int(os.getenv("ENERGY_INTERVAL", "900"))
WEB_PORT = int(os.getenv("WEB_PORT", "8080"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

# MQTT global config (broker connection params)
MQTT_HOST = os.getenv("MQTT_HOST", "").strip().strip("\"'")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883").strip().strip("\"'"))
MQTT_USERNAME = os.getenv("MQTT_USERNAME", "").strip().strip("\"'")
MQTT_PASSWORD = os.getenv("MQTT_PASSWORD", "").strip().strip("\"'")
MQTT_RETAIN = os.getenv("MQTT_RETAIN", "true").strip().strip("\"'").lower() in ("true", "1", "yes")
MQTT_QOS = int(os.getenv("MQTT_QOS", "0").strip().strip("\"'"))

# Legacy single-inverter env var (used as seed on first boot)
FRONIUS_HOST = os.getenv("FRONIUS_HOST", "172.20.203.100")
MQTT_TOPIC = os.getenv("MQTT_TOPIC", "froniusalt/power").strip().strip("\"'")

# Inverters persistence
# Path is overridable (e.g. mount to a volume). Defaults to next to main.py.
# Container default via docker-compose.example.yml: /app/data/inverters.json
# (mounted from ./fronius2vim-data on the host, survives container redeploys).
INVERTERS_FILE = os.getenv(
    "INVERTERS_FILE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "inverters.json"),
).strip().strip("\"'")
INVERTERS_BACKUP = INVERTERS_FILE + ".bak"

# Serializes read-modify-write of the inverter config (prevents races / lost updates)
config_lock = asyncio.Lock()

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL.upper()),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("fronius2vim")

# ---------------------------------------------------------------------------
# FastAPI
# ---------------------------------------------------------------------------
app = FastAPI(title="fronius2vim", version="2.0.0")

# ---------------------------------------------------------------------------
# Inverter config
# ---------------------------------------------------------------------------
INVERTER_MODELS = ("auto", "day_energy", "total_energy")


def normalize_model(model: str) -> str:
    """Return a valid inverter model, falling back to 'auto'."""
    if model in INVERTER_MODELS:
        return model
    return "auto"


@dataclass
class InverterConfig:
    name: str
    host: str
    mqtt_enabled: bool = False
    mqtt_topic: str = ""
    model: str = "auto"

    def __post_init__(self):
        if not self.mqtt_topic:
            slug = re.sub(r"[^a-z0-9]+", "-", self.name.lower()).strip("-")
            self.mqtt_topic = f"froniusalt/{slug}/power"
        self.model = normalize_model(self.model)


def load_inverters() -> List[InverterConfig]:
    """Load inverters from JSON file. NEVER writes to disk — a transient/missing
    file must not silently reset the user's configuration."""
    if os.path.exists(INVERTERS_FILE) and os.path.isfile(INVERTERS_FILE):
        try:
            with open(INVERTERS_FILE, "r") as f:
                data = json.load(f)
            return [InverterConfig(**item) for item in data]
        except Exception as e:
            logger.error(f"Failed to load {INVERTERS_FILE}: {e}")
    elif os.path.exists(INVERTERS_FILE) and not os.path.isfile(INVERTERS_FILE):
        # Docker bind-mount created a directory instead of a file — move it aside and heal
        broken = f"{INVERTERS_FILE}.broken-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}"
        logger.error(
            f"{INVERTERS_FILE} is a DIRECTORY (docker bind-mount created it) — moving it aside to {broken}"
        )
        try:
            os.rename(INVERTERS_FILE, broken)
        except Exception as e:
            logger.error(f"Could not move directory aside: {e}")

    # Try to heal from the last known-good backup so config is never lost
    if os.path.isfile(INVERTERS_BACKUP):
        logger.warning(f"{INVERTERS_FILE} missing — restoring from backup {INVERTERS_BACKUP}")
        try:
            with open(INVERTERS_BACKUP, "r") as f:
                data = json.load(f)
            configs = [InverterConfig(**item) for item in data]
            _write_inverters_atomic(configs)  # re-create the main file from backup
            return configs
        except Exception as e:
            logger.error(f"Failed to restore from backup: {e}")

    # First boot fallback (in-memory only, derived from legacy env vars)
    seed = [InverterConfig(
        name="Fronius", host=FRONIUS_HOST,
        mqtt_enabled=bool(MQTT_HOST), mqtt_topic=MQTT_TOPIC,
    )]
    logger.warning(
        f"{INVERTERS_FILE} not found — using ephemeral config {[c.name for c in seed]}. "
        f"Add inverters via /admin to persist them. Check the volume mount to avoid data loss."
    )
    return seed


def _write_inverters_atomic(configs: List[InverterConfig]):
    """Write config to a temp file, then atomically replace INVERTERS_FILE.
    The backup always holds the latest written config so a lost main file can be fully restored."""
    tmp = INVERTERS_FILE + ".tmp"
    try:
        os.makedirs(os.path.dirname(INVERTERS_FILE) or ".", exist_ok=True)
        with open(tmp, "w") as f:
            json.dump([asdict(c) for c in configs], f, indent=2)
            f.flush()
            os.fsync(f.fileno())

        os.replace(tmp, INVERTERS_FILE)       # atomic on POSIX

        # Backup reflects the just-written (latest) config for rollback/recovery
        shutil.copy2(INVERTERS_FILE, INVERTERS_BACKUP)
        logger.debug(f"Saved {len(configs)} inverters to {INVERTERS_FILE}")
    except Exception as e:
        logger.error(f"Failed to save inverters: {e}")


def save_inverters(configs: List[InverterConfig]):
    """Persist inverter list (atomic, backup-kept, serializer-safe)."""
    _write_inverters_atomic(configs)


# ---------------------------------------------------------------------------
# Metric extraction (unchanged)
# ---------------------------------------------------------------------------
def extract_metric_value(metric_data: Any) -> float:
    """Safely extract numeric metric values from all Fronius Solar API formats"""
    if not metric_data:
        return 0.0
    if isinstance(metric_data, (int, float)):
        return float(metric_data)
    if not isinstance(metric_data, dict):
        return 0.0

    values = metric_data.get("Values")
    if isinstance(values, dict):
        total = 0.0
        for v in values.values():
            if v is not None:
                try:
                    total += float(v)
                except (ValueError, TypeError):
                    pass
        return total

    value = metric_data.get("Value")
    if isinstance(value, dict):
        total = 0.0
        for v in value.values():
            if v is not None:
                try:
                    total += float(v)
                except (ValueError, TypeError):
                    pass
        return total
    elif value is not None:
        try:
            return float(value)
        except (ValueError, TypeError):
            pass

    return 0.0


# ---------------------------------------------------------------------------
# FroniusCollector (unchanged)
# ---------------------------------------------------------------------------
class FroniusCollector:
    def __init__(self, host: str):
        self.host = host
        self.base_url = f"http://{host}/solar_api/v1"
        self.client = httpx.AsyncClient(timeout=10.0)

    async def get_realtime_data(self) -> Optional[Dict]:
        url = f"{self.base_url}/GetInverterRealtimeData.cgi"
        params = {"Scope": "System", "DataCollection": "CumulationInverterData"}
        try:
            response = await self.client.get(url, params=params)
            response.raise_for_status()
            body = response.json().get("Body", {}).get("Data", {})
            return {
                "power": extract_metric_value(body.get("PAC")),
                "day_energy": extract_metric_value(body.get("DAY_ENERGY")),
                "year_energy": extract_metric_value(body.get("YEAR_ENERGY")),
                "total_energy": extract_metric_value(body.get("TOTAL_ENERGY")),
            }
        except Exception as e:
            logger.error(f"[{self.host}] Failed to get realtime data: {e}")
            return None

    async def _get_power_flow_energy(self) -> Optional[Dict]:
        try:
            url = f"{self.base_url}/GetPowerFlowRealtimeData.cgi"
            response = await self.client.get(url)
            if response.status_code == 200:
                data = response.json().get("Body", {}).get("Data", {})
                site = data.get("Site", {})
                e_day = site.get("E_Day")
                e_year = site.get("E_Year")
                e_total = site.get("E_Total")

                if e_day is None:
                    inverters = data.get("Inverters", {})
                    if isinstance(inverters, dict):
                        e_day = sum(float(inv.get("E_Day") or 0) for inv in inverters.values())
                        e_year = sum(float(inv.get("E_Year") or 0) for inv in inverters.values())
                        e_total = sum(float(inv.get("E_Total") or 0) for inv in inverters.values())

                if e_day is not None or e_total is not None:
                    return {
                        "daily": float(e_day) if e_day is not None else 0.0,
                        "yearly": float(e_year) if e_year is not None else 0.0,
                        "total": float(e_total) if e_total is not None else 0.0,
                    }
        except Exception as e:
            logger.debug(f"[{self.host}] Power flow fallback error: {e}")
        return None

    async def _get_common_inverter_energy(self) -> Optional[Dict]:
        try:
            url = f"{self.base_url}/GetInverterRealtimeData.cgi"
            for dev_id in ["1", "0", "2"]:
                response = await self.client.get(
                    url,
                    params={"Scope": "Device", "DataCollection": "CommonInverterData", "DeviceId": dev_id},
                )
                if response.status_code == 200:
                    data = response.json().get("Body", {}).get("Data", {})
                    if data:
                        daily = extract_metric_value(data.get("DAY_ENERGY"))
                        yearly = extract_metric_value(data.get("YEAR_ENERGY"))
                        total = extract_metric_value(data.get("TOTAL_ENERGY"))
                        if daily > 0 or total > 0:
                            return {"daily": daily, "yearly": yearly, "total": total}
        except Exception as e:
            logger.debug(f"[{self.host}] Common inverter data fallback error: {e}")
        return None

    async def get_energy_data(self, model: str = "auto") -> Optional[Dict]:
        url = f"{self.base_url}/GetInverterRealtimeData.cgi"
        params = {"Scope": "System", "DataCollection": "CumulationInverterData"}
        try:
            response = await self.client.get(url, params=params)
            response.raise_for_status()
            body = response.json().get("Body", {}).get("Data", {})
            daily = extract_metric_value(body.get("DAY_ENERGY"))
            yearly = extract_metric_value(body.get("YEAR_ENERGY"))
            total = extract_metric_value(body.get("TOTAL_ENERGY"))

            # Only bother with fallbacks for inverters that lack native DAY_ENERGY
            # in auto mode. Explicit total_energy models (Gen24/Verto/Tauro) skip them.
            if model != "total_energy" and daily == 0.0:
                alt = await self._get_power_flow_energy()
                if not alt or alt.get("daily", 0.0) == 0.0:
                    alt = await self._get_common_inverter_energy()
                if alt:
                    if daily == 0.0 and alt.get("daily", 0.0) > 0:
                        daily = alt["daily"]
                    if yearly == 0.0 and alt.get("yearly", 0.0) > 0:
                        yearly = alt["yearly"]
                    if total == 0.0 and alt.get("total", 0.0) > 0:
                        total = alt["total"]

            return {
                "daily": daily,
                "yearly": yearly,
                "total": total,
            }
        except Exception as e:
            logger.error(f"[{self.host}] Failed to get energy data: {e}")
            alt = await self._get_power_flow_energy()
            if alt:
                return alt
            return None


# ---------------------------------------------------------------------------
# VictoriaMetricsWriter (unchanged except inverter label)
# ---------------------------------------------------------------------------
class VictoriaMetricsWriter:
    def __init__(self, url: str):
        self.url = url.rstrip("/")
        self.client = httpx.AsyncClient(timeout=10.0)

    async def write_metric(self, name: str, value: float, labels: Optional[Dict[str, str]] = None):
        global metrics_log
        if labels is None:
            labels = {}

        label_str = ",".join([f'{k}="{v}"' for k, v in labels.items()])
        metric_line = f"{name}{{{label_str}}} {value}" if label_str else f"{name} {value}"

        try:
            resp = await self.client.post(
                f"{self.url}/api/v1/import/prometheus",
                content=metric_line.encode(),
                headers={"Content-Type": "text/plain"},
            )
            resp.raise_for_status()
            entry = {"timestamp": datetime.utcnow().strftime("%H:%M:%S"), "metric": name, "value": value, "labels": labels, "status": "success"}
            metrics_log.insert(0, entry)
            if len(metrics_log) > MAX_LOG_ENTRIES:
                metrics_log = metrics_log[:MAX_LOG_ENTRIES]
        except Exception as e:
            entry = {"timestamp": datetime.utcnow().strftime("%H:%M:%S"), "metric": name, "value": value, "labels": labels, "status": "error", "error": str(e)}
            metrics_log.insert(0, entry)
            if len(metrics_log) > MAX_LOG_ENTRIES:
                metrics_log = metrics_log[:MAX_LOG_ENTRIES]
            logger.error(f"Failed to write metric: {e}")


# ---------------------------------------------------------------------------
# MqttPublisher (unchanged)
# ---------------------------------------------------------------------------
class MqttPublisher:
    def __init__(self, host: str, port: int = 1883, topic: str = "froniusalt/power",
                 username: Optional[str] = None, password: Optional[str] = None,
                 client_id: str = "fronius2vim", qos: int = 0, retain: bool = True):
        self.host = host.strip().strip("\"'") if host else ""
        self.port = port
        self.topic = topic.strip().strip("\"'") if topic else "froniusalt/power"
        self.qos = qos
        self.retain = retain
        self.client: Optional[mqtt.Client] = None
        self.connected: bool = False

        if not self.host:
            return

        try:
            if hasattr(mqtt, "CallbackAPIVersion"):
                self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)
            else:
                self.client = mqtt.Client(client_id=client_id)

            if username and password:
                self.client.username_pw_set(username, password)
            elif username:
                self.client.username_pw_set(username)

            self.client.on_connect = self._on_connect
            self.client.on_disconnect = self._on_disconnect
            self.client.on_publish = self._on_publish
            self.client.connect_async(self.host, self.port, keepalive=60)
            self.client.loop_start()
            logger.info(f"MQTT connecting to {self.host}:{self.port}, topic: '{self.topic}'")
        except Exception as e:
            logger.error(f"MQTT init failed: {e}")
            self.client = None

    def _on_connect(self, client, userdata, flags, rc, properties=None):
        rc_code = getattr(rc, "value", rc) if rc is not None else 0
        self.connected = rc_code == 0
        if self.connected:
            logger.info(f"MQTT connected ({self.host}:{self.port})")
        else:
            logger.warning(f"MQTT connect failed rc={rc}")

    def _on_disconnect(self, client, userdata, *args, **kwargs):
        self.connected = False

    def _on_publish(self, client, userdata, mid, reason_codes=None, properties=None):
        pass

    def publish_power(self, power: float):
        if not self.client:
            return
        try:
            res = self.client.publish(self.topic, payload=f"{power:.1f}", qos=self.qos, retain=self.retain)
            if res.rc != mqtt.MQTT_ERR_SUCCESS:
                logger.warning(f"MQTT publish error rc={res.rc}")
        except Exception as e:
            logger.error(f"MQTT publish failed: {e}")

    def close(self):
        if self.client:
            try:
                self.connected = False
                self.client.loop_stop()
                self.client.disconnect()
            except Exception:
                pass

    @property
    def status(self) -> str:
        if not self.host:
            return "disabled"
        return "connected" if self.connected else "disconnected"


# ---------------------------------------------------------------------------
# Global state
# ---------------------------------------------------------------------------
metrics_log: list = []
MAX_LOG_ENTRIES = 50
inverters_data: Dict[str, Dict[str, Any]] = {}
collector_tasks: Dict[str, List[asyncio.Task]] = {}
mqtt_publishers: Dict[str, MqttPublisher] = {}


midnight_baselines: Dict[str, Dict[str, Any]] = {}


async def get_midnight_baseline_total(name: str, writer_url: str) -> Optional[float]:
    """Fetch the total_energy reading at midnight (start of today) from VictoriaMetrics."""
    try:
        now = datetime.now()
        start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
        start_ts = int(start_of_day.timestamp())

        # 1. Point query at start of today
        query_url = f"{writer_url}/api/v1/query"
        params = {"query": f'fronius_total_energy_watthours{{inverter="{name}"}}', "time": str(start_ts)}
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(query_url, params=params)
            if resp.status_code == 200:
                result = resp.json().get("data", {}).get("result", [])
                if result:
                    val = float(result[0].get("value", [0, 0])[1])
                    if val > 0:
                        return val

        # 2. Range query for the first recorded point today
        range_url = f"{writer_url}/api/v1/query_range"
        range_params = {
            "query": f'fronius_total_energy_watthours{{inverter="{name}"}}',
            "start": str(start_ts),
            "end": str(int(now.timestamp())),
            "step": "15m",
        }
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(range_url, params=range_params)
            if resp.status_code == 200:
                result = resp.json().get("data", {}).get("result", [])
                if result:
                    values = result[0].get("values", [])
                    if values:
                        val = float(values[0][1])
                        if val > 0:
                            return val
    except Exception as e:
        logger.debug(f"[{name}] Could not fetch baseline total from VM: {e}")
    return None


async def calculate_daily_energy(name: str, day_energy: float, total_energy: float, writer_url: str, model: str = "auto") -> float:
    """Calculate daily energy (Wh). Uses native DAY_ENERGY for Symo/Primo-like
    inverters, or derives it from TOTAL_ENERGY deltas for Gen24/Verto/Tauro
    which only expose TOTAL_ENERGY. 'model' may force either strategy."""
    day_energy = day_energy or 0.0
    total_energy = total_energy or 0.0
    today = datetime.now().strftime("%Y-%m-%d")

    # Native DAY_ENERGY provided by inverter (Symo/Primo, or auto-detected)
    if day_energy > 0 and model != "total_energy":
        if total_energy > 0:
            midnight_baselines[name] = {"date": today, "total": total_energy - day_energy}
        return day_energy

    # Derive from TOTAL_ENERGY (only option for Gen24/Verto/Tauro)
    if total_energy > 0:
        base = midnight_baselines.get(name)
        if base and base.get("date") == today and total_energy >= base["total"]:
            return max(0.0, total_energy - base["total"])

        # Fetch baseline from VictoriaMetrics for today
        vm_base = await get_midnight_baseline_total(name, writer_url)
        if vm_base is not None and vm_base > 0 and vm_base <= total_energy:
            midnight_baselines[name] = {"date": today, "total": vm_base}
            return max(0.0, total_energy - vm_base)
        else:
            midnight_baselines[name] = {"date": today, "total": total_energy}
            return 0.0

    return 0.0


# ---------------------------------------------------------------------------
# Per-inverter background tasks
# ---------------------------------------------------------------------------
async def realtime_collector(name: str, collector: FroniusCollector, writer: VictoriaMetricsWriter, mqtt_pub: Optional[MqttPublisher], model: str = "auto"):
    while True:
        try:
            data = await collector.get_realtime_data()
            if data:
                power = data["power"]
                await writer.write_metric("fronius_power_watts", power, {"inverter": name})
                if mqtt_pub:
                    mqtt_pub.publish_power(power)

                # Calculate live daily energy (native DAY_ENERGY or TOTAL_ENERGY delta)
                daily = await calculate_daily_energy(name, data.get("day_energy", 0.0), data.get("total_energy", 0.0), VICTORIAMETRICS_URL, model)

                inverters_data[name] = {
                    **inverters_data.get(name, {}),
                    "power": power,
                    "daily_energy": daily,
                    "online": True,
                    "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                }
            else:
                if mqtt_pub:
                    mqtt_pub.publish_power(0.0)
                inverters_data[name] = {**inverters_data.get(name, {}), "online": False}
        except Exception as e:
            logger.error(f"[{name}] realtime error: {e}")
        await asyncio.sleep(REALTIME_INTERVAL)


async def energy_collector(name: str, collector: FroniusCollector, writer: VictoriaMetricsWriter, model: str = "auto"):
    while True:
        try:
            data = await collector.get_energy_data(model)
            if data:
                daily = data.get("daily", 0.0)
                total = data.get("total", 0.0)
                if (daily is None or daily == 0.0) and total and total > 0:
                    daily = await calculate_daily_energy(name, 0.0, total, VICTORIAMETRICS_URL, model)

                await writer.write_metric("fronius_daily_energy_watthours", daily or 0.0, {"inverter": name})
                await writer.write_metric("fronius_yearly_energy_watthours", data.get("yearly") or 0.0, {"inverter": name})
                await writer.write_metric("fronius_total_energy_watthours", total or 0.0, {"inverter": name})
                inverters_data[name] = {
                    **inverters_data.get(name, {}),
                    "daily_energy": daily or 0.0,
                }
        except Exception as e:
            logger.error(f"[{name}] energy error: {e}")
        await asyncio.sleep(ENERGY_INTERVAL)


def start_inverter_tasks(config: InverterConfig, writer: VictoriaMetricsWriter):
    """Start background tasks for a single inverter."""
    if config.name in collector_tasks:
        return  # already running

    collector = FroniusCollector(config.host)
    mqtt_pub = None
    if MQTT_HOST and config.mqtt_enabled:
        mqtt_pub = MqttPublisher(
            host=MQTT_HOST, port=MQTT_PORT, topic=config.mqtt_topic,
            username=MQTT_USERNAME or None, password=MQTT_PASSWORD or None,
            client_id=f"fronius2vim-{config.name}", qos=MQTT_QOS, retain=MQTT_RETAIN,
        )
    mqtt_publishers[config.name] = mqtt_pub

    inverters_data[config.name] = {"power": 0, "daily_energy": 0, "online": False, "timestamp": ""}

    t1 = asyncio.create_task(realtime_collector(config.name, collector, writer, mqtt_pub, config.model))
    t2 = asyncio.create_task(energy_collector(config.name, collector, writer, config.model))
    collector_tasks[config.name] = [t1, t2]
    logger.info(f"Started collector for '{config.name}' ({config.host}) model={config.model} mqtt={config.mqtt_enabled}")


def stop_inverter_tasks(name: str):
    """Stop background tasks for an inverter."""
    tasks = collector_tasks.pop(name, [])
    for t in tasks:
        t.cancel()
    pub = mqtt_publishers.pop(name, None)
    if pub:
        pub.close()
    inverters_data.pop(name, None)
    logger.info(f"Stopped collector for '{name}'")


# ---------------------------------------------------------------------------
# Dashboard HTML
# ---------------------------------------------------------------------------
HTML_DASHBOARD = """
<!DOCTYPE html>
<html>
<head>
    <title>fronius2vim Dashboard</title>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        *{margin:0;padding:0;box-sizing:border-box}
        :root{--bg:#f3f3f7;--card:#fff;--border:#f9f9fb;--text:#28293e;--muted:#93949e;--green:#0fde41}
        body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:var(--bg);color:var(--text);min-height:100vh}
        .top-bar{background:var(--card);border-bottom:1px solid var(--border);padding:16px 24px;display:flex;justify-content:space-between;align-items:center}
        .site-title{font-size:1.125rem;font-weight:700;letter-spacing:.5px;text-transform:uppercase}
        .status-group{display:flex;align-items:center;gap:16px}
        .status-badge{display:flex;align-items:center;gap:8px;font-size:.875rem;color:var(--muted)}
        .status-dot{width:8px;height:8px;border-radius:50%;background:#ef4444}
        .status-dot.connected{background:var(--green)}
        .status-dot.disabled{background:var(--muted);opacity:.5}
        .container{max-width:900px;margin:0 auto;padding:24px}
        .inv-card{background:var(--card);border-radius:16px;padding:20px 24px;box-shadow:0 1px 3px rgba(0,0,0,.05);display:flex;justify-content:space-between;align-items:center}
        #inverterCards{display:grid;gap:16px;margin-bottom:24px}
        #inverterCards:has(.inv-card:nth-child(2)){grid-template-columns:repeat(2,1fr)}
        @media(max-width:640px){#inverterCards:has(.inv-card:nth-child(2)){grid-template-columns:1fr}}
        .inv-name{font-weight:600;font-size:1rem}
        .inv-status{display:flex;align-items:center;gap:6px;font-size:.8rem;color:var(--muted)}
        .inv-metrics{display:flex;gap:32px}
        .inv-metric-val{font-size:1.5rem;font-weight:700}
        .inv-metric-unit{font-size:.8rem;color:var(--muted);margin-left:2px}
        .inv-metric-lbl{font-size:.7rem;color:var(--muted);text-transform:uppercase;letter-spacing:.5px}
        .chart-card{background:var(--card);border-radius:16px;padding:20px;margin-bottom:20px;box-shadow:0 1px 3px rgba(0,0,0,.05)}
        .chart-header{display:flex;justify-content:space-between;align-items:center;margin-bottom:16px}
        .chart-title{font-size:.875rem;font-weight:600}
        .timestamp{font-size:.75rem;color:var(--muted)}
        .legend{display:flex;gap:16px;margin-bottom:16px;padding:10px 14px;background:#f9fafb;border-radius:8px}
        .legend-item{display:flex;align-items:center;gap:6px;font-size:.75rem;color:var(--muted)}
        .legend-color{width:12px;height:12px;border-radius:2px}
        .metrics-log{max-height:300px;overflow-y:auto;font-family:'SF Mono',Monaco,monospace;font-size:.75rem;line-height:1.5}
        .log-entry{display:flex;gap:12px;padding:8px 12px;border-bottom:1px solid var(--border);animation:fadeIn .3s ease}
        .log-entry:last-child{border-bottom:none}
        @keyframes fadeIn{from{opacity:0;background:rgba(11,166,49,.1)}to{opacity:1;background:transparent}}
        .log-time{color:var(--muted);flex-shrink:0;min-width:60px}
        .log-status{flex-shrink:0;width:16px;text-align:center}
        .log-status.success{color:var(--green)}
        .log-status.error{color:#ef4444}
        .log-data{flex:1;word-break:break-all}
        .log-metric{font-weight:600}
        .log-value{color:#faf000}
        .log-labels{color:var(--muted)}
        .log-empty{padding:24px;text-align:center;color:var(--muted);font-style:italic}
        .footer{text-align:center;padding:24px;color:var(--muted);font-size:.75rem}
    </style>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
</head>
<body>
    <div class="top-bar">
        <div class="site-title">fronius2vim</div>
        <div class="status-group">
            <div class="status-badge"><span class="status-dot disabled" id="mqttDot"></span><span id="mqttText">MQTT</span></div>
            <div class="status-badge"><span class="status-dot" id="wsDot"></span><span id="wsText">Connecting...</span></div>
            <a href="/admin" style="font-size:.8rem;color:var(--muted);text-decoration:none">admin</a>
        </div>
    </div>
    <div class="container">
        <div id="inverterCards"></div>
        <div class="chart-card">
            <div class="chart-header">
                <div class="chart-title">Energy Generation Today</div>
                <span class="timestamp" id="timestamp">--</span>
            </div>
            <div class="legend" id="todayLegend"></div>
            <canvas id="combinedChart"></canvas>
        </div>
        <div class="chart-card">
            <div class="chart-header"><div class="chart-title">Last 7 Days Production</div></div>
            <canvas id="sevenDayChart"></canvas>
        </div>
        <div class="chart-card">
            <div class="chart-header"><div class="chart-title">Raw Metrics to VictoriaMetrics</div><span class="timestamp">Last 50</span></div>
            <div class="metrics-log" id="metricsLog"><div class="log-empty">Waiting for metrics...</div></div>
        </div>
    </div>
    <div class="footer">fronius2vim</div>
    <script>
        const ctx = document.getElementById('combinedChart').getContext('2d');
        const combinedChart = new Chart(ctx, {
            type: 'bar',
            data: {labels:[], datasets:[]},
            options: {
                responsive: true,
                maintainAspectRatio: true,
                interaction: {mode: 'index', intersect: false},
                scales: {
                    y: {
                        type: 'linear',
                        position: 'left',
                        beginAtZero: true,
                        stacked: true,
                        grid: {color: 'rgba(0,0,0,.04)', drawBorder: false},
                        ticks: {color: '#6b7280', font: {size: 11}}
                    },
                    y1: {
                        type: 'linear',
                        position: 'right',
                        beginAtZero: true,
                        max: 35,
                        grid: {display: false},
                        ticks: {color: '#93949e', font: {size: 11}}
                    },
                    x: {
                        type: 'category',
                        stacked: true,
                        grid: {display: false},
                        ticks: {color: '#6b7280', font: {size: 11}, maxRotation: 45, autoSkip: true, maxTicksLimit: 12}
                    }
                },
                plugins: {legend: {display: false}}
            }
        });
        const PALETTE=['#0fde41','#faf000','#0ea5e9','#f97316','#a855f7','#ef4444','#14b8a6','#84cc16'];
        async function fetchCombinedData(){try{const r=await fetch('/api/today');const d=await r.json();if(d.times&&d.times.length&&d.series&&d.series.length){combinedChart.data.labels=d.times;combinedChart.data.datasets=[];d.series.forEach((s,i)=>{const c=PALETTE[i%PALETTE.length];combinedChart.data.datasets.push({type:'bar',label:s.name,data:s.energy_kwh,backgroundColor:c+'cc',borderWidth:0,borderRadius:3,stack:'energy',barPercentage:1.0,categoryPercentage:3.5,order:2});combinedChart.data.datasets.push({type:'line',label:s.name+' (power)',data:s.power_kw,borderColor:c,backgroundColor:'transparent',borderWidth:2,tension:.4,pointRadius:0,yAxisID:'y1',order:1})});combinedChart.update();document.getElementById('todayLegend').innerHTML=d.series.map((s,i)=>{const c=PALETTE[i%PALETTE.length];return `<div class="legend-item"><div class="legend-color" style="background:${c}"></div><span>${s.name}</span></div>`}).join('')}}catch(e){}}
        fetchCombinedData();setInterval(fetchCombinedData,300000);

        const sCtx = document.getElementById('sevenDayChart').getContext('2d');
        const sevenDayChart = new Chart(sCtx, {
            type:'bar',data:{labels:[],datasets:[]},
            options:{responsive:true,maintainAspectRatio:true,scales:{y:{beginAtZero:true,stacked:true,grid:{color:'rgba(0,0,0,.04)',drawBorder:false},ticks:{color:'#93949e',font:{size:11}}},x:{type:'category',stacked:true,grid:{display:false},ticks:{color:'#93949e',font:{size:11}}}},plugins:{legend:{display:false}}}
        });
        async function fetchSevenDay(){try{const r=await fetch('/api/history/7days');const d=await r.json();if(d.days&&d.days.length&&d.series&&d.series.length){sevenDayChart.data.labels=d.days;sevenDayChart.data.datasets=d.series.map((s,i)=>{const c=PALETTE[i%PALETTE.length];return {label:s.name,data:s.kwh,backgroundColor:c+'cc',borderWidth:0,borderRadius:4,stack:'energy'}});sevenDayChart.update()}}catch(e){}}
        fetchSevenDay();setInterval(fetchSevenDay,3600000);

        let ws;
        function renderCards(inverters){const c=document.getElementById('inverterCards');c.innerHTML=inverters.map(inv=>{const d=inv.data||{};const on=d.online;const p=(d.power/1000).toFixed(2);const e=(d.daily_energy/1000).toFixed(2);return `<div class="inv-card"><div><div class="inv-name">${inv.name}</div><div class="inv-status"><span class="status-dot ${on?'connected':'disabled'}"></span>${on?'Online':'Offline'}<span style="color:var(--muted);margin-left:8px">${inv.host}</span></div></div><div class="inv-metrics"><div><div class="inv-metric-val">${p}<span class="inv-metric-unit">kW</span></div><div class="inv-metric-lbl">Power</div></div><div><div class="inv-metric-val">${e}<span class="inv-metric-unit">kWh</span></div><div class="inv-metric-lbl">Today</div></div></div></div>`}).join('')}

        function connect(){
            ws=new WebSocket(`ws://${window.location.host}/ws`);
            ws.onopen=()=>{document.getElementById('wsText').textContent='Connected';document.getElementById('wsDot').className='status-dot connected'};
            ws.onmessage=(e)=>{
                const d=JSON.parse(e.data);
                if(d.inverters)renderCards(d.inverters);
                if(d.timestamp)document.getElementById('timestamp').textContent=d.timestamp;
                if(d.mqtt_status!==undefined){const m=document.getElementById('mqttDot');const t=document.getElementById('mqttText');if(d.mqtt_status==='connected'){m.className='status-dot connected';t.textContent='MQTT'}else if(d.mqtt_status==='disconnected'){m.className='status-dot';t.textContent='MQTT'}else{m.className='status-dot disabled';t.textContent='MQTT (off)'}}
                if(d.metrics_log&&d.metrics_log.length){document.getElementById('metricsLog').innerHTML=d.metrics_log.map(en=>{const lb=Object.entries(en.labels||{}).map(([k,v])=>`${k}="${v}"`).join(', ');return `<div class="log-entry"><span class="log-time">${en.timestamp}</span><span class="log-status ${en.status}">${en.status==='success'?'✓':'✗'}</span><span class="log-data"><span class="log-metric">${en.metric}</span> <span class="log-value">${en.value}</span> <span class="log-labels">{${lb}}</span></span></div>`}).join('')}
            };
            ws.onclose=()=>{document.getElementById('wsText').textContent='Disconnected';document.getElementById('wsDot').className='status-dot';setTimeout(connect,5000)};
        }
        connect();
    </script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Admin HTML
# ---------------------------------------------------------------------------
HTML_ADMIN = """
<!DOCTYPE html>
<html>
<head>
    <title>fronius2vim Admin</title>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        *{margin:0;padding:0;box-sizing:border-box}
        body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:#f3f3f7;color:#28293e;min-height:100vh;padding:24px}
        .wrap{max-width:800px;margin:0 auto}
        h1{font-size:1.25rem;margin-bottom:24px}
        table{width:100%;border-collapse:collapse;background:#fff;border-radius:12px;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,.05);margin-bottom:24px}
        th,td{padding:12px 16px;text-align:left;border-bottom:1px solid #f3f3f7;font-size:.875rem}
        th{background:#f9f9fb;font-weight:600;text-transform:uppercase;font-size:.75rem;letter-spacing:.5px;color:#93949e}
        .dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px}
        .dot.on{background:#0fde41}.dot.off{background:#ef4444}.dot.na{background:#93949e;opacity:.5}
        .btn{border:none;border-radius:6px;padding:6px 14px;font-size:.8rem;cursor:pointer;font-weight:500}
        .btn-del{background:#fee;color:#e11d48}.btn-del:hover{background:#fdd}
        .btn-save{background:#e8fde8;color:#16a34a}.btn-save:hover{background:#d4f5d4}
        .btn-edit{background:#eee;color:#374151}.btn-edit:hover{background:#e5e7eb}
        .form-card{background:#fff;border-radius:12px;padding:20px 24px;box-shadow:0 1px 3px rgba(0,0,0,.05);margin-bottom:24px}
        .form-card h2{font-size:.875rem;font-weight:600;margin-bottom:16px}
        .field{margin-bottom:12px}
        .field label{display:block;font-size:.75rem;color:#93949e;text-transform:uppercase;letter-spacing:.5px;margin-bottom:4px}
        .field input,.field select{width:100%;padding:8px 12px;border:1px solid #e5e7eb;border-radius:8px;font-size:.875rem}
        .field input:focus,.field select:focus{outline:none;border-color:#0fde41}
        .actions{display:flex;gap:12px;margin-top:16px}
        a{color:#93949e;text-decoration:none}
    </style>
</head>
<body>
<div class="wrap">
    <h1>Inverter Management <a href="/">back to dashboard</a></h1>
    <table>
        <thead><tr><th>Name</th><th>IP</th><th>Model</th><th>MQTT</th><th>Status</th><th></th></tr></thead>
        <tbody id="invTable"></tbody>
    </table>
    <div class="form-card">
        <h2 id="formTitle">Add Inverter</h2>
        <div class="field"><label>Name</label><input id="fName" placeholder="e.g. Verto1-37123716"></div>
        <div class="field"><label>IP Address</label><input id="fHost" placeholder="e.g. 172.20.204.102"></div>
        <div class="field"><label>Inverter Model</label><select id="fModel">
            <option value="auto" selected>Auto-detect (recommended)</option>
            <option value="day_energy">Symo / Primo / legacy — native DAY_ENERGY</option>
            <option value="total_energy">Gen24 / Verto / Tauro — TOTAL_ENERGY only</option>
        </select></div>
        <div class="field"><label>Publish to MQTT</label><select id="fMqtt"><option value="false">No</option><option value="true">Yes</option></select></div>
        <div class="actions"><button class="btn btn-save" id="saveLabel" onclick="addInverter()">Add Inverter</button> <button class="btn btn-edit" id="cancelBtn" style="display:none" onclick="cancelEdit()">Cancel</button></div>
    </div>
</div>
<script>
let inverters=[];
async function load(){const r=await fetch('/api/inverters');inverters=await r.json();render()}
const modelLabels={'auto':'Auto-detect','day_energy':'DAY_ENERGY','total_energy':'TOTAL_ENERGY'};
function render(){document.getElementById('invTable').innerHTML=inverters.map((inv,i)=>{const st=inv.online?'on':(inv.host?'off':'na');const stl=inv.online?'Online':(inv.host?'Offline':'?');return `<tr><td><strong>${inv.name}</strong></td><td>${inv.host}</td><td>${modelLabels[inv.model]||inv.model}</td><td>${inv.mqtt_enabled?'<span style="color:#16a34a">Yes</span>':'<span style="color:#93949e">No</span>'}</td><td><span class="dot ${st}"></span>${stl}</td><td><button class="btn btn-edit" onclick="editInverter(${i})">Edit</button> <button class="btn btn-del" onclick="delInverter(${i})">Remove</button></td></tr>`}).join('')}
let editingIdx=null;
function editInverter(i){editingIdx=i;const inv=inverters[i];document.getElementById('fName').value=inv.name;document.getElementById('fHost').value=inv.host;document.getElementById('fModel').value=inv.model||'auto';document.getElementById('fMqtt').value=inv.mqtt_enabled?'true':'false';document.getElementById('formTitle').textContent='Edit Inverter';document.getElementById('saveLabel').textContent='Save Changes';document.getElementById('cancelBtn').style.display='inline-block';window.scrollTo({top:0,behavior:'smooth'})}
function cancelEdit(){editingIdx=null;document.getElementById('fName').value='';document.getElementById('fHost').value='';document.getElementById('fModel').value='auto';document.getElementById('fMqtt').value='false';document.getElementById('formTitle').textContent='Add Inverter';document.getElementById('saveLabel').textContent='Add Inverter';document.getElementById('cancelBtn').style.display='none'}
async function addInverter(){const n=document.getElementById('fName').value.trim();const h=document.getElementById('fHost').value.trim();const m=document.getElementById('fMqtt').value==='true';const model=document.getElementById('fModel').value;if(!n||!h)return alert('Name and IP required');
  if(editingIdx!==null){await fetch('/api/inverters/'+encodeURIComponent(inverters[editingIdx].name),{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:n,host:h,mqtt_enabled:m,model})});editingIdx=null}
  else{await fetch('/api/inverters',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:n,host:h,mqtt_enabled:m,model})})}
  cancelEdit();load()}
async function delInverter(i){if(!confirm('Remove '+inverters[i].name+'?'))return;await fetch('/api/inverters/'+encodeURIComponent(inverters[i].name),{method:'DELETE'});load()}
load();setInterval(load,5000);
</script>
</body>
</html>
"""

# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
async def dashboard():
    return HTML_DASHBOARD


@app.get("/admin", response_class=HTMLResponse)
async def admin_page():
    return HTML_ADMIN


@app.get("/api/data")
async def get_data():
    inv_list = load_inverters()
    mqtt_any = any(mqtt_publishers.get(c.name) and mqtt_publishers[c.name].connected for c in inv_list)
    mqtt_status = "connected" if mqtt_any else ("disabled" if not MQTT_HOST else "disconnected")
    return {"inverters": inverters_data, "mqtt_status": mqtt_status}


@app.get("/api/inverters")
async def get_inverters():
    configs = load_inverters()
    result = []
    for c in configs:
        pub = mqtt_publishers.get(c.name)
        result.append({
            "name": c.name,
            "host": c.host,
            "mqtt_enabled": c.mqtt_enabled,
            "mqtt_topic": c.mqtt_topic,
            "model": c.model,
            "online": inverters_data.get(c.name, {}).get("online", False),
        })
    return result


@app.post("/api/inverters")
async def add_inverter(body: dict):
    async with config_lock:
        configs = load_inverters()
        name = body.get("name", "").strip()
        host = body.get("host", "").strip()
        mqtt_enabled = body.get("mqtt_enabled", False)
        model = normalize_model(body.get("model", "auto"))

        if not name or not host:
            return {"error": "name and host required"}
        if any(c.name == name for c in configs):
            return {"error": f"inverter '{name}' already exists"}

        new_cfg = InverterConfig(name=name, host=host, mqtt_enabled=mqtt_enabled, model=model)
        configs.append(new_cfg)
        save_inverters(configs)

    writer = VictoriaMetricsWriter(VICTORIAMETRICS_URL)
    start_inverter_tasks(new_cfg, writer)

    return {"ok": True, "name": name}


@app.delete("/api/inverters/{name}")
async def delete_inverter(name: str):
    async with config_lock:
        configs = [c for c in load_inverters() if c.name != name]
        save_inverters(configs)
        stop_inverter_tasks(name)
    return {"ok": True}


@app.patch("/api/inverters/{name}")
async def update_inverter(name: str, body: dict):
    async with config_lock:
        configs = load_inverters()
        target = None
        for c in configs:
            if c.name == name:
                target = c
                break

        if target is None:
            return {"error": "not found"}

        if "mqtt_enabled" in body:
            target.mqtt_enabled = body["mqtt_enabled"]
        if "host" in body and body["host"]:
            target.host = body["host"]
        if "name" in body and body["name"].strip():
            target.name = body["name"].strip()
        if "model" in body:
            target.model = normalize_model(body["model"])

        save_inverters(configs)
        stop_inverter_tasks(name)

    writer = VictoriaMetricsWriter(VICTORIAMETRICS_URL)
    start_inverter_tasks(target, writer)
    return {"ok": True}


def resolve_inverter_name(raw_name: Optional[str], active_names: List[str]) -> Optional[str]:
    """Map raw metric labels (including legacy 'system' or whitespace variants) to active inverters."""
    if not active_names:
        return raw_name or "unknown"
    if not raw_name:
        return active_names[0]
    if raw_name in active_names:
        return raw_name
    if raw_name.lower() in ("system", "unknown", "default"):
        return active_names[0]
    norm_raw = raw_name.replace(" ", "").lower()
    for name in active_names:
        if name.replace(" ", "").lower() == norm_raw:
            return name
    return None


@app.get("/api/today")
async def get_today():
    try:
        configs = load_inverters()
        active_names = [c.name for c in configs]

        now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
        start_24h_utc = now_utc - timedelta(hours=24)
        start_ts = int(start_24h_utc.timestamp())
        query_url = f"{VICTORIAMETRICS_URL}/api/v1/query_range"

        energy_params = {"query": "fronius_daily_energy_watthours", "start": start_ts, "end": int(now_utc.timestamp()), "step": "15m"}
        total_params = {"query": "fronius_total_energy_watthours", "start": start_ts, "end": int(now_utc.timestamp()), "step": "15m"}
        power_params = {"query": "avg_over_time(fronius_power_watts[15m])", "start": start_ts, "end": int(now_utc.timestamp()), "step": "15m"}

        async with httpx.AsyncClient() as client:
            e_res, t_res, p_res = await asyncio.gather(
                client.get(query_url, params=energy_params),
                client.get(query_url, params=total_params),
                client.get(query_url, params=power_params),
            )
            e_res.raise_for_status()
            t_res.raise_for_status()
            p_res.raise_for_status()
            e_data, t_data, p_data = e_res.json(), t_res.json(), p_res.json()

        # Compute 15-minute energy deltas (kWh) for each inverter.
        # total_energy is continuous across midnight and available on all inverters.
        e_series = {name: {} for name in active_names}

        # 1. Primary: 15-minute deltas from total_energy
        if t_data.get("status") == "success":
            for result in t_data.get("data", {}).get("result", []):
                raw_name = (result.get("metric") or {}).get("inverter", "")
                name = resolve_inverter_name(raw_name, active_names)
                if not name:
                    continue
                if name not in e_series:
                    e_series[name] = {}
                values = result.get("values", [])
                for i in range(1, len(values)):
                    ts, curr, prev = int(values[i][0]), float(values[i][1]), float(values[i - 1][1])
                    kwh = (curr - prev) / 1000.0
                    if 0 <= kwh < 50:
                        e_series[name][ts] = kwh

        # 2. Daily_energy deltas fallback (if any total_energy interval was missing)
        if e_data.get("status") == "success":
            for result in e_data.get("data", {}).get("result", []):
                raw_name = (result.get("metric") or {}).get("inverter", "")
                name = resolve_inverter_name(raw_name, active_names)
                if not name:
                    continue
                if name not in e_series:
                    e_series[name] = {}
                values = result.get("values", [])
                for i in range(1, len(values)):
                    ts, curr, prev = int(values[i][0]), float(values[i][1]), float(values[i - 1][1])
                    kwh = (curr - prev) / 1000.0
                    if 0 <= kwh < 50:
                        e_series[name][ts] = max(e_series[name].get(ts, 0.0), kwh)

        p_series = {name: {} for name in active_names}
        if p_data.get("status") == "success":
            for result in p_data.get("data", {}).get("result", []):
                raw_name = (result.get("metric") or {}).get("inverter", "")
                name = resolve_inverter_name(raw_name, active_names)
                if not name:
                    continue
                if name not in p_series:
                    p_series[name] = {}
                for v in result.get("values", []):
                    ts = int(v[0])
                    val = round(float(v[1]), 0)
                    p_series[name][ts] = max(p_series[name].get(ts, 0.0), val)

        all_ts_set = set()
        for d in e_series.values():
            all_ts_set.update(d)
        for d in p_series.values():
            all_ts_set.update(d)
        all_ts = sorted(all_ts_set)

        series_out = []
        for name in active_names:
            per15 = e_series.get(name, {})
            ppts = p_series.get(name, {})
            hourly, cur = {}, 0.0
            for ts in all_ts:
                cur += per15.get(ts, 0.0)
                if datetime.fromtimestamp(ts).minute == 0:
                    hourly[ts - 1800] = round(cur, 2)
                    cur = 0.0
            if cur > 0 and all_ts:
                hourly[all_ts[-1]] = round(cur, 2)

            series_out.append({
                "name": name,
                "energy_kwh": [hourly.get(ts) for ts in all_ts],
                "power_kw": [round(ppts.get(ts, 0.0) / 1000, 2) for ts in all_ts],
            })

        times = [datetime.fromtimestamp(ts).strftime("%H:%M") for ts in all_ts]
        return {"times": times, "series": series_out}
    except Exception as e:
        logger.error(f"Failed to fetch today data: {e}")
        return {"times": [], "series": [], "error": str(e)}


@app.get("/api/history/7days")
async def get_7day_history():
    try:
        configs = load_inverters()
        active_names = [c.name for c in configs]

        now = datetime.now()
        days_list = []
        for i in range(6, -1, -1):
            day = (now - timedelta(days=i)).replace(hour=0, minute=0, second=0, microsecond=0)
            days_list.append({"date": day.strftime("%a %d"), "start_ts": int(day.timestamp())})

        query_url = f"{VICTORIAMETRICS_URL}/api/v1/query_range"
        params = {"query": "fronius_daily_energy_watthours", "start": days_list[0]["start_ts"], "end": int(now.timestamp()), "step": "15m"}
        total_params = {"query": "fronius_total_energy_watthours", "start": days_list[0]["start_ts"], "end": int(now.timestamp()), "step": "15m"}

        async with httpx.AsyncClient() as client:
            resp, t_resp = await asyncio.gather(
                client.get(query_url, params=params),
                client.get(query_url, params=total_params),
            )
            resp.raise_for_status()
            t_resp.raise_for_status()
            data, t_data = resp.json(), t_resp.json()

        per_inverter = {name: {} for name in active_names}
        if data.get("status") == "success":
            for result in data.get("data", {}).get("result", []):
                raw_name = (result.get("metric") or {}).get("inverter", "")
                name = resolve_inverter_name(raw_name, active_names)
                if not name:
                    continue
                if name not in per_inverter:
                    per_inverter[name] = {}
                for v in result.get("values", []):
                    ts, wh = int(v[0]), float(v[1])
                    day_key = datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
                    per_inverter[name][day_key] = max(per_inverter[name].get(day_key, 0.0), wh)

        # Merge total_energy max-min per day
        if t_data.get("status") == "success":
            for result in t_data.get("data", {}).get("result", []):
                raw_name = (result.get("metric") or {}).get("inverter", "")
                name = resolve_inverter_name(raw_name, active_names)
                if not name:
                    continue
                if name not in per_inverter:
                    per_inverter[name] = {}

                totals_by_day = {}
                for v in result.get("values", []):
                    ts, wh = int(v[0]), float(v[1])
                    day_key = datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
                    if day_key not in totals_by_day:
                        totals_by_day[day_key] = []
                    totals_by_day[day_key].append(wh)

                for day_key, vals in totals_by_day.items():
                    if len(vals) >= 2:
                        diff = max(vals) - min(vals)
                        if diff > 0:
                            per_inverter[name][day_key] = max(per_inverter[name].get(day_key, 0.0), diff)

        labels = [d["date"] for d in days_list]
        series_out = []
        for name in active_names:
            by_day = per_inverter.get(name, {})
            kwhs = []
            for d in days_list:
                day_key = datetime.fromtimestamp(d["start_ts"]).strftime("%Y-%m-%d")
                kwhs.append(round(by_day.get(day_key, 0.0) / 1000, 2))
            series_out.append({"name": name, "kwh": kwhs})

        return {"days": labels, "series": series_out}
    except Exception as e:
        logger.error(f"Failed to fetch 7-day history: {e}")
        return {"days": [], "series": [], "error": str(e)}


@app.get("/api/metrics-log")
async def get_metrics_log():
    return {"metrics": metrics_log}


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    try:
        while True:
            configs = load_inverters()
            mqtt_any = any(mqtt_publishers.get(c.name) and mqtt_publishers[c.name].connected for c in configs)
            mqtt_status = "connected" if mqtt_any else ("disabled" if not MQTT_HOST else "disconnected")
            await websocket.send_json({
                "inverters": [{"name": c.name, "host": c.host, "data": inverters_data.get(c.name, {})} for c in configs],
                "metrics_log": metrics_log,
                "mqtt_status": mqtt_status,
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            })
            await asyncio.sleep(REALTIME_INTERVAL)
    except Exception:
        await websocket.close()


# ---------------------------------------------------------------------------
# Startup / Shutdown
# ---------------------------------------------------------------------------
@app.on_event("startup")
async def startup_event():
    writer = VictoriaMetricsWriter(VICTORIAMETRICS_URL)
    configs = load_inverters()

    logger.info("Starting fronius2vim")
    logger.info(f"Inverter config file: {INVERTERS_FILE}")
    logger.info(f"VictoriaMetrics URL: {VICTORIAMETRICS_URL}")
    logger.info(f"Realtime interval: {REALTIME_INTERVAL}s  |  Energy interval: {ENERGY_INTERVAL}s")
    if MQTT_HOST:
        logger.info(f"MQTT broker: {MQTT_HOST}:{MQTT_PORT}")
    else:
        logger.info("MQTT broker: disabled")

    for cfg in configs:
        start_inverter_tasks(cfg, writer)


@app.on_event("shutdown")
async def shutdown_event():
    for name in list(mqtt_publishers.keys()):
        stop_inverter_tasks(name)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=WEB_PORT)
