#!/usr/bin/env python3
"""
fronius2vim - Fronius Inverter to VictoriaMetrics Collector
Polls Fronius Solar API and writes metrics to VictoriaMetrics
"""

import asyncio
import logging
import os
from datetime import datetime
from typing import Dict, Optional

import httpx
from fastapi import FastAPI, WebSocket
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse

# Configuration
# Default IPs hardcoded for this deployment
FRONIUS_HOST = os.getenv("FRONIUS_HOST", "172.20.203.100")
VICTORIAMETRICS_URL = os.getenv("VICTORIAMETRICS_URL", "http://172.20.204.22:8428")
REALTIME_INTERVAL = int(os.getenv("REALTIME_INTERVAL", "10"))
ENERGY_INTERVAL = int(os.getenv("ENERGY_INTERVAL", "900"))
WEB_PORT = int(os.getenv("WEB_PORT", "8080"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

# Setup logging
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL.upper()),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("fronius2vim")

# FastAPI app
app = FastAPI(title="fronius2vim", version="1.0.0")

# Latest data cache for WebSocket
latest_data = {
    "power": 0,
    "ac_voltage": 0,
    "dc_voltage": 0,
    "ac_current": 0,
    "dc_current": 0,
    "daily_energy": 0,
    "timestamp": None,
}


class FroniusCollector:
    """Collects data from Fronius inverter API"""

    def __init__(self, host: str):
        self.host = host
        self.base_url = f"http://{host}/solar_api/v1"
        self.client = httpx.AsyncClient(timeout=10.0)

    async def get_realtime_data(self) -> Optional[Dict]:
        """Get real-time inverter data (PAC, voltages, currents)"""
        url = f"{self.base_url}/GetInverterRealtimeData.cgi"
        params = {"Scope": "System", "DataCollection": "CumulationInverterData"}

        try:
            response = await self.client.get(url, params=params)
            response.raise_for_status()
            data = response.json()

            body = data.get("Body", {}).get("Data", {})

            return {
                "power": body.get("PAC", {}).get("Values", {}).get("1", 0),
                "ac_voltage": body.get("UAC", {}).get("Values", {}).get("1", 0),
                "dc_voltage": body.get("UDC", {}).get("Values", {}).get("1", 0),
                "ac_current": body.get("IAC", {}).get("Values", {}).get("1", 0),
                "dc_current": body.get("IDC", {}).get("Values", {}).get("1", 0),
            }
        except Exception as e:
            logger.error(f"Failed to get realtime data: {e}")
            return None

    async def get_energy_data(self) -> Optional[Dict]:
        """Get energy counters (DAY, YEAR, TOTAL)"""
        url = f"{self.base_url}/GetInverterRealtimeData.cgi"
        params = {"Scope": "System", "DataCollection": "CumulationInverterData"}

        try:
            response = await self.client.get(url, params=params)
            response.raise_for_status()
            data = response.json()

            body = data.get("Body", {}).get("Data", {})

            return {
                "daily": body.get("DAY_ENERGY", {}).get("Values", {}).get("1", 0),
                "yearly": body.get("YEAR_ENERGY", {}).get("Values", {}).get("1", 0),
                "total": body.get("TOTAL_ENERGY", {}).get("Values", {}).get("1", 0),
            }
        except Exception as e:
            logger.error(f"Failed to get energy data: {e}")
            return None


class VictoriaMetricsWriter:
    """Writes metrics to VictoriaMetrics using Prometheus remote write"""

    def __init__(self, url: str):
        self.url = url.rstrip("/")
        self.client = httpx.AsyncClient(timeout=10.0)

    async def write_metric(
        self, name: str, value: float, labels: Optional[Dict[str, str]] = None
    ):
        """Write a single metric to VictoriaMetrics"""
        if labels is None:
            labels = {}

        # Build Prometheus format: metric_name{label1="value1"} value timestamp
        label_str = ",".join([f'{k}="{v}"' for k, v in labels.items()])
        if label_str:
            metric_line = f"{name}{{{label_str}}} {value}"
        else:
            metric_line = f"{name} {value}"

        import_url = f"{self.url}/api/v1/import/prometheus"

        try:
            response = await self.client.post(
                import_url,
                content=metric_line.encode(),
                headers={"Content-Type": "text/plain"},
            )
            response.raise_for_status()
            logger.debug(f"Wrote metric: {metric_line}")
        except Exception as e:
            logger.error(f"Failed to write metric to VictoriaMetrics: {e}")

    async def write_realtime_metrics(self, data: Dict):
        """Write real-time metrics"""
        await self.write_metric(
            "fronius_power_watts", data["power"], {"inverter": "symo"}
        )
        await self.write_metric(
            "fronius_ac_voltage_volts", data["ac_voltage"], {"inverter": "symo"}
        )
        await self.write_metric(
            "fronius_dc_voltage_volts", data["dc_voltage"], {"inverter": "symo"}
        )
        await self.write_metric(
            "fronius_ac_current_amperes", data["ac_current"], {"inverter": "symo"}
        )
        await self.write_metric(
            "fronius_dc_current_amperes", data["dc_current"], {"inverter": "symo"}
        )

    async def write_energy_metrics(self, data: Dict):
        """Write energy counter metrics"""
        await self.write_metric(
            "fronius_daily_energy_watthours", data["daily"], {"inverter": "symo"}
        )
        await self.write_metric(
            "fronius_yearly_energy_watthours", data["yearly"], {"inverter": "symo"}
        )
        await self.write_metric(
            "fronius_total_energy_watthours", data["total"], {"inverter": "symo"}
        )


async def realtime_collector(
    collector: FroniusCollector, writer: VictoriaMetricsWriter
):
    """Background task: collect real-time data every 10 seconds"""
    while True:
        try:
            data = await collector.get_realtime_data()
            if data:
                await writer.write_realtime_metrics(data)
                # Update cache for WebSocket
                latest_data.update(
                    {
                        "power": data["power"],
                        "ac_voltage": data["ac_voltage"],
                        "dc_voltage": data["dc_voltage"],
                        "ac_current": data["ac_current"],
                        "dc_current": data["dc_current"],
                        "timestamp": datetime.now().isoformat(),
                    }
                )
                logger.info(f"Realtime data: Power={data['power']}W")
        except Exception as e:
            logger.error(f"Error in realtime collector: {e}")

        await asyncio.sleep(REALTIME_INTERVAL)


async def energy_collector(collector: FroniusCollector, writer: VictoriaMetricsWriter):
    """Background task: collect energy data every 15 minutes"""
    while True:
        try:
            data = await collector.get_energy_data()
            if data:
                await writer.write_energy_metrics(data)
                latest_data["daily_energy"] = data["daily"]
                logger.info(f"Energy data: Daily={data['daily']}Wh")
        except Exception as e:
            logger.error(f"Error in energy collector: {e}")

        await asyncio.sleep(ENERGY_INTERVAL)


# HTML Dashboard
HTML_DASHBOARD = """
<!DOCTYPE html>
<html>
<head>
    <title>fronius2vim Dashboard</title>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            max-width: 800px;
            margin: 0 auto;
            padding: 20px;
            background: #1a1a2e;
            color: #eee;
        }
        h1 { color: #4ecca3; text-align: center; }
        .status { text-align: center; margin: 20px 0; }
        .metrics {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 15px;
            margin-top: 30px;
        }
        .metric-card {
            background: #16213e;
            border-radius: 10px;
            padding: 20px;
            text-align: center;
        }
        .metric-value {
            font-size: 2.5em;
            font-weight: bold;
            color: #4ecca3;
        }
        .metric-label {
            color: #888;
            margin-top: 5px;
        }
        .timestamp {
            text-align: center;
            color: #666;
            margin-top: 20px;
            font-size: 0.9em;
        }
        .connected { color: #4ecca3; }
        .disconnected { color: #e74c3c; }
        .chart-container {
            background: #16213e;
            border-radius: 10px;
            padding: 20px;
            margin-top: 30px;
        }
        .chart-title {
            color: #4ecca3;
            text-align: center;
            margin-bottom: 15px;
            font-size: 1.2em;
        }
    </style>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
</head>
<body>
    <h1>☀️ fronius2vim Dashboard</h1>
    <div class="status">
        Inverter: <span id="status" class="disconnected">Connecting...</span>
    </div>
    
    <div class="metrics">
        <div class="metric-card">
            <div class="metric-value" id="power">--</div>
            <div class="metric-label">Current Power (W)</div>
        </div>
        <div class="metric-card">
            <div class="metric-value" id="daily_energy">--</div>
            <div class="metric-label">Daily Energy (Wh)</div>
        </div>
        <div class="metric-card">
            <div class="metric-value" id="ac_voltage">--</div>
            <div class="metric-label">AC Voltage (V)</div>
        </div>
        <div class="metric-card">
            <div class="metric-value" id="dc_voltage">--</div>
            <div class="metric-label">DC Voltage (V)</div>
        </div>
        <div class="metric-card">
            <div class="metric-value" id="ac_current">--</div>
            <div class="metric-label">AC Current (A)</div>
        </div>
        <div class="metric-card">
            <div class="metric-value" id="dc_current">--</div>
            <div class="metric-label">DC Current (A)</div>
        </div>
    </div>
    
    <div class="timestamp" id="timestamp">--</div>

    <div class="chart-container">
        <div class="chart-title">⚡ Energy Generation & Power Output Today</div>
        <canvas id="combinedChart"></canvas>
    </div>

    <script>
        let ws = null;
        
        // Chart.js setup for combined chart (bars + line with dual y-axes)
        const ctx = document.getElementById('combinedChart').getContext('2d');
        const combinedChart = new Chart(ctx, {
            type: 'bar',
            data: {
                labels: [],
                datasets: [
                    {
                        label: 'kWh Generated (bars)',
                        data: [],
                        backgroundColor: 'rgba(78, 204, 163, 0.7)',
                        borderColor: 'rgba(78, 204, 163, 1)',
                        borderWidth: 1,
                        yAxisID: 'y'
                    },
                    {
                        label: 'Power (W) - line',
                        data: [],
                        type: 'line',
                        borderColor: 'rgba(255, 206, 86, 1)',
                        backgroundColor: 'rgba(255, 206, 86, 0.1)',
                        borderWidth: 2,
                        tension: 0.4,
                        pointRadius: 2,
                        yAxisID: 'y1'
                    }
                ]
            },
            options: {
                responsive: true,
                maintainAspectRatio: true,
                scales: {
                    y: {
                        type: 'linear',
                        display: true,
                        position: 'left',
                        beginAtZero: true,
                        grid: { color: 'rgba(255, 255, 255, 0.1)' },
                        ticks: { color: '#888' },
                        title: {
                            display: true,
                            text: 'kWh',
                            color: '#4ecca3'
                        }
                    },
                    y1: {
                        type: 'linear',
                        display: true,
                        position: 'right',
                        beginAtZero: true,
                        grid: { display: false },
                        ticks: { color: '#ffce56' },
                        title: {
                            display: true,
                            text: 'Watts',
                            color: '#ffce56'
                        }
                    },
                    x: {
                        grid: { color: 'rgba(255, 255, 255, 0.1)' },
                        ticks: { color: '#888', maxRotation: 45 }
                    }
                },
                plugins: {
                    legend: {
                        labels: { color: '#eee' }
                    }
                }
            }
        });

        // Fetch combined data
        async function fetchCombinedData() {
            try {
                const [historyRes, powerRes] = await Promise.all([
                    fetch('/api/history'),
                    fetch('/api/power')
                ]);
                
                const historyData = await historyRes.json();
                const powerData = await powerRes.json();
                
                // Use energy data as the base (15min intervals)
                if (historyData.intervals && historyData.intervals.length > 0) {
                    combinedChart.data.labels = historyData.intervals.map(i => i.time);
                    combinedChart.data.datasets[0].data = historyData.intervals.map(i => i.kwh);
                }
                
                // Add power data
                if (powerData.points && powerData.points.length > 0) {
                    combinedChart.data.datasets[1].data = powerData.points.map(p => p.power);
                }
                
                combinedChart.update();
            } catch (error) {
                console.error('Failed to fetch combined data:', error);
            }
        }

        // Fetch data on load and every 5 minutes
        fetchCombinedData();
        setInterval(fetchCombinedData, 300000);
        
        function connect() {
            ws = new WebSocket(`ws://${window.location.host}/ws`);
            
            ws.onopen = () => {
                document.getElementById('status').textContent = 'Connected';
                document.getElementById('status').className = 'connected';
            };
            
            ws.onmessage = (event) => {
                const data = JSON.parse(event.data);
                if (data.power !== undefined) {
                    document.getElementById('power').textContent = data.power.toLocaleString();
                }
                if (data.daily_energy !== undefined) {
                    document.getElementById('daily_energy').textContent = data.daily_energy.toLocaleString();
                }
                if (data.ac_voltage !== undefined) {
                    document.getElementById('ac_voltage').textContent = data.ac_voltage.toFixed(1);
                }
                if (data.dc_voltage !== undefined) {
                    document.getElementById('dc_voltage').textContent = data.dc_voltage.toFixed(1);
                }
                if (data.ac_current !== undefined) {
                    document.getElementById('ac_current').textContent = data.ac_current.toFixed(2);
                }
                if (data.dc_current !== undefined) {
                    document.getElementById('dc_current').textContent = data.dc_current.toFixed(2);
                }
                if (data.timestamp) {
                    document.getElementById('timestamp').textContent = 'Last update: ' + data.timestamp;
                }
            };
            
            ws.onclose = () => {
                document.getElementById('status').textContent = 'Disconnected - Retrying...';
                document.getElementById('status').className = 'disconnected';
                setTimeout(connect, 5000);
            };
            
            ws.onerror = (error) => {
                console.error('WebSocket error:', error);
            };
        }
        
        connect();
    </script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    """Serve the dashboard HTML"""
    return HTML_DASHBOARD


@app.get("/api/data")
async def get_data():
    """REST API endpoint for current data"""
    return latest_data


@app.get("/api/history")
async def get_history():
    """Query VictoriaMetrics for today's energy data per 15min intervals"""
    try:
        # Calculate today's start timestamp (midnight)
        now = datetime.now()
        start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
        start_timestamp = int(start_of_day.timestamp())

        # Query VictoriaMetrics for daily energy data today
        query_url = f"{VICTORIAMETRICS_URL}/api/v1/query_range"
        params = {
            "query": "fronius_daily_energy_watthours",
            "start": start_timestamp,
            "end": int(now.timestamp()),
            "step": "15m",
        }

        async with httpx.AsyncClient() as client:
            response = await client.get(query_url, params=params)
            response.raise_for_status()
            data = response.json()

            if data.get("status") == "success" and data.get("data", {}).get("result"):
                result = data["data"]["result"][0]
                values = result.get("values", [])

                # Calculate kWh per 15min interval
                intervals = []
                for i in range(1, len(values)):
                    timestamp = int(values[i][0])
                    current_wh = float(values[i][1])
                    previous_wh = float(values[i - 1][1])
                    kwh_generated = (current_wh - previous_wh) / 1000

                    # Skip negative values (inverter counter reset at midnight)
                    if kwh_generated >= 0:
                        intervals.append(
                            {
                                "time": datetime.fromtimestamp(timestamp).strftime(
                                    "%H:%M"
                                ),
                                "kwh": round(kwh_generated, 3),
                            }
                        )

                return {"intervals": intervals}

        return {"intervals": []}
    except Exception as e:
        logger.error(f"Failed to fetch history: {e}")
        return {"intervals": [], "error": str(e)}


@app.get("/api/power")
async def get_power():
    """Query VictoriaMetrics for today's power data (watts)"""
    try:
        # Calculate today's start timestamp (midnight)
        now = datetime.now()
        start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
        start_timestamp = int(start_of_day.timestamp())

        # Query VictoriaMetrics for power data today (avg over 15min to match energy intervals)
        query_url = f"{VICTORIAMETRICS_URL}/api/v1/query_range"
        params = {
            "query": "avg_over_time(fronius_power_watts[15m])",
            "start": start_timestamp,
            "end": int(now.timestamp()),
            "step": "15m",
        }

        async with httpx.AsyncClient() as client:
            response = await client.get(query_url, params=params)
            response.raise_for_status()
            data = response.json()

            if data.get("status") == "success" and data.get("data", {}).get("result"):
                result = data["data"]["result"][0]
                values = result.get("values", [])

                points = []
                for value in values:
                    timestamp = int(value[0])
                    power = float(value[1])
                    points.append(
                        {
                            "time": datetime.fromtimestamp(timestamp).strftime("%H:%M"),
                            "power": round(power, 0),
                        }
                    )

                return {"points": points}

        return {"points": []}
    except Exception as e:
        logger.error(f"Failed to fetch power: {e}")
        return {"points": [], "error": str(e)}


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket endpoint for real-time updates"""
    await websocket.accept()
    try:
        while True:
            await websocket.send_json(latest_data)
            await asyncio.sleep(REALTIME_INTERVAL)
    except Exception:
        await websocket.close()


@app.on_event("startup")
async def startup_event():
    """Start background collectors on app startup"""
    collector = FroniusCollector(FRONIUS_HOST)
    writer = VictoriaMetricsWriter(VICTORIAMETRICS_URL)

    logger.info(f"Starting fronius2vim")
    logger.info(f"Fronius host: {FRONIUS_HOST}")
    logger.info(f"VictoriaMetrics URL: {VICTORIAMETRICS_URL}")
    logger.info(f"Realtime interval: {REALTIME_INTERVAL}s")
    logger.info(f"Energy interval: {ENERGY_INTERVAL}s")

    # Start background tasks
    asyncio.create_task(realtime_collector(collector, writer))
    asyncio.create_task(energy_collector(collector, writer))


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=WEB_PORT)
