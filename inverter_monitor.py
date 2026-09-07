"""
inverter_monitor - Automatic Fronius inverter failover

On startup: scans discovery_subnet (172.20.204.0/24), logs all live hosts.
During runtime: tracks consecutive failed API calls to the current inverter.
When the inverter goes offline: scans the discovery subnet for Fronius devices,
switches the collector to the first one found, and persists the new IP.
"""

import asyncio
import ipaddress
import logging
import os
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Callable, Awaitable

import httpx

logger = logging.getLogger("fronius2vim.monitor")


class MonitorState(str, Enum):
    MONITORING = "monitoring"
    SEARCHING = "searching"
    RECOVERED = "recovered"


@dataclass
class InverterMonitor:
    # Subnets
    primary_subnet: str  # e.g. "172.20.203.0/24" - where the inverter normally lives
    discovery_subnet: str  # e.g. "172.20.204.0/24" - where to scan if it goes offline

    # Thresholds
    fail_threshold: int = 3  # consecutive failures before triggering scan
    scan_interval: float = 10.0  # seconds between ARP probes during search
    recover_check_interval: float = 60.0  # seconds between checks of original IP after recovery
    discovery_scan_retries: int = 2  # how many times to scan discovery subnet

    # State
    state: MonitorState = MonitorState.MONITORING
    consecutive_failures: int = 0
    last_known_hosts: list = field(default_factory=list)
    discovered_inverters: list = field(default_factory=list)
    switched_at: Optional[str] = None
    original_host: Optional[str] = None

    def __post_init__(self):
        self.original_host = None

    def get_status(self) -> dict:
        return {
            "monitor_state": self.state.value,
            "consecutive_failures": self.consecutive_failures,
            "last_known_hosts": self.last_known_hosts,
            "discovered_inverters": self.discovered_inverters,
            "switched_at": self.switched_at,
            "original_host": self.original_host,
        }


def arp_scan(subnet: str, timeout: float = 3.0) -> list[dict]:
    """ARP-scan a subnet, return list of {ip, mac} for live hosts."""
    from scapy.all import ARP, Ether, srp, conf

    conf.verb = 0
    hosts = []
    try:
        ans, _ = srp(
            Ether(dst="ff:ff:ff:ff:ff:ff") / ARP(pdst=subnet),
            timeout=timeout,
            verbose=0,
        )
        for _, rcv in ans:
            hosts.append({"ip": rcv.psrc, "mac": rcv.hwsrc})
    except Exception as e:
        logger.error(f"ARP scan failed on {subnet}: {e}")
    return hosts


async def is_fronius_device(ip: str, timeout: float = 3.0) -> Optional[dict]:
    """Check if an IP hosts a Fronius Solar API. Returns device info or None."""
    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            resp = await client.get(
                f"http://{ip}/solar_api/v1/GetInfo.cgi",
                params={"Scope": "System"},
            )
            if resp.status_code == 200:
                data = resp.json()
                body = data.get("Body", {}).get("Data", {})
                return {
                    "ip": ip,
                    "serial": body.get("Serial", "unknown"),
                    "model": body.get("Model", "unknown"),
                    "software": body.get("SoftwareVersion", "unknown"),
                }
        except Exception:
            pass
    return None


async def verify_inverter(ip: str, timeout: float = 5.0) -> bool:
    """Quick check: can we reach the inverter's realtime data endpoint?"""
    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            resp = await client.get(
                f"http://{ip}/solar_api/v1/GetInverterRealtimeData.cgi",
                params={"Scope": "System", "DataCollection": "CumulationInverterData"},
            )
            return resp.status_code == 200
        except Exception:
            return False


def persist_new_host(new_host: str, env_path: Optional[str] = None):
    """Append or update FRONIUS_HOST in the env file for future restarts."""
    env_path = env_path or default_env_path()
    lines = []
    found = False
    if os.path.exists(env_path):
        with open(env_path, "r") as f:
            for line in f:
                if line.strip().startswith("FRONIUS_HOST="):
                    lines.append(f"FRONIUS_HOST={new_host}\n")
                    found = True
                else:
                    lines.append(line)
    if not found:
        lines.append(f"FRONIUS_HOST={new_host}\n")
    with open(env_path, "w") as f:
        f.writelines(lines)
    logger.info(f"Persisted FRONIUS_HOST={new_host} to {env_path}")


def load_persisted_host(env_path: Optional[str] = None) -> Optional[str]:
    """Read the last persisted FRONIUS_HOST from the env file, if any."""
    env_path = env_path or default_env_path()
    if not os.path.exists(env_path):
        return None
    try:
        with open(env_path, "r") as f:
            for line in f:
                line = line.strip()
                if line.startswith("FRONIUS_HOST="):
                    host = line.split("=", 1)[1].strip().strip("\"'")
                    return host if host else None
    except OSError as e:
        logger.warning(f"Could not read persisted host from {env_path}: {e}")
    return None


def default_env_path() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), ".fronius_host")


async def monitor_loop(
    monitor: InverterMonitor,
    get_current_host: Callable[[], str],
    set_current_host: Callable[[str], Awaitable[None]],
    env_path: Optional[str] = None,
):
    """
    Background task that:
    1. On first run: ARP-scans the discovery subnet, logs devices
    2. Continuously: if current inverter fails N times, scan & switch
    3. After switching: periodically check if original came back
    """
    # Wait for app to be fully started
    await asyncio.sleep(5)

    initial_host = get_current_host()
    monitor.original_host = initial_host

    # --- Phase 1: Initial discovery scan ---
    logger.info(f"[monitor] Initial discovery scan of {monitor.discovery_subnet}")
    hosts = await asyncio.get_event_loop().run_in_executor(
        None, arp_scan, monitor.discovery_subnet
    )
    monitor.last_known_hosts = hosts
    logger.info(f"[monitor] Found {len(hosts)} live hosts on {monitor.discovery_subnet}:")
    for h in hosts:
        logger.info(f"  {h['ip']} ({h['mac']})")

    # Probe each host for Fronius API
    frdevices = []
    for h in hosts:
        info = await is_fronius_device(h["ip"])
        if info:
            frdevices.append(info)
            logger.info(f"  Fronius device at {h['ip']}: {info['model']} s/n {info['serial']}")
    monitor.discovered_inverters = frdevices

    if frdevices:
        logger.info(f"[monitor] {len(frdevices)} Fronius device(s) found on discovery subnet")
    else:
        logger.info("[monitor] No Fronius devices found on discovery subnet (may appear later)")

    # --- Phase 2: Runtime monitoring ---
    logger.info(f"[monitor] Monitoring inverter at {initial_host}")

    while True:
        await asyncio.sleep(monitor.scan_interval)

        current = get_current_host()
        alive = await verify_inverter(current)

        if alive:
            if monitor.consecutive_failures > 0:
                logger.info(f"[monitor] Inverter at {current} is back online")
            monitor.consecutive_failures = 0

            if monitor.state == MonitorState.RECOVERED and current == monitor.original_host:
                logger.info(f"[monitor] Original inverter {monitor.original_host} confirmed recovered")
                monitor.state = MonitorState.MONITORING
                monitor.switched_at = None

            continue

        monitor.consecutive_failures += 1
        logger.warning(
            f"[monitor] Inverter at {current} unreachable "
            f"({monitor.consecutive_failures}/{monitor.fail_threshold})"
        )

        if monitor.consecutive_failures < monitor.fail_threshold:
            continue

        # --- Threshold exceeded: start searching ---
        if monitor.state == MonitorState.SEARCHING:
            continue  # already searching

        monitor.state = MonitorState.SEARCHING
        logger.warning(f"[monitor] Inverter offline! Scanning {monitor.discovery_subnet} for replacement...")

        new_inverter = None
        try:
            for attempt in range(monitor.discovery_scan_retries):
                hosts = await asyncio.get_event_loop().run_in_executor(
                    None, arp_scan, monitor.discovery_subnet
                )
                monitor.last_known_hosts = hosts
                logger.info(f"[monitor] Scan attempt {attempt + 1}: {len(hosts)} hosts found")

                for h in hosts:
                    if h["ip"] == current:
                        continue  # skip the one that's already failed
                    info = await is_fronius_device(h["ip"])
                    if info:
                        logger.info(f"[monitor] Found Fronius at {h['ip']}: {info['model']}")
                        if await verify_inverter(h["ip"]):
                            new_inverter = h["ip"]
                            break
                    else:
                        logger.debug(f"  {h['ip']} is not a Fronius device")

                if new_inverter:
                    break
                if attempt < monitor.discovery_scan_retries - 1:
                    logger.info(
                        f"[monitor] No usable inverter found, retrying in {monitor.scan_interval}s..."
                    )
                    await asyncio.sleep(monitor.scan_interval)
        except Exception as e:
            logger.error(f"[monitor] Error during discovery scan: {e}")
            monitor.state = MonitorState.MONITORING
            monitor.consecutive_failures = 0
            continue

        if new_inverter:
            logger.warning(f"[monitor] SWITCHING inverter: {current} -> {new_inverter}")
            try:
                await set_current_host(new_inverter)
                persist_new_host(new_inverter, env_path=env_path)
                monitor.switched_at = time.strftime("%Y-%m-%d %H:%M:%S")
                monitor.state = MonitorState.RECOVERED
                monitor.consecutive_failures = 0

                # Keep checking if the original comes back
                asyncio.create_task(_watch_original(monitor, get_current_host, set_current_host))
            except Exception as e:
                logger.error(f"[monitor] Failed to switch host to {new_inverter}: {e}")
                monitor.state = MonitorState.MONITORING
                monitor.consecutive_failures = 0
        else:
            logger.error("[monitor] No Fronius devices found on discovery subnet. Will retry.")
            monitor.state = MonitorState.MONITORING
            monitor.consecutive_failures = 0


async def _watch_original(
    monitor: InverterMonitor,
    get_current_host: Callable[[], str],
    set_current_host: Callable[[str], Awaitable[None]],
):
    """After failover, periodically check if the original inverter came back and switch back."""
    logger.info(f"[monitor] Watching for original inverter {monitor.original_host} to return...")
    while monitor.state == MonitorState.RECOVERED:
        await asyncio.sleep(monitor.recover_check_interval)
        if await verify_inverter(monitor.original_host, timeout=5.0):
            logger.info(f"[monitor] Original inverter {monitor.original_host} is back!")
            try:
                await set_current_host(monitor.original_host)
                logger.info(
                    f"[monitor] SWITCHING BACK: {get_current_host()} -> {monitor.original_host}"
                )
            except Exception as e:
                logger.error(f"[monitor] Failed to switch back to {monitor.original_host}: {e}")
            monitor.state = MonitorState.MONITORING
            monitor.switched_at = None
            return
    logger.info("[monitor] Stopped watching for original (state changed)")
